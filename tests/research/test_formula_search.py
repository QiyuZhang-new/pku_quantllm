"""公式数值、算子学习边界、随机对照及封存证据回归。"""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

from quant_research.cli import demo_protocol
from quant_research.factors import FactorSpec, check_causality, compute_factor
from quant_research.formula_search import FormulaSearch, fitness
from quant_research.inputs import demo_inputs, load_inputs
from quant_research.pipeline import discover, review


def item(index, op="return", window=5, excess=.01, net=.02, passed=False):
    spec = FactorSpec(f"seed{index}", {"op": op, "window": window}, "测试假设")
    return {"attempt_id": index, "spec": spec,
            "feedback": {"passed": passed, "reason": "账户超额不足"},
            "folds": [{"account": {"excess_total_return": excess, "strategy": {"total_return": net}}}]}


class FormulaPolicyTests(unittest.TestCase):
    def setUp(self):
        self.parents = [item(1), item(2, "volatility", 20), item(3, window=20), item(4, "market_deviation", 20)]

    def test_fitness_uses_worst_window_actual_accounts_and_all_gates(self):
        parent = self.parents[0]
        parent["folds"].append({"account": {"excess_total_return": -.02, "strategy": {"total_return": -.03}}})
        self.assertAlmostEqual(fitness(parent)[1], -.0501)
        approved = item(5, excess=-.1, passed=True)
        self.assertGreater(fitness(approved), fitness(parent))

    def test_proposals_are_reproducible_unique_and_record_real_parents(self):
        first, second = FormulaSearch("adaptive_gp", 13), FormulaSearch("adaptive_gp", 13)
        seen = {x["spec"].factor_id for x in self.parents}
        a, records = first.propose(self.parents, seen, 8, 2)
        self.assertEqual((a, records), second.propose(self.parents, seen, 8, 2))
        self.assertEqual(len({FactorSpec(**spec).factor_id for spec in a}), 8)
        self.assertTrue(all(spec["parent_id"] in seen for spec in a))
        self.assertTrue(all(row["donor_id"] in seen or row["donor_id"] is None for row in records))
        self.assertTrue({"difference", "product", "blend", "window", "smooth", "replace"}
                        <= {row["operator"] for row in records})

    def test_actual_child_reward_updates_operator_once(self):
        search = FormulaSearch("adaptive_gp", 11)
        proposals, records = search.propose(self.parents, {x["spec"].factor_id for x in self.parents}, 1, 2)
        child = item(5, excess=.20)
        child["spec"] = FactorSpec(**proposals[0])
        observations = search.observe(self.parents + [child])
        self.assertTrue(observations[0]["improved_parent"])
        self.assertEqual(search.statistics[records[0]["operator"]], {"observed": 1, "improved": 1})
        self.assertEqual(search.observe(self.parents + [child]), [])
        worse = deepcopy(child)
        worse["folds"][0]["account"]["excess_total_return"] = -.5
        other = FormulaSearch("adaptive_gp", 11)
        other.propose(self.parents, {x["spec"].factor_id for x in self.parents}, 1, 2)
        other.observe(self.parents + [worse])
        self.assertLess(other.operator_scores()["difference"], search.operator_scores()["difference"])

    def test_random_control_ignores_performance_in_generation(self):
        changed = deepcopy(self.parents)
        for row in changed:
            row["folds"][0]["account"]["excess_total_return"] = -row["attempt_id"]
        a, b = FormulaSearch("random_gp", 17), FormulaSearch("random_gp", 17)
        seen = {x["spec"].factor_id for x in self.parents}
        self.assertEqual(a.propose(self.parents, seen, 8, 2), b.propose(changed, seen, 8, 2))

    def test_snapshot_does_not_change_when_later_feedback_arrives(self):
        search = FormulaSearch("adaptive_gp", 11)
        snapshot = search.snapshot(self.parents, 1, [], [])
        search.statistics["difference"]["improved"] += 10
        self.assertEqual(snapshot["operator_statistics"]["difference"]["improved"], 0)


class FormulaArithmeticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.data = Path(cls.temporary.name) / "data"
        demo_inputs(cls.data)
        cls.market, _ = load_inputs(cls.data, demo_protocol().development_end)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_rank_difference_and_product_match_independent_calculation(self):
        a, b = {"op": "return", "window": 5}, {"op": "volatility", "window": 20}
        close = self.market.adjusted("close")
        eligible = self.market.frames["eligible"].eq(1) & self.market.frames["is_st"].eq(0)
        left = close.div(close.shift(5)).sub(1).where(eligible).rank(axis=1, pct=True).sub(.5)
        right = close.pct_change(fill_method=None).rolling(20, min_periods=20).std().where(eligible).rank(axis=1, pct=True).sub(.5)
        for op, expected in (("subtract", left - right), ("multiply", left * right)):
            spec = FactorSpec(op, {"op": op, "args": [{"op": "rank", "arg": a}, {"op": "rank", "arg": b}]}, "测试")
            pd.testing.assert_frame_equal(check_causality(spec, self.market), expected)

    def test_ranks_exclude_ineligible_stock(self):
        self.market.frames["eligible"].iloc[200, 0] = 0
        spec = FactorSpec("rank", {"op": "rank", "arg": {"op": "return", "window": 5}}, "测试")
        self.assertTrue(np.isnan(compute_factor(spec, self.market).iloc[200, 0]))

    def test_subtraction_preserves_order_and_product_canonicalizes(self):
        a, b = {"op": "return", "window": 5}, {"op": "volatility", "window": 20}
        for op, equal in (("subtract", False), ("multiply", True)):
            first = FactorSpec("first", {"op": op, "args": [a, b]}, "测试")
            second = FactorSpec("second", {"op": op, "args": [b, a]}, "测试")
            self.assertEqual(first.factor_id == second.factor_id, equal)

    def test_malformed_and_excessive_formula_rejected(self):
        leaf = {"op": "return", "window": 5}
        tree = leaf
        for _ in range(4):
            tree = {"op": "multiply", "args": [tree, tree]}
        for expression in ({"op": "rank", "arg": []}, {"op": "subtract", "args": "unsafe"}, tree):
            with self.subTest(expression=expression), self.assertRaises(ValueError):
                FactorSpec("bad", expression, "测试")

    def test_pipeline_feedback_snapshots_are_sealed_before_review(self):
        full = demo_protocol()
        protocol = replace(full, folds=(full.folds[0],), holdout_end=full.development_end,
                           max_rounds=2, batch_size=2, max_candidates=4, max_finalists=1)
        seeds = [item(1, window=20)["spec"].to_dict(), item(2, "volatility", 20)["spec"].to_dict()]
        target = Path(self.temporary.name) / "formula_campaign"
        result = discover(protocol, self.data, target, seeds, progress=lambda _: None,
                          search_method="adaptive_gp", random_seed=7)
        self.assertEqual(result["attempt_count"], 4)
        self.assertEqual([row["round"] for row in result["attempts"]], [1, 1, 2, 2])
        first = json.loads((target / "search/round_0001.json").read_text())
        last = json.loads((target / "search/round_0002.json").read_text())
        self.assertEqual(first["evaluated_attempt_ids"], [1, 2])
        self.assertEqual(len(first["next_proposals"]), 2)
        self.assertEqual(len(last["observations"]), 2)
        seal = json.loads((target / "seal.json").read_text())
        self.assertIn("search/round_0001.json", seal)
        self.assertFalse((target / "review").exists())
        # 若有合格冻结候选，篡改搜索反馈必须在读取留出行情前失败。
        if json.loads((target / "selection.json").read_text())["finalists"]:
            (target / "search/round_0001.json").write_text("{}")
            with patch("quant_research.pipeline.load_inputs") as loader:
                with self.assertRaisesRegex(ValueError, "产物已变化"):
                    review(target, progress=lambda _: None)
                loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
