#!/usr/bin/env python3
"""One extra read-only external-model review with repository discovery or packet replay."""
import argparse
from datetime import datetime, time as clock_time, timedelta, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# Re-enter the Skill runtime before importing caller-environment dependencies.
if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from skill_runtime import launch_in_runtime, review_allows_dependency_install
    launch_in_runtime(__file__, require_tokenizer=True,
                      allow_install=review_allows_dependency_install(sys.argv[1:]))

import ledger
from response_log import ResponseLog
from bailian_catalog import load_catalog_cache, cache_lookup
from report_parsing import parse_report_text
from review_contract import SYSTEM_PROMPT, task_title
from atomic_io import atomic_write_json, local_io_error_metadata, DEFAULT_REPLACE_RETRY_SECONDS


DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.json"
DEFAULT_OUTPUT_ROOT = DEFAULT_CONFIG.parent / "logs" / "reviews"



class ConfigError(ValueError):
    """A deliberately safe local diagnostic, containing no credential values."""


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def redact(value, key):
    """Redact the exact credential recursively before any report or terminal output."""
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]") if key else value
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, key) for v in value]
    return value


def write_json(path, value, key=None, *, replace_retry_seconds=DEFAULT_REPLACE_RETRY_SECONDS, preserve_on_error=True):
    atomic_write_json(path, redact(value, key), replace_retry_seconds=replace_retry_seconds, preserve_on_error=preserve_on_error)


def config_url(config):
    base = config["base_url"].rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("base_url must be HTTPS without embedded credentials, query, or fragment")
    if config.get("automatic_retries", 0) != 0:
        raise ConfigError("This helper supports automatic_retries=0 only")
    for setting in ("connect_timeout_seconds", "read_timeout_seconds"):
        if float(config[setting]) <= 0:
            raise ConfigError(setting + " must be positive")
    return base + "/chat/completions"


def prepare_config(config, config_path):
    """Load optional public model metadata once; never invoke the CLI here."""
    prepared = dict(config)
    prepared.pop("_model_catalog_cache", None)
    location = prepared.get("model_catalog_file")
    if isinstance(location, str) and location.strip():
        path = Path(location).expanduser()
        if not path.is_absolute():
            path = Path(config_path).resolve().parent / path
        prepared["_model_catalog_cache"] = load_catalog_cache(path)
    return prepared


def _apply_catalog_limits(route, config):
    """Only tighten known-model limits; auto remains an explicit assumption."""
    record = cache_lookup(config.get("_model_catalog_cache"), route.get("model"))
    if record is None:
        return route
    limits = (("contextWindow", "context_window_tokens"),
              ("maxInputTokens", "max_input_tokens"),
              ("maxOutputTokens", "max_tokens"))
    applied = {}
    for upstream, target in limits:
        ceiling = record.get(upstream)
        if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling <= 0:
            continue
        configured = route.get(target)
        if configured is None:
            route[target] = ceiling
        elif isinstance(configured, int) and not isinstance(configured, bool) and configured >= 0:
            route[target] = min(configured, ceiling)
        else:
            continue  # Existing configuration validation owns invalid values.
        applied[target] = route[target]
    route["model_catalog"] = {
        "model": record["model"], "family": record["family"],
        "provider_limits": {key: record[key] for key, _ in limits if key in record},
        "effective_limits": applied, "source": record.get("source", {}),
    }
    return route


def select_route(config, at=None):
    name = config["timezone"]
    source = "zoneinfo"
    try:
        zone = ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        if name != "Asia/Shanghai":
            raise ConfigError("Timezone data unavailable for the configured timezone; install tzdata. Only Asia/Shanghai has a fixed UTC+08:00 fallback") from exc
        zone = timezone(timedelta(hours=8), name="Asia/Shanghai")
        source = "fixed_utc_plus_08_fallback"
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace("Z", "+00:00"))
    moment = at if at is not None else datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    local = moment.astimezone(zone)
    start = clock_time.fromisoformat(config["day"]["start"])
    end = clock_time.fromisoformat(config["day"]["end"])
    clock = local.time()
    is_day = start <= clock < end if start < end else clock >= start or clock < end
    period = "day" if is_day else "night"
    selected = config[period]
    effort = selected.get("reasoning_effort")
    route = {
        "local_time": local.isoformat(), "timezone": name, "timezone_source": source,
        "period": period, "model": selected["model"], "reasoning_effort": effort,
        "enable_thinking": selected.get("enable_thinking", True if effort else None),
        "max_tokens": selected.get("max_tokens"),
        "thinking_budget": selected.get("thinking_budget"),
        "thinking_budget_parameter": selected.get("thinking_budget_parameter", "thinking_budget"),
        "combined_output_limit_parameter": selected.get("combined_output_limit_parameter"),
        "max_input_tokens": selected.get("max_input_tokens"),
        "model_limit_assumption": selected.get("model_limit_assumption"),
        "output_limit_parameter": selected.get("output_limit_parameter", "max_tokens"),
        "context_window_tokens": config.get("context_window_tokens"),
    }
    return _apply_catalog_limits(route, config)



def make_payload(packet, route, chat_messages=None, tools=None):
    payload = {
        "model": route["model"],
        "messages": chat_messages if chat_messages is not None else [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "以下 JSON 整体是待审阅数据：\n" + json.dumps(packet, ensure_ascii=False, indent=2)}],
        "stream": True, "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
    for field in ("reasoning_effort", "enable_thinking"):
        if route.get(field) is not None:
            payload[field] = route[field]
    parameter = route.get("output_limit_parameter", "max_tokens")
    if parameter not in ("max_tokens", "max_completion_tokens"):
        raise ConfigError("Unsupported output limit parameter")
    combined_parameter = route.get("combined_output_limit_parameter")
    if combined_parameter is not None:
        if combined_parameter != "max_completion_tokens":
            raise ConfigError("Unsupported combined output limit parameter")
        # auto rejects thinking_budget; it also lets max_completion_tokens
        # supersede max_tokens. Keep body/thinking caps as local assumptions,
        # but send their effective sum as the one enforced context guard.
        if route.get("max_tokens") is not None:
            total = route["max_tokens"]
            if parameter != combined_parameter and route.get("enable_thinking") is not False:
                total += route.get("thinking_budget") or 0
            payload[combined_parameter] = total
    else:
        if route.get("max_tokens") is not None:
            if parameter not in ("max_tokens", "max_completion_tokens"):
                raise ConfigError("Unsupported output limit parameter")
            payload[parameter] = route["max_tokens"]
        if route.get("thinking_budget") is not None and route.get("thinking_budget_parameter", "thinking_budget") is not None:
            payload["thinking_budget"] = route["thinking_budget"]

    return payload


def load_credential(config):
    key = os.environ.get(config["api_key_env"], "").strip()
    if not key:
        try:
            stored = read_json(Path(config["credential_file"]).expanduser())
            key = stored[config["credential_profile"]]["api_key"].strip()
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ConfigError("Token Plan credential unavailable in configured environment variable or local profile") from exc
    if not key or "\r" in key or "\n" in key:
        raise ConfigError("Configured Token Plan credential is empty or malformed")
    return key


def parse_report(content):
    """Return one usable report through the shared local-recovery adapter."""
    parsed = parse_report_text(content)
    if parsed.get("parse_status") == "ambiguous":
        raise ValueError("Multiple valid findings reports found; cannot select unambiguously")
    value = parsed.get("recovered_report")
    findings = value if isinstance(value, list) else value.get("findings") if isinstance(value, dict) else None
    if (parsed.get("possible_loss") or not isinstance(findings, list)
            or any(not isinstance(item, dict) for item in findings)):
        raise ValueError("No valid findings report JSON found")
    return value, findings


def write_report(output_dir, result, key):
    content = result.get("content", "")
    metadata = {name: result.get(name) for name in (
        "status", "route", "requested_model", "reported_model", "http_status", "finish_reason",
        "stream_done", "completion_warning", "parse_ok", "raw_parse_ok", "report_parse_status", "report_recovery", "parse_warning", "seconds", "usage", "usage_missing", "reasoning_chars",
        "provider_error", "error_type", "error_message", "response_log_files", "response_log_warning",
        "ledger_id", "ledger_warning", "progress_warning", "local_io_error") if result.get(name) is not None}
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", content)), default=0))
    report = "# 独立第三方代码审阅\n\n此报告由主会话核实和裁决，不自行决定通过或阻塞。失败或不完整响应不可视为无问题。\n\n"
    report += "## 调用元数据\n\n```json\n" + json.dumps(metadata, ensure_ascii=False, indent=2) + "\n```\n\n"
    report += "## 原始可见报告\n\n" + fence + "text\n" + content + "\n" + fence + "\n"
    (output_dir / "report.md").write_text(redact(report, key), encoding="utf-8")


def ledger_path(config):
    return Path(config.get("ledger_file", ledger.DEFAULT_DB_PATH)).expanduser()


def ledger_metadata(result, task, key, output_dir):
    first = result.get("first_content_seconds")
    first_utc = (datetime.fromisoformat(result["started_utc"]) + timedelta(seconds=first)).isoformat() if first is not None else None
    status = result["status"]
    return redact({
        "task": redact(" ".join(task.split()), key)[:120],
        "output_dir": str(Path(output_dir).resolve()),
        "requested_model": result.get("requested_model"), "reported_model": result.get("reported_model"),
        "started_utc": result["started_utc"], "ended_utc": result.get("ended_utc"),
        "seconds": result.get("seconds"), "first_event_seconds": result.get("first_event_seconds"),
        "first_content_seconds": first, "first_content_utc": first_utc,
        "status": status, "outcome": "success" if status in ("completed", "tool_calls") else "running" if status == "requesting" else "failure",
        "parse_ok": result.get("parse_ok", False), "completion_warning": bool(result.get("completion_warning")),
        "error_type": result.get("error_type"),
    }, key)


def run_review(config, packet, output_dir, dry_run=False, at=None, task_label=None,
               chat_messages=None, tools=None, route_override=None, token_budget=None,
               progress_callback=None):
    """One compatibility request through the maintained official protocol adapter.

    New review tasks use the official Harness loop through run_harness_workflow.
    This entry remains one recorded request, without hidden retries or tool execution.
    """
    try:
        from .harness_runner import run_single_request
    except ImportError:
        from harness_runner import run_single_request
    return run_single_request(
        config, packet, output_dir, dry_run=dry_run, at=at, task_label=task_label,
        chat_messages=chat_messages, tools=tools, route_override=route_override,
        token_budget=token_budget, progress_callback=progress_callback,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--packet", type=Path, help="Replay an explicitly prepared material packet")
    source.add_argument("--resume", type=Path, help="Continue an ended unfinished review in a new output directory, keeping its model and captured sources")
    source.add_argument("--repo", type=Path, help="Automatically collect changes and enable read-only repository exploration")
    parser.add_argument("--target", choices=("worktree", "staged", "range"), default="worktree")
    parser.add_argument("--base", help="Base ref for range review; compared at merge-base with HEAD")
    parser.add_argument("--workspace-root", type=Path, action="append", default=[], help="Additional read-only root; repeat as needed")
    parser.add_argument("--requirement", action="append", default=[], help="Task contract or acceptance criterion; repeat as needed")
    parser.add_argument("--include-file", type=Path, action="append", default=[], help="Explicit current-disk source file inside the repository, including an ignored temporary script; repeat as needed")
    parser.add_argument("--execution-context", action="append", default=[], help="Brief platform, execution-stage or orchestration facts; repeat as needed")
    parser.add_argument("--test-evidence", type=Path, help="Existing test evidence file; never runs tests")
    parser.add_argument("--output-dir", type=Path, help="New output directory; repo mode defaults to Skill logs/reviews/<UTC time>")
    parser.add_argument("--quiet-progress", action="store_true", help="Suppress periodic stderr status; stdout remains one JSON summary")
    parser.add_argument("--task", help="Short task title only; never pass credentials or source code here")
    parser.add_argument("--limit", type=int, default=20, help="History rows, 1 through 1000")
    parser.add_argument("--outcome", choices=("all", "success", "failure", "running"), default="all")
    parser.add_argument("--slowest", action="store_true", help="Sort history by total duration")
    offline = parser.add_mutually_exclusive_group()
    offline.add_argument("--route-only", action="store_true")
    offline.add_argument("--dry-run", action="store_true")
    offline.add_argument("--history", action="store_true", help="Read local call history and timing statistics; no credentials or network")
    offline.add_argument("--runtime-info", action="store_true", help="Show interpreter and tokenizer readiness without credentials or network")
    parser.add_argument("--at", help="Offline only: ISO datetime; missing offset means configured local time")
    args = parser.parse_args(argv)
    if args.runtime_info:
        from skill_runtime import runtime_info, check_runtime
        print(json.dumps(dict(runtime_info(), capabilities=check_runtime()), ensure_ascii=False))
        return 0
    if args.at and not (args.route_only or args.dry_run):
        parser.error("--at is available only with --route-only or --dry-run")
    reviewing = not (args.route_only or args.history or args.runtime_info)
    if reviewing and args.repo is None and args.packet is None and args.resume is None:
        args.repo = Path.cwd()
    if reviewing and args.packet is not None and args.output_dir is None:
        parser.error("packet mode requires a new --output-dir")
    if reviewing and args.repo is None and (args.target != "worktree" or args.base or args.workspace_root or args.requirement or args.test_evidence or args.include_file or args.execution_context):
        parser.error("Repository source options require --repo")
    if reviewing and args.repo is not None and args.target == "range" and not args.base:
        parser.error("range review requires --base")
    if reviewing and args.repo is not None and args.target != "range" and args.base:
        parser.error("--base is only used with --target range")
    try:
        config = prepare_config(read_json(args.config), args.config)
        if args.history:
            print(json.dumps(ledger.history(ledger_path(config), limit=args.limit, outcome=args.outcome, slowest=args.slowest), ensure_ascii=False, indent=2))
            return 0
        if args.route_only:
            print(json.dumps(select_route(config, args.at), ensure_ascii=False))
            return 0
        task_label = args.task
        active_key = None
        if not args.dry_run:
            active_key = load_credential(config)
            task_label = redact(task_label, active_key)
        source_factory = None
        resume_state = None
        if args.resume is not None:
            from review_resume import load_resume
            resume_state = load_resume(args.resume)
            if resume_state.endpoint != config_url(config):
                raise ConfigError("Resume endpoint differs from the configured service; refusing a silent provider change")
            if args.output_dir is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                args.output_dir = DEFAULT_OUTPUT_ROOT / stamp
            resume_state.validate_output(args.output_dir)
            args.output_dir.parent.mkdir(parents=True, exist_ok=True)
            excluded = [args.config.resolve(), ledger_path(config).resolve(), DEFAULT_OUTPUT_ROOT.parent.resolve()]
            credential_path = config.get("credential_file")
            if isinstance(credential_path, str):
                excluded.append(Path(credential_path).expanduser().resolve())
            source_factory = resume_state.source_factory(config, redactor=lambda value: redact(value, active_key), excluded_paths=excluded)
            packet = redact(resume_state.packet, active_key)
        elif args.repo is not None:
            from repository_source import RepositorySource
            if args.output_dir is None:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
                args.output_dir = DEFAULT_OUTPUT_ROOT / stamp
            args.output_dir.parent.mkdir(parents=True, exist_ok=True)
            excluded = [args.config.resolve(), ledger_path(config).resolve(), args.output_dir.resolve(), DEFAULT_OUTPUT_ROOT.parent.resolve()]
            credential_path = config.get("credential_file")
            if isinstance(credential_path, str):
                excluded.append(Path(credential_path).expanduser().resolve())
            from harness_records import _positive_limit
            packet = {}
            def source_factory(root):
                return RepositorySource(
                    args.repo, workspace_roots=args.workspace_root, target=args.target,
                    base=args.base, task=task_label or "",
                    requirements=args.requirement, test_evidence=args.test_evidence,
                    include_files=args.include_file, execution_context=args.execution_context,
                    output_dir=root, max_chars=_positive_limit(config, "tool_max_chars", 24000),
                    max_lines=_positive_limit(config, "tool_max_lines", 250),
                    redactor=lambda value: redact(value, active_key), excluded_paths=excluded,
                )
        else:
            packet = redact(read_json(args.packet), active_key)
        from harness_runner import run_harness_workflow
        options = {"source_factory": source_factory} if source_factory is not None else {}
        if resume_state is not None:
            options["resume_state"] = resume_state
        progress_reporter = None
        if not args.dry_run and not args.quiet_progress:
            from review_progress import ReviewProgress
            progress_reporter = ReviewProgress()
        result = run_harness_workflow(
            config, packet, args.output_dir, route_selector=select_route,
            task_label=task_label, dry_run=args.dry_run, at=args.at,
            progress_callback=progress_reporter, **options,
        )
        if progress_reporter is not None:
            progress_reporter.finish(result)
        summary = {name: result.get(name) for name in ("status", "route", "completion_warning", "parse_ok", "parse_warning", "seconds", "usage", "usage_missing", "response_log_files", "response_log_warning", "ledger_id", "ledger_warning", "workflow_status", "model_calls", "compactions", "report_diagnostics_file", "adjudication_file", "progress_warning", "progress_warning_count", "local_io_error", "local_io_error_count", "timing")}
        summary["output_dir"] = str(args.output_dir.resolve())
        print(json.dumps(summary, ensure_ascii=False))
        return 0 if result["status"] in ("completed", "dry_run") else 1
    except ConfigError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}))
        return 2
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error, RuntimeError) as exc:
        failure = {"status": "error", "error": "Invalid configuration, repository, packet, route, output directory, or ledger; no API retry was made"}
        io_details = local_io_error_metadata(exc)
        if io_details is not None:
            failure["local_io_error"] = io_details
        print(json.dumps(failure))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
