"""Offline recovery-policy fixtures; no API, filesystem replay, or sleeping."""
import copy
from datetime import datetime, timezone
import math
import unittest
from unittest.mock import patch

try:
    from . import api_recovery
except ImportError:
    import api_recovery


def provider(code, *, message="", http=200, **fields):
    return dict(status="error", error_type="ProviderError", http_status=http,
                provider_error={"code": code, "message": message}, **fields)


class FailureClassificationTests(unittest.TestCase):
    def check(self, result, action, category=None, progress=None, **kwargs):
        original = copy.deepcopy(result)
        decision = api_recovery.classify_failure(result, **kwargs)
        self.assertEqual(decision["action"], action)
        if category is not None:
            self.assertEqual(decision["category"], category)
        if progress is not None:
            self.assertEqual(decision["has_progress"], progress)
        self.assertEqual(result, original, "Classification must not rewrite raw failure evidence")
        return decision

    def test_http200_error_first_event_does_not_prove_generation(self):
        self.check(provider("internal_server_error", first_event_seconds=1.2),
                   "retry", "transient_capacity", False)

    def test_long_deadline_after_real_reasoning_creates_checkpoint(self):
        self.check(provider("GatewayTimeout", message="Deadline Exceeded", seconds=1796.7,
                            reasoning_chars=137741), "checkpoint", "provider_deadline", True)

    def test_deadline_with_external_reasoning_log_is_progress(self):
        self.check(provider("GatewayTimeout"), "checkpoint", "provider_deadline", True,
                   reasoning_text="Tentative issue with source range and uncertainty")

    def test_content_and_partial_tool_arguments_are_generation_progress(self):
        for extra in (
            {"content": "partial finding"},
            {"tool_calls": [{"id": "read-one", "function": {"name": "read_file", "arguments": '{"path":'}}]},
            {"tool_calls": [{"function": {"arguments": "{"}}]},
            {"reasoning_text": "verify behavior"},
        ):
            with self.subTest(extra=extra):
                self.check(provider("internal_server_error", **extra), "checkpoint", progress=True)

    def test_empty_tool_placeholders_and_usage_are_not_progress(self):
        self.check(provider("GatewayTimeout", tool_calls=[{}, {"function": {"name": "", "arguments": ""}}],
                            usage={"completion_tokens": 20}, first_content_seconds=1.0),
                   "retry", progress=False)

    def test_resource_exhausted_even_with_provider_prose_is_capacity_failure(self):
        self.check(provider("ServiceUnavailable", http=503,
                            message="Master scheduling failed (RESOURCE_EXHAUSTED); do not retry or route locally"),
                   "retry", "transient_capacity", False)

    def test_transient_http_statuses(self):
        for http in (408, 409, 425, 429, 500, 502, 503, 504):
            with self.subTest(http=http):
                self.check(provider("", http=http), "retry", progress=False)
                self.check(provider("", http=http, reasoning_chars=2), "checkpoint", progress=True)

    def test_transport_interruptions_before_and_after_generation(self):
        for error in ("ReadTimeout", "ConnectTimeout", "Timeout", "ConnectionError", "ChunkedEncodingError", "ContentDecodingError"):
            with self.subTest(error=error):
                self.check({"status": "error", "error_type": error}, "retry", "transient_transport", False)
                self.check({"status": "error", "error_type": error, "content": "partial"},
                           "checkpoint", "transient_transport", True)

    def test_typed_transport_categories_recover_custom_exception_subclasses(self):
        for category, error_type in (("timeout", "ProviderReadTimeout"),
                                     ("connection", "ProviderProxyConnection"),
                                     ("stream", "ProviderChunkDecodingError")):
            with self.subTest(category=category):
                result = {"status": "error", "error_type": error_type,
                          "transport_error_category": category}
                self.check(result, "retry", "transient_transport", False)
                result["reasoning_chars"] = 20
                self.check(result, "checkpoint", "transient_transport", True)
                self.assertEqual(result["error_type"], error_type)

    def test_legacy_proxy_error_recovers_without_new_transport_category(self):
        self.check({"status": "error", "error_type": "ProxyError"},
                   "retry", "transient_transport", False)
        self.check({"status": "error", "error_type": "ProxyError", "content": "partial"},
                   "checkpoint", "transient_transport", True)

    def test_tls_stops_before_broad_connection_or_http_recovery(self):
        for extra in ({"error_type": "ProviderCertificateError", "transport_error_category": "tls"},
                      {"error_type": "SSLError"},
                      {"error_type": "SSLError", "transport_error_category": "connection"}):
            with self.subTest(extra=extra):
                result = {"status": "error", "http_status": 503, "reasoning_chars": 20, **extra}
                self.check(result, "stop", "tls_failure", True)

    def test_unknown_transport_category_does_not_blindly_use_string_fallback(self):
        for error_type in ("ProviderTransportFailure", "ConnectionError"):
            with self.subTest(error_type=error_type):
                self.check({"status": "error", "error_type": error_type,
                            "transport_error_category": "unknown", "http_status": 503},
                           "stop", "unclassified_transport", False)
        self.check({"status": "error", "error_type": "ProviderTransportFailure"},
                   "stop", "unclassified_failure", False)

    def test_authentication_policy_and_parameters_never_recover(self):
        for http, code in ((401, "internal_server_error"), (403, "GatewayTimeout"),
                           (400, "InvalidParameter"), (404, "model_not_found"),
                           (429, "insufficient_quota"), (400, "DataInspectionFailed")):
            with self.subTest(http=http, code=code):
                self.check(provider(code, http=http, reasoning_chars=20), "stop", "permanent_provider")
        self.check({"status": "incomplete", "finish_reason": "content_filter", "content": "partial"},
                   "stop", "permanent_provider")

    def test_operator_cancellation_takes_precedence_over_recovery(self):
        for extra in ({"status": "interrupted"}, {"error_type": "KeyboardInterrupt"},
                      {"error_type": "CancelledError"}):
            value = provider("RESOURCE_EXHAUSTED", reasoning_chars=200)
            value.update(extra)
            self.check(value, "stop", "operator_cancelled")
        self.check(provider("request_cancelled", message="request cancelled"), "stop", "operator_cancelled")

    def test_local_io_and_config_errors_do_not_replay_api(self):
        for error in ("PermissionError", "FileNotFoundError", "ConfigError", "ValueError", "JSONDecodeError", "OSError"):
            value = provider("internal_server_error", http=503, reasoning_chars=100)
            value["error_type"] = error
            self.check(value, "stop", "local_failure")
        self.check(provider("internal_server_error", local_io_error={"operation": "replace"}), "stop", "local_failure")

    def test_explicit_context_overflow_precedes_generic_invalid_request(self):
        value = provider("context_length_exceeded", http=400, message="maximum context length exceeded")
        value["provider_error"]["type"] = "invalid_request_error"
        self.check(value, "context", "context_overflow")
        self.check(provider("InvalidParameter", http=400, message="max_tokens is invalid"), "stop", "permanent_provider")

    def test_incomplete_stream_checkpoint_and_empty_stream_retry(self):
        self.check({"status": "incomplete", "finish_reason": None, "stream_done": False, "content": "[]"},
                   "checkpoint", "incomplete_stream", True)
        self.check({"status": "incomplete", "finish_reason": None, "stream_done": False},
                   "retry", "incomplete_stream", False)

    def test_missing_done_with_tool_finish_cannot_execute_partial_tools(self):
        self.check({"status": "tool_calls", "finish_reason": "tool_calls", "stream_done": False,
                    "tool_calls": [{"id": "read-one", "function": {"name": "read_file", "arguments": '{"path":'}}]},
                   "checkpoint", "incomplete_stream", True)
        self.check({"status": "tool_calls", "finish_reason": "tool_calls", "stream_done": True,
                    "tool_calls": [{"id": "read-one", "function": {"name": "read_file", "arguments": "{}"}}]},
                   "none", "ordinary_response", True)

    def test_length_and_completed_stop_remain_normal_workflow_handling(self):
        for value in (
            {"status": "incomplete", "finish_reason": "length", "content": "partial"},
            {"status": "incomplete", "finish_reason": "stop", "content": "[]", "stream_done": False},
            {"status": "completed", "finish_reason": "stop", "content": "[]"},
            {"status": "tool_calls", "finish_reason": "tool_calls", "tool_calls": []},
            {"status": "dry_run"},
        ):
            with self.subTest(value=value):
                self.check(value, "none")

    def test_unknown_provider_error_and_generated_error_like_text_do_not_trigger_retry(self):
        self.check(provider("unrecognized", content="RESOURCE_EXHAUSTED inside source example"),
                   "stop", "unclassified_failure", True)
        self.check({"status": "error", "error_type": "ProviderError", "content": "partial"},
                   "stop", "unclassified_failure", True)

    def test_engine_abort_and_resource_finish_recover(self):
        self.check(provider("internal_server_error", message="Inference engine abort [UNKNOWN]", reasoning_chars=200),
                   "checkpoint", progress=True)
        for finish in ("aborted", "insufficient_system_resource"):
            self.check({"status": "incomplete", "finish_reason": finish}, "retry")

    def test_bool_http_or_reasoning_count_is_not_numeric_evidence(self):
        self.check(provider("GatewayTimeout", http=True, reasoning_chars=True), "retry", progress=False)


class RetryWaitTests(unittest.TestCase):
    def test_retry_after_seconds_is_case_insensitive_and_not_artificially_capped(self):
        with patch.object(api_recovery, "_BACKOFF", side_effect=AssertionError("must honor header")):
            self.assertEqual(api_recovery.retry_wait_seconds(1, headers={"Retry-After": "900"}), 900)
            self.assertEqual(api_recovery.retry_wait_seconds(1, headers={"RETRY-AFTER": " 2.5 "}), 2.5)

    def test_retry_after_date_uses_supplied_clock(self):
        now = datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc)
        self.assertEqual(api_recovery.retry_wait_seconds(1, headers={"retry-after": "Wed, 28 Oct 2026 14:00:30 GMT"}, now=now), 30)
        self.assertEqual(api_recovery.retry_wait_seconds(1, headers={"retry-after": "Wed, 28 Oct 2026 14:00:30 GMT"}, now=now.timestamp()), 30)

    def test_retry_after_milliseconds_precedes_seconds(self):
        self.assertEqual(api_recovery.retry_wait_seconds(1, headers={"retry-after-ms": "1250", "retry-after": "30"}), 1.25)

    def test_malformed_or_negative_nonfinite_headers_fall_back_to_tenacity(self):
        for raw in ("nonsense", "-1", "nan", "inf", [], None):
            with self.subTest(raw=raw), patch.object(api_recovery, "_BACKOFF", return_value=7.5) as backoff:
                self.assertEqual(api_recovery.retry_wait_seconds(3, headers={"retry-after": raw}), 7.5)
                self.assertEqual(backoff.call_args.args[0].attempt_number, 3)

    def test_nonpositive_or_expired_retry_after_uses_backoff_instead_of_rapid_repeats(self):
        now = datetime(2026, 10, 28, 14, 0, tzinfo=timezone.utc)
        for headers in (
            {"retry-after": "0"}, {"retry-after": "-2"},
            {"retry-after-ms": "0"}, {"retry-after-ms": "-10"},
            {"retry-after": "Wed, 28 Oct 2026 13:00:00 GMT"},
            {"retry-after": "Wed, 28 Oct 2026 14:00:00 GMT"},
        ):
            with self.subTest(headers=headers), patch.object(api_recovery, "_BACKOFF", return_value=6.0) as backoff:
                waits = [api_recovery.retry_wait_seconds(attempt, headers=headers, now=now) for attempt in (1, 2, 3)]
                self.assertEqual(waits, [6.0, 6.0, 6.0])
                self.assertEqual(backoff.call_count, 3)
                self.assertEqual([call.args[0].attempt_number for call in backoff.call_args_list], [1, 2, 3])

    def test_real_tenacity_wait_uses_exponential_full_jitter(self):
        for attempt, maximum in ((1, 2), (2, 4), (5, 30), (100, 30)):
            wait = api_recovery.retry_wait_seconds(attempt)
            self.assertGreaterEqual(wait, 0)
            self.assertLessEqual(wait, maximum)

    def test_attempt_number_validation(self):
        for value in (True, 0, -1, 1.5, "2", None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api_recovery.retry_wait_seconds(value)

    def test_window_default_explicit_zero_and_custom_budget(self):
        self.assertEqual(api_recovery.recovery_window_seconds({}), 120)
        self.assertEqual(api_recovery.recovery_window_seconds({"api_recovery": {"retry_window_seconds": 0}}), 0)
        self.assertEqual(api_recovery.recovery_window_seconds({"api_recovery": {"retry_window_seconds": 240.5}}), 240.5)

    def test_window_rejects_bad_values_instead_of_unbounded_recovery(self):
        for value in (True, -1, "120", math.inf, math.nan, [], None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                api_recovery.recovery_window_seconds({"api_recovery": {"retry_window_seconds": value}})
        for value in ("invalid", [], False, 0):
            with self.subTest(settings=value), self.assertRaises(ValueError):
                api_recovery.recovery_window_seconds({"api_recovery": value})


if __name__ == "__main__":
    unittest.main()