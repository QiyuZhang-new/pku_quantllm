"""有界公式树搜索：开发反馈驱动的算子选择，及同预算随机对照。

仅接收本批次已经完成的开发结果；不接收行情、未来标签或历史审核结果。
这是遗传搜索与UCB算子选择，不涉及在线LLM或模型权重训练。
"""

from copy import deepcopy
import math
import random

from .factors import FactorSpec, LEAVES, node_count


METHODS = {"rules": "规则反馈基线", "adaptive_gp": "公式进化 · 反馈选父与算子学习",
           "random_gp": "随机公式对照 · 不使用反馈选父"}
ARMS = ("difference", "product", "blend", "window", "smooth", "replace")
WINDOWS = (3, 5, 10, 20, 40, 60)
SEARCH_VERSION = "bounded-formula-ucb-v1"
OBJECTIVE = ("先比较全部开发准入，再比较 min(各开发窗口账户净超额)"
             " - max(0, -min(各窗口账户净收益)) - 0.0001×公式节点数；"
             "该目标仅用于研究父代选择，不改变最终准入规则")


def fitness(item):
    accounts = [fold["account"] for fold in item["folds"]]
    excess = [account["excess_total_return"] for account in accounts]
    returns = [account["strategy"]["total_return"] for account in accounts]
    if not accounts or not all(math.isfinite(value) for value in excess + returns):
        return (False, -1e30)
    return (bool(item["feedback"]["passed"]), min(excess) - max(0., -min(returns))
            - .0001 * node_count(item["spec"].expression))


def locations(node, path=()):
    yield path, node
    if "arg" in node:
        yield from locations(node["arg"], path + ("arg",))
    for index, child in enumerate(node.get("args", [])):
        yield from locations(child, path + ("args", index))


def replaced(expression, path, value):
    if not path:
        return deepcopy(value)
    result = deepcopy(expression)
    cursor = result
    for step in path[:-1]:
        cursor = cursor[step]
    cursor[path[-1]] = deepcopy(value)
    return result


class FormulaSearch:
    def __init__(self, method, seed):
        if method not in {"adaptive_gp", "random_gp"}:
            raise ValueError("不支持的公式搜索方法")
        if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
            raise ValueError("随机种子必须为0至2**32-1的整数")
        self.method, self.seed = method, seed
        self.rng = random.Random(seed)
        self.statistics = {arm: {"observed": 0, "improved": 0} for arm in ARMS}
        self.origins, self.observed = {}, set()

    def configuration(self):
        return {"method": self.method, "seed": self.seed, "version": SEARCH_VERSION,
                "objective": OBJECTIVE, "arms": list(ARMS), "max_nodes": 15, "max_depth": 4,
                "feedback_boundary": "仅本批次已完成的开发账户，未读历史审核",
                "online_llm_calls": 0}

    def observe(self, completed):
        by_id = {item["spec"].factor_id: item for item in completed}
        changes = []
        for item in completed:
            key = item["spec"].factor_id
            if key in self.observed:
                continue
            self.observed.add(key)
            origin = self.origins.get(key)
            if origin is None:
                continue
            parent = by_id[origin["parent_id"]]
            improved = fitness(item) > fitness(parent)
            stats = self.statistics[origin["operator"]]
            stats["observed"] += 1
            stats["improved"] += int(improved)
            changes.append({"attempt_id": item["attempt_id"], "factor_id": key,
                "parent_id": origin["parent_id"], "operator": origin["operator"],
                "parent_fitness": list(fitness(parent)), "child_fitness": list(fitness(item)),
                "improved_parent": improved, "development_passed": item["feedback"]["passed"]})
        return changes

    def operator_scores(self, pending=None):
        pending = pending or {}
        total = sum(stats["observed"] for stats in self.statistics.values()) + sum(pending.values())
        result = {}
        for arm, stats in self.statistics.items():
            trials = stats["observed"] + pending.get(arm, 0)
            result[arm] = (stats["improved"] / max(1, stats["observed"])
                           + math.sqrt(2 * math.log(max(2, total + 1)) / max(1, trials)))
        return result

    def _arm(self, pending):
        if self.method == "random_gp":
            return self.rng.choice(ARMS)
        # 未试算子优先；本轮已预订次数也计入探索，避免一轮全押同一算子。
        unused = [arm for arm in ARMS if self.statistics[arm]["observed"] + pending.get(arm, 0) == 0]
        if unused:
            return unused[0]
        scores = self.operator_scores(pending)
        return max(ARMS, key=lambda arm: scores[arm])

    def _mutate(self, parent, donor, arm):
        expression = parent.expression
        if arm in {"difference", "product", "blend"}:
            a, b = expression, donor.expression
            if arm == "blend":
                return {"op": "blend", "args": [a, b]}, []
            return {"op": "subtract" if arm == "difference" else "multiply", "args": [
                {"op": "rank", "arg": a}, {"op": "rank", "arg": b}]}, []
        if arm == "smooth":
            return {"op": "smooth", "window": self.rng.choice((3, 5, 10)), "arg": expression}, []
        nodes = [(path, node) for path, node in locations(expression)
                 if node["op"] in LEAVES | {"alpha101", "smooth"}]
        if arm == "window":
            nodes = [(path, node) for path, node in nodes if "window" in node]
            if not nodes:
                raise ValueError("父公式没有可变异的窗口")
            path, node = self.rng.choice(nodes)
            window = self.rng.choice([value for value in WINDOWS if value != node["window"]])
            return replaced(expression, path, {**node, "window": window}), list(path)
        path, node = self.rng.choice([(path, node) for path, node in nodes if node["op"] != "smooth"])
        alternatives = [{"op": "return", "window": value} for value in (5, 20, 60)]
        alternatives += [{"op": "volatility", "window": 20}, {"op": "market_deviation", "window": 20}]
        alternatives += [{"op": "alpha101", "number": value} for value in (4, 9, 10, 18, 20, 23, 33, 101)]
        return replaced(expression, path, self.rng.choice([x for x in alternatives if x != node])), list(path)

    def propose(self, completed, excluded, budget, round_number):
        if not completed or budget <= 0:
            return [], []
        ranked = sorted(completed, key=lambda item: (fitness(item), -item["attempt_id"]), reverse=True)
        # 自适应臂从开发适应度最优的三个父代选取；随机臂在全部已测父代均匀取样。
        parents = ranked[:3] if self.method == "adaptive_gp" else sorted(completed, key=lambda x: x["attempt_id"])
        pending, candidates, records, used = {}, [], [], set(excluded)
        for _ in range(max(100, budget * 80)):
            if len(candidates) >= budget:
                break
            arm = self._arm(pending)
            parent = self.rng.choice(parents)
            donors = [item for item in completed if item["spec"].factor_id != parent["spec"].factor_id]
            donor = self.rng.choice(donors) if donors else parent
            try:
                expression, path = self._mutate(parent["spec"], donor["spec"], arm)
                spec = FactorSpec(f"gp_r{round_number}_{len(candidates) + 1}_{arm}", expression,
                    f"公式树{arm}变异；父候选#{parent['attempt_id']}；开发诊断：{parent['feedback']['reason']}。"
                    "检验跨开发窗口账户收益是否改善；结果须重新经过全部准入。", parent["spec"].factor_id)
            except ValueError:
                # 无可用窗口/深度越界时改试另一算子；没有消耗评价预算或读取指标。
                pending[arm] = pending.get(arm, 0) + 1
                continue
            if spec.factor_id in used:
                pending[arm] = pending.get(arm, 0) + 1
                continue
            used.add(spec.factor_id)
            origin = {"factor_id": spec.factor_id, "parent_id": parent["spec"].factor_id,
                "donor_id": donor["spec"].factor_id if arm in {"difference", "product", "blend"} else None,
                "operator": arm, "mutation_path": path, "spec": spec.to_dict()}
            self.origins[spec.factor_id] = origin
            records.append(origin)
            candidates.append(spec.to_dict())
            pending[arm] = pending.get(arm, 0) + 1
        if len(candidates) != budget:
            raise ValueError("未能在有界公式空间生成足够的不重复候选；停止并保留已完成证据")
        return candidates, records

    def snapshot(self, completed, round_number, observations, proposals):
        best = max(completed, key=lambda item: (fitness(item), -item["attempt_id"])) if completed else None
        return {"round": round_number, "configuration": self.configuration(),
            "evaluated_attempt_ids": [item["attempt_id"] for item in completed],
            "operator_statistics": deepcopy(self.statistics), "ucb_scores": self.operator_scores(),
            "observations": observations, "next_proposals": proposals,
            "incumbent": {"attempt_id": best["attempt_id"], "factor_id": best["spec"].factor_id,
                          "fitness": list(fitness(best)), "spec": best["spec"].to_dict()} if best else None,
            "note": "研究父代改进不等于有效因子入库；随机对照记录反馈但不用于生成选择"}
