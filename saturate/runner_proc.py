"""saturate.runner_proc — thin subprocess entry point for the scheduler.

Usage (spawned by scheduler.py):
    python -m saturate.runner_proc <task_id> <queue_db_path>

The scheduler claims the task in-process before launching this subprocess,
OR this process claims it.  For Phase 1 we claim here so the scheduler does
not need to claim on behalf of the subprocess (avoids race with concurrent
workers).

Writes a heartbeat timestamp to the task's metadata so the scheduler can
detect stale tasks.

Exit codes:
    0 — turn completed successfully (any outcome including 'terminal')
    1 — unexpected error
"""

from __future__ import annotations

import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def main() -> int:
    if len(sys.argv) < 3:
        print(
            "Usage: python -m saturate.runner_proc <task_id> <queue_db_path>",
            file=sys.stderr,
        )
        return 1

    task_id = sys.argv[1]
    db_path = sys.argv[2]

    # Import here to avoid circular imports at module level
    from saturate.queue_sqlite import SqliteQueue
    from saturate.runner import run_turn

    # Derive base_dir from the db_path so the queue uses the same root
    base_dir = str(Path(db_path).parent)
    queue = SqliteQueue(db_path=db_path, base_dir=base_dir)

    # Claim the specific task (the scheduler posted it as pending)
    # We need to claim by task_id, but SqliteQueue.claim() takes the next
    # highest-priority pending task.  Claim and verify we got the right one.
    claimed = queue.claim()
    if claimed is None:
        print(
            f"runner_proc: no pending task to claim (wanted {task_id})", file=sys.stderr
        )
        return 1

    claimed_id: str = claimed.get("task_id", "")
    if claimed_id != task_id:
        # We got a different task — requeue it and abort
        # (another worker may have already claimed our target)
        queue.requeue(claimed_id)
        print(
            f"runner_proc: claimed {claimed_id!r} but wanted {task_id!r}; requeued",
            file=sys.stderr,
        )
        return 1

    # Write initial heartbeat into metadata so stale-detection works
    try:
        queue.update_metadata(task_id, {"heartbeat": _now_iso()})
    except Exception:
        pass

    try:
        outcome = run_turn(task_id, queue)  # type: ignore[arg-type]
        print(f"runner_proc: {task_id} → {outcome}", flush=True)
        return 0
    except Exception as exc:
        traceback.print_exc()
        print(f"runner_proc: {task_id} crashed: {exc}", file=sys.stderr)
        # Requeue so the scheduler can retry
        try:
            queue.requeue(task_id)
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
