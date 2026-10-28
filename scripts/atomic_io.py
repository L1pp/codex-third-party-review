"""Atomic local sidecar writes with bounded Windows replace contention recovery.

Only the final local os.replace is retried. This module never wraps providers,
HTTP, tools or model requests. Progress writers opt out with a zero budget.
"""
import json
import math
import os
from pathlib import Path
import tempfile
import stat

from tenacity import Retrying, retry_if_exception, stop_before_delay, wait_fixed


DEFAULT_REPLACE_RETRY_SECONDS = 1.0
_REPLACE_WAIT_SECONDS = 0.05
_IS_WINDOWS = os.name == "nt"
_WINDOWS_REPLACE_CONTENTION = {5, 32, 33}


def _retryable_replace_error(error):
    # Windows may report a reader preventing deletion as access denied (5),
    # rather than sharing/lock violation (32/33). Persistent ACL denial is
    # indistinguishable here: it still raises the original error after budget.
    return (_IS_WINDOWS and isinstance(error, PermissionError)
            and getattr(error, "winerror", None) in _WINDOWS_REPLACE_CONTENTION)


def _commit(temporary, destination, retry_seconds):
    if retry_seconds == 0:
        os.replace(temporary, destination)
        return
    Retrying(
        retry=retry_if_exception(_retryable_replace_error),
        wait=wait_fixed(_REPLACE_WAIT_SECONDS),
        stop=stop_before_delay(retry_seconds),
        reraise=True,
    )(os.replace, temporary, destination)


def _safe_errno(value):
    return value if type(value) is int else None


def local_io_error_metadata(error):
    """Return local-only structured facts, never arbitrary exception text."""
    if not isinstance(error, OSError):
        return None
    attached = getattr(error, "local_io_error", None)
    if isinstance(attached, dict):
        return {name: attached.get(name) for name in (
            "operation", "target_file", "retained_temp_file", "errno", "winerror", "payload_complete")}
    filename = getattr(error, "filename", None)
    if filename is None and error.errno is None and not isinstance(error, PermissionError):
        return None
    return {
        "operation": "local_io", "target_file": Path(filename).name if isinstance(filename, str) else None,
        "retained_temp_file": None, "errno": _safe_errno(error.errno),
        "winerror": _safe_errno(getattr(error, "winerror", None)), "payload_complete": False,
    }


def _record_io_failure(directory, details):
    # This small append-only event survives a blocked authoritative JSON file.
    # It contains no payload, absolute paths, credentials or exception message.
    try:
        with (directory / "local_io_errors.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(details, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError:
        pass  # The original exception and its structured metadata still escape.


def atomic_write_text(path, text, *, replace_retry_seconds=DEFAULT_REPLACE_RETRY_SECONDS,
                      preserve_on_error=True):
    """Commit once or retain critical payload and raise the original error.

    The retry budget bounds waiting between replace attempts, not an OS call.
    Existing POSIX regular-file modes are preserved without changing umask or
    following destination symlinks. New files keep NamedTemporaryFile's 0600.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if (isinstance(replace_retry_seconds, bool)
            or not isinstance(replace_retry_seconds, (int, float))
            or not math.isfinite(replace_retry_seconds) or replace_retry_seconds < 0):
        raise ValueError("replace_retry_seconds must be a finite non-negative number")
    if not isinstance(preserve_on_error, bool):
        raise ValueError("preserve_on_error must be a boolean")
    path = Path(path)
    temporary = None
    committed = False
    payload_complete = False
    operation = "create_temp"
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            operation = "write_temp"
            handle.write(text)
        payload_complete = True
        if os.name == "posix":
            operation = "inspect_mode"
            try:
                destination_stat = path.lstat()
            except FileNotFoundError:
                destination_stat = None
            operation = "preserve_mode"
            temporary.chmod(stat.S_IMODE(destination_stat.st_mode)
                            if destination_stat is not None and stat.S_ISREG(destination_stat.st_mode) else 0o600)
        operation = "replace"
        _commit(temporary, path, replace_retry_seconds)
        committed = True
    except OSError as error:
        error.local_io_error = {
            "operation": operation, "target_file": path.name,
            "retained_temp_file": temporary.name if preserve_on_error and temporary is not None else None,
            "errno": _safe_errno(error.errno), "winerror": _safe_errno(getattr(error, "winerror", None)),
            "payload_complete": payload_complete,
        }
        if preserve_on_error:
            _record_io_failure(path.parent, error.local_io_error)
        raise
    finally:
        if temporary is not None and (committed or not preserve_on_error):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass  # Do not hide the primary failure or undo a good commit.


def atomic_write_json(path, value, *, default=None, allow_nan=True,
                      trailing_newline=False, replace_retry_seconds=DEFAULT_REPLACE_RETRY_SECONDS,
                      preserve_on_error=True):
    text = json.dumps(value, ensure_ascii=False, indent=2, default=default, allow_nan=allow_nan)
    if trailing_newline:
        text += "\n"
    atomic_write_text(path, text, replace_retry_seconds=replace_retry_seconds,
                      preserve_on_error=preserve_on_error)
