"""Recovery state-machine regressions; provider calls and waiting are offline fakes."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from . import history_cleanup, workflow
except ImportError:
    import history_cleanup
    import workflow


ROUTE = {
    "model": "deepseek-v4.1-flash-1017", "reasoning_effort": "max",
    "enable_thinking": True, "max_tokens": 256000,
    "context_window_tokens": 1000000,
}
PACKET = {
    "task": "Review calculate behavior.",
    "files": [{"path": "calc.py", "content": "first = 1\nsecond = 2\n"}],
}
CONFIG = {
    "context_window_tokens": 1000000,
    "context_management": {
        "inline_material_token_limit": 0, "compaction_max_tokens": 16384,
        "tool_max_chars": 24000, "tool_max_lines": 250,
    },
    "api_recovery": {"retry_window_seconds": 120},
}


def budget(messages, tools, route, config, output_cap=None):
    output = output_cap if output_cap is not None else route["max_tokens"]
    return {
        "input_tokens": 1000, "method": "offline_test",
        "context_limit": 1000000, "safety_tokens": 2048,
        "compression_trigger_tokens": 678464,
        "requested_output_tokens": output, "effective_output_tokens": output,
        "needs_compaction": False, "retain_recent_tokens": 119040,
    }


def completed(content='{"findings":[],"context_gaps":[]}'):
    return {
        "status": "completed", "finish_reason": "stop", "stream_done": True,
        "content": content, "parse_ok": True,
        "parsed_report": {"findings": [], "context_gaps": []},
        "parsed_findings": [], "seconds": 0.25,
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def tool(line=1, content=""):
    return {
        "status": "tool_calls", "finish_reason": "tool_calls", "stream_done": True,
        "content": content, "seconds": 0.25,
        "tool_calls": [{"id": "read-%d" % line, "type": "function", "function": {
            "name": "read_material", "arguments": json.dumps({
                "path": "calc.py", "start_line": line, "line_count": 1,
            }),
        }}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


def failure(code="GatewayTimeout", *, status="error", reasoning_chars=0,
            error_type="ProviderError", http_status=200):
    return {
        "status": status, "error_type": error_type, "http_status": http_status,
        "provider_error": {"code": code, "message": "Deadline Exceeded" if code == "GatewayTimeout" else code},
        "reasoning_chars": reasoning_chars, "content": "", "stream_done": False,
        "finish_reason": None, "seconds": 2.0, "usage": {}, "parse_ok": False,
    }


class WorkflowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "review"
        self.calls = []
        # Counting is deterministic and cannot download tokenizer assets.
        self.addCleanup(patch.stopall)
        patch.object(workflow, "plan_budget", side_effect=budget).start()
        patch.object(history_cleanup, "estimate_tokens", return_value={"input_tokens": 10}).start()
        self.sleep = patch.object(workflow.time, "sleep").start()
        patch.object(workflow, "retry_wait_seconds", return_value=0).start()

    def run_flow(self, respond, config=None, **options):
        def call(config, packet, output_dir, **kwargs):
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=False)
            entry = {"directory": output_dir, **copy.deepcopy(kwargs)}
            self.calls.append(entry)
            result = respond(entry)
            (output_dir / "result.json").write_text(json.dumps(result), encoding="utf-8")
            return result
        return workflow.run_workflow(
            copy.deepcopy(config or CONFIG), copy.deepcopy(PACKET), self.root,
            review_call=call, route_selector=options.pop("route_selector", lambda cfg, at=None: copy.deepcopy(ROUTE)),
            **options,
        )

    def test_transport_retry_uses_successful_attempt_reasoning_and_preserves_failure(self):
        def respond(call):
            number = len(self.calls)
            if number == 1:
                result = failure(error_type="ReadTimeout", http_status=None)
                result.pop("provider_error")
                return result
            if number == 2:
                (call["directory"] / "reasoning.txt").write_text("SUCCESSFUL_TOOL_REASONING", encoding="utf-8")
                return tool()
            assistant = next(m for m in call["chat_messages"] if m.get("role") == "assistant")
            self.assertEqual(assistant["reasoning_content"], "SUCCESSFUL_TOOL_REASONING")
            self.assertEqual(call["chat_messages"][-1]["role"], "tool")
            return completed()
        result = self.run_flow(respond)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len({str(c["directory"]) for c in self.calls}), 3)
        self.assertEqual(self.calls[0]["chat_messages"], self.calls[1]["chat_messages"])
        self.assertEqual(result["tools_executed"], 1)
        original = json.loads((self.calls[0]["directory"] / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(original["status"], "error")
        self.assertEqual(original["error_type"], "ReadTimeout")

    def test_capacity_retry_does_not_execute_previously_returned_tool_again(self):
        def respond(call):
            if len(self.calls) == 1:
                return tool()
            if len(self.calls) == 2:
                (call["directory"] / "response_metadata.json").write_text(
                    json.dumps({"headers": {"retry-after": "0"}}), encoding="utf-8")
                return failure("RESOURCE_EXHAUSTED", http_status=503)
            return completed()
        result = self.run_flow(respond)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tools_executed"], 1)
        self.assertEqual(self.calls[1]["chat_messages"], self.calls[2]["chat_messages"])
        returned = [json.loads(line) for line in (self.root / "tool_results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(returned), 1)

    def test_deadline_summarizes_progress_then_resumes_without_old_tool_tail(self):
        def respond(call):
            number = len(self.calls)
            if number <= 2:
                return tool(number, content="OLD_ASSISTANT_PROGRESS_%d" % number)
            if number == 3:
                (call["directory"] / "reasoning.txt").write_text("PARTIAL_FINDING_NEEDS_VERIFICATION", encoding="utf-8")
                return failure(reasoning_chars=34)
            if number == 4:
                self.assertFalse(call["route_override"]["enable_thinking"])
                payload = json.dumps(call["chat_messages"])
                self.assertIn("PARTIAL_FINDING_NEEDS_VERIFICATION", payload)
                self.assertIn("OLD_ASSISTANT_PROGRESS_1", payload)
                self.assertNotIn("GatewayTimeout", payload)
                return completed("CHECKPOINT: validate calculate evidence next.")
            payload = json.dumps(call["chat_messages"])
            self.assertIn("CHECKPOINT: validate calculate evidence next.", payload)
            self.assertNotIn("OLD_ASSISTANT_PROGRESS_", payload)
            self.assertFalse(any(m.get("role") in ("assistant", "tool") for m in call["chat_messages"]))
            self.assertEqual(call["route_override"]["reasoning_effort"], "max")
            self.assertEqual(call["route_override"]["max_tokens"], 256000)
            return completed()
        result = self.run_flow(respond)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(result["tools_executed"], 2)
        self.assertEqual(result["compactions"], 1)
        self.assertEqual(result["timing"]["successful_request_seconds"], 1.0)
        self.assertEqual(result["timing"]["failed_request_seconds"], 2.0)
        self.assertEqual(result["timing"]["compaction_request_seconds"], 0.25)
        doc = json.loads((self.root / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["last_compaction_reason"], "provider_deadline")
        self.assertEqual(doc["retained_recent_messages"], 0)

    def test_interrupted_tool_arguments_are_not_executed(self):
        def respond(call):
            if len(self.calls) == 1:
                (call["directory"] / "reasoning.txt").write_text("Need to check caller contract.", encoding="utf-8")
                result = failure(reasoning_chars=29)
                result["tool_calls"] = [{"id": "partial", "type": "function", "function": {
                    "name": "read_material", "arguments": '{"path":"calc.py"',
                }}]
                return result
            if len(self.calls) == 2:
                return completed("CHECKPOINT: caller contract still unverified.")
            return completed()
        result = self.run_flow(respond)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tools_executed"], 0)
        self.assertFalse((self.root / "tool_results.jsonl").exists())
        self.assertEqual(len(self.calls), 3)

    def test_tool_finish_without_stream_completion_does_not_execute_partial_batch(self):
        def respond(call):
            if len(self.calls) == 1:
                result = tool()
                result["stream_done"] = False
                result["tool_calls"][0]["function"]["arguments"] = '{"path":"calc.py"'
                return result
            if len(self.calls) == 2:
                return completed("CHECKPOINT: requested read was not delivered.")
            return completed()
        result = self.run_flow(respond)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tools_executed"], 0)
        self.assertFalse((self.root / "tool_results.jsonl").exists())
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(result["timing"]["failed_request_seconds"], 0.25)
        self.assertEqual(result["timing"]["successful_request_seconds"], 0.5)

    def test_permanent_errors_and_operator_cancellation_never_retry_or_compact(self):
        cases = [
            failure("Unauthorized", http_status=401, reasoning_chars=5),
            failure("invalid_parameter", http_status=400),
            failure("", status="interrupted", error_type="KeyboardInterrupt", http_status=None),
        ]
        for index, failed in enumerate(cases):
            with self.subTest(error=failed["error_type"], http=failed["http_status"]):
                self.root = Path(self.temp.name) / ("permanent-%d" % index)
                self.calls = []
                result = self.run_flow(lambda call: copy.deepcopy(failed))
                self.assertNotEqual(result["status"], "completed")
                self.assertEqual(len(self.calls), 1)
                self.assertEqual(result["compactions"], 0)
                self.assertEqual(result["tools_executed"], 0)

    def test_failed_compressor_stops_without_nested_recovery(self):
        def respond(call):
            if len(self.calls) == 1:
                (call["directory"] / "reasoning.txt").write_text("Tentative finding, not final.", encoding="utf-8")
                return failure(reasoning_chars=28)
            (call["directory"] / "reasoning.txt").write_text("Partial checkpoint text.", encoding="utf-8")
            return failure(reasoning_chars=24)
        result = self.run_flow(respond)
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["compactions"], 1)
        self.assertFalse(result["parse_ok"])

    def test_later_permanent_failure_does_not_present_old_tool_narrative_as_report(self):
        def respond(call):
            if len(self.calls) == 1:
                return tool(content="OLD_TOOL_TURN_NARRATIVE_IS_NOT_FINAL_REPORT")
            return failure("Unauthorized", http_status=401)
        result = self.run_flow(respond)
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(result["content"], "")
        self.assertFalse(result["parse_ok"])
        self.assertNotIn("OLD_TOOL_TURN_NARRATIVE_IS_NOT_FINAL_REPORT", (self.root / "report.md").read_text(encoding="utf-8"))

    def test_request_failure_and_backoff_timings_are_separate(self):
        clock = SimpleNamespace(value=0.0)
        def wait(seconds):
            clock.value += seconds
        def respond(call):
            return failure("RESOURCE_EXHAUSTED", http_status=503) if len(self.calls) == 1 else completed()
        with patch.object(workflow.time, "monotonic", side_effect=lambda: clock.value), \
                patch.object(workflow, "retry_wait_seconds", return_value=3), \
                patch.object(workflow.time, "sleep", side_effect=wait):
            result = self.run_flow(respond)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["timing"]["successful_request_seconds"], 0.25)
        self.assertEqual(result["timing"]["failed_request_seconds"], 2.0)
        self.assertEqual(result["timing"]["retry_wait_seconds"], 3.0)
        self.assertEqual(result["timing"]["compaction_request_seconds"], 0.0)
        self.assertEqual(result["child_calls"][0]["usage"], {})
        self.assertEqual(result["child_calls"][1]["usage"]["completion_tokens"], 5)

    def test_overslept_backoff_expires_window_without_sending_another_request(self):
        clock = SimpleNamespace(value=0.0)
        def suspended_wait(seconds):
            # Requested wait fits the window, but an OS suspension resumes much later.
            clock.value += 125.0
        with patch.object(workflow.time, "monotonic", side_effect=lambda: clock.value), \
                patch.object(workflow, "retry_wait_seconds", return_value=3), \
                patch.object(workflow.time, "sleep", side_effect=suspended_wait) as sleep:
            result = self.run_flow(lambda call: failure("RESOURCE_EXHAUSTED", http_status=503))
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 1)
        sleep.assert_called_once_with(3)
        self.assertEqual(result["timing"]["retry_wait_seconds"], 125.0)
        self.assertEqual(result["timing"]["failed_request_seconds"], 2.0)
        self.assertEqual(result["timing"]["successful_request_seconds"], 0.0)
        self.assertEqual(result["child_calls"][0]["recovery_stopped"], "transient_retry_window_exhausted")

    def test_provider_retry_after_beyond_window_does_not_send_an_early_retry(self):
        try:
            from .api_recovery import retry_wait_seconds
        except ImportError:
            from api_recovery import retry_wait_seconds
        def respond(call):
            (call["directory"] / "response_metadata.json").write_text(
                json.dumps({"headers": {"Retry-After": "121"}}), encoding="utf-8")
            return failure("RESOURCE_EXHAUSTED", http_status=503)
        with patch.object(workflow, "retry_wait_seconds", side_effect=retry_wait_seconds):
            result = self.run_flow(respond)
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 1)
        self.sleep.assert_not_called()
        self.assertEqual(result["timing"]["failed_request_seconds"], 2.0)
        self.assertEqual(result["timing"]["successful_request_seconds"], 0.0)
        self.assertEqual(result["timing"]["retry_wait_seconds"], 0.0)

    def test_cross_process_restore_compacts_old_progress_without_replaying_old_calls(self):
        old = Path(self.temp.name) / "old-review"
        old_call = old / "calls" / "0027-review"
        old_call.mkdir(parents=True)
        old_failure = failure(reasoning_chars=33)
        old_failure["seconds"] = 1796.7
        old_failure["usage"] = {"prompt_tokens": 999999, "completion_tokens": 99999}
        saved = json.dumps(old_failure)
        (old_call / "result.json").write_text(saved, encoding="utf-8")
        (old_call / "reasoning.txt").write_text("RESUME_SAVED_REASONING", encoding="utf-8")
        old_tool = tool()["tool_calls"][0]
        state = SimpleNamespace(
            route=copy.deepcopy(ROUTE), validate_output=lambda root: None,
            provenance={"source_review_directory": str(old)},
            read_sources=[{"path": "calc.py", "source_id": "source-0001", "line_numbers": [1]}],
            inline_delivered_ranges=[{"path": "calc.py", "line_numbers": [2]}],
            review_messages=[
                {"role": "system", "content": "Review the same task."},
                {"role": "user", "content": '{"checkpoint_from_this_review":"PRIOR_CHECKPOINT"}'},
                {"role": "assistant", "content": "OLD_PROGRESS", "tool_calls": [old_tool]},
                {"role": "tool", "tool_call_id": old_tool["id"], "content": '{"text":"first = 1"}'},
            ],
            latest_result=old_failure, latest_call_dir=old_call,
        )
        store = workflow.MaterialStore(copy.deepcopy(PACKET))
        store.packet = copy.deepcopy(PACKET)
        def respond(call):
            self.assertEqual(call["route_override"]["model"], ROUTE["model"])
            if len(self.calls) == 1:
                self.assertFalse(call["route_override"]["enable_thinking"])
                payload = json.dumps(call["chat_messages"])
                self.assertIn("RESUME_SAVED_REASONING", payload)
                self.assertIn("PRIOR_CHECKPOINT", payload)
                self.assertIn("OLD_PROGRESS", payload)
                return completed("RESTORED_CHECKPOINT: inspect unresolved evidence.")
            self.assertEqual(call["route_override"]["reasoning_effort"], "max")
            self.assertEqual(call["route_override"]["max_tokens"], 256000)
            self.assertFalse(any(m.get("role") in {"assistant", "tool"} for m in call["chat_messages"]))
            self.assertIn("RESTORED_CHECKPOINT", json.dumps(call["chat_messages"]))
            return completed()
        result = self.run_flow(
            respond, resume_state=state, source_factory=lambda root: store,
            route_selector=lambda config, at=None: {"model": "auto", "max_tokens": 131072},
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tools_executed"], 0)
        self.assertEqual(result["route"]["model"], ROUTE["model"])
        self.assertEqual(result["coverage"]["read_sources"], state.read_sources)
        self.assertEqual(result["timing"]["successful_request_seconds"], 0.5)
        self.assertEqual(result["timing"]["failed_request_seconds"], 0.0)
        self.assertEqual(result["timing"]["compaction_request_seconds"], 0.25)
        self.assertEqual((old_call / "result.json").read_text(encoding="utf-8"), saved)
        self.assertFalse((self.root / "tool_results.jsonl").exists())
        self.assertEqual(sum(call["usage"]["prompt_tokens"] for call in result["child_calls"]), 20)
        self.assertEqual(sum(call["usage"]["completion_tokens"] for call in result["child_calls"]), 10)

    def test_resume_compaction_does_not_claim_newly_prepared_inline_as_previously_read(self):
        for index, old_inline in enumerate(([], [{"path": "calc.py", "line_numbers": [1]}])):
            with self.subTest(old_inline=old_inline):
                self.calls = []
                self.root = Path(self.temp.name) / ("resume-inline-%d" % index)
                old_call = Path(self.temp.name) / ("old-inline-%d" % index)
                old_call.mkdir()
                (old_call / "reasoning.txt").write_text("Continue checking this task.", encoding="utf-8")
                state = SimpleNamespace(
                    route=copy.deepcopy(ROUTE), validate_output=lambda root: None, provenance={"source_review_directory": str(old_call)},
                    read_sources=[], inline_delivered_ranges=copy.deepcopy(old_inline),
                    review_messages=[
                        {"role": "system", "content": "Review calculate behavior."},
                        {"role": "user", "content": '{"checkpoint_from_this_review":"OLD_CHECKPOINT"}'},
                    ], latest_result=failure(reasoning_chars=28), latest_call_dir=old_call,
                )
                config = copy.deepcopy(CONFIG)
                config["context_management"]["inline_material_token_limit"] = 100000
                def respond(call):
                    user = json.loads(next(m["content"] for m in call["chat_messages"] if m.get("role") == "user"))
                    if len(self.calls) == 1:
                        self.assertFalse(call["route_override"]["enable_thinking"])
                        self.assertEqual(user["inline_delivered_material_ranges"], old_inline)
                        self.assertEqual(user["materials_index"]["inline_delivered_ranges"], old_inline)
                        self.assertNotIn("second = 2", json.dumps(user))
                        return completed("RESTORED_CHECKPOINT: newly supplied code still needs review.")
                    self.assertTrue(call["route_override"]["enable_thinking"])
                    self.assertIn("second = 2", json.dumps(user["inline_materials"]))
                    self.assertTrue(user["materials_index"]["inline_delivered_ranges"])
                    return completed()
                result = self.run_flow(respond, config=config, resume_state=state)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(self.calls), 2)
                self.assertTrue(result["coverage"]["inline_delivered_ranges"])
                self.assertEqual(result["tools_executed"], 0)

    def test_failed_short_fragment_preserves_an_earlier_real_partial_report(self):
        partial = '{"findings":[{"title":"EARLIER_SUBSTANTIAL_FINDING","body":"Evidence remains to be verified."}]'
        def respond(call):
            if len(self.calls) == 1:
                result = completed(partial)
                result.update(status="incomplete", finish_reason="length", parse_ok=False,
                              parsed_report=None, parsed_findings=None, seconds=1.5)
                return result
            if len(self.calls) == 2:
                return completed("CHECKPOINT: retain the earlier tentative finding and verify it.")
            result = failure("Unauthorized", http_status=401)
            result["content"] = "short failed fragment"
            return result
        result = self.run_flow(respond)
        self.assertIn(result["status"], {"error", "incomplete"})
        self.assertFalse(result["parse_ok"])
        self.assertEqual(result["content"], partial)
        self.assertEqual(len(self.calls), 3)
        failed = json.loads((self.calls[-1]["directory"] / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "error")
        self.assertEqual(failed["content"], "short failed fragment")
        self.assertEqual(result["timing"]["incomplete_request_seconds"], 1.5)
        self.assertEqual(result["timing"]["failed_request_seconds"], 2.0)
        self.assertEqual(result["timing"]["successful_request_seconds"], 0.25)

    def test_length_and_synthetic_stop_have_distinct_nonfailure_request_timings(self):
        for finish in ("length", "stop"):
            with self.subTest(finish=finish):
                self.calls = []
                self.root = Path(self.temp.name) / ("timing-" + finish)
                def respond(call):
                    if len(self.calls) == 1:
                        result = completed()
                        result.update(status="incomplete", finish_reason=finish, seconds=1.5)
                        return result
                    if len(self.calls) == 2:
                        return completed("CHECKPOINT: report generation reached its length allowance.")
                    return completed()
                result = self.run_flow(respond)
                self.assertEqual(result["timing"]["failed_request_seconds"], 0.0)
                if finish == "length":
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(len(self.calls), 3)
                    self.assertEqual(result["timing"]["incomplete_request_seconds"], 1.5)
                    self.assertEqual(result["timing"]["successful_request_seconds"], 0.5)
                    self.assertEqual(result["timing"]["compaction_request_seconds"], 0.25)
                else:
                    self.assertEqual(result["status"], "incomplete")
                    self.assertEqual(len(self.calls), 1)
                    self.assertEqual(result["timing"]["incomplete_request_seconds"], 0.0)
                    self.assertEqual(result["timing"]["successful_request_seconds"], 1.5)
                    self.assertEqual(result["timing"]["compaction_request_seconds"], 0.0)

    def test_disabled_retry_window_preserves_original_failure_without_wait(self):
        config = copy.deepcopy(CONFIG)
        config["api_recovery"]["retry_window_seconds"] = 0
        result = self.run_flow(lambda call: failure("RESOURCE_EXHAUSTED", http_status=503), config=config)
        self.assertNotEqual(result["status"], "completed")
        self.assertEqual(len(self.calls), 1)
        self.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
