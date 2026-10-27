"""Offline tests for local token estimation and bounded material tools."""

import builtins
import copy
import importlib.util
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import context_budget
from materials import MaterialStore


def _native_skip_reason():
    missing_dependencies = [name for name in ("tokenizers", "jinja2") if importlib.util.find_spec(name) is None]
    missing_assets = [name for name in ("tokenizer.json", "tokenizer_config.json")
                      if not (context_budget._TOKENIZER_DIR / name).is_file()]
    if missing_dependencies or missing_assets:
        return "Optional native tokenizer unavailable: " + ", ".join(missing_dependencies + missing_assets)
    return None


_NATIVE_SKIP_REASON = _native_skip_reason()


class ContextBudgetTests(unittest.TestCase):
    def test_plan_does_not_mutate_messages_or_tools(self):
        messages = [{"role": "user", "content": "保留中文  和空格"}]
        tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
        before = copy.deepcopy((messages, tools))
        result = context_budget.plan_budget(
            messages,
            tools,
            {"model": "auto", "max_tokens": None},
            {"context_window_tokens": 1_000_000, "context_management": {"safety_tokens": 2048}},
        )
        self.assertEqual((messages, tools), before)
        self.assertEqual(result["requested_output_tokens"], 393_216)

    def test_split_thinking_and_body_caps_are_counted_as_one_total(self):
        route = {
            "model": "qwen3.8-flash",
            "output_limit_parameter": "max_tokens",
            "max_tokens": 131_072,
            "thinking_budget": 262_144,
            "max_input_tokens": 983_616,
        }
        result = context_budget.plan_budget(
            [{"role": "user", "content": "small request"}], [], route,
            {"context_window_tokens": 1_000_000, "context_management": {"safety_tokens": 2048, "compression_trigger_ratio": 0.9}},
        )
        self.assertTrue(result["split_output_budget"])
        self.assertEqual(result["requested_output_tokens"], 393_216)
        self.assertEqual(result["effective_output_tokens"], 393_216)
        self.assertEqual(result["requested_body_tokens"], 131_072)
        self.assertEqual(result["effective_body_tokens"], 131_072)
        self.assertEqual(result["requested_thinking_tokens"], 262_144)
        self.assertEqual(result["effective_thinking_tokens"], 262_144)
        self.assertEqual(result["effective_max_tokens"], 131_072)
        self.assertEqual(result["effective_thinking_budget"], 262_144)
        self.assertEqual(result["compression_trigger_tokens"], 900_000)
        self.assertFalse(result["needs_compaction"])

    def test_explicit_cap_overrides_split_total_and_keeps_wire_components(self):
        result = context_budget.plan_budget(
            [{"role": "user", "content": "small"}], [],
            {"model": "qwen3.8-max", "output_limit_parameter": "max_tokens", "max_tokens": 131_072, "thinking_budget": 262_144},
            {"context_window_tokens": 1_000_000, "context_management": {"safety_tokens": 2048}},
            output_cap=1_000,
        )
        self.assertEqual(result["requested_output_tokens"], 1_000)
        self.assertEqual(result["effective_output_tokens"], 1_000)
        self.assertEqual(result["effective_body_tokens"] + result["effective_thinking_tokens"], 1_000)
        self.assertLessEqual(result["effective_body_tokens"], 131_072)
        self.assertLessEqual(result["effective_thinking_tokens"], 262_144)

    def test_split_budget_requires_two_tokens_before_a_request(self):
        messages = [{"role": "user", "content": ""}]
        input_tokens = context_budget.estimate_tokens(messages, [], "auto")["input_tokens"]
        result = context_budget.plan_budget(
            messages, [],
            {"model": "qwen3.8-flash", "output_limit_parameter": "max_tokens", "max_tokens": 100, "thinking_budget": 200},
            {"context_window_tokens": input_tokens + 1, "context_management": {"safety_tokens": 0, "compression_trigger_ratio": 1.0}},
        )
        self.assertEqual(result["remaining_tokens"], 1)
        self.assertTrue(result["needs_compaction"])
        self.assertEqual(result["effective_output_tokens"], 0)

    def test_deepseek_max_tokens_remains_single_total_cap(self):
        result = context_budget.plan_budget(
            [{"role": "user", "content": "short"}], [],
            {"model": "deepseek-v4.1-flash-1017", "max_tokens": 393_216},
            {"context_window_tokens": 1_000_000, "context_management": {"safety_tokens": 2048, "compression_trigger_ratio": 0.9}},
        )
        self.assertFalse(result["split_output_budget"])
        self.assertEqual(result["requested_output_tokens"], 393_216)
        self.assertEqual(result["effective_output_tokens"], 393_216)

    @unittest.skipIf(_NATIVE_SKIP_REASON is not None, _NATIVE_SKIP_REASON or "")
    def test_deepseek_native_tokenizer_round_trips_chinese(self):
        tokenizer, template, bos = context_budget._load_deepseek_assets()
        text = "中文，保留重复空格  和符号[]"
        encoded = tokenizer.encode(text, add_special_tokens=False)
        self.assertGreater(len(encoded.ids), 0)
        self.assertEqual(tokenizer.decode(encoded.ids), text)
        messages = [{"role": "user", "content": text}]
        result = context_budget.estimate_tokens(messages, [], "deepseek-v4.1-flash-1017")
        self.assertEqual(result["method"], "deepseek_v4_official_tokenizer_template_plus_tool_format_estimate")
        self.assertGreater(result["input_tokens"], 0)
        rendered = template.render(messages=messages, bos_token=bos, add_generation_prompt=True)
        expected_chat_tokens = len(tokenizer.encode(rendered, add_special_tokens=False).ids)
        self.assertEqual(result["chat_tokens"], expected_chat_tokens)
        text_count = context_budget.estimate_text_tokens(text, "deepseek-v4.1-flash-1017")
        text_prefix = context_budget.estimate_text_prefix(text, text_count["tokens"], "deepseek-v4.1-flash-1017")
        self.assertTrue(text_prefix["complete"])
        self.assertEqual(text_prefix["text"], text)

    @unittest.skipIf(_NATIVE_SKIP_REASON is not None, _NATIVE_SKIP_REASON or "")
    def test_deepseek_counts_reasoning_and_tool_json_separately(self):
        base = [{
            "role": "assistant", "content": None, "reasoning_content": "额外思考",
            "tool_calls": [{"type": "function", "function": {"name": "read_material", "arguments": "{\"path\":\"src/a.py\"}"}}],
        }]
        before = copy.deepcopy(base)
        tools = [{"type": "function", "function": {"name": "read_material", "parameters": {"type": "object"}}}]
        result = context_budget.estimate_tokens(base, tools, "deepseek-v4.1-flash-1017")
        self.assertEqual(base, before)
        self.assertGreater(result["reasoning_tokens"], 0)
        self.assertGreater(result["tool_schema_tokens"], 0)
        self.assertGreater(result["tool_call_tokens"], 0)
        self.assertEqual(
            result["input_tokens"],
            result["chat_tokens"] + result["tool_schema_tokens"] + result["tool_call_tokens"] + result["reasoning_tokens"] + result["unrendered_role_tokens"],
        )

    def test_auto_method_is_explicit_utf8_upper_bound(self):
        messages = [{"role": "user", "content": "中文和emoji🙂"}]
        tools = [{"type": "function", "function": {"name": "x"}}]
        result = context_budget.estimate_tokens(messages, tools, "auto")
        byte_count = sum(len(context_budget._compact_json(item).encode("utf-8")) for item in messages)
        byte_count += len(context_budget._compact_json(tools).encode("utf-8"))
        self.assertEqual(result["method"], "utf8_byte_upper_bound_plus_message_overhead")
        self.assertGreaterEqual(result["input_tokens"], byte_count)

    def test_deepseek_missing_tokenizer_uses_explicit_fallback_without_mutation(self):
        messages = [{"role": "user", "content": "中文请求"}]
        tools = [{"type": "function", "function": {"name": "read_material"}}]
        before = copy.deepcopy((messages, tools))
        with patch.object(context_budget, "_load_deepseek_assets", side_effect=RuntimeError("missing local tokenizer")):
            estimate = context_budget.estimate_tokens(messages, tools, "deepseek-v4.1-flash-1017")
            plan = context_budget.plan_budget(
                messages, tools, {"model": "deepseek-v4.1-flash-1017"},
                {"context_window_tokens": 1_000_000, "context_management": {"safety_tokens": 2048}},
            )
        self.assertEqual((messages, tools), before)
        self.assertEqual(estimate["method"], "deepseek_utf8_upper_bound_tokenizer_unavailable")
        self.assertIn("不是原生分词结果", estimate["count_warning"])
        self.assertEqual(plan["method"], estimate["method"])
        self.assertEqual(plan["count_warning"], estimate["count_warning"])

        with patch.object(context_budget, "_load_deepseek_assets", side_effect=RuntimeError("missing local tokenizer")):
            with self.assertRaises(TypeError):
                context_budget.estimate_tokens([None], [], "deepseek-v4.1-flash-1017")

    def test_utf8_fallback_counts_chinese_bytes_and_selects_prefix_by_same_rule(self):
        text = "汉" * 4
        with patch.object(context_budget, "_load_deepseek_assets", side_effect=RuntimeError("missing local tokenizer")):
            count = context_budget.estimate_text_tokens(text, "auto")
            prefix = context_budget.estimate_text_prefix(text, 2, "auto")
        self.assertEqual(count["tokens"], 3)
        self.assertEqual(count["method"], "utf8_bytes_per_4_tokens_fallback")
        self.assertIn("UTF-8字节数", count["count_warning"])
        self.assertEqual(prefix["text"], "汉汉")
        self.assertEqual(prefix["tokens"], 2)
        self.assertEqual(prefix["next_char_offset"], 2)

    def test_over_budget_returns_compaction_plan_instead_of_raising(self):
        result = context_budget.plan_budget(
            [{"role": "user", "content": "这段内容很长" * 80}], [], {"model": "auto"},
            {"context_window_tokens": 32, "context_management": {"safety_tokens": 4}},
            output_cap=20,
        )
        self.assertTrue(result["needs_compaction"])
        self.assertEqual(result["effective_output_tokens"], 0)
        self.assertLess(result["remaining_tokens"], 0)

    def test_output_cap_precedence_and_available_space(self):
        result = context_budget.plan_budget(
            [{"role": "user", "content": "hi"}], [], {"model": "auto", "max_tokens": 50},
            {"context_window_tokens": 1000, "context_management": {"safety_tokens": 10, "auto_output_reserve_tokens": 80}},
            output_cap=40,
        )
        self.assertEqual(result["requested_output_tokens"], 40)
        self.assertEqual(result["effective_output_tokens"], 40)
        self.assertFalse(result["needs_compaction"])

        estimate = context_budget.estimate_tokens([{"role": "user", "content": "hi"}], [], "auto")["input_tokens"]
        limited = context_budget.plan_budget(
            [{"role": "user", "content": "hi"}], [], {"model": "auto"},
            {"context_window_tokens": estimate + 10 + 10, "context_management": {"safety_tokens": 10, "compression_trigger_ratio": 1.0}},
            output_cap=20,
        )
        self.assertEqual(limited["remaining_tokens"], 10)
        self.assertEqual(limited["effective_output_tokens"], 10)
        self.assertTrue(limited["output_limited_by_context"])
        self.assertFalse(limited["needs_compaction"])

    def test_89_percent_input_limits_output_without_triggering_compaction(self):
        context_limit = 10_000
        base = context_budget.estimate_tokens([{"role": "user", "content": ""}], [], "auto")["input_tokens"]
        target_input = 8_900
        messages = [{"role": "user", "content": "x" * (target_input - base)}]
        result = context_budget.plan_budget(
            messages, [], {"model": "auto"},
            {"context_window_tokens": context_limit, "context_management": {"safety_tokens": 10, "compression_trigger_ratio": 0.9}},
            output_cap=384,
        )
        self.assertEqual(result["input_tokens"], target_input)
        self.assertEqual(result["compression_trigger_tokens"], 9_000)
        self.assertEqual(result["compression_trigger_ratio"], 0.9)
        self.assertEqual(result["effective_output_tokens"], 90)
        self.assertTrue(result["output_limited_by_context"])
        self.assertFalse(result["needs_compaction"])

    def test_90_percent_threshold_triggers_compaction_and_ratio_is_configurable(self):
        base = context_budget.estimate_tokens([{"role": "user", "content": ""}], [], "auto")["input_tokens"]
        messages = [{"role": "user", "content": "x" * (8_990 - base)}]
        reached = context_budget.plan_budget(
            messages, [], {"model": "auto"},
            {"context_window_tokens": 10_000, "context_management": {"safety_tokens": 10, "compression_trigger_ratio": 0.9}},
            output_cap=384,
        )
        self.assertTrue(reached["needs_compaction"])
        self.assertEqual(reached["remaining_tokens"], 0)

        configured = context_budget.plan_budget(
            [{"role": "user", "content": "short"}], [], {"model": "auto"},
            {"context_window_tokens": 10_000, "context_management": {"safety_tokens": 10, "compression_trigger_ratio": 0.8}},
            output_cap=100,
        )
        self.assertEqual(configured["compression_trigger_tokens"], 8_000)
        self.assertEqual(configured["compression_trigger_ratio"], 0.8)


class MaterialStoreTests(unittest.TestCase):
    @staticmethod
    def character_counter(text, model):
        return {"tokens": len(text), "method": "test_character_counter"}

    def test_index_has_metadata_only_and_conflicting_snapshots_are_kept(self):
        store = MaterialStore({
            "task": "fix behavior",
            "requirements": "preserve existing API",
            "files": [
                {"path": "src/a.py", "content": "first\n"},
                {"path": "src/a.py", "content": "first\n"},
                {"path": "src/a.py", "content": "different\n"},
            ],
            "diff": "--- a\n+++ b\n",
        })
        index = store.index()
        self.assertEqual(len(index), 3)
        self.assertEqual(index[0]["path"], "src/a.py")
        self.assertEqual(index[1]["path"], "src/a.py#snapshot-2")
        self.assertIn("冲突", index[1]["note"])
        self.assertNotIn("content", index[0])
        self.assertEqual(index[2]["path"], "@diff")

    def test_task_context_omits_materials_review_conclusions_and_failure_logs(self):
        store = MaterialStore({
            "task": "fix parser",
            "requirements": "support unicode",
            "tests": {"plan": "run parser tests", "stdout": "FAIL stack trace", "result": "failed"},
            "main_gpt_conclusion": "approved",
            "diff": "secret diff body",
            "files": [{"path": "a.py", "content": "secret source"}],
        })
        context = store.task_context()
        rendered = str(context)
        self.assertIn("support unicode", rendered)
        self.assertIn("run parser tests", rendered)
        self.assertNotIn("FAIL stack trace", rendered)
        self.assertNotIn("approved", rendered)
        self.assertNotIn("secret source", rendered)
        self.assertNotIn("secret diff body", rendered)

    def test_long_requirements_have_explicit_pointer_and_recover_by_pages(self):
        requirements = "验收条件😀  " * 900
        store = MaterialStore({"requirements": requirements}, max_chars=513, max_lines=10)
        context = store.task_context()
        pointer = context["requirements"]
        self.assertTrue(pointer["has_more"])
        self.assertEqual(pointer["material_path"], "@requirements")
        self.assertEqual(len(pointer["preview"]), 1200)

        metadata = next(item for item in store.index() if item["path"] == "@requirements")
        self.assertEqual(metadata["kind"], "metadata")
        self.assertEqual(metadata["source_field"], "requirements")

        chunks = []
        cursor = {"path": pointer["material_path"]}
        while True:
            page = store.execute("read_material", cursor)
            self.assertNotIn("error", page)
            chunks.append(page["text"])
            if not page["has_more"]:
                break
            cursor = {
                "path": page["path"],
                "start_line": page["next_start_line"],
                "char_offset": page["next_char_offset"],
            }
        self.assertEqual("".join(chunks), requirements)

    def test_test_results_are_indexed_and_readable_but_not_inlined(self):
        test_results = "TEST_RESULT_SECRET\n" + "PASS output repeated  " * 80
        store = MaterialStore({"task": "verify", "test_results": test_results}, max_chars=47, max_lines=20)
        context = store.task_context()
        pointer = context["test_results"]
        self.assertTrue(pointer["has_more"])
        self.assertEqual(pointer["material_path"], "@test_results")
        self.assertNotIn(test_results, str(context))
        self.assertNotIn("TEST_RESULT_SECRET", str(context))

        metadata = next(item for item in store.index() if item["path"] == "@test_results")
        self.assertEqual(metadata["kind"], "metadata")
        self.assertEqual(metadata["source_field"], "test_results")
        page = store.execute("read_material", {"path": pointer["material_path"]})
        self.assertEqual(page["text"], test_results[:47])

    def test_path_forgery_is_rejected_without_opening_any_path(self):
        store = MaterialStore({"files": [{"path": "src/safe.py", "content": "snapshot"}]})
        with patch.object(builtins, "open", side_effect=AssertionError("must not open paths")):
            result = store.execute("read_material", {"path": "../../secret.txt"})
            self.assertIn("error", result)
            absolute = store.execute("read_material", {"path": "C:\\Users\\someone\\secret.txt"})
            self.assertIn("error", absolute)
            valid = store.execute("read_material", {"path": "src/safe.py"})
        self.assertEqual(valid["text"], "snapshot")

    def test_read_paginates_without_losing_unicode_or_repeated_spaces(self):
        text = "中文  A\n第二  行\n"
        store = MaterialStore({"files": [{"path": "unicode.txt", "content": text}]}, max_chars=6, max_lines=10)
        first = store.execute("read_material", {"path": "unicode.txt"})
        self.assertEqual(first["text"], text[:6])
        self.assertTrue(first["has_more"])
        second = store.execute("read_material", {
            "path": first["path"],
            "start_line": first["next_start_line"],
            "line_count": 10,
            "char_offset": first["next_char_offset"],
        })
        self.assertEqual(first["text"] + second["text"], text)
        self.assertEqual(second["line_numbers"][0], first["next_start_line"])

    def test_single_long_line_uses_character_cursor(self):
        text = "甲乙丙丁戊己庚辛"
        store = MaterialStore({"files": [{"path": "long.txt", "content": text}]}, max_chars=4, max_lines=3)
        first = store.execute("read_material", {"path": "long.txt"})
        self.assertEqual(first["text"], "甲乙丙丁")
        self.assertEqual(first["next_start_line"], 1)
        self.assertEqual(first["next_char_offset"], 4)
        second = store.execute("read_material", {
            "path": "long.txt", "start_line": first["next_start_line"], "char_offset": first["next_char_offset"]
        })
        self.assertEqual(first["text"] + second["text"], text)

    def test_search_is_literal_and_capped_at_twenty(self):
        content = "\n".join("a[b] repeated" for _ in range(30))
        store = MaterialStore({"files": [{"path": "literal.txt", "content": content}]})
        result = store.execute("search_material", {"query": "a[b]", "limit": 80})
        self.assertEqual(len(result["matches"]), 20)
        self.assertTrue(result["has_more"])
        self.assertTrue(all("a[b]" in item["snippet"] for item in result["matches"]))

        multiline = MaterialStore({"files": [{"path": "multiline.txt", "content": "first\nsecond"}]})
        cross_line = multiline.execute("search_material", {"query": "first\nsecond"})
        self.assertEqual(len(cross_line["matches"]), 1)
        self.assertEqual(cross_line["matches"][0]["line"], 1)

    def test_tool_schemas_and_argument_allowlist(self):
        store = MaterialStore({"files": []})
        schemas = store.tools()
        self.assertEqual([item["function"]["name"] for item in schemas], ["list_materials", "read_material", "search_material"])
        self.assertIn("error", store.execute("list_materials", {"offset": 0, "extra": "ignored?"}))
        self.assertIn("error", store.execute("unknown", {}))

    def test_inline_materials_fully_inlines_priority_materials_without_mutation(self):
        packet = {
            "task": "repair pagination",
            "requirements": "keep API stable",
            "test_results": "PASS",
            "diff": "tiny diff",
            "files": [{"path": "src/a.py", "content": "source"}],
        }
        before = copy.deepcopy(packet)
        store = MaterialStore(packet)
        result = store.inline_materials(token_budget=10_000, model="auto", counter=self.character_counter)
        self.assertTrue(result["fully_inlined"])
        self.assertFalse(result["needs_tools"])
        self.assertEqual([item["path"] for item in result["materials"]], ["@test_results", "@diff", "src/a.py"])
        self.assertEqual(result["task_context"]["requirements"], "keep API stable")
        self.assertEqual((packet, store._packet), (before, before))

    def test_empty_file_is_present_in_inline_output_even_with_zero_text_budget(self):
        packet = {"files": [{"path": "deleted-or-empty.txt", "content": ""}]}
        before = copy.deepcopy(packet)
        store = MaterialStore(packet)
        result = store.inline_materials(token_budget=0, model="auto", counter=lambda _text, _model: 0)
        self.assertEqual(len(result["materials"]), 1)
        self.assertEqual(result["materials"][0]["path"], "deleted-or-empty.txt")
        self.assertEqual(result["materials"][0]["kind"], "file")
        self.assertEqual(result["materials"][0]["text"], "")
        self.assertEqual(result["materials"][0]["chars"], 0)
        self.assertEqual(result["materials"][0]["lines"], 0)
        self.assertTrue(result["fully_inlined"])
        self.assertFalse(result["needs_tools"])
        self.assertEqual(result["remaining_materials"], [])
        self.assertEqual((packet, store._packet), (before, before))

    def test_inline_materials_prioritizes_metadata_diff_then_files_across_overflow(self):
        packet = {
            "requirements": "R" * 1_500,
            "diff": "D" * 7,
            "files": [
                {"path": "a.py", "content": "A" * 7},
                {"path": "b.py", "content": "B" * 5},
            ],
        }
        before = copy.deepcopy(packet)
        store = MaterialStore(packet)
        result = store.inline_materials(token_budget=1_514, model="auto", counter=self.character_counter)
        self.assertEqual([item["path"] for item in result["materials"]], ["@requirements", "@diff", "a.py"])
        self.assertEqual(result["materials"][0]["text"], "R" * 1_500)
        self.assertEqual(result["materials"][1]["text"], "D" * 7)
        self.assertEqual(result["materials"][2]["text"], "A" * 5)
        self.assertFalse(result["fully_inlined"])
        self.assertTrue(result["needs_tools"])
        self.assertEqual(result["remaining_materials"][0]["path"], "a.py")
        self.assertEqual(result["remaining_materials"][0]["cursor"], {"start_line": 1, "char_offset": 5})
        self.assertEqual(result["remaining_materials"][1]["path"], "b.py")
        self.assertEqual(result["remaining_materials"][1]["cursor"], {"start_line": 1, "char_offset": 0})
        self.assertEqual((packet, store._packet), (before, before))

    def test_inline_file_prefix_cursor_preserves_unicode_on_long_single_line(self):
        text = "甲乙🙂丙丁"
        packet = {"files": [{"path": "long.txt", "content": text}]}
        before = copy.deepcopy(packet)
        store = MaterialStore(packet)
        result = store.inline_materials(token_budget=5, model="auto", counter=self.character_counter)
        item = result["materials"][0]
        self.assertEqual(item["text"], "甲乙🙂")
        self.assertEqual(result["remaining_materials"][0]["cursor"], {"start_line": 1, "char_offset": 3})
        page = store.execute("read_material", {
            "path": "long.txt",
            "start_line": result["remaining_materials"][0]["cursor"]["start_line"],
            "char_offset": result["remaining_materials"][0]["cursor"]["char_offset"],
        })
        self.assertEqual(item["text"] + page["text"], text)
        self.assertEqual((packet, store._packet), (before, before))

    def test_inline_materials_uses_labeled_four_char_fallback_when_tokenizer_missing(self):
        packet = {"files": [{"path": "text.txt", "content": "x" * 30}]}
        before = copy.deepcopy(packet)
        store = MaterialStore(packet)
        with patch.object(context_budget, "_load_deepseek_assets", side_effect=RuntimeError("missing local assets")):
            result = store.inline_materials(token_budget=5, model="auto")
        self.assertEqual(result["method"], "utf8_bytes_per_4_tokens_fallback")
        self.assertIn("UTF-8字节数", result["count_warning"])
        self.assertEqual(result["materials"][0]["text"], "x" * 16)
        self.assertEqual(result["estimated_tokens"], 5)
        self.assertEqual(result["remaining_materials"][0]["cursor"], {"start_line": 1, "char_offset": 16})
        self.assertEqual((packet, store._packet), (before, before))

    def test_inline_end_cursor_is_exclusive_for_line_ending_prefix_and_full_file(self):
        prefix_store = MaterialStore({"files": [{"path": "prefix.txt", "content": "a\nb"}]})
        prefix_result = prefix_store.inline_materials(token_budget=4, model="auto", counter=self.character_counter)
        prefix_item = prefix_result["materials"][0]
        self.assertEqual(prefix_item["text"], "a\n")
        self.assertEqual(prefix_item["lines"], 1)
        self.assertEqual(prefix_item["end_line"], 2)
        self.assertEqual(prefix_item["end_char_offset"], 0)
        self.assertTrue(prefix_item["end_is_exclusive"])
        self.assertEqual(prefix_result["delivered_ranges"][0]["end_line"], 2)
        self.assertTrue(prefix_result["delivered_ranges"][0]["end_is_exclusive"])
        self.assertEqual(prefix_result["remaining_materials"][0]["cursor"], {"start_line": 2, "char_offset": 0})

        full_store = MaterialStore({"files": [{"path": "full.txt", "content": "a\n"}]})
        full_result = full_store.inline_materials(token_budget=4, model="auto", counter=self.character_counter)
        full_item = full_result["materials"][0]
        self.assertEqual(full_item["text"], "a\n")
        self.assertEqual(full_item["lines"], 1)
        self.assertEqual(full_item["end_line"], 2)
        self.assertEqual(full_item["end_char_offset"], 0)
        self.assertTrue(full_item["end_is_exclusive"])


if __name__ == "__main__":
    unittest.main()
