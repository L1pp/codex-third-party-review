"""Offline route/transport tests. No real credentials, model calls, or repository code execution."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review
import response_log


CONFIG = review.read_json(review.DEFAULT_CONFIG.with_name("config.example.json"))
FAKE_KEY = "sk-test-secret-never-a-real-key"


class PartialEventThenError:
    def __init__(self, body, error):
        self.body = body
        self.error = error


class FakeResponse:
    def __init__(self, events=None, body=None, status=200, text=""):
        self.events, self.body = events, body
        self.status_code, self.text = status, text
        self.headers = {
            "Content-Type": "text/event-stream; charset=utf-8" if events is not None else "application/json",
            "X-Request-Id": "req-test-123", "Trace-Id": "trace-test-456",
            "Authorization": "must-not-log", "Set-Cookie": "secret-cookie",
        }
        self.encoding = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def json(self):
        if isinstance(self.body, (str, bytes)):
            return json.loads(self.body)
        return self.body

    @property
    def content(self):
        if isinstance(self.body, bytes):
            return self.body
        if isinstance(self.body, str):
            return self.body.encode("utf-8")
        if self.body is not None:
            return json.dumps(self.body, ensure_ascii=False).encode("utf-8")
        return self.text.encode("utf-8")

    def iter_content(self, **kwargs):
        yield ": keepalive\n\n"
        for item in self.events:
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, PartialEventThenError):
                value = json.dumps(item.body, ensure_ascii=False)
                yield "data: " + value + "\n"
                raise item.error
            value = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
            yield "data: " + value + "\n\n"


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.trust_env = True
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


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

    def test_auto_wire_total_uses_effective_split_caps_and_compression_has_no_thinking(self):
        import workflow
        route = review.select_route(CONFIG, "2026-10-20T12:00:00+08:00")
        limited = dict(route, max_tokens=2650, thinking_budget=5302)
        payload = review.make_payload({}, limited)
        self.assertEqual(payload["max_completion_tokens"], 7952)
        self.assertNotIn("max_tokens", payload)
        self.assertNotIn("thinking_budget", payload)
        compact = workflow._compression_route(route, 16384)
        payload = review.make_payload({}, compact)
        self.assertEqual(payload["max_completion_tokens"], 16384)
        self.assertFalse(payload["enable_thinking"])
        self.assertNotIn("thinking_budget", payload)

    def test_real_budget_to_route_to_payload_keeps_both_models_within_context(self):
        import context_budget
        import workflow
        config = dict(CONFIG, context_window_tokens=1_000_000)
        messages = [{"role": "user", "content": "bounded context integration " * 100}]
        for moment, field in [("2026-10-20T12:00:00+08:00", "max_completion_tokens"),
                              ("2026-10-20T23:00:00+08:00", "max_tokens")]:
            with self.subTest(moment=moment):
                route = review.select_route(config, moment)
                budget = context_budget.plan_budget(messages, [], route, config)
                self.assertFalse(budget["needs_compaction"])
                self.assertFalse(budget["output_limited_by_context"])
                self.assertEqual(budget["effective_output_tokens"], 256000 if field == "max_tokens" else 393216)
                effective_route = workflow._review_route_with_budget(route, budget)
                payload = review.make_payload({}, effective_route, chat_messages=messages)
                self.assertEqual(payload[field], budget["effective_output_tokens"])
                self.assertLessEqual(payload[field], budget["remaining_tokens"])
                self.assertLessEqual(budget["input_tokens"] + payload[field] + budget["safety_tokens"],
                                     budget["context_limit"])
                self.assertNotIn("thinking_budget", payload)

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

    def test_route_only_and_dry_run_do_not_load_key_or_network_or_repository_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            packet = folder / "packet.json"
            packet.write_text(json.dumps({"files": [{"path": "Z:/does-not-exist/never-open.py", "content": "pass"}]}), encoding="utf-8")
            with patch.object(review, "load_credential", side_effect=AssertionError("must not load key")), \
                    patch.object(review.requests, "Session", side_effect=AssertionError("must not use network")), \
                    patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(review.main(["--route-only", "--at", "2026-10-20T12:00:00+08:00"]), 0)
                self.assertEqual(review.main(["--packet", str(packet), "--output-dir", str(folder / "run"), "--dry-run", "--at", "2026-10-20T23:00:00+08:00"]), 0)
            requests = list((folder / "run" / "calls").glob("*/request.json"))
            self.assertEqual(len(requests), 1)
            request = review.read_json(requests[0])
            self.assertEqual(request["payload"]["max_tokens"], 256000)
            self.assertNotIn("Authorization", requests[0].read_text(encoding="utf-8"))
            self.assertNotIn("tools", request["payload"])
            user_data = json.loads(request["payload"]["messages"][1]["content"])
            self.assertEqual(user_data["inline_materials"][0]["text"], "pass")
            self.assertEqual(user_data["materials_index"]["overflow_materials"], [])

    def execute_mock(self, response, packet=None, task_label=None, **review_kwargs):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "new-review"
        session = FakeSession(response)
        with patch.object(review, "load_credential", return_value=FAKE_KEY), patch.object(review.requests, "Session", return_value=session):
            result = review.run_review(self.config, {"task": "test"} if packet is None else packet, output, task_label=task_label, **review_kwargs)
        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.trust_env)
        self.assertTrue(session.closed)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        self.assertEqual(session.calls[0][1]["timeout"], (30.0, 300.0))
        return result, output, session

    @staticmethod
    def recorded_sse_lines(output):
        return [json.loads(line)["line"] for line in (output / "response_stream.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_tool_call_fragments_are_reassembled_and_count_as_successful_api_round(self):
        tools=[{"type":"function","function":{"name":"read_material","parameters":{"type":"object"}}}]
        messages=[{"role":"system","content":"read-only"},{"role":"user","content":"task index only"}]
        result,output,session=self.execute_mock(FakeResponse(events=[
            {"choices":[{"delta":{"reasoning_content":"读取必要源码"}}]},
            {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"read_","arguments":"{\"path\":"}}]}}]},
            {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"material","arguments":"\"src/a.py\"}"}}]},"finish_reason":"tool_calls"}]},
            "[DONE]",
        ]),chat_messages=messages,tools=tools,token_budget={"input_tokens":10,"effective_output_tokens":100})
        self.assertEqual(result["status"],"tool_calls")
        self.assertNotIn("parse_warning",result)
        self.assertEqual(result["tool_calls"][0]["function"]["name"],"read_material")
        self.assertEqual(json.loads(result["tool_calls"][0]["function"]["arguments"]),{"path":"src/a.py"})
        self.assertEqual((output/"reasoning.txt").read_text(encoding="utf-8"),"读取必要源码")
        self.assertEqual(session.calls[0][1]["json"]["messages"],messages)
        self.assertEqual(review.ledger.history(self.ledger_path)["records"][0]["outcome"],"success")
        self.assertEqual(review.read_json(output/"token_budget.json")["input_tokens"],10)

    def test_nonstream_multiple_tool_calls_do_not_merge_missing_indices(self):
        calls=[{"id":"one","type":"function","function":{"name":"read_material","arguments":"{}"}},
               {"id":"two","type":"function","function":{"name":"list_materials","arguments":"{}"}}]
        result,_,_=self.execute_mock(FakeResponse(body={"choices":[{"message":{"tool_calls":calls},"finish_reason":"tool_calls"}]}),tools=[{"type":"function","function":{"name":"read_material"}}])
        self.assertEqual(result["tool_calls"],calls)
        self.assertEqual(result["status"],"tool_calls")

    def test_packet_credential_is_redacted_in_actual_request_without_changing_authorization(self):
        _, output, session = self.execute_mock(
            FakeResponse(body={"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]}),
            packet={"files": [{"path": "config.txt", "content": "accidental credential: " + FAKE_KEY}]},
        )
        sent = session.calls[0][1]
        self.assertEqual(sent["headers"]["Authorization"], "Bearer " + FAKE_KEY)
        payload = json.dumps(sent["json"], ensure_ascii=False)
        self.assertNotIn(FAKE_KEY, payload)
        self.assertIn("[REDACTED]", payload)
        self.assertNotIn(FAKE_KEY, (output / "request.json").read_text(encoding="utf-8"))

    def test_chinese_sse_reasoning_and_raw_events_are_saved_and_empty_findings_is_valid(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "completion_tokens_details": {"reasoning_tokens": 15}}
        hidden = "需要保留的中文思考内容"
        report = '{"findings": [], "context_gaps": ["缺少调用方"]}'
        result, output, _ = self.execute_mock(FakeResponse(events=[
            {"model": "reported-model", "choices": [{"index": 0, "delta": {"reasoning_content": hidden}}]},
            {"choices": [{"delta": {"content": report[:20]}}]},
            {"choices": [{"delta": {"content": report[20:]}, "finish_reason": "stop"}]},
            {"choices": [], "usage": usage}, "[DONE]",
        ]))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["parse_ok"])
        self.assertEqual(result["parsed_findings"], [])
        self.assertEqual(result["content"], report)
        self.assertEqual(result["usage"], usage)
        self.assertEqual(result["reasoning_chars"], len(hidden))
        self.assertEqual(result["reported_model"], "reported-model")
        self.assertTrue(result["stream_done"])
        self.assertIsNotNone(result["first_content_seconds"])
        self.assertEqual((output / "reasoning.txt").read_text(encoding="utf-8"), hidden)
        raw_log = "\n".join(self.recorded_sse_lines(output))
        self.assertIn(hidden, raw_log)
        self.assertIn("data: [DONE]", raw_log)
        self.assertIn('"reasoning_tokens": 15', raw_log)
        self.assertIn('"finish_reason": "stop"', raw_log)
        metadata = review.read_json(output / "response_metadata.json")
        self.assertEqual(metadata["http_status"], 200)
        self.assertEqual(metadata["headers"]["x-request-id"], "req-test-123")
        self.assertEqual(metadata["headers"]["trace-id"], "trace-test-456")
        serialized_metadata = json.dumps(metadata)
        self.assertNotIn("Authorization", serialized_metadata)
        self.assertNotIn("Set-Cookie", serialized_metadata)
        self.assertIn("reasoning.txt", result["response_log_files"])

    def test_cross_chunk_reasoning_key_is_redacted_before_persistence(self):
        pieces = [FAKE_KEY[:11], FAKE_KEY[11:22], FAKE_KEY[22:]]
        events = [{"choices": [{"delta": {"reasoning_content": part}}]} for part in pieces]
        events += [{"choices": [{"delta": {"content": "[]"}, "finish_reason": "stop"}]}, "[DONE]"]
        result, output, _ = self.execute_mock(FakeResponse(events=events))
        self.assertEqual(result["status"], "completed")
        saved_reasoning = (output / "reasoning.txt").read_text(encoding="utf-8")
        self.assertEqual(saved_reasoning, "[REDACTED]")
        raw_deltas = []
        for line in self.recorded_sse_lines(output):
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                event = json.loads(line[6:])
                for choice in event.get("choices", []):
                    raw_deltas.append(choice.get("delta", {}).get("reasoning_content", ""))
        self.assertEqual("".join(raw_deltas), "[REDACTED]")
        for path in output.iterdir():
            self.assertNotIn(FAKE_KEY, path.read_bytes().decode("utf-8", errors="replace"))

    def test_cross_reasoning_and_content_delta_key_is_redacted_in_stream_log(self):
        response = FakeResponse(events=[
            {"choices": [{"delta": {"reasoning_content": FAKE_KEY[:13]}}]},
            {"choices": [{"delta": {"content": FAKE_KEY[13:]}}]},
            {"choices": [{"delta": {"content": "[]"}, "finish_reason": "stop"}]},
            "[DONE]",
        ])
        _, output, _ = self.execute_mock(response)
        text_deltas = []
        for line in self.recorded_sse_lines(output):
            if line.startswith("data: ") and line[6:].strip() != "[DONE]":
                event = json.loads(line[6:])
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    text_deltas.extend(delta.get(name, "") for name in ("reasoning_content", "reasoning", "content"))
        self.assertEqual("".join(text_deltas), "[REDACTED][]")
        for path in output.iterdir():
            self.assertNotIn(FAKE_KEY, path.read_bytes().decode("utf-8", errors="replace"))

    def test_unterminated_tail_event_joins_prior_delta_for_key_redaction(self):
        response = FakeResponse(events=[
            {"choices": [{"delta": {"reasoning_content": FAKE_KEY[:13]}}]},
            PartialEventThenError(
                {"choices": [{"delta": {"content": FAKE_KEY[13:]}}]},
                review.requests.ReadTimeout("synthetic timeout after JSON line"),
            ),
        ])
        result, output, _ = self.execute_mock(response)
        self.assertEqual(result["error_type"], "ReadTimeout")
        text_deltas = []
        raw_lines = self.recorded_sse_lines(output)
        for line in raw_lines:
            if line.startswith("data: "):
                event = json.loads(line[6:])
                for choice in event.get("choices", []):
                    delta = choice.get("delta", {})
                    text_deltas.extend(delta.get(name, "") for name in ("reasoning_content", "reasoning", "content"))
        self.assertEqual("".join(text_deltas), "[REDACTED]")
        logged_records = [json.loads(line) for line in (output / "response_stream.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertIn(False, [record.get("event_complete") for record in logged_records])
        for path in output.iterdir():
            self.assertNotIn(FAKE_KEY, path.read_bytes().decode("utf-8", errors="replace"))

    def test_non_json_report_is_preserved_and_not_converted_to_no_findings(self):
        raw = "可能存在参数兼容问题，需要确认调用方。"
        result, output, _ = self.execute_mock(FakeResponse(body={"choices": [{"message": {"content": raw}, "finish_reason": "stop"}]}))
        self.assertEqual(result["status"], "completed")
        self.assertFalse(result["parse_ok"])
        self.assertIsNone(result["parsed_findings"])
        self.assertEqual(result["content"], raw)
        self.assertTrue(result["usage_missing"])
        self.assertIn(raw, (output / "report.md").read_text(encoding="utf-8"))

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

    def test_service_error_redacts_key_and_preserves_partial_visible_report(self):
        result, output, _ = self.execute_mock(FakeResponse(events=[
            {"choices": [{"delta": {"content": "部分报告 " + FAKE_KEY}}]},
            {"error": {"code": "engine_error", "message": "failed using " + FAKE_KEY}},
        ]))
        self.assertEqual(result["status"], "error")
        self.assertIn("部分报告 [REDACTED]", result["content"])
        self.assertEqual(result["provider_error"]["code"], "engine_error")
        self.assertIn("engine_error", (output / "response_stream.jsonl").read_text(encoding="utf-8"))
        self.assertIn("部分报告 [REDACTED]", (output / "response_stream.jsonl").read_text(encoding="utf-8"))
        for file in output.iterdir():
            self.assertNotIn(FAKE_KEY, file.read_bytes().decode("utf-8", errors="replace"))

    def test_read_timeout_preserves_previously_received_lines_and_reasoning(self):
        broken = FakeResponse(events=[
            {"choices": [{"delta": {"reasoning_content": "已收到的部分思考"}}]},
            review.requests.ReadTimeout("synthetic timeout"),
        ])
        result, output, _ = self.execute_mock(broken)
        self.assertEqual(result["error_type"], "ReadTimeout")
        self.assertIn('"reasoning_content": "已收到的部分思考"', "\n".join(self.recorded_sse_lines(output)))
        self.assertEqual((output / "reasoning.txt").read_text(encoding="utf-8"), "已收到的部分思考")

    def test_keyboard_interrupt_closes_logs_and_keeps_partial_reasoning(self):
        interrupted = FakeResponse(events=[
            {"choices": [{"delta": {"reasoning_content": "中断前已经收到"}}]},
            KeyboardInterrupt(),
        ])
        result, output, _ = self.execute_mock(interrupted)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual((output / "reasoning.txt").read_text(encoding="utf-8"), "中断前已经收到")
        self.assertIn('"kind": "sse_line"', (output / "response_stream.jsonl").read_text(encoding="utf-8"))

    def test_malformed_sse_event_is_preserved_before_json_parse_failure(self):
        result, output, _ = self.execute_mock(FakeResponse(events=["{broken-json: 保留原文"]))
        self.assertEqual(result["status"], "error")
        self.assertIn("{broken-json: 保留原文", "\n".join(self.recorded_sse_lines(output)))

    def test_non_sse_success_and_http_error_keep_full_body_and_safe_headers(self):
        raw_body = ("{" + '"message":"' + ("响应正文" * 500) + FAKE_KEY + '"}')
        success, success_dir, _ = self.execute_mock(FakeResponse(events=None, body=raw_body))
        self.assertEqual(success["status"], "completed")
        self.assertFalse(success["parse_ok"])
        saved = (success_dir / "response_body.bin").read_bytes().decode("utf-8")
        self.assertIn("响应正文" * 500, saved)
        self.assertNotIn(FAKE_KEY, saved)
        error, error_dir, _ = self.execute_mock(FakeResponse(events=None, body=raw_body, status=502))
        self.assertEqual(error["status"], "error")
        error_saved = (error_dir / "response_body.bin").read_bytes().decode("utf-8")
        self.assertIn("响应正文" * 500, error_saved)
        self.assertNotIn(FAKE_KEY, error_saved)
        metadata = review.read_json(error_dir / "response_metadata.json")
        self.assertEqual(metadata["http_status"], 502)
        self.assertNotIn("authorization", metadata["headers"])
        self.assertNotIn("set-cookie", metadata["headers"])

    def test_aborted_and_insufficient_resource_finish_reasons_are_incomplete(self):
        for finish_reason in ("aborted", "insufficient_system_resource"):
            with self.subTest(finish_reason=finish_reason):
                response = FakeResponse(events=[
                    {"choices": [{"delta": {"content": "[]"}, "finish_reason": finish_reason}]},
                    "[DONE]",
                ])
                result, output, _ = self.execute_mock(response)
                self.assertEqual(result["status"], "incomplete")
                self.assertEqual(result["finish_reason"], finish_reason)
                self.assertIn(finish_reason, (output / "response_stream.jsonl").read_text(encoding="utf-8"))

    def test_response_log_io_failure_warns_but_does_not_abort_request(self):
        original_open = Path.open

        def selective_failure(path, *args, **kwargs):
            if path.name == "response_stream.jsonl":
                raise OSError("synthetic log disk error")
            return original_open(path, *args, **kwargs)

        with patch.object(response_log.Path, "open", selective_failure):
            result, _, _ = self.execute_mock(FakeResponse(events=[
                {"choices": [{"delta": {"content": "[]"}, "finish_reason": "stop"}]}, "[DONE]",
            ]))
        self.assertEqual(result["status"], "completed")
        self.assertIn("Response log I/O failed", result["response_log_warning"])

    def test_redirect_is_never_followed_and_incomplete_stream_is_not_success(self):
        redirected, _, _ = self.execute_mock(FakeResponse(status=302, body={"secret": FAKE_KEY}))
        self.assertEqual(redirected["provider_error"]["code"], "redirect_rejected")
        incomplete, _, _ = self.execute_mock(FakeResponse(events=[{"choices": [{"delta": {"content": "[]"}}]}]))
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertTrue(incomplete["parse_ok"])
        self.assertFalse(incomplete["stream_done"])

    def test_missing_finish_reason_is_reported_for_sse_and_json(self):
        for response in (
            FakeResponse(events=[{"choices": [{"delta": {"content": "[]"}}]}, "[DONE]"]),
            FakeResponse(body={"choices": [{"message": {"content": "[]"}}]}),
        ):
            with self.subTest(content_type=response.headers["Content-Type"]):
                result, output, _ = self.execute_mock(response)
                self.assertEqual(result["status"], "completed")
                self.assertTrue(result["parse_ok"])
                self.assertIn("No finish_reason", result["completion_warning"])
                self.assertIn("completion_warning", (output / "report.md").read_text(encoding="utf-8"))

    def test_invalid_route_does_not_create_output_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "new-review"
            invalid = dict(CONFIG, day=dict(CONFIG["day"], start="not-a-time"))
            with patch.object(review.requests, "Session", side_effect=AssertionError("must not call")):
                with self.assertRaises(ValueError):
                    review.run_review(invalid, {}, output, dry_run=True)
            self.assertFalse(output.exists())

    def test_ledger_records_only_title_timing_and_model_metadata(self):
        code = "NEVER_COPY_FULL_SOURCE_TO_LEDGER = True"
        result, output, _ = self.execute_mock(
            FakeResponse(body={"model": "reported-model", "choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]}),
            packet={"task": code, "files": [{"content": code}]},
            task_label="修复页码定位 " + FAKE_KEY,
        )
        history = review.ledger.history(self.ledger_path)
        self.assertEqual(len(history["records"]), 1)
        record = history["records"][0]
        self.assertEqual(record["task"], "修复页码定位 [REDACTED]")
        self.assertEqual(record["output_dir"], str(output.resolve()))
        for name in ("request.json", "result.json", "report.md", "progress.json"):
            self.assertTrue((Path(record["output_dir"]) / name).is_file())
        self.assertEqual(record["requested_model"], result["requested_model"])
        self.assertEqual(record["reported_model"], "reported-model")
        self.assertEqual(record["seconds"], result["seconds"])
        self.assertEqual(record["first_content_seconds"], result["first_content_seconds"])
        self.assertIsNotNone(record["first_content_utc"])
        self.assertEqual(record["outcome"], "success")
        raw = self.ledger_path.read_bytes()
        self.assertNotIn(FAKE_KEY.encode(), raw)
        self.assertNotIn(code.encode(), raw)

    def test_output_directory_is_absolute_and_known_key_is_redacted(self):
        result = {"started_utc": "2026-10-23T16:00:00+00:00", "status": "completed"}
        metadata = review.ledger_metadata(result, "short title", FAKE_KEY, Path("relative") / ("中文  目录-" + FAKE_KEY))
        self.assertTrue(Path(metadata["output_dir"]).is_absolute())
        self.assertIn("中文  目录-", metadata["output_dir"])
        self.assertNotIn(FAKE_KEY, metadata["output_dir"])
        self.assertIn("[REDACTED]", metadata["output_dir"])

    def test_title_redaction_precedes_length_limit(self):
        self.execute_mock(
            FakeResponse(body={"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]}),
            task_label="x" * 110 + " " + FAKE_KEY,
        )
        raw = self.ledger_path.read_bytes()
        self.assertNotIn(b"sk-test-", raw)
        title = review.ledger.history(self.ledger_path)["records"][0]["task"]
        self.assertLessEqual(len(title), 120)

    def test_timeout_is_separate_from_success_in_ledger(self):
        class TimeoutResponse(FakeResponse):
            def iter_content(self, **kwargs):
                raise review.requests.ReadTimeout("sensitive transport detail " + FAKE_KEY)
                yield ""
        self.execute_mock(FakeResponse(body={"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]}))
        failed, failed_dir, _ = self.execute_mock(TimeoutResponse(events=[]))
        self.assertEqual(failed["status"], "error")
        records = review.ledger.history(self.ledger_path)["records"]
        self.assertEqual({row["outcome"] for row in records}, {"success", "failure"})
        failure = next(row for row in records if row["outcome"] == "failure")
        self.assertEqual(failure["error_type"], "ReadTimeout")
        self.assertEqual(failure["output_dir"], str(failed_dir.resolve()))
        self.assertIsNone(failure["first_content_seconds"])
        self.assertNotIn(FAKE_KEY.encode(), self.ledger_path.read_bytes())
        self.assertNotIn(b"sensitive transport detail", self.ledger_path.read_bytes())

    def test_ledger_write_failure_does_not_fail_completed_review(self):
        with patch.object(review.ledger, "start_call", side_effect=OSError(FAKE_KEY)), \
                patch.object(review.ledger, "finish_call", side_effect=OSError(FAKE_KEY)):
            result, output, _ = self.execute_mock(FakeResponse(body={"choices": [{"message": {"content": "[]"}, "finish_reason": "stop"}]}))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["parse_ok"])
        self.assertIn("OSError", result["ledger_warning"])
        self.assertNotIn(FAKE_KEY, (output / "result.json").read_text(encoding="utf-8"))

    def test_live_cli_redacts_packet_before_workflow_material_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            packet=folder/"packet.json"
            original={"task":"review","files":[{"path":"a.py","content":"accidental "+FAKE_KEY}]}
            packet.write_text(json.dumps(original),encoding="utf-8")
            with patch.object(review,"load_credential",return_value=FAKE_KEY), \
                    patch("workflow.run_workflow",return_value={"status":"completed"}) as workflow, \
                    patch.object(review.requests,"Session",side_effect=AssertionError("no real requests")), \
                    patch("sys.stdout",new_callable=io.StringIO):
                self.assertEqual(review.main(["--packet",str(packet),"--output-dir",str(folder/"out"),"--task","validation "+FAKE_KEY]),0)
            supplied=workflow.call_args.args[1]
            self.assertNotIn(FAKE_KEY,workflow.call_args.kwargs["task_label"])
            self.assertNotIn(FAKE_KEY,json.dumps(supplied))
            self.assertIn("[REDACTED]",supplied["files"][0]["content"])
            self.assertEqual(json.loads(packet.read_text(encoding="utf-8")),original)

    def test_history_and_dry_run_do_not_load_credentials_or_network_or_log_calls(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(review, "load_credential", side_effect=AssertionError("no key reads")), \
                patch.object(review.requests, "Session", side_effect=AssertionError("no network")), \
                patch.object(review, "read_json", return_value=self.config), \
                patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(review.main(["--history", "--outcome", "success", "--slowest"]), 0)
            history = json.loads(stdout.getvalue())
            self.assertEqual(history["records"], [])
            review.run_review(self.config, {"task": "offline"}, Path(temporary) / "dry", dry_run=True)
        self.assertFalse(self.ledger_path.exists())

    def test_credential_failure_is_recorded_without_supplied_title(self):
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(review, "load_credential", side_effect=review.ConfigError("Credential unavailable")), \
                patch.object(review.requests, "Session", side_effect=AssertionError("must not call")):
            result = review.run_review(self.config, {"task": "not copied"}, Path(temporary) / "failed", task_label=FAKE_KEY)
        self.assertEqual(result["status"], "error")
        rows = review.ledger.history(self.ledger_path)["records"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "failure")
        self.assertEqual(rows[0]["error_type"], "ConfigError")
        self.assertNotIn(FAKE_KEY.encode(), self.ledger_path.read_bytes())

    def test_live_time_override_and_output_overwrite_are_rejected_before_network(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(review.requests, "Session", side_effect=AssertionError("must not call")):
            with self.assertRaisesRegex(ValueError, "offline"):
                review.run_review(CONFIG, {}, Path(temporary) / "new", at="2026-10-20T12:00:00+08:00")
            with self.assertRaises(FileExistsError):
                review.run_review(CONFIG, {}, temporary, dry_run=True)


if __name__ == "__main__":
    unittest.main()
