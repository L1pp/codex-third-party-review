"""Report selection and integrity policy around the json-repair dependency.

Syntax repair belongs to json-repair. This adapter only chooses independent
review reports and records whether an original frame was visibly unfinished;
its candidates are never evidence that review coverage or findings are correct.
"""
from __future__ import annotations

import copy
from importlib.metadata import PackageNotFoundError, version
import json
import re

try:
    from json_repair.json_parser import JSONParser as _JSONParser
except ImportError:  # Valid JSON can still be inspected before dependencies are installed.
    _JSONParser = None

try:
    _VERSION = version("json-repair")
except PackageNotFoundError:
    _VERSION = None


def findings_from(value):
    items = value if isinstance(value, list) else value.get("findings") if isinstance(value, dict) else None
    return items if isinstance(items, list) else None


def _library_values(text):
    """Read distinct values with json-repair 0.64.2's single-value parser.

    The public top-level repair helper may merge or deduplicate successive
    objects. This small version-pinned adapter uses the upstream parser's
    cursor instead, so our report-selection policy sees every answer. No
    JSON quoting, bracket repair, or character resynchronization lives here.
    """
    parser = _JSONParser(text, None, True, try_valid_json_suffix=True)
    while parser.index < len(text):
        start, log_start = parser.index, len(parser.logger)
        value = parser.parse_json(record_top_level_value=True)
        if parser.index <= start:
            raise ValueError("The JSON recovery parser did not advance")
        if parser._last_parse_found_value:
            yield value, text[start:parser.index].strip(), copy.deepcopy(parser.logger[log_start:])


def _visibly_unfinished(original, value, repair_log, strict_error):
    """Keep visible truncation distinct from successfully repaired syntax.

    Closing text is not a guarantee of semantic completeness. The library
    reconstructs partial values too, which must remain explicitly unverified.
    """
    expected = "}" if isinstance(value, dict) else "]"
    if not original.rstrip().endswith(expected):
        return True
    if strict_error is not None:
        if getattr(strict_error, "pos", 0) >= len(original.rstrip()):
            return True
        if "Unterminated string" in str(strict_error):
            return True
    return any("missed the closing" in entry.get("text", "")
               or "missed closing" in entry.get("text", "") for entry in repair_log)


def _base():
    return {
        "parse_status": "invalid", "possible_loss": True, "warnings": [],
        "repair": {"engine": "json-repair", "version": _VERSION, "applied": False},
        "repair_log": [], "recovery_integrity": "unverified",
        "counts": {"recovered_findings": 0, "candidate_reports": 0,
                   "candidate_findings": 0, "unreadable_candidates": 0},
    }


def parse_report_text(content):
    """Return a derived report candidate and explicit recovery/integrity metadata.

    No schema-guided coercion or default business fields are requested. Any
    library reconstruction remains labelled as such; unfinished report fields
    and ambiguous independent reports must not become a clean empty verdict.
    """
    result = _base()
    if not isinstance(content, str) or not content.strip():
        result["warnings"] = ["No visible report text is available."]
        return result
    text = content.strip()
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        result["raw_parse_error"] = {"message": str(exc), "offset": getattr(exc, "pos", None)}
    else:
        items = findings_from(value)
        if items is None:
            result["warnings"] = ["JSON is not a top-level findings report."]
            return result
        result.update(parse_status="valid", possible_loss=False, recovered_report=value,
                      recovery_integrity="original_json")
        result["counts"].update(recovered_findings=len(items), candidate_reports=1,
                                candidate_findings=len(items))
        return result
    if _JSONParser is None:
        result["warnings"] = ["The json-repair runtime dependency is unavailable; original report text is preserved."]
        result["repair"]["error"] = "dependency_unavailable"
        return result

    values = []
    recovery_error = None
    try:
        for value in _library_values(text):
            values.append(value)
    except Exception as exc:
        # Keep every value and repair log already yielded before a later
        # parser failure; an incomplete suffix cannot erase earlier evidence.
        recovery_error = type(exc).__name__
        result["repair"].update(applied=True, error=recovery_error)
        result["warnings"].append("Local JSON recovery stopped early; already recovered candidates and original report text are preserved.")
    candidates = []
    unreadable = []
    for index, (value, original, logs) in enumerate(values):
        strict_error = None
        try:
            json.loads(original)
        except (ValueError, RecursionError) as exc:
            strict_error = exc
            result["repair"]["applied"] = True
        result["repair_log"].extend(dict(entry, candidate_index=index) for entry in logs)
        closed = not _visibly_unfinished(original, value, logs, strict_error)
        items = findings_from(value)
        # Incidental scalar arrays in prose are not findings reports. Whole JSON
        # arrays are accepted above, including malformed fields for diagnostics.
        if isinstance(value, list) and value and not any(isinstance(item, dict) for item in value):
            continue
        if items is None:
            if isinstance(value, dict) and "findings" in value:
                unreadable.append(original)
            continue
        if isinstance(value, list) and not value:
            explicit_line = re.search(r"(?m)^[ \t]*\[\s*\][ \t]*$", original)
            explicit_fence = (re.search(r"```(?:json)?\s*\[\s*\]\s*$", original, flags=re.I)
                              and re.search(r"```(?:json)?\s*\[\s*\]\s*```", text, flags=re.I))
            if not explicit_line and not explicit_fence:
                # Incidental prose [] is not evidence of an empty review.
                continue
        if not closed and not items:
            unreadable.append(original)
            continue
        candidates.append({"value": value, "closed": closed, "original": original})
    result["unreadable_candidates"] = unreadable
    result["counts"].update(candidate_reports=len(candidates),
                            candidate_findings=sum(len(findings_from(row["value"])) for row in candidates),
                            unreadable_candidates=len(unreadable))

    complete = [row for row in candidates if row["closed"]]
    unfinished = [row for row in candidates if not row["closed"]]
    if len(complete) > 1 or not complete and len(unfinished) > 1:
        result.update(parse_status="ambiguous", candidates=[row["value"] for row in candidates],
                      warnings=result["warnings"] + ["Multiple independent findings reports were found; none was selected."])
        return result
    if not candidates:
        result.update(parse_status="partial" if unreadable else "invalid",
                      warnings=result["warnings"] + ["No findings report could be recovered without selecting unrelated or empty reconstructed data."])
        return result
    chosen = (complete or unfinished)[0]
    if complete and unfinished:
        # Preserve the readable report and explicitly expose additional partial
        # reports; never silently replace it with an unfinished second answer.
        unreadable.extend(row["original"] for row in unfinished)
        result["unverified_candidates"] = [row["value"] for row in unfinished]
        result["counts"]["unreadable_candidates"] = len(unreadable)
    partial = not chosen["closed"] or bool(unreadable) or recovery_error is not None
    result.update(parse_status="partial" if partial else "recovered", possible_loss=partial,
                  recovered_report=chosen["value"],
                  recovery_integrity="unverified" if partial or result["repair"]["applied"] else "original_json",
                  warnings=result["warnings"] + (["The report is visibly unfinished or has additional unreadable content; reconstructed fields are unverified."] if partial else
                           ["Report syntax or surrounding formatting was recovered locally; findings still require host verification."]))
    result["counts"]["recovered_findings"] = len(findings_from(chosen["value"]))
    return result
