"""绩效统计与可直接查看的回测产物。"""

from dataclasses import asdict, dataclass
import json
from pathlib import Path

import numpy as np
import pandas as pd


def performance(equity: pd.DataFrame, initial_cash: float, annual_days: int):
    returns = equity["return"]
    nav = equity["equity"] / initial_cash
    # 将回测前的初始资金作为高水位，首日亏损也必须计入回撤。
    peak = nav.cummax().clip(lower=1.0)
    drawdown = nav / peak - 1
    volatility = float(returns.std(ddof=1)) if len(returns) > 1 else 0.0
    annual_return = float(np.expm1(np.log(nav.iloc[-1]) * annual_days / len(nav)))
    return {
        "initial_cash": initial_cash,
        "final_equity": float(equity["equity"].iloc[-1]),
        "final_cash": float(equity["cash"].iloc[-1]),
        "average_exposure": float((equity["market_value"] / equity["equity"]).mean()),
        "total_return": float(nav.iloc[-1] - 1),
        "annualized_return": annual_return,
        "annualized_volatility": volatility * np.sqrt(annual_days),
        "sharpe_zero_rf": float(returns.mean() / volatility * np.sqrt(annual_days)) if volatility > 0 else None,
        "max_drawdown": float(drawdown.min()),
        "total_fees": float(equity["fees"].sum()),
        "total_slippage_cost": float(equity["slippage_cost"].sum()),
        "average_daily_turnover": float(equity["turnover"].mean()),
        "trading_days": len(equity),
        "stale_position_days": int(equity["stale_positions"].sum()),
    }


@dataclass
class BacktestResult:
    equity: pd.DataFrame
    trades: pd.DataFrame
    positions: pd.DataFrame
    rejected_orders: pd.DataFrame
    metrics: dict
    config: object

    def save(self, directory):
        """输出目录必须不存在，以免覆盖之前的实验。"""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False)
        self.equity.to_csv(directory / "equity.csv", index_label="date")
        self.trades.to_csv(directory / "trades.csv", index=False)
        self.positions.to_csv(directory / "positions.csv", index=False)
        self.rejected_orders.to_csv(directory / "rejected_orders.csv", index=False)
        for name, value in (("metrics", self.metrics), ("config", asdict(self.config))):
            (directory / f"{name}.json").write_text(
                json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
            )
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        nav = self.equity["equity"] / self.config.initial_cash
        fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
        nav.plot(ax=axes[0], title="Portfolio NAV", grid=True)
        drawdown = nav / nav.cummax().clip(lower=1) - 1
        drawdown.plot(ax=axes[1], title="Drawdown", grid=True)
        fig.tight_layout()
        fig.savefig(directory / "equity.png", dpi=150)
        plt.close(fig)
