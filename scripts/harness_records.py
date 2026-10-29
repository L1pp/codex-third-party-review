"""Project-specific review artifacts around the official Harness runtime.

Provider parsing, retries, tool scheduling and context mutation belong to Node
Harness services. This module owns only local audit records and adjudication.
"""
from datetime import datetime, timedelta, timezone
import base64
import codecs
import json
from pathlib import Path
import re
import time

try:
    from .atomic_io import atomic_write_json, local_io_error_metadata
    from .response_log import ResponseLog
    from .report_parsing import parse_report_text
    from .review_contract import SYSTEM_PROMPT
    from . import ledger
except ImportError:
    from atomic_io import atomic_write_json, local_io_error_metadata
    from response_log import ResponseLog
    from report_parsing import parse_report_text
    from review_contract import SYSTEM_PROMPT
    import ledger

def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    return str(value)

def _write_json(path, value):
    atomic_write_json(path, value, default=_json_default)

def _append_jsonl(path, value):
    """Append one complete local observation without rewriting earlier records."""
    with Path(path).open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_dump(value) + "\n")


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)

def _as_text(value):
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)

def _positive_limit(config, name, default):
    settings = config.get("context_management", {})
    value = settings.get(name, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, value)

def _messages_for_review(task_context, material_index, inline_materials=None, checkpoint=None, exploration=None):
    user = {
        "task_context": task_context,
        "materials_index": material_index,
        "instruction": "审阅已提供的材料；索引中的overflow部分可按需读取。",
    }
    if exploration:
        user["repository_access"] = exploration
        user["instruction"] += " 需要验证调用方、契约、配置或测试时，主动使用只读仓库工具，不受初始材料索引限制。"
    if inline_materials:
        user["inline_materials"] = inline_materials
    if checkpoint:
        user["checkpoint_from_this_review"] = checkpoint
        user["instruction"] += " 从 checkpoint 继续核查；需要时仍可调用材料读取函数验证原始证据。"
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _dump(user)},
    ]

def _scope_only(arguments):
    """Keep a local path/range trace without copying search text or source."""
    allowed = (
        "root", "path", "file", "file_path", "start_line", "end_line", "line_start",
        "line_end", "start", "end", "offset", "limit", "line_count",
        "char_offset", "section", "symbol",
    )
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return {}
    if not isinstance(arguments, dict):
        return {}
    scope = {key: arguments[key] for key in allowed if key in arguments}
    if isinstance(arguments.get("requests"), list):
        scope["requests"] = [
            {"tool": item.get("tool"), "scope": _scope_only(item.get("arguments"))}
            for item in arguments["requests"] if isinstance(item, dict)
        ]
    if "query" in arguments and isinstance(arguments["query"], str):
        scope["query_chars"] = len(arguments["query"])
    return scope

def _save_outputs(root, workflow, final_result, material_count, tool_trace, read_sources,
                  started_utc, source_store=None):
    diagnostic_files = {}
    report_summary = None
    recovered_report = None
    recovered_findings = None
    try:
        try:
            from .report_diagnostics import analyze_report, save_diagnostics
        except ImportError:
            from report_diagnostics import analyze_report, save_diagnostics
        if source_store is not None and hasattr(source_store, "diagnostic_sources"):
            sources = source_store.diagnostic_sources()
        else:
            sources = [
                {"path": item["path"], "aliases": [item.get("source_path")], "content": item["content"]}
                for item in getattr(source_store, "_materials", []) if item.get("kind") == "file"
            ]
        diagnostics = (
            {"parse_status": "not_requested", "possible_loss": False, "warnings": [], "findings": []}
            if workflow.get("status") == "dry_run" else analyze_report(
                _as_text((final_result or {}).get("content")),
                findings=(final_result or {}).get("parsed_findings"), sources=sources,
            )
        )
        save_diagnostics(root, diagnostics)
        recovered_report = diagnostics.get("recovered_report")
        recovered_findings = diagnostics.get("recovered_findings")
        report_summary = {
            "request_status": (final_result or {}).get("status"),
            "finish_reason": (final_result or {}).get("finish_reason"),
            "stream_done": (final_result or {}).get("stream_done"),
            "parse_status": diagnostics.get("parse_status"),
            "raw_parse_ok": diagnostics.get("parse_status") == "valid",
            "retained_findings": len(diagnostics.get("findings", [])),
            "possible_loss": bool(diagnostics.get("possible_loss")),
            "recovery_counts": diagnostics.get("counts", {}),
            "adjudication_status_at_creation": "pending",
        }
        diagnostic_files = {
            "report_diagnostics_file": str((root / "report_diagnostics.json").resolve()),
            "adjudication_file": str((root / "adjudication.json").resolve()),
        }
        if diagnostics.get("possible_loss") and workflow.get("status") == "completed":
            workflow.update(status="incomplete", uncompleted_reason="Report recovery may have lost findings; original response and diagnostics are preserved.")
    except (ImportError, OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        workflow["report_diagnostics_warning"] = "Derived report diagnostics unavailable (%s)." % type(exc).__name__
        io_details = local_io_error_metadata(exc)
        if io_details is not None:
            workflow.setdefault("local_io_error", io_details)
    if source_store is not None and hasattr(source_store, "repository_manifest"):
        workflow["repository"] = source_store.repository_manifest()
    workflow.update(diagnostic_files)
    elapsed = time.perf_counter() - workflow["_started_monotonic"]
    workflow.pop("_started_monotonic", None)
    workflow["workflow_status"] = workflow.get("status", "incomplete")
    workflow["model_calls"] = 0 if workflow["status"] == "dry_run" else len(workflow.get("child_calls", []))
    if workflow["status"] == "dry_run":
        workflow["planned_calls"] = len(workflow.get("child_calls", []))
    workflow["content_is_partial"] = (
        workflow["status"] != "completed" or (final_result or {}).get("status") != "completed"
    )
    workflow["ended_utc"] = datetime.now(timezone.utc).isoformat()
    workflow["seconds"] = elapsed
    inline_requests = [
        {
            "call": call.get("directory"),
            "stage": call.get("stage"),
            "status": call.get("status"),
            "ranges": call.get("inline_material_ranges", []),
            "estimated_tokens": call.get("inline_estimated_tokens"),
            "method": call.get("inline_estimate_method"),
            **({"overflow_materials_file": call["inline_overflow_materials_file"]}
               if "inline_overflow_materials_file" in call else
               {"overflow_materials": call.get("inline_overflow_materials", [])}),
        }
        for call in workflow.get("child_calls", [])
        if "inline_estimated_tokens" in call
    ]
    inline_ranges = [item for request in inline_requests for item in request["ranges"]]
    workflow["coverage"] = {
        "indexed_materials": material_count,
        "material_read_requests": len(tool_trace),
        "tool_requests": tool_trace,
        "read_sources": read_sources,
        "inline_delivery_requests": inline_requests,
        "inline_delivered_ranges": workflow.get("confirmed_inline_ranges", inline_ranges),
        **({"overflow_materials_at_last_review_file": workflow["material_delivery_index_file"],
            "overflow_materials_at_last_review_count": workflow.get("inline_overflow_materials_count", 0)}
           if workflow.get("material_delivery_index_file") else
           {"overflow_materials_at_last_review": inline_requests[-1].get("overflow_materials", []) if inline_requests else []}),
        "tool_delivered_ranges": read_sources,
        "status": "not_verified_by_workflow",
        "coverage_gap": (
            "No source material was included." if not material_count else
            "No source material was delivered in the request or through tools."
            if not inline_ranges and not read_sources else None
        ),
    }
    workflow["started_utc"] = started_utc
    def seconds_for(call):
        value = call.get("seconds")
        return max(0.0, float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    child_calls = workflow.get("child_calls", [])
    workflow["timing"] = {
        "successful_request_seconds": sum(seconds_for(c) for c in child_calls if c.get("timing_outcome") == "success"),
        "failed_request_seconds": sum(seconds_for(c) for c in child_calls if c.get("timing_outcome") == "failure"),
        "incomplete_request_seconds": sum(seconds_for(c) for c in child_calls if c.get("timing_outcome") == "incomplete"),
        "other_request_seconds": sum(seconds_for(c) for c in child_calls if c.get("timing_outcome") not in {"success", "failure", "incomplete"}),
        "retry_wait_seconds": workflow.get("retry_wait_seconds", 0.0),
        # This is a subset of the request timings above, not an additive total.
        "compaction_request_seconds": sum(seconds_for(c) for c in child_calls if c.get("stage", "").startswith("compaction")),
    }
    warning_count = sum(bool(call.get("progress_warning")) for call in workflow.get("child_calls", []))
    workflow["progress_warning_count"] = warning_count
    if warning_count:
        workflow["progress_warning"] = "Progress reporting warnings occurred in %d call(s); see child call records." % warning_count
    workflow["local_io_error_count"] = sum(bool(call.get("local_io_error")) for call in workflow.get("child_calls", [])) + int(bool(workflow.get("local_io_error")))
    _write_json(root / "workflow.json", workflow)

    content = _as_text((final_result or {}).get("content"))
    status = workflow.get("status", "incomplete")
    result = {
        "status": status,
        "workflow_status": status,
        "route": workflow.get("route"),
        "started_utc": started_utc,
        "ended_utc": workflow["ended_utc"],
        "seconds": elapsed,
        "child_calls": workflow.get("child_calls", []),
        "model_calls": workflow.get("model_calls", 0),
        "planned_calls": workflow.get("planned_calls"),
        "compactions": workflow.get("compactions", 0),
        "tools_executed": len(tool_trace),
        "coverage": workflow["coverage"],
        "checkpoint_file": workflow.get("checkpoint_file"),
        "uncompleted_reason": workflow.get("uncompleted_reason"),
        "content": content,
        "content_is_partial": workflow["content_is_partial"],
        "parsed_report": recovered_report if recovered_report is not None else (final_result or {}).get("parsed_report"),
        "parsed_findings": recovered_findings if recovered_findings is not None else (final_result or {}).get("parsed_findings"),
        "parse_ok": bool((final_result or {}).get("parse_ok")) and not (report_summary or {}).get("possible_loss", False),
        "raw_parse_ok": (report_summary or {}).get("raw_parse_ok", False),
        "report_summary": report_summary,
        **diagnostic_files,
        "report_diagnostics_warning": workflow.get("report_diagnostics_warning"),
        "checkpoint_export_warning": workflow.get("checkpoint_export_warning"),
        "checkpoint_session_directory": workflow.get("checkpoint_session_directory"),
        "progress_warning_count": workflow.get("progress_warning_count", 0),
        "progress_warning": workflow.get("progress_warning"),
        "local_io_error_count": workflow.get("local_io_error_count", 0),
        "local_io_error": workflow.get("local_io_error"),
        "repository": workflow.get("repository"),
        "engine": workflow.get("engine"),
        "resume": workflow.get("resume"),
        "recovery_events": workflow.get("recovery_events", []),
        "timing": workflow.get("timing"),
        "record_format_version": workflow.get("record_format_version", 1),
        "call_history_file": workflow.get("call_history_file"),
        "material_delivery_index_file": workflow.get("material_delivery_index_file"),
        "tool_metrics": workflow.get("tool_metrics", {}),
        "review_timing": workflow.get("review_timing"),
        "review_policy": workflow.get("review_policy"),
        "review_timing_file": workflow.get("review_timing_file"),
        "review_timing_warning": workflow.get("review_timing_warning"),
        "record_metadata_warning": workflow.get("record_metadata_warning"),
    }
    _write_json(root / "result.json", result)
    # Keep the human report small. Full per-request/delivery facts remain in
    # workflow/result and the unmodified raw audit files, not copied into Markdown.
    meta = {
        "status": status, "model": (workflow.get("route") or {}).get("model"),
        "seconds": elapsed, "model_calls": result["model_calls"],
        "tools_executed": len(tool_trace), "compactions": result["compactions"],
        "content_is_partial": result["content_is_partial"],
        "parse_ok": result["parse_ok"], "report_summary": report_summary,
        "timing": workflow.get("timing"), "tool_metrics": workflow.get("tool_metrics", {}),
        "review_timing": workflow.get("review_timing"),
        "coverage": {"indexed_materials": material_count, "read_observations": len(read_sources),
                     "status": workflow["coverage"]["status"],
                     "coverage_gap": workflow["coverage"]["coverage_gap"]},
        "uncompleted_reason": result["uncompleted_reason"],
        "progress_warning_count": result["progress_warning_count"],
        "local_io_error_count": result["local_io_error_count"],
        "review_timing_warning": workflow.get("review_timing_warning"),
        "record_metadata_warning": workflow.get("record_metadata_warning"),
    }
    report = "# 独立第三方代码审阅工作流\n\n"
    report += "工作流状态：`%s`。由主会话核实和裁决；失败、不完整或缺少报告不表示审阅通过。\n\n" % status
    report += "## 摘要\n\n```json\n%s\n```\n\n" % json.dumps(meta, ensure_ascii=False, indent=2)
    trace_links = [("workflow.json", "工作流"), ("result.json", "结果"), ("calls", "调用目录"),
                   ("call_history.jsonl", "调用事件"), ("tool_results.jsonl", "工具原文"),
                   ("material_delivery_index.json", "材料投递索引"), ("review_timing.jsonl", "本机计时")]
    report += "完整追踪：" + "、".join("[%s](%s)" % (label, filename)
                                        for filename, label in trace_links if (root / filename).exists()) + "。\n\n"
    if content:
        report += "## 最终可见报告\n\n"
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", content)), default=0))
        report += fence + "text\n" + content + "\n" + fence + "\n"
    elif workflow.get("uncompleted_reason"):
        report += "## 未完成原因\n\n" + str(workflow["uncompleted_reason"]) + "\n"
    (root / "report.md").write_text(report, encoding="utf-8")
    return result


def redact(value, key):
    """Mask the active credential before any local record or RPC tool reply."""
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]") if key else value
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    return value


def has_generation_progress(result, reasoning_text=None):
    """Observe delivered generation only; usage/first-event metadata is not proof."""
    if isinstance(reasoning_text, str) and reasoning_text:
        return True
    for name in ("content", "reasoning_text", "reasoning_content"):
        if isinstance(result.get(name), str) and result[name]:
            return True
    count = result.get("reasoning_chars")
    if isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0:
        return True
    for item in result.get("tool_calls") or []:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("id"), str) and item["id"]:
            return True
        function = item.get("function")
        if isinstance(function, dict) and any(function.get(key) for key in ("name", "arguments")):
            return True
    return False


def enrich_report(result):
    """Local syntax recovery remains independent of provider completion status."""
    if result.get("status") == "tool_calls":
        return result
    try:
        parsed = parse_report_text(_as_text(result.get("content")))
    except Exception as exc:
        result.update(parse_ok=False, raw_parse_ok=False, report_parse_status="invalid",
                      parsed_report=None, parsed_findings=None,
                      report_recovery={"error": type(exc).__name__, "possible_loss": True},
                      parse_warning="Local report recovery failed; original response is preserved for host adjudication")
        return result
    result["report_parse_status"] = parsed["parse_status"]
    result["raw_parse_ok"] = parsed["parse_status"] == "valid"
    result["report_recovery"] = {
        key: parsed[key] for key in (
            "repair", "repair_log", "raw_parse_error", "counts", "possible_loss",
            "warnings", "candidates", "unverified_candidates", "unreadable_candidates",
            "recovery_integrity",
        ) if key in parsed
    }
    value = parsed.get("recovered_report")
    findings = value if isinstance(value, list) else value.get("findings") if isinstance(value, dict) else None
    if isinstance(findings, list):
        result.update(parsed_report=value, parsed_findings=findings,
                      parse_ok=not parsed.get("possible_loss") and all(isinstance(item, dict) for item in findings))
    else:
        result.update(parsed_report=None, parsed_findings=None, parse_ok=False)
    return result


class WireAudit:
    """Frame physical wire lines only for audit/redaction, never model parsing.

    pi-ai and the official Harness converter own interpretation of provider
    events. This observer preserves received SSE text and forwards its data
    grouping only to ResponseLog's cross-event credential masker.
    """
    def __init__(self, directory, key):
        self.log = ResponseLog(directory, key)
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""
        self.data_lines = []
        self.sse = True
        self.body = bytearray()
        self.closed = False

    def metadata(self, status, headers):
        self.sse = "event-stream" in str(next((v for k, v in headers.items() if str(k).lower() == "content-type"), "")).lower()
        self.log.record_http_metadata(status, headers)

    def feed(self, raw):
        if not self.sse:
            self.body.extend(raw)
            return
        self.pending += self.decoder.decode(raw, final=False)
        self._lines(final=False)

    def _line(self, line):
        self.log.record_sse_line(line)
        if not line:
            self.log.record_sse_event("\n".join(self.data_lines) if self.data_lines else None)
            self.data_lines = []
        elif line.lstrip("\ufeff").startswith("data:"):
            data = line.lstrip("\ufeff")[5:]
            self.data_lines.append(data[1:] if data.startswith(" ") else data)

    def _lines(self, final):
        start = index = 0
        while index < len(self.pending):
            char = self.pending[index]
            if char not in "\r\n":
                index += 1
                continue
            if char == "\r" and index + 1 == len(self.pending) and not final:
                break
            self._line(self.pending[start:index])
            index += 2 if char == "\r" and self.pending[index:index+2] == "\r\n" else 1
            start = index
        self.pending = self.pending[start:]
        if final and self.pending:
            self._line(self.pending)
            self.pending = ""

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.sse:
            self.pending += self.decoder.decode(b"", final=True)
            self._lines(final=True)
        elif self.body:
            self.log.record_response_body(bytes(self.body))
        self.log.close()


def inline_ranges_in_request(payload):
    """Return host inline ranges physically present on this request, never bundle intent."""
    ranges = []
    for message in payload.get("messages", []) if isinstance(payload, dict) else []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        texts = [content] if isinstance(content, str) else [part.get("text") for part in content if isinstance(part, dict) and part.get("type") == "text"] if isinstance(content, list) else []
        for text in texts:
            if not isinstance(text, str):
                continue
            try:
                envelope = json.loads(text)
            except ValueError:
                continue
            if not isinstance(envelope, dict) or not isinstance(envelope.get("inline_materials"), list):
                continue
            index = envelope.get("materials_index") or {}
            material_paths = {item.get("path") for item in envelope["inline_materials"] if isinstance(item, dict)}
            for scope in index.get("inline_delivered_ranges", []) if isinstance(index, dict) else []:
                if isinstance(scope, dict) and scope.get("path") in material_paths and scope not in ranges:
                    ranges.append(scope)
    return ranges


class RuntimeRecords:
    """Local records for explicit attempts emitted by the official Node loop."""
    def __init__(self, root, config, route, label, key, workflow, inline_bundle,
                 endpoint, progress_callback=None, direct_request=False):
        self.root, self.config, self.route = Path(root), config, route
        self.label, self.key, self.workflow = label, key, workflow
        self.bundle, self.endpoint, self.progress_callback = inline_bundle, endpoint, progress_callback
        self.calls = {}
        self.latest_candidates = []
        self.final_result = None
        self.tool_trace, self.read_sources = [], []
        self.last_progress = {}
        self.confirmed_inline = workflow.setdefault("confirmed_inline_ranges", [])
        self.recovery_waits = {}
        self.direct_request = direct_request
        self.read_spans = {}
        self.tool_metrics = {"read_requests": 0, "read_chars": 0,
                             "repeated_read_chars": 0, "read_line_instances": 0,
                             "execution_seconds": 0.0}
        self.workflow["tool_metrics"] = self.tool_metrics
        if not direct_request:
            index_file = self.root / "material_delivery_index.json"
            overflow = self.bundle.get("remaining_materials", [])
            _write_json(index_file, self.safe({"overflow_materials": overflow}))
            self.workflow.update(record_format_version=2,
                                 material_delivery_index_file=str(index_file.resolve()),
                                 inline_overflow_materials_count=len(overflow),
                                 call_history_file=str((self.root / "call_history.jsonl").resolve()))

    def safe(self, value):
        return redact(value, self.key)

    def save_flow(self):
        if self.direct_request:
            return
        # The official Session, per-call audit and append-only traces retain all
        # history. A running checkpoint needs only the latest usable candidates.
        snapshot = {key: value for key, value in self.workflow.items()
                    if key not in {"_started_monotonic", "child_calls", "coverage", "inherited_read_sources"}}
        children = self.workflow.get("child_calls", [])
        snapshot["child_calls"] = self.latest_candidates
        snapshot["child_calls_count"] = len(children)
        snapshot["child_calls_are_latest_candidates"] = True
        inherited = list(self.workflow.get("inherited_inline_delivered_ranges") or [])
        for scope in self.confirmed_inline:
            if scope not in inherited:
                inherited.append(scope)
        snapshot["inherited_inline_delivered_ranges"] = inherited
        # Search/read observations are small source references. Retaining these
        # keeps the existing resume reader compatible without replaying tools.
        snapshot["coverage"] = {"read_sources": self.read_sources,
                                "inline_delivered_ranges": self.confirmed_inline}
        _write_json(self.root / "workflow.json", self.safe(snapshot))

    def _record_call(self, call, event):
        if self.direct_request:
            return
        metadata = self.safe(call["meta"])
        try:
            _write_json(call["directory"] / "audit.json", metadata)
            _append_jsonl(self.root / "call_history.jsonl", {"event": event, **metadata})
        except OSError as exc:
            # These are lightweight mirrors. Request/result and the official
            # Session remain authoritative; a new mirror cannot add a stop gate.
            warning = "Call metadata mirror unavailable (%s); original attempt records remain available." % type(exc).__name__
            self.workflow["record_metadata_warning"] = warning
            call["meta"]["record_metadata_warning"] = warning

    def record_timing(self, data):
        """Host-clock facts only; no operational details enter model materials."""
        value = self.safe(data)
        self.workflow["review_timing"] = value
        if not self.direct_request:
            try:
                path = self.root / "review_timing.jsonl"
                _append_jsonl(path, value)
                self.workflow["review_timing_file"] = str(path.resolve())
                self.save_flow()
            except OSError as exc:
                # The durable official Session owns the clock/reminder state.
                # This lightweight operational sidecar must not interrupt it.
                self.workflow["review_timing_warning"] = "Timing sidecar unavailable (%s); review continued." % type(exc).__name__

    def _progress(self, call, state, force=False):
        now = time.monotonic()
        if not force and now - self.last_progress.get(call["id"], -1000) < 10:
            return
        self.last_progress[call["id"]] = now
        result = call["result"]
        event = {"state": state, "call": call["directory"].name,
                 "reasoning_chars": result.get("reasoning_chars", 0),
                 "visible_chars": call.get("visible_chars", 0)}
        try:
            atomic_write_json(call["directory"] / "progress.json", self.safe(dict(
                event, seconds=time.monotonic()-call["started_monotonic"], route=self.route,
                first_event_seconds=result.get("first_event_seconds"),
                first_content_seconds=result.get("first_content_seconds"),
                updated_utc=datetime.now(timezone.utc).isoformat(),
            )), replace_retry_seconds=0, preserve_on_error=False)
        except OSError as exc:
            result["progress_warning"] = "Progress metadata unavailable (%s); provider stream continued." % type(exc).__name__
        if self.progress_callback is not None:
            try:
                self.progress_callback(self.safe(event))
            except Exception:
                result.setdefault("progress_warning", "CLI progress output unavailable; provider stream continued.")

    def request_start(self, data):
        ident = str(data.get("id", ""))
        if not ident or ident in self.calls:
            raise ValueError("Duplicate or missing runtime request ID")
        stage = "compaction" if "compact" in str(data.get("purpose", "")).lower() else "review"
        number = len(self.workflow["child_calls"]) + 1
        if self.direct_request and number != 1:
            raise ValueError("A direct request cannot hide a second API attempt")
        directory = self.root if self.direct_request else self.root / "calls" / ("%04d-%s" % (number, stage))
        if not self.direct_request:
            directory.mkdir(parents=True, exist_ok=False)
        started = datetime.now(timezone.utc).isoformat()
        route = data.get("route") if isinstance(data.get("route"), dict) else self.route
        result = {"status": "requesting", "route": route, "requested_model": route.get("model"),
                  "started_utc": started, "content": "", "usage": {}, "usage_missing": True,
                  "reasoning_chars": 0, "first_event_seconds": None, "first_content_seconds": None,
                  "finish_reason": None, "stream_done": False, "tool_calls": [], "parse_ok": False}
        # This critical write must succeed before the RPC acknowledge permits
        # fetch. Ledger and progress remain advisory, matching the existing CLI.
        _write_json(directory / "request.json", self.safe({"url": self.endpoint, "route": route, "payload": data.get("payload")}))
        budget = data.get("token_budget") or data.get("tokenBudget")
        if budget is not None:
            _write_json(directory / "token_budget.json", self.safe(budget))
            result["token_budget"] = budget
        call_meta = {"stage": stage, "directory": str(directory.resolve()), "status": "requesting",
                     "requested_model": route.get("model"), "runtime_request_id": ident}
        actual_inline = inline_ranges_in_request(data.get("payload") or {})
        call_meta.update(inline_material_ranges=actual_inline,
                         inline_estimated_tokens=None,
                         inline_estimate_method="actual_wire_envelope_ranges")
        if not self.direct_request:
            call_meta["inline_overflow_materials_file"] = self.workflow["material_delivery_index_file"]

        call = {"id": ident, "directory": directory, "result": result, "meta": call_meta,
                "started_monotonic": time.monotonic(), "visible_chars": 0, "audit": WireAudit(directory, self.key)}
        self.calls[ident] = call
        self.workflow["child_calls"].append(call_meta)
        self.latest_candidates = [item for item in self.latest_candidates if item.get("stage") != stage] + [call_meta]
        self.workflow["status"] = "running"
        if stage == "compaction":
            self.workflow["compactions"] += 1
        try:
            call["ledger_id"] = ledger.start_call(self.config.get("ledger_file", ledger.DEFAULT_DB_PATH), self._ledger_metadata(result, directory))
            result["ledger_id"] = call["ledger_id"]
        except Exception as exc:
            result["ledger_warning"] = "Local call ledger unavailable (%s)." % type(exc).__name__
        self._record_call(call, "request_start")
        self.save_flow()
        self._progress(call, "requesting", force=True)
        return {"recorded": True, "directory": str(directory.resolve())}

    def _ledger_metadata(self, result, directory):
        first = result.get("first_content_seconds")
        started = result["started_utc"]
        # Node emits a terminal Z; Python 3.10's fromisoformat needs +00:00.
        timestamp = started[:-1] + "+00:00" if started.endswith(("Z", "z")) else started
        first_utc = (datetime.fromisoformat(timestamp) + timedelta(seconds=first)).isoformat() if isinstance(first, (int, float)) and not isinstance(first, bool) else None
        status = result.get("status", "error")
        return self.safe({"task": self.label[:120], "output_dir": str(directory.resolve()),
                          "requested_model": result.get("requested_model"), "reported_model": result.get("reported_model"),
                          "started_utc": result["started_utc"], "ended_utc": result.get("ended_utc"),
                          "seconds": result.get("seconds"), "first_event_seconds": result.get("first_event_seconds"),
                          "first_content_seconds": first, "first_content_utc": first_utc,
                          "status": status, "outcome": "success" if status in ("completed", "tool_calls") else "running" if status == "requesting" else "failure",
                          "parse_ok": result.get("parse_ok", False), "completion_warning": bool(result.get("completion_warning")),
                          "error_type": result.get("error_type")})

    def response_event(self, event, data):
        call = self.calls.get(str(data.get("id", "")))
        if call is None:
            raise ValueError("Response event has no recorded request")
        result, audit = call["result"], call["audit"]
        if event == "response.http":
            result["http_status"] = data.get("status")
            audit.metadata(data.get("status"), data.get("headers") or {})
        elif event == "response.raw":
            audit.feed(base64.b64decode(data.get("base64", ""), validate=True))
        elif event == "response.reasoning":
            text = _as_text(data.get("text"))
            audit.log.record_reasoning(text)
            result["reasoning_chars"] += len(text)
        elif event == "response.content":
            call["visible_chars"] += len(_as_text(data.get("text")))
        elif event == "response.provider":
            # This is an audit/progress observation, never the authoritative
            # parser or aggregate used by the official agent loop.
            body = data.get("data")
            if isinstance(body, dict):
                for choice in body.get("choices") or []:
                    delta = choice.get("delta") or {} if isinstance(choice, dict) else {}
                    if isinstance(delta, dict) and isinstance(delta.get("content"), str):
                        call["visible_chars"] += len(delta["content"])
        self._progress(call, "streaming")

    def request_end(self, data):
        ident = str(data.get("id", ""))
        call = self.calls.get(ident)
        if call is None:
            raise ValueError("Unknown runtime request completion")
        result = call["result"]
        if call.get("ended"):
            raise ValueError("Request completion already recorded")
        call["audit"].close()
        result.update(self.safe({k: v for k, v in data.items() if k not in {"id", "number"}}))
        result.setdefault("seconds", time.monotonic()-call["started_monotonic"])
        result["ended_utc"] = datetime.now(timezone.utc).isoformat()
        result["usage_missing"] = not bool(result.get("usage"))
        result["response_log_files"] = call["audit"].log.files
        if call["audit"].log.warning:
            result["response_log_warning"] = call["audit"].log.warning
        if call["meta"]["stage"] != "compaction":
            enrich_report(result)
        status, finish = result.get("status"), result.get("finish_reason")
        # Final progress is advisory, but any warning must be present before
        # the authoritative attempt and root metadata are committed.
        self._progress(call, str(status), force=True)
        outcome = "incomplete" if finish == "length" else "success" if status in {"completed", "tool_calls"} else "failure"
        call["meta"].update({name: result.get(name) for name in (
            "status", "seconds", "finish_reason", "first_content_seconds", "usage", "reported_model", "parse_ok", "error_type", "progress_warning", "local_io_error") if result.get(name) is not None})
        call["meta"].update(timing_outcome=outcome, response_log_files=result["response_log_files"])
        if call["meta"]["stage"] == "review":
            has_progress = has_generation_progress(result)
            call["meta"]["recovery_classification"] = {"has_progress": has_progress}
            if status in {"completed", "tool_calls"} or has_progress:
                for scope in call["meta"].get("inline_material_ranges", []):
                    if scope not in self.confirmed_inline:
                        self.confirmed_inline.append(scope)
            if status != "tool_calls":
                visible = _as_text(result.get("content")).strip()
                if (visible and status not in {"error", "interrupted"}) or self.final_result is None or not _as_text(self.final_result.get("content")).strip():
                    self.final_result = result
        _write_json(call["directory"] / "result.json", result)
        self._write_call_report(call["directory"], result)
        try:
            ledger.finish_call(self.config.get("ledger_file", ledger.DEFAULT_DB_PATH), call.get("ledger_id"), self._ledger_metadata(result, call["directory"]))
        except Exception as exc:
            result["ledger_warning"] = "Local call ledger finish unavailable (%s)." % type(exc).__name__
            _write_json(call["directory"] / "result.json", result)
        call["ended"] = True
        self._record_call(call, "request_end")
        self.save_flow()
        return {"recorded": True}

    @staticmethod
    def _write_call_report(directory, result):
        content = _as_text(result.get("content"))
        metadata = {k: v for k, v in result.items() if k not in {"content", "reasoning_text", "parsed_report", "parsed_findings"}}
        fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", content)), default=0))
        text = "# Independent review request\n\nFailures and partial reports require host adjudication.\n\n```json\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n```\n\n" + fence + "text\n" + content + "\n" + fence + "\n"
        (directory / "report.md").write_text(text, encoding="utf-8")

    def _observe_delivery(self, name, arguments, result):
        if not isinstance(result, dict) or result.get("error"):
            return
        if name in {"batch_read", "batch_search"}:
            for item in result.get("results") or []:
                if isinstance(item, dict):
                    self._observe_delivery(item.get("tool"), item.get("arguments") or {}, item.get("result"))
            return
        if name in {"read_material", "read_file"} and result.get("text"):
            scope = {key: result.get(key) for key in (
                "path", "source_id", "root", "view", "source_origin", "start_line", "end_line",
                "line_numbers", "char_offset", "next_char_offset", "next_start_line", "complete") if key in result}
            if isinstance(arguments, dict) and "char_offset" not in scope:
                requested_line = arguments.get("start_line", 1)
                returned_line = scope.get("start_line")
                if returned_line is None and scope.get("line_numbers"):
                    returned_line = scope["line_numbers"][0]
                # MaterialStore normalizes a cursor at a line's end to the
                # next line at offset zero. Record the delivered position,
                # while the raw tool log retains the caller's original cursor.
                advanced = isinstance(returned_line, int) and returned_line > requested_line
                scope["char_offset"] = 0 if advanced else arguments.get("char_offset", 0)
            self.read_sources.append(scope)
            self.tool_metrics["read_requests"] += 1
            text = result["text"]
            self.tool_metrics["read_chars"] += len(text)
            lines = result.get("line_numbers") or []
            self.tool_metrics["read_line_instances"] += len(lines)
            identity = tuple(result.get(key) for key in ("path", "source_id", "root", "view"))
            # Track exact character intervals on each delivered source line;
            # split pages of one long line are not falsely counted as rereads.
            for index, (line, part) in enumerate(zip(lines, text.splitlines(keepends=True))):
                start = scope.get("char_offset", 0) if index == 0 else 0
                end = start + len(part)
                spans = self.read_spans.setdefault((identity, line), [])
                overlap = sum(max(0, min(end, right)-max(start, left)) for left, right in spans)
                self.tool_metrics["repeated_read_chars"] += overlap
                merged = []
                for left, right in sorted(spans + [(start, end)]):
                    if merged and left <= merged[-1][1]:
                        merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                    else:
                        merged.append((left, right))
                self.read_spans[(identity, line)] = merged
        elif name in {"search_material", "search_code"}:
            for match in result.get("matches") or []:
                if isinstance(match, dict) and (match.get("text") or match.get("snippet")):
                    self.read_sources.append({**{key: match.get(key) for key in (
                        "path", "source_id", "root", "view", "line", "char_offset") if key in match},
                        "access": "search_snippet"})

    def record_tool(self, store, params):
        started = time.perf_counter()
        name = params.get("name")
        arguments = params.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                arguments = None
        if not isinstance(name, str) or not isinstance(arguments, dict):
            result = {"error": "Tool arguments must be a JSON object."}
        else:
            try:
                result = store.execute(name, arguments)
            except Exception as exc:
                result = {"error": "Material read failed (%s)." % type(exc).__name__}
        result = self.safe(result)
        elapsed = max(0.0, time.perf_counter()-started)
        before_reads = {key: self.tool_metrics[key] for key in ("read_requests", "read_chars", "repeated_read_chars")}
        is_error = isinstance(result, dict) and bool(result.get("error"))
        if not is_error:
            self._observe_delivery(name, arguments, result)
        delivery = {key: self.tool_metrics[key] - value for key, value in before_reads.items()}
        _append_jsonl(self.root / "tool_results.jsonl", {
            "index": len(self.tool_trace)+1, "tool_call_id": params.get("callId"),
            "name": name, "arguments": self.safe(arguments), "result": result,
            "execution_seconds": elapsed, "delivery": delivery,
        })
        self.tool_trace.append({"name": name, "scope": _scope_only(arguments),
                                "status": "error" if is_error else "returned",
                                "execution_seconds": elapsed, "delivery": delivery})
        self.tool_metrics["execution_seconds"] += elapsed
        self.workflow["tool_calls"] = len(self.tool_trace)
        self.save_flow()
        return result

    def close_pending(self, reason="Runtime ended before request completion"):
        for call in self.calls.values():
            if call.get("ended"):
                continue
            result = dict(call["result"], status="error", error_type="RuntimeProtocolError", error_message=reason,
                          seconds=time.monotonic()-call["started_monotonic"])
            self.request_end(dict(result, id=call["id"]))
