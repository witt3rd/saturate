"""saturate.scheduler — scheduler tick for the Saturate loop execution fabric.

scheduler_tick(queue, goals_dir) -> int

Runs one scheduling cycle:
  1. Harvest completed tasks (status=done) — write summary.md to output_path.
  2. Reap stale running tasks — revert to pending if heartbeat > 120s ago.
  3. Seed goals — post any .yaml files in goals_dir not already in queue.
  4. Dispatch eligible pending tasks to the local node via subprocess.

Phase 1: single-node, spawns `python -m saturate.runner_proc <task_id> <db_path>`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import psutil
import yaml

from saturate.task import SaturateTask, dispatch_score

if TYPE_CHECKING:
    from saturate.queue_sqlite import SqliteQueue


def _scheduler_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STALE_SECONDS: float = 120.0  # running task with no heartbeat for this long → requeued
_CPU_SATURATION: float = 80.0  # don't launch new workers above this CPU%


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scheduler_tick(
    queue: "SqliteQueue",
    goals_dir: str,
    local_node_class: "str | None" = None,
    local_gpu_count: int = 0,
) -> int:
    """Run one scheduler tick.  Returns number of tasks dispatched this tick.

    Steps
    -----
    1. Harvest done tasks — write summary to each task's output_path.
    2. Reap stale running tasks — requeue tasks with no heartbeat for >120 s.
    3. Seed from goals_dir — post any .yaml files not yet in the queue.
    4. Dispatch eligible pending tasks while local CPU is not saturated.
    """
    now = datetime.now(tz=timezone.utc)

    # -- 1. Harvest ----------------------------------------------------------
    _harvest_done(queue)

    # -- 2. Reap stale -------------------------------------------------------
    _reap_stale(queue, now)

    # -- 3. Seed goals -------------------------------------------------------
    _seed_goals(queue, goals_dir)

    # -- 4. Dispatch ---------------------------------------------------------
    return _dispatch(
        queue, now, local_node_class=local_node_class, local_gpu_count=local_gpu_count
    )


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------


def _harvest_done(queue: "SqliteQueue") -> None:
    """Write summary.md to output_path for each done task (idempotent)."""
    for task in queue.list_tasks("done"):
        output_path = task.get("output_path")
        if not output_path:
            continue
        summary_path = Path(output_path) / "summary.md"
        if summary_path.exists():
            continue  # already harvested
        try:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            task_id = task.get("task_id", "unknown")
            name = task.get("name", task_id)
            reason = task.get("terminal_reason", "done")
            completed_at = task.get("completed_at", "")
            summary_path.write_text(
                f"# Task Summary: {name}\n\n"
                f"- **task_id**: {task_id}\n"
                f"- **terminal_reason**: {reason}\n"
                f"- **completed_at**: {completed_at}\n"
            )
        except Exception:
            pass  # best-effort; don't let harvest failure block other work


def _reap_stale(queue: "SqliteQueue", now: datetime) -> None:
    """Requeue running tasks whose heartbeat is older than _STALE_SECONDS."""
    for task in queue.list_tasks("running"):
        task_id = task.get("task_id")
        if task_id is None:
            continue
        heartbeat_iso: str | None = task.get("heartbeat")
        if heartbeat_iso is None:
            # No heartbeat ever written → treat submitted_at as the epoch.
            submitted_iso: str | None = task.get("submitted_at")
            if submitted_iso is None:
                # Can't determine age → leave alone.
                continue
            epoch_iso = submitted_iso
        else:
            epoch_iso = heartbeat_iso

        try:
            epoch = _parse_iso(epoch_iso)
        except (ValueError, TypeError):
            continue

        # Ensure both are timezone-aware for comparison
        if epoch.tzinfo is None:
            epoch = epoch.replace(tzinfo=timezone.utc)
        age_seconds = (now - epoch).total_seconds()
        if age_seconds > _STALE_SECONDS:
            queue.requeue(task_id)


def _seed_goals(queue: "SqliteQueue", goals_dir: str) -> None:
    """Post any .yaml spec files in goals_dir that aren't already in the queue.

    The spec file must contain at least a ``name`` and ``kind`` field.
    The task_id is derived from the stem of the filename so re-seeding is
    idempotent (the queue will ignore a duplicate task_id on post via the
    PRIMARY KEY constraint).
    """
    goals_path = Path(goals_dir)
    if not goals_path.exists():
        return

    for spec_file in sorted(goals_path.glob("*.yaml")):
        task_id = spec_file.stem
        if queue.get(task_id) is not None:
            continue  # already posted

        try:
            spec = yaml.safe_load(spec_file.read_text()) or {}
        except Exception:
            continue

        # Guard: required_node_class/num_gpus must NOT be in the loop-spec YAML body.
        # LoopSpec uses model_config={"extra": "forbid"} — these fields would cause
        # a Pydantic ValidationError on every turn, producing misleading exhausted_retries
        # output. Catch this at seed time with a clear, actionable error.
        for forbidden_field in ("required_node_class", "num_gpus"):
            if forbidden_field in spec:
                raise ValueError(
                    f"{spec_file}: '{forbidden_field}' must not be set inside the loop-spec "
                    f"YAML body — loop-spec uses extra='forbid' and will fail on every turn. "
                    f"Pass {forbidden_field!r} as a separate task field via "
                    f"'saturate submit --{forbidden_field.replace('_', '-')} <value>' instead."
                )

        queue.post(
            {
                "task_id": task_id,
                "name": spec.get("name", task_id),
                "kind": spec.get("kind", "task-execution"),
                "spec_path": str(spec_file),
                "state_path": str(goals_path.parent / "state" / task_id),
                "output_path": str(goals_path.parent / "output" / task_id),
                "priority": int(spec.get("priority", 50)),
                **{
                    k: v
                    for k, v in spec.items()
                    if k
                    not in (
                        "name",
                        "kind",
                        "spec_path",
                        "state_path",
                        "output_path",
                        "priority",
                    )
                },
            }
        )


def _dispatch(
    queue: "SqliteQueue",
    now: datetime,
    local_node_class: "str | None" = None,
    local_gpu_count: int = 0,
) -> int:
    """Dispatch eligible pending tasks, returning the count dispatched."""
    pending = queue.list_tasks("pending")
    if not pending:
        return 0

    # Build set of completed task_ids for dependency checking
    done_ids: set[str] = {
        t["task_id"] for t in queue.list_tasks("done") if "task_id" in t
    }

    # Filter eligible tasks
    eligible = []
    for task in pending:
        # depends_on: all must be done
        depends_on = task.get("depends_on") or []
        if isinstance(depends_on, str):
            depends_on = [depends_on]
        if any(dep not in done_ids for dep in depends_on):
            continue

        # earliest_start: must have passed
        earliest_start_iso: str | None = task.get("earliest_start")
        if earliest_start_iso is not None:
            try:
                earliest = _parse_iso(earliest_start_iso)
                if earliest.tzinfo is None:
                    earliest = earliest.replace(tzinfo=timezone.utc)
                if now < earliest:
                    continue
            except (ValueError, TypeError):
                pass

        # node-class / GPU eligibility gate
        required_node_class = task.get("required_node_class")
        num_gpus = float(task.get("num_gpus") or 0.0)
        node_mismatch = False
        if required_node_class and local_node_class != required_node_class:
            node_mismatch = True
        if num_gpus > 0 and local_gpu_count < num_gpus:
            node_mismatch = True
        if node_mismatch:
            task_id = task.get("task_id")
            if task_id:
                reason = (
                    f"node_mismatch: needs {required_node_class or 'gpu:' + str(num_gpus)}, "
                    f"local node is {local_node_class!r} with {local_gpu_count} gpu(s)"
                )
                conn = queue._connect()
                try:
                    conn.execute(
                        "UPDATE tasks SET status='done', terminal_reason=?, completed_at=? WHERE task_id=?",
                        (reason, _scheduler_now_iso(), task_id),
                    )
                    conn.commit()
                finally:
                    conn.close()
            continue

        eligible.append(task)

    if not eligible:
        return 0

    # Build SaturateTask objects for dispatch_score (best-effort)
    scored: list[tuple[float, dict]] = []
    for task in eligible:
        score = _score_task(task, now)
        scored.append((score, task))

    # Sort descending by score (highest priority first)
    scored.sort(key=lambda t: t[0], reverse=True)

    dispatched = 0
    for score, task in scored:
        # Check CPU saturation before each launch
        try:
            cpu = psutil.cpu_percent(interval=0.0)
        except Exception:
            cpu = 0.0
        if cpu >= _CPU_SATURATION:
            break

        task_id = task.get("task_id")
        if task_id is None:
            continue

        db_path = str(queue._db_path)
        _launch_runner(task_id, db_path)
        dispatched += 1

    return dispatched


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _score_task(task: dict, now: datetime) -> float:
    """Compute dispatch_score from a raw task dict (best-effort)."""
    try:
        deadline_iso: str | None = task.get("deadline")
        deadline = _parse_iso(deadline_iso) if deadline_iso else None

        # Build a minimal SaturateTask for scoring
        st = SaturateTask(
            task_id=task.get("task_id", ""),
            name=task.get("name", ""),
            kind=task.get("kind", ""),
            spec_path=task.get("spec_path", ""),
            state_path=task.get("state_path", ""),
            output_path=task.get("output_path", ""),
            priority=int(task.get("priority", 50)),
            deadline=deadline,
            estimated_duration_seconds=int(task.get("estimated_duration_seconds", 0)),
        )
        return dispatch_score(st, now)
    except Exception:
        # If anything fails, fall back to pure priority score
        priority = int(task.get("priority", 50))
        return (100 - max(0, min(100, priority))) / 100.0


def _launch_runner(task_id: str, db_path: str) -> subprocess.Popen:
    """Spawn a subprocess running saturate.runner_proc.

    SATURATE_TASK and SATURATE_QUEUE_DIR are injected into the subprocess
    environment so that any code the runner invokes (including cyclus_queue
    backends) can detect the Saturate backend without parsing CLI args.
    """
    env = {
        **os.environ,
        "SATURATE_TASK": task_id,
        "SATURATE_TASK_ID": task_id,  # canonical var for cyclus_queue backend detection
        "SATURATE_QUEUE_DIR": str(Path(db_path).parent),
    }
    return subprocess.Popen(
        [sys.executable, "-m", "saturate.runner_proc", task_id, db_path],
        # Inherit stdout/stderr so logs surface in the parent terminal
        stdout=None,
        stderr=None,
        env=env,
    )


def _parse_iso(iso: str) -> datetime:
    """Parse an ISO-8601 string to datetime (Python 3.11+ supports Z suffix)."""
    # Replace trailing Z with +00:00 for Python < 3.11 compatibility
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)
