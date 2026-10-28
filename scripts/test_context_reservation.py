"""Offline boundary checks against published DSH defaults and wire budgets."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import context_budget
import review
import workflow


class ReservationBoundaryTests(unittest.TestCase):
    def plan(self, tokens, output=256000, input_limit=1000000, management=None):
        with patch.object(context_budget, "estimate_tokens", return_value={"input_tokens": tokens, "method": "offline_control"}):
            return context_budget.plan_budget([], [], {"model": "offline", "max_tokens": output, "max_input_tokens": input_limit},
                {"context_window_tokens": 1000000, "context_management": management or {}})

    def test_published_dsh_threshold_and_retention(self):
        for tokens in (504736, 550000, 604736, 678463):
            with self.subTest(tokens=tokens):
                result = self.plan(tokens)
                self.assertFalse(result["needs_compaction"])
                self.assertEqual(result["effective_output_tokens"], 256000)
                self.assertEqual(result["effective_input_trigger_tokens"], 678464)
                self.assertEqual(result["headroom_tokens"], 65536)
                self.assertEqual(result["compression_trigger_tokens"], 800000)
                self.assertEqual(result["retain_recent_tokens"], 119040)
        reached = self.plan(678464)
        self.assertTrue(reached["needs_compaction"])
        self.assertEqual(reached["effective_output_tokens"], 0)
        self.assertEqual(reached["compaction_reason"], "output_reservation")

    def test_custom_384k_is_supported_without_forcing_it_as_the_default(self):
        self.assertEqual(self.plan(0, output=393216)["effective_input_trigger_tokens"], 541248)
        self.assertFalse(self.plan(541247, output=393216)["needs_compaction"])
        self.assertTrue(self.plan(541248, output=393216)["needs_compaction"])

    def test_ratio_can_be_the_first_trigger_for_a_small_reply(self):
        self.assertFalse(self.plan(799999, output=16384)["needs_compaction"])
        reached = self.plan(800000, output=16384)
        self.assertTrue(reached["needs_compaction"])
        self.assertEqual(reached["compaction_reason"], "input_trigger")

    def test_provider_input_limit_is_separate_from_completion_reservation(self):
        result = self.plan(100000, input_limit=200000)
        self.assertFalse(result["needs_compaction"])
        self.assertEqual(result["effective_input_trigger_tokens"], 197952)
        self.assertTrue(self.plan(197952, input_limit=200000)["needs_compaction"])

    def test_headroom_includes_safety_without_deducting_it_twice(self):
        self.assertEqual(self.plan(0, management={"headroom_tokens": 0, "safety_tokens": 2048})["effective_input_trigger_tokens"], 741952)
        with self.assertRaises(ValueError):
            self.plan(0, management={"headroom_tokens": -1})
        for value in (True, -0.1, 1.0, "0.16"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.plan(0, management={"retain_recent_ratio": value})

    def test_default_example_reaches_real_wire_with_max_effort(self):
        config = json.loads((Path(__file__).resolve().parent.parent / "config.example.json").read_text(encoding="utf-8"))
        original = copy.deepcopy(config)
        for moment, total, field in (("2026-10-28T23:00:00+08:00", 256000, "max_tokens"),
                                      ("2026-10-28T12:00:00+08:00", 393216, "max_completion_tokens")):
            route = review.select_route(config, moment)
            with patch.object(context_budget, "estimate_tokens", return_value={"input_tokens": 500000, "method": "offline_control"}):
                budget = context_budget.plan_budget([], [], route, config)
            self.assertFalse(budget["needs_compaction"])
            payload = review.make_payload({}, workflow._review_route_with_budget(route, budget))
            self.assertEqual(payload[field], total)
            if field == "max_tokens":
                self.assertEqual(payload["reasoning_effort"], "max")
                self.assertEqual(route["context_window_tokens"], 1000000)
        self.assertEqual(config, original)


if __name__ == "__main__":
    unittest.main()
