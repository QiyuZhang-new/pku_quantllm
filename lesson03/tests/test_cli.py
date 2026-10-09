"""用最小真实 Parquet 验证回放入口的截止时间和计划尾部。"""
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import pyarrow as pa
import pyarrow.parquet as pq
from sse_sim.__main__ import replay
from sse_sim.replay import REQUIRED


class ReplayRuntimeTests(unittest.TestCase):
    def run_replay(self, root, end, items):
        data = root / "data"
        data.mkdir()
        for kind in ("ord", "exe"):
            schema = pa.schema([(name, pa.int64()) for name in sorted(REQUIRED[kind])])
            pq.write_table(pa.Table.from_pylist([], schema=schema), data / f"{kind}_20260923.parquet")
        base = {name: 0 for name in REQUIRED["snp"]}
        base.update(Symbol=600036, PreClose=10, Price=10,
                    BidPrice1=9.99, BidVolume1=100, AskPrice1=10, AskVolume1=100)
        pq.write_table(pa.Table.from_pylist([base | {"Time": 90000000}, base | {"Time": 93000000}]),
                       data / "snp_20260923.parquet")
        plan = root / "plan.json"
        plan.write_text(json.dumps(items))
        args = SimpleNamespace(data_dir=data, symbols=[600036], all_symbols=False,
                               end=end, output=root / "out", cash="10000", initial_shares=0,
                               latency_ms=100, no_star_permission=True, strategy="none", plan=plan)
        with contextlib.redirect_stdout(io.StringIO()):
            replay(args)
        return json.loads((args.output / "summary.json").read_text())

    def test_tail_plan_executes_without_fabricating_fills(self):
        with tempfile.TemporaryDirectory() as temp:
            summary = self.run_replay(Path(temp), "09:30:01", [
                {"time": "09:30:00.100", "id": "entry", "symbol": 600036,
                 "side": "BUY", "price": 10, "quantity": 100},
                {"time": "09:30:00.200", "action": "cancel", "ref": "entry"},
                {"time": "09:31:00", "symbol": 600036, "side": "BUY", "price": 10, "quantity": 100},
            ])
            self.assertEqual(summary["plan"]["executed"], 2)
            self.assertEqual(summary["plan"]["pending"], 1)
            self.assertEqual(summary["simulated_fills"], 0)
            self.assertEqual(summary["account"]["orders"][0]["status"], "CANCELLED")
            self.assertEqual(summary["account"]["frozen_cash_cents"], 0)

    def test_close_without_close_snapshot_releases_order(self):
        with tempfile.TemporaryDirectory() as temp:
            summary = self.run_replay(Path(temp), "15:00:00", [
                {"time": "09:30:00.100", "symbol": 600036, "side": "BUY", "price": 10, "quantity": 100},
            ])
            self.assertEqual(summary["account"]["orders"][0]["status"], "EXPIRED")
            self.assertEqual(summary["account"]["frozen_cash_cents"], 0)
            self.assertEqual(summary["simulated_fills"], 0)
