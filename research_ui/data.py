"""只读取已保存的研究证据；不调用研究器、审核器或写入台账。"""

import json
from datetime import datetime
from pathlib import Path
import sqlite3

import pandas as pd


STATUS = {
    "replay": "代际回放",
    "running": "研究中", "sealed": "开发已封存", "reviewing": "审核中",
    "reviewed": "历史审核已结束", "development_error": "开发异常", "review_error": "审核异常",
    "reserved": "已登记", "candidate": "候选", "development_passed": "开发通过",
    "rejected": "开发拒绝", "invalid": "定义无效", "duplicate": "重复候选", "error": "计算异常",
    "frozen_for_review": "已冻结待审核", "historical_review_passed": "历史审核通过",
    "synthetic_control_passed": "合成对照通过", "review_rejected": "历史审核拒绝",
}


def label(value):
    return STATUS.get(value, value or "未知")


def contained(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError("产物路径超出当前研究批次")
    return path


def read_json(root, relative):
    return json.loads(contained(root, relative).read_text(encoding="utf-8"))


def list_campaigns(root):
    root = Path(root).resolve()
    if not root.is_dir():
        return []
    result = []
    for directory in root.iterdir():
        if not directory.resolve().is_relative_to(root):
            continue
        if not (directory / "registry.sqlite3").is_file() or not (directory / "manifest.json").is_file():
            continue
        try:
            manifest = read_json(directory, "manifest.json")
            result.append({"path": directory, "synthetic": bool(manifest.get("synthetic")),
                           "name": directory.name, "created": datetime.fromisoformat(manifest.get("created_utc", "1970-01-01").replace("Z", "+00:00")).timestamp()})
        except (OSError, ValueError, TypeError):
            continue
    return sorted(result, key=lambda row: (row["synthetic"], -row["created"], row["name"]))


def load_campaign(directory):
    directory = Path(directory).resolve()
    database = contained(directory, "registry.sqlite3")
    # mode=ro连缺失数据库也不会创建；query_only阻止通过此连接意外写入。
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=3)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        metadata = {row["key"]: json.loads(row["value"]) for row in connection.execute("SELECT * FROM metadata")}
        attempts = [dict(row) for row in connection.execute("SELECT * FROM attempts ORDER BY id")]
        catalog = [dict(row) for row in connection.execute("SELECT * FROM factors ORDER BY factor_id")]
        events = [dict(row) for row in connection.execute("SELECT * FROM events ORDER BY id")]
        for row in attempts:
            row["spec"] = json.loads(row["spec"])
            row["feedback"] = json.loads(row["feedback"]) if row["feedback"] else {}
        for row in catalog:
            row["spec"] = json.loads(row["spec"])
        for row in events:
            row["payload"] = json.loads(row["payload"])
    finally:
        connection.close()
    return {"directory": directory, "state": metadata.get("state", "unknown"),
            "manifest": metadata["manifest"], "attempts": attempts, "catalog": catalog, "events": events}


def attempt_frame(campaign):
    rows = []
    lifecycle = {row["factor_id"]: row.get("lifecycle") for row in campaign["catalog"]}
    for attempt in campaign["attempts"]:
        spec = attempt["spec"] if isinstance(attempt["spec"], dict) else {}
        feedback = attempt["feedback"] or {}
        rows.append({"id": attempt["id"], "round": attempt["round"], "name": spec.get("name", "无效定义"),
                     "status": label(attempt["status"]), "raw_status": attempt["status"],
                     "lifecycle": label(lifecycle.get(attempt["factor_id"]) or attempt["status"]),
                     "score": feedback.get("development_score"), "reason": feedback.get("reason", attempt["error"] or ""),
                     "factor_id": attempt["factor_id"], "parent_id": spec.get("parent_id")})
    return pd.DataFrame(rows, columns=["id", "round", "name", "status", "raw_status", "lifecycle", "score",
                                       "reason", "factor_id", "parent_id"])


def lineage_dot(campaign):
    nodes, edges = {}, set()
    for attempt in campaign["attempts"]:
        spec = attempt["spec"] if isinstance(attempt["spec"], dict) else {}
        key = attempt.get("factor_id") or f"attempt_{attempt['id']}"
        nodes[key] = f"{spec.get('name', '无效定义')}\n第{attempt['round']}轮 · {label(attempt['status'])}"
        if spec.get("parent_id"):
            edges.add((spec["parent_id"], key))
    lines = ['digraph { rankdir=LR; bgcolor="transparent";',
             'node [shape=box style="rounded,filled" fillcolor="#eaf5f4" color="#84b5ad" fontname="sans-serif"];']
    for key, title in nodes.items():
        lines.append(f"{json.dumps(key)} [label={json.dumps(title, ensure_ascii=False)}];")
    for parent, child in sorted(edges):
        if parent not in nodes:
            lines.append(f"{json.dumps(parent)} [label={json.dumps('外部父因子 ' + parent[:12])}];")
        lines.append(f"{json.dumps(parent)} -> {json.dumps(child)};")
    return "\n".join([*lines, "}"])


def curve_frame(root, relative, initial_cash, name):
    frame = pd.read_csv(contained(root, relative), parse_dates=["date"])
    frame["nav"] = frame.equity / initial_cash
    frame["drawdown"] = frame.nav / frame.nav.cummax().clip(lower=1.) - 1
    frame["series"] = name
    return frame[["date", "nav", "drawdown", "series"]]
