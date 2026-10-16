"""python -m backtest 命令行入口。"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from . import BacktestConfig, BacktestEngine, EqualWeightStrategy, MarketData


def main():
    parser = argparse.ArgumentParser(description="日频回测：收盘信号、下一交易日开盘撮合")
    parser.add_argument("--data", default="feathers", help="Feather 数据目录")
    parser.add_argument("--symbols", nargs="+", help="证券代码；省略时使用全部证券")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--initial-cash", type=float, default=1_000_000)
    parser.add_argument("--commission", type=float, default=0.0003)
    parser.add_argument("--sell-tax", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.0005)
    parser.add_argument("--lot-size", type=int, default=100, help="每手份额，0 表示允许零碎份额")
    parser.add_argument("--rebalance", choices=["daily", "weekly", "monthly"], default="monthly")
    parser.add_argument("--output", help="输出至尚不存在的目录；默认 runs/时间戳")
    args = parser.parse_args()
    config = BacktestConfig(
        initial_cash=args.initial_cash, commission=args.commission,
        sell_tax=args.sell_tax, slippage=args.slippage, lot_size=args.lot_size,
        start=args.start, end=args.end,
    )
    output = Path(args.output) if args.output else Path("runs") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if output.exists():
        parser.error(f"输出目录已存在: {output}")
    data = MarketData.from_feathers(args.data, args.symbols)
    result = BacktestEngine(data, config).run(EqualWeightStrategy(args.rebalance))
    result.save(output)
    (output / "experiment.json").write_text(json.dumps({
        "strategy": "EqualWeightStrategy", "rebalance": args.rebalance,
        "data_directory": str(Path(args.data).resolve()),
        "symbols": data.symbols.tolist(), "first_data_date": str(data.dates[0]),
        "last_data_date": str(data.dates[-1]),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2, allow_nan=False))
    print(f"成交记录: {len(result.trades)}，未成交记录: {len(result.rejected_orders)}")
    print(f"输出目录: {output.resolve()}")


if __name__ == "__main__":
    main()
