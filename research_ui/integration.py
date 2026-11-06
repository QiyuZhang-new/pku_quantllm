"""将研究账户原样接入既有回测历史，保留来源、准入及文件指纹。"""

from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from lesson04.backtest import BacktestConfig, BacktestResult
from lesson04.backtest.dashboard import load_experiment
from quant_research.contracts import fingerprint
from quant_research.storage import digest, write_json
from .data import contained, label, read_json
from .evolution import expression_text


def open_in_backtest(campaign, attempt_id, window, output_parent):
    attempt = next((row for row in campaign["attempts"] if row["id"] == attempt_id), None)
    if attempt is None or not attempt.get("artifact"):
        raise ValueError("没有可导入的候选账户")
    if attempt["status"] not in {"development_passed", "rejected"}:
        raise ValueError("候选尚未完成开发验证，不能导入回测历史")
    if window == "历史审核":
        if campaign["state"] != "reviewed":
            raise ValueError("历史审核尚未完成")
        relative = f"review/{attempt['factor_id'][:16]}"
    else:
        if not window.startswith("fold_") or not window[5:].isdigit():
            raise ValueError("窗口名称无效")
        relative = f"{attempt['artifact']}/{window}"
    source = contained(campaign["directory"], relative)
    account = read_json(campaign["directory"], relative + "/account.json")
    paths = {name: contained(campaign["directory"], relative + "/strategy/" + name + ".csv")
             for name in ("equity", "trades", "positions", "rejected_orders")}
    hashes = {name: digest(path) for name, path in paths.items()}
    hashes["account"] = digest(source / "account.json")
    provenance = {"campaign": campaign["directory"].name, "campaign_directory": str(campaign["directory"]),
        "factor_id": attempt["factor_id"], "attempt_id": attempt_id, "window": window,
        "decision": label(attempt["status"]), "account_passed": account["passed"],
        "research_only": True, "sha256": hashes}
    if window == "历史审核":
        summary = read_json(campaign["directory"], "review/summary.json")
        record = next((row for row in summary["results"] if row["factor_id"] == attempt["factor_id"]), None)
        if record is None:
            raise ValueError("找不到该候选的最终审核结论")
        provenance["decision"] = label(record["lifecycle"])
    identifier = fingerprint(provenance)
    output_parent = Path(output_parent).resolve()
    output_parent.mkdir(parents=True, exist_ok=True)
    target = output_parent / f"factor_{identifier[:20]}"
    with (output_parent / ".factor_import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists():
            previous = json.loads((target / "experiment.json").read_text())
            if previous.get("research_source") != provenance:
                raise ValueError("目标实验已存在且来源不同，拒绝覆盖")
            return load_experiment(target)
        equity = pd.read_csv(paths["equity"], index_col="date", parse_dates=["date"])
        tables = {}
        for name in ("trades", "positions", "rejected_orders"):
            try:
                table = pd.read_csv(paths[name])
            except pd.errors.EmptyDataError:
                columns = ["date", "signal_date", "symbol", "side", "quantity"] if name == "trades" else (
                    ["date", "symbol", "shares", "weight"] if name == "positions" else ["date", "signal_date", "symbol", "reason"])
                table = pd.DataFrame(columns=columns)
            for column in ("date", "signal_date"):
                if column in table:
                    table[column] = pd.to_datetime(table[column])
            tables[name] = table
        config = BacktestConfig(**account["execution_config"])
        result = BacktestResult(equity, tables["trades"], tables["positions"], tables["rejected_orders"], account["strategy"], config)
        with TemporaryDirectory(prefix=".factor_import_", dir=output_parent) as temporary:
            stage = Path(temporary) / "result"
            result.save(stage)
            spec = attempt["spec"]
            write_json(stage / "experiment.json", {"strategy": f"研究候选 · {expression_text(spec['expression'])}",
                "created_at": datetime.now(timezone.utc).isoformat(), "research_source": provenance,
                "factor": spec, "config": account["execution_config"], "rebalance": "冻结研究协议"})
            if any(digest(path) != hashes[name] for name, path in paths.items()) or digest(source / "account.json") != hashes["account"]:
                raise ValueError("导入期间研究产物发生变化，请等待研究完成")
            stage.rename(target)
    return load_experiment(target)
