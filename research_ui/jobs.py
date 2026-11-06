"""界面仅在明确提交时创建独立研究任务；每个结果目录只写一次。"""

from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from .data import contained


PROJECT = Path(__file__).resolve().parents[1]


def atomic_json(path, value):
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def job_path(root, identifier, suffix):
    if not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise ValueError("无效任务ID")
    return contained(root, f".ui_jobs/{identifier}.{suffix}")


def is_alive(pid, identifier=None):
    try:
        state = Path(f"/proc/{int(pid)}/stat").read_text().rsplit(")", 1)[1].split()[0]
        if state == "Z":
            return False
        if identifier is not None:
            command = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
            return b"research_ui.worker" in command and identifier.encode() in command
        return True
    except (OSError, ValueError, TypeError, IndexError):
        return False


def list_jobs(root):
    directory = contained(root, ".ui_jobs")
    if not directory.exists():
        return []
    result = []
    for path in directory.glob("*.state.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not value.get("pid"):
                pid_file = job_path(root, value["id"], "pid")
                if pid_file.exists():
                    value["pid"] = int(pid_file.read_text())
            if value["status"] in {"queued", "running"} and value.get("pid") and not is_alive(value.get("pid"), value["id"]):
                value["status"] = "interrupted"
            result.append(value)
        except (OSError, ValueError, KeyError):
            continue
    return sorted(result, key=lambda value: value["created_utc"], reverse=True)


def validate_request(request):
    required = {"mode", "rounds", "batch_size", "scope", "seeds", "initial_cash"}
    if not required <= set(request) or set(request) - required - {"search_method", "random_seed"}:
        raise ValueError("任务参数不完整或包含未知字段")
    if request.get("search_method", "rules") not in {"rules", "adaptive_gp", "random_gp"}:
        raise ValueError("不支持的提案方法")
    seed = request.get("random_seed", 20261106)
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError("随机种子必须为0至2**32-1的整数")
    if request["mode"] not in {"synthetic", "historical"} or request["scope"] not in {"120", "full"}:
        raise ValueError("不支持的数据模式或证券范围")
    for key, upper in (("rounds", 8), ("batch_size", 8)):
        if type(request[key]) is not int or not 1 <= request[key] <= upper:
            raise ValueError(f"{key} 超出允许预算")
    if request["rounds"] * request["batch_size"] > 64:
        raise ValueError("候选总预算不能超过64")
    if not isinstance(request["seeds"], list) or not request["seeds"] or len(request["seeds"]) != len(set(request["seeds"])):
        raise ValueError("必须选择不重复的种子因子")
    supported = {"return5", "return20", "volatility20", "market_deviation20"}
    supported |= {f"alpha{number}" for number in (4, 9, 10, 18, 20, 23, 33, 101)}
    if set(request["seeds"]) - supported:
        raise ValueError("包含不支持的种子因子")
    if request["initial_cash"] not in {1_000_000, 10_000_000, 1_000_000_000}:
        raise ValueError("请选择固定的资金规模")


def launch(root, request, data_directory=None, status_directory=None):
    validate_request(request)
    root = Path(root).resolve()
    if any(row["status"] in {"queued", "running"} for row in list_jobs(root)):
        raise ValueError("已有研究任务运行，请等待或请求停止后再启动")
    directory = contained(root, ".ui_jobs")
    directory.mkdir(parents=True, exist_ok=True)
    identifier = uuid.uuid4().hex
    created = datetime.now(timezone.utc).isoformat()
    name = datetime.now(timezone.utc).strftime("web_%Y%m%d_%H%M%S_") + identifier[:8]
    payload = {"id": identifier, "created_utc": created, "campaign": name, "runs_root": str(root), "request": request}
    if data_directory is not None:
        payload["sources"] = {"data": str(Path(data_directory).resolve()),
                              "status": str(Path(status_directory).resolve()) if status_directory else None}
    request_path = job_path(root, identifier, "request.json")
    atomic_json(request_path, payload)
    state = {**payload, "status": "queued", "pid": None, "message": "等待启动"}
    state_path = job_path(root, identifier, "state.json")
    atomic_json(state_path, state)
    try:
        with job_path(root, identifier, "log").open("xb") as log:
            process = subprocess.Popen([sys.executable, "-u", "-m", "research_ui.worker", str(request_path)],
                cwd=PROJECT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        job_path(root, identifier, "pid").write_text(str(process.pid))
        # 由worker原子更新状态，避免父进程覆盖它已经写入的running/completed。
        state["pid"] = process.pid
        return state
    except OSError as exc:
        state.update(status="failed", message=str(exc))
        atomic_json(state_path, state)
        raise


def cancel(root, identifier):
    path = job_path(root, identifier, "state.json")
    value = json.loads(path.read_text(encoding="utf-8"))
    if value["status"] not in {"queued", "running"}:
        raise ValueError("任务已结束或已中断")
    job_path(root, identifier, "cancel").touch(exist_ok=True)


def task_lock(root):
    """进程锁由worker持有至退出；不强杀其他CLI或服务进程。"""
    handle = contained(root, ".ui_jobs/worker.lock").open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError("另一个界面研究任务正在执行") from None
    return handle
