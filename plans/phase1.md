# Saturate Phase 1 — Implementation Plan

## Goal
Replace the file-based queue (phase 0) with a proper SQLite-backed queue,
add the SaturateTask dataclass with full scheduling fields, and implement
the scheduler tick with idle detection and priority dispatch.

All existing tests must continue to pass. New code must be tested.

---

## Task 1 — SaturateTask dataclass

**Files to create:** saturate/task.py, tests/test_task.py

**Description:**
Define the SaturateTask dataclass with all scheduling fields from ARCHITECTURE.md:
task_id (str, UUID), name (str), kind (str), priority (int 0-100),
deadline (Optional[datetime]), earliest_start (Optional[datetime]),
spawned_by (Optional[str]), depends_on (List[str]), num_cpus (float),
num_gpus (float, default 0.0), required_node_class (Optional[str]),
estimated_duration_seconds (int, default 0), spec_path (str),
max_retries (int, default 3), max_turns (Optional[int]),
budget_tokens (Optional[int]), stagnation_n (Optional[int]),
state_path (str), output_path (str), tags (List[str]),
submitted_by (str, default 'user'), submitted_at (datetime, default now).

Also implement dispatch_score(task, now) -> float for priority+deadline scoring.

**Acceptance criteria:**
- SaturateTask instantiates with minimal fields (task_id, name, kind, spec_path, state_path, output_path)
- dispatch_score returns float between 0.0 and 2.0
- P0 task (priority=0) scores higher than P50 (priority=50)
- Task with imminent deadline scores higher than identical task with no deadline

**Dependencies:** none

---

## Task 2 — SQLite queue backend

**Files to create:** saturate/queue_sqlite.py, tests/test_queue_sqlite.py

**Description:**
Implement SqliteQueue with same interface as Queue (post, claim, write_state,
read_state, record_turn, complete, requeue, counts, get, turn_history).
Use ~/.saturate/queue.db as the default database path. Schema:
- tasks table: task_id TEXT PK, name TEXT, kind TEXT, status TEXT, spec_path TEXT,
  state_path TEXT, output_path TEXT, priority INT DEFAULT 50, submitted_at TEXT,
  completed_at TEXT, terminal_reason TEXT, metadata TEXT (JSON blob for extra fields)
- turns table: task_id TEXT, turn_n INT, outcome TEXT, value REAL, correct INT,
  hypothesis_path TEXT, raw TEXT, recorded_at TEXT

claim() uses BEGIN EXCLUSIVE transaction to prevent double-claiming.
complete() sets status='done', completed_at=now, merges output into metadata.
requeue() sets status='pending'.

**Acceptance criteria:**
- All Queue interface methods work identically to file-based Queue
- claim() is safe under concurrent access (BEGIN EXCLUSIVE)
- complete() merges output dict into metadata JSON
- Existing tests still pass (file queue unchanged)
- New tests: test_sqlite_post, test_sqlite_claim_atomic, test_sqlite_complete,
  test_sqlite_requeue, test_sqlite_counts, test_sqlite_turn_history

**Dependencies:** task-1

---

## Task 3 — Scheduler tick

**Files to create:** saturate/scheduler.py, tests/test_scheduler.py

**Description:**
Implement the scheduler tick as a function: scheduler_tick(queue, goals_dir) -> int
(returns number of tasks dispatched this tick).

The tick:
1. Harvest completed tasks (status=done): call harvest(task) which logs to output_path/summary.md
2. Reap crashed/stale tasks: tasks in status=running with no heartbeat for > 120s revert to pending
3. Seed from goals directory: for each .yaml file in goals_dir not already in queue, post it
4. Dispatch eligible tasks to local node (single-node Phase 1):
   - eligible = status=pending, depends_on all completed, earliest_start passed
   - sort by dispatch_score descending
   - for each eligible task: if local CPU not saturated (psutil.cpu_percent < 80), launch runner

For Phase 1, "launch runner" means: spawn a subprocess running
  python -m saturate.runner_proc <task_id> <queue_db_path>

Also create saturate/runner_proc.py as a thin __main__ entry point:
  if __name__ == '__main__': claim task_id, call run_turn, exit

**Acceptance criteria:**
- scheduler_tick with empty queue returns 0
- scheduler_tick with one pending task seeds and dispatches it, returns 1
- stale running task (heartbeat > 120s ago) gets requeued
- tasks are dispatched in priority order (higher priority first)

**Dependencies:** task-1, task-2

---

## Task 4 — saturate start command + integration test

**Files to modify:** saturate/cli.py
**Files to create:** tests/test_integration.py

**Description:**
Add `saturate start` CLI command that runs the scheduler tick in a loop:
  saturate start [--interval 30] [--goals-dir ./goals]
Runs scheduler_tick every --interval seconds until Ctrl-C.

Also write an end-to-end integration test using a ShellExecutor spec:
- Create a spec with kind=metric-optimization, executor type=shell,
  a command that writes an improving value to a temp file each turn
- Submit it, run --loop, verify it eventually hits 'terminal' via stagnation

**Acceptance criteria:**
- saturate start --help works
- integration test: submit shell-executor spec, run --loop, get terminal in <= 5 turns
- all 49 existing tests still pass

**Dependencies:** task-1, task-2, task-3
