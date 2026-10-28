"""Repository discovery end-to-end tests; model and transport are offline fakes."""
import copy
import io
import json
import os
import sys
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import review
import workflow
from repository_source import RepositorySource

EXAMPLE = review.read_json(Path(__file__).resolve().parent.parent / "config.example.json")
NIGHT = "2026-10-27T23:00:00+08:00"


class RepositoryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.repo = self.home / "project"
        self.repo.mkdir()
        self.hooks = self.home / "no-hooks"
        self.hooks.mkdir()
        self.git("init", "-q")
        (self.repo / "app.py").write_text("from helper import add\nvalue = add(1, 2)\n", encoding="utf-8")
        (self.repo / "helper.py").write_text("def add(a, b):\n    return a + b  # UNCHANGED_HELPER\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "baseline")
        (self.repo / "app.py").write_text("from helper import add\nvalue = add(2, 3)\n", encoding="utf-8")
        self.config = copy.deepcopy(EXAMPLE)
        self.config["ledger_file"] = str(self.home / "calls.sqlite3")
        self.config["credential_file"] = str(self.home / "absent-credentials.json")
        self.config["context_management"]["max_model_calls"] = 8
        self.out = self.home / "review"
        self.calls = []
        self.sources = []

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Offline Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=" + str(self.hooks), *args],
            cwd=self.repo, check=True, capture_output=True, text=True, encoding="utf-8",
        ).stdout

    def factory(self, root, **extra):
        store = RepositorySource(self.repo, task="Check caller compatibility", output_dir=root, **extra)
        self.sources.append(store)
        return store

    def fake_call(self, config, packet, output_dir, **kwargs):
        self.calls.append(kwargs)
        return self.response(kwargs)

    def run_flow(self, response, **source_options):
        self.response = response
        return workflow.run_workflow(
            self.config, {}, self.out, review_call=self.fake_call,
            route_selector=lambda cfg, at=None: review.select_route(cfg, NIGHT),
            source_factory=lambda root: self.factory(root, **source_options),
        )

    @staticmethod
    def tool(name, arguments):
        return {"status": "tool_calls", "finish_reason": "tool_calls", "content": "",
                "tool_calls": [{"id": "t1", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments)}}]}

    @staticmethod
    def completed():
        report = {"findings": [], "context_gaps": []}
        return {"status": "completed", "finish_reason": "stop", "content": json.dumps(report),
                "parse_ok": True, "parsed_report": report, "parsed_findings": []}

    def test_small_inline_change_still_explores_unchanged_file_and_keeps_source(self):
        def response(call):
            if len(self.calls) == 1:
                names = {t["function"]["name"] for t in call["tools"]}
                self.assertIn("read_file", names)
                self.assertIn("search_code", names)
                initial = json.dumps(call["chat_messages"])
                self.assertNotIn("UNCHANGED_HELPER", initial)
                self.assertIn("repository_access", initial)
                return self.tool("read_file", {"root": "repo", "path": "helper.py"})
            tool_result = json.loads(call["chat_messages"][-1]["content"])
            self.assertIn("UNCHANGED_HELPER", tool_result["text"])
            self.assertTrue(tool_result["source_id"])
            return self.completed()
        result = self.run_flow(response)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["tools_executed"], 1)
        self.assertTrue(result["coverage"]["read_sources"][0]["source_id"])
        self.assertTrue((self.out / "sources/index.json").is_file())
        self.assertTrue(any("UNCHANGED_HELPER" in p.read_text(encoding="utf-8") for p in (self.out / "sources").glob("source-*.txt")))
        self.assertTrue((self.out / "report_diagnostics.json").is_file())
        self.assertTrue((self.out / "adjudication.json").is_file())

    def test_workspace_search_result_tracks_the_captured_source(self):
        workspace = self.home / "shared"
        workspace.mkdir()
        (workspace / "contract.py").write_text("RULE = 'SHARED_CONTRACT'\n", encoding="utf-8")
        def response(call):
            if len(self.calls) == 1:
                return self.tool("search_code", {"root": "workspace-1", "query": "SHARED_CONTRACT"})
            value = json.loads(call["chat_messages"][-1]["content"])
            self.assertTrue(value["matches"])
            self.assertTrue(value["matches"][0]["source_id"])
            return self.completed()
        result = self.run_flow(response, workspace_roots=[workspace])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["coverage"]["read_sources"][0]["access"], "search_snippet")
        self.assertTrue(result["coverage"]["read_sources"][0]["source_id"])

    def test_length_recovery_keeps_repository_tools_after_checkpoint(self):
        def response(call):
            if len(self.calls) == 1:
                return {"status": "incomplete", "finish_reason": "length", "content": "partial report", "parse_ok": False}
            if call["route_override"].get("enable_thinking") is False:
                return {"status": "completed", "finish_reason": "stop", "content": "check helper contract next", "parse_ok": False}
            if len(self.calls) == 3:
                self.assertIn("read_file", {t["function"]["name"] for t in call["tools"]})
                return self.tool("read_file", {"root": "repo", "path": "helper.py"})
            self.assertIn("UNCHANGED_HELPER", call["chat_messages"][-1]["content"])
            return self.completed()
        result = self.run_flow(response)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["compactions"], 1)
        self.assertEqual(result["model_calls"], 4)

    def test_inline_shrink_after_checkpoint_keeps_exploration_context(self):
        real_budget = workflow.plan_budget
        pressure = {"applied": False}
        def budgeter(messages, tools, route, config, output_cap=None):
            budget = real_budget(messages, tools, route, config, output_cap=output_cap)
            resumed = any("checkpoint_from_this_review" in str(m.get("content", ""))
                          for m in messages if m.get("role") == "user")
            if tools and resumed and not pressure["applied"]:
                pressure["applied"] = True
                budget = dict(budget, needs_compaction=True,
                              input_tokens=budget["compression_trigger_tokens"] + 1)
            return budget
        def response(call):
            if len(self.calls) == 1:
                return {"status": "incomplete", "finish_reason": "length", "content": "partial", "parse_ok": False}
            if call["route_override"].get("enable_thinking") is False:
                return {"status": "completed", "finish_reason": "stop", "content": "checkpoint keep exploring", "parse_ok": False}
            context = json.loads(next(m["content"] for m in call["chat_messages"] if m.get("role") == "user"))
            self.assertIn("repository_access", context)
            self.assertIn("checkpoint_from_this_review", context)
            self.assertIn("read_file", {t["function"]["name"] for t in call["tools"]})
            self.assertNotIn("缺失时才按需", context["instruction"])
            return self.completed()
        with patch.object(workflow, "plan_budget", side_effect=budgeter):
            result = self.run_flow(response)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(pressure["applied"])
        saved = json.loads((self.out / "workflow.json").read_text(encoding="utf-8"))
        self.assertTrue(saved["inline_budget_reductions"])

    def test_scalar_array_in_prose_does_not_make_report_incomplete(self):
        content = 'Reviewed constants [1, 2].\n{"findings": [], "context_gaps": []}'
        report, findings = review.parse_report(content)
        result = self.run_flow(lambda call: dict(self.completed(), content=content,
                                                parsed_report=report, parsed_findings=findings))
        self.assertEqual(result["status"], "completed")
        diagnostics = json.loads((self.out / "report_diagnostics.json").read_text(encoding="utf-8"))
        self.assertFalse(diagnostics["possible_loss"])

    def test_truncated_second_report_is_preserved_as_incomplete(self):
        content = '{"findings": []}\n{"findings": ['
        with self.assertRaises(ValueError):
            review.parse_report(content)
        result = self.run_flow(lambda call: dict(self.completed(), content=content,
                                                parse_ok=False))
        self.assertEqual(result["status"], "incomplete")
        self.assertTrue(result["content_is_partial"])
        self.assertEqual(result["content"], content)
        self.assertEqual(result["parsed_findings"], [])
        diagnostics = json.loads((self.out / "report_diagnostics.json").read_text(encoding="utf-8"))
        self.assertTrue(diagnostics["possible_loss"])

    def test_preparation_failure_keeps_structured_outputs_without_model_call(self):
        from repository_source import RepositoryError
        def unavailable(root):
            raise RepositoryError("Selected Git read timed out")
        result = workflow.run_workflow(
            self.config, {}, self.out,
            review_call=lambda *args, **kwargs: self.fail("no model call during source failure"),
            route_selector=lambda cfg, at=None: review.select_route(cfg, NIGHT),
            source_factory=unavailable,
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["model_calls"], 0)
        self.assertIn("RepositoryError", result["uncompleted_reason"])
        for filename in ["workflow.json", "result.json", "report.md"]:
            self.assertTrue((self.out / filename).is_file())

    def test_repository_cli_uses_legacy_tool_limit_normalization(self):
        self.config["context_management"].update(tool_max_chars=None, tool_max_lines=None)
        config_path = self.home / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(review, "load_credential", side_effect=AssertionError("no keys")), \
             patch.object(review.requests, "Session", side_effect=AssertionError("no HTTP")), \
             patch("sys.stdout", new_callable=io.StringIO):
            code = review.main(["--config", str(config_path), "--repo", str(self.repo),
                                "--output-dir", str(self.out), "--dry-run"])
        self.assertEqual(code, 0)
        request = json.loads((self.out / "calls/0001-review/request.json").read_text(encoding="utf-8"))
        tool = next(t["function"] for t in request["payload"]["tools"] if t["function"]["name"] == "read_file")
        self.assertEqual(tool["parameters"]["properties"]["line_count"]["default"], 250)

    def test_resume_prompt_keeps_overflow_reading_guidance(self):
        self.config["context_management"]["inline_material_token_limit"] = 1
        def response(call):
            if len(self.calls) == 1:
                return {"status": "incomplete", "finish_reason": "length", "content": "partial", "parse_ok": False}
            if call["route_override"].get("enable_thinking") is False:
                return {"status": "completed", "finish_reason": "stop", "content": "checkpoint", "parse_ok": False}
            user = json.loads(next(m["content"] for m in call["chat_messages"] if m.get("role") == "user"))
            self.assertTrue(user["materials_index"]["overflow_materials"])
            self.assertIn("overflow_materials", user["materials_index"]["instruction"])
            self.assertIn("read_material", user["materials_index"]["instruction"])
            return self.completed()
        result = self.run_flow(response)
        self.assertEqual(result["status"], "completed")

    def test_package_import_preserves_final_report_and_diagnostics(self):
        script = """
import json,sys
from pathlib import Path
from scripts.workflow import run_workflow
report = {"findings": []}
def call(*args, **kwargs):
    return {"status":"completed", "finish_reason":"stop", "content":json.dumps(report),
            "parse_ok":True, "parsed_report":report, "parsed_findings":[]}
r=run_workflow({"context_window_tokens":10000,"context_management":{"headroom_tokens":0}}, {"files":[{"path":"x.py","content":"value=1"}]},
    Path(sys.argv[1]), review_call=call, route_selector=lambda cfg, at=None:{"model":"offline","max_tokens":1024})
print(json.dumps({"status":r["status"], "diagnostics":r.get("report_diagnostics_file")}))
"""
        environment = dict(os.environ, PYTHONUTF8="1")
        environment.pop("PYTHONPATH", None)
        completed = subprocess.run([sys.executable, "-c", script, str(self.out)],
                                   cwd=Path(__file__).resolve().parent.parent, env=environment,
                                   capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["status"], "completed")
        self.assertTrue((self.out / "report_diagnostics.json").is_file())

    def test_cli_repo_dry_run_without_key_uses_default_output_root(self):
        config_path = self.home / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        default_outputs = self.home / "skill-logs" / "reviews"
        with patch.object(review, "DEFAULT_OUTPUT_ROOT", default_outputs), \
             patch.object(review, "load_credential", side_effect=AssertionError("no credential reads")), \
             patch.object(review.requests, "Session", side_effect=AssertionError("no HTTP")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            code = review.main(["--config", str(config_path), "--repo", str(self.repo), "--task", "offline setup", "--dry-run"])
        self.assertEqual(code, 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["status"], "dry_run")
        self.assertEqual(summary["model_calls"], 0)
        out = Path(summary["output_dir"])
        self.assertEqual(out.parent, default_outputs)
        materials = json.loads((out / "materials.json").read_text(encoding="utf-8"))
        self.assertTrue(materials["files"])
        request = json.loads((out / "calls/0001-review/request.json").read_text(encoding="utf-8"))
        self.assertIn("read_file", {t["function"]["name"] for t in request["payload"]["tools"]})
        diagnostics = json.loads((out / "report_diagnostics.json").read_text(encoding="utf-8"))
        self.assertEqual(diagnostics["parse_status"], "not_requested")

    def test_cli_defaults_to_current_repository_without_packet(self):
        config_path = self.home / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        with patch.object(review.Path, "cwd", return_value=self.repo), \
             patch.object(review, "load_credential", side_effect=AssertionError("no credential reads")), \
             patch.object(review.requests, "Session", side_effect=AssertionError("no HTTP")), \
             patch("sys.stdout", new_callable=io.StringIO) as output:
            code = review.main(["--config", str(config_path), "--output-dir", str(self.out), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["model_calls"], 0)
        request = json.loads((self.out / "calls/0001-review/request.json").read_text(encoding="utf-8"))
        self.assertTrue({"read_file", "list_files", "search_code"} <= {t["function"]["name"] for t in request["payload"]["tools"]})

    def test_cli_live_repo_source_redacts_known_key_before_capture(self):
        key = "offline-integration-key-never-real"
        (self.repo / "app.py").write_text("value = '" + key + "'\n", encoding="utf-8")
        config_path = self.home / "config.json"
        config_path.write_text(json.dumps(self.config), encoding="utf-8")
        def inspect_workflow(config, packet, root, **kwargs):
            root.mkdir()
            source = kwargs["source_factory"](root)
            self.assertNotIn(key, json.dumps(source.packet))
            self.assertIn("[REDACTED]", json.dumps(source.packet))
            self.assertNotIn(key, "".join(p.read_text(encoding="utf-8") for p in (root / "sources").rglob("*.txt")))
            return {"status": "completed"}
        with patch.object(review, "load_credential", return_value=key), \
             patch("workflow.run_workflow", side_effect=inspect_workflow), \
             patch.object(review.requests, "Session", side_effect=AssertionError("no HTTP")), \
             patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(review.main(["--config", str(config_path), "--repo", str(self.repo), "--output-dir", str(self.out)]), 0)


if __name__ == "__main__":
    unittest.main()
