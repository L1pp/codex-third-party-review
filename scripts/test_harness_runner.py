"""Offline host protocol regressions; no Node model request or network is used."""
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

try:
    from . import harness_runner as runner
    from . import ledger
except ImportError:
    import harness_runner as runner
    import ledger

KEY = "sk-host-offline-secret-value"
ROUTE = {"model": "auto", "max_tokens": 131072, "thinking_budget": 262144,
         "combined_output_limit_parameter": "max_completion_tokens", "context_window_tokens": 1000000}
PACKET = {"task": "Review one supplied function", "files": [{"path": "calc.py", "content": "value = 1\n"}]}
REPORT = '{"findings":[],"context_gaps":[]}'


def line(value):
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


def req(ident, method, params):
    return {"request": {"id": ident, "method": method, "params": params}}


def event(name, data):
    return {"event": name, "data": data}


class CaptureInput(io.BytesIO):
    def close(self):
        self.saved = self.getvalue()
        super().close()


class FakeProcess:
    def __init__(self, messages, stderr=b""):
        self.stdin = CaptureInput()
        self.stdout = io.BytesIO(b"".join(line(value) for value in messages))
        self.stderr = io.BytesIO(stderr)
        self.returncode = 0
        self.terminated = False
    def wait(self, timeout=None):
        return self.returncode
    def terminate(self):
        self.terminated = True
    def kill(self):
        self.terminated = True


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"base_url": "https://provider.example.invalid/v1", "automatic_retries": 0,
                       "connect_timeout_seconds": 30, "read_timeout_seconds": 300,
                       "ledger_file": str(self.root / "calls.sqlite3"), "node_executable": "node-fixture",
                       "context_management": {"inline_material_token_limit": 0}}
        self.module = runner._review_module()
        handle = patch.object(self.module, "load_credential", return_value=KEY)
        handle.start(); self.addCleanup(handle.stop)
        self.key_loader = handle
        handle = patch.object(runner, "_ensure_runtime", return_value={"executable":"node-fixture"})
        handle.start(); self.addCleanup(handle.stop)
        self.out = self.root / "review"

    def execute(self, messages, **kwargs):
        process = FakeProcess(messages, b"runtime diagnostic " + KEY.encode())
        with patch.object(runner.subprocess, "Popen", return_value=process) as launch:
            result = runner.run_harness_workflow(self.config, PACKET, self.out,
                       route_selector=lambda config, at=None: ROUTE, **kwargs)
        self.session = process
        self.launch = launch
        return result

    def test_engine_startup_error_keeps_its_safe_diagnostic_without_an_api_call(self):
        message="Session format is newer than this runtime " + KEY
        result=self.execute([event("engine.complete",{"status":"error","error_type":"SessionFormatError","error_message":message,"finalContent":"","finishReason":None})])
        self.assertEqual(result["status"],"error")
        self.assertEqual(result["model_calls"],0)
        self.assertIn("Session format is newer",result["uncompleted_reason"])
        self.assertEqual(result["engine"]["error_type"],"SessionFormatError")
        self.assertNotIn(KEY,json.dumps(result))
    def test_missing_node_reports_the_actionable_setup_error_without_an_api_call(self):
        from node_runtime import NodeRuntimeError
        with patch.object(runner,"_ensure_runtime",side_effect=NodeRuntimeError("Node.js >=24.9.0 is required; run Skill setup")):
            result=runner.run_harness_workflow(self.config,PACKET,self.out,route_selector=lambda cfg,at=None:ROUTE)
        self.assertEqual(result["model_calls"],0)
        self.assertIn("Node.js >=24.9.0",result["uncompleted_reason"])

    def test_initial_source_distribution_respects_declared_input_cap_and_keeps_overflow_readable(self):
        from materials import MaterialStore
        from context_budget import estimate_tokens
        text="source_payload = 1\n"*3000
        store=MaterialStore({"task":"Review the supplied source.","files":[{"path":"large.py","content":text}]})
        cfg=dict(self.config,context_management={"inline_material_token_limit":100000})
        route=dict(ROUTE,max_input_tokens=15000)
        bundle,tools,messages=runner._material_context(store,cfg,route)
        self.assertLessEqual(estimate_tokens(messages,tools,route["model"])["input_tokens"],15000)
        self.assertTrue(bundle["remaining_materials"])
        self.assertIn("source_payload",store.execute("read_material",{"path":"large.py","line_count":1})["text"])
    def test_impossible_base_input_cap_stops_locally_without_starting_a_runtime(self):
        from materials import MaterialStore
        with patch.object(runner,"_run_bridge",side_effect=AssertionError("No runtime")):
            with self.assertRaisesRegex(ValueError,"input limit"):
                runner._material_context(MaterialStore(PACKET),self.config,dict(ROUTE,max_input_tokens=1))

    def test_inline_delivery_comes_from_the_actual_wire_not_prepared_bundle(self):
        from harness_records import inline_ranges_in_request
        scope={"path":"calc.py","line_start":1,"line_end":1}
        envelope={"materials_index":{"inline_delivered_ranges":[scope]},"inline_materials":[{"path":"calc.py","text":"value=1"}]}
        self.assertEqual(inline_ranges_in_request({"messages":[{"role":"user","content":json.dumps(envelope)}]}),[scope])
        self.assertEqual(inline_ranges_in_request({"messages":[{"role":"user","content":"<compacted-summary>only progress, no original source</compacted-summary>"}]}),[])
        self.assertEqual(inline_ranges_in_request({"messages":[{"role":"user","content":json.dumps({"materials_index":{"inline_delivered_ranges":[scope]}})}]}),[])

    def test_checkpoint_export_failure_warns_without_interrupting_committed_session(self):
        messages=[event("context.checkpoint",{"text":"Committed review checkpoint","reason":"official_compaction"}),
                  req("start","requestStart",{"id":"r1","payload":{"model":"auto","messages":[]},"route":ROUTE}),
                  req("end","requestEnd",{"id":"r1","status":"completed","finish_reason":"stop","content":REPORT,"seconds":0.1}),
                  event("engine.complete",{"status":"completed","sessionId":"s","finalContent":REPORT,"finishReason":"completed"})]
        write=Path.write_text
        def blocked(path,*args,**kwargs):
            if path.name=="checkpoint.txt":raise PermissionError("synthetic held derived export")
            return write(path,*args,**kwargs)
        with patch.object(Path,"write_text",new=blocked):result=self.execute(messages)
        self.assertEqual(result["status"],"completed",result)
        self.assertIn("PermissionError",result["checkpoint_export_warning"])
        self.assertTrue(Path(result["checkpoint_file"]).is_file())
        child=json.loads((self.out/"calls/0001-review/result.json").read_text(encoding="utf-8"))
        self.assertEqual(child["status"],"completed")

    def test_explicit_attempts_tool_dispatch_and_redacted_wire_records(self):
        reasoning = ["中文 " + KEY[:8], KEY[8:] + " done"]
        wire = ''.join('data: ' + json.dumps({"choices":[{"delta":{"reasoning_content":part}}]}, ensure_ascii=False) + '\r\n\r\n' for part in reasoning).encode()
        messages = [req("start1", "requestStart", {"id":"r1", "payload":{"model":"auto","messages":[]}, "route":ROUTE}),
                    event("response.http", {"id":"r1","status":200,"headers":{"content-type":"text/event-stream"}})]
        messages += [event("response.raw", {"id":"r1","base64":base64.b64encode(wire[:7]).decode()}),
                     event("response.raw", {"id":"r1","base64":base64.b64encode(wire[7:]).decode()})]
        messages += [event("response.reasoning", {"id":"r1","text":text}) for text in reasoning]
        messages += [req("end1","requestEnd", {"id":"r1","status":"tool_calls","finish_reason":"tool_calls","seconds":0.3,"content":"","tool_calls":[{"id":"t1"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}),
                     req("tool1","executeTool", {"name":"read_material","arguments":{"path":"calc.py"},"callId":"t1"}),
                     req("start2","requestStart", {"id":"r2","payload":{"model":"auto","messages":[]},"route":ROUTE}),
                     req("end2","requestEnd", {"id":"r2","status":"completed","finish_reason":"stop","stream_done":True,"seconds":0.4,"content":REPORT,"usage":{"prompt_tokens":11,"completion_tokens":7}}),
                     event("engine.complete", {"status":"completed","finalContent":REPORT,"sessionId":"saved-session","finishReason":"completed","retryWaitSeconds":0})]
        result = self.execute(messages)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["parse_ok"])
        self.assertEqual(result["model_calls"], 2)
        self.assertEqual(result["tools_executed"], 1)
        self.assertAlmostEqual(result["timing"]["successful_request_seconds"], 0.7)
        self.assertTrue((self.out / "tool_results.jsonl").is_file())
        self.assertEqual(len(ledger.history(self.config["ledger_file"])["records"]), 2)
        received = [json.loads(raw) for raw in self.session.stdin.saved.splitlines()]
        self.assertEqual(received[0]["secret"]["apiKey"], KEY)
        self.assertNotIn(KEY, json.dumps(received[0]["spec"]))
        replies = {row["reply"]:row for row in received[1:]}
        self.assertIn("value = 1", replies["tool1"]["result"]["text"])
        self.assertTrue(replies["start1"]["result"]["recorded"])
        raw_text = (self.out / "calls/0001-review/response_stream.jsonl").read_text(encoding="utf-8")
        self.assertIn("中文", raw_text)
        self.assertIn("[REDACTED]", raw_text)
        self.assertNotIn(KEY, raw_text)
        for path in self.out.rglob("*"):
            if path.is_file():
                self.assertNotIn(KEY.encode(), path.read_bytes(), str(path))
        args, options = self.launch.call_args
        self.assertNotIn(KEY, str(args))
        self.assertNotIn("HTTPS_PROXY", options["env"])

    def test_dry_run_does_not_launch_node_or_read_credentials(self):
        with patch.object(runner.subprocess, "Popen", side_effect=AssertionError("No runtime in dry run")), \
             patch.object(self.module, "load_credential", side_effect=AssertionError("No credentials in dry run")):
            result = runner.run_harness_workflow(self.config, PACKET, self.out,
                       route_selector=lambda config, at=None: ROUTE, dry_run=True)
        self.assertEqual(result["status"], "dry_run")
        self.assertEqual(result["model_calls"], 0)
        self.assertTrue((self.out / "calls/0001-review/request.json").exists())
        self.assertFalse((self.root / "calls.sqlite3").exists())

    def test_failed_attempts_and_recovery_wait_are_not_success_timing(self):
        messages = [req("start1","requestStart", {"id":"r1","payload":{},"route":ROUTE}),
                    req("end1","requestEnd", {"id":"r1","status":"error","seconds":4,"error_type":"ProviderError","content":"","provider_error":{"code":"GatewayTimeout"}}),
                    req("start2","requestStart", {"id":"r2","payload":{},"route":ROUTE}),
                    req("end2","requestEnd", {"id":"r2","status":"completed","seconds":2,"content":REPORT,"finish_reason":"stop"}),
                    event("engine.complete", {"status":"completed","finalContent":REPORT,"finishReason":"completed","retryWaitSeconds":1.25})]
        result = self.execute(messages)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["timing"]["failed_request_seconds"], 4)
        self.assertEqual(result["timing"]["successful_request_seconds"], 2)
        self.assertEqual(result["timing"]["retry_wait_seconds"], 1.25)
        old = json.loads((self.out / "calls/0001-review/result.json").read_text(encoding="utf-8"))
        self.assertEqual(old["status"], "error")

    def test_unexpected_process_eof_preserves_partial_and_marks_failure(self):
        partial = '{"findings":[{"issue":"unfinished"}'
        result = self.execute([req("start","requestStart", {"id":"r1","payload":{},"route":ROUTE}),
                               req("end","requestEnd", {"id":"r1","status":"incomplete","seconds":1,"content":partial,"finish_reason":"length"})])
        self.assertEqual(result["status"], "error")
        self.assertTrue(result["content_is_partial"])
        self.assertIn("unfinished", result["content"])
        self.assertEqual(result["timing"]["incomplete_request_seconds"], 1)

    def test_request_start_io_failure_is_a_negative_ack_before_any_tool(self):
        actual = runner.RuntimeRecords.request_start
        with patch.object(runner.RuntimeRecords, "request_start", side_effect=PermissionError("private " + KEY)):
            result = self.execute([req("start","requestStart", {"id":"r1","payload":{},"route":ROUTE}),
                                   event("engine.complete", {"status":"error","finishReason":"HOST_IO_ERROR"})])
        self.assertEqual(result["status"], "error")
        received = [json.loads(raw) for raw in self.session.stdin.saved.splitlines()]
        self.assertEqual(received[1]["error"]["code"], "HOST_IO_ERROR")
        self.assertNotIn(KEY, json.dumps(received[1]))
        self.assertEqual(result["model_calls"], 0)

    def test_report_parser_failure_never_skips_raw_attempt_persistence(self):
        try:
            from . import harness_records
        except ImportError:
            import harness_records
        with patch.object(harness_records,"parse_report_text",side_effect=RuntimeError("private parse detail " + KEY)):
            result=self.execute([req("start","requestStart",{"id":"r1","payload":{},"route":ROUTE}),
                                 req("end","requestEnd",{"id":"r1","status":"completed","content":REPORT,"seconds":1}),
                                 event("engine.complete",{"status":"completed","finalContent":REPORT,"finishReason":"completed"})])
        self.assertEqual(result["status"],"incomplete")
        self.assertFalse(result["parse_ok"])
        raw=json.loads((self.out/"calls/0001-review/result.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["content"],REPORT)
        self.assertEqual(raw["report_recovery"]["error"],"RuntimeError")
        self.assertNotIn(KEY,json.dumps(raw))

    def test_invalid_report_never_becomes_completed(self):
        result = self.execute([req("start","requestStart", {"id":"r1","payload":{},"route":ROUTE}),
                               req("end","requestEnd", {"id":"r1","status":"completed","content":"plain prose","seconds":1}),
                               event("engine.complete", {"status":"completed","finalContent":"plain prose","finishReason":"completed"})])
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["parse_ok"])

    def test_resume_boundary_is_validated_before_creating_directories(self):
        def reject(path):
            raise ValueError("Overlapping prior run")
        state = SimpleNamespace(validate_output=reject)
        with self.assertRaises(ValueError):
            runner.run_harness_workflow(self.config, PACKET, self.out,
                       route_selector=lambda config, at=None: ROUTE, resume_state=state)
        self.assertFalse(self.out.exists())

    def test_single_request_keeps_direct_artifacts_and_disables_host_tools(self):
        process = FakeProcess([
            req("start","requestStart", {"id":"r1","payload":{"model":"auto"},"route":ROUTE}),
            req("end","requestEnd", {"id":"r1","status":"completed","seconds":0.2,"content":REPORT,"finish_reason":"stop"}),
            event("engine.complete", {"status":"completed","finalContent":REPORT}),
        ])
        with patch.object(runner.subprocess,"Popen",return_value=process):
            result = runner.run_single_request(self.config, PACKET, self.out, route_override=ROUTE)
        self.assertEqual(result["status"],"completed")
        self.assertTrue(result["parse_ok"])
        self.assertTrue((self.out/"request.json").is_file())
        self.assertTrue((self.out/"result.json").is_file())
        self.assertFalse((self.out/"calls").exists())
        self.assertFalse((self.out/"workflow.json").exists())
        self.assertEqual(len(ledger.history(self.config["ledger_file"])["records"]),1)
        sent = [json.loads(raw) for raw in process.stdin.saved.splitlines()]
        self.assertEqual(sent[0]["method"],"single")
        self.assertEqual(sent[0]["payload"]["model"],"auto")

    def test_single_dry_run_has_no_node_and_no_credential_access(self):
        with patch.object(runner,"_ensure_runtime",side_effect=AssertionError("No setup in offline mode")), \
             patch.object(runner.subprocess,"Popen",side_effect=AssertionError("No Node in dry mode")), \
             patch.object(self.module,"load_credential",side_effect=AssertionError("No key in dry mode")):
            result = runner.run_single_request(self.config,PACKET,self.out,dry_run=True,route_override=ROUTE)
        self.assertEqual(result["status"],"dry_run")
        self.assertFalse((self.root/"calls.sqlite3").exists())
        self.assertTrue((self.out/"request.json").is_file())

    def test_operator_cancel_drains_request_end_and_keeps_partial(self):
        original = runner.RuntimeRecords.response_event
        interrupted = []
        def callback(records,event_name,data):
            original(records,event_name,data)
            if event_name == "response.reasoning" and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt()
        partial='{"findings":[{"issue":"partial item"}'
        with patch.object(runner.RuntimeRecords,"response_event",callback):
            result=self.execute([
                req("start","requestStart",{"id":"r1","payload":{},"route":ROUTE}),
                event("response.reasoning",{"id":"r1","text":"already received task progress"}),
                req("end","requestEnd",{"id":"r1","status":"interrupted","seconds":0.3,"content":partial,"error_type":"AbortError"}),
                event("engine.complete",{"status":"interrupted","partialContent":partial,"finishReason":"aborted"}),
            ])
        self.assertEqual(result["status"],"incomplete")
        self.assertIn("partial item",result["content"])
        request=json.loads((self.out/"calls/0001-review/result.json").read_text(encoding="utf-8"))
        self.assertEqual(request["status"],"interrupted")
        sent=[json.loads(raw) for raw in self.session.stdin.saved.splitlines()]
        self.assertTrue(any(item.get("cancel") is True for item in sent))
        self.assertTrue(any(item.get("reply")=="end" and "result" in item for item in sent))

    def test_legacy_progress_filters_operational_and_tool_logs(self):
        old=self.root/"old";old.mkdir();call=old/"calls";call.mkdir()
        (call/"reasoning.txt").write_text("SAME_REVIEW_REASONING",encoding="utf-8")
        state=SimpleNamespace(directory=old,review_messages=[
            {"role":"user","content":'{"checkpoint_from_this_review":"PRIOR_TASK_CHECKPOINT"}'},
            {"role":"assistant","content":"CONFIRMED_TASK_PROGRESS","reasoning_content":"OLDER_TASK_REASONING"},
            {"role":"tool","content":"OPS_TOOL_LOG_SENTINEL"},
            {"role":"user","content":"MAIN_HOST_VERDICT_SENTINEL"}],
            latest_result={"content":"FINAL_PARTIAL_TASK","provider_error":{"message":"OPS_PROVIDER_ERROR_SENTINEL"}},
            latest_call_dir=call,read_sources=[{"path":"calc.py","start_line":1}],inline_delivered_ranges=[],
            previous_checkpoint="PRIOR_TASK_CHECKPOINT")
        progress=runner._legacy_progress(state)
        self.assertEqual(set(progress),{"prior_checkpoint","completed_work","internal_notes","read_sources","inline_delivered_ranges"})
        text=json.dumps(progress)
        self.assertIn("SAME_REVIEW_REASONING",text)
        self.assertIn("FINAL_PARTIAL_TASK",text)
        self.assertNotIn("OPS_",text)
        self.assertNotIn("MAIN_HOST_VERDICT",text)

    def test_final_result_and_stderr_mask_current_key(self):
        report = json.dumps({"findings":[{"issue":KEY}],"context_gaps":[]})
        result = self.execute([req("start","requestStart", {"id":"r1","payload":{"message":KEY},"route":ROUTE}),
                               req("end","requestEnd", {"id":"r1","status":"completed","content":report,"seconds":1}),
                               event("engine.complete", {"status":"completed","finalContent":report,"finishReason":"completed"})])
        self.assertNotIn(KEY, json.dumps(result))
        self.assertIn("[REDACTED]", result["content"])
        self.assertIn("[REDACTED]", (self.out / "runtime_stderr.txt").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
