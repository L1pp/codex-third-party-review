"""Offline CLI resume wiring; credentials and network calls are always fakes."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import history_cleanup
import repository_source
import review
import review_resume
import workflow


ROUTE = {
    "model": "deepseek-v4.1-flash-1017", "reasoning_effort": "max",
    "enable_thinking": True, "max_tokens": 256000,
    "context_window_tokens": 1000000,
}
FAKE_KEY = "sk-resume-cli-offline-placeholder"


def budget(messages, tools, route, config, output_cap=None):
    cap = output_cap if output_cap is not None else route["max_tokens"]
    return {
        "input_tokens": 1000, "method": "offline_fixture",
        "context_limit": 1000000, "safety_tokens": 2048,
        "compression_trigger_tokens": 678464,
        "requested_output_tokens": cap, "effective_output_tokens": cap,
        "needs_compaction": False, "retain_recent_tokens": 119040,
    }


class ResumeCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.old = self.home / "old-review"
        self.call = self.old / "calls" / "0001-review"
        self.call.mkdir(parents=True)
        self.output = self.home / "new-review"
        self.default_output = self.home / "skill-logs" / "reviews"
        self.config = review.read_json(Path(review.__file__).resolve().parent.parent / "config.example.json")
        self.config.update(base_url="https://resume.example.invalid/v1", ledger_file=str(self.home / "calls.sqlite3"),
                           context_management=dict(self.config["context_management"], inline_material_token_limit=0))
        self.config.pop("model_catalog_file", None)
        self.config_path = self.home / "local-config.json"
        self.save(self.config_path, self.config)
        self.packet = {"task": "Review recorded source behavior", "files": [{"path": "calc.py", "content": "value = 1\n"}]}
        self.save(self.old / "materials.json", self.packet)
        self.save(self.old / "workflow.json", {
            "status": "error", "child_calls": [{"stage": "review", "directory": str(self.call), "status": "error"}],
            "coverage": {"read_sources": [], "inline_delivered_ranges": []},
        })
        self.save(self.call / "request.json", {
            "url": review.config_url(self.config), "route": ROUTE,
            "payload": {"messages": [{"role": "system", "content": "Review source"},
                                       {"role": "user", "content": "Validate calc behavior"}]},
        })
        self.save(self.call / "result.json", {
            "status": "error", "error_type": "ProviderError", "http_status": 200,
            "provider_error": {"code": "GatewayTimeout", "message": "Deadline Exceeded"},
            "content": "", "reasoning_chars": 20, "usage": {}, "seconds": 1800,
        })
        (self.call / "reasoning.txt").write_text("Check uncertain boundary in calc.py", encoding="utf-8")
        self.original = {str(p.relative_to(self.old)): p.read_bytes() for p in self.old.rglob("*") if p.is_file()}
        for target, attribute, replacement in (
            (review, "DEFAULT_OUTPUT_ROOT", self.default_output),
            (workflow, "plan_budget", budget),
        ):
            handle = patch.object(target, attribute, replacement)
            handle.start()
            self.addCleanup(handle.stop)
        # These are strict side-effect guards, not substituted model responses.
        handle = patch.object(review.requests, "Session", side_effect=AssertionError("Network/model calls are forbidden in offline CLI tests"))
        self.session = handle.start()
        self.addCleanup(handle.stop)
        handle = patch.object(history_cleanup, "estimate_tokens", return_value={"input_tokens": 10})
        handle.start()
        self.addCleanup(handle.stop)

    @staticmethod
    def save(path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def main(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = review.main(["--config", str(self.config_path), "--quiet-progress", *map(str, args)])
        payload = json.loads(stdout.getvalue())
        self.session.assert_not_called()
        return code, payload

    def assert_original_unchanged(self):
        actual = {str(p.relative_to(self.old)): p.read_bytes() for p in self.old.rglob("*") if p.is_file()}
        self.assertEqual(actual, self.original)

    def test_resume_does_not_infer_cwd_repository_and_passes_state_factory(self):
        state = review_resume.load_resume(self.old)
        real_factory = state.source_factory
        with patch.object(review_resume, "load_resume", return_value=state) as loader, \
                patch.object(state, "source_factory", wraps=real_factory) as factory, \
                patch.object(repository_source, "RepositorySource", side_effect=AssertionError("Resume must not collect cwd as a new repository")), \
                patch.object(review, "load_credential", side_effect=AssertionError("Dry run must not read credentials")), \
                patch.object(workflow, "run_workflow", return_value={"status": "dry_run", "route": ROUTE}) as run:
            code, summary = self.main("--resume", self.old, "--output-dir", self.output, "--dry-run")
        self.assertEqual(code, 0)
        loader.assert_called_once_with(self.old)
        factory.assert_called_once()
        self.assertIs(run.call_args.kwargs["resume_state"], state)
        self.assertTrue(callable(run.call_args.kwargs["source_factory"]))
        self.assertEqual(run.call_args.args[1], self.packet)
        self.assertEqual(run.call_args.args[2], self.output)
        self.assertTrue(run.call_args.kwargs["dry_run"])
        self.assertEqual(summary["output_dir"], str(self.output))
        self.assert_original_unchanged()

    def test_real_resume_dry_run_prepares_new_directory_without_credentials_or_network(self):
        with patch.object(review, "load_credential", side_effect=AssertionError("Dry run must not read credentials")), \
                patch.object(review, "select_route", side_effect=AssertionError("Resume must keep recorded route")):
            code, summary = self.main("--resume", self.old, "--output-dir", self.output, "--dry-run")
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["status"], "dry_run")
        self.assertEqual(summary["model_calls"], 0)
        request = json.loads((self.output / "calls" / "0001-review" / "request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["route"], ROUTE)
        self.assertEqual(request["payload"]["model"], ROUTE["model"])
        self.assertEqual(request["payload"]["reasoning_effort"], "max")
        flow = json.loads((self.output / "workflow.json").read_text(encoding="utf-8"))
        self.assertTrue(flow["resume"]["checkpoint_pending"])
        self.assertFalse(flow["resume"]["old_usage_in_new_call_totals"])
        self.assert_original_unchanged()

    def test_resume_default_output_is_new_skill_log_directory(self):
        with patch.object(review, "load_credential", side_effect=AssertionError("Dry run must not read credentials")):
            code, summary = self.main("--resume", self.old, "--dry-run")
        self.assertEqual(code, 0, summary)
        output = Path(summary["output_dir"])
        self.assertEqual(output.parent, self.default_output)
        self.assertRegex(output.name, r"^[0-9]{8}T[0-9]{12}Z$")
        self.assertTrue((output / "resume-state.json").is_file())
        self.assertNotEqual(output, self.old)
        self.assert_original_unchanged()

    def test_explicit_nested_output_parent_is_created_and_old_record_preserved(self):
        destination = self.home / "chosen-parent" / "continuations" / "fresh"
        with patch.object(review, "load_credential", side_effect=AssertionError("Dry run must not read credentials")):
            code, summary = self.main("--resume", self.old, "--output-dir", destination, "--dry-run")
        self.assertEqual(code, 0, summary)
        self.assertEqual(summary["output_dir"], str(destination))
        self.assertTrue((destination / "workflow.json").is_file())
        self.assert_original_unchanged()

    def test_existing_output_is_rejected_without_model_or_old_record_changes(self):
        self.output.mkdir()
        marker = self.output / "untouched.txt"
        marker.write_text("preserve", encoding="utf-8")
        with patch.object(review, "load_credential", side_effect=AssertionError("Dry run must not read credentials")):
            code, summary = self.main("--resume", self.old, "--output-dir", self.output, "--dry-run")
        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "error")
        self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
        self.assert_original_unchanged()

    def test_nested_output_is_rejected_before_creating_any_old_run_files(self):
        nested = self.old / "never-created-parent" / "new-run"
        with patch.object(review, "load_credential", side_effect=AssertionError("Dry run must not read credentials")):
            code, summary = self.main("--resume", self.old, "--output-dir", nested, "--dry-run")
        self.assertEqual(code, 2)
        self.assertEqual(summary["status"], "error")
        self.assertFalse(nested.parent.exists())
        self.assert_original_unchanged()

    def test_endpoint_mismatch_stops_before_workflow_or_model(self):
        request = json.loads((self.call / "request.json").read_text(encoding="utf-8"))
        request["url"] = "https://other.example.invalid/v1/chat/completions"
        self.save(self.call / "request.json", request)
        with patch.object(review, "load_credential", return_value=FAKE_KEY), \
                patch.object(workflow, "run_workflow", side_effect=AssertionError("Mismatched endpoint must stop before execution")):
            code, summary = self.main("--resume", self.old, "--output-dir", self.output)
        self.assertEqual(code, 2)
        self.assertIn("endpoint differs", summary["error"])
        self.assertFalse(self.output.exists())

    def test_live_resume_keeps_old_route_and_passes_only_actual_attempts_to_runner(self):
        calls = []
        def local_call(config, packet, directory, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=False)
            is_checkpoint = kwargs["route_override"].get("reasoning_effort") == "none"
            result = {
                "status": "completed", "finish_reason": "stop", "stream_done": True,
                "content": "Checked caller; verify uncertain calc boundary" if is_checkpoint else '{"findings":[],"context_gaps":[]}',
                "parse_ok": not is_checkpoint, "parsed_report": None if is_checkpoint else {"findings": [], "context_gaps": []},
                "parsed_findings": None if is_checkpoint else [], "seconds": 0.25,
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
            self.save(directory / "result.json", result)
            return result
        with patch.object(review, "load_credential", return_value=FAKE_KEY) as credential, \
                patch.object(review, "select_route", side_effect=AssertionError("Current time must not re-route old DeepSeek review to auto")), \
                patch.object(review, "run_review", side_effect=local_call):
            code, summary = self.main("--resume", self.old, "--output-dir", self.output)
        self.assertEqual(code, 0, summary)
        credential.assert_called_once()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call["route_override"]["model"] == ROUTE["model"] for call in calls))
        self.assertEqual(calls[-1]["route_override"], ROUTE)
        self.assertEqual(summary["model_calls"], 2)
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(len(list((self.output / "calls").iterdir())), 2)
        self.assert_original_unchanged()

    def test_resume_packet_and_repo_are_mutually_exclusive_before_side_effects(self):
        alternatives = (("--resume", self.old, "--packet", self.home / "packet.json"),
                        ("--resume", self.old, "--repo", self.home / "repo"),
                        ("--packet", self.home / "packet.json", "--repo", self.home / "repo"))
        for arguments in alternatives:
            with self.subTest(arguments=arguments), \
                    patch.object(review, "read_json", side_effect=AssertionError("Parse conflict must stop before reading config")), \
                    patch.object(review, "load_credential", side_effect=AssertionError("Parse conflict must stop before credentials")), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    review.main(list(map(str, arguments)))
                self.assertEqual(caught.exception.code, 2)
        self.session.assert_not_called()


if __name__ == "__main__":
    unittest.main()