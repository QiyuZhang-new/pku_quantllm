"""固定预算的真实行情公式挖掘实例；两臂共享种子、评价器、数据与交易约束。"""

import argparse
from dataclasses import replace
import json
from pathlib import Path
import re

import numpy as np
import pandas as pd
import pyarrow.feather as feather

from .contracts import Protocol
from .factors import FactorSpec
from .formula_search import METHODS
from .pipeline import discover
from .storage import digest, now, source_hashes, write_json


PROJECT = Path(__file__).resolve().parents[1]


def example_seeds():
    return [FactorSpec(name, expression, "固定初始对照；方向仅由训练数据确定").to_dict()
            for name, expression in (("alpha33", {"op": "alpha101", "number": 33}),
                ("alpha101", {"op": "alpha101", "number": 101}),
                ("return5", {"op": "return", "window": 5}),
                ("volatility20", {"op": "volatility", "window": 20}))]


def collect(campaign):
    report = json.loads((campaign / "report.json").read_text())
    manifest = json.loads((campaign / "manifest.json").read_text())
    method = manifest["search"]["method"]
    origins, history, observations = {}, [], []
    for path in sorted((campaign / "search").glob("round_*.json")):
        snapshot = json.loads(path.read_text())
        origins.update({row["factor_id"]: row for row in snapshot["next_proposals"]})
        observations += snapshot["observations"]
        if snapshot["incumbent"]:
            history.append({"method": method, "round": snapshot["round"],
                "attempt_id": snapshot["incumbent"]["attempt_id"],
                "fitness": snapshot["incumbent"]["fitness"][1],
                "development_passed": snapshot["incumbent"]["fitness"][0]})
    rows = []
    for attempt in report["attempts"]:
        origin = origins.get(attempt["factor_id"], {})
        for fold in sorted((campaign / attempt["artifact"]).glob("fold_*")):
            if not (fold / "account.json").exists():
                continue
            evaluation = json.loads((fold / "evaluation.json").read_text())
            account = json.loads((fold / "account.json").read_text())
            valid = evaluation["periods"]["validation"]
            rows.append({"method": method, "attempt_id": attempt["id"], "round": attempt["round"],
                "factor_id": attempt["factor_id"], "parent_id": attempt["spec"].get("parent_id"),
                "donor_id": origin.get("donor_id"), "operator": origin.get("operator", "seed"),
                "window": fold.name, "rank_ic": valid["ic"]["mean"],
                "icir": valid["ic"]["ir"], "direction": evaluation["direction"],
                "net_return": account["strategy"]["total_return"],
                "excess_return": account["excess_total_return"],
                "drawdown": account["strategy"]["max_drawdown"],
                "development_passed": attempt["status"] == "development_passed",
                "failed_metrics": ",".join(attempt["feedback"].get("failed_metrics", [])),
                "expression": json.dumps(attempt["spec"]["expression"], ensure_ascii=False)})
    best = history[-1]
    winner = next(a for a in report["attempts"] if a["id"] == best["attempt_id"])
    return {"method": method, "campaign": campaign.name, "attempts": report["attempt_count"],
            "development_passed": sum(a["status"] == "development_passed" for a in report["attempts"]),
            "children_improved_parent": sum(row["improved_parent"] for row in observations),
            "best": best, "best_spec": winner["spec"], "history": history,
            "online_llm_calls": 0, "holdout_opened": (campaign / "review").exists()}, rows


def save_comparison(directory, summaries, rows):
    write_json(directory / "comparison.json", {"arms": summaries,
        "interpretation": "一次固定种子的开发期机制实例；不能据此证明自适应方法更优或因子独立有效"})
    pd.DataFrame(rows).to_csv(directory / "candidates.csv", index=False)
    history = pd.DataFrame([row for result in summaries for row in result["history"]])
    history.to_csv(directory / "history.csv", index=False)
    # 独立图表可随报告下载分享；界面另外提供交互回放。
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    fig, ax = plt.subplots(figsize=(8.5, 4.5), layout="constrained")
    for method, group in history.groupby("method", sort=False):
        ax.plot(group["round"], group["fitness"], marker="o", label=method)
    ax.set(xlabel="Completed development rounds", ylabel="Research fitness (account return units)",
           title="ALPHA GENESIS | Formula mining: adaptive vs random")
    ax.set_xticks(sorted(history["round"].unique()))
    ax.legend()
    ax.grid(alpha=.2)
    fig.savefig(directory / "search_comparison.png", dpi=160)
    plt.close(fig)
    lines = ["# ALPHA GENESIS · 真实行情公式自动挖掘实例", "",
             "固定种子、同预算的开发期比较；未打开2025—2026历史审核。", "",
             "| 方法 | 候选数 | 子代优于实际父代 | 初始最佳适应度 | 最终最佳适应度 | 完整准入通过 |", "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for result in summaries:
        lines.append(f"| {METHODS[result['method']]} | {result['attempts']} | {result['children_improved_parent']} | "
                     f"{result['history'][0]['fitness']:.6f} | {result['best']['fitness']:.6f} | {result['development_passed']} |")
    lines += ["", "![同预算搜索轨迹](search_comparison.png)", "", "## 选定的研究公式", ""]
    for result in summaries:
        lines += [f"### {METHODS[result['method']]}", "", f"候选 #{result['best']['attempt_id']}；`{result['campaign']}`。", "",
                  "```json", json.dumps(result["best_spec"], ensure_ascii=False, indent=2), "```", ""]
    lines += ["## 解释边界", "",
        "- 反馈进化使用有界遗传变异和UCB算子选择；在线LLM调用为0，不涉及模型权重训练。",
        "- 研究适应度为最差开发窗口账户净超额，扣除最差负收益与公式节点惩罚；完整准入优先。",
        "- 一次种子、固定120列（如使用默认范围）的工程实例，不能外推全市场或证明搜索机制更优。",
        "- 已看过的历史不称盲测；未调用历史审核不代表证明了样本外收益。",
        "- 指标改善的拒绝候选仍不能入有效因子库或自动启用交易。",
        "- 全部候选、失败原因、父代和交叉来源见 candidates.csv；每轮反馈及提案在原研究批次 search/ 中封存。", ""]
    (directory / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(directory / "evidence_sha256.json", {path.name: digest(path) for path in directory.iterdir()
               if path.is_file() and path.name != "evidence_sha256.json"})


def main(argv=None):
    parser = argparse.ArgumentParser(description="运行一组有反馈公式搜索与同预算随机对照，不打开历史审核")
    parser.add_argument("--name", required=True, help="全新实验名称，只允许字母数字下划线")
    parser.add_argument("--root", type=Path, default=PROJECT / "research_runs")
    parser.add_argument("--data", type=Path, default=PROJECT / "lesson04/feathers")
    parser.add_argument("--status", type=Path, default=PROJECT / "lesson04/research/assignment/data/historical_status")
    parser.add_argument("--scope", choices=("120", "full"), default="120")
    parser.add_argument("--rounds", type=int, default=4)
    parser.add_argument("--seed", type=int, default=20261106)
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_]{1,70}", args.name):
        parser.error("实验名称只允许1至70个字母、数字或下划线")
    protocol = Protocol.from_dict(json.loads((PROJECT / "configs/research/smoke.json").read_text()))
    protocol = replace(protocol, max_rounds=args.rounds, batch_size=4, max_candidates=args.rounds * 4)
    if args.rounds < 2 or not 0 <= args.seed <= 2**32 - 1:
        parser.error("至少需要两轮，且随机种子必须在0至2**32-1内")
    root = args.root.resolve()
    comparison = root / f"{args.name}_comparison"
    methods = ("adaptive_gp", "random_gp")
    outputs = [root / f"{args.name}_{method}" for method in methods]
    if any(path.exists() for path in [comparison, *outputs]):
        parser.error("实验目录已存在；请使用全新名称，已有结果不会覆盖")
    columns = feather.read_table(args.data / "close.feather", memory_map=True).column_names[1:]
    symbols = [columns[index] for index in np.linspace(0, len(columns) - 1, 120, dtype=int)] if args.scope == "120" else None
    comparison.mkdir(parents=True)
    write_json(comparison / "plan.json", {"created_utc": now(), "methods": list(methods),
        "protocol": protocol.to_dict(), "seeds": example_seeds(), "random_seed": args.seed,
        "symbols": symbols, "scope": args.scope, "source_sha256": source_hashes(),
        "holdout_opening": False, "online_llm_calls": 0})
    summaries, all_rows = [], []
    for method, output in zip(methods, outputs):
        print(f"开始 {METHODS[method]}：{protocol.max_candidates}个候选，股票范围{args.scope}", flush=True)
        discover(protocol, args.data, output, example_seeds(), symbols, args.status,
                 search_method=method, random_seed=args.seed)
        summary, rows = collect(output)
        summaries.append(summary)
        all_rows += rows
    save_comparison(comparison, summaries, all_rows)
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"实例报告：{comparison / 'REPORT.md'}", flush=True)


if __name__ == "__main__":
    main()
