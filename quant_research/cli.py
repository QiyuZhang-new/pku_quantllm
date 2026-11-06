"""从仓库根目录运行 python -m quant_research。"""

import argparse
import json
from pathlib import Path

import pandas as pd

from .contracts import Execution, Fold, Protocol
from .formula_search import METHODS
from .pipeline import discover, report, review
from .storage import Registry


def demo_protocol():
    dates = pd.bdate_range("2020-01-02", periods=900)
    day = lambda row: str(dates[row].date())
    return Protocol(folds=(Fold(day(269), day(479)), Fold(day(479), day(689))),
                    holdout_end=day(899), evaluation={"min_pairs": 20, "min_history": 40,
                    "min_adv": 0, "top_n": 0}, execution=Execution(max_drawdown=.30))


def main(argv=None):
    parser = argparse.ArgumentParser(description="固定协议、可追溯反馈搜索与一次性历史审核")
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="合成正对照，自动运行开发及一次历史审核")
    demo.add_argument("--output", type=Path, required=True)
    search = commands.add_parser("discover", help="只运行开发搜索，不计算留出期指标")
    search.add_argument("--protocol", type=Path, required=True)
    search.add_argument("--data", type=Path, default=Path("lesson04/feathers"))
    search.add_argument("--status", type=Path, help="第四讲historical_status目录，或在data中提供状态宽表")
    search.add_argument("--symbols", nargs="+", help="固定证券子集；属于本研究协议的一部分")
    search.add_argument("--seeds", type=Path, help="FactorSpec JSON数组；可包含外部模型生成的表达式")
    search.add_argument("--search-method", choices=list(METHODS), default="rules", help="规则、反馈公式搜索或随机对照")
    search.add_argument("--random-seed", type=int, default=20261106, help="公式搜索的固定随机种子")
    search.add_argument("--output", type=Path, required=True)
    for name, help_text in (("review", "封存候选的一次性历史审核，之后关闭该批次"),
                            ("inspect", "查看台账并更新可读报告")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            discover(demo_protocol(), args.output.resolve() / "inputs", args.output, synthetic=True)
            selection = json.loads((args.output / "selection.json").read_text())
            if selection["finalists"]:
                review(args.output)
        elif args.command == "discover":
            protocol = Protocol.from_dict(json.loads(args.protocol.read_text(encoding="utf-8")))
            candidates = json.loads(args.seeds.read_text(encoding="utf-8")) if args.seeds else None
            if candidates is not None and (not isinstance(candidates, list) or not candidates):
                raise ValueError("seeds 必须是非空JSON数组")
            if args.symbols and len(args.symbols) != len(set(args.symbols)):
                raise ValueError("证券列表不能重复")
            discover(protocol, args.data, args.output, candidates, args.symbols, args.status,
                     search_method=args.search_method, random_seed=args.random_seed)
        elif args.command == "review":
            review(args.output)
        else:
            value = report(Registry(args.output))
            print(json.dumps({"state": value["state"], "attempt_count": value["attempt_count"]}, ensure_ascii=False))
        print(f"记录：{args.output.resolve() / 'REPORT.md'}")
        return 0
    except (ValueError, TypeError, OSError, AssertionError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
