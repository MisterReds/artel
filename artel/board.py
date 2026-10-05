"""Kanban board: SQLite task DAG with compare-and-swap transitions.

Modeled on Hermes ``kanban_db``: WAL, ``BEGIN IMMEDIATE`` and CAS on
``revision`` so exactly one claimer wins; each attempt is a row in ``runs``.
Every transition is also written to the project event log.

Statuses:
    todo     waiting on dependencies
    ready    claimable (subject to ``backoff_until``)
    running  claimed by a worker
    review   submitted, gates + reviewer pending
    done     accepted
    blocked  needs the manager (or a human) to decide
    cancelled
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Optional

STATUSES = ("todo", "ready", "running", "review", "done", "blocked", "cancelled")
OPEN = ("todo", "ready", "running", "review", "blocked")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL DEFAULT 'work',      -- goal | work
    status        TEXT NOT NULL,
    assignee      TEXT,
    priority      INTEGER NOT NULL DEFAULT 0,
    criteria      TEXT NOT NULL DEFAULT '[]',        -- JSON list of acceptance criteria
    checks        TEXT NOT NULL DEFAULT '[]',        -- JSON list of shell gate commands
    result        TEXT,
    evidence      TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    no_progress   INTEGER NOT NULL DEFAULT 0,
    backoff_until REAL NOT NULL DEFAULT 0,
    needs_human   INTEGER NOT NULL DEFAULT 0,
    revision      INTEGER NOT NULL DEFAULT 1,
    created_by    TEXT,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    started_at    REAL,
    completed_at  REAL
);
CREATE TABLE IF NOT EXISTS deps (
    task_id    TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    PRIMARY KEY (task_id, depends_on)
);
CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL,
    author     TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT NOT NULL,
    worker      TEXT NOT NULL,
    status      TEXT NOT NULL,       -- running | submitted | blocked | stalled | turn_limit | error | refused
    turns       INTEGER NOT NULL DEFAULT 0,
    tool_calls  INTEGER NOT NULL DEFAULT 0,
    tokens_in   INTEGER NOT NULL DEFAULT 0,
    tokens_out  INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    summary     TEXT,
    heartbeat   REAL,
    started_at  REAL NOT NULL,
    ended_at    REAL
);
"""


class StaleRevision(Exception):
    pass


class Board:
    def __init__(self, path: Path, log=None):
        self.path = Path(path)
        self.log = log
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(runs)")}
        if "cost_usd" not in cols:  # boards created before cost tracking
            self._db.execute("ALTER TABLE runs ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0")

    # --- helpers --------------------------------------------------------
    def _event(self, kind: str, **payload: Any) -> None:
        if self.log is not None:
            self.log.append(kind, **payload)

    @staticmethod
    def _task(row: sqlite3.Row) -> dict:
        t = dict(row)
        t["criteria"] = json.loads(t["criteria"] or "[]")
        t["checks"] = json.loads(t["checks"] or "[]")
        return t

    def _tx(self):
        db = self._db

        class _Tx:
            def __enter__(self_inner):
                self._lock.acquire()
                db.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self_inner, exc_type, *_):
                try:
                    db.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    self._lock.release()

        return _Tx()

    # --- tasks ----------------------------------------------------------
    def add_task(
        self,
        title: str,
        body: str = "",
        *,
        kind: str = "work",
        assignee: Optional[str] = None,
        criteria: Iterable[str] = (),
        checks: Iterable[str] = (),
        depends_on: Iterable[str] = (),
        priority: int = 0,
        created_by: str = "manager",
        task_id: Optional[str] = None,
    ) -> str:
        tid = task_id or "t-" + uuid.uuid4().hex[:8]
        now = time.time()
        deps = list(depends_on)
        with self._tx() as db:
            db.execute(
                "INSERT INTO tasks (id,title,body,kind,status,assignee,priority,criteria,checks,"
                "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, title, body, kind, "todo", assignee, priority, json.dumps(list(criteria), ensure_ascii=False),
                 json.dumps(list(checks), ensure_ascii=False), created_by, now, now),
            )
            for d in deps:
                db.execute("INSERT OR IGNORE INTO deps (task_id, depends_on) VALUES (?,?)", (tid, d))
        self._event("board/task_created", task_id=tid, title=title, task_kind=kind, assignee=assignee,
                    depends_on=deps, criteria=list(criteria), checks=list(checks), created_by=created_by)
        return tid

    def add_dep(self, task_id: str, depends_on: str) -> None:
        if task_id == depends_on or depends_on in self.ancestors(task_id):
            raise ValueError(f"dependency {task_id} -> {depends_on} would create a cycle")
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO deps (task_id, depends_on) VALUES (?,?)", (task_id, depends_on))

    def remove_dep(self, task_id: str, depends_on: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM deps WHERE task_id=? AND depends_on=?", (task_id, depends_on))

    def ancestors(self, task_id: str) -> set:
        """All tasks that (transitively) depend on ``task_id``."""
        seen: set = set()
        frontier = [task_id]
        while frontier:
            cur = frontier.pop()
            with self._lock:
                rows = self._db.execute("SELECT task_id FROM deps WHERE depends_on=?", (cur,)).fetchall()
            for r in rows:
                if r[0] not in seen:
                    seen.add(r[0])
                    frontier.append(r[0])
        return seen

    def deps_of(self, task_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT depends_on FROM deps WHERE task_id=?", (task_id,))]

    def dependents_of(self, task_id: str) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT task_id FROM deps WHERE depends_on=?", (task_id,))]

    def get(self, task_id: str) -> Optional[dict]:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def list(self, status: Optional[Iterable[str]] = None, kind: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM tasks WHERE 1=1", []
        if status:
            st = list(status)
            sql += f" AND status IN ({','.join('?' * len(st))})"
            args += st
        if kind:
            sql += " AND kind=?"
            args.append(kind)
        sql += " ORDER BY priority DESC, created_at"
        with self._lock:
            return [self._task(r) for r in self._db.execute(sql, args)]

    def update(self, task_id: str, *, expect_revision: Optional[int] = None, actor: str = "manager",
               **fields: Any) -> dict:
        """CAS update; raises StaleRevision when ``expect_revision`` no longer matches."""
        for k in ("criteria", "checks"):
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields["updated_at"] = time.time()
        if fields.get("status") == "running":
            fields.setdefault("started_at", time.time())
        if fields.get("status") == "done":
            fields.setdefault("completed_at", time.time())
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._tx() as db:
            before = db.execute("SELECT status, revision FROM tasks WHERE id=?", (task_id,)).fetchone()
            if before is None:
                raise KeyError(task_id)
            if expect_revision is not None and before["revision"] != expect_revision:
                raise StaleRevision(f"{task_id}: revision {before['revision']} != {expect_revision}")
            db.execute(f"UPDATE tasks SET {cols}, revision=revision+1 WHERE id=?", (*fields.values(), task_id))
        changed = {k: v for k, v in fields.items() if k != "updated_at"}
        if "status" in fields and fields["status"] != before["status"]:
            self._event("board/task_moved", task_id=task_id, frm=before["status"], to=fields["status"], actor=actor)
        if set(changed) - {"status"}:
            self._event("board/task_updated", task_id=task_id, actor=actor,
                        fields={k: v for k, v in changed.items() if k not in ("status",)})
        return self.get(task_id)

    def claim(self, task_id: str, worker: str) -> bool:
        """Atomically move ready -> running; exactly one caller wins."""
        now = time.time()
        with self._tx() as db:
            cur = db.execute(
                "UPDATE tasks SET status='running', assignee=?, started_at=COALESCE(started_at, ?), updated_at=?,"
                " revision=revision+1 WHERE id=? AND status='ready' AND backoff_until<=?",
                (worker, now, now, task_id, now),
            )
            won = cur.rowcount == 1
        if won:
            self._event("board/task_moved", task_id=task_id, frm="ready", to="running", actor=worker)
        return won

    def refresh_readiness(self) -> list[str]:
        """Promote todo -> ready when every dependency is done or cancelled."""
        promoted = []
        for t in self.list(status=["todo"]):
            if t["kind"] == "goal":
                continue
            deps = [self.get(d) for d in self.deps_of(t["id"])]
            if all(d is None or d["status"] in ("done", "cancelled") for d in deps):
                self.update(t["id"], status="ready", actor="board")
                promoted.append(t["id"])
        return promoted

    # --- comments (blackboard) -----------------------------------------
    def comment(self, task_id: str, author: str, body: str) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                       (task_id, author, body, time.time()))
        self._event("board/comment", task_id=task_id, author=author, body=body)

    def comments(self, task_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM comments WHERE task_id=? ORDER BY id", (task_id,))]

    # --- runs -----------------------------------------------------------
    def start_run(self, task_id: str, worker: str) -> int:
        now = time.time()
        with self._tx() as db:
            cur = db.execute("INSERT INTO runs (task_id, worker, status, heartbeat, started_at) VALUES (?,?,?,?,?)",
                             (task_id, worker, "running", now, now))
            run_id = cur.lastrowid
        self._event("run/started", task_id=task_id, worker=worker, run_id=run_id)
        return run_id

    def heartbeat(self, run_id: int, **counters: int) -> None:
        sets = ", ".join(f"{k}=?" for k in counters)
        with self._tx() as db:
            db.execute(f"UPDATE runs SET heartbeat=?{', ' + sets if sets else ''} WHERE id=?",
                       (time.time(), *counters.values(), run_id))

    def finish_run(self, run_id: int, status: str, summary: str = "", **counters: int) -> None:
        sets = "".join(f", {k}=?" for k in counters)
        with self._tx() as db:
            db.execute(f"UPDATE runs SET status=?, summary=?, ended_at=?{sets} WHERE id=?",
                       (status, summary, time.time(), *counters.values(), run_id))
        self._event("run/finished", run_id=run_id, status=status, summary=summary[:2000], **counters)

    def runs(self, task_id: Optional[str] = None) -> list[dict]:
        with self._lock:
            if task_id:
                rows = self._db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id", (task_id,))
            else:
                rows = self._db.execute("SELECT * FROM runs ORDER BY id")
            return [dict(r) for r in rows]

    def stale_runs(self, older_than: float) -> list[dict]:
        cutoff = time.time() - older_than
        with self._lock:
            return [dict(r) for r in self._db.execute(
                "SELECT * FROM runs WHERE status='running' AND heartbeat<?", (cutoff,))]

    # --- views ----------------------------------------------------------
    def snapshot(self) -> dict:
        tasks = self.list()
        for t in tasks:
            t["depends_on"] = self.deps_of(t["id"])
            t["comments"] = self.comments(t["id"])
            t["runs"] = self.runs(t["id"])
        counts = {s: 0 for s in STATUSES}
        for t in tasks:
            counts[t["status"]] += 1
        return {"tasks": tasks, "counts": counts}

    def close(self) -> None:
        with self._lock:
            self._db.close()
