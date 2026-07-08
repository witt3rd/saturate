"""SQLite-backed work queue with the same interface as Queue.

Uses a single SQLite database file (default: ~/.saturate/queue.db).
State (write_state / read_state) is stored as JSON files in the same
directory layout as the file-based Queue so both implementations are
interchangeable by the runner.

Schema
------
tasks
    task_id         TEXT PRIMARY KEY
    name            TEXT
    kind            TEXT
    status          TEXT NOT NULL DEFAULT 'pending'   -- pending | running | done
    spec_path       TEXT
    state_path      TEXT
    output_path     TEXT
    priority        INT  NOT NULL DEFAULT 50           -- 0=highest … 100=lowest
    submitted_at    TEXT                               -- ISO-8601
    completed_at    TEXT                               -- ISO-8601, NULL while live
    terminal_reason TEXT
    human_gated     INT  NOT NULL DEFAULT 0             -- 1 if ClarificationKind
    metadata        TEXT NOT NULL DEFAULT '{}'         -- JSON blob for every other field

turns
    task_id         TEXT NOT NULL
    turn_n          INT  NOT NULL
    outcome         TEXT
    value           REAL
    correct         INT          -- 1=True / 0=False / NULL=unknown
    hypothesis_path TEXT
    raw             TEXT
    recorded_at     TEXT         -- ISO-8601

claim() uses BEGIN EXCLUSIVE to prevent double-claiming under concurrent access.
complete() sets status='done', completed_at=now, and merges the output dict
into the metadata JSON column.
requeue() sets status='pending'.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class HumanGatedViolation(Exception):
    """Raised when complete() is called on a HUMAN_GATED task without confirmed_by_human=True."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

# Columns that have a dedicated column in the tasks table.
# Anything posted with a key not in this set goes into the metadata JSON blob.
_TASK_COLUMNS: frozenset[str] = frozenset(
    {
        "task_id",
        "name",
        "kind",
        "status",
        "spec_path",
        "state_path",
        "output_path",
        "priority",
        "submitted_at",
        "completed_at",
        "terminal_reason",
        "human_gated",
    }
)

_DDL = """\
CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT PRIMARY KEY,
    name            TEXT,
    kind            TEXT,
    status          TEXT    NOT NULL DEFAULT 'pending',
    spec_path       TEXT,
    state_path      TEXT,
    output_path     TEXT,
    priority        INT     NOT NULL DEFAULT 50,
    submitted_at    TEXT,
    completed_at    TEXT,
    terminal_reason TEXT,
    metadata        TEXT    NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS turns (
    task_id         TEXT    NOT NULL,
    turn_n          INT     NOT NULL,
    outcome         TEXT,
    value           REAL,
    correct         INT,
    hypothesis_path TEXT,
    raw             TEXT,
    recorded_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks (status, priority, submitted_at);

CREATE INDEX IF NOT EXISTS idx_turns_task
    ON turns (task_id, turn_n);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a tasks row to a flat dict, expanding the metadata JSON blob."""
    d = dict(row)
    raw_meta = d.pop("metadata", "{}")
    meta: dict = json.loads(raw_meta) if raw_meta else {}
    # Keep only non-None column values so callers don't see spurious None entries
    result = {k: v for k, v in d.items() if v is not None}
    # metadata fields overlay on top; column values win for conflicts
    merged = {**meta, **result}
    return merged


# ---------------------------------------------------------------------------
# SqliteQueue
# ---------------------------------------------------------------------------


class SqliteQueue:
    """SQLite-backed work queue compatible with the file-based Queue API."""

    def __init__(
        self,
        db_path: str | None = None,
        base_dir: str | None = None,
    ) -> None:
        root = Path(base_dir) if base_dir else Path.home() / ".saturate"

        # Database file
        if db_path is not None:
            self._db_path = Path(db_path)
        else:
            self._db_path = root / "queue.db"

        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # State directory (file-based, same layout as Queue so runner.py works)
        # runner.py accesses queue._state directly as a Path
        self._state: Path = root / "state"
        self._state.mkdir(parents=True, exist_ok=True)

        self._init_db()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        """Open a connection in autocommit mode (isolation_level=None)."""
        conn = sqlite3.connect(
            str(self._db_path),
            timeout=30,
            isolation_level=None,  # explicit transaction management
        )
        conn.row_factory = sqlite3.Row
        # WAL mode allows concurrent readers during writes
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(_DDL)
            # Migrate existing DBs — add columns introduced after initial schema
            for stmt in [
                "ALTER TABLE tasks ADD COLUMN human_gated INT NOT NULL DEFAULT 0",
            ]:
                try:
                    conn.execute(stmt)
                    conn.commit()
                except Exception:
                    pass  # column already exists
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Core four operations
    # ------------------------------------------------------------------

    def post(self, task: dict) -> str:
        """Assign task_id if absent, insert into tasks table, return task_id.

        If ``spec_path`` is present, the spec is loaded via ``loop_spec.load_spec()``
        and ``name``, ``kind``, and ``human_gated`` are derived from it.
        The spec is the source of truth — it overrides any caller-supplied values
        for those fields.
        """
        from loop_spec import ClarificationSpec
        from loop_spec import load_spec as _load_spec

        task = dict(task)  # don't mutate caller's dict
        if "task_id" not in task:
            task["task_id"] = str(uuid.uuid4())
        if "submitted_at" not in task:
            task["submitted_at"] = _now_iso()
        # Derive metadata from spec when spec_path is present
        if "spec_path" in task:
            try:
                _spec = _load_spec(task["spec_path"])
                task["name"] = _spec.name
                task["kind"] = _spec.kind
                task["human_gated"] = 1 if isinstance(_spec, ClarificationSpec) else 0
            except Exception:
                pass  # spec unreadable at post time — runner will surface it

        task_id: str = task["task_id"]

        # Separate fields that have dedicated columns from extra metadata
        metadata = {k: v for k, v in task.items() if k not in _TASK_COLUMNS}

        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """INSERT INTO tasks (
                    task_id, name, kind, status,
                    spec_path, state_path, output_path,
                    priority, submitted_at, human_gated, metadata
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    task.get("name"),
                    task.get("kind"),
                    task.get("spec_path"),
                    task.get("state_path"),
                    task.get("output_path"),
                    int(task.get("priority", 50)),
                    task["submitted_at"],
                    int(task.get("human_gated", 0)),
                    json.dumps(metadata),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        return task_id

    def claim(self) -> Optional[dict]:
        """Atomically claim the highest-priority pending task.

        Uses BEGIN EXCLUSIVE to prevent two concurrent workers from both seeing
        the same task as 'pending' and double-claiming it.

        Returns the task dict with status='running', or None if no pending tasks.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN EXCLUSIVE")

            row = conn.execute(
                """SELECT * FROM tasks
                   WHERE status = 'pending'
                   ORDER BY priority ASC, submitted_at ASC
                   LIMIT 1"""
            ).fetchone()

            if row is None:
                conn.execute("ROLLBACK")
                return None

            task_id: str = row["task_id"]
            conn.execute(
                "UPDATE tasks SET status = 'running' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute("COMMIT")

            # Build return dict from the pre-UPDATE row, then override status
            task_dict = _row_to_dict(row)
            task_dict["status"] = "running"
            return task_dict

        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def write_state(self, task_id: str, state: dict) -> None:
        """Persist loop state for task_id as a JSON file (same as Queue)."""
        state_dir = self._state / task_id
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text(json.dumps(state, indent=2))

    def complete(
        self, task_id: str, output: dict, *, confirmed_by_human: bool = False
    ) -> None:
        """Set status='done', completed_at=now, merge output into metadata.

        Raises ``HumanGatedViolation`` if the task has ``human_gated=1`` and
        ``confirmed_by_human`` is not True — enforcing that a human explicitly
        confirmed completion before the loop closes.
        """
        # Enforce human gate before any state change
        with self._connect() as _c:
            _row = _c.execute(
                "SELECT human_gated FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if _row and _row["human_gated"] and not confirmed_by_human:
                raise HumanGatedViolation(
                    f"Task {task_id!r} is HUMAN_GATED — pass confirmed_by_human=True "
                    "only after explicit human confirmation."
                )

        conn = self._connect()
        try:
            conn.execute("BEGIN")

            row = conn.execute(
                "SELECT metadata FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Task {task_id} not found in queue")

            current_meta: dict = json.loads(row["metadata"]) if row["metadata"] else {}

            # Merge output into metadata, skipping fields that are protected
            # DB columns managed by complete() itself.
            _protected = {"task_id", "status", "completed_at", "submitted_at"}
            terminal_reason: Optional[str] = output.get("terminal_reason")

            for k, v in output.items():
                if k not in _protected and k != "terminal_reason":
                    current_meta[k] = v

            conn.execute(
                """UPDATE tasks SET
                    status          = 'done',
                    completed_at    = ?,
                    terminal_reason = COALESCE(?, terminal_reason),
                    metadata        = ?
                WHERE task_id = ?""",
                (
                    _now_iso(),
                    terminal_reason,
                    json.dumps(current_meta),
                    task_id,
                ),
            )
            conn.execute("COMMIT")

        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Auxiliary operations
    # ------------------------------------------------------------------

    def read_state(self, task_id: str) -> Optional[dict]:
        """Return state dict or None if not yet written."""
        path = self._state / task_id / "state.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def record_turn(self, task_id: str, turn_n: int, record: dict) -> None:
        """Insert a per-turn audit record into the turns table."""
        # Convert Python bool correct → SQLite INT 1/0/NULL
        correct_raw = record.get("correct")
        if correct_raw is True:
            correct_int: Optional[int] = 1
        elif correct_raw is False:
            correct_int = 0
        else:
            correct_int = None

        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """INSERT INTO turns
                    (task_id, turn_n, outcome, value, correct,
                     hypothesis_path, raw, recorded_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id,
                    turn_n,
                    record.get("outcome"),
                    record.get("value"),
                    correct_int,
                    record.get("hypothesis_path"),
                    record.get("raw"),
                    _now_iso(),
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def requeue(self, task_id: str) -> None:
        """Set status='pending' (e.g. when terminal condition not yet met)."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE tasks SET status = 'pending' WHERE task_id = ?",
                (task_id,),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def cancel(self, task_id: str, reason: str = "user request") -> None:
        """Cancel a pending or running task. Sets status='done' with terminal_reason='cancelled: <reason>'."""
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE tasks SET status='done', terminal_reason=?, completed_at=? WHERE task_id=?",
                (f"cancelled: {reason}", _now_iso(), task_id),
            )
            conn.commit()
        finally:
            conn.close()

    def find(self, name: str, kind: str | None = None) -> str | None:
        """Return the task_id of the most-recently posted task matching name (and optionally kind).

        Returns None if no match. Enables idempotent post patterns — check if
        a spec is already queued before posting again.
        """
        with self._connect() as conn:
            if kind:
                row = conn.execute(
                    "SELECT task_id FROM tasks WHERE name=? AND kind=? "
                    "ORDER BY submitted_at DESC LIMIT 1",
                    (name, kind),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT task_id FROM tasks WHERE name=? ORDER BY submitted_at DESC LIMIT 1",
                    (name,),
                ).fetchone()
            return row["task_id"] if row else None

    def counts(self) -> dict:
        """Return {pending, running, done} counts."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
        finally:
            conn.close()

        result = {"pending": 0, "running": 0, "done": 0}
        for row in rows:
            status = row["status"]
            if status in result:
                result[status] = row["n"]
        return result

    def get(self, task_id: str) -> Optional[dict]:
        """Find task regardless of status; return dict or None."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:
            return None
        return _row_to_dict(row)

    def list_tasks(self, status: str) -> list[dict]:
        """Return all tasks with the given status as a list of dicts."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY priority ASC, submitted_at ASC",
                (status,),
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_dict(row) for row in rows]

    def update_metadata(self, task_id: str, updates: dict) -> None:
        """Merge *updates* into the metadata JSON blob for task_id."""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT metadata FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                raise ValueError(f"Task {task_id} not found")
            meta: dict = json.loads(row["metadata"]) if row["metadata"] else {}
            meta.update(updates)
            conn.execute(
                "UPDATE tasks SET metadata = ? WHERE task_id = ?",
                (json.dumps(meta), task_id),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

    def turn_history(self, task_id: str) -> list[dict]:
        """Return all per-turn records sorted by turn_n ascending."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """SELECT * FROM turns
                   WHERE task_id = ?
                   ORDER BY turn_n ASC""",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]
