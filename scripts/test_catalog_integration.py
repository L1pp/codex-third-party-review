"""Offline tests for official catalog metadata in actual routing and budgets."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review
import context_budget
import workflow

EXAMPLE = review.read_json(Path(__file__).resolve().parent.parent / "config.example.json")
NIGHT = "2026-10-26T23:00:00+08:00"
DAY = "2026-10-26T12:00:00+08:00"


def cache_for(model="deepseek-v4.1-flash-1017", context=1000000, output=393216, aliases=None):
    return {"schema_version": 1, "models": [{
        "model": model, "api_model_ids": [model], "aliases": aliases or [],
        "family": "group-deepseek", "contextWindow": context,
        "maxInputTokens": context, "maxOutputTokens": output,
        "predictConfig": [], "source": {"type": "bailian-cli-public-model-catalog", "cli_version": "2.5.1"},
    }]}


class CatalogIntegrationTests(unittest.TestCase):
    def config(self, cache):
        return dict(copy.deepcopy(EXAMPLE), _model_catalog_cache=cache)

    def test_known_provider_ceilings_tighten_route_and_real_wire_budget(self):
        config = self.config(cache_for(context=10000, output=5000))
        config["context_management"]["headroom_tokens"] = 0
        route = review.select_route(config, NIGHT)
        self.assertEqual(route["context_window_tokens"], 10000)
        self.assertEqual(route["max_tokens"], 5000)
        messages = [{"role": "user", "content": "bounded metadata integration " * 10}]
        budget = context_budget.plan_budget(messages, [], route, config)
        self.assertEqual(budget["context_limit"], 10000)
        payload = review.make_payload({}, workflow._review_route_with_budget(route, budget), chat_messages=messages)
        self.assertLessEqual(payload["max_tokens"], 5000)
        self.assertLessEqual(budget["input_tokens"] + payload["max_tokens"] + budget["safety_tokens"], budget["context_limit"])

    def test_catalog_never_expands_an_explicit_smaller_limit(self):
        config = self.config(cache_for())
        config["context_window_tokens"] = 900000
        config["night"]["max_tokens"] = 64000
        route = review.select_route(config, NIGHT)
        self.assertEqual(route["context_window_tokens"], 900000)
        self.assertEqual(route["max_tokens"], 64000)

    def test_auto_is_not_assigned_a_catalog_model_or_family_limit(self):
        config = self.config(cache_for(model="auto", context=10000, output=4096))
        route = review.select_route(config, DAY)
        self.assertNotIn("model_catalog", route)
        self.assertEqual(route["max_tokens"], 131072)
        self.assertEqual(review.make_payload({}, route)["max_completion_tokens"], 393216)

    def test_only_declared_alias_matches_and_wire_model_is_not_switched(self):
        config = self.config(cache_for(aliases=["explicit-review-alias"]))
        config["night"]["model"] = "explicit-review-alias"
        route = review.select_route(config, NIGHT)
        self.assertIn("model_catalog", route)
        self.assertEqual(review.make_payload({}, route)["model"], "explicit-review-alias")
        config["night"]["model"] = "deepseek-flash-1017"
        self.assertNotIn("model_catalog", review.select_route(config, NIGHT))

    def test_relative_cache_load_is_offline_and_does_not_mutate_user_config(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / "models.json").write_text(json.dumps(cache_for()), encoding="utf-8")
            config = dict(copy.deepcopy(EXAMPLE), model_catalog_file="models.json")
            before = copy.deepcopy(config)
            with patch("bailian_catalog.subprocess.run", side_effect=AssertionError("no command allowed")):
                prepared = review.prepare_config(config, folder / "config.json")
                self.assertIn("model_catalog", review.select_route(prepared, NIGHT))
            self.assertEqual(config, before)

    def test_missing_or_invalid_cache_preserves_explicit_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            config = dict(copy.deepcopy(EXAMPLE), model_catalog_file="bad.json")
            for content in [None, "not json", '{"api_key":"unrelated-local-value"}']:
                if content is not None:
                    (folder / "bad.json").write_text(content, encoding="utf-8")
                with self.subTest(content=content), patch("bailian_catalog.subprocess.run", side_effect=AssertionError("no command allowed")):
                    route = review.select_route(review.prepare_config(config, folder / "config.json"), NIGHT)
                    self.assertEqual(route["max_tokens"], 256000)
                    self.assertNotIn("model_catalog", route)

    def test_length_recovery_compaction_obeys_provider_and_smaller_user_caps(self):
        for user_cap, expected in [(393216, 5000), (1024, 1024)]:
            with self.subTest(user_cap=user_cap), tempfile.TemporaryDirectory() as folder:
                config = self.config(cache_for(output=5000))
                config["night"]["max_tokens"] = user_cap
                calls = []
                def fake_call(config, packet, output_dir, **kwargs):
                    route = kwargs["route_override"]
                    wire = review.make_payload({}, route, chat_messages=kwargs["chat_messages"])
                    calls.append((wire, kwargs["token_budget"]))
                    if len(calls) == 1:
                        return {"status": "incomplete", "finish_reason": "length",
                                "content": "partial report", "parse_ok": False}
                    return {"status": "completed", "finish_reason": "stop",
                            "content": "checkpoint" if len(calls) == 2 else '{"findings":[]}',
                            "parse_ok": len(calls) == 3, "parsed_findings": []}
                result = workflow.run_workflow(
                    config, {"task": "review sample", "files": [{"path": "x.py", "content": "value = 1"}]},
                    Path(folder)/"review", review_call=fake_call,
                    route_selector=lambda cfg, at=None: review.select_route(cfg, NIGHT))
                self.assertEqual(result["status"], "completed")
                self.assertEqual(len(calls), 3)
                compact_wire, compact_budget = calls[1]
                self.assertEqual(compact_wire["max_tokens"], expected)
                self.assertEqual(compact_budget["requested_output_tokens"], expected)
                self.assertEqual(compact_budget["effective_output_tokens"], expected)
                self.assertFalse(compact_wire["enable_thinking"])

    def test_an_external_route_cannot_raise_configured_context_budget(self):
        config = copy.deepcopy(EXAMPLE)
        config["context_window_tokens"] = 10000
        route = review.select_route(config, NIGHT)
        route["context_window_tokens"] = 1000000
        budget = context_budget.plan_budget([{"role": "user", "content": "small"}], [], route, config)
        self.assertEqual(budget["context_limit"], 10000)


if __name__ == "__main__":
    unittest.main()
