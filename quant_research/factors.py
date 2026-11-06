"""有界 JSON 表达式；不执行候选提供的 Python、文件操作或网络请求。"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from lesson05.factor_eval import Market
from .contracts import fingerprint
from .alpha101 import DEFINITIONS, compute as compute_alpha101


OPERATORS_VERSION = "causal-price-volume-v3-formula"
LEAVES = {"return", "volatility", "market_deviation"}
UNARY = {"rank", "negate"}
BINARY = {"blend", "subtract", "multiply"}


def node_count(expression):
    stack, count = [expression], 0
    while stack:
        node = stack.pop()
        if not isinstance(node, dict) or not isinstance(node.get("args", []), list):
            raise ValueError("子表达式必须为对象，args必须为数组")
        count += 1
        if count > 15:
            return count
        stack.extend(node.get("args", []))
        if "arg" in node:
            stack.append(node["arg"])
    return count


def normalize(expression, depth=0):
    if not isinstance(expression, dict) or depth > 4:
        raise ValueError("表达式必须为对象，深度不能超过4")
    if depth == 0 and node_count(expression) > 15:
        raise ValueError("表达式最多15个节点")
    op = expression.get("op")
    if op == "alpha101":
        if set(expression) != {"op", "number"} or type(expression["number"]) is not int or expression["number"] not in DEFINITIONS:
            raise ValueError("Alpha101仅接受已核验并实现的编号")
        return {"op": op, "number": expression["number"]}
    keys = ({"op", "window"} if op in LEAVES else {"op", "window", "arg"} if op == "smooth"
            else {"op", "arg"} if op in UNARY else {"op", "args"})
    if op not in LEAVES | {"smooth"} | UNARY | BINARY or set(expression) != keys:
        raise ValueError(f"不支持的算子或参数: {op}")
    if op in UNARY:
        return {"op": op, "arg": normalize(expression["arg"], depth + 1)}
    if op in BINARY:
        args = expression["args"]
        if not isinstance(args, list) or len(args) != 2:
            raise ValueError(f"{op} 必须有两个子表达式")
        args = [normalize(arg, depth + 1) for arg in args]
        if op in {"blend", "multiply"}:
            args.sort(key=fingerprint)
        if op == "blend" and args[0] == args[1]:
            return args[0]
        return {"op": op, "args": args}
    window = expression["window"]
    if type(window) is not int or not 2 <= window <= 252:
        raise ValueError("window 必须为2至252的整数")
    result = {"op": op, "window": window}
    if op == "smooth":
        result["arg"] = normalize(expression["arg"], depth + 1)
    return result


@dataclass(frozen=True)
class FactorSpec:
    name: str
    expression: dict
    hypothesis: str
    parent_id: str | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or len(self.name) > 100:
            raise ValueError("因子名须为1至100个字符")
        if not isinstance(self.hypothesis, str) or not self.hypothesis.strip():
            raise ValueError("必须记录研究假设")
        object.__setattr__(self, "expression", normalize(self.expression))

    @property
    def factor_id(self):
        return fingerprint({"operators": OPERATORS_VERSION, "expression": self.expression})

    def to_dict(self):
        return {"name": self.name, "expression": self.expression,
                "hypothesis": self.hypothesis, "parent_id": self.parent_id}


def compute_factor(spec, market):
    close = market.adjusted("close")
    returns = close.pct_change(fill_method=None)

    def visit(node):
        op = node["op"]
        if op == "alpha101":
            return compute_alpha101(node["number"], market)
        if op == "return":
            return close.div(close.shift(node["window"])).sub(1)
        if op == "volatility":
            return returns.rolling(node["window"], min_periods=node["window"]).std()
        if op == "market_deviation":
            eligible = market.frames.get("eligible", close.notna().astype(float)).eq(1)
            if "is_st" in market.frames:
                eligible &= market.frames["is_st"].eq(0)
            reference = returns.where(eligible).mean(axis=1)
            return returns.sub(reference, axis=0).rolling(node["window"], min_periods=node["window"]).std()
        if op == "smooth":
            return visit(node["arg"]).rolling(node["window"], min_periods=node["window"]).mean()
        if op == "rank":
            eligible = market.frames.get("eligible", close.notna().astype(float)).eq(1)
            if "is_st" in market.frames:
                eligible &= market.frames["is_st"].eq(0)
            # 居中截面秩，允许差分与乘积表达相对强弱及交互，不混合量纲。
            return visit(node["arg"]).where(eligible).rank(axis=1, pct=True).sub(.5)
        if op == "negate":
            return -visit(node["arg"])
        if op == "subtract":
            return visit(node["args"][0]) - visit(node["args"][1])
        if op == "multiply":
            return visit(node["args"][0]) * visit(node["args"][1])
        return (visit(node["args"][0]).rank(axis=1, pct=True)
                + visit(node["args"][1]).rank(axis=1, pct=True)) / 2

    return visit(spec.expression).replace([np.inf, -np.inf], np.nan)


def check_causality(spec, market):
    """检查历史前缀及未来扰动；只接收开发数据。这是回归检查，不是数学证明。"""
    original = compute_factor(spec, market)
    for cut in sorted({len(market.dates) // 2, len(market.dates) * 3 // 4}):
        prefix = Market({k: v.iloc[:cut].copy() for k, v in market.frames.items()})
        pd.testing.assert_frame_equal(original.iloc[:cut], compute_factor(spec, prefix))
        changed = {k: v.copy() for k, v in market.frames.items()}
        for key in ("open", "high", "low", "close", "amt", "cumadj"):
            if key in changed:
                changed[key].iloc[cut:] *= 1.7
        pd.testing.assert_frame_equal(original.iloc[:cut], compute_factor(spec, Market(changed)).iloc[:cut])
    return original


def seeds():
    return [FactorSpec(f"{op}_{window}", {"op": op, "window": window}, hypothesis)
            for op, window, hypothesis in (
                ("return", 5, "短期价格变化可能反映反转或延续，方向仅由训练期确定"),
                ("return", 20, "较长价格变化可能改善信号持续性"),
                ("volatility", 20, "历史波动可能反映横截面风险补偿差异"),
                ("market_deviation", 20, "扣除当日可知市场均值后，偏离波动可能提供额外排序信息"))]


def propose(parent, feedback):
    """确定性反馈搜索基线；外部模型可生成同一 FactorSpec JSON 契约。"""
    expression = parent.expression
    reason = feedback.get("reason", "检验信号持续性")
    failures = feedback.get("failed_metrics", [])
    smooth = ("smooth", {"op": "smooth", "window": 3, "arg": expression},
              f"根据开发诊断“{reason}”，检验三日平滑能否减少信号噪声与换手")
    variants = [smooth]
    if expression["op"] == "alpha101":
        reference = 33 if expression["number"] != 33 else 101
        variants.append(("blend", {"op": "blend", "args": [expression, {"op": "alpha101", "number": reference}]},
                         f"根据开发诊断“{reason}”，检验与Alpha#{reference}的截面秩组合；收益由本地重新验证"))
    if expression["op"] in LEAVES:
        window = expression["window"]
        insufficient = any("coverage" in metric or metric == "valid_days" for metric in failures)
        window = max(2, window // 2) if insufficient else min(252, window * 2)
        change = ("window", {**expression, "window": window},
                  f"根据开发诊断“{reason}”，检验窗口{window}的覆盖和稳定性")
        variants = [change, smooth] if insufficient or feedback.get("passed") else [smooth, change]
    for suffix, expr, hypothesis in variants:
        try:
            yield FactorSpec(f"{parent.name[:80]}_{suffix}", expr, hypothesis, parent.factor_id)
        except ValueError:
            continue
