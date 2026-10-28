"""Python source/audit adapter for the official DeepSeek Harness Node engine.

There is one live agent loop, in runtime/engine.mjs. This host dispatches only
its read-only material tools and persists project-specific artifacts.
"""
from datetime import datetime, timezone
import json
import os
import queue
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

try:
    from .harness_records import (RuntimeRecords, _as_text, _dump, _messages_for_review,
                                  _positive_limit, _save_outputs, _write_json, enrich_report, redact)
    from .materials import MaterialStore
    from .response_log import StreamingRedactor
    from .review_contract import SYSTEM_PROMPT, task_title
except ImportError:
    from harness_records import (RuntimeRecords, _as_text, _dump, _messages_for_review,
                                 _positive_limit, _save_outputs, _write_json, enrich_report, redact)
    from materials import MaterialStore
    from response_log import StreamingRedactor
    from review_contract import SYSTEM_PROMPT, task_title


SKILL_ROOT = Path(__file__).resolve().parent.parent
BRIDGE = SKILL_ROOT / "runtime" / "bridge.mjs"


class RuntimeProtocolError(RuntimeError):
    """Safe failure in the local Node/Python channel; never a provider retry."""


def _review_module():
    try:
        from . import review
    except ImportError:
        import review
    return review


def _material_context(store, config, route):
    limit = (config.get("context_management") or {}).get("inline_material_token_limit", 100_000)
    if limit is None:
        limit = 100_000
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("inline_material_token_limit must be a non-negative integer or null")
    input_cap = route.get("max_input_tokens")
    if not isinstance(input_cap, int) or isinstance(input_cap, bool) or input_cap <= 0:
        input_cap = None
    if input_cap is not None:
        safety = (config.get("context_management") or {}).get("safety_tokens", 0)
        if isinstance(safety, bool) or not isinstance(safety, int) or safety < 0:
            raise ValueError("safety_tokens must be a non-negative integer")
        input_cap -= safety
        if input_cap < 1:
            raise ValueError("The selected model input limit cannot fit the configured safety margin")
        limit = min(limit, input_cap)
    exploration = store.exploration_context() if getattr(store, "exploration_enabled", False) else None
    index_preview = None
    while True:
        bundle = store.inline_materials(token_budget=limit, model=route["model"])
        remaining = bundle.get("remaining_materials", [])
        preview = remaining if index_preview is None else remaining[:index_preview]
        index = {
            "available_materials": len(store.index()),
            "inline_delivered_ranges": bundle.get("delivered_ranges", []),
            "overflow_materials": preview,
            "inline_estimate": {"token_budget": limit, "estimated_tokens": bundle.get("estimated_tokens"),
                                "method": bundle.get("method"), "inline_chars": bundle.get("inline_chars"),
                                "inline_lines": bundle.get("inline_lines"), "count_warning": bundle.get("count_warning")},
        }
        if len(preview) < len(remaining):
            index["overflow_index_continuation"] = {"tool": "list_materials", "offset": len(preview),
                "instruction": "The complete material index remains available through the read-only list_materials tool."}
        tools = store.tools() if exploration or bundle.get("needs_tools") else []
        messages = _messages_for_review(bundle["task_context"], index,
                                        inline_materials=bundle.get("materials"), exploration=exploration)
        if input_cap is None:
            return bundle, tools, messages
        try:
            from .context_budget import estimate_tokens
        except ImportError:
            from context_budget import estimate_tokens
        # This distributes initial source data below a declared hard input cap.
        # Live pressure, retention and compression are decided only by Harness.
        counted = estimate_tokens(messages, tools, route["model"])
        if counted["input_tokens"] <= input_cap:
            bundle["declared_input_cap"] = input_cap
            bundle["prepared_request_estimate"] = counted
            return bundle, tools, messages
        if limit > 0:
            limit //= 2
            continue
        current_preview = len(preview)
        if current_preview > 0:
            index_preview = current_preview // 2
            continue
        raise ValueError("The selected model input limit cannot fit the task and read-only tool interface")


def _legacy_progress(state):
    work, notes = [], []
    for message in state.review_messages:
        if message.get("role") == "assistant":
            if _as_text(message.get("content")).strip():
                work.append(_as_text(message["content"]))
            for key in ("reasoning_content", "reasoning"):
                if isinstance(message.get(key), str) and message[key].strip():
                    notes.append(message[key])
        elif message.get("role") == "user":
            try:
                body = json.loads(_as_text(message.get("content")))
            except (ValueError, TypeError):
                continue
            if isinstance(body, dict) and isinstance(body.get("checkpoint_from_this_review"), str):
                work.append(body["checkpoint_from_this_review"])
    latest = state.latest_result
    if _as_text(latest.get("content")).strip():
        work.append(_as_text(latest["content"]))
    note = latest.get("reasoning_text")
    if not isinstance(note, str):
        try:
            note = (state.latest_call_dir / "reasoning.txt").read_text(encoding="utf-8")
        except OSError:
            note = ""
    if note.strip():
        notes.append(note)
    # Excludes provider errors, tool failure logs, terminal output and host
    # adjudication. All notes belong to this same unfinished review.
    return {"prior_checkpoint": getattr(state, "previous_checkpoint", None),
            "completed_work": work, "internal_notes": "\n\n".join(notes),
            "read_sources": state.read_sources,
            "inline_delivered_ranges": state.inline_delivered_ranges}


def _native_session_pair(state):
    """Match the loader's format decision; an orphan header is not a session."""
    directory = state.directory / "engine-session"
    return (directory / "session-header.json").is_file() and (directory / "session.jsonl").is_file()


def build_engine_spec(config, root, route, messages, tools, resume_state=None):
    session_id = "review-" + str(uuid.uuid4())
    spec = {"sessionId": session_id, "outputDir": str(Path(root).resolve()), "provider": "review",
            "route": route, "systemPrompt": SYSTEM_PROMPT,
            "initialUserContent": messages[-1]["content"], "tools": tools,
            "contextManagement": config.get("context_management") or {},
            "apiRecovery": config.get("api_recovery") or {},
            "readTimeoutSeconds": config.get("read_timeout_seconds", 300),
            "connectTimeoutSeconds": config.get("connect_timeout_seconds", 30),
            "tokenizerDir": str(SKILL_ROOT / "assets" / "deepseek_v4_tokenizer"),
            "tokenizerDirectory": str(SKILL_ROOT / "assets" / "deepseek_v4_tokenizer")}
    if resume_state is not None:
        previous_engine = resume_state.directory / "engine-session"
        header_path = previous_engine / "session-header.json"
        if _native_session_pair(resume_state):
            resolved = previous_engine.resolve()
            if not resolved.is_relative_to(resume_state.directory.resolve()):
                raise ValueError("The prior engine session escapes its review directory")
            if not header_path.resolve().is_relative_to(resolved):
                raise ValueError("The prior engine session header escapes its directory")
            events_path = previous_engine / "session.jsonl"
            if events_path.exists() and not events_path.resolve().is_relative_to(resolved):
                raise ValueError("The prior engine event log escapes its directory")
            metadata = json.loads(header_path.read_text(encoding="utf-8"))
            header = metadata.get("header") if isinstance(metadata, dict) else None
            old_id = header.get("id") if isinstance(header, dict) else None
            if not isinstance(old_id, str) or not old_id:
                raise ValueError("Recorded engine session ID is missing")
            spec["sessionId"] = old_id
            spec["resume"] = {"sourceDirectory": str(resolved), "sessionId": old_id}
            # Hard interruption may leave a received reasoning prefix only in
            # the audit capture, before the official attempt was settled.
            if getattr(resume_state, "crash_recovery_candidate", False):
                notes = resume_state.latest_result.get("reasoning_text")
                if isinstance(notes, str) and notes.strip():
                    spec["legacyProgress"] = {"internal_notes": notes,
                        "read_sources": resume_state.read_sources,
                        "inline_delivered_ranges": resume_state.inline_delivered_ranges}
        else:
            spec["legacyProgress"] = _legacy_progress(resume_state)
    return spec


def _bridge_environment():
    result = dict(os.environ)
    for name in ("NODE_OPTIONS", "NODE_PATH", "NODE_USE_ENV_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                 "http_proxy", "https_proxy", "all_proxy"):
        result.pop(name, None)
    result["NODE_USE_ENV_PROXY"] = "0"
    return result


def _read_stderr(pipe, path, key, warnings):
    masker = StreamingRedactor(key)
    import codecs
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    try:
        with path.open("w", encoding="utf-8", newline="\n") as log:
            while True:
                raw = pipe.read(4096)
                if not raw:
                    break
                safe = masker.feed(decoder.decode(raw, final=False))
                if safe:
                    log.write(safe)
                    log.flush()
            tail = masker.feed(decoder.decode(b"", final=True)) + masker.finish()
            log.write(tail)
    except OSError as exc:
        warnings.append("Runtime stderr capture unavailable (%s)." % type(exc).__name__)


def _ensure_runtime(config):
    try:
        from .node_runtime import ensure_node_runtime
    except ImportError:
        from node_runtime import ensure_node_runtime
    return ensure_node_runtime(SKILL_ROOT, allow_install=True, config=config)


def _run_bridge(spec, secret, records, store, config, *, method="run", payload=None):
    executable = config.get("node_executable") or shutil.which("node")
    if not executable:
        raise RuntimeProtocolError("Node.js is unavailable; run Skill setup with Node.js installed")
    command = [str(executable), str(BRIDGE)]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               cwd=str(SKILL_ROOT), env=_bridge_environment(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    warnings = []
    stderr = threading.Thread(target=_read_stderr, args=(process.stderr, records.root / "runtime_stderr.txt", secret["apiKey"], warnings), daemon=True)
    stderr.start()
    completed = None
    fatal = None

    def send(value):
        process.stdin.write((_dump(value) + "\n").encode("utf-8"))
        process.stdin.flush()

    def handle_request(request):
        method, params = request.get("method"), request.get("params") or {}
        if method == "requestStart":
            return records.request_start(params)
        if method == "requestEnd":
            return records.request_end(params)
        if method in {"executeTool", "execute_tool"}:
            if store is None:
                raise RuntimeProtocolError("Direct requests do not expose executable tools")
            return records.record_tool(store, params)
        raise RuntimeProtocolError("Unsupported runtime host request")

    incoming = queue.Queue()
    def read_stdout():
        try:
            for raw in process.stdout:
                incoming.put(raw)
        finally:
            incoming.put(None)
    stdout = threading.Thread(target=read_stdout, daemon=True)
    stdout.start()
    cancelled = False
    cancel_deadline = None
    active_request = None

    def consume(raw):
        nonlocal completed, fatal, active_request
        try:
            message = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise RuntimeProtocolError("Runtime emitted an invalid protocol message") from None
        if not isinstance(message, dict):
            raise RuntimeProtocolError("Runtime protocol message must be an object")
        if isinstance(message.get("request"), dict):
            request = message["request"]
            active_request = request.get("id")
            try:
                if fatal is not None or cancelled and request.get("method") not in {"requestEnd"}:
                    raise RuntimeProtocolError("Host stopped dispatching further work")
                response = handle_request(request)
            except Exception as exc:
                # Negative acknowledgement prevents a record failure from
                # becoming an unlogged request or an automatic provider retry.
                fatal = exc
                send({"reply": active_request, "error": {"code": "HOST_IO_ERROR", "message": "Local review record or source dispatch failed (%s)" % type(exc).__name__}})
                active_request = None
                return False
            send({"reply": active_request, "result": response})
            active_request = None
            return False
        event = message.get("event")
        data = message.get("data") or {}
        if not isinstance(data, dict):
            raise RuntimeProtocolError("Runtime event data must be an object")
        if event == "engine.complete":
            completed = data.get("result") if isinstance(data.get("result"), dict) else data
            return True
        if event in {"request.start", "request.end"}:
            (records.request_start if event == "request.start" else records.request_end)(data)
        elif isinstance(event, str) and event.startswith("response."):
            records.response_event(event, data)
        elif event in {"context.checkpoint", "recovery.checkpoint"}:
            text = data.get("text", data.get("checkpoint", data.get("content")))
            if isinstance(text, str) and text.strip():
                try:
                    checkpoints = records.root / "checkpoints"
                    checkpoints.mkdir(exist_ok=True)
                    path = checkpoints / ("%04d.txt" % (len(records.workflow["checkpoint_history"])+1))
                    path.write_text(records.safe(text), encoding="utf-8")
                    records.workflow["checkpoint_history"].append(str(path.resolve()))
                    records.workflow["checkpoint_file"] = str(path.resolve())
                    (records.root / "checkpoint.txt").write_text(records.safe(text), encoding="utf-8")
                    records.workflow["checkpoint_file"] = str((records.root / "checkpoint.txt").resolve())
                except OSError as exc:
                    # The official Session has already committed the checkpoint.
                    # This human-readable compatibility export is advisory.
                    records.workflow["checkpoint_export_warning"] = "Checkpoint text export unavailable (%s); the committed official Session remains available." % type(exc).__name__
                    records.workflow["checkpoint_session_directory"] = str((records.root / "engine-session").resolve())
            records.workflow["recovery_events"].append(records.safe(dict(data, action="checkpoint")))
            records.save_flow()
        elif event == "recovery.stopped":
            records.workflow["recovery_events"].append(records.safe(data))
            records.save_flow()
        elif event == "session.event":
            records.workflow["engine_event_count"] = records.workflow.get("engine_event_count", 0) + 1
        elif event == "runtime.preflight_error":
            records.workflow.setdefault("runtime_preflight_errors", []).append(records.safe(data))
            records.save_flow()
        return False

    try:
        initialization = {"method": method, "spec": spec, "secret": secret}
        if payload is not None:
            initialization["payload"] = payload
        send(initialization)
        while True:
            try:
                timeout = None if cancel_deadline is None else max(0.0, cancel_deadline-time.monotonic())
                raw = incoming.get(timeout=timeout)
                if raw is None or consume(raw):
                    break
            except queue.Empty:
                break
            except KeyboardInterrupt:
                if cancelled:
                    raise
                cancelled = True
                cancel_deadline = time.monotonic() + 10
                send({"method": "cancel", "cancel": True})
                if active_request is not None:
                    send({"reply": active_request, "error": {"code": "HOST_CANCELLED", "message": "Operator cancelled"}})
                    active_request = None
        if cancelled:
            completed = dict(completed or {}, status="interrupted", finishReason="operator_cancelled")
        if completed is None:
            raise RuntimeProtocolError("Runtime ended without a completed host result")
        if fatal is not None and not cancelled:
            raise RuntimeProtocolError("Runtime encountered a permanent local record failure") from fatal
    finally:
        try:
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        stdout.join(timeout=2)
        stderr.join(timeout=2)
        for pipe in (process.stdout, process.stderr):
            try:
                pipe.close()
            except OSError:
                pass
        if warnings:
            records.workflow["runtime_log_warning"] = warnings[0]
    return completed


def run_harness_workflow(config, packet, output_dir, *, route_selector,
                         source_factory=None, resume_state=None, dry_run=False, at=None,
                         task_label=None, progress_callback=None):
    """Prepare local materials, then drive exactly one official Harness session."""
    if at is not None and not dry_run:
        raise ValueError("at is available only for offline preparation")
    root = Path(output_dir)
    if resume_state is not None:
        resume_state.validate_output(root)
    root.mkdir(parents=True, exist_ok=False)
    (root / "calls").mkdir()
    start = datetime.now(timezone.utc).isoformat()
    workflow = {"status": "preparing", "task": str(task_label or "Independent review")[:120], "route": None,
                "started_utc": start, "child_calls": [], "compactions": 0, "tool_calls": 0,
                "checkpoint_history": [], "recovery_events": [], "retry_wait_seconds": 0.0,
                "history_cleanup_events": [], "uncompleted_reason": None, "_started_monotonic": time.perf_counter()}
    store, records = None, None
    module = _review_module()
    try:
        route = dict(resume_state.route) if resume_state is not None else route_selector(config, at if dry_run else None)
        workflow["route"] = route
        endpoint = module.config_url(config)
        key = None if dry_run else module.load_credential(config)
        packet = redact(packet, key)
        store = source_factory(root) if source_factory is not None else MaterialStore(
            packet, max_chars=_positive_limit(config, "tool_max_chars", 24000),
            max_lines=_positive_limit(config, "tool_max_lines", 250))
        if source_factory is not None:
            packet = store.packet
        _write_json(root / "materials.json", redact(packet, key))
        native_resume = resume_state is not None and _native_session_pair(resume_state)
        material_config = config
        if native_resume:
            # The official session already owns prior source history. Refresh
            # only its readable index instead of duplicating the old inline body.
            material_config = dict(config, context_management=dict(config.get("context_management") or {}, inline_material_token_limit=0))
        bundle, tools, messages = _material_context(store, material_config, route)
        if native_resume:
            tools = store.tools()
            workflow["resume_material_strategy"] = "recorded_history_plus_readonly_index"
        messages, tools = redact(messages, key), redact(tools, key)
        workflow["task"] = task_title(task_label, bundle["task_context"].get("task") or packet.get("task"))[:120]
        workflow["inline_materials"] = {key: bundle.get(key) for key in (
            "estimated_tokens", "method", "inline_chars", "inline_lines", "fully_inlined", "count_warning")}
        records = RuntimeRecords(root, config, route, workflow["task"], key, workflow, bundle, endpoint, progress_callback)
        if resume_state is not None:
            workflow["resume"] = resume_state.provenance
            workflow["inherited_read_sources"] = list(resume_state.read_sources)
            workflow["inherited_inline_delivered_ranges"] = list(resume_state.inline_delivered_ranges)
            records.read_sources.extend(resume_state.read_sources)
            records.confirmed_inline.extend(resume_state.inline_delivered_ranges)
        records.save_flow()
        if dry_run:
            directory = root / "calls" / "0001-review"
            directory.mkdir()
            payload = module.make_payload({}, route, chat_messages=messages, tools=tools)
            _write_json(directory / "request.json", {"url": endpoint, "route": route, "payload": payload})
            workflow["child_calls"].append({"stage": "review", "directory": str(directory.resolve()), "status": "dry_run",
                                             "timing_outcome": "planned", "inline_material_ranges": bundle.get("delivered_ranges", []),
                                             "inline_estimated_tokens": bundle.get("estimated_tokens"), "inline_estimate_method": bundle.get("method")})
            workflow.update(status="dry_run", uncompleted_reason="Prepared local material and initial request only; no runtime or model call was started.")
            if resume_state is not None:
                workflow["resume"]["checkpoint_pending"] = True
            return _save_outputs(root, workflow, {"status": "dry_run", "content": ""}, len(store.index()), [], records.read_sources, start, source_store=store)
        spec = redact(build_engine_spec(config, root, route, messages, tools, resume_state), key)
        ready = _ensure_runtime(config)
        runtime_config = dict(config, node_executable=ready["executable"])
        engine = redact(_run_bridge(spec, {"apiKey": key, "endpoint": endpoint}, records, store, runtime_config), key)
        workflow["engine"] = {"name": "DeepSeek Harness", "version": "0.2.1-rc.2", "sessionId": engine.get("sessionId", spec["sessionId"]),
                              "sessionDirectory": engine.get("sessionDirectory", str((root / "engine-session").resolve())),
                              "finishReason": engine.get("finishReason"), "stats": engine.get("stats"),
                              "error_type": engine.get("error_type"), "error_message": engine.get("error_message")}
        workflow["retry_wait_seconds"] = engine.get("retryWaitSeconds", 0) or 0
        status = engine.get("status")
        workflow["status"] = "completed" if status == "completed" else "error" if status == "error" else "incomplete"
        final = records.final_result
        content = engine.get("finalContent", engine.get("content"))
        if workflow["status"] == "completed" and isinstance(content, str):
            final = enrich_report(dict(final or {}, status="completed", content=content))
        if workflow["status"] == "completed" and not (final or {}).get("parse_ok"):
            workflow.update(status="incomplete", uncompleted_reason="The runtime ended but the structured review report is incomplete or ambiguous.")
        elif workflow["status"] != "completed":
            detail = engine.get("error_message") or engine.get("finishReason") or status
            workflow["uncompleted_reason"] = "Official Harness session ended with %s; original attempts and available partial reports are preserved." % detail
        records.close_pending()
        return _save_outputs(root, workflow, final, len(store.index()), records.tool_trace, records.read_sources, start, source_store=store)
    except KeyboardInterrupt:
        workflow.update(status="incomplete", uncompleted_reason="Interrupted by the operator; received attempts are preserved.")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        workflow.update(status="error", uncompleted_reason="Local Harness preparation or protocol failed (%s); no automatic model rerun was made." % type(exc).__name__)
        workflow["runtime_error_type"] = type(exc).__name__
        if isinstance(exc, (RuntimeProtocolError, module.ConfigError)) or type(exc).__name__ == "NodeRuntimeError":
            workflow["uncompleted_reason"] = "Local Harness preparation failed: " + redact(str(exc), key if "key" in locals() else None)
        try:
            from .atomic_io import local_io_error_metadata
        except ImportError:
            from atomic_io import local_io_error_metadata
        details = local_io_error_metadata(exc)
        if details is not None:
            workflow["local_io_error"] = details
    if records is not None:
        records.close_pending()
    return _save_outputs(root, workflow, records.final_result if records else None, len(store.index()) if store else 0,
                         records.tool_trace if records else [], records.read_sources if records else [], start, source_store=store)


def run_single_request(config, packet, output_dir, dry_run=False, at=None, task_label=None,
                       chat_messages=None, tools=None, route_override=None, token_budget=None,
                       progress_callback=None):
    """Compatibility entry for one explicitly recorded official protocol call.

    This never executes tool functions, runs an agent loop or retries internally.
    Workflow requests use run_harness_workflow instead.
    """
    if at is not None and not dry_run:
        raise ValueError("at is available only for offline preparation")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=False)
    module = _review_module()
    start = time.perf_counter()
    route = None
    key = None
    records = None
    result = {"status": "dry_run" if dry_run else "requesting", "content": "", "parse_ok": False,
              "started_utc": datetime.now(timezone.utc).isoformat(), "usage": {}, "usage_missing": True,
              "tool_calls": [], "finish_reason": None, "first_event_seconds": None,
              "first_content_seconds": None, "reasoning_chars": 0, "response_log_files": []}
    try:
        endpoint = module.config_url(config)
        route = dict(route_override) if route_override is not None else module.select_route(config, at)
        result.update(route=route, requested_model=route["model"], reported_model=None)
        if not dry_run:
            key = module.load_credential(config)
        payload = redact(module.make_payload(packet, route, chat_messages=chat_messages, tools=tools), key)
        if token_budget is not None:
            _write_json(root / "token_budget.json", redact(token_budget, key))
        if dry_run:
            _write_json(root / "request.json", {"url": endpoint, "route": route, "payload": payload})
            result.update(seconds=time.perf_counter()-start, ended_utc=datetime.now(timezone.utc).isoformat())
            _write_json(root / "result.json", result)
            RuntimeRecords._write_call_report(root, result)
            return result
        label = task_title(redact(task_label, key), redact(packet.get("task"), key))[:120]
        workflow = {"status":"preparing", "task":label, "route":route, "child_calls":[], "compactions":0,
                    "tool_calls":0, "checkpoint_history":[], "recovery_events":[], "retry_wait_seconds":0}
        records = RuntimeRecords(root, config, route, label, key, workflow, {}, endpoint, progress_callback, direct_request=True)
        ready = _ensure_runtime(config)
        spec = {"sessionId":"request-"+str(uuid.uuid4()), "outputDir":str(root.resolve()), "route":route,
                "readTimeoutSeconds":config.get("read_timeout_seconds",300),
                "connectTimeoutSeconds":config.get("connect_timeout_seconds",30), "provider":"review"}
        runtime_config = dict(config, node_executable=ready["executable"])
        _run_bridge(spec, {"apiKey":key,"endpoint":endpoint}, records, None, runtime_config, method="single", payload=payload)
        records.close_pending()
        if len(records.calls) != 1:
            raise RuntimeProtocolError("Direct transport did not record exactly one attempt")
        result = dict(next(iter(records.calls.values()))["result"])
        result["transport_process_seconds"] = time.perf_counter()-start
    except KeyboardInterrupt:
        result.update(status="interrupted", error_type="KeyboardInterrupt", error_message="Interrupted by the operator; available output is preserved")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        if records is not None:
            records.close_pending()
            if records.calls:
                result = dict(next(iter(records.calls.values()))["result"])
        result.update(status="error", error_type=type(exc).__name__, error_message="Local runtime or request setup failed; no automatic retry was made")
    result = redact(result, key)
    result.setdefault("seconds", time.perf_counter()-start)
    result.setdefault("ended_utc", datetime.now(timezone.utc).isoformat())
    _write_json(root / "result.json", result)
    RuntimeRecords._write_call_report(root, result)
    return result
