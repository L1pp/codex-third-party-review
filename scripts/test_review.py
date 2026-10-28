"""Offline routing, wire-contract and local review-artifact regressions.

The maintained Node protocol/loop behavior is tested in runtime/test_*.mjs.
These tests never construct a provider or perform a network request.
"""
import base64
import copy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import harness_records
import harness_runner
import ledger
import review
from materials import MaterialStore

CONFIG=review.read_json(review.DEFAULT_CONFIG.with_name("config.example.json"))
FAKE_KEY="sk-test-secret-never-a-real-key"


class RecordFixture:
    """Drive real project records with deterministic already-received outcomes."""
    def __init__(self, root, *, packet=None, key=FAKE_KEY, label="review", callback=None):
        self.root=Path(root);self.root.mkdir(parents=True)
        self.key=key
        self.config=copy.deepcopy(CONFIG);self.config["ledger_file"]=str(self.root.parent/"calls.sqlite3")
        self.route=review.select_route(self.config,"2026-10-27T12:00:00+08:00")
        self.packet=packet or {"task":"Check return contract","files":[{"path":"sample.py","content":"value = 1\n"}]}
        self.store=MaterialStore(self.packet)
        self.start="2026-10-28T12:00:00+00:00"
        self.workflow={"status":"preparing","task":label,"route":self.route,"child_calls":[],"compactions":0,
                       "tool_calls":0,"checkpoint_history":[],"recovery_events":[],"retry_wait_seconds":0.0,
                       "_started_monotonic":time.perf_counter()}
        self.records=harness_records.RuntimeRecords(self.root,self.config,self.route,label,key,self.workflow,{},
                    "https://provider.example.invalid/v1/chat/completions",callback)
    def begin(self, ident="r1", *, purpose="review", payload=None):
        return self.records.request_start({"id":ident,"payload":payload or {"model":"auto","messages":[]},
                       "route":self.route,"purpose":purpose})
    def wire(self, text, *, ident="r1", headers=None, chunks=None):
        self.records.response_event("response.http",{"id":ident,"status":200,
              "headers":headers or {"content-type":"text/event-stream; charset=utf-8"}})
        raw=text.encode("utf-8") if isinstance(text,str) else text
        for chunk in chunks or [raw]:
            self.records.response_event("response.raw",{"id":ident,"base64":base64.b64encode(chunk).decode()})
    def end(self, text='{"findings":[],"context_gaps":[]}', *, ident="r1", status="completed", finish="stop", seconds=.25, **extra):
        value={"id":ident,"status":status,"finish_reason":finish,"content":text,"seconds":seconds,
               "stream_done":True,"usage":{"prompt_tokens":20,"completion_tokens":40}}
        value.update(extra);self.records.request_end(value)
        return self.records.calls[ident]["result"]
    def finish(self, status="completed"):
        self.workflow["status"]=status
        return harness_records._save_outputs(self.root,self.workflow,self.records.final_result,len(self.store.index()),
               self.records.tool_trace,self.records.read_sources,self.start,source_store=self.store)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.ledger_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.ledger_temp.cleanup)
        self.ledger_path = Path(self.ledger_temp.name) / "calls.sqlite3"
        self.config = dict(CONFIG, ledger_file=str(self.ledger_path))
        self.config_path = Path(self.ledger_temp.name) / "config.json"
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        default_config = patch.object(review, "DEFAULT_CONFIG", self.config_path)
        default_config.start()
        self.addCleanup(default_config.stop)

    def test_beijing_boundaries_and_utc_conversion(self):
        for moment, period in [
            ("2026-10-20T07:59:59+08:00", "night"), ("2026-10-20T08:00:00+08:00", "day"),
            ("2026-10-20T21:59:59+08:00", "day"), ("2026-10-20T22:00:00+08:00", "night"),
            ("2026-10-20T00:00:00Z", "day"), ("2026-10-20T14:00:00Z", "night"),
            ("2026-10-20T08:00:00", "day"),
        ]:
            with self.subTest(moment=moment):
                self.assertEqual(review.select_route(CONFIG, moment)["period"], period)

    def test_payload_has_assumed_qwen_auto_limits_and_max_night_without_truncation(self):
        packet = {"files": [{"path": "nonexistent.py", "content": "中文" * 10000}]}
        day = review.make_payload(packet, review.select_route(CONFIG, "2026-10-20T12:00:00+08:00"))
        night = review.make_payload(packet, review.select_route(CONFIG, "2026-10-20T23:00:00+08:00"))
        self.assertEqual(day["model"], "auto")
        for name in ("reasoning_effort", "enable_thinking", "max_tokens", "thinking_budget", "context_window_tokens"):
            self.assertNotIn(name, day)
        self.assertEqual(day["max_completion_tokens"], 393216)
        self.assertNotIn("model_limit_assumption", day)
        self.assertNotIn("max_input_tokens", day)
        self.assertEqual(night["model"], "deepseek-v4.1-flash-1017")
        self.assertEqual(night["reasoning_effort"], "max")
        self.assertTrue(night["enable_thinking"])
        self.assertEqual(night["max_tokens"], 256000)
        self.assertNotIn("thinking_budget", night)
        self.assertIn(packet["files"][0]["content"], night["messages"][1]["content"])

    def test_auto_answer_and_thinking_limits_are_separate_and_context_mapping_still_supported(self):
        route = review.select_route(CONFIG, "2026-10-20T12:00:00+08:00")
        self.assertEqual(route["max_input_tokens"], 983616)
        self.assertEqual(route["model_limit_assumption"], "qwen3.8-flash-or-qwen3.8-max")
        payload = review.make_payload({}, route)
        self.assertEqual(payload["max_completion_tokens"], 393216)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("thinking_budget", payload)
        explicit_total = dict(route, max_tokens=12345, output_limit_parameter="max_completion_tokens")
        self.assertEqual(review.make_payload({}, explicit_total)["max_completion_tokens"], 12345)
        self.assertNotIn("max_tokens", review.make_payload({}, explicit_total))
        night = review.select_route(CONFIG, "2026-10-20T23:00:00+08:00")
        self.assertEqual(review.make_payload({}, night)["max_tokens"], 256000)
        self.assertNotIn("thinking_budget", review.make_payload({}, night))

    def test_auto_combined_route_rejects_unknown_output_parameter(self):
        route = review.select_route(CONFIG, "2026-10-20T12:00:00+08:00")
        route["output_limit_parameter"] = "max_ouput_tokens"
        with self.assertRaises(review.ConfigError):
            review.make_payload({}, route)

    def test_timezone_fallback_is_explicit_and_other_zones_do_not_use_host_time(self):
        with patch.object(review, "ZoneInfo", side_effect=review.ZoneInfoNotFoundError("missing")):
            route = review.select_route(CONFIG, "2026-10-20T00:00:00Z")
            self.assertEqual(route["local_time"], "2026-10-20T08:00:00+08:00")
            self.assertEqual(route["timezone_source"], "fixed_utc_plus_08_fallback")
            other = dict(CONFIG, timezone="Europe/London")
            with self.assertRaisesRegex(ValueError, "Timezone data unavailable"):
                review.select_route(other)
            with patch.object(review, "read_json", return_value=other), patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(review.main(["--route-only"]), 2)
                self.assertIn("install tzdata", output.getvalue())

    def test_parse_report_preserves_direct_json_and_complete_fence_compatibility(self):
        report = {"findings": [{"issue": "存在一个问题"}], "context_gaps": []}
        raw = json.dumps(report, ensure_ascii=False)
        self.assertEqual(review.parse_report(raw), (report, report["findings"]))
        fenced = "```json\n" + json.dumps(report, ensure_ascii=False, indent=2) + "\n```"
        self.assertEqual(review.parse_report(fenced), (report, report["findings"]))

    def test_parse_report_extracts_one_json_report_from_prose_and_single_fence(self):
        report = {"findings": [{"issue": "内联额度未生效", "file": "config.json"}], "context_gaps": []}
        content = (
            "先说明本次核对范围。\n\n```\n"
            + json.dumps(report, ensure_ascii=False, indent=2)
            + "\n```\n\n以上是完整结果。"
        )
        self.assertEqual(review.parse_report(content), (report, report["findings"]))

    def test_parse_report_does_not_fabricate_findings_for_invalid_candidate(self):
        invalid = "前言。\n```\n{\"context_gaps\": []}\n```\n结语。"
        with self.assertRaisesRegex(ValueError, "No valid findings report JSON found"):
            review.parse_report(invalid)

    def test_parse_report_rejects_multiple_different_valid_candidates(self):
        first = {"findings": [{"issue": "问题A"}], "context_gaps": []}
        second = {"findings": [{"issue": "问题B"}], "context_gaps": []}
        ambiguous = (
            "候选一：\n```\n" + json.dumps(first, ensure_ascii=False) + "\n```\n"
            "候选二：\n```\n" + json.dumps(second, ensure_ascii=False) + "\n```"
        )
        with self.assertRaisesRegex(ValueError, "Multiple valid findings reports found"):
            review.parse_report(ambiguous)

    def test_output_directory_is_absolute_and_known_key_is_redacted(self):
        result = {"started_utc": "2026-10-23T16:00:00+00:00", "status": "completed"}
        metadata = review.ledger_metadata(result, "short title", FAKE_KEY, Path("relative") / ("中文  目录-" + FAKE_KEY))
        self.assertTrue(Path(metadata["output_dir"]).is_absolute())
        self.assertIn("中文  目录-", metadata["output_dir"])
        self.assertNotIn(FAKE_KEY, metadata["output_dir"])
        self.assertIn("[REDACTED]", metadata["output_dir"])

    def test_auto_combined_wire_cap_is_separate_from_summary_cap(self):
        route=review.select_route(CONFIG,"2026-10-20T12:00:00+08:00")
        self.assertEqual(review.make_payload({},dict(route,max_tokens=2650,thinking_budget=5302))["max_completion_tokens"],7952)
        compact=review.make_payload({},dict(route,max_tokens=16384,thinking_budget=0,enable_thinking=False))
        self.assertEqual(compact["max_completion_tokens"],16384)
        self.assertFalse(compact["enable_thinking"])
        self.assertNotIn("thinking_budget",compact)

    def test_route_history_and_dry_run_never_load_credentials_or_launch_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);packet=root/"packet.json"
            packet.write_text(json.dumps({"files":[{"path":"Z:/never-open.py","content":"pass"}]}),encoding="utf-8")
            with patch.object(review,"load_credential",side_effect=AssertionError("No key read")), \
                 patch.object(harness_runner,"_run_bridge",side_effect=AssertionError("No runtime/model")), \
                 patch.object(harness_runner,"_ensure_runtime",side_effect=AssertionError("No dependency installation")), \
                 patch("sys.stdout",new_callable=io.StringIO):
                self.assertEqual(review.main(["--route-only"]),0)
                self.assertEqual(review.main(["--history"]),0)
                self.assertEqual(review.main(["--packet",str(packet),"--output-dir",str(root/"run"),"--dry-run"]),0)
            result=review.read_json(root/"run/result.json")
            self.assertEqual(result["model_calls"],0)
            self.assertFalse(self.ledger_path.exists())
            request=review.read_json(root/"run/calls/0001-review/request.json")
            self.assertIn("pass",request["payload"]["messages"][1]["content"])

    def test_live_cli_redacts_selected_packet_and_title_before_host(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);packet=root/"packet.json"
            original={"task":"review","files":[{"path":"a.py","content":"accidental "+FAKE_KEY}]}
            packet.write_text(json.dumps(original),encoding="utf-8")
            with patch.object(review,"load_credential",return_value=FAKE_KEY), \
                 patch.object(harness_runner,"run_harness_workflow",return_value={"status":"completed"}) as run, \
                 patch("sys.stdout",new_callable=io.StringIO):
                self.assertEqual(review.main(["--packet",str(packet),"--output-dir",str(root/"out"),"--task","validation "+FAKE_KEY,"--quiet-progress"]),0)
            self.assertNotIn(FAKE_KEY,json.dumps(run.call_args.args[1]))
            self.assertNotIn(FAKE_KEY,run.call_args.kwargs["task_label"])
            self.assertEqual(json.loads(packet.read_text(encoding="utf-8")),original)

    def test_ledger_contains_metadata_and_absolute_locator_without_source_or_key(self):
        fixture=RecordFixture(Path(self.ledger_temp.name)/"recorded",label="修复定位 [REDACTED]")
        fixture.begin(payload={"model":"auto","messages":[{"role":"user","content":"PRIVATE_SOURCE"}]})
        fixture.end(reported_model="reported-model",first_content_seconds=.1)
        result=fixture.finish();rows=ledger.history(fixture.config["ledger_file"])["records"]
        self.assertEqual(len(rows),1);row=rows[0]
        self.assertEqual(row["task"],"修复定位 [REDACTED]")
        self.assertEqual(row["reported_model"],"reported-model")
        self.assertTrue(Path(row["output_dir"]).is_absolute())
        self.assertIsNotNone(row["first_content_utc"])
        self.assertEqual(row["outcome"],"success")
        raw=Path(fixture.config["ledger_file"]).read_bytes()
        self.assertNotIn(b"PRIVATE_SOURCE",raw);self.assertNotIn(FAKE_KEY.encode(),raw)
        self.assertTrue(result["parse_ok"])

    def test_response_log_io_failure_is_visible_without_changing_the_report(self):
        class BrokenLog:
            def write(self,text):raise OSError("private logging path")
            def flush(self):pass
            def close(self):pass
        fixture=RecordFixture(Path(self.ledger_temp.name)/"log-warning");fixture.begin()
        original=fixture.records.calls["r1"]["audit"].log.reasoning_file
        if original is not None:original.close()
        fixture.records.calls["r1"]["audit"].log.reasoning_file=BrokenLog()
        fixture.records.response_event("response.reasoning",{"id":"r1","text":"received review reasoning"})
        child=fixture.end();result=fixture.finish()
        self.assertEqual(result["status"],"completed")
        self.assertIn("response_log_warning",child)
        self.assertNotIn("private logging path",child["response_log_warning"])
        self.assertIn("response_log_warning",review.read_json(fixture.root/"calls/0001-review/result.json"))

    def test_provider_failure_keeps_received_report_and_masks_the_active_key(self):
        fixture=RecordFixture(Path(self.ledger_temp.name)/"provider-failure");fixture.begin()
        wire='data: '+json.dumps({"error":{"code":"GatewayTimeout","message":"provider "+FAKE_KEY}})+'\n\n'
        fixture.wire(wire)
        fixture.end("partial visible finding "+FAKE_KEY,status="error",finish=None,
                    error_type="ProviderError",provider_error={"code":"GatewayTimeout","message":FAKE_KEY})
        result=fixture.finish("error")
        self.assertEqual(result["status"],"error");self.assertTrue(result["content_is_partial"])
        self.assertIn("partial visible finding",result["content"])
        for path in fixture.root.rglob("*"):
            if path.is_file():self.assertNotIn(FAKE_KEY,path.read_text(encoding="utf-8",errors="replace"))

    def test_advisory_ledger_failure_does_not_change_completed_report(self):
        fixture=RecordFixture(Path(self.ledger_temp.name)/"ledger-warning")
        with patch.object(ledger,"start_call",side_effect=RuntimeError("local failure")):
            fixture.begin()
        fixture.end();result=fixture.finish()
        self.assertEqual(result["status"],"completed")
        self.assertIn("ledger",review.read_json(fixture.root/"calls/0001-review/result.json")["ledger_warning"].lower())


if __name__=="__main__":unittest.main()
