"""File-based work queue using JSON sidecars.

Directory layout under base_dir (default: ~/.saturate):

    queue/
        pending/   <task_id>.json   # submitted, not yet claimed
        running/   <task_id>.json   # claimed, in progress
        done/      <task_id>.json   # completed

    state/
        <task_id>/
            state.json              # loop state (baseline, turn_count, …)
            hypothesis.md           # written by executor each turn
            turns/
                <n>.json            # per-turn audit record

claim() is atomic on POSIX via os.rename (same filesystem).
No locking needed — single-node, single-process in Phase 0.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Optional


class Queue:
    def __init__(self, base_dir: str | None = None) -> None:
        root = Path(base_dir) if base_dir else Path.home() / ".saturate"
        self._pending = root / "queue" / "pending"
        self._running = root / "queue" / "running"
        self._done = root / "queue" / "done"
        self._state = root / "state"

        for d in (self._pending, self._running, self._done, self._state):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Core four operations
    # ------------------------------------------------------------------

    def post(self, task: dict) -> str:
        """Assign task_id if absent, write to pending/, return task_id."""
        task = dict(task)  # don't mutate caller's dict
        if "task_id" not in task:
            task["task_id"] = str(uuid.uuid4())
        task_id = task["task_id"]
        path = self._pending / f"{task_id}.json"
        path.write_text(json.dumps(task, indent=2))
        return task_id

    def claim(self) -> Optional[dict]:
        """Atomically move first pending task to running/. Return it or None."""
        candidates = sorted(self._pending.glob("*.json"))
        for path in candidates:
            running_path = self._running / path.name
            try:
                os.rename(path, running_path)  # POSIX atomic on same fs
                return json.loads(running_path.read_text())
            except FileNotFoundError:
                # Another process claimed it first — try next
                continue
        return None

    def write_state(self, task_id: str, state: dict) -> None:
        """Persist loop state for task_id."""
        state_dir = self._state / task_id
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "state.json").write_text(json.dumps(state, indent=2))

    def complete(self, task_id: str, output: dict) -> None:
        """Merge output into task JSON and move running/ → done/."""
        running_path = self._running / f"{task_id}.json"
        task = json.loads(running_path.read_text())
        task.update(output)
        # Write merged content back to running_path, then atomically rename.
        # (Writing to done_path first and then renaming would clobber the write.)
        running_path.write_text(json.dumps(task, indent=2))
        done_path = self._done / f"{task_id}.json"
        os.rename(running_path, done_path)

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
        """Write per-turn audit record."""
        turns_dir = self._state / task_id / "turns"
        turns_dir.mkdir(parents=True, exist_ok=True)
        (turns_dir / f"{turn_n}.json").write_text(json.dumps(record, indent=2))

    def requeue(self, task_id: str) -> None:
        """Move running/ → pending/ (e.g. on terminal-condition not yet met)."""
        running_path = self._running / f"{task_id}.json"
        os.rename(running_path, self._pending / f"{task_id}.json")

    def counts(self) -> dict:
        """Return {pending, running, done} file counts."""
        return {
            "pending": len(list(self._pending.glob("*.json"))),
            "running": len(list(self._running.glob("*.json"))),
            "done": len(list(self._done.glob("*.json"))),
        }

    def get(self, task_id: str) -> Optional[dict]:
        """Find task in any queue state; return dict or None."""
        for directory in (self._pending, self._running, self._done):
            path = directory / f"{task_id}.json"
            if path.exists():
                return json.loads(path.read_text())
        return None

    def turn_history(self, task_id: str) -> list[dict]:
        """Return all per-turn records sorted by turn number."""
        turns_dir = self._state / task_id / "turns"
        if not turns_dir.exists():
            return []
        records = []
        for path in sorted(turns_dir.glob("*.json"), key=lambda p: int(p.stem)):
            records.append(json.loads(path.read_text()))
        return records
