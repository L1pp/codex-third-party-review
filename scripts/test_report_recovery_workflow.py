"""Offline transport-to-adjudication checks for local JSON recovery."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review
import workflow
from test_review import FakeResponse, FakeSession, FAKE_KEY, CONFIG


def issue(number):
    return {"priority": "P2", "file": "sample.py", "line": number,
            "issue": "Finding %s" % number, "basis": "Provided contract", "uncertainty": "Host checks required"}


class ReportRecoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = copy.deepcopy(CONFIG)
        self.config["ledger_file"] = str(self.root / "calls.sqlite3")
        self.output = self.root / "review"

    def run_response(self, text, status=200):
        response = FakeResponse(body={"choices": [{"message": {"content": text}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 20, "completion_tokens": 40}}, status=status)
        session = FakeSession(response)
        with patch.object(review, "load_credential", return_value=FAKE_KEY), \
                patch.object(review.requests, "Session", return_value=session):
            result = workflow.run_workflow(
                self.config, {"task": "Check return contract", "files": [{"path": "sample.py", "content": "value = 1\n"}]},
                self.output, review_call=review.run_review,
                route_selector=lambda config, at=None: review.select_route(config, "2026-10-27T12:00:00+08:00"))
        self.assertEqual(len(session.calls), 1, "Local recovery must not request another model response")
        self.assertEqual(result["model_calls"], 1)
        self.assertEqual(result["content"], text.replace(FAKE_KEY, "[REDACTED]"))
        return result

    def test_bad_middle_element_preserves_all_ten_findings_and_pending_verdicts(self):
        expected = {"findings": [issue(n) for n in range(1, 11)], "context_gaps": ["Missing caller"]}
        raw = json.dumps(expected, separators=(",", ":"), ensure_ascii=False).replace('"line":5,', '"line":5",', 1)
        result = self.run_response(raw)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["parse_ok"])
        self.assertFalse(result["raw_parse_ok"])
        self.assertEqual(result["parsed_report"], expected)
        self.assertEqual(result["report_summary"]["retained_findings"], 10)
        self.assertEqual(result["report_summary"]["request_status"], "completed")
        self.assertEqual(result["report_summary"]["adjudication_status_at_creation"], "pending")
        derived = review.read_json(self.output / "report_diagnostics.json")
        self.assertEqual(len(derived["findings"]), 10)
        self.assertTrue(derived["repair"]["applied"])
        self.assertIsInstance(derived["repair_log"], list)
        self.assertIsNotNone(derived["raw_parse_error"]["offset"])
        verdicts = review.read_json(self.output / "adjudication.json")
        self.assertEqual(len(verdicts["findings"]), 10)
        self.assertTrue(all(row["verdict"] == "pending" for row in verdicts["findings"]))
        child = review.read_json(self.output / "calls" / "0001-review" / "result.json")
        self.assertEqual(child["content"], raw)
        self.assertTrue(child["report_recovery"]["repair"]["applied"])
        self.assertIsNotNone(child["report_recovery"]["raw_parse_error"]["offset"])

    def test_truncated_tail_candidates_stay_incomplete_despite_normal_transport_stop(self):
        raw = '{"findings":[' + json.dumps(issue(1)) + ',{"priority":"P1","file":"cut'
        result = self.run_response(raw)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["parse_ok"])
        self.assertEqual(result["parsed_findings"][0], issue(1))
        self.assertTrue(result["report_summary"]["possible_loss"])
        self.assertEqual(result["report_summary"]["request_status"], "completed")
        self.assertGreaterEqual(result["report_summary"]["retained_findings"], 1)

    def test_ambiguous_reports_remain_visible_without_arbitrary_selection(self):
        raw = json.dumps({"findings": [issue(1)]}) + "\n" + json.dumps({"findings": [issue(2)]})
        result = self.run_response(raw)
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["parse_ok"])
        self.assertEqual(result["report_summary"]["parse_status"], "ambiguous")
        derived = review.read_json(self.output / "report_diagnostics.json")
        self.assertEqual(len(derived["candidates"]), 2)
        self.assertFalse(derived["findings"])
        child = review.read_json(self.output / "calls" / "0001-review" / "result.json")
        self.assertEqual(child["report_recovery"]["candidates"], derived["candidates"])

    def test_unexpected_parser_error_does_not_skip_raw_call_persistence(self):
        raw = '{"findings":[]}'
        with patch.object(review, "parse_report_text", side_effect=AttributeError("synthetic parser API error")):
            self.run_response(raw)
        folder = self.output / "calls" / "0001-review"
        child = review.read_json(folder / "result.json")
        self.assertEqual(child["content"], raw)
        self.assertFalse(child["parse_ok"])
        self.assertEqual(child["report_recovery"]["error"], "AttributeError")
        self.assertTrue((folder / "report.md").is_file())
        self.assertEqual(review.read_json(folder / "progress.json")["state"], "completed")
        record = review.ledger.history(Path(self.config["ledger_file"]))["records"][0]
        self.assertIsNotNone(record["ended_utc"])

    def test_empty_broken_report_is_never_clean(self):
        result = self.run_response('{"findings":[],"context_gaps":[')
        self.assertEqual(result["status"], "incomplete")
        self.assertFalse(result["parse_ok"])
        self.assertTrue(result["report_summary"]["possible_loss"])

    def test_repair_diagnostics_do_not_persist_the_active_key(self):
        item = issue(1)
        item["issue"] += " " + FAKE_KEY
        raw = json.dumps({"findings": [item]}, separators=(",", ":")).replace('"line":1,', '"line":1",', 1)
        result = self.run_response(raw)
        self.assertEqual(result["status"], "completed")
        for path in self.output.rglob("*"):
            if path.is_file():
                self.assertNotIn(FAKE_KEY, path.read_text(encoding="utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
