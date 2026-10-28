"""Offline catalog limits, wire parameters and official-engine handoff tests."""
import copy
import json
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import review
from harness_runner import build_engine_spec
from node_runtime import node_environment

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

    def test_known_provider_ceilings_tighten_route_wire_and_preserve_attribution(self):
        config = self.config(cache_for(context=10000, output=5000))
        route = review.select_route(config, NIGHT)
        self.assertEqual(route["context_window_tokens"], 10000)
        self.assertEqual(route["max_input_tokens"], 10000)
        self.assertEqual(route["max_tokens"], 5000)
        messages = [{"role": "user", "content": "Review supplied source."}]
        payload = review.make_payload({}, route, chat_messages=messages)
        self.assertEqual(payload["max_tokens"], 5000)
        catalog = route["model_catalog"]
        self.assertEqual(catalog["provider_limits"], {
            "contextWindow": 10000, "maxInputTokens": 10000, "maxOutputTokens": 5000})
        self.assertEqual(catalog["effective_limits"], {
            "context_window_tokens": 10000, "max_input_tokens": 10000, "max_tokens": 5000})
        self.assertEqual(catalog["source"]["type"], "bailian-cli-public-model-catalog")
        self.assertEqual(catalog["source"]["cli_version"], "2.5.1")

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

    def test_official_runtime_compaction_wire_obeys_provider_and_smaller_user_caps(self):
        # Pressure and continuation transactions are tested in runtime/test_context.mjs.
        # This crosses the catalog -> selected route -> actual Node wire boundary.
        root = Path(__file__).resolve().parent.parent
        node = shutil.which("node")
        if not node or not (root / "runtime/node_modules/@deepseek-ai/dsh-llm/package.json").is_file():
            self.skipTest("Node runtime dependencies are not installed; run Skill setup")
        program = """import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const {buildPayload}=await import(pathToFileURL(process.argv[1]).href);
const route=JSON.parse(readFileSync(0,'utf8'));
const options={provider:'review',model:route.model,purpose:'compaction',maxTokens:16384,
  reasoningEffort:'none',messages:[{role:'user',content:[{type:'text',text:'Review supplied source.'}]}]};
console.log(JSON.stringify(buildPayload(options,route)));
"""
        for user_cap, expected in [(393216, 5000), (1024, 1024)]:
            with self.subTest(user_cap=user_cap):
                config = self.config(cache_for(output=5000))
                config["night"]["max_tokens"] = user_cap
                route = review.select_route(config, NIGHT)
                result = subprocess.run(
                    [node, "--input-type=module", "-e", program, str(root / "runtime/adapter.mjs")],
                    input=json.dumps(route), capture_output=True, text=True, encoding="utf-8",
                    env=node_environment(), cwd=root, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                wire = json.loads(result.stdout)
                self.assertEqual(wire["max_tokens"], expected)
                self.assertFalse(wire["enable_thinking"])
                self.assertEqual(wire["reasoning_effort"], "none")

    def test_configured_context_and_catalog_provenance_reach_official_engine_spec(self):
        config = self.config(cache_for())
        config["context_window_tokens"] = 10000
        config["night"]["max_tokens"] = 1024
        route = review.select_route(config, NIGHT)
        with tempfile.TemporaryDirectory() as folder:
            spec = build_engine_spec(config, Path(folder), route,
                                     [{"role": "user", "content": "Review supplied source."}], [])
        self.assertEqual(spec["route"]["context_window_tokens"], 10000)
        self.assertEqual(spec["route"]["max_tokens"], 1024)
        self.assertEqual(spec["route"]["model_catalog"]["provider_limits"]["contextWindow"], 1000000)
        self.assertEqual(spec["route"]["model_catalog"]["effective_limits"]["context_window_tokens"], 10000)
        self.assertEqual(spec["route"]["model_catalog"]["source"]["cli_version"], "2.5.1")


if __name__ == "__main__":
    unittest.main()
