"""The bridge's durable state: one sqlite database. Tasks, rounds, check results and per-agent model defaults.

The database is the restart-recovery source of truth: on service start every task in a live state is loaded
back and its interrupted round is marked failed (resumable), so a bridge crash never loses track of a
half-created worktree. Token values and provider secrets never appear here: agents authenticate with a
sha256 hash, and model aliases reference env var names only.
"""
import json
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    agent        TEXT NOT NULL,
    project      TEXT NOT NULL,
    spec         TEXT NOT NULL,
    done_criteria TEXT NOT NULL,
    mode         TEXT NOT NULL,
    base_branch  TEXT NOT NULL,
    branch       TEXT NOT NULL,
    worktree     TEXT NOT NULL,
    base_commit  TEXT NOT NULL,
    status       TEXT NOT NULL,
    revision     INTEGER NOT NULL DEFAULT 0,
    round        INTEGER NOT NULL DEFAULT 0,
    max_rounds   INTEGER NOT NULL,
    worker_status TEXT,
    questions    TEXT,
    summary      TEXT,
    model        TEXT NOT NULL,
    models_used  TEXT NOT NULL DEFAULT '[]',
    stop_reason  TEXT,
    error        TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL,
    started_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(agent, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    n INTEGER NOT NULL,
    model TEXT NOT NULL,
    session_id TEXT,
    session_file TEXT,
    prompt_bytes INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    ended_at REAL,
    worker_status TEXT,
    stop_reason TEXT,
    last_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_rounds_task ON rounds(task_id, n);
CREATE TABLE IF NOT EXISTS checks (
    task_id TEXT NOT NULL,
    round INTEGER NOT NULL,
    name TEXT NOT NULL,
    ok INTEGER NOT NULL,
    exit_code INTEGER NOT NULL,
    output_tail TEXT NOT NULL,
    PRIMARY KEY (task_id, round, name)
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def new_task_id():
    """Unique and filesystem-safe: the worktree directory, branch suffix and evidence dir all use it."""
    return "t" + uuid.uuid4().hex[:12]


class Store:
    """One connection, one lock: the HTTP threads and the round workers meet here."""

    def __init__(self, path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        with self._lock, self._db:
            self._db.executescript(SCHEMA)

    def _run(self, sql, args=()):
        with self._lock, self._db:
            return self._db.execute(sql, args)

    def _one(self, sql, args=()):
        # The fetch MUST happen under the lock: the connection is shared by every thread, and another thread's statement
        # between execute() and fetchone() invalidates the cursor (a just-inserted row then reads back as None).
        with self._lock, self._db:
            return self._db.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        with self._lock, self._db:
            return self._db.execute(sql, args).fetchall()

    # --- tasks ---------------------------------------------------------------

    @staticmethod
    def new_task_id():
        return new_task_id()

    def create_task(self, task_id, agent, project, spec, done_criteria, mode, base_branch, branch, worktree,
                    base_commit, max_rounds, model):
        now = time.time()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO tasks (task_id, agent, project, spec, done_criteria, mode, base_branch, branch,"
                " worktree, base_commit, status, revision, round, max_rounds, model, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,'queued',0,0,?,?,?,?)",
                (task_id, agent, project, spec, json.dumps(done_criteria), mode, base_branch, branch, worktree,
                 base_commit, max_rounds, model, now, now))
        return task_id

    def get_task(self, task_id, agent=None):
        """One task as a dict, or None. With `agent`, another agent's task is invisible (not_found)."""
        row = self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if row is None or (agent is not None and row["agent"] != agent):
            return None
        return dict(row)

    def list_tasks(self, agent, limit=50):
        rows = self._all("SELECT * FROM tasks WHERE agent=? ORDER BY updated_at DESC LIMIT ?", (agent, limit))
        return [dict(row) for row in rows]

    def count_active(self):
        """Tasks occupying a worker slot (max_active_tasks is enforced against this)."""
        row = self._one("SELECT COUNT(*) AS n FROM tasks WHERE status IN ('queued','running','awaiting_review',"
                        "'needs_info','blocked')")
        return row["n"]

    def bump(self, task_id, status, **fields):
        """A state change: status, a fresh revision, and any task fields, atomically."""
        fields = dict(fields)
        if "models_used" in fields and not isinstance(fields["models_used"], str):
            fields["models_used"] = json.dumps(fields["models_used"])
        if "questions" in fields and fields["questions"] is not None and not isinstance(fields["questions"], str):
            fields["questions"] = json.dumps(fields["questions"])
        assignments = ", ".join([f"{key}=?" for key in fields])
        sql = (f"UPDATE tasks SET status=?, revision=revision+1, updated_at=?, {assignments} WHERE task_id=?"
               if assignments else
               "UPDATE tasks SET status=?, revision=revision+1, updated_at=? WHERE task_id=?")
        args = [status, time.time(), *fields.values(), task_id] if assignments else [status, time.time(), task_id]
        with self._lock, self._db:
            cursor = self._db.execute(sql, args)
            if cursor.rowcount != 1:
                raise RuntimeError(f"bump matched {cursor.rowcount} rows for {task_id}: {sql}")
        task = self.get_task(task_id)
        if task is None:
            raise RuntimeError(f"task {task_id} vanished after bump to {status}")
        return task

    def set_fields(self, task_id, **fields):
        """Fields without a status change or revision bump (internal bookkeeping)."""
        if "models_used" in fields and not isinstance(fields["models_used"], str):
            fields["models_used"] = json.dumps(fields["models_used"])
        if "questions" in fields and fields["questions"] is not None and not isinstance(fields["questions"], str):
            fields["questions"] = json.dumps(fields["questions"])
        assignments = ", ".join([f"{key}=?" for key in fields])
        with self._lock, self._db:
            self._db.execute(f"UPDATE tasks SET updated_at=?, {assignments} WHERE task_id=?",
                             [time.time(), *fields.values(), task_id])

    def live_tasks(self):
        """Every task a previous bridge process may have been running (restart recovery)."""
        return [dict(row) for row in self._all(
            "SELECT * FROM tasks WHERE status IN ('queued','running') ORDER BY created_at")]

    # --- rounds ----------------------------------------------------------------

    def start_round(self, task_id, n, model, session_id, prompt_bytes):
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO rounds (task_id, n, model, session_id, prompt_bytes, started_at) VALUES (?,?,?,?,?,?)",
                (task_id, n, model, session_id, prompt_bytes, time.time()))
            return cur.lastrowid

    def end_round(self, round_pk, worker_status, stop_reason, last_message, session_id=None, session_file=None):
        with self._lock, self._db:
            self._db.execute(
                "UPDATE rounds SET ended_at=?, worker_status=?, stop_reason=?, last_message=?,"
                " session_id=COALESCE(?, session_id), session_file=COALESCE(?, session_file) WHERE id=?",
                (time.time(), worker_status, stop_reason, last_message, session_id, session_file, round_pk))

    def last_round(self, task_id):
        row = self._one("SELECT * FROM rounds WHERE task_id=? ORDER BY n DESC LIMIT 1", (task_id,))
        return dict(row) if row else None

    def round_by_n(self, task_id, n):
        row = self._one("SELECT * FROM rounds WHERE task_id=? AND n=?", (task_id, n))
        return dict(row) if row else None

    def rounds(self, task_id):
        return [dict(row) for row in self._all("SELECT * FROM rounds WHERE task_id=? ORDER BY n", (task_id,))]

    # --- checks ------------------------------------------------------------------

    def put_checks(self, task_id, n, results):
        """results: [{name, ok, exit_code, output_tail}] for one round, replacing any previous row set."""
        with self._lock, self._db:
            self._db.execute("DELETE FROM checks WHERE task_id=? AND round=?", (task_id, n))
            self._db.executemany(
                "INSERT INTO checks (task_id, round, name, ok, exit_code, output_tail) VALUES (?,?,?,?,?,?)",
                [(task_id, n, r["name"], 1 if r["ok"] else 0, r["exit_code"], r["output_tail"]) for r in results])

    def get_checks(self, task_id, n=None):
        if n is None:
            rows = self._all("SELECT * FROM checks WHERE task_id=? ORDER BY round, name", (task_id,))
        else:
            rows = self._all("SELECT * FROM checks WHERE task_id=? AND round=? ORDER BY name", (task_id, n))
        return [dict(row) for row in rows]

    # --- kv: per-agent defaults and the uploaded models.yml hash -------------------

    def kv_get(self, key):
        row = self._one("SELECT v FROM kv WHERE k=?", (key,))
        return row["v"] if row else None

    def kv_set(self, key, value):
        with self._lock, self._db:
            self._db.execute("INSERT INTO kv (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                             (key, value))

    def agent_default_model(self, agent):
        return self.kv_get(f"default_model:{agent}")

    def set_agent_default_model(self, agent, alias):
        self.kv_set(f"default_model:{agent}", alias)

    def models_yml_hash(self):
        return self.kv_get("models_yml_hash")

    def set_models_yml_hash(self, digest):
        self.kv_set("models_yml_hash", digest)

    def close(self):
        with self._lock:
            self._db.close()
