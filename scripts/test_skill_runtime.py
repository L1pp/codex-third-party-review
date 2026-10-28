"""Offline environment-isolation and full-output reservation regressions."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from importlib.metadata import PackageNotFoundError
import unittest
from unittest.mock import patch

import context_budget
import skill_runtime


class SkillRuntimeTests(unittest.TestCase):
    def test_child_environment_isolated_without_mutating_parent(self):
        source={"PYTHONPATH":"foreign-package-root","PYTHONHOME":"foreign-python","PYTHONUSERBASE":"foreign-user-site",
                "VIRTUAL_ENV":"foreign-venv","PATH":"original-path","BAILIAN_TOKEN_PLAN_API_KEY":"fake-key","HOME":"keep-home"}
        before=copy.deepcopy(source)
        env=skill_runtime.clean_environment(Path("skill location"),source)
        self.assertEqual(source,before)
        self.assertNotIn("PYTHONPATH",env)
        self.assertNotIn("PYTHONHOME",env)
        self.assertNotIn("PYTHONUSERBASE",env)
        self.assertEqual(env["HOME"],"keep-home")
        self.assertEqual(env["BAILIAN_TOKEN_PLAN_API_KEY"],"fake-key")
        self.assertNotEqual(env["VIRTUAL_ENV"],"foreign-venv")
        self.assertTrue(env["PATH"].endswith("original-path"))

    def test_foreign_interpreter_reexec_preserves_arguments_and_cwd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();entry=root/"scripts/review.py";entry.parent.mkdir();entry.touch()
            exe=skill_runtime.runtime_python(root);exe.parent.mkdir(parents=True);exe.touch()
            with patch.object(skill_runtime,"using_skill_runtime",return_value=False), \
                    patch.object(skill_runtime.os,"execve",side_effect=RuntimeError("reexec intercepted")) as execute, \
                    patch.object(skill_runtime.subprocess,"Popen") as spawn, \
                    patch.object(sys,"argv",[str(entry),"--repo","project with spaces","--dry-run"]):
                spawn.return_value.wait.return_value=0
                cwd=Path.cwd()
                if os.name=="nt":
                    with self.assertRaises(SystemExit) as raised:skill_runtime.launch_in_runtime(entry)
                    self.assertEqual(raised.exception.code,0)
                    args=spawn.call_args.args[0]
                else:
                    with self.assertRaisesRegex(RuntimeError,"reexec intercepted"):skill_runtime.launch_in_runtime(entry)
                    args=execute.call_args.args[1]
            self.assertEqual(Path.cwd(),cwd)
            self.assertEqual(args,[str(exe),"-I",str(entry),"--repo","project with spaces","--dry-run"])

    def test_missing_runtime_exits_before_any_model_or_installer(self):
        with tempfile.TemporaryDirectory() as temporary:
            entry=Path(temporary)/"scripts/review.py";entry.parent.mkdir();entry.touch()
            with patch("sys.stderr",new_callable=io.StringIO), patch.object(skill_runtime.subprocess,"run",side_effect=AssertionError("no install/network")):
                with self.assertRaises(SystemExit) as raised:skill_runtime.launch_in_runtime(entry)
        self.assertEqual(raised.exception.code,2)

    def test_dependency_readiness_includes_pinned_local_io_policy(self):
        versions={name:skill_runtime.PINNED_DEPENDENCIES.get(name,"installed") for name in skill_runtime.DEPENDENCIES}
        with patch("importlib.metadata.version",side_effect=lambda name:versions[name]):
            result=skill_runtime.dependency_versions()
        self.assertIn("tenacity",result)
        self.assertEqual(result["tenacity"],"9.2.1")
        self.assertEqual(len(result),6)

    def test_existing_runtime_repairs_missing_or_wrong_dependency_then_rechecks(self):
        versions={name:skill_runtime.PINNED_DEPENDENCIES.get(name,"installed") for name in skill_runtime.DEPENDENCIES}
        for error in (PackageNotFoundError("tenacity"),RuntimeError("wrong dependency version")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(skill_runtime,"dependency_versions",side_effect=[error,versions]) as checks, \
                    patch.object(skill_runtime,"_install_dependencies") as install:
                result=skill_runtime.ensure_runtime_dependencies(Path("existing skill"),allow_install=True)
                self.assertEqual(result,versions)
                install.assert_called_once_with(Path("existing skill"))
                self.assertEqual(checks.call_count,2)

    def test_offline_dependency_check_missing_or_wrong_version_never_installs(self):
        for error in (PackageNotFoundError("tenacity"),RuntimeError("wrong tenacity version")):
            with self.subTest(error=type(error).__name__), \
                    patch.object(skill_runtime,"dependency_versions",side_effect=error), \
                    patch.object(skill_runtime,"_install_dependencies",side_effect=AssertionError("Offline means no pip")):
                with self.assertRaisesRegex(RuntimeError,"setup"):
                    skill_runtime.ensure_runtime_dependencies(allow_install=False)

    def test_review_offline_flags_disable_installs_and_live_default_allows_them(self):
        for flag in ("--history","--route-only","--runtime-info","--dry-run","--help","-h","--at"):
            with self.subTest(flag=flag):
                self.assertFalse(skill_runtime.review_allows_dependency_install(["--repo","project",flag]))
        self.assertTrue(skill_runtime.review_allows_dependency_install([]))
        self.assertTrue(skill_runtime.review_allows_dependency_install(["--repo","project"]))

    def test_ready_runtime_never_runs_installer(self):
        with patch.object(skill_runtime,"dependency_versions",return_value={"tenacity":"9.2.1"}), \
                patch.object(skill_runtime,"_install_dependencies",side_effect=AssertionError("No extra install")):
            self.assertEqual(skill_runtime.ensure_runtime_dependencies(),{"tenacity":"9.2.1"})

    def test_dependency_installer_output_uses_stderr_preserving_json_stdout(self):
        def run(command,**kwargs):
            self.assertEqual(command[1:6],["-I","-m","pip","install","-r"])
            self.assertTrue(command[6].endswith("requirements-tokenizer.txt"))
            print("pip dependency diagnostic",file=kwargs["stdout"])
            return SimpleNamespace(returncode=0)
        with patch("sys.stdout",new_callable=io.StringIO) as stdout, \
                patch("sys.stderr",new_callable=io.StringIO) as stderr, \
                patch.object(skill_runtime.subprocess,"run",side_effect=run):
            skill_runtime._install_dependencies(Path("existing skill"))
            print(json.dumps({"status":"ready"}))
        self.assertEqual(json.loads(stdout.getvalue()),{"status":"ready"})
        self.assertIn("pip dependency diagnostic",stderr.getvalue())

    def test_readonly_check_rejects_missing_tenacity_without_installing(self):
        versions={name:skill_runtime.PINNED_DEPENDENCIES.get(name,"installed") for name in skill_runtime.DEPENDENCIES if name!="tenacity"}
        completed=SimpleNamespace(returncode=0,stdout=json.dumps({"dependencies":versions}))
        with patch.object(skill_runtime.subprocess,"run",return_value=completed), \
                patch.object(skill_runtime,"_install_dependencies",side_effect=AssertionError("Check is offline")):
            with self.assertRaisesRegex(RuntimeError,"tenacity"):
                skill_runtime.check_runtime()

    def test_isolated_bootstrap_checks_dependencies_even_without_tokenizer_requirement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();entry=root/"scripts/report_diagnostics.py";entry.parent.mkdir();entry.touch()
            exe=skill_runtime.runtime_python(root);exe.parent.mkdir(parents=True);exe.touch()
            with patch.object(skill_runtime,"using_skill_runtime",return_value=True), \
                    patch.object(skill_runtime.sys,"flags",SimpleNamespace(isolated=1)), \
                    patch.object(skill_runtime,"ensure_runtime_dependencies") as ensure:
                skill_runtime.launch_in_runtime(entry,require_tokenizer=False)
            ensure.assert_called_once_with(root,allow_install=False)

    def test_unrepairable_dependencies_exit_before_entry_imports(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary).resolve();entry=root/"scripts/review.py";entry.parent.mkdir();entry.touch()
            exe=skill_runtime.runtime_python(root);exe.parent.mkdir(parents=True);exe.touch()
            with patch.object(skill_runtime,"using_skill_runtime",return_value=True), \
                    patch.object(skill_runtime.sys,"flags",SimpleNamespace(isolated=1)), \
                    patch.object(skill_runtime,"ensure_runtime_dependencies",side_effect=RuntimeError("unavailable")), \
                    patch("sys.stderr",new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as raised:
                    skill_runtime.launch_in_runtime(entry,require_tokenizer=True)
            self.assertEqual(raised.exception.code,2)

    def test_old_budget_is_compacted_instead_of_sending_3661_tokens(self):
        route={"model":"deepseek-v4.1-flash-1017","max_tokens":393216}
        config={"context_window_tokens":1_000_000,"context_management":{"safety_tokens":2048,"compression_trigger_ratio":0.9}}
        with patch.object(context_budget,"estimate_tokens",return_value={"input_tokens":894291,"method":"recorded_upper_bound"}):
            blocked=context_budget.plan_budget([],[],route,config)
        self.assertTrue(blocked["needs_compaction"])
        self.assertEqual(blocked["effective_output_tokens"],0)
        self.assertEqual(blocked["generation_reserve_tokens"],393216)
        with patch.object(context_budget,"estimate_tokens",return_value={"input_tokens":220437,"method":"recorded_actual_usage"}):
            ready=context_budget.plan_budget([],[],route,config)
        self.assertFalse(ready["needs_compaction"])
        self.assertEqual(ready["effective_output_tokens"],393216)

    def test_incompatible_output_configuration_is_explicit_not_low_budget(self):
        with patch.object(context_budget,"estimate_tokens",return_value={"input_tokens":100,"method":"offline"}):
            result=context_budget.plan_budget([],[],{"model":"offline","max_tokens":10000},
                 {"context_window_tokens":5000,"context_management":{"safety_tokens":10}})
        self.assertTrue(result["output_budget_exceeds_context"])
        self.assertTrue(result["needs_compaction"])
        self.assertEqual(result["effective_output_tokens"],0)


if __name__=="__main__":unittest.main()
