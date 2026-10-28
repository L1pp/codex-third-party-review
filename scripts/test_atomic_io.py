"""Offline local file-commit recovery; no HTTP or model calls."""
import errno
import json
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import atomic_io


def permission_error(code=32):
    error = PermissionError(errno.EACCES, "synthetic file contention")
    error.winerror = code
    return error


class AtomicIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.path = self.root / "state.json"
        self.path.write_text('{"old": true}', encoding="utf-8")

    def test_transient_windows_replace_retries_serialized_sibling_then_commits(self):
        actual = atomic_io.os.replace
        calls = []
        error = permission_error()

        def replace(source, target):
            calls.append((Path(source), Path(target), Path(source).read_text(encoding="utf-8")))
            if len(calls) == 1:
                self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": True})
                raise error
            return actual(source, target)

        with patch.object(atomic_io, "_IS_WINDOWS", True), patch.object(atomic_io.os, "replace", side_effect=replace):
            atomic_io.atomic_write_json(self.path, {"新状态": "complete"})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][0].parent, self.root)
        self.assertNotEqual(calls[0][0], self.path.with_suffix(".json.tmp"))
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"新状态": "complete"})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_persistent_access_denied_raises_original_after_short_budget(self):
        error = permission_error(5)
        with patch.object(atomic_io, "_IS_WINDOWS", True), \
                patch.object(atomic_io.os, "replace", side_effect=error) as replace:
            with self.assertRaises(PermissionError) as raised:
                atomic_io.atomic_write_json(self.path, {"new": True}, replace_retry_seconds=0.12)
        self.assertIs(raised.exception, error)
        self.assertGreater(replace.call_count, 1)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": True})
        details = atomic_io.local_io_error_metadata(raised.exception)
        retained = self.root / details["retained_temp_file"]
        self.assertEqual(json.loads(retained.read_text(encoding="utf-8")), {"new": True})
        self.assertTrue(details["payload_complete"])
        self.assertEqual(details["operation"], "replace")
        events = (self.root / "local_io_errors.jsonl").read_text(encoding="utf-8")
        self.assertEqual(json.loads(events), details)
        self.assertNotIn("synthetic file contention", events)
        self.assertNotIn(str(self.root), events)

    def test_progress_zero_budget_never_waits_or_constructs_retry_policy(self):
        error = permission_error()
        with patch.object(atomic_io, "_IS_WINDOWS", True), \
                patch.object(atomic_io, "Retrying", side_effect=AssertionError("No progress wait")), \
                patch.object(atomic_io.os, "replace", side_effect=error) as replace:
            with self.assertRaises(PermissionError) as raised:
                atomic_io.atomic_write_json(self.path, {"new": True}, replace_retry_seconds=0, preserve_on_error=False)
        self.assertIs(raised.exception, error)
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertFalse((self.root / "local_io_errors.jsonl").exists())
        self.assertIsNone(atomic_io.local_io_error_metadata(raised.exception)["retained_temp_file"])

    def test_disk_error_and_unknown_permission_error_are_not_retried(self):
        for error in (OSError(errno.ENOSPC, "disk full"), PermissionError(errno.EACCES, "unknown cause")):
            with self.subTest(error=type(error).__name__), patch.object(atomic_io, "_IS_WINDOWS", True), \
                    patch.object(atomic_io.os, "replace", side_effect=error) as replace:
                with self.assertRaises(OSError) as raised:
                    atomic_io.atomic_write_json(self.path, {"new": True})
                self.assertIs(raised.exception, error)
                self.assertEqual(replace.call_count, 1)

    def test_non_windows_permission_error_is_not_retried(self):
        error = permission_error()
        with patch.object(atomic_io, "_IS_WINDOWS", False), \
                patch.object(atomic_io.os, "replace", side_effect=error) as replace:
            with self.assertRaises(PermissionError):
                atomic_io.atomic_write_json(self.path, {"new": True})
        self.assertEqual(replace.call_count, 1)

    def test_serialization_failure_never_touches_existing_destination(self):
        with patch.object(atomic_io.os, "replace", side_effect=AssertionError("No invalid JSON commit")):
            with self.assertRaises(ValueError):
                atomic_io.atomic_write_json(self.path, {"value": float("nan")}, allow_nan=False)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": True})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_unique_siblings_are_not_shared_between_writers(self):
        actual = atomic_io.os.replace
        siblings = []

        def commit(source, target):
            siblings.append(Path(source))
            return actual(source, target)

        with patch.object(atomic_io.os, "replace", side_effect=commit):
            atomic_io.atomic_write_text(self.path, "first")
            atomic_io.atomic_write_text(self.path, "second")
        self.assertNotEqual(siblings[0], siblings[1])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "second")

    def test_invalid_retry_budgets_fail_before_creating_a_temporary(self):
        for value in (True, -1, float("nan"), float("inf"), "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                atomic_io.atomic_write_json(self.path, {}, replace_retry_seconds=value)
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_four_runtime_json_writers_share_recovery_and_keep_their_formats(self):
        import review
        import harness_records as workflow
        import repository_source
        import report_diagnostics
        actual = atomic_io.os.replace
        attempts = {}

        def replace(source, target):
            name = Path(target).name
            attempts[name] = attempts.get(name, 0) + 1
            if attempts[name] == 1:
                raise permission_error()
            return actual(source, target)

        paths = [self.root / (name + ".json") for name in ("review", "workflow", "source", "diagnostics")]
        with patch.object(atomic_io, "_IS_WINDOWS", True), patch.object(atomic_io.os, "replace", side_effect=replace):
            review.write_json(paths[0], {"value": "fake-only-key"}, "fake-only-key")
            workflow._write_json(paths[1], {"path": Path("somewhere")})
            repository_source._write_json(paths[2], {"value": "源码"})
            report_diagnostics._atomic_json(paths[3], {"verdict": "pending"})
        self.assertEqual(json.loads(paths[0].read_text(encoding="utf-8")), {"value": "[REDACTED]"})
        self.assertEqual(json.loads(paths[1].read_text(encoding="utf-8")), {"path": "somewhere"})
        self.assertEqual(json.loads(paths[2].read_text(encoding="utf-8")), {"value": "源码"})
        self.assertTrue(paths[3].read_text(encoding="utf-8").endswith("\n"))
        self.assertEqual(set(attempts.values()), {2})

    @unittest.skipUnless(os.name == "nt", "Windows file sharing behavior")
    def test_windows_reader_released_between_attempts_allows_critical_commit(self):
        reader = self.path.open("r", encoding="utf-8")
        actual_retry = atomic_io.Retrying
        released = []

        def release_after_failed_attempt(delay):
            self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": True})
            released.append(delay)
            reader.close()

        def retry_policy(**kwargs):
            return actual_retry(sleep=release_after_failed_attempt, **kwargs)

        try:
            with patch.object(atomic_io, "Retrying", side_effect=retry_policy):
                atomic_io.atomic_write_json(self.path, {"released": True}, replace_retry_seconds=0.5)
        finally:
            reader.close()
        self.assertTrue(released)
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"released": True})

    @unittest.skipUnless(os.name == "nt", "Windows file sharing behavior")
    def test_windows_continuously_held_reader_keeps_old_value_and_raises(self):
        with self.path.open("r", encoding="utf-8"):
            with self.assertRaises(PermissionError) as raised:
                atomic_io.atomic_write_json(self.path, {"blocked": True}, replace_retry_seconds=0.12)
            self.assertIn(raised.exception.winerror, (5, 32, 33))
            self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"old": True})
        details = atomic_io.local_io_error_metadata(raised.exception)
        self.assertEqual(json.loads((self.root / details["retained_temp_file"]).read_text(encoding="utf-8")), {"blocked": True})

    def test_structured_io_metadata_keeps_exception_and_never_records_error_text(self):
        error = permission_error(5)
        error.args = (errno.EACCES, "a fake secret that must never appear in metadata")
        with patch.object(atomic_io, "_IS_WINDOWS", False), patch.object(atomic_io.os, "replace", side_effect=error):
            with self.assertRaises(PermissionError) as raised:
                atomic_io.atomic_write_json(self.path, {"retained": "complete"})
        self.assertIs(raised.exception, error)
        details = atomic_io.local_io_error_metadata(raised.exception)
        self.assertEqual(set(details), {"operation", "target_file", "retained_temp_file", "errno", "winerror", "payload_complete"})
        self.assertEqual(details["target_file"], "state.json")
        self.assertEqual(details["errno"], errno.EACCES)
        self.assertEqual(details["winerror"], 5)
        self.assertNotIn("fake secret", json.dumps(details))
        self.assertEqual(Path(details["retained_temp_file"]).name, details["retained_temp_file"])
        self.assertEqual(json.loads((self.root / details["retained_temp_file"]).read_text(encoding="utf-8")), {"retained": "complete"})

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_existing_regular_file_mode_is_preserved_without_changing_umask(self):
        self.path.chmod(0o640)
        with patch.object(atomic_io.os, "umask", side_effect=AssertionError("Never alter process umask")):
            atomic_io.atomic_write_json(self.path, {"updated": True})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    @unittest.skipUnless(os.name == "posix", "POSIX file permissions")
    def test_new_private_file_keeps_mode_0600(self):
        target = self.root / "new.json"
        atomic_io.atomic_write_json(target, {})
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    @unittest.skipUnless(os.name == "posix", "POSIX symlink permissions")
    def test_destination_symlink_does_not_inherit_external_target_mode(self):
        outside = self.root / "external.json"
        outside.write_text("original external", encoding="utf-8")
        outside.chmod(0o744)
        link = self.root / "destination.json"
        link.symlink_to(outside)
        atomic_io.atomic_write_json(link, {"private": True})
        self.assertFalse(link.is_symlink())
        self.assertEqual(stat.S_IMODE(link.stat().st_mode), 0o600)
        self.assertEqual(outside.read_text(encoding="utf-8"), "original external")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o744)


if __name__ == "__main__":
    unittest.main()
