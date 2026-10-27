"""Offline tests for the local review context workflow; no API/network is used."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from . import workflow
except ImportError:  # pragma: no cover - direct scripts/ invocation
    import workflow


SOURCE = "def calculate(value):\n    return value + 1\n# SOURCE_BODY_SENTINEL\n"
PACKET = {
    "task": "Validate calculate behavior and error handling.",
    "requirements": ["Preserve valid input behavior."],
    "tests": [{"description": "Unit coverage for ordinary inputs."}],
    "files": [{"path": "src/calc.py", "content": SOURCE}],
    "diff": "@@ -1 +1 @@\n-return value\n+return value + 1\n",
}
ROUTE_DS = {
    "local_time": "2026-10-23T23:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "period": "night",
    "model": "deepseek-v4.1-flash-1017",
    "reasoning_effort": "max",
    "enable_thinking": True,
    "max_tokens": 393216,
    "context_window_tokens": 1048576,
}
ROUTE_AUTO = {
    "local_time": "2026-10-23T12:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "period": "day",
    "model": "auto",
    "reasoning_effort": None,
    "enable_thinking": None,
    "max_tokens": 131072,
    "thinking_budget": 262144,
    "output_limit_parameter": "max_tokens",
    "max_input_tokens": 983616,
    "context_window_tokens": 1048576,
}
ROUTE_QWEN = {
    "local_time": "2026-10-23T12:00:00+08:00",
    "timezone": "Asia/Shanghai",
    "period": "day",
    "model": "qwen3.8-flash",
    "reasoning_effort": "max",
    "enable_thinking": True,
    "max_tokens": 131072,
    "thinking_budget": 262144,
    "output_limit_parameter": "max_tokens",
    "max_input_tokens": 983616,
    "context_window_tokens": 1048576,
}
CONFIG = {
    "context_window_tokens": 1048576,
    "context_management": {
        "safety_tokens": 2048,
        "compaction_max_tokens": 16384,
        "max_model_calls": 16,
        "max_compactions": 3,
        "tool_max_chars": 24000,
        "tool_max_lines": 250,
        # Existing state-machine tests intentionally exercise every tool round.
        "inline_material_token_limit": 0,
        "auto_output_reserve_tokens": 393216,
    },
}


def _budget(needs=None, input_tokens=1000, output_cap=None, trigger_tokens=900000, safety_tokens=2048, route=None):
    split = (
        output_cap is None and isinstance(route, dict)
        and route.get("output_limit_parameter") == "max_tokens"
        and isinstance(route.get("max_tokens"), int) and route.get("max_tokens") > 0
        and isinstance(route.get("thinking_budget"), int) and route.get("thinking_budget") > 0
    )
    requested_body = route["max_tokens"] if split else None
    requested_thinking = route["thinking_budget"] if split else None
    requested = output_cap if output_cap is not None else (requested_body + requested_thinking if split else 393216)
    effective = max(0, min(requested, trigger_tokens - input_tokens - safety_tokens))
    effective_body = None
    effective_thinking = None
    if split:
        if effective >= 2:
            effective_body = max(1, (effective * requested_body) // requested)
            effective_body = min(effective_body, requested_body, effective - 1)
            effective_thinking = min(requested_thinking, effective - effective_body)
            if effective_thinking < 1:
                effective_thinking = 1
                effective_body = min(effective_body, effective - 1)
        else:
            effective_body = 0
            effective_thinking = 0
    if needs is None:
        needs = input_tokens >= trigger_tokens - safety_tokens or (split and effective < 2)
    return {
        "input_tokens": input_tokens,
        "method": "test_estimate",
        "context_limit": 1048576,
        "safety_tokens": safety_tokens,
        "compression_trigger_tokens": trigger_tokens,
        "compression_trigger_ratio": 0.90,
        "remaining_tokens": trigger_tokens - input_tokens - safety_tokens,
        "requested_output_tokens": requested,
        "effective_output_tokens": effective,
        "requested_body_tokens": requested_body,
        "requested_thinking_tokens": requested_thinking,
        "effective_body_tokens": effective_body,
        "effective_thinking_tokens": effective_thinking,
        "effective_max_tokens": effective_body,
        "effective_thinking_budget": effective_thinking,
        "split_output_budget": split,
        "output_limited_by_context": effective < requested,
        "needs_compaction": needs,
    }


def _get_message_text(message):
    return str(message.get("content", "")) if isinstance(message, dict) else ""


def _fake_result(status="completed", finish="stop", content='{"findings":[],"context_gaps":[]}'):
    return {
        "status": status,
        "finish_reason": finish,
        "content": content,
        "parse_ok": status == "completed",
        "parsed_report": {"findings": [], "context_gaps": []} if status == "completed" else None,
        "parsed_findings": [] if status == "completed" else None,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        "seconds": 0.25,
        "first_content_seconds": 0.2,
        "response_log_files": ["response_stream.jsonl", "reasoning.txt"],
    }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "review"

    def call(self, config, packet, out_dir, *, dry_run=False, at=None,
             task_label=None, chat_messages=None, tools=None, route_override=None,
             token_budget=None):
        out_dir.mkdir(parents=True, exist_ok=False)
        self.calls.append({
            "dir": out_dir,
            "dry_run": dry_run,
            "task_label": task_label,
            "messages": chat_messages,
            "tools": tools,
            "route": route_override,
            "budget": token_budget,
        })
        return self.respond(self.calls[-1])

    def execute_workflow(self, responder, budgeter=None, config=None, route=ROUTE_DS, packet=None, dry_run=False, at=None):
        self.calls = []
        self.respond = responder
        if budgeter is None:
            budgeter = lambda messages, tools, route, config, output_cap=None: _budget(output_cap=output_cap, route=route)
        with patch.object(workflow, "plan_budget", side_effect=budgeter):
            return workflow.run_workflow(
                config or CONFIG,
                packet or PACKET,
                self.root,
                review_call=self.call,
                route_selector=lambda cfg, moment=None: route,
                task_label="unit workflow",
                dry_run=dry_run,
                at=at,
            )

    def test_normal_tool_read_then_report_keeps_sources_local_and_tracks_coverage(self):
        def response(entry):
            if len(self.calls) == 1:
                initial = json.dumps(entry["messages"], ensure_ascii=False)
                self.assertNotIn("SOURCE_BODY_SENTINEL", initial)
                self.assertIn("materials_index", initial)
                self.assertTrue(entry["tools"])
                (entry["dir"] / "reasoning.txt").write_text("private tool reasoning", encoding="utf-8")
                return {
                    "status": "tool_calls",
                    "finish_reason": "tool_calls",
                    "content": "",
                    "reasoning_text": "private tool reasoning",
                    "tool_calls": [{
                        "id": "tc-1", "type": "function",
                        "function": {"name": "read_material", "arguments": '{"path":"src/calc.py","start_line":1,"line_count":3}'},
                    }],
                    "response_log_files": ["reasoning.txt"],
                }
            messages = entry["messages"]
            assistant = next(item for item in messages if item.get("role") == "assistant")
            tool = next(item for item in messages if item.get("role") == "tool")
            self.assertEqual(assistant["reasoning_content"], "private tool reasoning")
            self.assertEqual(assistant["content"], "")
            self.assertIn("SOURCE_BODY_SENTINEL", tool["content"])
            return _fake_result()

        result = self.execute_workflow(response)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0]["route"]["max_tokens"], 393216)
        self.assertEqual(self.calls[1]["route"]["max_tokens"], 393216)
        stored_packet = json.loads((self.root / "materials.json").read_text(encoding="utf-8"))
        self.assertEqual(stored_packet["files"][0]["content"], SOURCE)
        workflow_doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow_doc["coverage"]["read_sources"][0]["path"], "src/calc.py")
        self.assertEqual(workflow_doc["coverage"]["read_sources"][0]["line_numbers"], [1, 2, 3])
        root_text = "".join(path.read_text(encoding="utf-8") for path in self.root.glob("*.json") if path.name != "materials.json")
        self.assertNotIn("private tool reasoning", root_text)

    def test_small_source_materials_are_inline_and_do_not_require_tools(self):
        config = json.loads(json.dumps(CONFIG))
        config["context_management"]["inline_material_token_limit"] = 100000

        def response(entry):
            self.assertEqual(entry["tools"], [])
            user = json.loads(next(message["content"] for message in entry["messages"] if message.get("role") == "user"))
            inline_text = json.dumps(user.get("inline_materials", []), ensure_ascii=False)
            self.assertIn("SOURCE_BODY_SENTINEL", inline_text)
            self.assertEqual(user["materials_index"]["overflow_materials"], [])
            self.assertTrue(user["materials_index"]["inline_delivered_ranges"])
            return _fake_result()

        result = self.execute_workflow(response, config=config, route=ROUTE_AUTO)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 1)
        coverage = result["coverage"]
        self.assertEqual(coverage["indexed_materials"], 2)
        self.assertTrue(coverage["inline_delivered_ranges"])
        self.assertEqual(coverage["tool_delivered_ranges"], [])
        self.assertEqual(coverage["status"], "not_verified_by_workflow")

    def test_hybrid_request_inlines_prefix_and_reads_overflow_cursor_on_demand(self):
        config = json.loads(json.dumps(CONFIG))
        config["context_management"]["inline_material_token_limit"] = 100
        source = "prefix_line = 1\n" + ("x = 2\n" * 200) + "# OVERFLOW_RECOVERY_SENTINEL\n"
        packet = {"task": "Review the long source.", "files": [{"path": "src/long.py", "content": source}]}

        def response(entry):
            if len(self.calls) == 1:
                self.assertEqual({tool["function"]["name"] for tool in entry["tools"]}, {
                    "list_materials", "read_material", "search_material",
                })
                user = json.loads(next(message["content"] for message in entry["messages"] if message.get("role") == "user"))
                self.assertTrue(user["inline_materials"])
                overflow = next(item for item in user["materials_index"]["overflow_materials"] if item["path"] == "src/long.py")
                cursor = overflow["cursor"]
                self.assertNotEqual(cursor, {"start_line": 1, "char_offset": 0})
                args = {"path": overflow["path"], **cursor}
                return {
                    "status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                    "tool_calls": [{
                        "id": "read-overflow", "type": "function",
                        "function": {"name": "read_material", "arguments": json.dumps(args)},
                    }],
                }
            tool = next(message for message in entry["messages"] if message.get("role") == "tool")
            self.assertIn("OVERFLOW_RECOVERY_SENTINEL", tool["content"])
            return _fake_result()

        result = self.execute_workflow(response, config=config, route=ROUTE_AUTO, packet=packet)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        coverage = result["coverage"]
        self.assertTrue(coverage["inline_delivered_ranges"])
        self.assertEqual(coverage["tool_delivered_ranges"][0]["path"], "src/long.py")
        self.assertEqual(coverage["status"], "not_verified_by_workflow")

    def test_auto_estimate_pressure_locally_shrinks_inline_then_overflow_is_readable(self):
        config = json.loads(json.dumps(CONFIG))
        config["context_management"]["inline_material_token_limit"] = 100000

        def budgeter(messages, tools, route, config, output_cap=None):
            user = json.loads(next(message["content"] for message in messages if message.get("role") == "user"))
            return _budget(needs=bool(user.get("inline_materials")), route=route, output_cap=output_cap)

        def response(entry):
            if len(self.calls) == 1:
                self.assertEqual({tool["function"]["name"] for tool in entry["tools"]}, {
                    "list_materials", "read_material", "search_material",
                })
                user = json.loads(next(message["content"] for message in entry["messages"] if message.get("role") == "user"))
                self.assertEqual(user.get("inline_materials", []), [])
                overflow = next(item for item in user["materials_index"]["overflow_materials"] if item["path"] == "src/calc.py")
                return {
                    "status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                    "tool_calls": [{
                        "id": "read-after-shrink", "type": "function",
                        "function": {"name": "read_material", "arguments": json.dumps({"path": overflow["path"], **overflow["cursor"]})},
                    }],
                }
            tool = next(message for message in entry["messages"] if message.get("role") == "tool")
            self.assertIn("SOURCE_BODY_SENTINEL", tool["content"])
            return _fake_result()

        result = self.execute_workflow(
            response, budgeter=budgeter, config=config, route=ROUTE_AUTO,
        )
        self.assertEqual(result["status"], "completed")
        workflow_doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertTrue(workflow_doc["inline_budget_reductions"])
        reduction = workflow_doc["inline_budget_reductions"][0]
        self.assertTrue(reduction["reason"])
        self.assertEqual(reduction["compression_trigger_ratio"], 0.90)
        self.assertEqual(reduction["compression_trigger_tokens"], 900000)
        self.assertLessEqual(workflow_doc["inline_materials"]["token_budget"], 1)
        self.assertEqual(self.calls[0]["route"]["max_tokens"], 131072)
        self.assertEqual(workflow_doc["compactions"], 0)

    def test_received_reasoning_is_replayed_for_auto_and_qwen_tools(self):
        for route in (ROUTE_AUTO, ROUTE_QWEN):
            with self.subTest(model=route["model"]):
                self.root = Path(self.temp.name) / ("review-" + route["model"])

                def response(entry):
                    if len(self.calls) == 1:
                        self.assertEqual(entry["route"]["max_tokens"], 131072)
                        self.assertEqual(entry["route"]["thinking_budget"], 262144)
                        return {
                            "status": "tool_calls",
                            "finish_reason": "tool_calls",
                            "content": "",
                            "reasoning_text": "received reasoning for " + route["model"],
                            "tool_calls": [{
                                "id": "reasoning-tool-call", "type": "function",
                                "function": {"name": "read_material", "arguments": '{"path":"src/calc.py","start_line":1,"line_count":2}'},
                            }],
                        }
                    assistant = next(message for message in entry["messages"] if message.get("role") == "assistant")
                    self.assertEqual(assistant["reasoning_content"], "received reasoning for " + route["model"])
                    return _fake_result()

                result = self.execute_workflow(response, route=route)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(self.calls), 2)

    def test_auto_tool_round_without_received_reasoning_does_not_fabricate_field(self):
        def response(entry):
            if len(self.calls) == 1:
                return {
                    "status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                    "tool_calls": [{
                        "id": "no-reasoning", "type": "function",
                        "function": {"name": "read_material", "arguments": '{"path":"src/calc.py"}'},
                    }],
                }
            assistant = next(message for message in entry["messages"] if message.get("role") == "assistant")
            self.assertNotIn("reasoning_content", assistant)
            return _fake_result()

        result = self.execute_workflow(response, route=ROUTE_AUTO)
        self.assertEqual(result["status"], "completed")

    def test_auto_reasoning_survives_active_compaction_and_stays_out_of_aggregate_logs(self):
        marker = "AUTO_REASONING_PRIVATE_MARKER"

        def budgeter(messages, tools, route, config, output_cap=None):
            if tools and any(message.get("role") == "tool" for message in messages):
                return _budget(input_tokens=900000, output_cap=output_cap, route=route)
            return _budget(output_cap=output_cap, route=route)

        def response(entry):
            messages = entry["messages"]
            if entry["route"].get("enable_thinking") is False:
                compact_input = json.dumps(messages, ensure_ascii=False)
                self.assertIn(marker, compact_input)
                self.assertNotIn("SOURCE_BODY_SENTINEL", compact_input)
                return _fake_result(content="checkpoint: null branch requires a guard")
            user_text = next(message["content"] for message in messages if message.get("role") == "user")
            if "checkpoint_from_this_review" in user_text:
                self.assertIn("null branch requires a guard", user_text)
                return _fake_result()
            return {
                "status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                "reasoning_text": marker + ": a null input reaches the branch.",
                "tool_calls": [{
                    "id": "auto-read", "type": "function",
                    "function": {"name": "read_material", "arguments": '{"path":"src/calc.py","start_line":1,"line_count":3}'},
                }],
            }

        result = self.execute_workflow(response, budgeter=budgeter, route=ROUTE_AUTO)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 3)
        root_logs = "".join(
            path.read_text(encoding="utf-8")
            for path in self.root.iterdir()
            if path.is_file() and path.name != "materials.json" and path.suffix in {".json", ".md"}
        )
        self.assertNotIn(marker, root_logs)

    def test_budget_pressure_compacts_on_same_route_and_resumes_with_minimal_index(self):
        def budgeter(messages, tools, route, config, output_cap=None):
            # The first review request is pressured; compaction and the fresh
            # checkpoint request fit. This is a local planning simulation.
            needs = bool(tools) and not any("checkpoint_from_this_review" in str(m.get("content", "")) for m in messages if m.get("role") == "user")
            return _budget(needs=needs, output_cap=output_cap)

        def response(entry):
            if entry["route"].get("reasoning_effort") == "none":
                self.assertFalse(entry["route"]["enable_thinking"])
                self.assertEqual(entry["route"]["model"], ROUTE_DS["model"])
                self.assertEqual(entry["route"]["max_tokens"], 16384)
                return _fake_result(content="检查进度checkpoint：已准备按需读取源码。")
            if len(self.calls) == 1:
                self.fail("budget pressure must compact before the first review call")
            message = next(m["content"] for m in entry["messages"] if m.get("role") == "user")
            self.assertIn("checkpoint_from_this_review", message)
            self.assertIn("list_materials", json.dumps(entry["tools"], ensure_ascii=False))
            return _fake_result()

        result = self.execute_workflow(response, budgeter=budgeter)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("compaction", self.calls[0]["task_label"])

    def test_length_compresses_partial_and_continues_without_replaying_source_packet(self):
        def response(entry):
            if entry["route"].get("reasoning_effort") == "none":
                compact_text = json.dumps(entry["messages"], ensure_ascii=False)
                self.assertIn("partial finding before length", compact_text)
                self.assertIn("checkpoint clue", compact_text)
                self.assertNotIn("SOURCE_BODY_SENTINEL", compact_text)
                return _fake_result(content="checkpoint: prior finding retained; continue remaining checks.")
            if len(self.calls) == 1:
                return {
                    "status": "incomplete",
                    "finish_reason": "length",
                    "content": "partial finding before length",
                    "reasoning_text": "checkpoint clue",
                    "parse_ok": False,
                    "response_log_files": ["reasoning.txt"],
                }
            return _fake_result()

        result = self.execute_workflow(response)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 3)
        second_review = self.calls[2]
        self.assertEqual(second_review["route"]["reasoning_effort"], "max")
        user_message = next(m["content"] for m in second_review["messages"] if m.get("role") == "user")
        self.assertIn("checkpoint_from_this_review", user_message)
        self.assertNotIn("SOURCE_BODY_SENTINEL", user_message)
        self.assertNotIn("checkpoint clue", (self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertTrue((self.root / "checkpoint.txt").is_file())
        workflow_doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow_doc["checkpoint_file"], str((self.root / "checkpoint.txt").resolve()))

    def test_failed_resume_keeps_partial_and_checkpoint_reference(self):
        def response(entry):
            if entry["route"].get("reasoning_effort") == "none":
                return _fake_result(content="checkpoint: first finding and checked scope")
            if len(self.calls) == 1:
                return {
                    "status": "incomplete", "finish_reason": "length",
                    "content": "visible partial before continuation failure", "parse_ok": False,
                }
            return {
                "status": "error", "error_type": "ProviderError",
                "finish_reason": None, "content": "",
            }

        result = self.execute_workflow(response)
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["content_is_partial"])
        self.assertEqual(result["content"], "visible partial before continuation failure")
        workflow_doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertTrue(workflow_doc["content_is_partial"])
        checkpoint_path = Path(result["checkpoint_file"])
        self.assertEqual(checkpoint_path, (self.root / "checkpoint.txt").resolve())
        self.assertIn("first finding", checkpoint_path.read_text(encoding="utf-8"))
        report = (self.root / "report.md").read_text(encoding="utf-8")
        self.assertIn("visible partial before continuation failure", report)
        self.assertNotIn("checkpoint: first finding", report)
        self.assertEqual(len(self.calls), 3)

    def test_active_90_percent_compaction_preserves_finding_from_reasoning_content(self):
        def budgeter(messages, tools, route, config, output_cap=None):
            if tools and any(message.get("role") == "tool" for message in messages):
                return _budget(input_tokens=900000, output_cap=output_cap)
            return _budget(output_cap=output_cap)

        def response(entry):
            if entry["route"].get("reasoning_effort") == "none":
                payload = json.dumps(entry["messages"], ensure_ascii=False)
                self.assertIn("REASONING_ONLY_FINDING", payload)
                self.assertNotIn("SOURCE_BODY_SENTINEL", payload)
                return _fake_result(content="checkpoint retained REASONING_ONLY_FINDING")
            if any(message.get("role") == "user" and "checkpoint_from_this_review" in message.get("content", "")
                   for message in entry["messages"]):
                user = next(message["content"] for message in entry["messages"] if message.get("role") == "user")
                self.assertIn("REASONING_ONLY_FINDING", user)
                return _fake_result()
            return {
                "status": "tool_calls",
                "finish_reason": "tool_calls",
                "content": "",
                "reasoning_text": "REASONING_ONLY_FINDING: null input reaches an unchecked branch.",
                "tool_calls": [{
                    "id": "read-90", "type": "function",
                    "function": {"name": "read_material", "arguments": '{"path":"src/calc.py","start_line":1,"line_count":3}'},
                }],
            }

        result = self.execute_workflow(response, budgeter=budgeter)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 3)
        self.assertIn("compaction", self.calls[1]["task_label"])
        self.assertEqual(self.calls[1]["route"]["model"], ROUTE_DS["model"])
        self.assertEqual(self.calls[1]["route"]["reasoning_effort"], "none")

    def test_two_compactions_preserve_prior_checkpoint_and_only_actual_page_range(self):
        config = json.loads(json.dumps(CONFIG))
        config["context_management"]["tool_max_chars"] = 24
        packet = {"task": "Review source pages.", "files": [{"path": "large.py", "content": "x" * 100 + "\n"}]}

        def budgeter(messages, tools, route, cfg, output_cap=None):
            if route.get("reasoning_effort") == "none":
                return _budget(output_cap=output_cap)
            if tools and any(message.get("role") == "tool" for message in messages):
                return _budget(input_tokens=900000, output_cap=output_cap)
            return _budget(output_cap=output_cap)

        compression_count = 0

        def response(entry):
            nonlocal compression_count
            route = entry["route"]
            messages = entry["messages"]
            if route.get("reasoning_effort") == "none":
                compression_count += 1
                payload = json.dumps(messages, ensure_ascii=False)
                if compression_count == 1:
                    return _fake_result(content="FIRST_CHECKPOINT_ONLY_FINDING: null guard missing")
                self.assertIn("FIRST_CHECKPOINT_ONLY_FINDING", payload)
                self.assertIn("TOOL_TURN_REASONING_PROGRESS", payload)
                user = json.loads(next(item["content"] for item in messages if item.get("role") == "user"))
                delivered = user["successfully_delivered_material_ranges"]
                self.assertEqual(len(delivered), 1)
                self.assertEqual(delivered[0]["line_numbers"], [1])
                self.assertEqual(delivered[0]["returned_chars"], 24)
                self.assertTrue(delivered[0]["has_more"])
                self.assertEqual(delivered[0]["next_start_line"], 1)
                self.assertEqual(delivered[0]["next_char_offset"], 24)
                self.assertNotIn('"line_count": 250', json.dumps(delivered))
                return _fake_result(content="SECOND_CHECKPOINT carries FIRST_CHECKPOINT_ONLY_FINDING and page cursor")

            user_message = next(item["content"] for item in messages if item.get("role") == "user")
            if "checkpoint_from_this_review" in user_message and "SECOND_CHECKPOINT" in user_message:
                self.assertIn("FIRST_CHECKPOINT_ONLY_FINDING", user_message)
                return _fake_result()
            if len(self.calls) == 1:
                return {
                    "status": "incomplete", "finish_reason": "length",
                    "content": "partial analysis before first checkpoint", "parse_ok": False,
                }
            return {
                "status": "tool_calls",
                "finish_reason": "tool_calls",
                "content": "",
                "reasoning_text": "TOOL_TURN_REASONING_PROGRESS: continue from the previous finding.",
                "tool_calls": [{
                    "id": "read-page", "type": "function",
                    "function": {"name": "read_material", "arguments": '{"path":"large.py","start_line":1,"line_count":250}'},
                }],
            }

        result = self.execute_workflow(response, budgeter=budgeter, config=config, packet=packet)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(compression_count, 2)
        self.assertEqual(len(self.calls), 5)
        root_result = json.loads((self.root / "result.json").read_text(encoding="utf-8"))
        source_read = root_result["coverage"]["read_sources"][0]
        self.assertEqual(source_read["line_numbers"], [1])
        self.assertEqual(source_read["next_char_offset"], 24)
        aggregate = (self.root / "workflow.json").read_text(encoding="utf-8") + (self.root / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("TOOL_TURN_REASONING_PROGRESS", aggregate)

    def test_provider_error_is_not_retried_or_rerouted(self):
        def response(_entry):
            return {"status": "error", "error_type": "ProviderError", "content": "partial before error", "finish_reason": None}

        result = self.execute_workflow(response)
        self.assertEqual(result["status"], "error")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("no automatic retry", result["uncompleted_reason"])

    def test_oversized_checkpoint_is_split_then_merged_within_compaction_limit(self):
        def budgeter(messages, tools, route, config, output_cap=None):
            joined = " ".join(_get_message_text(m) for m in messages)
            if route.get("reasoning_effort") == "none":
                if "checkpoint_segment_summaries" in joined:
                    return _budget(output_cap=output_cap)
                if "checkpoint_segment" in joined:
                    segment = next((m.get("content", "") for m in messages if m.get("role") == "user"), "")
                    # Each isolated metadata/work segment fits; joining two
                    # forces the bounded chunk path.
                    return _budget(needs=segment.count('"section"') > 1, output_cap=output_cap)
                return _budget(needs=True, output_cap=output_cap)
            return _budget(output_cap=output_cap)

        packet = {"task": "Review the source.", "files": [{"path": "a.py", "content": SOURCE}]}

        def response(entry):
            if entry["route"].get("reasoning_effort") == "none":
                user = next(m["content"] for m in entry["messages"] if m.get("role") == "user")
                if "checkpoint_segment_summaries" in user:
                    return _fake_result(content="merged checkpoint preserves both segments")
                return _fake_result(content="segment summary")
            if len(self.calls) == 1:
                return {
                    "status": "incomplete", "finish_reason": "length",
                    "content": "partial finding before compaction split", "parse_ok": False,
                }
            return _fake_result()

        result = self.execute_workflow(response, budgeter=budgeter, packet=packet)
        self.assertEqual(result["status"], "completed")
        workflow_doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow_doc["compactions"], 3)
        self.assertEqual(len(self.calls), 5)
        for entry in self.calls[1:4]:
            self.assertEqual(entry["route"]["reasoning_effort"], "none")
            self.assertFalse(entry["route"]["enable_thinking"])


    def test_call_limit_preserves_length_partial_as_incomplete(self):
        config = json.loads(json.dumps(CONFIG))
        config["context_management"]["max_model_calls"] = 1

        def response(_entry):
            return {
                "status": "incomplete", "finish_reason": "length",
                "content": "visible partial finding", "parse_ok": False,
            }

        result = self.execute_workflow(response, config=config)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["content"], "visible partial finding")
        self.assertEqual(len(self.calls), 1)
        self.assertIsNotNone(result["uncompleted_reason"])
        self.assertNotEqual(result["status"], "completed")

    def test_unbounded_defaults_allow_more_than_sixteen_tool_calls(self):
        config = json.loads(json.dumps(CONFIG))
        config["context_management"].pop("max_model_calls")
        config["context_management"]["max_compactions"] = None

        def response(entry):
            if len(self.calls) <= 17:
                return {
                    "status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                    "tool_calls": [{
                        "id": "list-%d" % len(self.calls), "type": "function",
                        "function": {"name": "list_materials", "arguments": '{"offset":0,"limit":1}'},
                    }],
                }
            return _fake_result()

        result = self.execute_workflow(response, config=config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 18)
        self.assertEqual(result["model_calls"], 18)
        self.assertEqual(result["compactions"], 0)

    def test_unbounded_defaults_allow_more_than_three_successful_compactions(self):
        config = json.loads(json.dumps(CONFIG))
        config["context_management"].pop("max_compactions")
        config["context_management"]["max_model_calls"] = 0
        review_count = 0

        def response(entry):
            nonlocal review_count
            if entry["route"].get("reasoning_effort") == "none":
                return _fake_result(content="checkpoint after compression %d" % (sum(
                    1 for call in self.calls if call["route"].get("reasoning_effort") == "none"
                )))
            review_count += 1
            if review_count <= 4:
                return {
                    "status": "incomplete", "finish_reason": "length",
                    "content": "partial review %d" % review_count, "parse_ok": False,
                }
            return _fake_result()

        result = self.execute_workflow(response, config=config)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(review_count, 5)
        self.assertEqual(result["compactions"], 4)
        self.assertEqual(len(self.calls), 9)

    def test_explicit_smoke_limits_four_calls_and_one_compaction_still_apply(self):
        config = json.loads(json.dumps(CONFIG))
        config["context_management"]["max_model_calls"] = 4
        config["context_management"]["max_compactions"] = 1

        def response(entry):
            if entry["route"].get("reasoning_effort") == "none":
                return _fake_result(content="single allowed checkpoint")
            if len(self.calls) == 1:
                return {
                    "status": "incomplete", "finish_reason": "length",
                    "content": "initial partial", "parse_ok": False,
                }
            return {
                "status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                "tool_calls": [{
                    "id": "bounded-%d" % len(self.calls), "type": "function",
                    "function": {"name": "list_materials", "arguments": '{"offset":0,"limit":1}'},
                }],
            }

        result = self.execute_workflow(response, config=config)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(result["model_calls"], 4)
        self.assertEqual(result["compactions"], 1)
        self.assertEqual(result["content"], "initial partial")

        self.root = Path(self.temp.name) / "review-compaction-limit"
        review_calls = 0

        def repeated_length(entry):
            nonlocal review_calls
            if entry["route"].get("reasoning_effort") == "none":
                return _fake_result(content="one permitted checkpoint")
            review_calls += 1
            return {
                "status": "incomplete", "finish_reason": "length",
                "content": "partial review %d" % review_calls, "parse_ok": False,
            }

        compact_limited = self.execute_workflow(repeated_length, config=config)
        self.assertEqual(compact_limited["status"], "incomplete")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(compact_limited["model_calls"], 3)
        self.assertEqual(compact_limited["compactions"], 1)
        self.assertEqual(compact_limited["content"], "partial review 2")

    def test_compaction_error_preserves_original_partial_and_is_not_retried(self):
        def response(entry):
            if entry["route"].get("reasoning_effort") == "none":
                return {"status": "error", "error_type": "ProviderError", "content": ""}
            return {
                "status": "incomplete", "finish_reason": "length",
                "content": "original visible partial", "parse_ok": False,
            }

        result = self.execute_workflow(response)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["content"], "original visible partial")
        self.assertEqual(len(self.calls), 2)
        workflow_doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(workflow_doc["child_calls"][1]["status"], "error")
        self.assertIn("did not return a usable", result["uncompleted_reason"])

    def test_dry_run_prepares_only_minimal_first_request(self):
        def response(entry):
            self.assertTrue(entry["dry_run"])
            prompt = json.dumps(entry["messages"], ensure_ascii=False)
            self.assertNotIn("SOURCE_BODY_SENTINEL", prompt)
            return {"status": "dry_run", "content": "", "parse_ok": False}

        result = self.execute_workflow(response, route=ROUTE_AUTO, dry_run=True, at="2026-10-23T08:00:00+08:00")
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(len(self.calls), 1)
        self.assertTrue((self.root / "materials.json").is_file())
        self.assertEqual(json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))["coverage"]["read_sources"], [])
        self.assertEqual(self.calls[0]["route"]["max_tokens"], 131072)
        self.assertEqual(self.calls[0]["route"]["thinking_budget"], 262144)
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["planned_calls"], 1)
        self.assertEqual(result["workflow_status"], "dry_run")

    def test_89_percent_input_caps_output_without_precompression(self):
        def budgeter(messages, tools, route, config, output_cap=None):
            return _budget(input_tokens=890000, output_cap=output_cap, route=route)

        result = self.execute_workflow(lambda _entry: _fake_result(), budgeter=budgeter, route=ROUTE_AUTO)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["route"]["model"], "auto")
        self.assertEqual(self.calls[0]["route"]["max_tokens"], 2650)
        self.assertEqual(self.calls[0]["route"]["thinking_budget"], 5302)
        self.assertLessEqual(self.calls[0]["route"]["max_tokens"] + self.calls[0]["route"]["thinking_budget"], 7952)
        self.assertTrue(self.calls[0]["budget"]["output_limited_by_context"])

    def test_90_percent_input_compacts_before_review_then_resumes_same_route(self):
        def budgeter(messages, tools, route, config, output_cap=None):
            has_checkpoint = any(
                "checkpoint_from_this_review" in _get_message_text(message)
                for message in messages if isinstance(message, dict) and message.get("role") == "user"
            )
            if tools and not has_checkpoint:
                return _budget(input_tokens=900000, output_cap=output_cap, route=route)
            return _budget(input_tokens=890000 if tools else 1000, output_cap=output_cap, route=route)

        def response(entry):
            if entry["route"].get("enable_thinking") is False:
                self.assertNotIn("thinking_budget", entry["route"])
                return _fake_result(content="compact checkpoint for 90 percent threshold")
            return _fake_result()

        result = self.execute_workflow(response, budgeter=budgeter, route=ROUTE_AUTO)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertIn("compaction", self.calls[0]["task_label"])
        self.assertEqual(self.calls[0]["route"]["model"], "auto")
        self.assertFalse(self.calls[0]["route"]["enable_thinking"])
        self.assertEqual(self.calls[1]["route"]["model"], "auto")
        self.assertEqual(self.calls[1]["route"]["max_tokens"], 2650)
        self.assertEqual(self.calls[1]["route"]["thinking_budget"], 5302)

    def test_compaction_output_cap_stays_below_90_percent_before_trigger(self):
        def budgeter(messages, tools, route, config, output_cap=None):
            if route.get("reasoning_effort") == "none":
                # 88.7952% of the 1M context leaves exactly 10,000 tokens
                # before the configured output ceiling.
                return _budget(input_tokens=887952, output_cap=output_cap, route=route)
            return _budget(output_cap=output_cap, route=route)

        def response(entry):
            if entry["route"].get("reasoning_effort") == "none":
                self.assertEqual(entry["route"]["max_tokens"], 10000)
                self.assertEqual(entry["budget"]["requested_output_tokens"], 16384)
                self.assertEqual(entry["budget"]["effective_output_tokens"], 10000)
                self.assertFalse(entry["budget"]["needs_compaction"])
                return _fake_result(content="checkpoint within the active threshold")
            if len(self.calls) == 1:
                return {
                    "status": "incomplete", "finish_reason": "length",
                    "content": "visible partial that needs compaction", "parse_ok": False,
                }
            return _fake_result()

        result = self.execute_workflow(response, budgeter=budgeter)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 3)


if __name__ == "__main__":
    unittest.main()

