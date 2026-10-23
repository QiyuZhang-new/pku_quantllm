"""市场偏离信号、公告可知时点及分批退出的经济语义。"""

import unittest

import numpy as np
import pandas as pd

from backtest import StrategyContext
from backtest.market_deviation_strategy import MarketDeviationStrategy
from test_backtest import market


class MarketDeviationTests(unittest.TestCase):
    def setUp(self):
        x = np.arange(180)
        m = .01*np.sin(x)
        r = np.column_stack([m*.1, m, m*1.1+.004*np.cos(x*1.7)])
        self.data = market(10*np.cumprod(1+r,axis=0), symbols=["600000.SH", "000001.SZ", "600001.SH"])
        self.data.frames["amt"] = pd.DataFrame(1e9,index=self.data.dates,columns=self.data.symbols)
        self.data.frames["is_st"] = pd.DataFrame(0.,index=self.data.dates,columns=self.data.symbols)

    def context(self, row, weights=None):
        z = pd.Series(0.,index=self.data.symbols) if weights is None else pd.Series(weights,index=self.data.symbols)
        return StrategyContext(self.data,row,1e9,1e9,z,z)

    def test_low_market_deviation_differs_from_low_total_volatility(self):
        strategy = MarketDeviationStrategy(k=1)
        target = strategy.on_close(self.context(150))
        self.assertEqual(target.index.tolist(),["000001.SZ"])

    def test_future_prices_and_status_cannot_change_signal(self):
        first = MarketDeviationStrategy(k=1).on_close(self.context(150))
        for field in ["close","amt","is_st"]:
            self.data.frames[field].iloc[151:] = 9999
        second = MarketDeviationStrategy(k=1).on_close(self.context(150))
        pd.testing.assert_series_equal(first,second)

    def test_notice_cannot_exclude_stock_before_announcement(self):
        notice = {"000001.SZ": self.data.dates[151]}
        strategy = MarketDeviationStrategy(k=1,holding=1,notices=notice)
        before = strategy.on_close(self.context(150))
        after = strategy.on_close(self.context(151))
        self.assertIn("000001.SZ",before.index)
        self.assertNotIn("000001.SZ",after.index)

    def test_liquidation_sells_one_remaining_day_fraction(self):
        strategy = MarketDeviationStrategy(liquidation_signals={self.data.dates[150]:5,self.data.dates[151]:1})
        target = strategy.on_close(self.context(150,[.3,.7,0]))
        np.testing.assert_allclose(target,[.24,.56,0])
        final = strategy.on_close(self.context(151,[.3,.7,0]))
        self.assertEqual(final.sum(),0)

    def test_st_is_excluded_even_if_it_tracks_market_well(self):
        self.data.frames["is_st"].iloc[150,1] = 1
        target = MarketDeviationStrategy(k=1).on_close(self.context(150))
        self.assertNotIn("000001.SZ",target.index)


if __name__ == "__main__":
    unittest.main()
