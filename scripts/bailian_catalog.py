"""Small offline cache for the public Bailian CLI model catalog.

Only ``refresh_catalog_cache`` invokes ``bl``. Normal reviews can call
``load_catalog_cache`` and ``cache_lookup`` without a CLI, credentials, network,
or a model call. Family metadata and similarly named models are never used as
limits for another API model; ``auto`` deliberately has no catalog match.

The supported metadata command is the public, no-auth CLI 2.5.1 interface:
    bl model list --model <family> --enrich --output json
This module uses Python's standard library and argv lists, never shell=True.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


SCHEMA_VERSION = 1
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent.parent / "assets" / "bailian_catalog_cache.json"
SOURCE_TYPE = "bailian-cli-public-model-catalog"
MAX_CACHE_BYTES = 4 * 1024 * 1024
_TOKEN_FIELDS = ("contextWindow", "maxInputTokens", "maxOutputTokens")
_PARAM_FIELDS = ("name", "key", "default", "tip", "range")
_SOURCE_FIELDS = ("type", "cli_version", "requested_family", "catalog_updated_at", "retrieved_at")


class CatalogError(RuntimeError):
    """A safe diagnostic code. Raw CLI output is intentionally not included."""


def _ids(value):
    """Read only upstream-declared ids/aliases; never infer them from a name."""
    if isinstance(value, str):
        # Some catalog producers explicitly group equivalent ids with commas.
        # Spaces, hyphens, date suffixes, and model-family prefixes are not
        # aliases and must not be expanded or matched heuristically.
        values = re.split(r"[,，\n]", value)
    elif isinstance(value, list):
        values = [part for item in value if isinstance(item, str) for part in re.split(r"[,，\n]", item)]
    else:
        values = []
    return list(dict.fromkeys(item.strip() for item in values if item.strip()))


def _token_limit(value, allow_zero=False):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= (0 if allow_zero else 1) else None


def _json_copy(value):
    # Direct callers may supply objects that did not come from json.loads.
    # Do not accidentally serialize arbitrary objects as parameter metadata.
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError):
        raise CatalogError("invalid_catalog_value") from None


def _parameters(value):
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]:
            continue
        parameter = {}
        for field in _PARAM_FIELDS:
            if field not in item:
                continue
            if field in ("name", "key", "tip") and not isinstance(item[field], str):
                continue
            parameter[field] = _json_copy(item[field])
        result.append(parameter)
    return result


def _source(value):
    if not isinstance(value, dict):
        return {"type": SOURCE_TYPE}
    result = {key: value[key] for key in _SOURCE_FIELDS if isinstance(value.get(key), str)}
    result.setdefault("type", SOURCE_TYPE)
    return result


def _record(item, family, source):
    if not isinstance(item, dict):
        return None
    models = _ids(item.get("model"))
    if not models:
        return None
    api_ids = list(dict.fromkeys(models + _ids(item.get("api_model_ids"))))
    explicit_aliases = _ids(item.get("modelAlias")) + _ids(item.get("aliases"))
    aliases = [alias for alias in dict.fromkeys(explicit_aliases) if alias not in api_ids]
    result = {
        "model": models[0],
        "api_model_ids": api_ids,
        "aliases": aliases,
        "family": family,
        "predictConfig": _parameters(item.get("predictConfig")),
        "source": _source(source),
    }
    for field in ("name", "provider", "versionTag"):
        if isinstance(item.get(field), str):
            result[field] = item[field]
    for field in _TOKEN_FIELDS:
        value = _token_limit(item.get(field), allow_zero=field == "maxOutputTokens")
        if value is not None:
            result[field] = value
    return result


def normalize_catalog(payload, family=None, cli_version=None, retrieved_at=None):
    """Extract only concrete item ids, limits and parameter schema from a family.

    Returns a list of small records. The root family's limits, aliases and
    parameter schema are never inherited by child items. ``predictConfig`` is
    empty when the public CLI did not enrich that particular item (the CLI may
    enrich only trunk versions). A missing schema is not evidence of support.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise CatalogError("unsupported_catalog_shape")
    requested_family = family
    root_id = payload.get("model")
    family = root_id if isinstance(root_id, str) and root_id else family
    if not isinstance(family, str) or not family:
        raise CatalogError("missing_catalog_family")
    source = {"type": SOURCE_TYPE}
    if isinstance(requested_family, str) and requested_family:
        source["requested_family"] = requested_family
    if isinstance(cli_version, str) and cli_version:
        source["cli_version"] = cli_version
    if isinstance(payload.get("updateAt"), str):
        source["catalog_updated_at"] = payload["updateAt"]
    if isinstance(retrieved_at, str):
        source["retrieved_at"] = retrieved_at
    records = [_record(item, family, source) for item in payload["items"]]
    records = [record for record in records if record is not None]
    if not records:
        raise CatalogError("empty_catalog_family")
    return records


def load_catalog_cache(path=DEFAULT_CACHE_PATH):
    """Read a compatible cache, or return None for missing/invalid metadata.

    This function never invokes a subprocess or network and never reads the CLI
    credential/config files. Invalid cache metadata cannot block explicit route
    configuration. Only the small, documented fields are returned to callers.
    """
    try:
        path = Path(path)
        if path.stat().st_size > MAX_CACHE_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict) or type(payload.get("schema_version")) is not int or payload.get("schema_version") != SCHEMA_VERSION:
            return None
        if not isinstance(payload.get("models"), list):
            return None
        records = []
        for item in payload["models"]:
            if not isinstance(item, dict) or not isinstance(item.get("family"), str):
                return None
            record = _record(item, item["family"], item.get("source"))
            if record is None:
                return None
            records.append(record)
        result = {"schema_version": SCHEMA_VERSION, "models": records}
        for key in ("refreshed_at", "cli_version"):
            if isinstance(payload.get(key), str):
                result[key] = payload[key]
        result["source"] = _source(payload.get("source"))
        result["families"] = sorted({record["family"] for record in records})
        return result
    except (OSError, ValueError, TypeError, CatalogError):
        return None


def cache_lookup(cache, model):
    """Return a copied concrete record for one exact id or explicit alias.

    Unknown ids, family-only names, ambiguous aliases and ``auto`` return None.
    No case folding, prefix/date removal, family maximum or network fallback is
    performed. Callers must keep their explicit limits when this returns None.
    """
    if not isinstance(model, str) or not model or model.casefold() == "auto":
        return None
    if not isinstance(cache, dict) or type(cache.get("schema_version")) is not int or cache.get("schema_version") != SCHEMA_VERSION:
        return None
    models = cache.get("models")
    if not isinstance(models, list):
        return None
    matches = []
    for item in models:
        if not isinstance(item, dict):
            continue
        declared = (_ids(item.get("model")) + _ids(item.get("api_model_ids"))
                    + _ids(item.get("modelAlias")) + _ids(item.get("aliases")))
        if model in declared:
            matches.append(item)
    if len(matches) != 1:
        return None
    match = matches[0]
    family = match.get("family")
    if not isinstance(family, str) or not family:
        return None
    try:
        return _record(match, family, match.get("source"))
    except CatalogError:
        return None


def resolve_cli(executable=None):
    """Resolve an executable argv prefix and known installed package version.

    npm's Windows .ps1/.cmd shims require a shell when launched directly. For
    those shims, read only their adjacent bailian-cli package.json and invoke
    its declared ``bl`` bin with Node as an argv list. No Node bridge is added.
    Standalone .exe and normal POSIX entrypoints run directly. An unrecognized
    Windows batch shim fails safely instead of enabling shell=True.
    """
    requested = os.fspath(executable) if executable is not None else "bl"
    located = shutil.which(requested)
    if not located and Path(requested).is_file():
        located = requested
    if not located:
        raise CatalogError("cli_not_found")
    launcher = Path(located).resolve()
    if launcher.suffix.lower() not in (".ps1", ".cmd", ".bat"):
        return {"argv": [str(launcher)], "version": None}
    package_root = launcher.parent / "node_modules" / "bailian-cli"
    try:
        package = json.loads((package_root / "package.json").read_text(encoding="utf-8-sig"))
        bins = package.get("bin")
        entry = bins.get("bl") if isinstance(bins, dict) else bins
        if package.get("name") != "bailian-cli" or not isinstance(entry, str):
            raise CatalogError("unsupported_cli_launcher")
        target = (package_root / entry).resolve()
        target.relative_to(package_root.resolve())
        if not target.is_file():
            raise CatalogError("unsupported_cli_launcher")
    except (OSError, ValueError, TypeError, AttributeError):
        raise CatalogError("unsupported_cli_launcher") from None
    local_node = launcher.parent / "node.exe"
    node = str(local_node) if local_node.is_file() else shutil.which("node")
    if not node:
        raise CatalogError("node_not_found")
    version = package.get("version")
    return {"argv": [node, str(target)], "version": version if isinstance(version, str) else None}


def _write_cache(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def refresh_catalog_cache(path=DEFAULT_CACHE_PATH, families=None, *, timeout=30, executable=None,
                          cli_version=None, skill_version="2.5.1"):
    """Manually refresh public metadata, retaining the old cache on any failure.

    Returns ``{ok, cache, error}``; ``error`` is a safe code without stdout,
    stderr, credentials or CLI configuration. A multi-family refresh writes
    once, only after every requested family was fetched and normalized. Other
    previously cached families remain present. This is an optional metadata
    refresh, with no model inference, authentication or automatic retry.
    """
    if isinstance(families, str):
        families = [families]
    if not isinstance(families, (list, tuple)) or not families:
        raise ValueError("families must contain at least one explicit model family")
    if any(not isinstance(item, str) or not item.strip() or item.strip().startswith("-") for item in families):
        raise ValueError("families must contain non-empty family ids")
    families = list(dict.fromkeys(item.strip() for item in families))
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")
    existing = load_catalog_cache(path)
    failed_family = None
    try:
        launcher = resolve_cli(executable)
        version = cli_version or launcher["version"]
        timestamp = datetime.now(timezone.utc).isoformat()
        environment = dict(os.environ)
        environment.update({
            "NO_COLOR": "1", "BAILIAN_CALLER_TYPE": "agent", "BAILIAN_CALLER_NAME": "codex",
            "BAILIAN_SKILL_NAME": "bailian-cli", "BAILIAN_SKILL_VERSION": skill_version,
        })
        records = []
        for family in families:
            failed_family = family
            argv = launcher["argv"] + ["model", "list", "--model", family, "--enrich", "--output", "json"]
            process = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                     timeout=timeout, env=environment, shell=False, check=False)
            if process.returncode != 0:
                raise CatalogError("cli_failed")
            try:
                payload = json.loads(process.stdout.lstrip("\ufeff"))
            except (ValueError, TypeError, AttributeError):
                raise CatalogError("invalid_cli_json") from None
            records.extend(normalize_catalog(payload, family=family, cli_version=version, retrieved_at=timestamp))
        replaced_families = {record["family"] for record in records}
        kept = [record for record in (existing or {}).get("models", []) if record["family"] not in replaced_families]
        merged = kept + records
        identities = [(record["family"], record["model"]) for record in merged]
        if len(identities) != len(set(identities)):
            raise CatalogError("duplicate_catalog_model")
        cache = {
            "schema_version": SCHEMA_VERSION, "source": {"type": SOURCE_TYPE},
            "refreshed_at": timestamp, "families": sorted({record["family"] for record in merged}),
            "models": sorted(merged, key=lambda item: (item["family"], item["model"])),
        }
        if isinstance(version, str) and version:
            cache["cli_version"] = version
        try:
            _write_cache(path, cache)
        except (OSError, ValueError, TypeError):
            raise CatalogError("cache_write_failed") from None
        return {"ok": True, "cache": cache, "error": None}
    except subprocess.TimeoutExpired:
        error = "cli_timeout"
    except (FileNotFoundError, PermissionError):
        error = "cli_unavailable"
    except OSError:
        error = "cli_unavailable"
    except CatalogError as exc:
        error = str(exc)
    return {"ok": False, "cache": existing, "error": error, "failed_family": failed_family}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Refresh or inspect the optional public Bailian model metadata cache")
    parser.add_argument("--cache", default=str(DEFAULT_CACHE_PATH))
    actions = parser.add_subparsers(dest="action", required=True)
    refresh = actions.add_parser("refresh", help="Manually call the public, no-auth bl model list endpoint")
    refresh.add_argument("--family", action="append", required=True)
    refresh.add_argument("--timeout", type=float, default=30)
    refresh.add_argument("--executable", help="Explicit bl executable or npm shim")
    lookup = actions.add_parser("lookup", help="Offline exact-id lookup; never invokes bl")
    lookup.add_argument("--model", required=True)
    args = parser.parse_args(argv)
    if args.action == "lookup":
        record = cache_lookup(load_catalog_cache(args.cache), args.model)
        print(json.dumps({"matched": record is not None, "model": record}, ensure_ascii=False, indent=2))
        return 0
    result = refresh_catalog_cache(args.cache, args.family, timeout=args.timeout, executable=args.executable)
    # Keep routine CLI output small. The cache contains only whitelisted metadata.
    print(json.dumps({"ok": result["ok"], "error": result["error"],
                      "models": len((result["cache"] or {}).get("models", []))}, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
