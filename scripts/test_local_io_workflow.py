"""Local-only I/O provenance and progress warning propagation regressions."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import atomic_io
import review
import workflow


class LocalIOWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.config = {"context_window_tokens": 1000000, "context_management": {"inline_material_token_limit": 0}}
        self.packet = {"task": "review", "files": [{"path": "x.py", "content": "x=1\n"}]}

    def completed(self):
        return {"status": "completed", "finish_reason": "stop", "content": '{"findings":[],"context_gaps":[]}',
                "parse_ok": True, "parsed_findings": []}

    def flow(self, call, **kwargs):
        return workflow.run_workflow(self.config, self.packet, self.root / "review",
            review_call=call, route_selector=lambda cfg, at=None: {"model": "offline", "max_tokens": 1000}, **kwargs)

    def test_child_progress_warning_is_local_and_root_only_summarizes_it(self):
        calls = []
        marker = "LOCAL_PROGRESS_WARNING_DO_NOT_SEND_TO_MODEL"
        def call(config, packet, directory, **kwargs):
            directory.mkdir()
            calls.append(copy.deepcopy(kwargs["chat_messages"]))
            self.assertNotIn(marker, json.dumps(calls[-1]))
            self.assertNotIn("LOCAL_IO_METADATA_DO_NOT_SEND", json.dumps(calls[-1]))
            if len(calls) == 1:
                return {"status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                        "progress_warning": marker, "local_io_error": {"operation":"LOCAL_IO_METADATA_DO_NOT_SEND"}, "tool_calls": [{"id": "read-one", "type": "function", "function": {
                            "name": "read_material", "arguments": '{"path":"x.py"}'}}]}
            return self.completed()
        result = self.flow(call)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["progress_warning_count"], 1)
        self.assertIn("1 call(s)", result["progress_warning"])
        self.assertNotIn(marker, result["progress_warning"])
        self.assertEqual(result["child_calls"][0]["progress_warning"], marker)
        self.assertEqual(result["local_io_error_count"], 1)

    def test_failed_child_commit_retains_payload_and_child_metadata(self):
        error = PermissionError(13, "sensitive exception text must not be persisted")
        actual_replace = atomic_io.os.replace
        def call(config, packet, directory, **kwargs):
            directory.mkdir()
            def fail_child(source, destination):
                if Path(destination).parent == directory:
                    raise error
                return actual_replace(source, destination)
            with patch.object(atomic_io.os, "replace", side_effect=fail_child):
                atomic_io.atomic_write_json(directory / "result.json", {"partial": "retained"}, replace_retry_seconds=0)
        result = self.flow(call)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["local_io_error_count"], 1)
        child = result["child_calls"][0]
        self.assertEqual(child["error_type"], "PermissionError")
        details = child["local_io_error"]
        retained = Path(child["directory"]) / details["retained_temp_file"]
        self.assertEqual(json.loads(retained.read_text(encoding="utf-8")), {"partial": "retained"})
        local_result = (self.root / "review/result.json").read_text(encoding="utf-8")
        self.assertNotIn("sensitive exception text", local_result)
        self.assertEqual(details["target_file"], "result.json")

    def test_preparation_io_error_keeps_local_provenance_without_model_call(self):
        error = PermissionError(13, "private preparation detail")
        def source_factory(directory):
            with patch.object(atomic_io.os, "replace", side_effect=error):
                atomic_io.atomic_write_json(directory / "source-index.json", {"source": "retained"}, replace_retry_seconds=0)
        result = self.flow(lambda *a, **k: self.fail("No model call"), source_factory=source_factory)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["model_calls"], 0)
        self.assertEqual(result["local_io_error_count"], 1)
        details = result["local_io_error"]
        self.assertEqual(json.loads((self.root / "review" / details["retained_temp_file"]).read_text(encoding="utf-8")), {"source": "retained"})
        self.assertNotIn("private preparation detail", json.dumps(result))

    def test_direct_cli_summary_contains_progress_warning_and_counts(self):
        config = review.read_json(Path(__file__).resolve().parent.parent / "config.example.json")
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        packet_path = self.root / "packet.json"
        packet_path.write_text(json.dumps(self.packet), encoding="utf-8")
        result = {"status": "completed", "progress_warning": "Progress sidecar warning", "progress_warning_count": 2,
                  "local_io_error_count": 0, "model_calls": 1}
        with patch.object(review, "load_credential", return_value="fake-only-key"), \
                patch.object(workflow, "run_workflow", return_value=result), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            code = review.main(["--config", str(config_path), "--packet", str(packet_path),
                                "--output-dir", str(self.root / "cli"), "--quiet-progress"])
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["progress_warning"], result["progress_warning"])
        self.assertEqual(summary["progress_warning_count"], 2)
        self.assertEqual(summary["local_io_error_count"], 0)

    def test_direct_request_failure_reports_retained_redacted_payload_without_http(self):
        config = review.read_json(Path(__file__).resolve().parent.parent / "config.example.json")
        config["ledger_file"] = str(self.root / "calls.sqlite3")
        key = "fake-only-secret-key"
        actual_replace = atomic_io.os.replace
        def replace(source, destination):
            if Path(destination).name == "request.json":
                raise PermissionError(13, "never expose " + key)
            return actual_replace(source, destination)
        with patch.object(review, "load_credential", return_value=key), \
                patch.object(atomic_io, "_IS_WINDOWS", False), \
                patch.object(atomic_io.os, "replace", side_effect=replace), \
                patch.object(review.requests, "Session", side_effect=AssertionError("No HTTP after failed request write")):
            result = review.run_review(config, {"task": "review", "files": [{"content": key}]}, self.root / "direct")
        self.assertEqual(result["status"], "error")
        details = result["local_io_error"]
        retained = self.root / "direct" / details["retained_temp_file"]
        self.assertTrue(details["payload_complete"])
        self.assertNotIn(key, retained.read_text(encoding="utf-8"))
        self.assertIn("[REDACTED]", retained.read_text(encoding="utf-8"))
        self.assertNotIn(key, (self.root / "direct/result.json").read_text(encoding="utf-8"))
        self.assertNotIn("never expose", (self.root / "direct/local_io_errors.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(review.ledger.history(Path(config["ledger_file"]))["records"][0]["outcome"], "failure")


if __name__ == "__main__":
    unittest.main()
