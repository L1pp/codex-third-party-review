"""Pure API-failure classification and bounded retry waiting policy.

This module never sends a request, sleeps, reads logs, or mutates an outcome.
The workflow owns attempt records, elapsed-time limits and checkpoint recovery.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from types import SimpleNamespace

from tenacity import wait_random_exponential


_BACKOFF = wait_random_exponential(multiplier=2, max=30)
_TRANSPORT_ERRORS = {
    "ConnectTimeout", "ReadTimeout", "Timeout", "ConnectionError", "ProxyError",
    "ChunkedEncodingError", "ContentDecodingError",
}
_LOCAL_ERRORS = {
    "ConfigError", "PermissionError", "FileNotFoundError", "FileExistsError",
    "IsADirectoryError", "NotADirectoryError", "OSError", "UnicodeError",
    "UnicodeDecodeError", "UnicodeEncodeError", "ValueError", "JSONDecodeError",
    "TypeError", "KeyError", "AttributeError",
}
_CANCELLATION_ERRORS = {"KeyboardInterrupt", "CancelledError", "CanceledError"}
_PERMANENT_MARKERS = (
    "invalid_api_key", "invalidapikey", "authentication", "unauthorized",
    "accessdenied", "permission_denied", "permissiondenied", "invalidparameter",
    "invalid_parameter", "invalid_request_error", "invalid_model", "model_not_found",
    "modelnotfound", "modelnotexist", "unsupportedmodel", "unsupported_model",
    "content_filter", "contentfilter", "data_inspection", "datainspection",
    "safety_violation", "policy_violation", "insufficient_quota", "quota_exceeded",
    "arrearage", "redirect_rejected",
)
_CONTEXT_MARKERS = (
    "context_length_exceeded", "context_window_exceeded", "context_overflow",
    "context length exceeded", "maximum context length", "context window exceeded",
    "input too long", "input length exceeds", "input length exceeded",
    "input token limit exceeded", "prompt is too long", "prompt too long",
    "prompt tokens exceeded", "max_input_tokens exceeded",
)
_DEADLINE_MARKERS = (
    "gatewaytimeout", "gateway_timeout", "deadline_exceeded", "deadline exceeded",
    "upstream_timeout", "upstreamtimeout", "requesttimeout", "request_timeout",
    "requesttimedout", "request_time_out",
)
_TEMPORARY_MARKERS = (
    "resource_exhausted", "resource exhausted", "rate_limit", "ratelimit",
    "rate limit", "throttling", "too_many_requests", "serviceunavailable",
    "service_unavailable", "internal_server_error", "internalservererror",
    "internalerror", "internal_error", "engine_overloaded", "engineoverloaded",
    "model_service_error", "modelserviceerror", "inference engine abort",
    "server_error", "overloaded",
)


def _has_progress(result, reasoning_text):
    if isinstance(reasoning_text, str) and reasoning_text:
        return True
    for key in ("content", "reasoning_text", "reasoning_content"):
        if isinstance(result.get(key), str) and result[key]:
            return True
    count = result.get("reasoning_chars")
    if isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0:
        return True
    calls = result.get("tool_calls")
    for call in calls if isinstance(calls, list) else []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") or {}
        if isinstance(call.get("id"), str) and call["id"]:
            return True
        if isinstance(function, dict):
            for key in ("name", "arguments"):
                value = function.get(key)
                if isinstance(value, str) and value:
                    return True
                if isinstance(value, (dict, list)) and value:
                    return True
    return False


def _provider_description(result):
    """Use only structured error fields, never task text or generated content."""
    provider = result.get("provider_error")
    if not isinstance(provider, dict):
        return ""
    return " ".join(str(provider[key]) for key in ("code", "type", "message")
                    if isinstance(provider.get(key), (str, int))).casefold()


def classify_failure(result, *, reasoning_text=None):
    """Select runtime recovery without rewriting the original failure.

    ``none`` delegates successful/length-ended responses to ordinary workflow
    handling; ``context`` requests input compaction, ``retry`` repeats only the
    current request, and ``checkpoint`` preserves generated review progress.
    Mere receipt of an SSE event or usage metadata does not prove generation.
    """
    if not isinstance(result, dict):
        raise TypeError("result must be an object")
    progress = _has_progress(result, reasoning_text)

    def decision(action, category):
        return {"action": action, "category": category, "has_progress": progress}

    status = result.get("status")
    error_type = result.get("error_type")
    transport_category = result.get("transport_error_category")
    finish = result.get("finish_reason")
    provider = _provider_description(result)
    http = result.get("http_status")
    if isinstance(http, bool) or not isinstance(http, int):
        http = None

    if (status in {"interrupted", "cancelled", "canceled"}
            or error_type in _CANCELLATION_ERRORS
            or any(marker in provider for marker in (
                "request_cancelled", "request_canceled", "request cancelled", "request canceled"))):
        return decision("stop", "operator_cancelled")
    if result.get("local_io_error") is not None or error_type in _LOCAL_ERRORS:
        return decision("stop", "local_failure")
    # The transport records isinstance-based categories, preserving subclass
    # identity in error_type. TLS must precede broad ConnectionError recovery:
    # certificate/configuration errors commonly persist across retries.
    if transport_category == "tls" or error_type == "SSLError":
        return decision("stop", "tls_failure")
    if transport_category is not None and transport_category not in ("timeout", "connection", "stream"):
        return decision("stop", "unclassified_transport")
    if finish == "content_filter" or http in {401, 403}:
        return decision("stop", "permanent_provider")
    # Explicit context-overflow codes may accompany a generic invalid-request
    # type, so recognize the specific condition before generic 400 markers.
    if any(marker in provider for marker in _CONTEXT_MARKERS):
        return decision("context", "context_overflow")
    if any(marker in provider for marker in _PERMANENT_MARKERS):
        return decision("stop", "permanent_provider")
    if finish == "tool_calls" and result.get("stream_done") is False:
        return decision("checkpoint", "incomplete_stream")
    if status in {"completed", "tool_calls", "dry_run"} and not error_type:
        return decision("none", "ordinary_response")
    if finish == "length" and status != "error":
        return decision("none", "length_termination")
    if any(marker in provider for marker in _DEADLINE_MARKERS) or http == 504:
        return decision("checkpoint" if progress else "retry", "provider_deadline")
    if transport_category in ("timeout", "connection", "stream") or error_type in _TRANSPORT_ERRORS:
        return decision("checkpoint" if progress else "retry", "transient_transport")
    if any(marker in provider for marker in _TEMPORARY_MARKERS):
        return decision("checkpoint" if progress else "retry", "transient_capacity")
    if http in {408, 409, 425, 429} or (http is not None and 500 <= http <= 599):
        return decision("checkpoint" if progress else "retry", "transient_http")
    if finish in {"aborted", "insufficient_system_resource"}:
        return decision("checkpoint" if progress else "retry", "interrupted_generation")
    if status == "incomplete" and finish is None and not error_type:
        return decision("checkpoint" if progress else "retry", "incomplete_stream")
    # A meaningful stop may arrive without the optional final SSE sentinel.
    if status == "incomplete" and finish in {"stop", "end_turn", "eos"} and not error_type:
        return decision("none", "ordinary_response")
    return decision("stop", "unclassified_failure")


def recovery_window_seconds(config):
    """Return a retry elapsed-time budget; zero disables automatic repeats."""
    settings = config.get("api_recovery")
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("api_recovery must be an object")
    value = settings.get("retry_window_seconds", 120)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError("api_recovery.retry_window_seconds must be finite and non-negative")
    return float(value)


def _positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _retry_after(headers, now):
    if not isinstance(headers, dict):
        return None
    lowered = {str(name).casefold(): value for name, value in headers.items()}
    milliseconds = _positive_number(lowered.get("retry-after-ms"))
    if milliseconds is not None:
        seconds = milliseconds / 1000
        return seconds if seconds > 0 else None
    raw = lowered.get("retry-after")
    seconds = _positive_number(raw)
    if seconds is not None:
        return seconds
    if not isinstance(raw, str):
        return None
    try:
        moment = parsedate_to_datetime(raw)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        reference = datetime.now(timezone.utc) if now is None else now
        if isinstance(reference, (int, float)) and not isinstance(reference, bool):
            reference = datetime.fromtimestamp(reference, timezone.utc)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)
        seconds = (moment - reference).total_seconds()
        return seconds if seconds > 0 else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def retry_wait_seconds(attempt_number, *, headers=None, now=None):
    """Honor a positive Retry-After, otherwise use Tenacity full jitter.

    The caller must reject a wait beyond its remaining recovery window rather
    than truncate a provider-requested delay. Zero, past-date and invalid delays
    fall back to ordinary backoff, avoiding a rapid repeat loop. No sleep or
    request occurs here.
    """
    if isinstance(attempt_number, bool) or not isinstance(attempt_number, int) or attempt_number < 1:
        raise ValueError("attempt_number must be a positive integer")
    instructed = _retry_after(headers, now)
    return instructed if instructed is not None else float(_BACKOFF(SimpleNamespace(attempt_number=attempt_number)))