"""python -m factor_eval：课程行情、自定义因子或合成验证。"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys

import numpy as np
import pandas as pd

from .core import Config, evaluate_batch
from .data import build_factors, demo_market, load_market, read_matrix
from .report import write_reports


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def parser():
    base = Path(__file__).resolve().parents[2]
    p = argparse.ArgumentParser(description="基于未来五交易日复权开盘收益，判断横截面因子好坏")
    p.add_argument("--data", type=Path, default=base / "lesson04" / "feathers")
    p.add_argument("--output", type=Path, default=base / "lesson05" / "results" / "evaluation")
    p.add_argument("--factors", default="5dr,momentum20,volatility20", help="内置因子列表；使用 none 可仅评自定义因子")
    p.add_argument("--factor", action="append", default=[], metavar="NAME=PATH", help="可重复；日期×证券宽表")
    p.add_argument("--eligible", type=Path, help="信号时点股票池掩码，1=可选，其余排除；宽表")
    p.add_argument("--demo", action="store_true", help="合成强信号/噪声/常数自检，非市场数据")
    p.add_argument("--train-end", help="训练期末日，须与 --valid-end 同时提供；默认前50%%/中25%%/后25%%")
    p.add_argument("--valid-end", help="验证期末日")
    p.add_argument("--one-way-bps", type=float, default=10., help="单边总成本情景，默认10bp")
    p.add_argument("--top-n", type=int, default=1000, help="信号日过去20日成交额前N，0=不限制")
    p.add_argument("--min-adv", type=float, default=1e8, help="过去20日最低平均成交额，数据单位须为元")
    p.add_argument("--config", type=Path, help="Config 字段的 JSON；覆盖命令行配置，便于冻结研究协议")
    return p


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    try:
        if args.output.exists() and any(args.output.iterdir()):
            raise ValueError(f"输出目录非空，为保留研究记录请换新目录：{args.output}")
        if args.demo and (args.factor or args.eligible or args.factors != "5dr,momentum20,volatility20"):
            raise ValueError("--demo 使用固定合成因子，不能同时指定其他因子或股票池")
        values = dict(train_end=args.train_end, valid_end=args.valid_end,
                      one_way_bps=args.one_way_bps, top_n=args.top_n, min_adv=args.min_adv)
        inputs = {}
        if args.demo:
            market, factors = demo_market()
            values.update(min_history=20, min_adv=0, top_n=0)
        else:
            market = load_market(args.data)
            inputs.update({str((args.data / f"{f}.feather").resolve()): digest(args.data / f"{f}.feather")
                           for f in market.frames})
            available = build_factors(market)
            names = [] if args.factors == "none" else args.factors.split(",")
            if len(names) != len(set(names)):
                raise ValueError("内置因子名称重复")
            unknown = set(names) - available.keys()
            if unknown:
                raise ValueError(f"未知因子：{sorted(unknown)}；可用：{list(available)}")
            factors = {name: available[name] for name in names}
            for spec in args.factor:
                if "=" not in spec:
                    raise ValueError("--factor 格式须为 NAME=PATH")
                name, path = spec.split("=", 1)
                if not name or name in factors:
                    raise ValueError(f"因子名为空或重复：{name}")
                factors[name] = read_matrix(path)
                inputs[str(Path(path).resolve())] = digest(path)
            if args.eligible:
                # 重新构造以触发所有对齐校验。
                from .data import Market
                market = Market({**market.frames, "eligible": read_matrix(args.eligible)})
                inputs[str(args.eligible.resolve())] = digest(args.eligible)
        if args.config:
            values.update(json.loads(args.config.read_text(encoding="utf-8")))
            inputs[str(args.config.resolve())] = digest(args.config)
        cfg = Config(**values)
        print(f"评估 {len(factors)} 个因子，{len(market.dates)} 交易日；方向只取训练集，五日标签按区间清洗。", flush=True)
        results = evaluate_batch(market, factors, cfg)
        manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "synthetic": args.demo,
                    "candidate_order": list(factors), "inputs_sha256": inputs,
                    "code_sha256": {path.name: digest(path) for path in sorted(Path(__file__).parent.glob("*.py"))},
                    "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
                    "date_start": str(market.dates[0].date()), "date_end": str(market.dates[-1].date()),
                    "symbols": len(market.frames["close"].columns), "config": results[0].summary["config"],
                    "boundaries": results[0].summary["boundaries"], "command": sys.argv if argv is None else argv}
        board = write_reports(results, args.output, manifest)
        print(board[["factor", "verdict", "score", "direction", "test_rank_ic", "test_long_excess_net_5dr"]].to_string(index=False))
        print(f"报告：{args.output.resolve() / 'report.html'}")
    except (ValueError, TypeError, FileNotFoundError, NotADirectoryError) as exc:
        p.error(str(exc))


if __name__ == "__main__":
    main()
