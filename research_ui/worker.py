"""可独立退出的本地研究进程；只执行开发搜索，不调用历史审核。"""

from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import traceback

import numpy as np
import pyarrow.feather as feather

from quant_research.cli import demo_protocol
from quant_research.contracts import Protocol
from quant_research.factors import FactorSpec
from quant_research.pipeline import discover
from .jobs import PROJECT, atomic_json, job_path, task_lock, validate_request


class Cancelled(Exception):
    pass


def run(request_path):
    payload = json.loads(Path(request_path).read_text(encoding="utf-8"))
    root = Path(payload["runs_root"]).resolve()
    identifier = payload["id"]
    if Path(request_path).resolve() != job_path(root, identifier, "request.json"):
        raise ValueError("任务文件位置与ID不一致")
    state = {**payload, "status": "running", "pid": os.getpid(), "message": "准备行情与冻结协议"}
    state_path = job_path(root, identifier, "state.json")
    atomic_json(state_path, state)
    lock = None
    try:
        request = payload["request"]
        validate_request(request)
        lock = task_lock(root)
        target = root / payload["campaign"]
        if target.parent != root or target.exists():
            raise ValueError("结果目录必须是研究目录内尚不存在的直接子目录")
        mode = request["mode"]
        if mode == "synthetic":
            protocol, data, status, symbols = demo_protocol(), target / "inputs", None, None
        else:
            config_path = PROJECT / "configs/research" / ("smoke.json" if request["scope"] == "120" else "historical.json")
            protocol = Protocol.from_dict(json.loads(config_path.read_text()))
            data = PROJECT / "lesson04/feathers"
            status = PROJECT / "lesson04/research/assignment/data/historical_status"
            if "sources" in payload:
                data = Path(payload["sources"]["data"])
                status = Path(payload["sources"]["status"]) if payload["sources"]["status"] else None
            symbols = None
            if request["scope"] == "120":
                columns = feather.read_table(data / "close.feather", memory_map=True).column_names[1:]
                symbols = [columns[index] for index in np.linspace(0, len(columns) - 1, 120, dtype=int)]
        protocol = replace(protocol, max_rounds=request["rounds"], batch_size=request["batch_size"],
                           max_candidates=request["rounds"] * request["batch_size"],
                           execution=replace(protocol.execution, initial_cash=float(request["initial_cash"])))
        definitions = {"return5": ("return", 5), "return20": ("return", 20),
                       "volatility20": ("volatility", 20), "market_deviation20": ("market_deviation", 20)}
        candidates = [FactorSpec(name, {"op": "alpha101", "number": int(name[5:])} if name.startswith("alpha")
                       else {"op": definitions[name][0], "window": definitions[name][1]},
                       "检验已有价量假设，方向仅由训练数据确定").to_dict() for name in request["seeds"]]

        def checkpoint(message):
            if message.startswith("第") and job_path(root, identifier, "cancel").exists():
                raise Cancelled("已在轮次边界停止；已有结果保留")
            state["message"] = message
            atomic_json(state_path, state)
            print(message, flush=True)

        if job_path(root, identifier, "cancel").exists():
            raise Cancelled("任务在加载行情前取消")
        result = discover(protocol, data, target, candidates, symbols, status,
                          synthetic=(mode == "synthetic"), progress=checkpoint,
                          search_method=request.get("search_method", "rules"),
                          random_seed=request.get("random_seed", 20261106))
        state.update(status="completed", message=f"开发搜索完成，记录{result['attempt_count']}次尝试；未打开历史审核")
    except Cancelled as exc:
        state.update(status="cancelled", message=str(exc))
    except Exception as exc:
        state.update(status="failed", message=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        traceback.print_exc()
    finally:
        atomic_json(state_path, state)
        if lock is not None:
            lock.close()
    return 0 if state["status"] in {"completed", "cancelled"} else 1


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1]))
