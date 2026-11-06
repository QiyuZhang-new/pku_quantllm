"""从真实开发产物整理演化证据；不重算收益，不读取审核指标来选优。"""

from copy import deepcopy
import math

import pandas as pd

from .data import contained, label, read_json


def expression_text(node):
    if not isinstance(node, dict):
        return "无效表达式"
    op, window = node.get("op"), node.get("window")
    if op == "alpha101":
        return f"Alpha#{node.get('number')}"
    if op == "return":
        return f"Ret(close, {window})"
    if op == "volatility":
        return f"Std(ret, {window})"
    if op == "market_deviation":
        return f"Std(ret − market, {window})"
    if op == "smooth":
        return f"Mean({expression_text(node.get('arg'))}, {window})"
    if op == "rank":
        return f"CRank({expression_text(node.get('arg'))})"
    if op == "negate":
        return f"−({expression_text(node.get('arg'))})"
    if op in {"subtract", "multiply"}:
        args = node.get("args", [])
        if len(args) == 2:
            symbol = "−" if op == "subtract" else "×"
            return f"({expression_text(args[0])} {symbol} {expression_text(args[1])})"
    if op == "blend":
        return "Blend(" + ", ".join(expression_text(arg) for arg in node.get("args", [])) + ")"
    return "无效表达式"


def operation(child, parent=None):
    if not isinstance(child, dict):
        return "定义无效"
    if not parent:
        return "初始假设"
    if child.get("op") == "smooth" and child.get("arg") == parent:
        return f"平滑 {child['window']} 日"
    if child.get("op") == parent.get("op") and child.get("window") != parent.get("window"):
        return f"窗口 {parent.get('window')} → {child.get('window')} 日"
    return "结构修改"


def replay(campaign, round_number):
    result = deepcopy(campaign)
    result["attempts"] = [row for row in result["attempts"] if row["round"] <= round_number]
    visible = {row["factor_id"] for row in result["attempts"] if row["factor_id"]}
    result["catalog"] = [row for row in result["catalog"] if row["factor_id"] in visible]
    latest_round = max((row["round"] for row in campaign["attempts"]), default=0)
    if round_number < latest_round:
        # 最终生命周期不能倒灌到历史代；确认结果也不能提前出现在回放中。
        states = {row["factor_id"]: row["status"] for row in result["attempts"] if row["status"] != "duplicate"}
        for row in result["catalog"]:
            row["lifecycle"] = states[row["factor_id"]]
        result["state"] = "replay"
        visible_attempts = {row["id"] for row in result["attempts"]}
        times = [row["time"] for row in result["events"]
                 if row["payload"].get("attempt_id") in visible_attempts]
        if times:
            boundary = max(times)
            result["events"] = [row for row in result["events"] if row["time"] <= boundary]
        else:
            result["events"] = []
    return result


def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def development_evidence(campaign, round_number, window=None):
    rows, snapshots = [], {}
    attempts = [row for row in campaign["attempts"] if row["round"] <= round_number]
    by_factor = {row["factor_id"]: row for row in attempts if row["factor_id"] and row["status"] != "duplicate"}
    for attempt in attempts:
        spec = attempt["spec"] if isinstance(attempt["spec"], dict) else {}
        parent = by_factor.get(spec.get("parent_id"))
        parent_spec = parent["spec"] if parent else {}
        base = {"id": attempt["id"], "round": attempt["round"], "name": spec.get("name", "无效定义"),
                "factor_id": attempt["factor_id"], "parent_id": spec.get("parent_id"),
                "parent_attempt": parent["id"] if parent else None,
                "expression": expression_text(spec.get("expression")), "hypothesis": spec.get("hypothesis", ""),
                "operation": operation(spec.get("expression", {}), parent_spec.get("expression")),
                "status": label(attempt["status"]), "all_passed": attempt["status"] == "development_passed",
                "rank_ic": None, "icir": None, "net_return": None, "excess": None, "drawdown": None,
                "coverage": None, "label_excess": None, "window": window, "ic_delta": None, "excess_delta": None}
        if attempt.get("artifact"):
            directory = contained(campaign["directory"], attempt["artifact"])
            windows = sorted(path.name for path in directory.glob("fold_*")
                             if (path / "evaluation.json").is_file() and (path / "account.json").is_file())
            chosen = window if window in windows else windows[-1] if window is None and windows else None
            if chosen:
                relative = f"{attempt['artifact']}/{chosen}"
                evaluation = read_json(campaign["directory"], relative + "/evaluation.json")
                account = read_json(campaign["directory"], relative + "/account.json")
                valid = evaluation.get("periods", {}).get("validation", {})
                ic = valid.get("ic", {})
                base.update(rank_ic=ic.get("mean"), icir=ic.get("ir"), coverage=valid.get("factor_coverage"),
                            label_excess=valid.get("long_excess_net", {}).get("mean"),
                            net_return=account["strategy"].get("total_return"),
                            excess=account.get("excess_total_return"), drawdown=account["strategy"].get("max_drawdown"),
                            window=chosen)
                snapshots[attempt["id"]] = {"evaluation": evaluation, "account": account}
        rows.append(base)
    by_attempt = {row["id"]: row for row in rows}
    for row in rows:
        parent = by_attempt.get(row["parent_attempt"])
        if parent and row["window"] == parent["window"]:
            for field, delta in (("rank_ic", "ic_delta"), ("excess", "excess_delta")):
                if finite(row[field]) and finite(parent[field]):
                    row[delta] = row[field] - parent[field]
    return pd.DataFrame(rows), snapshots


def round_history(evidence):
    """累计诊断最优与全部准入最优分开；不是虚构的冠军晋升日志。"""
    rows = []
    for number in sorted(evidence["round"].unique()):
        visible = evidence.loc[evidence["round"].le(number)]
        measured = visible.dropna(subset=["label_excess"])
        approved = measured.loc[measured.all_passed]
        best = measured.sort_values(["label_excess", "id"], ascending=[False, True]).iloc[0] if len(measured) else None
        accepted = approved.sort_values(["label_excess", "id"], ascending=[False, True]).iloc[0] if len(approved) else None
        rows.append({"round": int(number), "attempts": len(visible), "passed": int(visible.all_passed.sum()),
                     "best_diagnostic": float(best.label_excess) if best is not None else None,
                     "best_approved": float(accepted.label_excess) if accepted is not None else None,
                     "diagnostic_id": int(best.id) if best is not None else None,
                     "approved_id": int(accepted.id) if accepted is not None else None})
    return pd.DataFrame(rows)
