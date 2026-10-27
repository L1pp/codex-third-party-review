"""Offline checks for report diagnostics and main-session decision recording."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import report_diagnostics as diagnostics


def finding(**updates):
    value = {"priority": "P2", "file": "src/math.py", "line": 2,
             "issue": "Potential incorrect result", "basis": "Call contract", "uncertainty": "Needs host verification"}
    value.update(updates)
    return value


class ReportDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.sources = [{"path": "repo/src/math.py", "aliases": ["src/math.py"],
                         "content": "def calculate(value):\n    return value + 1\n", "source_id": "source-0001"}]

    def analyze(self, items, sources=None):
        return diagnostics.analyze_report(json.dumps({"findings": items}), sources=self.sources if sources is None else sources)

    def test_valid_report_preserves_input_and_only_checks_path_and_line(self):
        items = [finding()]
        before = copy.deepcopy(items), copy.deepcopy(self.sources)
        result = diagnostics.analyze_report(json.dumps({"findings": items}), items, self.sources)
        self.assertEqual(result["parse_status"], "valid")
        self.assertFalse(result["possible_loss"])
        self.assertEqual(result["findings"][0]["id"], "finding-0001")
        self.assertEqual(result["findings"][0]["location"]["verification"], "path_and_line_only")
        self.assertEqual(result["findings"][0]["original"], items[0])
        result["findings"][0]["original"]["issue"] = "changed derived copy"
        self.assertEqual((items, self.sources), before)

    def test_empty_findings_are_valid(self):
        for content in ['{"findings":[]}', '[]']:
            with self.subTest(content=content):
                result = diagnostics.analyze_report(content)
                self.assertEqual(result["parse_status"], "valid")
                self.assertEqual(result["findings"], [])
                self.assertFalse(result["possible_loss"])

    def test_invalid_fields_and_nonobject_elements_are_retained(self):
        malformed = {"priority": "critical", "file": 123, "line": True, "issue": "", "basis": [], "uncertainty": None}
        result = self.analyze([malformed, "not an object"])
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["findings"][0]["original"], malformed)
        self.assertFalse(result["findings"][0]["fields"]["valid"])
        self.assertEqual(result["findings"][1]["original"], "not an object")
        self.assertEqual(result["findings"][1]["fields"]["issues"][0]["code"], "not_object")

    def test_missing_fields_are_diagnostics_not_a_filter(self):
        result = self.analyze([{}])
        self.assertEqual(len(result["findings"]), 1)
        self.assertEqual({item["field"] for item in result["findings"][0]["fields"]["issues"]},
                         {"priority", "file", "line", "issue", "basis", "uncertainty"})
        self.assertEqual(result["findings"][0]["location"]["status"], "unverified")

    def test_unknown_path_and_invalid_line_are_not_removed(self):
        result = self.analyze([finding(file="missing.py"), finding(line=99), finding(line=None)])
        self.assertEqual([record["location"]["status"] for record in result["findings"]],
                         ["missing", "missing", "unverified"])
        self.assertEqual(len(result["findings"]), 3)

    def test_basis_is_not_assumed_to_be_a_code_quote(self):
        result = self.analyze([finding(basis="This explanation is absent from the source")])
        self.assertEqual(result["findings"][0]["location"]["status"], "matched")
        self.assertEqual(result["findings"][0]["location"]["verification"], "path_and_line_only")

    def test_unique_explicit_quote_suggests_line_without_rewriting_original(self):
        item = finding(line=1, evidence="return value + 1")
        result = self.analyze([item])
        record = result["findings"][0]
        self.assertEqual(record["location"]["status"], "matched")
        self.assertEqual(record["location"]["suggested_line"], 2)
        self.assertEqual(record["location"]["reported_line"], 1)
        self.assertEqual(record["original"]["line"], 1)
        self.assertEqual(item["line"], 1)

    def test_quote_can_supply_unknown_line(self):
        result = self.analyze([finding(line=None, quote="return value + 1")])
        self.assertEqual(result["findings"][0]["location"]["line"], 2)
        self.assertEqual(result["findings"][0]["location"]["status"], "matched")

    def test_missing_quote_does_not_drop_finding(self):
        result = self.analyze([finding(code="not in source")])
        self.assertEqual(result["findings"][0]["location"]["status"], "missing")
        self.assertEqual(len(result["findings"]), 1)

    def test_duplicate_quote_is_ambiguous(self):
        sources = [{"path": "a.py", "content": "value = 1\nvalue = 1\n", "source_id": "source-0001"}]
        result = self.analyze([finding(file="a.py", line=1, evidence="value = 1")], sources)
        location = result["findings"][0]["location"]
        self.assertEqual(location["status"], "ambiguous")
        self.assertEqual(location["occurrences_at_least"], 2)

    def test_multiple_versions_are_not_silently_selected(self):
        sources = [dict(self.sources[0]), dict(self.sources[0], source_id="source-0002", content="value = 3\n")]
        result = self.analyze([finding()], sources)
        self.assertEqual(result["findings"][0]["location"]["status"], "ambiguous")
        self.assertEqual(len(result["findings"][0]["location"]["source_ids"]), 2)

    def test_quote_can_explicitly_disambiguate_source_versions(self):
        sources = [dict(self.sources[0]), dict(self.sources[0], source_id="source-0002", content="value = 3\n")]
        result = self.analyze([finding(evidence="return value + 1")], sources)
        location = result["findings"][0]["location"]
        self.assertEqual(location["status"], "matched")
        self.assertEqual(location["source_id"], "source-0001")
        self.assertEqual(location["version_resolution"], "unique_evidence_match")

    def test_explicit_source_id_is_honored(self):
        sources = [dict(self.sources[0]), dict(self.sources[0], source_id="source-0002")]
        result = self.analyze([finding(source_id="source-0002"), finding(source_id="absent")], sources)
        self.assertEqual(result["findings"][0]["location"]["source_id"], "source-0002")
        self.assertEqual(result["findings"][1]["location"]["status"], "missing")

    def test_path_aliases_allow_slashes_without_resolving_files(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read files")), \
                patch("builtins.open", side_effect=AssertionError("must not open repository paths")):
            result = self.analyze([finding(file=".\\src\\math.py")])
        self.assertEqual(result["findings"][0]["location"]["status"], "matched")

    def test_git_diff_prefix_fallback_matches_sources_without_rewriting_finding(self):
        for prefix in ("a/", "b/"):
            with self.subTest(prefix=prefix):
                item = finding(file=prefix + "src/math.py")
                result = self.analyze([item])
                record = result["findings"][0]
                self.assertEqual(record["location"]["status"], "matched")
                self.assertEqual(record["location"]["path"], "repo/src/math.py")
                self.assertEqual(record["location"]["path_normalization"], {
                    "kind": "git_diff_prefix_removed", "reported_path": item["file"], "normalized_path": "src/math.py"})
                self.assertEqual(record["original"]["file"], prefix + "src/math.py")
                self.assertEqual(item["file"], prefix + "src/math.py")

    def test_real_a_or_b_directory_exact_match_wins_over_diff_prefix_guess(self):
        for prefix in ("a/", "b/"):
            with self.subTest(prefix=prefix):
                sources = self.sources + [{"path": prefix + "src/math.py", "content": "real_directory = True\n", "source_id": "real-directory"}]
                result = self.analyze([finding(file=prefix + "src/math.py", line=1)], sources)
                location = result["findings"][0]["location"]
                self.assertEqual(location["status"], "matched")
                self.assertEqual(location["source_id"], "real-directory")
                self.assertNotIn("path_normalization", location)

    def test_exact_alias_also_wins_before_diff_prefix_fallback(self):
        sources = self.sources + [{"path": "workspace-1/actual.py", "aliases": ["a/src/math.py"],
                                   "content": "alias_target = True\n", "source_id": "exact-alias"}]
        location = self.analyze([finding(file="a/src/math.py", line=1)], sources)["findings"][0]["location"]
        self.assertEqual(location["source_id"], "exact-alias")
        self.assertNotIn("path_normalization", location)

    def test_diff_prefix_fallback_preserves_multiple_version_ambiguity(self):
        sources = self.sources + [dict(self.sources[0], source_id="source-0002", content="value = 3\n")]
        location = self.analyze([finding(file="b/src/math.py")], sources)["findings"][0]["location"]
        self.assertEqual(location["status"], "ambiguous")
        self.assertEqual(location["source_ids"], ["source-0001", "source-0002"])
        self.assertEqual(location["path_normalization"]["normalized_path"], "src/math.py")

    def test_diff_prefix_fallback_preserves_multiple_root_ambiguity(self):
        sources = self.sources + [{"path": "workspace-1/src/math.py", "aliases": ["src/math.py"],
                                   "content": "shared_name = True\n", "source_id": "workspace-source"}]
        location = self.analyze([finding(file="a/src/math.py")], sources)["findings"][0]["location"]
        self.assertEqual(location["status"], "ambiguous")
        self.assertEqual(location["source_ids"], ["source-0001", "workspace-source"])

    def test_diff_prefix_fallback_does_not_invent_a_missing_location(self):
        item = finding(file="a/absent.py")
        result = self.analyze([item])
        location = result["findings"][0]["location"]
        self.assertEqual(location["status"], "missing")
        self.assertEqual(location["path"], "a/absent.py")
        self.assertNotIn("path_normalization", location)
        self.assertEqual(result["findings"][0]["original"], item)

    def test_fences_and_prose_recover_one_complete_report(self):
        for template in ["```json\n%s\n```", "Findings follow:\n%s\nEnd.", "Answer: %s"]:
            with self.subTest(template=template):
                result = diagnostics.analyze_report(template % json.dumps({"findings": [finding()]}), sources=self.sources)
                self.assertEqual(result["parse_status"], "recovered")
                self.assertFalse(result["possible_loss"])
                self.assertEqual(result["recovered_findings"], [finding()])

    def test_unmatched_prose_quote_does_not_hide_a_line_start_report(self):
        content = 'The reviewer wrote "summary follows:\n' + json.dumps({"findings": [finding()]})
        result = diagnostics.analyze_report(content)
        self.assertEqual(result["parse_status"], "recovered")
        self.assertFalse(result["possible_loss"])
        self.assertEqual(result["recovered_findings"], [finding()])

    def test_conflicting_supplied_findings_are_retained_with_a_warning(self):
        supplied = [finding(file="provided.py")]
        result = diagnostics.analyze_report(json.dumps({"findings": [finding()]}), findings=supplied)
        self.assertEqual(result["findings"][0]["original"], supplied[0])
        self.assertTrue(any("differ" in item for item in result["warnings"]))

    def test_trailing_commas_recover_without_changing_quoted_punctuation(self):
        content = '{"findings":[{"priority":"P2","file":"a.py","line":1,"issue":"literal ,] stays","basis":"x","uncertainty":"",},],}'
        result = diagnostics.analyze_report(content)
        self.assertEqual(result["parse_status"], "recovered")
        self.assertFalse(result["possible_loss"])
        self.assertEqual(result["recovered_findings"][0]["issue"], "literal ,] stays")

    def test_truncated_tail_keeps_complete_findings_and_marks_loss(self):
        content = '{"findings":[' + json.dumps(finding()) + ',{"priority":"P1","file":"other'
        result = diagnostics.analyze_report(content)
        self.assertEqual(result["parse_status"], "partial")
        self.assertTrue(result["possible_loss"])
        self.assertEqual(result["recovered_findings"][0], finding())
        self.assertEqual(len(result["findings"]), 2)
        self.assertEqual(result["findings"][1]["recovery_integrity"], "unverified")
        self.assertFalse(result["findings"][1]["fields"]["valid"])

    def test_unclosed_wrapper_is_not_complete_even_when_array_closed(self):
        content = '{"findings":[' + json.dumps(finding()) + ']'
        result = diagnostics.analyze_report(content)
        self.assertEqual(result["parse_status"], "partial")
        self.assertTrue(result["possible_loss"])

    def test_multiple_reports_are_ambiguous_and_all_candidates_retained(self):
        first = {"findings": [finding()]}
        second = {"findings": [finding(file="other.py")]}
        result = diagnostics.analyze_report(json.dumps(first) + "\n" + json.dumps(second))
        self.assertEqual(result["parse_status"], "ambiguous")
        self.assertTrue(result["possible_loss"])
        self.assertEqual(result["candidates"], [first, second])
        self.assertNotIn("recovered_report", result)
        self.assertEqual(result["findings"], [])

    def test_unreadable_second_report_cannot_hide_behind_a_complete_first_report(self):
        for first in [{"findings": []}, {"findings": [finding()]}]:
            for tail in ['{"findings":[', '{"findings":[{"priority":"P1"', '{"findings":']:
                with self.subTest(first=first, tail=tail):
                    result = diagnostics.analyze_report(json.dumps(first) + "\n" + tail)
                    self.assertEqual(result["parse_status"], "partial")
                    self.assertTrue(result["possible_loss"])
                    self.assertEqual(result["recovered_findings"], first["findings"])
                    self.assertEqual([item["original"] for item in result["findings"]], first["findings"])
                    self.assertEqual(result["unreadable_candidates"], [tail])

    def test_scalar_arrays_in_surrounding_prose_are_not_reports(self):
        report = {"findings": [finding()]}
        for text in ['Reviewed constants [1, 2].\n' + json.dumps(report),
                     json.dumps(report) + '\nRelated labels ["a", "b"].']:
            with self.subTest(text=text):
                result = diagnostics.analyze_report(text)
                self.assertEqual(result["parse_status"], "recovered")
                self.assertFalse(result["possible_loss"])
                self.assertEqual(result["recovered_findings"], report["findings"])

    def test_nonobject_elements_in_explicit_reports_are_still_diagnosed(self):
        for text in ['[1, "not an object"]', 'Report: {"findings":[1,"not an object"]}']:
            with self.subTest(text=text):
                result = diagnostics.analyze_report(text)
                self.assertFalse(result["possible_loss"])
                self.assertEqual([item["original"] for item in result["findings"]], [1, "not an object"])
                self.assertTrue(all(not item["fields"]["valid"] for item in result["findings"]))

    def test_empty_array_report_in_a_fence_remains_valid(self):
        result = diagnostics.analyze_report('```json\n[]\n```')
        self.assertEqual(result["parse_status"], "recovered")
        self.assertFalse(result["possible_loss"])
        self.assertEqual(result["recovered_findings"], [])

    def test_nested_report_shaped_data_is_not_selected(self):
        result = diagnostics.analyze_report('{"metadata":{"findings":[]}}')
        self.assertEqual(result["parse_status"], "invalid")
        self.assertEqual(result["findings"], [])

    def test_broken_empty_array_cannot_become_a_clean_review(self):
        result = diagnostics.analyze_report('prefix {"findings":[], "context_gaps": [')
        self.assertTrue(result["possible_loss"])
        self.assertNotEqual(result["parse_status"], "valid")
        self.assertNotIn("recovered_report", result)

    def test_supplied_findings_are_preserved_even_when_text_cannot_be_recovered(self):
        items = [finding()]
        result = diagnostics.analyze_report("not JSON", findings=items, sources=self.sources)
        self.assertEqual(result["parse_status"], "invalid")
        self.assertEqual(result["findings"][0]["original"], items[0])


class AdjudicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = {"report.md": "RAW ORIGINAL REPORT", "result.json": '{"content":"original"}',
                    "response_stream.jsonl": '{"data":"original"}\n'}
        for name, content in self.raw.items():
            (self.root / name).write_text(content, encoding="utf-8")
        self.derived = diagnostics.analyze_report(json.dumps({"findings": [finding(), finding(file="other.py")]}))
        diagnostics.save_diagnostics(self.root, self.derived)

    def record(self):
        return json.loads((self.root / "adjudication.json").read_text(encoding="utf-8"))

    def test_initial_records_are_pending_and_do_not_touch_original_files(self):
        self.assertEqual(self.record()["review_status"], "pending")
        self.assertEqual([r["verdict"] for r in self.record()["findings"]], ["pending", "pending"])
        for name, content in self.raw.items():
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), content)

    def test_decisions_keep_update_history(self):
        diagnostics.record_adjudication(self.root, "finding-0001", "confirmed", "Confirmed by host test", "src/math.py:2")
        diagnostics.record_adjudication(self.root, "finding-0001", "needs_check", "Additional caller needed")
        diagnostics.record_adjudication(self.root, "finding-0001", "dismissed", "Caller contract resolves concern")
        item = self.record()["findings"][0]
        self.assertEqual(item["verdict"], "dismissed")
        self.assertEqual([h["verdict"] for h in item["history"]], ["confirmed", "needs_check", "dismissed"])
        self.assertEqual(item["history"][0]["evidence"], "src/math.py:2")
        for name, content in self.raw.items():
            self.assertEqual((self.root / name).read_text(encoding="utf-8"), content)

    def test_existing_decisions_survive_diagnostics_save(self):
        diagnostics.record_adjudication(self.root, "finding-0001", "confirmed", "Valid")
        previous = (self.root / "adjudication.json").read_bytes()
        diagnostics.save_diagnostics(self.root, self.derived)
        self.assertEqual((self.root / "adjudication.json").read_bytes(), previous)

    def test_invalid_batch_is_atomic(self):
        previous = (self.root / "adjudication.json").read_bytes()
        with self.assertRaises(ValueError):
            diagnostics.record_adjudications(self.root, [
                {"id": "finding-0001", "verdict": "confirmed", "reason": "valid"},
                {"id": "finding-9999", "verdict": "dismissed", "reason": "unknown id"}])
        self.assertEqual((self.root / "adjudication.json").read_bytes(), previous)

    def test_invalid_verdict_reason_evidence_and_conflicting_ids_do_not_write(self):
        invalid = [
            {"id": "finding-0001", "verdict": "approved", "reason": "no"},
            {"id": "finding-0001", "verdict": "confirmed", "reason": " "},
            {"id": "finding-0001", "verdict": "confirmed", "reason": "x", "evidence": {}},
            {"id": "finding-0001", "finding_id": "finding-0002", "verdict": "confirmed", "reason": "x"},
        ]
        previous = (self.root / "adjudication.json").read_bytes()
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                diagnostics.record_adjudications(self.root, [item])
            self.assertEqual((self.root / "adjudication.json").read_bytes(), previous)

    def test_corrupt_existing_state_is_not_reclassified_as_recorded(self):
        record = self.record()
        record["findings"][1]["verdict"] = "passed"
        path = self.root / "adjudication.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        before = path.read_bytes()
        with self.assertRaises(ValueError):
            diagnostics.record_adjudication(self.root, "finding-0001", "confirmed", "valid decision")
        self.assertEqual(path.read_bytes(), before)

    def test_batch_and_review_note_are_recorded(self):
        result = diagnostics.record_adjudications(self.root, [
            {"id": "finding-0001", "verdict": "confirmed", "reason": "Verified"},
            {"id": "finding-0002", "verdict": "dismissed", "reason": "Not applicable"}], "Host checks complete; no automatic approval inferred")
        self.assertEqual(result["review_status"], "recorded")
        self.assertEqual(result["review_notes"][0]["text"], "Host checks complete; no automatic approval inferred")

    def test_empty_review_can_record_a_host_note(self):
        other = self.root / "empty"
        other.mkdir()
        diagnostics.save_diagnostics(other, diagnostics.analyze_report('{"findings":[]}'))
        result = diagnostics.record_adjudications(other, [], "Host inspected the empty report")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["review_status"], "recorded")

    def test_cli_accepts_batch_file_without_loading_configuration_or_credentials(self):
        path = self.root / "decisions.json"
        path.write_text(json.dumps({"decisions": [{"finding_id": "finding-0001", "verdict": "needs_check", "reason": "Need caller"}],
                                    "review_note": "Recorded locally"}), encoding="utf-8")
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            code = diagnostics.main(["--review-dir", str(self.root), "--decisions-file", str(path)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "recorded")
        self.assertEqual(self.record()["findings"][0]["verdict"], "needs_check")

    def test_cli_bad_input_returns_safe_error_and_preserves_record(self):
        before = (self.root / "adjudication.json").read_bytes()
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            code = diagnostics.main(["--review-dir", str(self.root), "--finding", "finding-9999", "--verdict", "confirmed", "--reason", "private evidence"])
        self.assertEqual(code, 2)
        self.assertNotIn("private evidence", output.getvalue())
        self.assertEqual((self.root / "adjudication.json").read_bytes(), before)

    def test_missing_review_directory_and_invalid_diagnostics_fail_without_creating_it(self):
        missing = self.root / "missing"
        with self.assertRaises(ValueError):
            diagnostics.save_diagnostics(missing, self.derived)
        self.assertFalse(missing.exists())
        before = (self.root / "report_diagnostics.json").read_bytes()
        with self.assertRaises(ValueError):
            diagnostics.save_diagnostics(self.root, {"findings": [{"id": "bad"}]})
        self.assertEqual((self.root / "report_diagnostics.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
