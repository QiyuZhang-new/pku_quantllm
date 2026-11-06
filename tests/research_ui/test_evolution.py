"""代际回放只能使用当前轮已出现的开发证据，指标改善不改变原准入。"""

from pathlib import Path
import tempfile
import unittest

from quant_research.storage import write_json
from research_ui.evolution import development_evidence, expression_text, replay, round_history


class EvolutionTests(unittest.TestCase):
    def campaign(self, directory):
        return {"directory": directory, "state": "reviewed", "manifest": {}, "attempts": [
            {"id": 1, "round": 1, "factor_id": "parent", "status": "rejected", "artifact": "trials/1",
             "spec": {"name": "alpha101", "expression": {"op": "alpha101", "number": 101}, "hypothesis": "初始假设"}},
            {"id": 2, "round": 2, "factor_id": "child", "status": "rejected", "artifact": "trials/2",
             "spec": {"name": "smooth", "expression": {"op": "smooth", "window": 3, "arg": {"op": "alpha101", "number": 101}},
                      "parent_id": "parent", "hypothesis": "平滑"}}],
            "catalog": [{"factor_id": "parent", "lifecycle": "historical_review_passed"}, {"factor_id": "child", "lifecycle": "review_rejected"}],
            "events": [{"time": "1", "payload": {"attempt_id": 1}}, {"time": "2", "payload": {"attempt_id": 2}},
                       {"time": "3", "payload": {"results": ["未来审核"]}}]}

    def test_replay_hides_future_children_lifecycle_and_events(self):
        campaign = self.campaign(Path("/tmp"))
        snapshot = replay(campaign, 1)
        self.assertEqual([row["id"] for row in snapshot["attempts"]], [1])
        self.assertEqual(snapshot["catalog"][0]["lifecycle"], "rejected")
        self.assertEqual([row["time"] for row in snapshot["events"]], ["1"])
        self.assertEqual(snapshot["state"], "replay")
        self.assertEqual(campaign["state"], "reviewed")

    def test_parent_delta_uses_same_window_and_never_promotes_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self.campaign(root)
            for identifier, ic, net in ((1, .02, -.02), (2, .04, -.01)):
                folder = root / f"trials/{identifier}/fold_01"
                write_json(folder / "evaluation.json", {"periods": {"validation": {"ic": {"mean": ic, "ir": .3},
                    "long_excess_net": {"mean": net}, "factor_coverage": 1}}})
                write_json(folder / "account.json", {"strategy": {"total_return": net, "max_drawdown": -.1}, "excess_total_return": net})
            evidence, _ = development_evidence(campaign, 2)
            self.assertAlmostEqual(evidence.iloc[1].ic_delta, .02)
            self.assertAlmostEqual(evidence.iloc[1].excess_delta, .01)
            self.assertFalse(evidence.iloc[1].all_passed)
            history = round_history(evidence)
            self.assertTrue(history.best_approved.isna().all())
            self.assertEqual(history.best_diagnostic.tolist(), [-.02, -.01])
            early, _ = development_evidence(campaign, 1)
            self.assertEqual(early.id.tolist(), [1])

    def test_alpha_reference_is_displayed_as_formula_identity(self):
        self.assertEqual(expression_text({"op": "alpha101", "number": 33}), "Alpha#33")
        self.assertEqual(expression_text({"op": "smooth", "window": 3, "arg": {"op": "alpha101", "number": 101}}),
                         "Mean(Alpha#101, 3)")


if __name__ == "__main__":
    unittest.main()
