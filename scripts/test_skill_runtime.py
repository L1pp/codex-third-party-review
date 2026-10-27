"""Offline environment-isolation and full-output reservation regressions."""
import copy
import io
import json
import os
from pathlib import Path
import sys
import tempfile
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
