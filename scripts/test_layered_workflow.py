"""End-to-end offline cleanup and checkpoint tests using real request budgets."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import workflow
import context_budget


class LayeredWorkflowTests(unittest.TestCase):
    def run_case(self, packet, responder, *, window=50000, management=None, budgeter=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / "review"
        self.calls = []
        config = {"context_window_tokens": window, "context_management": {
            "inline_material_token_limit": 0, "headroom_tokens": 1000, "safety_tokens": 128,
            "compression_trigger_ratio": 0.8, "retain_recent_ratio": 0.4,
            "compaction_max_tokens": 1000, "tool_max_chars": 12000,
            **(management or {}),
        }}
        route = {"model": "auto", "max_tokens": 2000}
        def call(config, packet, directory, **kwargs):
            directory.mkdir()
            entry = copy.deepcopy(kwargs)
            self.calls.append(entry)
            # Mimic persistent request logs; cleanup must never edit old files.
            (directory / "request.json").write_text(json.dumps(entry["chat_messages"]), encoding="utf-8")
            return responder(entry)
        kwargs = dict(review_call=call, route_selector=lambda cfg, at=None: route)
        if budgeter is None:
            return workflow.run_workflow(config, packet, self.root, **kwargs)
        with patch.object(workflow, "plan_budget", side_effect=budgeter):
            return workflow.run_workflow(config, packet, self.root, **kwargs)

    def completed(self):
        return {"status": "completed", "finish_reason": "stop", "content": '{"findings":[]}',
                "parse_ok": True, "parsed_findings": []}

    def tool(self, number, name="read_material", path=None, reasoning=""):
        return {"status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                "reasoning_text": reasoning, "tool_calls": [{"id": "read-%d" % number, "type": "function",
                "function": {"name": name, "arguments": json.dumps({"path": path or ("file%d.py" % number)})}}]}

    def test_pressure_can_be_resolved_without_any_summary_call_and_originals_remain(self):
        packet = {"task": "review", "files": [{"path": "file%d.py" % i, "content": str(i) * 10000} for i in range(1, 7)]}
        def respond(entry):
            self.assertNotEqual(entry["route_override"].get("enable_thinking"), False)
            text = json.dumps(entry["chat_messages"])
            if "context_cleanup" in text:
                self.assertEqual(entry["route_override"]["max_tokens"], 2000)
                return self.completed()
            self.assertLess(len(self.calls), 8)
            return self.tool(len(self.calls))
        result = self.run_case(packet, respond)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["compactions"], 0)
        doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertTrue(doc["history_cleanup_events"])
        event = doc["history_cleanup_events"][0]
        self.assertLess(event["after_tokens"], event["before_tokens"])
        raw = [json.loads(line) for line in (self.root / "tool_results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(raw), len(self.calls) - 1)
        self.assertTrue(all(len(item["result"]["text"]) == 10000 for item in raw))
        self.assertFalse(any("context_cleanup" in item["result"] for item in raw))
        old_requests = [path.read_text(encoding="utf-8") for path in sorted((self.root / "calls").glob("*/request.json"))]
        self.assertTrue(any("1" * 10000 in text and "context_cleanup" not in text for text in old_requests))

    def test_summary_drops_old_history_but_retains_recent_reasoning_and_tool_pairs(self):
        packet = {"task": "review small files", "files": [{"path": "file%d.py" % i, "content": "x=1"} for i in range(1, 9)]}
        seen = []
        def respond(entry):
            messages = entry["chat_messages"]
            if entry["route_override"].get("enable_thinking") is False:
                compact = json.dumps(messages)
                self.assertGreaterEqual(len(seen), 3)
                self.assertIn(seen[0], compact)
                self.assertNotIn(seen[-1], compact)
                return {"status": "completed", "finish_reason": "stop", "content": "checkpoint of older findings"}
            first_user = json.loads(messages[1]["content"])
            if first_user.get("checkpoint_from_this_review"):
                assistants = [m for m in messages if m.get("role") == "assistant"]
                self.assertTrue(assistants)
                self.assertEqual(assistants[-1]["reasoning_content"], seen[-1] + "z" * 3000)
                self.assertNotIn(seen[0], json.dumps(assistants))
                ids = [c["id"] for m in assistants for c in m.get("tool_calls", [])]
                returned = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
                self.assertEqual(ids, returned)
                return self.completed()
            self.assertLess(len(seen), 8)
            seen.append("PRIVATE_REASONING_ROUND_%d:" % (len(seen) + 1))
            return self.tool(len(seen), reasoning=seen[-1] + "z" * 3000)
        result = self.run_case(packet, respond, window=24000, management={"retain_recent_ratio": 0.3})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["compactions"], 1)
        doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertGreater(doc["retained_recent_messages"], 0)
        self.assertEqual(len(doc["checkpoint_history"]), 1)
        self.assertEqual(Path(doc["checkpoint_history"][0]).read_text(encoding="utf-8"), "checkpoint of older findings")
        self.assertNotIn("PRIVATE_REASONING", (self.root / "workflow.json").read_text(encoding="utf-8"))

    def test_over_budget_summary_without_size_progress_is_not_repeated(self):
        def budget(messages, tools, route, config, output_cap=None):
            result = context_budget.plan_budget(messages, tools, route, config, output_cap=output_cap)
            if route.get("enable_thinking") is not False:
                result.update(needs_compaction=True, input_tokens=900000, effective_output_tokens=0)
            return result
        def respond(entry):
            self.assertFalse(entry["route_override"]["enable_thinking"])
            return {"status": "completed", "finish_reason": "stop", "content": "summary with no size progress"}
        result = self.run_case({"task": "review"}, respond, budgeter=budget)
        self.assertEqual(result["status"], "incomplete")
        self.assertIn("did not reduce", result["uncompleted_reason"])
        self.assertEqual(len(self.calls), 1)

    def test_length_keeps_partial_reasoning_and_never_executes_unfinished_tool_calls(self):
        def respond(entry):
            if len(self.calls) == 1:
                partial = self.tool(1, path="missing.py", reasoning="LENGTH_PRIVATE_FINDING")
                partial.update(status="incomplete", finish_reason="length", content="partial finding")
                return partial
            if entry["route_override"].get("enable_thinking") is False:
                text = json.dumps(entry["chat_messages"])
                self.assertIn("LENGTH_PRIVATE_FINDING", text)
                self.assertIn("partial finding", text)
                return {"status": "completed", "finish_reason": "stop", "content": "checkpoint of partial finding"}
            return self.completed()
        result = self.run_case({"task": "review", "files": [{"path": "file1.py", "content": "x=1"}]}, respond)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["coverage"]["tool_delivered_ranges"], [])
        self.assertFalse((self.root / "tool_results.jsonl").exists())
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()
