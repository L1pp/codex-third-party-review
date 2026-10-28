"""Non-blocking report diagnostics and local main-session adjudication records.

All location checks use caller-supplied text. This module never reads repository
paths, invokes a model, executes project code, or rewrites original reports.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from skill_runtime import launch_in_runtime
    launch_in_runtime(__file__)


_VERDICTS = {"confirmed", "dismissed", "needs_check"}
_FINDING_ID = re.compile(r"^finding-[0-9]{4,}$")


def _utc():
    return datetime.now(timezone.utc).isoformat()


try:
    from .report_parsing import findings_from as _findings, parse_report_text
    from .review_contract import normalize_priority
except ImportError:
    from report_parsing import findings_from as _findings, parse_report_text
    from review_contract import normalize_priority


def _path(value):
    if not isinstance(value, str):
        return None
    value = value.replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _field_diagnostics(finding):
    issues = []
    if not isinstance(finding, dict):
        return {"valid": False, "issues": [{"field": None, "code": "not_object"}]}
    for name in ("priority", "file", "line", "issue", "basis", "uncertainty"):
        if name not in finding:
            issues.append({"field": name, "code": "missing"})
        elif name == "line":
            value = finding[name]
            if value is not None and (type(value) is not int or value < 1):
                issues.append({"field": name, "code": "invalid_line"})
        elif not isinstance(finding[name], str):
            issues.append({"field": name, "code": "not_string"})
        elif name in ("file", "issue") and not finding[name].strip():
            issues.append({"field": name, "code": "empty"})
    if isinstance(finding.get("priority"), str) and normalize_priority(finding["priority"]) is None:
        issues.append({"field": "priority", "code": "unknown_priority"})
    return {"valid": not issues, "issues": issues}


def _source_candidates(finding, sources):
    path = _path(finding.get("file"))
    if not path:
        return []

    def matching(reported_path):
        matches = []
        for index, source in enumerate(sources, 1):
            if not isinstance(source, dict) or not isinstance(source.get("content"), str):
                continue
            aliases = source.get("aliases") if isinstance(source.get("aliases"), list) else []
            names = [source.get("path")] + aliases
            if reported_path in [_path(name) for name in names if isinstance(name, str)]:
                matches.append({"path": source.get("path"),
                                "source_id": source.get("source_id") or "input-source-%04d" % index,
                                "content": source["content"]})
        return matches

    # Real directories and explicit aliases named a/ or b/ take precedence.
    # Only an otherwise unknown path may be a copied Git diff header.
    matches = matching(path)
    if not matches and path.startswith(("a/", "b/")):
        matches = matching(path[2:])
        for item in matches:
            item["path_normalization"] = {"kind": "git_diff_prefix_removed",
                                          "reported_path": finding["file"], "normalized_path": path[2:]}
    return matches


def _location(finding, sources):
    if not isinstance(finding, dict) or not isinstance(finding.get("file"), str) or not finding["file"].strip():
        return {"status": "unverified", "reason": "No usable file path was reported."}
    candidates = _source_candidates(finding, sources)
    result = _location_candidates(finding, candidates)
    if candidates and "path_normalization" in candidates[0]:
        result["path_normalization"] = dict(candidates[0]["path_normalization"])
    return result


def _location_candidates(finding, candidates):
    if not candidates:
        return {"status": "missing", "path": finding["file"], "reason": "Path is not among the supplied or read sources."}
    explicit = finding.get("source_id")
    if explicit is not None:
        candidates = [source for source in candidates if source["source_id"] == explicit]
        if not candidates:
            return {"status": "missing", "path": finding["file"], "reason": "The reported source version is unavailable."}
    quote = next((finding[key] for key in ("evidence", "quote", "code")
                  if isinstance(finding.get(key), str) and finding[key].strip()), None)
    if quote is not None:
        occurrences = []
        for source in candidates:
            position = 0
            while len(occurrences) < 2:
                offset = source["content"].find(quote, position)
                if offset < 0:
                    break
                start = source["content"].count("\n", 0, offset) + 1
                end = start + quote.count("\n") - int(quote.endswith("\n"))
                occurrences.append({"source_id": source["source_id"], "path": source["path"],
                                    "line": start, "end_line": max(start, end)})
                position = offset + 1
            if len(occurrences) >= 2:
                break
        if not occurrences:
            return {"status": "missing", "path": finding["file"], "reason": "Explicit quoted code was not found; the finding is retained."}
        if len(occurrences) > 1:
            return {"status": "ambiguous", "path": finding["file"], "reason": "Quoted code matches multiple locations or source versions.",
                    "matches": occurrences, "occurrences_at_least": 2}
        result = dict(occurrences[0], status="matched", verification="quoted_text_only", reported_line=finding.get("line"))
        if len(candidates) > 1:
            result["version_resolution"] = "unique_evidence_match"
        line = finding.get("line")
        if type(line) is int and not result["line"] <= line <= result["end_line"]:
            result["suggested_line"] = result["line"]
        return result
    if len(candidates) > 1:
        return {"status": "ambiguous", "path": finding["file"], "reason": "Multiple supplied versions share this path; no version was chosen.",
                "source_ids": [source["source_id"] for source in candidates]}
    source = candidates[0]
    line = finding.get("line")
    base = {"source_id": source["source_id"], "path": source["path"], "reported_line": line}
    if line is None:
        return dict(base, status="unverified", reason="The file is available but no line or explicit quote was reported.")
    if type(line) is not int or line < 1 or line > len(source["content"].splitlines()):
        return dict(base, status="missing", reason="The reported line is outside the supplied file.")
    return dict(base, status="matched", line=line, verification="path_and_line_only")


def analyze_report(content, findings=None, sources=()):
    """Return derived diagnostics; no finding, source, or original text is mutated."""
    result = parse_report_text(content)
    recovered = _findings(result.get("recovered_report"))
    if recovered is not None:
        result["recovered_findings"] = copy.deepcopy(recovered)
    chosen = findings if isinstance(findings, list) else recovered or []
    if isinstance(findings, list) and recovered is not None and findings != recovered:
        result["warnings"].append("Supplied findings differ from the recovered text; supplied findings were retained unchanged.")
    if findings is not None and not isinstance(findings, list):
        result["warnings"].append("Supplied findings were not an array; recovered report data was used when available.")
    records = []
    source_items = list(sources)
    for index, finding in enumerate(chosen, 1):
        records.append({"id": "finding-%04d" % index, "original": copy.deepcopy(finding),
                        "fields": _field_diagnostics(finding), "location": _location(finding, source_items),
                        "normalized_priority": normalize_priority(finding.get("priority")) if isinstance(finding, dict) else None,
                        "recovery_integrity": result["recovery_integrity"]})
    result["findings"] = records
    result["verification_scope"] = "Supplied/read text and structural checks only; diagnostic matches do not establish a real defect."
    return result


def _atomic_json(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _review_directory(review_dir):
    directory = Path(review_dir)
    if not directory.is_dir():
        raise ValueError("Review directory must already exist")
    return directory


def _record_ids(records):
    if not isinstance(records, list):
        raise ValueError("Finding records must be an array")
    ids = [item.get("id") if isinstance(item, dict) else None for item in records]
    if any(not isinstance(item, str) or not _FINDING_ID.fullmatch(item) for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("Finding records must have distinct sequential IDs")
    return ids


def save_diagnostics(review_dir, diagnostics):
    """Save a sidecar and seed pending verdicts, preserving any existing verdict file."""
    directory = _review_directory(review_dir)
    if not isinstance(diagnostics, dict):
        raise ValueError("Diagnostics must be an object")
    ids = _record_ids(diagnostics.get("findings"))
    _atomic_json(directory / "report_diagnostics.json", diagnostics)
    adjudication = directory / "adjudication.json"
    if not adjudication.exists():
        timestamp = _utc()
        _atomic_json(adjudication, {"schema_version": 1, "created_utc": timestamp, "updated_utc": timestamp,
                                   "review_status": "pending", "review_notes": [],
                                   "findings": [{"id": item, "verdict": "pending", "reason": "", "evidence": None, "history": []} for item in ids]})
    return {"diagnostics_file": str((directory / "report_diagnostics.json").resolve()),
            "adjudication_file": str(adjudication.resolve())}


def record_adjudications(review_dir, decisions, review_note=None):
    """Validate a batch completely, then atomically append main-session decisions."""
    directory = _review_directory(review_dir)
    path = directory / "adjudication.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("A valid adjudication file is required") from None
    if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record.get("schema_version") != 1:
        raise ValueError("Unsupported adjudication record")
    ids = _record_ids(record.get("findings"))
    for item in record["findings"]:
        if not isinstance(item.get("verdict"), str) or item["verdict"] not in _VERDICTS | {"pending"} or not isinstance(item.get("history"), list):
            raise ValueError("Invalid existing adjudication state")
    if not isinstance(decisions, list):
        raise ValueError("Decisions must be an array")
    checked = []
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) - {"id", "finding_id", "verdict", "reason", "evidence"}:
            raise ValueError("Invalid decision object")
        ident = decision.get("finding_id", decision.get("id"))
        if "finding_id" in decision and "id" in decision and decision["finding_id"] != decision["id"]:
            raise ValueError("Conflicting finding IDs")
        verdict, reason, evidence = decision.get("verdict"), decision.get("reason"), decision.get("evidence")
        if ident not in ids or not isinstance(verdict, str) or verdict not in _VERDICTS:
            raise ValueError("Unknown finding or unsupported verdict")
        if not isinstance(reason, str) or not reason.strip() or (evidence is not None and not isinstance(evidence, str)):
            raise ValueError("A reason and optional text evidence are required")
        checked.append({"id": ident, "verdict": verdict, "reason": reason, "evidence": evidence})
    if review_note is not None and (not isinstance(review_note, str) or not review_note.strip()):
        raise ValueError("Review note must be nonempty text")
    if not checked and review_note is None:
        raise ValueError("At least one decision or review note is required")
    updated = copy.deepcopy(record)
    by_id = {item["id"]: item for item in updated["findings"]}
    timestamp = _utc()
    for decision in checked:
        item = by_id[decision["id"]]
        history = item.setdefault("history", [])
        if not isinstance(history, list):
            raise ValueError("Invalid adjudication history")
        history.append({"recorded_utc": timestamp, **{key: decision[key] for key in ("verdict", "reason", "evidence")}})
        item.update({key: decision[key] for key in ("verdict", "reason", "evidence")})
        item["updated_utc"] = timestamp
    notes = updated.setdefault("review_notes", [])
    if not isinstance(notes, list):
        raise ValueError("Invalid review notes")
    if review_note is not None:
        notes.append({"recorded_utc": timestamp, "text": review_note})
    states = [item.get("verdict") for item in updated["findings"]]
    updated["review_status"] = "pending" if "pending" in states else "needs_check" if "needs_check" in states else "recorded"
    updated["updated_utc"] = timestamp
    _atomic_json(path, updated)
    return updated


def record_adjudication(review_dir, finding_id, verdict, reason, evidence=None):
    return record_adjudications(review_dir, [{"finding_id": finding_id, "verdict": verdict, "reason": reason, "evidence": evidence}])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-dir", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--finding")
    mode.add_argument("--decisions-file", type=Path)
    parser.add_argument("--verdict", choices=sorted(_VERDICTS))
    parser.add_argument("--reason")
    parser.add_argument("--evidence")
    parser.add_argument("--note")
    args = parser.parse_args(argv)
    if args.finding and (not args.verdict or not args.reason):
        parser.error("--finding requires --verdict and --reason")
    if not args.finding and any(value is not None for value in (args.verdict, args.reason, args.evidence)):
        parser.error("--verdict, --reason and --evidence require --finding")
    try:
        if args.finding:
            decisions = [{"finding_id": args.finding, "verdict": args.verdict, "reason": args.reason, "evidence": args.evidence}]
        elif args.decisions_file:
            payload = json.loads(args.decisions_file.read_text(encoding="utf-8-sig"))
            if isinstance(payload, dict):
                if set(payload) - {"decisions", "review_note"}:
                    raise ValueError("Unsupported decisions file fields")
                decisions = payload.get("decisions", [])
                if args.note is None:
                    args.note = payload.get("review_note")
            else:
                decisions = payload
        else:
            decisions = []
        result = record_adjudications(args.review_dir, decisions, review_note=args.note)
        print(json.dumps({"status": "recorded", "review_status": result["review_status"],
                          "adjudication_file": str((args.review_dir / "adjudication.json").resolve())}))
        return 0
    except (OSError, ValueError, TypeError):
        print(json.dumps({"status": "error", "error": "Invalid review directory, adjudication record, or decision input"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
