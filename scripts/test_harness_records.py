"""Offline artifact size, crash recovery and host-ledger regressions."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import harness_records
import ledger
from materials import MaterialStore
from review_resume import load_resume
from test_review import RecordFixture


class RuntimeRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "review"
        self.fixture = RecordFixture(self.root)

    def tearDown(self):
        for call in self.fixture.records.calls.values():
            call["audit"].close()

    @staticmethod
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    def envelope(self, scope):
        return {"messages": [{"role": "user", "content": json.dumps({
            "materials_index": {"inline_delivered_ranges": [scope]},
            "inline_materials": [{"path": scope["path"], "text": "value = 1"}],
        })}]}

    def test_running_checkpoints_are_small_and_full_calls_are_separate(self):
        f = self.fixture
        for index in range(30):
            ident = "request-%d" % index
            f.begin(ident)
            f.end("", ident=ident, status="tool_calls", finish="tool_calls")
        running = self.read(self.root / "workflow.json")
        self.assertEqual(running["child_calls_count"], 30)
        self.assertEqual(len(running["child_calls"]), 1)
        self.assertTrue(running["child_calls"][0]["directory"].endswith("0030-review"))
        self.assertNotIn("_started_monotonic", running)
        history = [json.loads(line) for line in (self.root / "call_history.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(history), 60)
        self.assertEqual(history[0]["event"], "request_start")
        self.assertEqual(history[-1]["event"], "request_end")
        self.assertEqual(len(list((self.root / "calls").glob("*/audit.json"))), 30)
        self.assertLess((self.root / "workflow.json").stat().st_size, 12000)
        result = f.finish()
        self.assertEqual(len(result["child_calls"]), 30)

    def test_overflow_index_is_saved_once_and_human_report_omits_full_trace(self):
        # Recreate records with a deliberately large prepared overflow index.
        f = self.fixture
        overflow = [{"path": "unique-overflow-marker-%04d.py" % i, "cursor": {"start_line": 1}}
                    for i in range(120)]
        f.records = harness_records.RuntimeRecords(self.root, f.config, f.route, "review", f.key,
                        f.workflow, {"remaining_materials": overflow}, "https://example.invalid/v1")
        for index in range(8):
            ident = str(index)
            f.begin(ident)
            f.end(ident=ident)
        result = f.finish()
        self.assertEqual(self.read(self.root / "material_delivery_index.json")["overflow_materials"], overflow)
        self.assertTrue(all("inline_overflow_materials" not in child for child in result["child_calls"]))
        self.assertTrue(all(Path(child["inline_overflow_materials_file"]).name == "material_delivery_index.json"
                            for child in result["child_calls"]))
        for filename in ["workflow.json", "result.json", "report.md"]:
            text = (self.root / filename).read_text(encoding="utf-8")
            self.assertNotIn("unique-overflow-marker", text)
        report = (self.root / "report.md").read_text(encoding="utf-8")
        self.assertIn("workflow.json", report)
        self.assertIn('"findings":[]', report)
        self.assertLess(len(report), 7000)

    def test_crash_checkpoint_restores_latest_call_and_earlier_confirmed_delivery(self):
        f = self.fixture
        scope = {"path": "sample.py", "start_line": 1, "end_line": 1}
        f.begin("earlier", payload=self.envelope(scope))
        f.end("", ident="earlier", status="tool_calls", finish="tool_calls")
        f.begin("latest", payload={"messages": [{"role": "user", "content": "Continue"}]})
        (self.root / "materials.json").write_text(json.dumps(f.packet), encoding="utf-8")
        session = self.root / "engine-session"
        session.mkdir()
        (session / "session-header.json").write_text('{"format":"third-party-review-dsh-session"}', encoding="utf-8")
        (session / "session.jsonl").write_text("", encoding="utf-8")
        restored = load_resume(self.root)
        self.assertTrue(restored.crash_recovery_candidate)
        self.assertEqual(restored.latest_call_dir.name, "0002-review")
        self.assertIn(scope, restored.inline_delivered_ranges)
        self.assertEqual(restored.latest_result["error_type"], "ProcessInterrupted")
        # A second read changes none of the original records.
        self.assertFalse((restored.latest_call_dir / "result.json").exists())

    def test_final_content_with_node_z_timestamp_finishes_ledger_on_python_310(self):
        f = self.fixture
        f.begin()
        final = f.end(started_utc="2026-10-29T15:33:05.472Z", first_content_seconds=1.25)
        self.assertNotIn("ledger_warning", final)
        row = ledger.history(f.config["ledger_file"])["records"][0]
        self.assertEqual(row["status"], "completed")
        self.assertEqual(row["outcome"], "success")
        self.assertEqual(row["first_content_utc"], "2026-10-29T15:33:06.722000Z")

    def test_search_snippet_is_a_delivery_observation_without_full_file_claim(self):
        class Store:
            def execute(self, name, arguments):
                return {"matches": [{"path": "repo/helper.py", "source_id": "source-0001",
                                     "line": 12, "snippet": "return value"}]}
        f = self.fixture
        f.records.record_tool(Store(), {"name": "search_code", "arguments": {"query": "return"}, "callId": "t1"})
        self.assertEqual(f.records.read_sources, [{"path": "repo/helper.py", "source_id": "source-0001",
                                                  "line": 12, "access": "search_snippet"}])
        self.assertEqual(f.records.tool_metrics["read_requests"], 0)

    def test_batch_delivery_counts_each_leaf_once_and_preserves_raw_results(self):
        f = self.fixture
        arguments = {"requests": [
            {"tool": "read_material", "arguments": {"path": "sample.py"}},
            {"tool": "read_material", "arguments": {"path": "sample.py"}},
        ]}
        result = f.records.record_tool(f.store, {"name": "batch_read", "arguments": arguments, "callId": "batch"})
        self.assertEqual(len(result["results"]), 2)
        self.assertEqual(len(f.records.tool_trace), 1)
        self.assertEqual(len(f.records.read_sources), 2)
        metrics = f.records.tool_metrics
        self.assertEqual(metrics["read_requests"], 2)
        self.assertEqual(metrics["read_chars"], 2*len("value = 1\n"))
        self.assertEqual(metrics["repeated_read_chars"], len("value = 1\n"))
        log = json.loads((self.root / "tool_results.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(log["result"], result)
        self.assertGreaterEqual(log["execution_seconds"], 0)
        self.assertEqual(len(f.records.tool_trace[0]["scope"]["requests"]), 2)

    def test_adjacent_character_pages_of_one_line_are_not_rereads(self):
        f = self.fixture
        for start, text in [(0, "abc"), (3, "def"), (2, "cd")]:
            f.records._observe_delivery("read_material", {"char_offset": start},
                                       {"path": "long.py", "line_numbers": [1], "text": text})
        self.assertEqual(f.records.tool_metrics["read_chars"], 8)
        self.assertEqual(f.records.tool_metrics["repeated_read_chars"], 2)

    def test_real_line_end_cursors_record_normalized_offset_and_overlap(self):
        f = self.fixture
        cases = [
            ("normal.py", "abc\ndef\n", 1, 4, 2, "def\n"),
            ("empty.py", "abc\n\nxyz\n", 2, 1, 3, "xyz\n"),
        ]
        for path, text, requested_line, offset, delivered_line, expected in cases:
            with self.subTest(path=path):
                store = MaterialStore({"files": [{"path": path, "content": text}]})
                before_repeated = f.records.tool_metrics["repeated_read_chars"]
                first = f.records.record_tool(store, {"name": "read_material", "callId": path + "-first",
                    "arguments": {"path": path, "start_line": requested_line,
                                  "char_offset": offset, "line_count": 1}})
                self.assertEqual(first["text"], expected)
                self.assertEqual(f.records.read_sources[-1]["start_line"], delivered_line)
                self.assertEqual(f.records.read_sources[-1]["char_offset"], 0)
                second = f.records.record_tool(store, {"name": "read_material", "callId": path + "-second",
                    "arguments": {"path": path, "start_line": delivered_line,
                                  "char_offset": 1, "line_count": 1}})
                self.assertEqual(second["text"], expected[1:])
                self.assertEqual(f.records.read_sources[-1]["char_offset"], 1)
                self.assertEqual(f.records.tool_metrics["repeated_read_chars"] - before_repeated, len(expected) - 1)
        raw = [json.loads(line) for line in (self.root / "tool_results.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(raw[0]["arguments"]["char_offset"], 4)
        self.assertEqual(raw[2]["arguments"]["char_offset"], 1)

    def test_soft_clock_facts_are_logged_without_changing_completion(self):
        f = self.fixture
        value = {"phase": "conclude", "elapsed_seconds": 1200.5, "current_local": "2026-10-29T23:00:00+08:00"}
        f.records.record_timing(value)
        self.assertEqual(self.read(self.root / "workflow.json")["review_timing"], value)
        self.assertEqual(json.loads((self.root / "review_timing.jsonl").read_text(encoding="utf-8")), value)
        f.begin(); f.end()
        result = f.finish()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["review_timing"], value)

    def test_soft_clock_sidecar_failure_is_advisory(self):
        f = self.fixture
        with patch.object(harness_records, "_append_jsonl", side_effect=PermissionError("private path")):
            f.records.record_timing({"phase": "remind", "elapsed_seconds": 600})
        self.assertIn("PermissionError", f.workflow["review_timing_warning"])
        self.assertNotIn("private path", f.workflow["review_timing_warning"])
        f.begin(); f.end()
        result = f.finish()
        self.assertEqual(result["status"], "completed")
        self.assertIn("review_timing_warning", result)

    def test_derived_call_mirror_failure_does_not_stop_original_recording(self):
        f = self.fixture
        with patch.object(harness_records, "_append_jsonl", side_effect=PermissionError("private mirror path")):
            f.begin(); f.end()
        result = f.finish()
        self.assertEqual(result["status"], "completed")
        self.assertIn("PermissionError", result["record_metadata_warning"])
        self.assertNotIn("private mirror path", result["record_metadata_warning"])
        self.assertTrue((self.root / "calls/0001-review/request.json").is_file())
        self.assertEqual(self.read(self.root / "calls/0001-review/result.json")["status"], "completed")

    def test_raw_stream_and_reasoning_are_retained_with_compact_final_report(self):
        f = self.fixture
        f.begin()
        wire = 'data: {"choices":[{"delta":{"content":"audit-marker"}}]}\n\ndata: [DONE]\n\n'
        f.wire(wire)
        f.records.response_event("response.reasoning", {"id": "r1", "text": "reasoning audit marker"})
        f.end(); f.finish()
        self.assertIn("audit-marker", (self.root / "calls/0001-review/response_stream.jsonl").read_text(encoding="utf-8"))
        self.assertEqual((self.root / "calls/0001-review/reasoning.txt").read_text(encoding="utf-8"), "reasoning audit marker")


if __name__ == "__main__":
    unittest.main()