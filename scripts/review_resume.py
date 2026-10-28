"""Restore a recorded review into a new run without replaying provider calls.

Recorded file captures retain their source IDs and first-read views. Previously
uncaptured paths are explored using the current selected view and identified as
new reads. This is per-file provenance, not a whole-repository snapshot.
"""
from dataclasses import dataclass, field
import copy
import json
from pathlib import Path, PurePosixPath
import re

try:
    from .api_recovery import classify_failure
    from .atomic_io import atomic_write_json
    from .materials import MaterialStore
    from .repository_source import (RepositorySource, RepositoryError, _run_git,
                                    _private_name, _SKIP_DIRS, _PRIVATE_ROOT_DIRS,
                                    MAX_FILE_BYTES)
except ImportError:
    from api_recovery import classify_failure
    from atomic_io import atomic_write_json
    from materials import MaterialStore
    from repository_source import (RepositorySource, RepositoryError, _run_git,
                                   _private_name, _SKIP_DIRS, _PRIVATE_ROOT_DIRS,
                                   MAX_FILE_BYTES)


class ResumeError(ValueError):
    """A safe resume validation error; raw local diagnostics are not exposed."""


def _json(path, parent=None):
    if parent is not None and not _inside(path.resolve(), parent):
        raise ResumeError("A recorded document escapes the selected review directory")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        raise ResumeError("A required recorded review document is unavailable or invalid") from None
    if not isinstance(value, dict):
        raise ResumeError("Recorded review documents must be JSON objects")
    return value


def _inside(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _capture_path(root, entry):
    source_id = entry.get("source_id")
    if not isinstance(source_id, str) or not re.fullmatch(r"source-[0-9]{4,}", source_id):
        raise ResumeError("A recorded source ID is invalid")
    expected = "sources/" + source_id + ".txt"
    if entry.get("snapshot_file") != expected:
        raise ResumeError("A recorded source snapshot path is invalid")
    path = (root / expected).resolve()
    if not _inside(path, root / "sources"):
        raise ResumeError("A recorded source snapshot escapes its source directory")
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ResumeError("A recorded source snapshot is missing or too large")
    return path


def _validate_relative(entry, aliases):
    alias = entry.get("root")
    relative = entry.get("relative_path")
    if alias not in aliases:
        raise ResumeError("A recorded source uses an unknown root")
    try:
        relative = RepositorySource._relative(relative)
    except RepositoryError:
        raise ResumeError("A recorded source has an invalid relative path") from None
    parts = PurePosixPath(relative).parts
    if (not parts or any(p.casefold() in _SKIP_DIRS for p in parts)
            or any(p.casefold() in {".ssh", ".aws", ".bailian"} for p in parts)
            or _private_name(parts[-1].casefold())
            or (alias != "supplement" and parts[0].casefold() in _PRIVATE_ROOT_DIRS)):
        raise ResumeError("A recorded source is excluded by the source policy")
    if entry.get("view") not in {"worktree", "staged", "range", "base"}:
        raise ResumeError("A recorded source has an invalid view")
    expected = alias + "/" + relative + ("@base" if entry["view"] == "base" else "")
    if entry.get("path") != expected or entry.get("material_path", expected) != expected:
        raise ResumeError("A recorded source path does not match its identity")
    return relative


@dataclass
class ResumeState:
    directory: Path
    packet: dict
    review_messages: list
    latest_result: dict
    latest_call_dir: Path
    read_sources: list
    inline_delivered_ranges: list
    previous_checkpoint: str | None
    provenance: dict
    route: dict
    endpoint: str
    manifest: dict | None = None
    captures: list = field(default_factory=list)

    def validate_output(self, output_dir):
        """Resolve a separate destination without creating or changing files."""
        root = Path(output_dir).expanduser().resolve()
        previous = self.directory.resolve()
        if _inside(root, previous) or _inside(previous, root):
            raise ResumeError("A resumed run must use a separate output directory")
        return root

    def source_factory(self, config, redactor=None, excluded_paths=()):
        """Return the workflow's ordinary ``new_root -> source`` factory."""
        # Use the ordinary workflow's legacy normalization for optional tool
        # sizes too (None -> default, numeric strings -> int, zero -> one).
        try:
            from .workflow import _positive_limit
        except ImportError:
            from workflow import _positive_limit
        limits = {"max_chars": _positive_limit(config, "tool_max_chars", 24000),
                  "max_lines": _positive_limit(config, "tool_max_lines", 250)}
        def create(root):
            root = self.validate_output(root)
            if self.manifest is None:
                packet = copy.deepcopy(self.packet)
                if redactor is not None:
                    packet = redactor(packet)
                source = MaterialStore(packet, **limits)
                source.packet = source._packet
            else:
                source = ResumedRepositorySource(
                    self, root, redactor=redactor,
                    excluded_paths=excluded_paths, **limits,
                )
            atomic_write_json(root / "resume-state.json", self.provenance)
            return source
        return create


def _review_call_directory(root, call):
    raw_directory = call.get("directory")
    if not isinstance(raw_directory, str):
        raise ResumeError("A recorded review request directory is invalid")
    call_dir = Path(raw_directory).resolve()
    if (call_dir.parent != root / "calls"
            or not re.fullmatch(r"[0-9]{4,}-review", call_dir.name)):
        raise ResumeError("A recorded review request is outside the selected run")
    return call_dir


def _confirmed_inline_ranges(root, flow, children):
    """Reconstruct deliveries, excluding attempts rejected before generation.

    Legacy coverage.inline_delivered_ranges includes every planned attempt.
    It is retained in the original workflow, never used as delivery evidence.
    """
    inherited = flow.get("inherited_inline_delivered_ranges") or []
    if not isinstance(inherited, list) or any(not isinstance(scope, dict) for scope in inherited):
        raise ResumeError("Inherited inline delivery ranges are invalid")
    confirmed = copy.deepcopy(inherited)
    unconfirmed_entries = 0
    for call in children:
        if not isinstance(call, dict) or call.get("stage") != "review":
            continue
        ranges = call.get("inline_material_ranges") or []
        if not isinstance(ranges, list) or any(not isinstance(scope, dict) for scope in ranges):
            raise ResumeError("Recorded inline material ranges are invalid")
        if not ranges:
            continue
        call_dir = _review_call_directory(root, call)
        decision = call.get("recovery_classification") or {}
        has_progress = isinstance(decision, dict) and decision.get("has_progress") is True
        delivered = call.get("status") in {"completed", "tool_calls"} or has_progress
        if not delivered:
            result_path = call_dir / "result.json"
            raw = _json(result_path, call_dir) if result_path.exists() else {}
            reasoning_path = call_dir / "reasoning.txt"
            if reasoning_path.exists():
                if not _inside(reasoning_path.resolve(), call_dir):
                    raise ResumeError("A recorded reasoning file escapes its request directory")
                try:
                    with reasoning_path.open(encoding="utf-8") as handle:
                        reasoning = handle.read(1)
                except (OSError, UnicodeError):
                    raise ResumeError("A recorded reasoning file is unreadable") from None
            else:
                reasoning = None
            delivered = (raw.get("status") in {"completed", "tool_calls"}
                         or classify_failure(raw, reasoning_text=reasoning)["has_progress"])
        if delivered:
            for scope in ranges:
                if scope not in confirmed:
                    confirmed.append(copy.deepcopy(scope))
        else:
            unconfirmed_entries += len(ranges)
    return confirmed, unconfirmed_entries


def load_resume(review_dir):
    """Load only the caller-selected run's recorded source and review history.

    The original run is never modified. Operational errors remain in local
    metadata; the workflow receives only task history for its checkpoint call.
    """
    root = Path(review_dir).expanduser().resolve()
    if not root.is_dir():
        raise ResumeError("The prior review directory is unavailable")
    flow = _json(root / "workflow.json", root)
    if flow.get("status") not in {"error", "interrupted", "incomplete"}:
        raise ResumeError("Only an ended, unfinished review can be resumed")
    packet = _json(root / "materials.json", root)
    children = flow.get("child_calls")
    if not isinstance(children, list):
        raise ResumeError("Recorded review calls must be an array")
    candidates = [c for c in children
                  if isinstance(c, dict) and c.get("stage") == "review"]
    if not candidates:
        raise ResumeError("No recorded review request is available to resume")
    call = candidates[-1]
    call_dir = _review_call_directory(root, call)
    request = _json(call_dir / "request.json", call_dir)
    result = _json(call_dir / "result.json", call_dir)
    reasoning_path = call_dir / "reasoning.txt"
    if reasoning_path.exists() and not _inside(reasoning_path.resolve(), call_dir):
        raise ResumeError("The recorded reasoning file escapes its request directory")
    route, endpoint = request.get("route"), request.get("url")
    if (not isinstance(route, dict) or not isinstance(route.get("model"), str)
            or not route["model"].strip()):
        raise ResumeError("The recorded request has no usable fixed model route")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise ResumeError("The recorded request endpoint is missing")
    payload = request.get("payload")
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list) or not messages or any(not isinstance(m, dict) for m in messages):
        raise ResumeError("The last review request has no usable message history")
    coverage = flow.get("coverage") or {}
    if not isinstance(coverage, dict):
        raise ResumeError("Recorded material delivery coverage is invalid")
    packet_scope = packet.get("scope") or {}
    if not isinstance(packet_scope, dict):
        raise ResumeError("Recorded material scope is invalid")
    read_sources = coverage.get("read_sources") or []
    legacy_inline_ranges = coverage.get("inline_delivered_ranges") or []
    if not isinstance(read_sources, list) or not isinstance(legacy_inline_ranges, list):
        raise ResumeError("Recorded material delivery ranges are invalid")
    inline_ranges, unconfirmed_inline_entries = _confirmed_inline_ranges(root, flow, children)
    checkpoint = None
    for message in messages:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            try:
                body = json.loads(message["content"])
            except ValueError:
                continue
            if isinstance(body, dict) and isinstance(body.get("checkpoint_from_this_review"), str):
                checkpoint = body["checkpoint_from_this_review"]
    manifest = None
    captures = []
    index_path = root / "sources" / "index.json"
    if index_path.exists():
        manifest = _json(index_path, root / "sources")
        roots = manifest.get("roots")
        if (manifest.get("mode") != "repository" or manifest.get("target") not in {"worktree", "staged", "range"}
                or not isinstance(roots, list)):
            raise ResumeError("The recorded repository manifest is invalid")
        aliases = {}
        for entry in roots:
            if not isinstance(entry, dict) or not isinstance(entry.get("alias"), str) or not isinstance(entry.get("path"), str):
                raise ResumeError("A recorded root is invalid")
            alias = entry["alias"]
            if alias in aliases or not re.fullmatch(r"repo|supplement|workspace-[1-9][0-9]*", alias):
                raise ResumeError("A recorded root alias is invalid")
            aliases[alias] = entry["path"]
        if "repo" not in aliases:
            raise ResumeError("The recorded repository root is missing")
        sources = manifest.get("sources")
        if not isinstance(sources, list):
            raise ResumeError("Recorded sources must be an array")
        seen = set()
        for position, entry in enumerate(sources, 1):
            if not isinstance(entry, dict) or entry.get("source_id") != "source-%04d" % position:
                raise ResumeError("Recorded source IDs must retain their original order")
            relative = _validate_relative(entry, aliases)
            allowed_views = {manifest["target"], "base"} if entry["root"] == "repo" else {"worktree"}
            if entry["view"] not in allowed_views:
                raise ResumeError("A recorded source view does not match its root")
            key = (entry["root"], relative, entry["view"])
            if key in seen:
                raise ResumeError("The recorded manifest contains conflicting source identities")
            seen.add(key)
            path = _capture_path(root, entry)
            try:
                content = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeError):
                raise ResumeError("A recorded source is not readable UTF-8 text") from None
            if len(content) != entry.get("chars"):
                raise ResumeError("A recorded source is incomplete")
            captures.append(dict(entry, content=content))
    elif ((isinstance(flow.get("repository"), dict) and flow["repository"].get("mode") == "repository")
          or packet_scope.get("mode") == "repository"):
        raise ResumeError("The recorded repository source manifest is missing")
    provenance = {
        "source_review_directory": str(root),
        "source_review_status": flow.get("status"),
        "source_call_directory": str(call_dir),
        "restored_source_count": len(captures),
        "old_calls_replayed": 0,
        "old_tool_calls_reexecuted": 0,
        "old_usage_in_new_call_totals": False,
        "inline_delivery_reconstruction": {
            "confirmed_ranges": len(inline_ranges),
            "unconfirmed_attempt_range_entries": unconfirmed_inline_entries,
            "legacy_aggregate_range_entries": len(legacy_inline_ranges),
            "legacy_aggregate_is_delivery_evidence": False,
            "original_coverage_file": str(root / "workflow.json"),
        },
    }
    return ResumeState(root, packet, messages, result, call_dir,
                       read_sources, inline_ranges, checkpoint, provenance,
                       route, endpoint, manifest, captures)


class ResumedRepositorySource(RepositorySource):
    """Repository tools backed by prior first-read captures and new lazy reads."""

    def __init__(self, state, output_dir, *, redactor=None, excluded_paths=(), max_chars=24000, max_lines=250):
        manifest = state.manifest
        self._redactor = redactor or (lambda value: value)
        self.roots = {item["alias"]: Path(item["path"]).expanduser().resolve()
                      for item in manifest["roots"]}
        if any(not path.is_dir() for path in self.roots.values()):
            raise ResumeError("An original repository or workspace root is unavailable")
        self.repo_root = self.roots["repo"]
        top = _run_git(self.repo_root, ["rev-parse", "--show-toplevel"]).stdout.decode("utf-8").strip()
        if Path(top).resolve() != self.repo_root:
            raise ResumeError("The recorded primary root is no longer the repository root")
        self.target, self.base = manifest["target"], manifest.get("base")
        self._has_head = _run_git(self.repo_root, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0
        if self.target == "range":
            if not isinstance(self.base, str) or not self.base or self.base.startswith("-") or "\0" in self.base:
                raise ResumeError("The recorded range base is invalid")
            self._base_ref = _run_git(self.repo_root, ["merge-base", self.base, "HEAD"]).stdout.decode("ascii").strip()
        else:
            self._base_ref = "HEAD" if self._has_head else None
        self._included_paths = list((state.packet.get("scope") or {}).get("supplemental_files") or [])
        if "supplement" in self.roots:
            if self.roots["supplement"] != self.repo_root or not self._included_paths:
                raise ResumeError("The recorded supplemental file scope is invalid")
            self._included_paths = [self._relative(path) for path in self._included_paths]
        self._execution_context = list((state.packet.get("scope") or {}).get("execution_context") or [])
        self.output_dir = Path(output_dir).resolve()
        self._excluded_paths = [Path(p).expanduser().resolve() for p in excluded_paths if p is not None]
        self._excluded_paths.extend([self.output_dir, state.directory])
        (self.output_dir / "sources").mkdir(exist_ok=True)
        self._captures, self._cache, self._candidate_cache = [], {}, {}
        self._workspace_git = {}
        self._skipped = copy.deepcopy(manifest.get("skipped") or [])
        self._changes = copy.deepcopy(manifest.get("changes") or [])
        self._deleted_paths = {c["path"] for c in self._changes if str(c.get("status", "")).startswith("D")}
        self._skip_keys = {(s["root"], s["path"], s["reason"]) for s in self._skipped}
        self._manifest_batch_depth, self._manifest_dirty, self._ready = 1, False, False
        self._resume_provenance = dict(state.provenance)
        # Current credential/output exclusions also apply to already-captured
        # text; otherwise read_material could bypass read_file's path policy.
        for old in state.captures:
            try:
                self._check_path(old["root"], old["relative_path"])
            except (RepositoryError, OSError):
                raise ResumeError("A recorded source is now outside the permitted source scope") from None
        MaterialStore.__init__(self, self._redact(copy.deepcopy(state.packet)), max_chars=max_chars, max_lines=max_lines)
        self._ready = True
        try:
            for old in state.captures:
                capture = self._redact(copy.deepcopy(old))
                if capture["root"] == "supplement" and capture["relative_path"] not in self._included_paths:
                    raise ResumeError("A recorded supplemental source is outside its explicit scope")
                capture["source_origin"] = "restored_capture"
                capture["chars"] = len(capture["content"])
                capture["lines"] = len(capture["content"].splitlines())
                self._captures.append(capture)
                self._cache[(capture["root"], capture["relative_path"], capture["view"])] = capture
                (self.output_dir / capture["snapshot_file"]).write_bytes(capture["content"].encode("utf-8"))
                if not capture.get("initial_omitted") or capture.get("registered"):
                    self._register(capture)
        finally:
            self._manifest_batch_depth = 0
            self._persist()

    def _capture(self, root, relative, view=None):
        capture = super()._capture(root, relative, view)
        if "source_origin" not in capture:
            capture["source_origin"] = "new_after_resume"
            self._persist()
        return capture

    def _execute(self, name, arguments):
        result = super()._execute(name, arguments)
        if not isinstance(result, dict):
            return result
        captures = {c["source_id"]: c for c in self._captures}
        by_path = {c["material_path"]: c for c in self._captures}
        for item in [result] + list(result.get("matches") or []):
            if not isinstance(item, dict):
                continue
            capture = captures.get(item.get("source_id")) or by_path.get(item.get("path"))
            if capture:
                item.update(source_id=capture["source_id"], root=capture["root"], view=capture["view"],
                            source_origin=capture.get("source_origin", "new_after_resume"))
        return result

    def index(self):
        items = super().index()
        captures = {c["source_id"]: c for c in self._captures}
        for item in items:
            capture = captures.get(item.get("source_id"))
            if capture:
                item["source_origin"] = capture.get("source_origin", "new_after_resume")
        return items

    def repository_manifest(self):
        manifest = super().repository_manifest()
        manifest["resume"] = self._redact(self._resume_provenance)
        return manifest

    def exploration_context(self):
        context = super().exploration_context()
        context["resume_source_policy"] = (
            "restored_capture source IDs retain the prior review's recorded first-read content and view. "
            "Uncaptured paths use the current selected Git/worktree view and are marked new_after_resume. "
            "Do not assume newly read sources are the same repository version as restored captures."
        )
        return context
