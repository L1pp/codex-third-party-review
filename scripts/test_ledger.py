from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "calls.sqlite3"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_summary_keeps_success_failure_and_running_separate(self) -> None:
        success_id = ledger.start_call(
            self.db_path,
            {
                "task": "fast review",
                "requested_model": "auto",
                "started_utc": "2026-10-20T10:00:00+08:00",
            },
        )
        ledger.finish_call(
            self.db_path,
            success_id,
            {
                "status": "completed",
                "ended_utc": "2026-10-20T02:00:03Z",
                "seconds": 3,
                "first_content_seconds": 1,
                "parse_ok": False,
            },
        )
        failure_id = ledger.start_call(
            self.db_path,
            {
                "task": "slow review",
                "requested_model": "auto",
                "started_utc": "2026-10-20T02:10:00Z",
            },
        )
        ledger.finish_call(
            self.db_path,
            failure_id,
            {
                "status": "error",
                "outcome": "failure",
                "ended_utc": "2026-10-20T02:10:30Z",
                "seconds": 30,
                "first_content_seconds": 20,
                "error_type": "TimeoutError",
            },
        )
        ledger.start_call(
            self.db_path,
            {
                "task": "still running",
                "requested_model": "auto",
                "started_utc": "2026-10-20T02:20:00Z",
            },
        )

        result = ledger.history(self.db_path)
        grouped = {
            item["outcome"]: item
            for item in result["summary"]
            if item["requested_model"] == "auto"
        }
        self.assertEqual(grouped["success"]["count"], 1)
        self.assertEqual(grouped["success"]["avg_seconds"], 3)
        self.assertEqual(grouped["success"]["avg_first_content_seconds"], 1)
        self.assertEqual(grouped["failure"]["count"], 1)
        self.assertEqual(grouped["failure"]["avg_seconds"], 30)
        self.assertEqual(grouped["failure"]["avg_first_content_seconds"], 20)
        self.assertEqual(grouped["running"]["count"], 1)
        self.assertIsNone(grouped["running"]["avg_seconds"])
        self.assertIsNone(grouped["running"]["avg_first_content_seconds"])

        success = next(row for row in result["records"] if row["id"] == success_id)
        self.assertEqual(success["outcome"], "success")
        self.assertFalse(success["parse_ok"])
        self.assertEqual(success["started_utc"], "2026-10-20T02:00:00.000000Z")

    def test_only_whitelisted_metadata_is_persisted(self) -> None:
        secret_text = "private-code-and-secret-marker"
        call_id = ledger.start_call(
            self.db_path,
            {
                "task": "  review\nshort title  ",
                "requested_model": "auto",
                "packet": secret_text,
                "source_code": secret_text,
                "api_key": secret_text,
            },
        )
        ledger.finish_call(
            self.db_path,
            call_id,
            {"status": "completed", "outcome": "failure", "parse_ok": False},
        )

        row = ledger.history(self.db_path)["records"][0]
        self.assertEqual(row["task"], "review short title")
        self.assertEqual(row["outcome"], "success")
        self.assertNotIn("packet", row)
        self.assertNotIn("source_code", row)
        self.assertNotIn("api_key", row)
        self.assertNotIn(secret_text, repr(row))
        with closing(sqlite3.connect(self.db_path)) as connection:
            columns = {item[1] for item in connection.execute("PRAGMA table_info(calls)")}
        self.assertEqual(columns, {"id", *ledger._FIELDS})

    def test_final_only_record_and_slowest_filter(self) -> None:
        ledger.finish_call(
            self.db_path,
            None,
            {
                "task": "final only",
                "requested_model": "deepseek-v4.1-flash-1017",
                "started_utc": "2026-10-20T01:00:00Z",
                "ended_utc": "2026-10-20T01:00:12Z",
                "status": "completed",
            },
        )
        result = ledger.history(self.db_path, slowest=True)
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["seconds"], 12)
        self.assertEqual(result["records"][0]["outcome"], "success")

    def test_full_output_directory_preserves_unicode_spaces_and_length(self) -> None:
        directory = str(Path(self.temp_dir.name) / ("中文  空格" + "x" * 75) / ("子目录" + "y" * 75))
        self.assertGreater(len(directory), 120)
        call_id = ledger.start_call(self.db_path, {"task": "trace", "requested_model": "auto", "output_dir": directory})
        ledger.finish_call(self.db_path, call_id, {"status": "error", "error_type": "TimeoutError"})
        record = ledger.history(self.db_path)["records"][0]
        self.assertEqual(record["output_dir"], directory)
        self.assertEqual(record["outcome"], "failure")
        self.assertFalse(Path(directory).exists())

    def test_legacy_history_is_read_only_and_next_write_adds_path_column(self) -> None:
        old_schema = """CREATE TABLE calls (
            id INTEGER PRIMARY KEY, task TEXT, requested_model TEXT, reported_model TEXT,
            started_utc TEXT NOT NULL, ended_utc TEXT, seconds REAL, first_event_seconds REAL,
            first_content_seconds REAL, first_content_utc TEXT, status TEXT, outcome TEXT NOT NULL,
            parse_ok INTEGER, completion_warning INTEGER NOT NULL DEFAULT 0, error_type TEXT
        )"""
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(old_schema)
            connection.execute("INSERT INTO calls (id, task, requested_model, started_utc, ended_utc, seconds, status, outcome, parse_ok) VALUES (1, 'old review', 'auto', '2026-10-23T10:00:00Z', '2026-10-23T10:00:03Z', 3, 'completed', 'success', 1)")
            connection.commit()
        before = ledger.history(self.db_path)
        self.assertEqual(before["records"][0]["task"], "old review")
        self.assertIsNone(before["records"][0]["output_dir"])
        with closing(sqlite3.connect(self.db_path)) as connection:
            self.assertNotIn("output_dir", {row[1] for row in connection.execute("PRAGMA table_info(calls)")})
        directory = str(Path(self.temp_dir.name) / "new review")
        call_id = ledger.start_call(self.db_path, {"task": "new review", "requested_model": "auto", "output_dir": directory})
        ledger.finish_call(self.db_path, call_id, {"status": "completed", "seconds": 5})
        after = ledger.history(self.db_path)
        records = {row["id"]: row for row in after["records"]}
        self.assertEqual(records[1], before["records"][0])
        self.assertEqual(records[call_id]["output_dir"], directory)
        self.assertEqual(after["summary"][0]["count"], 2)
        self.assertEqual(after["summary"][0]["avg_seconds"], 4)

    def test_history_for_missing_database_does_not_create_it(self) -> None:
        missing = Path(self.temp_dir.name) / "missing" / "calls.sqlite3"
        result = ledger.history(missing)
        self.assertEqual(result["records"], [])
        self.assertEqual(result["summary"], [])
        self.assertFalse(missing.exists())

    def test_concurrent_short_writes(self) -> None:
        workers = 6
        barrier = threading.Barrier(workers)

        def write_call(index: int) -> int:
            barrier.wait(timeout=2)
            call_id = ledger.start_call(
                self.db_path,
                {
                    "task": f"parallel {index}",
                    "requested_model": "auto",
                    "started_utc": "2026-10-20T02:00:00Z",
                },
            )
            ledger.finish_call(
                self.db_path,
                call_id,
                {
                    "status": "completed",
                    "ended_utc": "2026-10-20T02:00:01Z",
                    "seconds": 1,
                },
            )
            return call_id

        # Verify concurrent record integrity independently of a shared CI
        # runner's scheduling/disk latency. Production keeps its short,
        # best-effort timeout; lock exhaustion is checked separately below.
        with patch.object(ledger, "CONNECT_TIMEOUT_SECONDS", 5.0), ThreadPoolExecutor(max_workers=workers) as pool:
            ids = list(pool.map(write_call, range(workers)))
        self.assertEqual(len(set(ids)), workers)
        result = ledger.history(self.db_path, limit=100)
        self.assertEqual(len(result["records"]), workers)
        self.assertEqual(result["summary"][0]["count"], workers)

    def test_locked_database_returns_an_error_without_overwriting_records(self) -> None:
        existing = ledger.start_call(self.db_path, {"task": "existing record"})
        with closing(sqlite3.connect(self.db_path, isolation_level=None)) as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            try:
                with self.assertRaisesRegex(sqlite3.OperationalError, "locked"):
                    ledger.start_call(self.db_path, {"task": "blocked record"})
            finally:
                blocker.rollback()
        records = ledger.history(self.db_path)["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], existing)
        self.assertEqual(records[0]["task"], "existing record")

    def test_time_fields_are_normalized_to_utc_and_bad_outcome_rejected(self) -> None:
        call_id = ledger.start_call(
            self.db_path,
            {
                "started_utc": datetime(2026, 10, 20, 10, tzinfo=timezone.utc).isoformat(),
                "requested_model": "auto",
            },
        )
        with self.assertRaises(ValueError):
            ledger.history(self.db_path, outcome="unknown")
        with self.assertRaises(ValueError):
            ledger.finish_call(self.db_path, call_id, {"outcome": "maybe"})


if __name__ == "__main__":
    unittest.main()
