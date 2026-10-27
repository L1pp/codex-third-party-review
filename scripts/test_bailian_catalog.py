"""Offline tests for public CLI metadata normalization and optional caching.

All subprocesses are mocked. No credentials, CLI config, network or model calls
are used by this test module.
"""

import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import bailian_catalog as catalog


PAYLOAD = {
    "model": "qwen-family", "name": "A family", "updateAt": "2026-10-20",
    "contextWindow": 9_999_999, "predictConfig": [{"key": "family_only"}],
    "rawConsoleResponse": {"irrelevant": "do not retain"},
    "items": [
        {
            "model": "qwen-concrete", "name": "Concrete", "provider": "alibaba", "versionTag": "MAJOR",
            "aliases": ["qwen-explicit-alias"],
            "contextWindow": 1_000_000, "maxInputTokens": 983_616, "maxOutputTokens": 131_072,
            "prices": [{"irrelevant": True}], "sampleCodeV2": {"irrelevant": "large"},
            "predictConfig": [
                {"name": "Answer limit", "key": "max_tokens", "default": 131_072,
                 "tip": "正文上限", "range": [1, 131_072], "consoleUI": "omit"},
                {"name": "Thinking", "key": "enable_thinking", "default": False},
                {"name": "Zero", "key": "seed", "default": 0, "range": {"min": 0, "max": 100}},
            ],
        },
        {"model": "qwen-concrete-1017", "versionTag": "SNAPSHOT", "maxOutputTokens": 65_536},
    ],
}


def cache_for(payload=PAYLOAD):
    return {
        "schema_version": catalog.SCHEMA_VERSION,
        "source": {"type": catalog.SOURCE_TYPE},
        "models": catalog.normalize_catalog(payload, cli_version="2.5.1", retrieved_at="2026-10-26T00:00:00+00:00"),
    }


class NormalizeTests(unittest.TestCase):
    def test_whitelists_item_metadata_and_each_parameter_field(self):
        original = copy.deepcopy(PAYLOAD)
        result = catalog.normalize_catalog(PAYLOAD, family="requested", cli_version="2.5.1", retrieved_at="now")
        concrete, snapshot = result
        self.assertEqual(concrete["model"], "qwen-concrete")
        self.assertEqual(concrete["api_model_ids"], ["qwen-concrete"])
        self.assertEqual(concrete["aliases"], ["qwen-explicit-alias"])
        self.assertEqual(concrete["family"], "qwen-family")
        self.assertEqual(concrete["contextWindow"], 1_000_000)
        self.assertEqual(concrete["maxInputTokens"], 983_616)
        self.assertEqual(concrete["maxOutputTokens"], 131_072)
        self.assertEqual(concrete["provider"], "alibaba")
        self.assertEqual(concrete["versionTag"], "MAJOR")
        self.assertEqual(concrete["source"]["cli_version"], "2.5.1")
        self.assertEqual(concrete["source"]["requested_family"], "requested")
        self.assertEqual(concrete["source"]["catalog_updated_at"], "2026-10-20")
        self.assertEqual(concrete["source"]["retrieved_at"], "now")
        self.assertEqual(concrete["predictConfig"][0], {
            "name": "Answer limit", "key": "max_tokens", "default": 131_072,
            "tip": "正文上限", "range": [1, 131_072],
        })
        self.assertIs(concrete["predictConfig"][1]["default"], False)
        self.assertEqual(concrete["predictConfig"][2]["default"], 0)
        self.assertNotIn("prices", concrete)
        self.assertNotIn("sampleCodeV2", concrete)
        self.assertNotIn("rawConsoleResponse", concrete)
        self.assertNotIn("contextWindow", snapshot)
        self.assertNotIn("maxInputTokens", snapshot)
        self.assertEqual(snapshot["predictConfig"], [])
        self.assertEqual(PAYLOAD, original)

    def test_grouped_api_ids_and_only_explicit_aliases(self):
        payload = {"model": "family", "items": [{
            "model": "first-id, second-id，third-id", "modelAlias": "official-alias",
            "aliases": ["declared-alias", "first-id", "official-alias"],
            "name": "display-name", "contextWindow": 100,
        }]}
        cache = cache_for(payload)
        record = cache["models"][0]
        self.assertEqual(record["model"], "first-id")
        self.assertEqual(record["api_model_ids"], ["first-id", "second-id", "third-id"])
        self.assertEqual(record["aliases"], ["official-alias", "declared-alias"])
        for model in ("first-id", "second-id", "third-id", "official-alias", "declared-alias"):
            self.assertEqual(catalog.cache_lookup(cache, model)["model"], "first-id")
        for model in ("family", "display-name", "first", "FIRST-ID", "first-id-latest"):
            self.assertIsNone(catalog.cache_lookup(cache, model))

    def test_real_model_alias_string_shape_keeps_date_id_independent(self):
        # The installed official qwen3.8-max group declares modelAlias as a
        # string equal to its MAJOR id; its 0902 item is a separate API id.
        payload = {"model": "group-qwen3.8-max", "items": [
            {"model": "qwen3.8-max", "modelAlias": "qwen3.8-max", "versionTag": "MAJOR"},
            {"model": "qwen3.8-max-0902", "versionTag": "SNAPSHOT"},
        ]}
        cache = {"schema_version": 1, "models": catalog.normalize_catalog(payload, family="qwen3.8-max-0902")}
        self.assertEqual(cache["models"][0]["aliases"], [])
        dated = catalog.cache_lookup(cache, "qwen3.8-max-0902")
        self.assertEqual(dated["model"], "qwen3.8-max-0902")
        self.assertEqual(dated["family"], "group-qwen3.8-max")
        self.assertEqual(dated["source"]["requested_family"], "qwen3.8-max-0902")
        self.assertIsNone(catalog.cache_lookup(cache, "qwen3.8-max-1017"))

    def test_array_grouped_ids_and_unknown_values_are_not_cast_to_ids(self):
        result = catalog.normalize_catalog({"model": "f", "items": [{
            "model": ["one", "two"], "aliases": [None, 12, "alias"],
        }, {"model": 42}, None]})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["api_model_ids"], ["one", "two"])
        self.assertEqual(result[0]["aliases"], ["alias"])

    def test_token_limits_reject_bool_negative_float_but_keep_explicit_zero_output(self):
        result = catalog.normalize_catalog({"model": "f", "items": [
            {"model": "bad", "contextWindow": True, "maxInputTokens": -2, "maxOutputTokens": 123.5},
            {"model": "embedding", "contextWindow": "512", "maxOutputTokens": 0},
        ]})
        self.assertFalse(any(field in result[0] for field in catalog._TOKEN_FIELDS))
        self.assertEqual(result[1]["contextWindow"], 512)
        self.assertEqual(result[1]["maxOutputTokens"], 0)

    def test_invalid_or_empty_shapes_have_safe_codes(self):
        for payload in (None, [], {"groups": []}, {"items": []}, {"model": "f", "items": [None]}):
            with self.subTest(payload=payload):
                with self.assertRaises(catalog.CatalogError):
                    catalog.normalize_catalog(payload)
        self.assertEqual(catalog.normalize_catalog({"items": [{"model": "exact"}]}, family="f")[0]["family"], "f")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "catalog.json"

    def write(self, value):
        self.path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_missing_bad_json_or_schema_returns_none_without_a_process(self):
        with patch.object(catalog.subprocess, "run") as process:
            self.assertIsNone(catalog.load_catalog_cache(self.path))
            self.path.write_text("broken", encoding="utf-8")
            self.assertIsNone(catalog.load_catalog_cache(self.path))
            for value in ([1], {"schema_version": True, "models": []}, {"schema_version": 42, "models": []}, {"schema_version": 1, "models": "wrong"}):
                self.write(value)
                self.assertIsNone(catalog.load_catalog_cache(self.path))
            process.assert_not_called()

    def test_cache_load_keeps_only_known_small_fields_and_handles_bom(self):
        cache = cache_for()
        cache["irrelevantConsole"] = {"large": "ignore"}
        cache["models"][0]["irrelevantConsole"] = {"large": "ignore"}
        self.path.write_text(json.dumps(cache), encoding="utf-8-sig")
        with patch.object(catalog.subprocess, "run") as process:
            loaded = catalog.load_catalog_cache(self.path)
            found = catalog.cache_lookup(loaded, "qwen-concrete")
        self.assertNotIn("irrelevantConsole", loaded)
        self.assertNotIn("irrelevantConsole", found)
        self.assertEqual(found["predictConfig"][0]["tip"], "正文上限")
        self.assertEqual(loaded["families"], ["qwen-family"])
        process.assert_not_called()

    def test_oversized_or_invalid_record_cache_is_ignored(self):
        self.write(cache_for())
        with patch.object(catalog, "MAX_CACHE_BYTES", 2):
            self.assertIsNone(catalog.load_catalog_cache(self.path))
        self.write({"schema_version": 1, "models": [{"model": "x"}]})
        self.assertIsNone(catalog.load_catalog_cache(self.path))

    def test_lookup_copies_metadata_and_auto_never_gets_catalog_guarantees(self):
        cache = cache_for()
        concrete = catalog.cache_lookup(cache, "qwen-concrete")
        concrete["predictConfig"][0]["range"][0] = 999
        self.assertEqual(cache["models"][0]["predictConfig"][0]["range"][0], 1)
        cache["models"][0]["aliases"].extend(["auto", "AUTO"])
        cache["models"][0]["unrelated_console_response"] = {"omit": True}
        self.assertNotIn("unrelated_console_response", catalog.cache_lookup(cache, "qwen-concrete"))
        self.assertIsNone(catalog.cache_lookup(cache, "auto"))
        self.assertIsNone(catalog.cache_lookup(cache, "AUTO"))
        self.assertIsNone(catalog.cache_lookup(cache, "qwen-family"))
        self.assertIsNone(catalog.cache_lookup(cache, "unknown-model"))
        self.assertIsNone(catalog.cache_lookup(None, "qwen-concrete"))

    def test_ambiguous_explicit_alias_returns_none(self):
        cache = cache_for()
        cache["models"][1]["aliases"].append("qwen-explicit-alias")
        self.assertIsNone(catalog.cache_lookup(cache, "qwen-explicit-alias"))


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def npm_install(self, suffix):
        shim = self.root / ("bl" + suffix)
        shim.write_text("fixture shim is never executed", encoding="utf-8")
        package = self.root / "node_modules" / "bailian-cli"
        target = package / "dist" / "bailian.mjs"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture is never executed", encoding="utf-8")
        (package / "package.json").write_text(json.dumps({
            "name": "bailian-cli", "version": "2.5.1", "bin": {"bl": "dist/bailian.mjs"},
        }), encoding="utf-8")
        return shim, target, package

    def test_exe_uses_direct_argv(self):
        executable = self.root / "bl.exe"
        executable.touch()
        with patch.object(catalog.shutil, "which", return_value=str(executable)):
            self.assertEqual(catalog.resolve_cli(), {"argv": [str(executable.resolve())], "version": None})

    def test_npm_ps1_and_cmd_shims_use_declared_bin_and_node_without_a_shell(self):
        node = self.root / "node.exe"
        node.touch()
        for suffix in (".ps1", ".cmd"):
            with self.subTest(suffix=suffix):
                shim, target, package = self.npm_install(suffix)
                with patch.object(catalog.shutil, "which", return_value=str(shim)):
                    resolved = catalog.resolve_cli()
                self.assertEqual(resolved["argv"], [str(node.resolve()), str(target.resolve())])
                self.assertEqual(resolved["version"], "2.5.1")

    def test_missing_cli_missing_node_and_unrecognized_batch_have_safe_codes(self):
        with patch.object(catalog.shutil, "which", return_value=None):
            with self.assertRaisesRegex(catalog.CatalogError, "cli_not_found"):
                catalog.resolve_cli(str(self.root / "absent.exe"))
        shim, target, package = self.npm_install(".cmd")
        with patch.object(catalog.shutil, "which", side_effect=[str(shim), None]):
            with self.assertRaisesRegex(catalog.CatalogError, "node_not_found"):
                catalog.resolve_cli()
        (package / "package.json").unlink()
        with patch.object(catalog.shutil, "which", return_value=str(shim)):
            with self.assertRaisesRegex(catalog.CatalogError, "unsupported_cli_launcher"):
                catalog.resolve_cli()

    def test_package_bin_cannot_escape_its_installed_package(self):
        shim, target, package = self.npm_install(".ps1")
        (package / "package.json").write_text(json.dumps({
            "name": "bailian-cli", "bin": {"bl": "../../outside.mjs"},
        }), encoding="utf-8")
        with patch.object(catalog.shutil, "which", return_value=str(shim)):
            with self.assertRaisesRegex(catalog.CatalogError, "unsupported_cli_launcher"):
                catalog.resolve_cli()


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "cache.json"
        self.path.write_text(json.dumps(cache_for()), encoding="utf-8")
        self.before = self.path.read_bytes()
        self.launcher = {"argv": ["node.exe", "path with spaces/bailian.mjs"], "version": "2.5.1"}

    def completed(self, payload=PAYLOAD, code=0):
        return subprocess.CompletedProcess(["unused"], code, stdout=json.dumps(payload), stderr="private stderr must not leak")

    def test_refresh_uses_official_argv_env_and_writes_only_normalized_cache(self):
        with patch.object(catalog, "resolve_cli", return_value=self.launcher), \
             patch.object(catalog.subprocess, "run", return_value=self.completed()) as run:
            result = catalog.refresh_catalog_cache(self.path, ["qwen-family"], timeout=7)
        self.assertTrue(result["ok"])
        argv = run.call_args.args[0]
        self.assertEqual(argv, self.launcher["argv"] + [
            "model", "list", "--model", "qwen-family", "--enrich", "--output", "json",
        ])
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertEqual(run.call_args.kwargs["timeout"], 7)
        self.assertEqual(run.call_args.kwargs["env"]["NO_COLOR"], "1")
        self.assertEqual(run.call_args.kwargs["env"]["BAILIAN_CALLER_TYPE"], "agent")
        self.assertEqual(run.call_args.kwargs["env"]["BAILIAN_SKILL_NAME"], "bailian-cli")
        written = self.path.read_text(encoding="utf-8")
        self.assertNotIn("rawConsoleResponse", written)
        self.assertNotIn("sampleCodeV2", written)
        self.assertNotIn("private stderr", written)
        self.assertEqual(catalog.load_catalog_cache(self.path)["cli_version"], "2.5.1")
        self.assertEqual(len(list(self.path.parent.glob("*.tmp"))), 0)

    def test_failed_cli_timeout_missing_cli_bad_json_preserve_existing_cache(self):
        cases = [
            (self.completed(code=1), None, "cli_failed"),
            (subprocess.CompletedProcess([], 0, "{broken", "hidden"), None, "invalid_cli_json"),
            (self.completed(payload={"items": []}), None, "empty_catalog_family"),
            (self.completed(payload=[]), None, "unsupported_catalog_shape"),
            (None, subprocess.TimeoutExpired(["bl"], 1), "cli_timeout"),
            (None, FileNotFoundError("credential-like text must stay hidden"), "cli_unavailable"),
        ]
        for completed, failure, expected in cases:
            with self.subTest(expected=expected), \
                 patch.object(catalog, "resolve_cli", return_value=self.launcher), \
                 patch.object(catalog.subprocess, "run", return_value=completed, side_effect=failure):
                result = catalog.refresh_catalog_cache(self.path, "qwen-family")
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], expected)
            self.assertEqual(result["failed_family"], "qwen-family")
            self.assertIsNotNone(result["cache"])
            self.assertEqual(self.path.read_bytes(), self.before)
            self.assertNotIn("credential-like", json.dumps(result))

    def test_cli_absence_does_not_invoke_process_or_remove_usable_cache(self):
        with patch.object(catalog, "resolve_cli", side_effect=catalog.CatalogError("cli_not_found")), \
             patch.object(catalog.subprocess, "run") as run:
            result = catalog.refresh_catalog_cache(self.path, "qwen-family")
        self.assertEqual(result["error"], "cli_not_found")
        self.assertIsNotNone(catalog.cache_lookup(result["cache"], "qwen-concrete"))
        run.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_multi_family_failure_is_all_or_nothing(self):
        with patch.object(catalog, "resolve_cli", return_value=self.launcher), \
             patch.object(catalog.subprocess, "run", side_effect=[self.completed(), self.completed(code=1)]):
            result = catalog.refresh_catalog_cache(self.path, ["qwen-family", "deepseek"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_family"], "deepseek")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_partial_refresh_keeps_other_family_and_replaces_returned_family(self):
        other = {"model": "other-family", "items": [{"model": "other-exact", "contextWindow": 50}]}
        with patch.object(catalog, "resolve_cli", return_value=self.launcher), \
             patch.object(catalog.subprocess, "run", return_value=self.completed(other)):
            result = catalog.refresh_catalog_cache(self.path, ["other-exact"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["cache"]["families"], ["other-family", "qwen-family"])
        self.assertEqual(catalog.cache_lookup(result["cache"], "other-exact")["contextWindow"], 50)
        changed = copy.deepcopy(other)
        changed["items"][0]["contextWindow"] = 75
        with patch.object(catalog, "resolve_cli", return_value=self.launcher), \
             patch.object(catalog.subprocess, "run", return_value=self.completed(changed)):
            result = catalog.refresh_catalog_cache(self.path, ["other-exact"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["cache"]["models"]), 3)
        self.assertEqual(catalog.cache_lookup(result["cache"], "other-exact")["contextWindow"], 75)

    def test_cache_write_failure_and_duplicate_models_preserve_old_file(self):
        with patch.object(catalog, "resolve_cli", return_value=self.launcher), \
             patch.object(catalog.subprocess, "run", return_value=self.completed()), \
             patch.object(catalog, "_write_cache", side_effect=PermissionError("secret local path")):
            result = catalog.refresh_catalog_cache(self.path, ["qwen-family"])
        self.assertEqual(result["error"], "cache_write_failed")
        self.assertEqual(self.path.read_bytes(), self.before)
        with patch.object(catalog, "resolve_cli", return_value=self.launcher), \
             patch.object(catalog.subprocess, "run", return_value=self.completed()):
            result = catalog.refresh_catalog_cache(self.path, ["qwen-family", "qwen-concrete"])
        self.assertEqual(result["error"], "duplicate_catalog_model")
        self.assertEqual(self.path.read_bytes(), self.before)

    def test_invalid_refresh_args_are_local_errors_before_process(self):
        with patch.object(catalog.subprocess, "run") as run:
            for families, timeout in (([], 1), (["--other-flag"], 1), ([None], 1), (["qwen"], 0), (["qwen"], True), (["qwen"], float("nan")), (["qwen"], float("inf"))):
                with self.subTest(families=families, timeout=timeout), self.assertRaises(ValueError):
                    catalog.refresh_catalog_cache(self.path, families, timeout=timeout)
            run.assert_not_called()

    def test_main_lookup_stays_offline(self):
        with patch.object(catalog.subprocess, "run") as run, patch("sys.stdout", new_callable=io.StringIO) as output:
            exit_code = catalog.main(["--cache", str(self.path), "lookup", "--model", "qwen-explicit-alias"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(json.loads(output.getvalue())["matched"])
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
