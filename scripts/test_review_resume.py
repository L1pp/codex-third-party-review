"""Cross-process source/history restoration fixtures; no model/API is used."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from repository_source import RepositorySource
from review_resume import load_resume, ResumeError


class ReviewResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.repo = self.home / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.write("app.py", "from helper import value\nanswer = value\n")
        self.write("helper.py", "value = 1\r\n# preserved CRLF\r\n")
        self.write("other.py", "unread = 1\n")
        self.git("add", ".")
        self.git("commit", "-qm", "baseline")
        self.write("app.py", "from helper import value\nanswer = value + 1\n")
        self.old = self.home / "old-review"
        self.old.mkdir()
        self.store = RepositorySource(self.repo, task="Check modified caller", output_dir=self.old)
        read = self.store.execute("read_file", {"path": "helper.py", "line_count": 1})
        self.read_scope = {"path": read["path"], "source_id": read["source_id"], "start_line": 1, "end_line": 1}
        self.call = self.old / "calls" / "0001-review"
        self.call.mkdir(parents=True)
        self.messages = [
            {"role": "system", "content": "Review task"},
            {"role": "user", "content": json.dumps({"checkpoint_from_this_review": "Prior uncertainty"})},
            {"role": "assistant", "content": "Verify helper behavior", "reasoning_content": "Already checked caller"},
        ]
        self.save(self.old / "materials.json", self.store.packet)
        self.route = {"model": "deepseek-v4.1-flash-1017", "reasoning_effort": "max", "max_tokens": 256000}
        self.endpoint = "https://api.example.invalid/v1/chat/completions"
        self.save(self.call / "request.json", {"payload": {"messages": self.messages},
                                               "route": self.route, "url": self.endpoint})
        self.save(self.call / "result.json", {"status": "error", "reasoning_chars": 12, "content": "", "usage": {}})
        (self.call / "reasoning.txt").write_text("Potential defect with uncertainty", encoding="utf-8")
        self.flow = {"status": "error", "child_calls": [
            {"stage": "review", "directory": str(self.call), "status": "error"}],
            "coverage": {"read_sources": [self.read_scope], "inline_delivered_ranges": []},
        }
        self.save(self.old / "workflow.json", self.flow)
        self.new = self.home / "new-review"
        self.new.mkdir()

    def git(self, *args):
        return subprocess.run([
            "git", "-c", "user.name=Offline fixture", "-c", "user.email=offline@example.invalid",
            "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(self.home / "no-hooks"),
            "-C", str(self.repo), *args], check=True, capture_output=True).stdout

    def write(self, name, content):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8"))

    @staticmethod
    def save(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def state(self):
        return load_resume(self.old)

    def restore(self, **kwargs):
        return self.state().source_factory({}, **kwargs)(self.new)

    def node_session(self):
        engine = self.old / "engine-session"
        engine.mkdir()
        self.save(engine / "session-header.json", {
            "format": "third-party-review-dsh-session", "formatVersion": 1,
            "header": {"id": "offline-session", "isSeeded": False},
            "inheritedEventCount": 0,
        })
        (engine / "session.jsonl").write_text("", encoding="utf-8")
        return engine

    def compaction_only(self, status="error", *, node=True, stage="compaction"):
        if node:
            self.node_session()
        destination = self.call.parent / ("0001-" + stage)
        self.call.rename(destination)
        self.call = destination
        self.flow.update(status=status, route=dict(self.route))
        self.flow["child_calls"] = [{"stage": stage, "directory": str(self.call), "status": "error"}]
        self.save(self.old / "workflow.json", self.flow)
        summary_route = dict(self.route, reasoning_effort="none", enable_thinking=False, max_tokens=16384)
        self.save(self.call / "request.json", {
            "url": self.endpoint, "route": summary_route,
            "payload": {"messages": [{"role": "user", "content": "Checkpoint task material"},
                                      {"role": "assistant", "content": "SUMMARY_ONLY_SENTINEL"}]},
        })
        self.save(self.call / "result.json", {
            "status": "error", "content": "SUMMARY_ONLY_SENTINEL", "reasoning_text": "SUMMARY_REASONING_SENTINEL",
            "reasoning_chars": 26, "usage": {}, "parse_ok": True,
            "parsed_findings": [{"title": "Not a review finding"}],
        })

    def test_node_running_with_only_initial_compaction_preserves_full_route(self):
        self.compaction_only("running")
        state = self.state()
        self.assertEqual(state.route, self.route)
        self.assertEqual(state.route["reasoning_effort"], "max")
        self.assertEqual(state.route["max_tokens"], 256000)
        self.assertEqual(state.endpoint, self.endpoint)
        self.assertTrue(state.crash_recovery_candidate)
        self.assertEqual(state.latest_call_dir, self.call)
        self.assertFalse(state.provenance["review_progress_available"])
        self.assertEqual(state.provenance["source_call_stage"], "compaction")

    def test_node_requesting_initial_compaction_without_result_is_resumable(self):
        self.compaction_only("requesting")
        (self.call / "result.json").unlink()
        state = self.state()
        self.assertTrue(state.crash_recovery_candidate)
        self.assertEqual(state.latest_result["status"], "interrupted")
        self.assertEqual(state.latest_result["error_type"], "ProcessInterrupted")
        self.assertTrue(state.latest_result["usage_missing"])
        self.assertEqual(state.route, self.route)
        self.assertEqual(state.latest_result["content"], "")
        self.assertEqual(state.latest_result["reasoning_text"], "")

    def test_node_ended_compaction_error_does_not_become_review_findings(self):
        self.compaction_only("error")
        originals = {p.relative_to(self.old).as_posix(): p.read_bytes() for p in self.old.rglob("*") if p.is_file()}
        state = self.state()
        self.assertFalse(state.crash_recovery_candidate)
        self.assertEqual(state.review_messages, [])
        self.assertEqual(state.latest_result["content"], "")
        self.assertEqual(state.latest_result["reasoning_text"], "")
        self.assertFalse(state.latest_result["parse_ok"])
        self.assertIsNone(state.latest_result["parsed_findings"])
        self.assertEqual(state.latest_result["source_stage"], "compaction")
        self.assertEqual(state.inline_delivered_ranges, [])
        self.assertEqual({p.relative_to(self.old).as_posix(): p.read_bytes() for p in self.old.rglob("*") if p.is_file()}, originals)

    def test_legacy_python_compaction_only_still_requires_a_review_record(self):
        self.compaction_only("error", node=False)
        with self.assertRaises(ResumeError): self.state()

    def test_node_compaction_only_never_falls_back_to_summary_route(self):
        self.compaction_only("error")
        self.flow.pop("route")
        self.save(self.old / "workflow.json", self.flow)
        with self.assertRaises(ResumeError): self.state()

    def test_node_compaction_part_directory_is_accepted_inside_calls(self):
        self.compaction_only("running", stage="compaction-part-1-2")
        state = self.state()
        self.assertEqual(state.latest_call_dir, self.call)
        self.assertEqual(state.route, self.route)
        self.assertEqual(state.provenance["source_call_stage"], "compaction-part-1-2")

    def test_node_review_uses_workflow_full_route_when_recorded(self):
        self.node_session()
        self.flow["route"] = dict(self.route, max_tokens=256000)
        self.save(self.old / "workflow.json", self.flow)
        request = json.loads((self.call / "request.json").read_text(encoding="utf-8"))
        request["route"] = dict(self.route, max_tokens=16384, reasoning_effort="none")
        self.save(self.call / "request.json", request)
        self.assertEqual(self.state().route, self.flow["route"])

    def test_node_crash_missing_result_preserves_reasoning_and_unknown_usage(self):
        engine = self.node_session()
        self.flow["status"] = "running"
        self.flow["child_calls"][0]["status"] = "requesting"
        scope = {"path": "repo/app.py", "start_line": 1, "end_line": 2}
        self.flow["child_calls"][0]["inline_material_ranges"] = [scope]
        self.save(self.old / "workflow.json", self.flow)
        (self.call / "result.json").unlink()
        reasoning = "Potential issue\r\nStill verifying evidence."
        (self.call / "reasoning.txt").write_bytes(reasoning.encode("utf-8"))
        # A partial response log is retained, never interpreted as a tool call.
        (self.call / "response_stream.jsonl").write_text('data: {"tool_calls":[{"arguments":"{"}]', encoding="utf-8")
        originals = {p.relative_to(self.old).as_posix(): p.read_bytes() for p in self.old.rglob("*") if p.is_file()}
        state = self.state()
        self.assertTrue(state.crash_recovery_candidate)
        self.assertTrue(state.provenance["crash_recovery_candidate"])
        self.assertEqual(state.latest_result["status"], "interrupted")
        self.assertEqual(state.latest_result["error_type"], "ProcessInterrupted")
        self.assertEqual(state.latest_result["reasoning_text"], reasoning)
        self.assertEqual(state.latest_result["reasoning_chars"], len(reasoning))
        self.assertEqual(state.latest_result["usage"], {})
        self.assertTrue(state.latest_result["usage_missing"])
        self.assertFalse(state.latest_result["parse_ok"])
        self.assertIsNone(state.latest_result["parsed_findings"])
        self.assertEqual(state.latest_result["tool_calls"], [])
        self.assertEqual(state.inline_delivered_ranges, [scope])
        self.assertEqual(state.route, self.route)
        self.assertEqual(state.endpoint, self.endpoint)
        restored = state.source_factory({})(self.new)
        self.assertTrue(restored.repository_manifest()["resume"]["crash_recovery_candidate"])
        self.assertTrue(json.loads((self.new / "resume-state.json").read_text(encoding="utf-8"))["crash_recovery_candidate"])
        self.assertEqual({p.relative_to(self.old).as_posix(): p.read_bytes() for p in self.old.rglob("*") if p.is_file()}, originals)
        self.assertTrue((engine / "session.jsonl").is_file())

    def test_node_unclosed_states_are_only_readonly_recovery_candidates(self):
        engine = self.node_session()
        lock = {"pid": 1, "writer": "possibly-active-offline-fixture"}
        self.save(engine / "writer.lock", lock)
        for status in ("running", "preparing", "requesting"):
            with self.subTest(status=status):
                self.flow["status"] = status
                self.save(self.old / "workflow.json", self.flow)
                state = self.state()
                self.assertTrue(state.crash_recovery_candidate)
                self.assertEqual(state.provenance["source_review_status"], status)
                self.assertEqual(json.loads((engine / "writer.lock").read_text(encoding="utf-8")), lock)

    def test_legacy_python_active_states_and_missing_result_remain_rejected(self):
        for status in ("running", "preparing", "requesting"):
            with self.subTest(status=status):
                self.flow["status"] = status
                self.save(self.old / "workflow.json", self.flow)
                with self.assertRaises(ResumeError): self.state()
        self.flow["status"] = "error"
        self.save(self.old / "workflow.json", self.flow)
        (self.call / "result.json").unlink()
        with self.assertRaises(ResumeError): self.state()

    def test_node_session_pair_is_required_for_unclosed_status(self):
        engine = self.node_session()
        (engine / "session.jsonl").unlink()
        self.flow["status"] = "running"
        self.save(self.old / "workflow.json", self.flow)
        with self.assertRaises(ResumeError): self.state()

    def test_node_session_schema_is_deferred_to_backend(self):
        engine = self.node_session()
        (engine / "session-header.json").write_text("not-json: backend must reject schema", encoding="utf-8")
        self.flow["status"] = "requesting"
        self.save(self.old / "workflow.json", self.flow)
        self.assertTrue(self.state().crash_recovery_candidate)

    def test_node_crash_without_reasoning_stays_incomplete_not_zero_findings(self):
        self.node_session()
        self.flow["status"] = "preparing"
        self.save(self.old / "workflow.json", self.flow)
        (self.call / "result.json").unlink()
        (self.call / "reasoning.txt").unlink()
        state = self.state()
        self.assertEqual(state.latest_result["status"], "interrupted")
        self.assertEqual(state.latest_result["reasoning_chars"], 0)
        self.assertIsNone(state.latest_result["parsed_findings"])
        self.assertTrue(state.latest_result["usage_missing"])

    def test_node_session_event_symlink_escape_is_rejected(self):
        engine = self.node_session()
        events = engine / "session.jsonl"
        events.unlink()
        outside = self.home / "outside-events.jsonl"
        outside.write_text("outside", encoding="utf-8")
        try: events.symlink_to(outside)
        except (OSError, NotImplementedError): self.skipTest("Symlink creation unavailable")
        self.flow["status"] = "running"
        self.save(self.old / "workflow.json", self.flow)
        with self.assertRaises(ResumeError): self.state()

    def test_node_crash_still_requires_request_route_and_endpoint(self):
        self.node_session()
        self.flow["status"] = "running"
        self.save(self.old / "workflow.json", self.flow)
        (self.call / "result.json").unlink()
        request = json.loads((self.call / "request.json").read_text(encoding="utf-8"))
        for field in ("route", "url"):
            with self.subTest(missing=field):
                malformed = dict(request)
                malformed.pop(field)
                self.save(self.call / "request.json", malformed)
                with self.assertRaises(ResumeError): self.state()

    def test_old_capture_identity_view_text_and_scope_survive_disk_changes(self):
        old_bytes = (self.old / "sources" / "source-0002.txt").read_bytes()
        self.write("helper.py", "value = 999\n")
        state = self.state()
        restored = state.source_factory({})(self.new)
        page = restored.execute("read_file", {"path": "helper.py", "line_count": 2})
        self.assertEqual(page["source_id"], "source-0002")
        self.assertEqual(page["view"], "worktree")
        self.assertEqual(page["source_origin"], "restored_capture")
        self.assertEqual(page["text"], "value = 1\r\n# preserved CRLF\r\n")
        self.assertEqual((self.new / "sources" / "source-0002.txt").read_bytes(), old_bytes)
        self.assertEqual((self.old / "sources" / "source-0002.txt").read_bytes(), old_bytes)
        self.assertEqual(state.read_sources, [self.read_scope])
        self.assertEqual(state.review_messages, self.messages)
        self.assertEqual(state.previous_checkpoint, "Prior uncertainty")
        self.assertEqual(state.route, self.route)
        self.assertEqual(state.endpoint, self.endpoint)
        self.assertFalse(state.provenance["old_usage_in_new_call_totals"])
        self.assertEqual(state.provenance["old_calls_replayed"], 0)

    def test_new_path_reads_current_content_with_distinct_identity(self):
        self.write("other.py", "unread = 2\n")
        restored = self.restore()
        page = restored.execute("read_file", {"path": "other.py"})
        self.assertEqual(page["text"], "unread = 2\n")
        self.assertEqual(page["source_id"], "source-0003")
        self.assertEqual(page["source_origin"], "new_after_resume")
        self.assertEqual(restored.repository_manifest()["sources"][-1]["source_origin"], "new_after_resume")
        self.assertIn("current selected", restored.exploration_context()["resume_source_policy"])

    def test_existing_capture_search_uses_old_content(self):
        self.write("helper.py", "value = 999\n")
        match = self.restore().execute("search_code", {"query": "preserved CRLF"})["matches"][0]
        self.assertEqual(match["source_id"], "source-0002")
        self.assertEqual(match["source_origin"], "restored_capture")

    def test_material_tools_keep_restored_source_identity(self):
        source = self.restore()
        read = source.execute("read_material", {"path": "repo/helper.py", "line_count": 1})
        self.assertEqual(read["source_id"], "source-0002")
        self.assertEqual(read["source_origin"], "restored_capture")
        match = source.execute("search_material", {"query": "preserved CRLF"})["matches"][0]
        self.assertEqual(match["source_id"], "source-0002")
        indexed = next(i for i in source.index() if i["path"] == "repo/helper.py")
        self.assertEqual(indexed["source_origin"], "restored_capture")

    def test_credential_and_explicit_exclusions_remain_active(self):
        self.write("config.py", "api_key = 'configured-key'\n")
        self.write(".env", "PRIVATE=1\n")
        source = self.restore(excluded_paths=[self.repo / "config.py"], redactor=lambda t: t.replace("configured-key", "[REDACTED]"))
        self.assertIn("error", source.execute("read_file", {"path": "config.py"}))
        self.assertIn("error", source.execute("read_file", {"path": ".env"}))
        self.assertIn("error", source.execute("read_file", {"path": "../old-review/workflow.json"}))

    def test_new_exclusion_cannot_be_bypassed_via_restored_material(self):
        with self.assertRaises(ResumeError):
            self.restore(excluded_paths=[self.repo / "helper.py"])

    def test_missing_route_is_not_replaced_with_current_time_route(self):
        request = json.loads((self.call / "request.json").read_text(encoding="utf-8"))
        request.pop("route")
        self.save(self.call / "request.json", request)
        with self.assertRaises(ResumeError): self.state()

    def test_snapshot_path_traversal_is_rejected(self):
        index = self.store.repository_manifest()
        index["sources"][0]["snapshot_file"] = "../secret.txt"
        self.save(self.old / "sources" / "index.json", index)
        with self.assertRaises(ResumeError): self.state()

    def test_snapshot_symlink_escape_is_rejected(self):
        snapshot = self.old / "sources" / "source-0001.txt"
        outside = self.home / "outside.txt"
        outside.write_bytes(snapshot.read_bytes())
        snapshot.unlink()
        try: snapshot.symlink_to(outside)
        except (OSError, NotImplementedError): self.skipTest("Symlink creation unavailable")
        with self.assertRaises(ResumeError): self.state()

    def test_recorded_request_symlink_escape_is_rejected(self):
        request = self.call / "request.json"
        outside = self.home / "outside-request.json"
        outside.write_bytes(request.read_bytes())
        request.unlink()
        try: request.symlink_to(outside)
        except (OSError, NotImplementedError): self.skipTest("Symlink creation unavailable")
        with self.assertRaises(ResumeError): self.state()

    def test_missing_repository_manifest_cannot_downgrade_to_packet_mode(self):
        (self.old / "sources" / "index.json").unlink()
        with self.assertRaises(ResumeError): self.state()

    def test_missing_or_incomplete_capture_is_rejected(self):
        (self.old / "sources" / "source-0001.txt").write_text("truncated", encoding="utf-8")
        with self.assertRaises(ResumeError): self.state()

    def test_duplicate_or_changed_source_identity_is_rejected(self):
        index = self.store.repository_manifest()
        index["sources"][1]["source_id"] = "source-0001"
        self.save(self.old / "sources" / "index.json", index)
        with self.assertRaises(ResumeError): self.state()

    def test_completed_run_and_outside_request_path_are_rejected(self):
        self.flow["status"] = "completed"
        self.save(self.old / "workflow.json", self.flow)
        with self.assertRaises(ResumeError): self.state()
        self.flow["status"] = "error"
        self.flow["child_calls"][0]["directory"] = str(self.home / "outside" / "0001-review")
        self.save(self.old / "workflow.json", self.flow)
        with self.assertRaises(ResumeError): self.state()

    def test_old_run_cannot_be_reused_as_new_output(self):
        state = self.state()
        with self.assertRaises(ResumeError): state.source_factory({})(self.old)
        nested = self.old / "child"
        nested.mkdir()
        with self.assertRaises(ResumeError): state.source_factory({})(nested)

    def test_validate_output_rejects_old_or_nested_path_without_creating_it(self):
        state = self.state()
        nested = self.old / "not-created" / "new-run"
        for output in (self.old, nested):
            with self.subTest(output=str(output)):
                with self.assertRaises(ResumeError): state.validate_output(output)
        self.assertFalse((self.old / "not-created").exists())

    def test_validate_output_rejects_ancestor_without_changing_old_record(self):
        old_record = (self.old / "workflow.json").read_bytes()
        state = self.state()
        with self.assertRaises(ResumeError): state.validate_output(self.home)
        self.assertEqual((self.old / "workflow.json").read_bytes(), old_record)

    def test_validate_output_accepts_separate_new_path_without_creating_it(self):
        output = self.home / "separate" / "future-run"
        self.assertEqual(self.state().validate_output(output), output.resolve())
        self.assertFalse(output.parent.exists())

    def test_tool_size_none_uses_normal_workflow_defaults(self):
        config = {"context_management": {"tool_max_chars": None, "tool_max_lines": None}}
        store = self.state().source_factory(config)(self.new)
        self.assertEqual(store.max_chars, 24000)
        self.assertEqual(store.max_lines, 250)

    def test_tool_size_numeric_strings_use_normal_workflow_coercion(self):
        config = {"context_management": {"tool_max_chars": "12", "tool_max_lines": "2"}}
        store = self.state().source_factory(config)(self.new)
        self.assertEqual(store.max_chars, 12)
        self.assertEqual(store.max_lines, 2)
        self.assertLessEqual(len(store.execute("read_file", {"path": "helper.py"})["text"]), 12)

    def test_tool_size_zero_uses_normal_workflow_minimum(self):
        config = {"context_management": {"tool_max_chars": 0, "tool_max_lines": 0}}
        store = self.state().source_factory(config)(self.new)
        self.assertEqual(store.max_chars, 1)
        self.assertEqual(store.max_lines, 1)
        self.assertEqual(len(store.execute("read_file", {"path": "helper.py"})["text"]), 1)

    def test_inline_no_progress_401_or_503_is_not_delivered(self):
        scope = {"path": "repo/app.py", "start_line": 1, "end_line": 2}
        self.flow["child_calls"][0]["inline_material_ranges"] = [scope]
        self.flow["coverage"]["inline_delivered_ranges"] = [scope]
        (self.call / "reasoning.txt").unlink()
        for code in (401, 503):
            with self.subTest(http_status=code):
                self.save(self.call / "result.json", {"status": "error", "http_status": code,
                                                       "content": "", "reasoning_chars": 0})
                self.save(self.old / "workflow.json", self.flow)
                state = self.state()
                self.assertEqual(state.inline_delivered_ranges, [])
                self.assertEqual(state.provenance["inline_delivery_reconstruction"]["unconfirmed_attempt_range_entries"], 1)

    def test_inline_raw_generated_progress_confirms_legacy_attempt(self):
        scope = {"path": "repo/helper.py", "start_line": 1, "end_line": 1}
        self.flow["child_calls"][0]["inline_material_ranges"] = [scope]
        self.flow["child_calls"][0]["recovery_classification"] = {"has_progress": False}
        (self.call / "reasoning.txt").unlink()
        for progress in ({"reasoning_chars": 12}, {"content": "Candidate issue"},
                         {"tool_calls": [{"function": {"name": "read_file", "arguments": "{"}}]}):
            with self.subTest(progress=progress):
                self.save(self.call / "result.json", {"status": "error", **progress})
                self.save(self.old / "workflow.json", self.flow)
                self.assertEqual(self.state().inline_delivered_ranges, [scope])

    def test_inline_confirmed_modern_attempt_and_inherited_ranges_are_retained(self):
        inherited = {"path": "repo/app.py", "start_line": 1, "end_line": 1}
        current = {"path": "repo/helper.py", "start_line": 1, "end_line": 2}
        self.flow["inherited_inline_delivered_ranges"] = [inherited]
        self.flow["child_calls"][0].update(inline_material_ranges=[current],
                                          recovery_classification={"has_progress": True})
        self.save(self.old / "workflow.json", self.flow)
        self.assertEqual(self.state().inline_delivered_ranges, [inherited, current])

    def test_inline_completed_tool_attempt_is_confirmed_without_using_legacy_union(self):
        completed = {"path": "repo/app.py", "start_line": 1, "end_line": 2}
        phantom = {"path": "repo/never-delivered.py", "start_line": 1, "end_line": 99}
        self.flow["child_calls"].insert(0, {
            "stage": "review", "directory": str(self.old / "calls" / "0000-review"),
            "status": "tool_calls", "inline_material_ranges": [completed],
        })
        (self.old / "calls" / "0000-review").mkdir()
        self.flow["coverage"]["inline_delivered_ranges"] = [completed, phantom]
        self.save(self.old / "workflow.json", self.flow)
        self.assertEqual(self.state().inline_delivered_ranges, [completed])

    def test_inline_prior_attempt_path_cannot_escape_recorded_calls(self):
        self.flow["child_calls"].insert(0, {
            "stage": "review", "directory": str(self.home / "outside" / "0000-review"),
            "status": "error", "inline_material_ranges": [self.read_scope],
        })
        self.save(self.old / "workflow.json", self.flow)
        with self.assertRaises(ResumeError): self.state()

    def test_packet_only_run_restores_without_repository(self):
        manifest = self.old / "sources" / "index.json"
        manifest.unlink()
        packet = {"task": "Packet review", "files": [{"path": "app.py", "content": "value=1\n"}]}
        self.save(self.old / "materials.json", packet)
        state = self.state()
        store = state.source_factory({})(self.new)
        self.assertFalse(getattr(store, "exploration_enabled", False))
        self.assertEqual(store.packet, packet)
        self.assertEqual(store.execute("read_material", {"path": "app.py"})["text"], "value=1\n")
        self.assertTrue((self.new / "resume-state.json").exists())

    def test_packet_resume_redacts_saved_materials_and_tool_content_consistently(self):
        (self.old / "sources" / "index.json").unlink()
        packet = {"task": "Packet review", "files": [{"path": "app.py", "content": "value='CURRENT_KEY_SENTINEL'\n"}]}
        self.save(self.old / "materials.json", packet)
        def redact(value):
            if isinstance(value, str):
                return value.replace("CURRENT_KEY_SENTINEL", "[REDACTED]")
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            if isinstance(value, list):
                return [redact(item) for item in value]
            return value
        source = self.state().source_factory({}, redactor=redact)(self.new)
        self.assertNotIn("CURRENT_KEY_SENTINEL", json.dumps(source.packet))
        page = source.execute("read_material", {"path": "app.py"})
        self.assertIn("[REDACTED]", page["text"])
        self.assertNotIn("CURRENT_KEY_SENTINEL", page["text"])
        self.assertIn("CURRENT_KEY_SENTINEL", (self.old / "materials.json").read_text(encoding="utf-8"))

    def test_resume_metadata_and_original_failure_are_unchanged(self):
        original = (self.old / "workflow.json").read_bytes()
        self.restore()
        meta = json.loads((self.new / "resume-state.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["source_review_status"], "error")
        self.assertEqual(meta["old_tool_calls_reexecuted"], 0)
        self.assertEqual((self.old / "workflow.json").read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
