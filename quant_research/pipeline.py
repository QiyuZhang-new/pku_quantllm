"""有预算的开发搜索与一次性历史审核。历史审核结果不回流至同一搜索批次。"""

import json
from pathlib import Path
import platform
import traceback

import numpy as np
import pandas as pd

from lesson05.factor_eval import Market, evaluate_batch
from lesson05.factor_eval.data import universe
from lesson05.factor_eval.stats import row_correlation
from .contracts import Protocol, fingerprint
from .evaluation import account_validation, development_batch, feedback_for, save_account
from .factors import FactorSpec, check_causality, compute_factor, propose, seeds
from .formula_search import FormulaSearch, METHODS
from .inputs import demo_inputs, load_inputs
from .storage import Registry, digest, now, source_hashes, write_json


def restricted(market, end):
    return Market({key: value.loc[:pd.Timestamp(end)].copy() for key, value in market.frames.items()})


def report(registry):
    attempts = registry.attempts()
    value = {"state": registry.metadata("state"), "attempt_count": len(attempts), "attempts": attempts,
             "catalog": registry.catalog(),
             "limitations": ["开发批内FDR不控制自适应搜索整体错误率",
                 "历史留出审核不等于未见数据或前瞻验证；通过不自动激活交易",
                 "算子前缀检查属于回归验证，运行隔离为有界表达式，非任意Python沙箱",
                 "未验证全历史退市覆盖、行业/规模中性化、真实盘口与市场冲击"]}
    write_json(registry.directory / "report.json", value)
    lines = ["# 自进化因子研究记录", "", f"批次状态：`{value['state']}`；尝试数：{len(attempts)}。", "",
             "| 尝试 | 轮次 | 因子 | 状态 | 开发反馈 |", "| --- | --- | --- | --- | --- |"]
    for attempt in attempts:
        raw = attempt["spec"]
        name = str(raw.get("name", "无效定义") if isinstance(raw, dict) else "无效定义").replace("|", "\\|").replace("\n", " ")
        reason = (attempt["feedback"] or {}).get("reason", attempt["error"] or "")
        reason = str(reason).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {attempt['id']} | {attempt['round']} | {name} | {attempt['status']} | {reason} |")
    lines.extend(["", *[f"- {item}" for item in value["limitations"]], ""])
    review_path = registry.directory / "review/summary.json"
    if review_path.exists():
        lines += ["## 已关闭的历史审核", "", "该结果未回流至本批次的开发搜索。", ""]
        for item in json.loads(review_path.read_text())["results"]:
            lines.append(f"- `{item['factor_id'][:12]}`：{item['lifecycle']}。")
    (registry.directory / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    return value


def select_finalists(completed, matrices, pool, protocol):
    selected, decisions = [], []
    cfg = protocol.config()
    for item in sorted(completed, key=lambda item: (not item["feedback"]["passed"],
                          -ranking_score(item), item["attempt_id"])):
        if not item["feedback"]["passed"]:
            continue
        redundant = None
        score = matrices[item["spec"].factor_id].where(pool) * item["direction"]
        score = score.loc[score.index > pd.Timestamp(cfg.train_end)]
        for prior in selected:
            other = matrices[prior["factor_id"]].where(pool).loc[score.index] * prior["direction"]
            correlation = row_correlation(score, other, cfg.min_pairs, rank=True).abs().mean()
            if not np.isfinite(correlation) or correlation >= protocol.max_correlation:
                redundant = prior["factor_id"]
                break
        decision = {"attempt_id": item["attempt_id"], "factor_id": item["spec"].factor_id,
                    "direction": item["direction"], "spec": item["spec"].to_dict(),
                    "artifact": item["artifact"], "redundant_to": redundant}
        decisions.append(decision)
        if redundant is None and len(selected) < protocol.max_finalists:
            selected.append(decision)
    return {"finalists": selected, "decisions": decisions,
            "criterion": "全部开发窗口的统计及账户门槛通过，按末窗口净标签超额排序，再按开发期绝对Rank相关去重",
            "holdout_used_to_select": False}


def ranking_score(item):
    value = item["feedback"]["development_score"]
    return value if value is not None else -1e30


def discover(protocol, data, output, raw_seeds=None, symbols=None, status_directory=None, synthetic=False, progress=print,
             search_method="rules", random_seed=20261106):
    if search_method not in METHODS:
        raise ValueError("不支持的提案方法")
    if type(random_seed) is not int or not 0 <= random_seed <= 2**32 - 1:
        raise ValueError("随机种子必须为0至2**32-1的整数")
    search = FormulaSearch(search_method, random_seed) if search_method != "rules" else None
    root = Path(output).resolve()
    registry = Registry(root)
    manifest = {"schema_version": 1, "created_utc": now(), "protocol": protocol.to_dict(),
                "protocol_sha256": fingerprint(protocol.to_dict()), "source_sha256": source_hashes(),
                "data_directory": str(Path(data).resolve()),
                "status_directory": str(Path(status_directory).resolve()) if status_directory else None,
                "symbols": symbols, "synthetic": synthetic,
                "evidence_level": "synthetic" if synthetic else "historical_exploratory",
                "python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__,
                "search": search.configuration() if search else {"method": "rules", "online_llm_calls": 0}}
    registry.create(manifest)
    write_json(root / "protocol.json", protocol.to_dict())
    write_json(root / "manifest.json", manifest)
    try:
        if synthetic:
            demo_inputs(data)
        market, hashes = load_inputs(data, protocol.development_end, symbols, status_directory)
        if (Path(data) / "SYNTHETIC.json").exists():
            manifest.update(synthetic=True, evidence_level="synthetic")
        if market.dates[-1] != pd.Timestamp(protocol.development_end):
            raise ValueError("development_end 必须是数据中存在的交易日")
        manifest.update(inputs_sha256=hashes, development_shape=list(market.frames["close"].shape))
        registry.freeze_inputs(manifest)
        write_json(root / "manifest.json", manifest)
        queue = list(raw_seeds) if raw_seeds is not None else [spec.to_dict() for spec in seeds()]
        remaining_seeds = len(queue)
        completed, matrices, seen = [], {}, set()
        count = 0
        for round_number in range(1, protocol.max_rounds + 1):
            batch = queue[:min(protocol.batch_size, protocol.max_candidates - count)]
            remaining_seeds -= min(remaining_seeds, len(batch))
            # 外部预登记种子保留顺序；自动提案每轮按最新反馈重新排序。
            queue = queue[len(batch):][:remaining_seeds]
            if not batch:
                break
            progress(f"第{round_number}轮：登记{len(batch)}个候选，数据截止{protocol.development_end}")
            current = {}
            # 在任何本轮结果出现前登记全批；无效和重复候选也消耗预算。
            reserved = [(registry.reserve(round_number, raw), raw) for raw in batch]
            count += len(batch)
            for attempt_id, raw in reserved:
                artifact = f"trials/{attempt_id:04d}"
                directory = root / artifact
                directory.mkdir(parents=True)
                write_json(directory / "submitted.json", raw)
                try:
                    spec = FactorSpec(**raw)
                    if registry.identify(attempt_id, spec):
                        registry.finish(attempt_id, "duplicate", {"reason": "相同算子版本与规范化表达式已尝试"}, artifact)
                        continue
                    seen.add(spec.factor_id)
                    write_json(directory / "factor.json", {"factor_id": spec.factor_id, **spec.to_dict()})
                    matrix = check_causality(spec, market)
                    matrix.to_parquet(directory / "development_factor.parquet")
                    matrices[spec.factor_id] = matrix
                    current[spec.factor_id] = {"attempt_id": attempt_id, "spec": spec,
                                               "artifact": artifact, "folds": []}
                except (ValueError, TypeError, AssertionError) as exc:
                    write_json(directory / "error.json", {"error": str(exc), "traceback": traceback.format_exc()})
                    registry.finish(attempt_id, "invalid", {"reason": "定义或因果性检查未通过"}, artifact, str(exc))
            pool = None
            for fold_index, fold in enumerate(protocol.folds):
                if not current:
                    break
                fold_market = restricted(market, fold.valid_end)
                cfg = protocol.config(fold)
                factors = {key: matrices[key].loc[fold_market.dates] for key in current}
                evaluations, pool = development_batch(fold_market, factors, cfg, len(batch))
                for key, item in list(current.items()):
                    directory = root / item["artifact"] / f"fold_{fold_index + 1:02d}"
                    directory.mkdir()
                    try:
                        evaluation = evaluations[key]
                        write_json(directory / "evaluation.json", evaluation.summary)
                        evaluation.daily.to_parquet(directory / "daily.parquet")
                        account, strategy, baseline = account_validation(fold_market, factors[key], pool,
                            evaluation.summary["direction"], cfg, protocol.execution)
                        write_json(directory / "account.json", account)
                        save_account(strategy, directory / "strategy")
                        save_account(baseline, directory / "baseline")
                        item["folds"].append({"evaluation": evaluation.summary, "account": account})
                        item["direction"] = evaluation.summary["direction"]
                    except Exception as exc:
                        write_json(directory / "error.json", {"error": str(exc), "traceback": traceback.format_exc()})
                        registry.finish(item["attempt_id"], "error", {"reason": "账户或产物计算失败"}, item["artifact"], str(exc))
                        del current[key]
            for item in current.values():
                item["feedback"] = feedback_for(item["folds"])
                write_json(root / item["artifact"] / "summary.json", {"folds": item["folds"], "feedback": item["feedback"]})
                registry.finish(item["attempt_id"], "development_passed" if item["feedback"]["passed"] else "rejected",
                                item["feedback"], item["artifact"])
                completed.append(item)
            parents = sorted(completed, key=lambda item: (not item["feedback"]["passed"],
                             -ranking_score(item), item["attempt_id"]))
            queued = set()
            for raw in queue:
                try:
                    queued.add(FactorSpec(**raw).factor_id)
                except (TypeError, ValueError):
                    pass
            if search:
                observations = search.observe(completed)
                budget = (min(protocol.batch_size, protocol.max_candidates - count) - len(queue)
                          if round_number < protocol.max_rounds else 0)
                proposals, origins = search.propose(completed, seen | queued, budget, round_number + 1)
                queue.extend(proposals)
                write_json(root / f"search/round_{round_number:04d}.json",
                           search.snapshot(completed, round_number, observations, origins))
                progress(f"公式搜索：{len(observations)}项子代反馈，预登记下轮{len(proposals)}项结构变异")
            else:
                for parent in parents:
                    for proposal in propose(parent["spec"], parent["feedback"]):
                        if proposal.factor_id not in seen | queued:
                            queue.append(proposal.to_dict())
                            queued.add(proposal.factor_id)
            report(registry)
            if count >= protocol.max_candidates:
                break
        cfg = protocol.config()
        pool = universe(market, cfg.min_history, cfg.min_adv, cfg.top_n)
        selection = select_finalists(completed, matrices, pool, protocol)
        write_json(root / "selection.json", selection)
        for candidate in selection["finalists"]:
            registry.set_lifecycle(candidate["factor_id"], "frozen_for_review", "selection.json")
        paths = [*root.glob("trials/**/*"), *root.glob("search/*.json"),
                 root / "selection.json", root / "protocol.json", root / "manifest.json"]
        seal = {str(path.relative_to(root)): digest(path) for path in paths if path.is_file()}
        write_json(root / "seal.json", seal)
        registry.transition("running", "sealed", {"seal_sha256": digest(root / "seal.json"),
                                                  "finalists": len(selection["finalists"])})
        result = report(registry)
        progress(f"开发结束：{count}次尝试，{len(selection['finalists'])}个冻结候选；未读取留出期指标")
        return result
    except Exception as exc:
        write_json(root / "campaign_error.json", {"error": str(exc), "traceback": traceback.format_exc()})
        if registry.metadata("state") == "running":
            for attempt in registry.attempts():
                if attempt["status"] == "reserved":
                    registry.finish(attempt["id"], "error", {"reason": "开发批次计算中断，详见campaign_error.json"},
                                    f"trials/{attempt['id']:04d}", str(exc))
            registry.transition("running", "development_error", {"error": str(exc)})
        report(registry)
        raise


def review(output, progress=print):
    root = Path(output).resolve()
    registry = Registry(root)
    # 开发结果可读；没有候选时无需占用审核机会或读取留出行情。
    selection = json.loads((root / "selection.json").read_text())
    if not selection["finalists"]:
        raise ValueError("没有通过全部开发准入的候选；保持失败记录，不打开历史审核")
    registry.transition("sealed", "reviewing", {"warning": "在读取留出期前占用审核机会；失败也不会自动重试"})
    try:
        manifest = registry.metadata("manifest")
        if json.loads((root / "manifest.json").read_text()) != manifest:
            raise ValueError("manifest 与台账不一致")
        if source_hashes() != manifest["source_sha256"]:
            raise ValueError("研究源码已变化，不能沿用旧批次的冻结结论")
        versions = {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__}
        if any(manifest[key] != value for key, value in versions.items()):
            raise ValueError("运行环境版本已变化，不能混用冻结研究证据")
        seal = json.loads((root / "seal.json").read_text())
        # seal 本身的指纹存于封存事件，防止连同候选一起无意覆盖。
        with registry.connect() as db:
            event = db.execute("SELECT payload FROM events WHERE kind='sealed' ORDER BY id DESC LIMIT 1").fetchone()
        if digest(root / "seal.json") != json.loads(event[0])["seal_sha256"]:
            raise ValueError("封存清单已变化")
        if any(digest(root / path) != sha for path, sha in seal.items()):
            raise ValueError("冻结的候选、协议或开发产物已变化")
        protocol = Protocol.from_dict(manifest["protocol"])
        if fingerprint(protocol.to_dict()) != manifest["protocol_sha256"]:
            raise ValueError("协议指纹不一致")
        if any(digest(path) != sha for path, sha in manifest["inputs_sha256"].items()):
            raise ValueError("输入数据快照已变化，禁止混用研究证据")
        market, hashes = load_inputs(manifest["data_directory"], protocol.holdout_end,
                                     manifest["symbols"], manifest["status_directory"])
        if hashes != manifest["inputs_sha256"]:
            raise ValueError("输入数据快照已变化，禁止混用研究证据")
        if market.dates[-1] != pd.Timestamp(protocol.holdout_end):
            raise ValueError("holdout_end 必须是快照中存在的交易日")
        factors = {}
        for candidate in selection["finalists"]:
            spec = FactorSpec(**candidate["spec"])
            if spec.factor_id != candidate["factor_id"]:
                raise ValueError("候选表达式指纹不一致")
            factor = compute_factor(spec, market)
            previous = pd.read_parquet(root / candidate["artifact"] / "development_factor.parquet")
            pd.testing.assert_frame_equal(factor.loc[previous.index], previous)
            factors[spec.factor_id] = factor
        cfg = protocol.config()
        evaluations = {result.name: result for result in evaluate_batch(market, factors, cfg)}
        pool = universe(market, cfg.min_history, cfg.min_adv, cfg.top_n)
        records = []
        for candidate in selection["finalists"]:
            key = candidate["factor_id"]
            evaluation = evaluations[key]
            if evaluation.summary["direction"] != candidate["direction"]:
                raise ValueError("训练冻结方向发生变化")
            directory = root / "review" / key[:16]
            write_json(directory / "evaluation.json", evaluation.summary)
            evaluation.daily.to_parquet(directory / "daily.parquet")
            start_signal = market.dates[market.dates > pd.Timestamp(protocol.development_end)][0]
            account, strategy, baseline = account_validation(market, factors[key], pool,
                candidate["direction"], cfg, protocol.execution, start_signal=start_signal)
            write_json(directory / "account.json", account)
            save_account(strategy, directory / "strategy")
            save_account(baseline, directory / "baseline")
            passed = evaluation.summary["verdict"] == "好因子（当前研究样本通过）" and account["passed"]
            lifecycle = ("synthetic_control_passed" if manifest["synthetic"] else "historical_review_passed") if passed else "review_rejected"
            records.append({"factor_id": key, "spec": candidate["spec"], "passed": passed,
                            "lifecycle": lifecycle, "factor_verdict": evaluation.summary["verdict"],
                            "account_passed": account["passed"]})
        summary = {"evidence_level": manifest["evidence_level"], "results": records,
                   "automatic_trading_activation": False,
                   "note": "历史审核不保证独立未见；本批次已关闭。新假设必须重新登记数据暴露历史。"}
        write_json(root / "review/summary.json", summary)
        for record in records:
            registry.set_lifecycle(record["factor_id"], record["lifecycle"], "review/summary.json")
        registry.transition("reviewing", "reviewed", summary)
        report(registry)
        progress(f"历史审核完成：{sum(r['passed'] for r in records)}/{len(records)}通过；批次已关闭")
        return summary
    except Exception as exc:
        write_json(root / "review/error.json", {"error": str(exc), "traceback": traceback.format_exc()})
        for item in registry.catalog():
            if item["lifecycle"] == "frozen_for_review":
                registry.set_lifecycle(item["factor_id"], "review_error", "review/error.json")
        registry.transition("reviewing", "review_error", {"error": str(exc)})
        report(registry)
        raise
