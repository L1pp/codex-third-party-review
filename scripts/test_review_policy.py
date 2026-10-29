"""Offline policy/config/CLI wiring: no provider requests or real waiting."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import harness_runner
import review
from review_contract import review_system_prompt
from review_policy import review_policy, resume_elapsed


class ReviewPolicyTests(unittest.TestCase):
    def test_defaults_and_explicit_disable_preserve_generation_settings(self):
        self.assertEqual(review_policy({}), {"profile":"standard", "timezone":"Asia/Shanghai", "remind_after_seconds":600, "conclude_after_seconds":1200})
        config={"timezone":"UTC", "review_policy":{"profile":"deep", "remind_after_seconds":0, "conclude_after_seconds":0}, "night":{"reasoning_effort":"max","max_tokens":256000}}
        original=json.dumps(config)
        result=review_policy(config)
        self.assertEqual(result["profile"],"deep")
        self.assertEqual(result["remind_after_seconds"],0)
        self.assertEqual(result["conclude_after_seconds"],0)
        self.assertEqual(json.dumps(config),original)

    def test_invalid_clock_settings_are_local_configuration_errors(self):
        for value in (True,-1,float("inf"),float("nan"),"600"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    review_policy({"review_policy":{"remind_after_seconds":value}})
        for value in ([],False,"",5):
            with self.subTest(settings=value):
                with self.assertRaises(ValueError):review_policy({"review_policy":value})
        with self.assertRaises(ValueError):review_policy({"review_policy":{"profile":"anything"}})
        with self.assertRaises(ValueError):review_policy({"review_policy":{"remind_after_seconds":900,"conclude_after_seconds":600}})
        self.assertEqual(review_policy({"review_policy":{"remind_after_seconds":900,"conclude_after_seconds":0}})["conclude_after_seconds"],0)

    def test_resume_uses_saved_active_elapsed_and_never_wall_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            self.assertEqual(resume_elapsed(root),0)
            (root/"workflow.json").write_text(json.dumps({"started_utc":"2000-01-01T00:00:00Z","seconds":61,"review_timing":{"elapsed_seconds":650}}),encoding="utf-8")
            self.assertEqual(resume_elapsed(root),650)
            (root/"workflow.json").write_text(json.dumps({"seconds":100,"review_timing":{"elapsed_seconds":False}}),encoding="utf-8")
            self.assertEqual(resume_elapsed(root),100)

    def test_engine_receives_profile_clock_policy_and_unchanged_route(self):
        with tempfile.TemporaryDirectory() as temp:
            config={"review_policy":{"profile":"deep"},"timezone":"UTC"}
            route={"model":"fixture","reasoning_effort":"max","max_tokens":256000}
            spec=harness_runner.build_engine_spec(config,Path(temp),route,[{"role":"user","content":"Review source"}],[])
            self.assertEqual(spec["reviewPolicy"]["profile"],"deep")
            self.assertEqual(spec["reviewPolicy"]["remind_after_seconds"],600)
            self.assertEqual(spec["reviewPolicy"]["conclude_after_seconds"],1200)
            self.assertEqual(spec["route"],route)
            self.assertEqual(spec["systemPrompt"],review_system_prompt("deep"))

    def test_cli_profile_override_preserves_clock_thresholds_without_starting_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);config=root/"config.json";packet=root/"packet.json";out=root/"out"
            config.write_text(json.dumps({"review_policy":{"remind_after_seconds":600,"conclude_after_seconds":1200}}),encoding="utf-8")
            packet.write_text(json.dumps({"task":"Review supplied source"}),encoding="utf-8")
            with patch.object(harness_runner,"run_harness_workflow",return_value={"status":"dry_run"}) as run, redirect_stdout(io.StringIO()):
                code=review.main(["--config",str(config),"--packet",str(packet),"--output-dir",str(out),"--review-profile","deep","--dry-run"])
            self.assertEqual(code,0)
            passed=run.call_args.args[0]
            self.assertEqual(passed["review_policy"],{"profile":"deep","remind_after_seconds":600,"conclude_after_seconds":1200})
            self.assertTrue(run.call_args.kwargs["dry_run"])


if __name__ == "__main__":
    unittest.main()
