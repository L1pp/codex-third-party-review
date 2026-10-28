"""Best-effort local capture of provider responses, with credential redaction."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time


REDACTED = "[REDACTED]"
HEADER_ALLOWLIST = {
    "content-type", "request-id", "x-request-id", "x-acs-request-id",
    "x-dashscope-request-id", "x-aliyun-request-id", "trace-id", "x-trace-id",
    "x-correlation-id", "traceparent",
    "x-amzn-trace-id",
    "retry-after", "retry-after-ms",
    "x-ratelimit-limit-requests", "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens",
}


def _redact_text(value, key):
    return value.replace(key, REDACTED) if key else value


class StreamingRedactor:
    """Redact a credential even when it is split over adjacent text chunks."""

    def __init__(self, secret):
        self.secret = secret or ""
        self.pending = ""

    def feed(self, text):
        if not self.secret:
            return text
        self.pending += text
        safe_end = max(0, len(self.pending) - len(self.secret) + 1)
        out = []
        pos = 0
        while pos < safe_end:
            if self.pending.startswith(self.secret, pos):
                out.append(REDACTED)
                pos += len(self.secret)
            else:
                out.append(self.pending[pos])
                pos += 1
        self.pending = self.pending[pos:]
        return "".join(out)

    def finish(self):
        if not self.secret:
            tail = self.pending
        else:
            tail = self.pending.replace(self.secret, REDACTED)
        self.pending = ""
        return tail


class ResponseLog:
    """Append decoded SSE lines and reasoning while the provider call runs.

    Writes are buffered and flushed periodically; no fsync or background worker is
    used. Logging failures are reported to the caller and do not abort the API call.
    """

    def __init__(self, output_dir, key, flush_interval_seconds=5.0):
        self.output_dir = Path(output_dir)
        self.key = key or ""
        self.flush_interval_seconds = flush_interval_seconds
        self.last_flush = time.monotonic()
        self.stream_file = None
        self.reasoning_file = None
        self.warning = None
        self._reasoning_redactor = StreamingRedactor(self.key)
        self._current_lines = []
        self._pending_events = []
        self._files = []
        self._open_stream()
        self._open_reasoning()

    @property
    def files(self):
        return list(self._files)

    def _warn(self, exc):
        if self.warning is None:
            self.warning = (
                "Response log I/O failed (" + type(exc).__name__ + "); "
                "the API call continued, but some provider response data may be missing."
            )

    def _open_stream(self):
        try:
            self.stream_file = (self.output_dir / "response_stream.jsonl").open(
                "a", encoding="utf-8", newline="\n"
            )
            self._files.append("response_stream.jsonl")
        except OSError as exc:
            self._warn(exc)

    def _open_reasoning(self):
        try:
            self.reasoning_file = (self.output_dir / "reasoning.txt").open(
                "a", encoding="utf-8", newline=""
            )
            self._files.append("reasoning.txt")
        except OSError as exc:
            self._warn(exc)

    def _flush_if_due(self, force=False):
        now = time.monotonic()
        if not force and now - self.last_flush < self.flush_interval_seconds:
            return
        self.last_flush = now
        for handle in (self.stream_file, self.reasoning_file):
            if handle is not None:
                try:
                    handle.flush()
                except OSError as exc:
                    self._warn(exc)

    def record_sse_line(self, line):
        """Buffer each decoded line before its SSE data is JSON-parsed."""
        self._current_lines.append({
            "received_utc": datetime.now(timezone.utc).isoformat(),
            "line": line,
        })

    def record_sse_event(self, event, event_complete=True, lines=None):
        """Sanitize an SSE event, then persist safe events incrementally."""
        if lines is None:
            lines = self._current_lines
            self._current_lines = []
        if not lines:
            return
        parsed = None
        if event is not None:
            try:
                parsed = json.loads(event)
            except (ValueError, TypeError):
                parsed = None
        if isinstance(parsed, (dict, list)):
            original = parsed
            parsed = self._redact_tree(parsed)
            segments = []
            self._collect_text_segments(parsed, (), segments)
            self._pending_events.append({
                "lines": lines, "body": parsed, "segments": segments,
                "raw_event": event, "needs_reserialize": parsed != original,
                "event_complete": event_complete,
            })
        else:
            safe_event = _redact_text(event, self.key) if event is not None else None
            if event is not None and self.key and self.key in event.replace("\n", ""):
                # A malformed multi-line event may have split a credential around an SSE newline.
                safe_event = "[REDACTED_MALFORMED_EVENT]"
            self._pending_events.append({
                "lines": lines, "body": None, "segments": [], "raw_event": safe_event,
                "needs_reserialize": safe_event != event,
                "event_complete": event_complete,
            })
        self._flush_safe_events()

    def _redact_tree(self, value):
        if isinstance(value, str):
            return _redact_text(value, self.key)
        if isinstance(value, list):
            return [self._redact_tree(item) for item in value]
        if isinstance(value, dict):
            return {self._redact_tree(name): self._redact_tree(item) for name, item in value.items()}
        return value

    def _collect_text_segments(self, value, path, segments):
        if isinstance(value, dict):
            for name, child in value.items():
                child_path = path + (name,)
                if name in ("reasoning_content", "reasoning", "content", "text") and isinstance(child, str):
                    segments.append((child_path, child))
                else:
                    self._collect_text_segments(child, child_path, segments)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._collect_text_segments(child, path + (index,), segments)

    @staticmethod
    def _set_path(value, path, replacement):
        parent = value
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]] = replacement

    def _sanitize_pending_segments(self):
        flat_parts = []
        owners = []
        segment_refs = []
        for event_index, item in enumerate(self._pending_events):
            for path, value in item["segments"]:
                segment_index = len(segment_refs)
                segment_refs.append((event_index, path, value))
                flat_parts.append(value)
                owners.extend([segment_index] * len(value))
        flat = "".join(flat_parts)
        output = [[] for _ in segment_refs]
        position = 0
        while position < len(flat):
            if self.key and flat.startswith(self.key, position):
                segment_index = owners[position]
                output[segment_index].append(REDACTED)
                position += len(self.key)
            else:
                output[owners[position]].append(flat[position])
                position += 1
        for segment_index, (event_index, path, original_text) in enumerate(segment_refs):
            sanitized = "".join(output[segment_index])
            if sanitized != original_text:
                self._pending_events[event_index]["needs_reserialize"] = True
            self._set_path(
                self._pending_events[event_index]["body"],
                path,
                sanitized,
            )

    def _write_sse_event(self, item):
        lines = item["lines"]
        body = item.get("body")
        raw_event = item.get("raw_event")
        normalized_event = None
        if body is not None:
            if item.get("needs_reserialize"):
                normalized_event = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            else:
                normalized_event = raw_event
        elif raw_event is not None:
            normalized_event = raw_event
        data_replaced = False
        for rec in lines:
            line = rec["line"]
            if item.get("needs_reserialize") and line.startswith("data:") and normalized_event is not None:
                if not data_replaced:
                    line = "data: " + normalized_event
                    data_replaced = True
                else:
                    # Preserve the physical SSE boundary without duplicating original text.
                    line = "data:"
            self._write_line_record(rec["received_utc"], line, item.get("event_complete"))

    def _write_line_record(self, received_utc, line, event_complete=None):
        if self.stream_file is None:
            return
        record = {
            "received_utc": received_utc,
            "kind": "sse_line",
            "line": _redact_text(line, self.key),
        }
        if event_complete is not None:
            record["event_complete"] = event_complete
        try:
            self.stream_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._flush_if_due()
        except OSError as exc:
            self._warn(exc)
            try:
                self.stream_file.close()
            except OSError:
                pass
            self.stream_file = None

    def _flush_safe_events(self, final=False):
        if not self._pending_events:
            return
        self._sanitize_pending_segments()
        total = sum(len(value) for item in self._pending_events for _, value in item["segments"])
        safe_end = total if final or not self.key else max(0, total - len(self.key) + 1)
        flush_count = 0
        cursor = 0
        for item in self._pending_events:
            cursor += sum(len(value) for _, value in item["segments"])
            if cursor <= safe_end:
                flush_count += 1
            else:
                break
        for item in self._pending_events[:flush_count]:
            self._write_sse_event(item)
        del self._pending_events[:flush_count]
        self._flush_if_due()

    def record_reasoning(self, text):
        """Append reasoning exactly in arrival order, with cross-chunk key masking."""
        if self.reasoning_file is None:
            return
        safe = self._reasoning_redactor.feed(text)
        if safe:
            try:
                self.reasoning_file.write(safe)
                self._flush_if_due()
            except OSError as exc:
                self._warn(exc)
                try:
                    self.reasoning_file.close()
                except OSError:
                    pass
                self.reasoning_file = None

    def record_http_metadata(self, status, headers):
        safe_headers = {}
        for name, value in headers.items():
            lower_name = str(name).lower()
            if lower_name in HEADER_ALLOWLIST:
                safe_headers[lower_name] = _redact_text(str(value), self.key)
        metadata = {
            "received_utc": datetime.now(timezone.utc).isoformat(),
            "http_status": status,
            "headers": safe_headers,
        }
        try:
            path = self.output_dir / "response_metadata.json"
            path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            if "response_metadata.json" not in self._files:
                self._files.append("response_metadata.json")
        except OSError as exc:
            self._warn(exc)

    def record_response_body(self, body):
        """Preserve the complete received non-SSE body after exact-key redaction."""
        try:
            key_bytes = self.key.encode("utf-8") if self.key else b""
            safe = body.replace(key_bytes, b"[REDACTED]") if key_bytes else body
            path = self.output_dir / "response_body.bin"
            path.write_bytes(safe)
            self._files.append("response_body.bin")
        except OSError as exc:
            self._warn(exc)

    def close(self):
        if self._current_lines:
            partial_data = []
            for record in self._current_lines:
                line = record["line"].lstrip("\ufeff")
                if line.startswith("data:"):
                    partial_data.append(line[5:].lstrip(" "))
            event = "\n".join(partial_data) if partial_data else None
            self.record_sse_event(event, event_complete=False)
        self._flush_safe_events(final=True)
        tail = self._reasoning_redactor.finish()
        if tail and self.reasoning_file is not None:
            try:
                self.reasoning_file.write(tail)
            except OSError as exc:
                self._warn(exc)
        self._flush_if_due(force=True)
        for handle_name in ("stream_file", "reasoning_file"):
            handle = getattr(self, handle_name)
            if handle is not None:
                try:
                    handle.close()
                except OSError as exc:
                    self._warn(exc)
                setattr(self, handle_name, None)

