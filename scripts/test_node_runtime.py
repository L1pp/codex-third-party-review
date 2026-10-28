"""Offline Node setup and readiness tests; no npm network or model calls."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import node_runtime
import skill_runtime


class NodeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.expected = {"@deepseek-ai/dsh-agent-loop": "0.2.1-rc.2", "eventsource-parser": "3.1.0"}
        self.manifest = self.runtime / "package.json"
        self.manifest.write_text(json.dumps({"dependencies": self.expected}), encoding="utf-8")
        self.bridge = self.runtime / "bridge.mjs"
        self.bridge.write_text("// offline marker", encoding="utf-8")
        self.node = self.root / "node location" / ("node.exe" if os.name == "nt" else "node")
        self.node.parent.mkdir()
        self.node.touch()
        self.ready = {"available": True, "ready": True, "executable": str(self.node),
                      "version": "v24.19.0", "official_harness": "0.2.1-rc.2", "dependencies": self.expected}

    def completed(self, text, code=0):
        return SimpleNamespace(returncode=code, stdout=text, stderr="not surfaced")

    def bridge_info(self, **extra):
        return json.dumps({"node": "v24.19.0", "officialHarness": "0.2.1-rc.2", "dependencies": self.expected, **extra})

    def test_explicit_executable_with_spaces_uses_argv_and_is_readonly(self):
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            self.assertNotIn("shell", kwargs)
            return self.completed("v24.19.0\n" if command[-1] == "--version" else self.bridge_info())
        original = self.manifest.read_bytes()
        with patch.object(node_runtime.subprocess, "run", side_effect=run), \
                patch.object(node_runtime, "setup_node_runtime", side_effect=AssertionError("check must not install")):
            value = node_runtime.check_node_runtime(self.root, {"node_executable": str(self.node)})
        self.assertTrue(value["ready"])
        self.assertEqual(value["version"], "v24.19.0")
        self.assertEqual(calls[0][0], [str(self.node), "--version"])
        self.assertEqual(calls[1][0], [str(self.node), str(self.bridge), "--runtime-info"])
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_missing_node_info_reports_false_without_install(self):
        with patch.object(node_runtime.shutil, "which", return_value=None), \
                patch.object(node_runtime.subprocess, "run", side_effect=AssertionError("missing executable")), \
                patch.object(node_runtime, "setup_node_runtime", side_effect=AssertionError("no implicit install")):
            value = node_runtime.node_runtime_info(self.root)
            with self.assertRaises(node_runtime.NodeRuntimeError):
                node_runtime.ensure_node_runtime(self.root, allow_install=False)
        self.assertFalse(value["available"])
        self.assertFalse(value["ready"])

    def test_old_or_malformed_node_rejected_before_bridge_or_installer(self):
        for output in ("v22.19.0", "v24.8.9", "unexpected private diagnostic"):
            with self.subTest(output=output), \
                    patch.object(node_runtime, "node_executable", return_value=str(self.node)), \
                    patch.object(node_runtime.subprocess, "run", return_value=self.completed(output)) as run:
                with self.assertRaises(node_runtime.NodeRuntimeError) as error:
                    node_runtime.check_node_runtime(self.root)
                self.assertNotIn("unexpected private diagnostic", str(error.exception))
                self.assertEqual(run.call_count, 1)

    def test_bridge_version_mismatch_or_bad_output_is_actionable_and_not_raw(self):
        bad_info = {"@deepseek-ai/dsh-agent-loop": "0.0.0", "eventsource-parser": "3.1.0"}
        for body, code in ((self.bridge_info(dependencies=bad_info), 0), ("secret diagnostic", 1), ("[]", 0)):
            with self.subTest(body=body), \
                    patch.object(node_runtime, "node_executable", return_value=str(self.node)), \
                    patch.object(node_runtime.subprocess, "run", side_effect=[self.completed("v24.19.0"), self.completed(body, code)]):
                with self.assertRaisesRegex(node_runtime.NodeRuntimeError, "setup") as error:
                    node_runtime.check_node_runtime(self.root)
                self.assertNotIn("secret diagnostic", str(error.exception))

    def test_missing_package_info_keeps_node_available(self):
        with patch.object(node_runtime, "node_executable", return_value=str(self.node)), \
                patch.object(node_runtime, "check_node_runtime", side_effect=node_runtime.NodeRuntimeError("run setup")):
            value = node_runtime.node_runtime_info(self.root)
        self.assertTrue(value["available"])
        self.assertFalse(value["ready"])
        self.assertIn("setup", value["error"])

    def test_no_install_by_default_and_explicit_live_can_repair(self):
        with patch.object(node_runtime, "check_node_runtime", side_effect=node_runtime.NodeRuntimeError("missing")), \
                patch.object(node_runtime, "setup_node_runtime", return_value=self.ready) as install:
            with self.assertRaises(node_runtime.NodeRuntimeError):
                node_runtime.ensure_node_runtime(self.root)
            install.assert_not_called()
            self.assertEqual(node_runtime.ensure_node_runtime(self.root, allow_install=True), self.ready)
            install.assert_called_once_with(self.root, None)

    def test_ready_setup_does_not_run_npm(self):
        with patch.object(node_runtime, "node_executable", return_value=str(self.node)), \
                patch.object(node_runtime, "_node_version", return_value="v24.19.0"), \
                patch.object(node_runtime, "check_node_runtime", return_value=self.ready), \
                patch.object(node_runtime, "_npm_cli", side_effect=AssertionError("No unnecessary npm lookup")):
            self.assertEqual(node_runtime.setup_node_runtime(self.root), self.ready)

    def test_npm_resolves_javascript_entry_without_running_shims(self):
        npm = self.node.parent / "node_modules/npm/bin/npm-cli.js"
        npm.parent.mkdir(parents=True)
        npm.write_text("// npm fixture", encoding="utf-8")
        with patch.object(node_runtime.shutil, "which", return_value=None):
            self.assertEqual(node_runtime._npm_cli(str(self.node)), str(npm))

    def test_install_is_local_scriptless_shellless_and_stderr_only(self):
        npm = self.node.parent / "node_modules/npm/bin/npm-cli.js"
        original = self.manifest.read_bytes()
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            print("npm install diagnostic", file=kwargs["stdout"])
            return self.completed("")
        with patch.object(node_runtime, "node_executable", return_value=str(self.node)), \
                patch.object(node_runtime, "_node_version", return_value="v24.19.0"), \
                patch.object(node_runtime, "check_node_runtime", side_effect=[node_runtime.NodeRuntimeError("missing"), self.ready]), \
                patch.object(node_runtime, "_npm_cli", return_value=str(npm)), \
                patch.object(node_runtime.subprocess, "run", side_effect=run), \
                redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            result = node_runtime.setup_node_runtime(self.root)
            print(json.dumps({"ready": result["ready"]}))
        command, options = calls[0]
        self.assertEqual(command[:5], [str(self.node), str(npm), "install", "--prefix", str(self.runtime)])
        for option in ("--ignore-scripts", "--no-audit", "--no-fund", "--package-lock=false", "--save-exact"):
            self.assertIn(option, command)
        self.assertNotIn("shell", options)
        self.assertEqual(options["cwd"], self.root)
        self.assertEqual(json.loads(stdout.getvalue()), {"ready": True})
        self.assertIn("npm install diagnostic", stderr.getvalue())
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_bad_manifest_rejected_without_npm(self):
        self.manifest.write_text(json.dumps({"dependencies": {"library": "latest"}}), encoding="utf-8")
        with patch.object(node_runtime, "node_executable", return_value=str(self.node)), \
                patch.object(node_runtime, "_node_version", return_value="v24.19.0"), \
                patch.object(node_runtime.subprocess, "run", side_effect=AssertionError("No npm for invalid manifest")):
            with self.assertRaises(node_runtime.NodeRuntimeError):
                node_runtime.setup_node_runtime(self.root)

    def test_node_child_environment_does_not_inherit_code_injection_options(self):
        source = {"NODE_OPTIONS": "--import foreign", "NODE_PATH": "foreign", "NODE_USE_ENV_PROXY": "1", "HOME": "keep"}
        before = copy.deepcopy(source)
        actual = node_runtime.node_environment(source)
        self.assertEqual(source, before)
        self.assertNotIn("NODE_OPTIONS", actual)
        self.assertNotIn("NODE_PATH", actual)
        self.assertEqual(actual["NODE_USE_ENV_PROXY"], "0")
        self.assertEqual(actual["HOME"], "keep")

    def test_explicit_check_requires_node_but_optional_metadata_does_not(self):
        python_info = {"dependencies": {name: skill_runtime.PINNED_DEPENDENCIES.get(name, "installed") for name in skill_runtime.DEPENDENCIES}}
        with patch.object(skill_runtime.subprocess, "run", return_value=self.completed(json.dumps(python_info))), \
                patch.object(skill_runtime, "_node_info", return_value={"available": False, "ready": False}), \
                patch.object(node_runtime, "check_node_runtime", side_effect=node_runtime.NodeRuntimeError("Node unavailable")):
            self.assertFalse(skill_runtime.check_runtime(self.root)["node"]["available"])
            with self.assertRaises(node_runtime.NodeRuntimeError):
                skill_runtime.check_runtime(self.root, require_node=True)

    def test_explicit_setup_installs_node_after_python_then_verifies_both(self):
        venv_dir = self.root / ".venv"
        venv_dir.mkdir()
        (venv_dir / "pyvenv.cfg").write_text("home = fixture", encoding="utf-8")
        executable = skill_runtime.runtime_python(self.root)
        executable.parent.mkdir(parents=True, exist_ok=True)
        executable.touch()
        assets = self.root / "assets/deepseek_v4_tokenizer"
        assets.mkdir(parents=True)
        for name in ("tokenizer.json", "tokenizer_config.json"):
            (assets / name).write_text("{}", encoding="utf-8")
        config = self.root / "config.json"
        config.write_text("personal configuration marker", encoding="utf-8")
        sequence = []
        result = {"python": "fixture", "node": self.ready}
        with patch.object(skill_runtime, "_install_dependencies", side_effect=lambda root: sequence.append("python")), \
                patch.object(node_runtime, "setup_node_runtime", side_effect=lambda root: sequence.append("node")), \
                patch.object(skill_runtime, "check_runtime", side_effect=lambda root, require_node: sequence.append("check") or result) as check:
            self.assertEqual(skill_runtime.setup_runtime(self.root), result)
        self.assertEqual(sequence, ["python", "node", "check"])
        check.assert_called_once_with(self.root, require_node=True)
        self.assertEqual(config.read_text(encoding="utf-8"), "personal configuration marker")
        self.assertTrue((venv_dir / "skill-runtime.json").is_file())

    def test_explicit_check_cli_requires_node_and_preserves_json_stdout(self):
        with patch.object(skill_runtime, "check_runtime", return_value={"node": self.ready}) as check, \
                redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(skill_runtime.main(["check"]), 0)
        check.assert_called_once_with(require_node=True)
        self.assertTrue(json.loads(stdout.getvalue())["node"]["ready"])

    def test_runtime_info_missing_node_is_structured_and_does_not_install(self):
        with patch.object(skill_runtime, "_node_info", return_value={"available": False, "ready": False}), \
                patch.object(node_runtime, "setup_node_runtime", side_effect=AssertionError("Metadata never installs")):
            value = skill_runtime.runtime_info(self.root)
        self.assertFalse(value["node"]["available"])
        self.assertIn("python", value)


if __name__ == "__main__":
    unittest.main()
