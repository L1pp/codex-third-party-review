"""Offline history and source-identity regressions; no provider is accessed."""
import copy
import json
import unittest

from history_cleanup import prune_old_reads, split_history
from context_budget import estimate_tokens


HEADER = [{"role": "system", "content": "review"}, {"role": "user", "content": "task"}]


def read_turn(call_id, text, *, path="repo/x.py", root="repo", view="staged", source="source-0001", offset=0, multi=False):
    arguments = {"root": root, "path": path, "start_line": 1, "char_offset": offset}
    calls = [{"id": call_id, "type": "function", "function": {"name": "read_file", "arguments": json.dumps(arguments)}}]
    result = {"path": path, "root": root, "view": view, "source_id": source, "text": text,
              "start_line": 1, "line_numbers": [1], "has_more": False,
              "next_start_line": None, "next_char_offset": None}
    group = [{"role": "assistant", "content": "Finding belongs to " + call_id,
              "reasoning_content": "original private reasoning " + call_id, "tool_calls": calls},
             {"role": "tool", "name": "read_file", "tool_call_id": call_id, "content": json.dumps(result)}]
    if multi:
        group[0]["tool_calls"].append({"id": call_id + "-other", "type": "function", "function": {"name": "list_files", "arguments": "{}"}})
        group.append({"role": "tool", "name": "list_files", "tool_call_id": call_id + "-other", "content": "{}"})
    return group


class HistoryCleanupTests(unittest.TestCase):
    def cost(self, messages):
        return estimate_tokens(messages, [], "auto")["input_tokens"]

    def test_recent_multi_tool_turn_is_kept_verbatim_and_pairs_are_not_split(self):
        old = read_turn("old", "old body")
        recent = read_turn("recent", "new body", multi=True)
        history = HEADER + old + recent
        before = copy.deepcopy(history)
        older, tail = split_history(history, self.cost(recent) + 50, "auto")
        self.assertEqual(older, HEADER + old)
        self.assertEqual(tail, recent)
        self.assertEqual(history, before)
        self.assertEqual(len(tail), 3)

    def test_single_overlarge_turn_is_summarized_whole(self):
        turn = read_turn("huge", "a" * 2000, multi=True)
        older, tail = split_history(HEADER + turn, 100, "auto")
        self.assertEqual(older, HEADER + turn)
        self.assertEqual(tail, [])

    def test_incomplete_or_duplicate_tool_result_cannot_be_retained_as_a_valid_tail(self):
        for turn in (read_turn("broken", "body", multi=True)[:-1],
                     read_turn("broken", "body") + read_turn("broken", "body")[1:]):
            messages = HEADER + read_turn("older", "body") + turn
            with self.subTest(messages=messages):
                older, tail = split_history(messages, 100000, "auto")
                self.assertEqual(older, messages)
                self.assertEqual(tail, [])
                cleaned, counts = prune_old_reads(messages, 100000, "auto")
                self.assertEqual(cleaned, messages)
                self.assertFalse(any(counts.values()))

    def test_only_old_large_read_text_is_excerpted_with_reread_cursor(self):
        old = read_turn("old", "HEAD" + "x" * 16000 + "TAIL", offset=23)
        recent = read_turn("recent", "recent body")
        history = HEADER + old + recent
        before = copy.deepcopy(history)
        cleaned, counts = prune_old_reads(history, self.cost(recent) + 50, "auto")
        self.assertEqual(counts, {"duplicate_reads": 0, "large_reads": 1})
        excerpt = json.loads(cleaned[3]["content"])
        self.assertTrue(excerpt["text"].startswith("HEAD"))
        self.assertTrue(excerpt["text"].endswith("TAIL"))
        detail = excerpt["context_cleanup"]
        self.assertTrue(detail["text_is_excerpt"])
        self.assertEqual(detail["reread"]["arguments"]["char_offset"], 23)
        self.assertEqual(excerpt["source_id"], "source-0001")
        self.assertEqual(cleaned[-2:], recent)
        self.assertEqual(history, before)
        self.assertEqual(cleaned[2], old[0])
        again, second = prune_old_reads(cleaned, self.cost(recent) + 50, "auto")
        self.assertEqual(again, cleaned)
        self.assertFalse(any(second.values()))

    def test_exact_duplicate_old_read_uses_recent_copy_and_keeps_original_messages(self):
        text = "repeat body " * 200
        old = read_turn("old", text)
        recent = read_turn("recent", text)
        history = HEADER + old + recent
        before = copy.deepcopy(history)
        cleaned, counts = prune_old_reads(history, self.cost(recent) + 50, "auto")
        value = json.loads(cleaned[3]["content"])
        self.assertNotIn("text", value)
        self.assertEqual(value["context_cleanup"]["duplicate_of_tool_call_id"], "recent")
        self.assertEqual(counts["duplicate_reads"], 1)
        self.assertEqual(cleaned[-2:], recent)
        self.assertEqual(history, before)

    def test_path_alone_does_not_deduplicate_other_roots_views_versions_or_cursors(self):
        text = "same bytes " * 200
        for kwargs in ({"root": "workspace-1"}, {"view": "worktree"},
                       {"source": "source-0002"}, {"offset": 12}, {"path": "repo/other.py"}):
            with self.subTest(kwargs=kwargs):
                old = read_turn("old", text)
                recent = read_turn("recent", text, **kwargs)
                history = HEADER + old + recent
                cleaned, counts = prune_old_reads(history, self.cost(recent) + 50, "auto")
                self.assertEqual(cleaned, history)
                self.assertEqual(counts["duplicate_reads"], 0)

    def test_same_metadata_but_different_text_is_not_deduplicated(self):
        old = read_turn("old", "A" * 2000)
        recent = read_turn("recent", "B" * 2000)
        cleaned, counts = prune_old_reads(HEADER + old + recent, self.cost(recent) + 50, "auto")
        self.assertEqual(cleaned, HEADER + old + recent)
        self.assertEqual(counts["duplicate_reads"], 0)

    def test_reused_call_id_is_resolved_inside_its_own_turn(self):
        old = read_turn("reused", "A" * 2000, offset=0)
        recent = read_turn("reused", "A" * 2000, offset=2000)
        cleaned, counts = prune_old_reads(HEADER + old + recent, self.cost(recent) + 50, "auto")
        self.assertEqual(cleaned, HEADER + old + recent)
        self.assertFalse(any(counts.values()))

    def test_single_recent_turn_below_budget_is_never_pruned(self):
        history = HEADER + read_turn("only", "x" * 12000)
        cleaned, counts = prune_old_reads(history, self.cost(history) + 50, "auto")
        self.assertEqual(cleaned, history)
        self.assertFalse(any(counts.values()))


if __name__ == "__main__":
    unittest.main()
