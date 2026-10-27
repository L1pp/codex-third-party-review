"""Public synthetic regressions for report recovery and host integrity policy."""
import copy
import json
import unittest
from unittest.mock import patch

import report_parsing as parsing
import report_diagnostics as diagnostics


def finding(number):
    return {"priority": "P2", "file": "src/example_%02d.py" % number,
            "line": number, "issue": "Example finding %d" % number,
            "basis": "An example contract", "uncertainty": "Host must verify"}


class ReportParsingTests(unittest.TestCase):
    def test_middle_numeric_quote_recovers_all_ten_findings_without_value_changes(self):
        report = {"findings": [finding(i) for i in range(1, 11)], "context_gaps": ["Example gap"]}
        original = json.dumps(report)
        malformed = original.replace('"line": 5,', '"line": 5",', 1)
        result = parsing.parse_report_text(malformed)
        self.assertEqual(result["parse_status"], "recovered")
        self.assertFalse(result["possible_loss"])
        self.assertEqual(result["recovery_integrity"], "unverified")
        self.assertEqual(result["recovered_report"], report)
        self.assertEqual(result["counts"]["recovered_findings"], 10)
        self.assertEqual(result["repair"]["engine"], "json-repair")
        self.assertTrue(result["repair"]["applied"])
        derived = diagnostics.analyze_report(malformed)
        self.assertEqual([row["original"] for row in derived["findings"]], report["findings"])
        self.assertEqual([row["id"] for row in derived["findings"]], ["finding-%04d" % i for i in range(1, 11)])
        self.assertEqual(malformed, original.replace('"line": 5,', '"line": 5",', 1))

    def test_missing_final_array_closer_recovers_eleven_and_keeps_partial_status(self):
        report = {"findings": [finding(i) for i in range(1, 12)], "context_gaps": ["Example gap"]}
        source = json.dumps(report)
        malformed = source[:-2] + source[-1:]
        result = parsing.parse_report_text(malformed)
        self.assertEqual(result["recovered_report"], report)
        self.assertEqual(result["counts"]["recovered_findings"], 11)
        self.assertEqual(result["parse_status"], "partial")
        self.assertTrue(result["possible_loss"])
        self.assertEqual(result["recovery_integrity"], "unverified")

    def test_valid_data_does_not_call_repair_or_change_business_values(self):
        report = {"findings": [finding(1), {"line": True, "priority": "custom"}], "unrelated": None}
        source = json.dumps(report)
        with patch.object(parsing, "_JSONParser", side_effect=AssertionError("valid JSON must not be repaired")):
            result = parsing.parse_report_text(source)
        self.assertEqual(result["recovered_report"], report)
        self.assertEqual(result["repair_log"], [])
        self.assertFalse(result["repair"]["applied"])
        self.assertEqual(result["recovery_integrity"], "original_json")

    def test_two_identical_independent_reports_are_ambiguous_not_library_deduplicated(self):
        source = json.dumps({"findings": [finding(1)]})
        result = parsing.parse_report_text(source + "\n" + source)
        self.assertEqual(result["parse_status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertNotIn("recovered_report", result)

    def test_malformed_first_report_does_not_hide_before_a_second_report(self):
        first = json.dumps({"findings": [finding(1)]}, separators=(",", ":"))
        second = json.dumps({"findings": [finding(2)]}, separators=(",", ":"))
        bad_first = first.replace('"line":1,', '"line":1",')
        bad_second = second.replace('"line":2,', '"line":2",')
        for left, right in [(bad_first, second), (first, bad_second), (bad_first, bad_second)]:
            with self.subTest(left=left, right=right):
                result = parsing.parse_report_text(left + "\n" + right)
                self.assertEqual(result["parse_status"], "ambiguous")
                self.assertTrue(result["possible_loss"])
                self.assertEqual(result["candidates"], [{"findings": [finding(1)]}, {"findings": [finding(2)]}])
                self.assertNotIn("recovered_report", result)

    def test_complete_report_and_partial_second_expose_both(self):
        first = {"findings": [finding(1)]}
        source = json.dumps(first) + '\n{"findings":[{"issue":"unfinished'
        result = parsing.parse_report_text(source)
        self.assertEqual(result["parse_status"], "partial")
        self.assertEqual(result["recovered_report"], first)
        self.assertEqual(result["unverified_candidates"][0]["findings"][0]["issue"], "unfinished")
        self.assertEqual(result["counts"]["candidate_findings"], 2)
        self.assertTrue(result["possible_loss"])

    def test_truncated_fields_remain_unverified_instead_of_disappearing(self):
        source = '{"findings":[' + json.dumps(finding(1)) + ',{"issue":"unfinished'
        result = diagnostics.analyze_report(source)
        self.assertEqual(result["parse_status"], "partial")
        self.assertEqual(result["findings"][0]["original"], finding(1))
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["findings"][1]["recovery_integrity"], "unverified")
        self.assertTrue(result["possible_loss"])

    def test_unavailable_dependency_preserves_honest_failure(self):
        with patch.object(parsing, "_JSONParser", None):
            result = parsing.parse_report_text('{"findings":[')
        self.assertEqual(result["parse_status"], "invalid")
        self.assertTrue(result["possible_loss"])
        self.assertNotIn("recovered_report", result)
        self.assertEqual(result["repair"]["error"], "dependency_unavailable")

    def test_repair_errors_are_data_not_uncaught_finalizer_exceptions(self):
        for exception in (ValueError, TypeError, IndexError, RecursionError):
            with self.subTest(exception=exception.__name__), patch.object(parsing, "_library_values", side_effect=exception("test")):
                result = parsing.parse_report_text('{"findings":[')
            self.assertNotIn("recovered_report", result)
            self.assertTrue(result["possible_loss"])
            self.assertEqual(result["repair"]["error"], exception.__name__)

    def test_later_library_failure_preserves_yielded_findings_and_repair_logs(self):
        report = {"findings": [finding(1)]}
        log = {"text": "Example repair action", "context": "example"}
        def partial_values(_):
            yield report, json.dumps(report), [log]
            raise AttributeError("simulated dependency incompatibility")
        with patch.object(parsing, "_library_values", partial_values):
            result = parsing.parse_report_text("Intro\n" + json.dumps(report))
        self.assertEqual(result["parse_status"], "partial")
        self.assertTrue(result["possible_loss"])
        self.assertEqual(result["recovered_report"], report)
        self.assertEqual(result["counts"]["recovered_findings"], 1)
        self.assertEqual(result["repair"]["error"], "AttributeError")
        self.assertEqual(result["repair_log"], [dict(log, candidate_index=0)])

    def test_explicit_empty_report_with_surrounding_explanation_is_accepted(self):
        for source in ["Intro\n```json\n[]\n```\nEnd", "Intro\n[]\nEnd", "Intro ```json []``` End"]:
            with self.subTest(source=source):
                result = parsing.parse_report_text(source)
                self.assertEqual(result["parse_status"], "recovered")
                self.assertFalse(result["possible_loss"])
                self.assertEqual(result["recovered_report"], [])

    def test_broken_empty_report_never_becomes_clean(self):
        for source in ['{"findings":[], "context_gaps":[', '{"findings":[', 'prefix [] "unfinished']:
            with self.subTest(source=source):
                result = parsing.parse_report_text(source)
                if "recovered_report" in result:
                    self.assertTrue(result["possible_loss"])
                self.assertNotEqual(result["parse_status"], "valid")


if __name__ == "__main__":
    unittest.main()
