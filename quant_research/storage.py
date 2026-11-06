"""文件证据与 SQLite 追加事件；审核在读取留出期之前原子占用一次机会。"""

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from .contracts import canonical


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子替换只用于本次运行自身的状态快照；历史产物目录不会复用。
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def source_hashes():
    root = Path(__file__).resolve().parents[1]
    paths = [*Path(__file__).parent.glob("*.py"),
             *(root / "lesson04/backtest").glob("*.py"),
             *(root / "lesson05/factor_eval").glob("*.py")]
    return {str(path.relative_to(root)): digest(path) for path in sorted(paths)}


class Registry:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "registry.sqlite3"

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, manifest):
        self.directory.mkdir(parents=True, exist_ok=False)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE factors (factor_id TEXT PRIMARY KEY, spec TEXT NOT NULL,
                    lifecycle TEXT NOT NULL, evidence TEXT);
                CREATE TABLE attempts (id INTEGER PRIMARY KEY, round INTEGER NOT NULL,
                    factor_id TEXT, spec TEXT NOT NULL, status TEXT NOT NULL,
                    feedback TEXT, artifact TEXT, error TEXT);
                CREATE TABLE events (id INTEGER PRIMARY KEY, time TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL);
            """)
            for key, value in {"state": "running", "manifest": manifest}.items():
                db.execute("INSERT INTO metadata VALUES (?, ?)", (key, canonical(value)))
            self._event(db, "campaign_created", manifest)

    def _event(self, db, kind, payload):
        db.execute("INSERT INTO events(time,kind,payload) VALUES (?,?,?)", (now(), kind, canonical(payload)))

    def metadata(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        if row is None:
            raise ValueError(f"台账缺少 {key}")
        return json.loads(row["value"])

    def freeze_inputs(self, manifest):
        with self.connect() as db:
            if db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]:
                raise ValueError("候选登记后不能更换数据")
            db.execute("UPDATE metadata SET value=? WHERE key='manifest'", (canonical(manifest),))
            self._event(db, "inputs_frozen", manifest)

    def reserve(self, round_number, raw):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT value FROM metadata WHERE key='state'").fetchone()[0])
            if state != "running":
                raise ValueError("研究批次已封存，不允许新增候选")
            cursor = db.execute("INSERT INTO attempts(round,spec,status) VALUES (?,?,?)",
                                (round_number, canonical(raw), "reserved"))
            self._event(db, "candidate_reserved", {"attempt_id": cursor.lastrowid, "spec": raw})
            return cursor.lastrowid

    def identify(self, attempt_id, spec):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute("SELECT 1 FROM factors WHERE factor_id=?", (spec.factor_id,)).fetchone() is not None
            if not duplicate:
                db.execute("INSERT INTO factors VALUES (?,?,?,?)", (spec.factor_id, canonical(spec.to_dict()), "candidate", None))
            db.execute("UPDATE attempts SET factor_id=? WHERE id=?", (spec.factor_id, attempt_id))
        return duplicate

    def finish(self, attempt_id, status, feedback=None, artifact=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE attempts SET status=?,feedback=?,artifact=?,error=? WHERE id=?",
                       (status, canonical(feedback or {}), artifact, error, attempt_id))
            if status != "duplicate":
                db.execute("UPDATE factors SET lifecycle=?,evidence=? WHERE factor_id="
                           "(SELECT factor_id FROM attempts WHERE id=?)", (status, artifact, attempt_id))
            self._event(db, "candidate_finished", {"attempt_id": attempt_id, "status": status,
                                                   "feedback": feedback or {}, "error": error})

    def set_lifecycle(self, factor_id, lifecycle, evidence):
        with self.connect() as db:
            cursor = db.execute("UPDATE factors SET lifecycle=?,evidence=? WHERE factor_id=?",
                                (lifecycle, evidence, factor_id))
            if cursor.rowcount != 1:
                raise ValueError("生命周期更新指向未知因子")
            self._event(db, "factor_lifecycle", {"factor_id": factor_id, "lifecycle": lifecycle, "evidence": evidence})

    def catalog(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM factors ORDER BY factor_id").fetchall()
        return [{**dict(row), "spec": json.loads(row["spec"])} for row in rows]

    def transition(self, expected, new_state, payload=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            cursor = db.execute("UPDATE metadata SET value=? WHERE key='state' AND value=?",
                                (canonical(new_state), canonical(expected)))
            if cursor.rowcount != 1:
                raise ValueError(f"批次状态不允许此操作；要求 {expected}，审核不可重复打开")
            self._event(db, new_state, payload or {})

    def attempts(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM attempts ORDER BY id").fetchall()
        result = []
        for row in rows:
            value = dict(row)
            for field in ("spec", "feedback"):
                value[field] = json.loads(value[field]) if value[field] else None
            result.append(value)
        return result
