"""Small SQLite call ledger for third-party code reviews.

The ledger stores only explicitly allowed metadata. It performs no network or
background work; callers should catch sqlite3 errors and continue without the
ledger if persistence is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping


DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "logs" / "calls.sqlite3"
CONNECT_TIMEOUT_SECONDS = 0.2
OUTCOMES = ("running", "success", "failure")
HISTORY_OUTCOMES = ("all", "success", "failure", "running")

_FIELDS = (
    "task",
    "requested_model",
    "reported_model",
    "started_utc",
    "ended_utc",
    "seconds",
    "first_event_seconds",
    "first_content_seconds",
    "first_content_utc",
    "status",
    "outcome",
    "parse_ok",
    "completion_warning",
    "error_type",
    "output_dir",
)
_STRING_LIMITS = {
    "task": 120,
    "requested_model": 120,
    "reported_model": 120,
    "status": 80,
}
_ERROR_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,119}$")
_COLUMNS = ", ".join(_FIELDS)
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task TEXT,
    requested_model TEXT,
    reported_model TEXT,
    started_utc TEXT NOT NULL,
    ended_utc TEXT,
    seconds REAL,
    first_event_seconds REAL,
    first_content_seconds REAL,
    first_content_utc TEXT,
    status TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('running', 'success', 'failure')),
    parse_ok INTEGER CHECK (parse_ok IS NULL OR parse_ok IN (0, 1)),
    completion_warning INTEGER NOT NULL DEFAULT 0 CHECK (completion_warning IN (0, 1)),
    error_type TEXT,
    output_dir TEXT
)
"""


def _db_path(db_path: str | Path | None) -> Path:
    if db_path is None:
        return DEFAULT_DB_PATH
    return Path(db_path).expanduser()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _utc_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp with a timezone")
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field} must be an ISO timestamp with a timezone")
    try:
        parsed = datetime.fromisoformat(
            candidate[:-1] + "+00:00" if candidate.endswith(("Z", "z")) else candidate
        )
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _one_line(value: Any, field: str, limit: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    normalized = " ".join(value.split())
    return normalized[:limit] or None


def _seconds(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a non-negative number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return normalized


def _normalize(metadata: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        raise TypeError("metadata must be a mapping")

    normalized: dict[str, Any] = {}
    for field in _FIELDS:
        if field not in metadata:
            continue
        value = metadata[field]
        if field == "output_dir":
            if value is not None and not isinstance(value, str):
                raise ValueError("output_dir must be a path string or null")
            # Preserve full paths, including repeated spaces; never shorten or open them.
            normalized[field] = value
        elif field in _STRING_LIMITS:
            normalized[field] = _one_line(value, field, _STRING_LIMITS[field])
        elif field in ("started_utc", "ended_utc", "first_content_utc"):
            normalized[field] = None if value is None else _utc_timestamp(value, field)
        elif field in ("seconds", "first_event_seconds", "first_content_seconds"):
            normalized[field] = _seconds(value, field)
        elif field == "outcome":
            if value not in OUTCOMES:
                raise ValueError(f"outcome must be one of {OUTCOMES}")
            normalized[field] = value
        elif field in ("parse_ok", "completion_warning"):
            if value is None and field == "parse_ok":
                normalized[field] = None
            elif type(value) is bool:
                normalized[field] = int(value)
            else:
                raise ValueError(f"{field} must be a boolean")
        elif field == "error_type":
            if value is None:
                normalized[field] = None
            elif isinstance(value, str) and _ERROR_CLASS.fullmatch(value):
                normalized[field] = value
            else:
                raise ValueError("error_type must contain only an exception class name")
    return normalized


def _connect_write(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(
        str(db_path), timeout=CONNECT_TIMEOUT_SECONDS, isolation_level=None
    )


def _begin_write(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(_CREATE_TABLE)
    columns = {row[1] for row in connection.execute("PRAGMA table_info(calls)")}
    if "output_dir" not in columns:
        connection.execute("ALTER TABLE calls ADD COLUMN output_dir TEXT")


def _insert(connection: sqlite3.Connection, values: Mapping[str, Any]) -> int:
    placeholders = ", ".join("?" for _ in _FIELDS)
    cursor = connection.execute(
        f"INSERT INTO calls ({_COLUMNS}) VALUES ({placeholders})",
        tuple(values.get(field) for field in _FIELDS),
    )
    return int(cursor.lastrowid)


def start_call(db_path: str | Path | None, metadata: Mapping[str, Any]) -> int:
    """Record a running call and return its row id."""
    values = _normalize(metadata)
    values["started_utc"] = values.get("started_utc") or _utc_now()
    values["status"] = values.get("status") or "running"
    values["outcome"] = "running"
    values["completion_warning"] = values.get("completion_warning", 0)

    path = _db_path(db_path)
    connection = _connect_write(path)
    try:
        _begin_write(connection)
        call_id = _insert(connection, values)
        connection.commit()
        return call_id
    except Exception:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def _outcome(values: dict[str, Any]) -> str:
    status = (values.get("status") or "").strip().lower()
    if status == "completed":
        return "success"
    if status in {"incomplete", "error", "interrupted"}:
        return "failure"
    outcome = values.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError("finish metadata must specify a valid outcome or final status")
    return outcome


def _duration(started_utc: str | None, ended_utc: str | None) -> float | None:
    if not started_utc or not ended_utc:
        return None
    start = datetime.fromisoformat(started_utc.replace("Z", "+00:00"))
    end = datetime.fromisoformat(ended_utc.replace("Z", "+00:00"))
    return max(0.0, (end - start).total_seconds())


def finish_call(
    db_path: str | Path | None, call_id: int | None, metadata: Mapping[str, Any]
) -> None:
    """Finalize a call; with no id, insert one final-only record."""
    values = _normalize(metadata)
    values["outcome"] = _outcome(values)
    if values["outcome"] != "running" and values.get("ended_utc") is None:
        values["ended_utc"] = _utc_now()
    values["completion_warning"] = values.get("completion_warning", 0)

    path = _db_path(db_path)
    connection = _connect_write(path)
    try:
        _begin_write(connection)
        if call_id is None:
            if values.get("started_utc") is None:
                values["started_utc"] = values.get("ended_utc") or _utc_now()
            if values.get("seconds") is None:
                values["seconds"] = _duration(
                    values.get("started_utc"), values.get("ended_utc")
                )
            values.setdefault("status", values["outcome"])
            _insert(connection, values)
        else:
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM calls WHERE id = ?", (call_id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"call id {call_id} does not exist")
            merged = dict(zip(_FIELDS, row))
            merged.update(values)
            if merged.get("outcome") != "running" and values.get("seconds") is None:
                merged["seconds"] = _duration(
                    merged.get("started_utc"), merged.get("ended_utc")
                )
            if not merged.get("status"):
                merged["status"] = merged["outcome"]
            assignments = ", ".join(f"{field} = ?" for field in _FIELDS)
            connection.execute(
                f"UPDATE calls SET {assignments} WHERE id = ?",
                tuple(merged[field] for field in _FIELDS) + (call_id,),
            )
        connection.commit()
    except Exception:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def _empty_history(limit: int, outcome: str, slowest: bool) -> dict[str, Any]:
    return {
        "filter": {"limit": limit, "outcome": outcome, "slowest": bool(slowest)},
        "summary": [],
        "records": [],
    }


def _record(row: tuple[Any, ...]) -> dict[str, Any]:
    values = dict(zip(("id",) + _FIELDS, row))
    if values["parse_ok"] is not None:
        values["parse_ok"] = bool(values["parse_ok"])
    values["completion_warning"] = bool(values["completion_warning"])
    return values


def history(
    db_path: str | Path | None,
    limit: int = 20,
    outcome: str = "all",
    slowest: bool = False,
) -> dict[str, Any]:
    """Read calls and grouped timing statistics without creating a database."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer from 1 to 1000")
    if outcome not in HISTORY_OUTCOMES:
        raise ValueError(f"outcome must be one of {HISTORY_OUTCOMES}")
    if type(slowest) is not bool:
        raise ValueError("slowest must be a boolean")

    path = _db_path(db_path)
    if not path.exists():
        return _empty_history(limit, outcome, slowest)

    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(
        uri, uri=True, timeout=CONNECT_TIMEOUT_SECONDS, isolation_level=None
    )
    connection.row_factory = None
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(calls)")}
        # Legacy ledgers remain readable before the next write performs the migration.
        selected_columns = ", ".join(
            "NULL AS output_dir" if field == "output_dir" and field not in columns else field
            for field in _FIELDS
        )
        where = "" if outcome == "all" else " WHERE outcome = ?"
        parameters: tuple[Any, ...] = () if outcome == "all" else (outcome,)

        summary_rows = connection.execute(
            "SELECT requested_model, outcome, COUNT(*), AVG(seconds), "
            "AVG(first_content_seconds) FROM calls"
            + where
            + " GROUP BY requested_model, outcome ORDER BY requested_model, outcome",
            parameters,
        ).fetchall()
        summary = [
            {
                "requested_model": row[0],
                "outcome": row[1],
                "count": row[2],
                "avg_seconds": row[3],
                "avg_first_content_seconds": row[4],
            }
            for row in summary_rows
        ]

        if slowest:
            ordering = (
                " ORDER BY CASE WHEN seconds IS NULL THEN 1 ELSE 0 END, "
                "seconds DESC, COALESCE(ended_utc, started_utc) DESC, id DESC"
            )
        else:
            ordering = " ORDER BY COALESCE(ended_utc, started_utc) DESC, id DESC"
        rows = connection.execute(
            f"SELECT id, {selected_columns} FROM calls{where}{ordering} LIMIT ?",
            parameters + (limit,),
        ).fetchall()
        return {
            "filter": {"limit": limit, "outcome": outcome, "slowest": slowest},
            "summary": summary,
            "records": [_record(row) for row in rows],
        }
    except sqlite3.OperationalError as exc:
        # A newly created but not yet initialized database is equivalent to an
        # empty ledger. Other I/O and lock failures remain visible to the caller.
        if "no such table: calls" in str(exc).lower():
            return _empty_history(limit, outcome, slowest)
        raise
    finally:
        connection.close()
