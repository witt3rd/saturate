"""Tests for saturate.scheduler — scheduler tick.

Acceptance criteria
-------------------
1. scheduler_tick with empty queue returns 0.
2. scheduler_tick with one pending task dispatches it, returns 1.
3. Stale running task (heartbeat > 120s ago) gets requeued to pending.
4. Tasks are dispatched in priority order (higher priority / lower number first).

Additional coverage
-------------------
- Goals directory seeding: .yaml files become pending tasks.
- Already-queued goals are not re-posted.
- Harvest: completed tasks get summary.md in output_path.
- Dependency gate: task with unsatisfied depends_on is not dispatched.
- earliest_start gate: task with future earliest_start is not dispatched.
"""
from __future__ import annotations

import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from saturate.queue_sqlite import SqliteQueue
from saturate.scheduler import (
    _STALE_SECONDS,
    _harvest_done,
    _reap_stale,
    _seed_goals,
    _dispatch,
    scheduler_tick,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_queue(tmp_path: Path) -> SqliteQueue:
    return SqliteQueue(base_dir=str(tmp_path / "saturate"))


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _old_iso(seconds_ago: float) -> str:
    old = datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)
    return old.isoformat()


def _future_iso(seconds_ahead: float) -> str:
    future = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds_ahead)
    return future.isoformat()


def _post_minimal(queue: SqliteQueue, task_id: str | None = None, **kwargs) -> str:
    """Post a minimal valid task."""
    task = {
        "name": kwargs.get("name", "test-task"),
        "kind": "task-execution",
        "spec_path": kwargs.get("spec_path", "/tmp/spec.yaml"),
        "state_path": "/tmp/state",
        "output_path": "/tmp/output",
        **kwargs,
    }
    if task_id is not None:
        task["task_id"] = task_id
    return queue.post(task)


# ---------------------------------------------------------------------------
# Acceptance criterion 1: empty queue returns 0
# ---------------------------------------------------------------------------


def test_scheduler_tick_empty_queue_returns_0(tmp_path):
    """scheduler_tick with no tasks returns 0 dispatched."""
    q = make_queue(tmp_path)
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    result = scheduler_tick(q, str(goals_dir))
    assert result == 0


# ---------------------------------------------------------------------------
# Acceptance criterion 2: one pending task → dispatched, returns 1
# ---------------------------------------------------------------------------


def test_scheduler_tick_one_pending_dispatches_1(tmp_path):
    """One pending task: scheduler seeds from goals and dispatches it."""
    q = make_queue(tmp_path)
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()

    # Create a goals spec file
    spec = {"name": "my-task", "kind": "task-execution"}
    spec_file = goals_dir / "task-alpha.yaml"
    spec_file.write_text(yaml.dump(spec))

    dispatched_ids = []

    def fake_launch(task_id: str, db_path: str):
        dispatched_ids.append(task_id)
        return MagicMock()

    with patch("saturate.scheduler._launch_runner", side_effect=fake_launch), \
         patch("saturate.scheduler.psutil") as mock_psutil:
        mock_psutil.cpu_percent.return_value = 0.0  # not saturated
        result = scheduler_tick(q, str(goals_dir))

    assert result == 1
    assert len(dispatched_ids) == 1
    assert dispatched_ids[0] == "task-alpha"  # stem of the yaml file


# ---------------------------------------------------------------------------
# Acceptance criterion 3: stale running task gets requeued
# ---------------------------------------------------------------------------


def test_stale_running_task_requeued(tmp_path):
    """Running task with heartbeat > 120s ago reverts to pending."""
    q = make_queue(tmp_path)

    # Post and claim a task (makes it running)
    task_id = _post_minimal(q, task_id="stale-task")
    q.claim()

    # Simulate old heartbeat by writing directly to metadata
    old_heartbeat = _old_iso(_STALE_SECONDS + 10)
    q.update_metadata(task_id, {"heartbeat": old_heartbeat})

    assert q.counts()["running"] == 1
    assert q.counts()["pending"] == 0

    now = datetime.now(tz=timezone.utc)
    _reap_stale(q, now)

    assert q.counts()["running"] == 0
    assert q.counts()["pending"] == 1


def test_fresh_running_task_not_requeued(tmp_path):
    """Running task with recent heartbeat is NOT requeued."""
    q = make_queue(tmp_path)

    task_id = _post_minimal(q, task_id="fresh-task")
    q.claim()

    # Write a recent heartbeat
    q.update_metadata(task_id, {"heartbeat": _now_iso()})

    now = datetime.now(tz=timezone.utc)
    _reap_stale(q, now)

    # Still running
    assert q.counts()["running"] == 1
    assert q.counts()["pending"] == 0


def test_running_task_no_heartbeat_uses_submitted_at(tmp_path):
    """Running task with no heartbeat uses submitted_at to determine staleness."""
    q = make_queue(tmp_path)

    # Post and claim the task
    task_id = _post_minimal(q, task_id="no-hb-task")

    # Manually manipulate submitted_at to be old via metadata (not a real column override,
    # but we'll test via the running behavior with a fresh submitted_at — it should NOT requeue)
    q.claim()

    # No heartbeat set → reap checks submitted_at
    # submitted_at is recent, so it should NOT requeue
    now = datetime.now(tz=timezone.utc)
    _reap_stale(q, now)

    assert q.counts()["running"] == 1  # not stale yet


# ---------------------------------------------------------------------------
# Acceptance criterion 4: tasks dispatched in priority order
# ---------------------------------------------------------------------------


def test_dispatch_priority_order(tmp_path):
    """Higher-priority tasks (lower number) are dispatched first."""
    q = make_queue(tmp_path)

    # Post three tasks with different priorities
    id_low = _post_minimal(q, task_id="low-pri", priority=80)
    id_mid = _post_minimal(q, task_id="mid-pri", priority=50)
    id_high = _post_minimal(q, task_id="high-pri", priority=10)

    dispatched_order = []

    def fake_launch(task_id: str, db_path: str):
        dispatched_order.append(task_id)
        return MagicMock()

    with patch("saturate.scheduler._launch_runner", side_effect=fake_launch), \
         patch("saturate.scheduler.psutil") as mock_psutil:
        mock_psutil.cpu_percent.return_value = 0.0
        now = datetime.now(tz=timezone.utc)
        count = _dispatch(q, now)

    assert count == 3
    # highest priority (lowest number) first
    assert dispatched_order[0] == id_high
    assert dispatched_order[1] == id_mid
    assert dispatched_order[2] == id_low


# ---------------------------------------------------------------------------
# Seeding from goals directory
# ---------------------------------------------------------------------------


def test_seed_goals_posts_yaml_files(tmp_path):
    """_seed_goals posts every .yaml in goals_dir as a pending task."""
    q = make_queue(tmp_path)
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()

    for i in range(3):
        (goals_dir / f"task-{i}.yaml").write_text(
            yaml.dump({"name": f"task {i}", "kind": "task-execution"})
        )

    _seed_goals(q, str(goals_dir))

    assert q.counts()["pending"] == 3


def test_seed_goals_idempotent(tmp_path):
    """Re-seeding the same goals dir does not create duplicate tasks."""
    q = make_queue(tmp_path)
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()
    (goals_dir / "my-task.yaml").write_text(
        yaml.dump({"name": "my task", "kind": "task-execution"})
    )

    _seed_goals(q, str(goals_dir))
    _seed_goals(q, str(goals_dir))  # second seed — should be no-op

    assert q.counts()["pending"] == 1


def test_seed_goals_empty_dir(tmp_path):
    """_seed_goals on an empty directory posts nothing."""
    q = make_queue(tmp_path)
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()

    _seed_goals(q, str(goals_dir))
    assert q.counts()["pending"] == 0


def test_seed_goals_missing_dir(tmp_path):
    """_seed_goals on a non-existent directory is a no-op (no exception)."""
    q = make_queue(tmp_path)
    _seed_goals(q, str(tmp_path / "does-not-exist"))
    assert q.counts()["pending"] == 0


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------


def test_harvest_writes_summary(tmp_path):
    """_harvest_done writes summary.md to output_path for each done task."""
    q = make_queue(tmp_path)
    output_dir = tmp_path / "output" / "task-done"
    task_id = _post_minimal(
        q,
        task_id="task-done",
        output_path=str(output_dir),
    )
    q.claim()
    q.complete(task_id, {"terminal_reason": "success"})

    _harvest_done(q)

    summary = output_dir / "summary.md"
    assert summary.exists()
    content = summary.read_text()
    assert "task-done" in content


def test_harvest_idempotent(tmp_path):
    """Calling _harvest_done twice does not raise or overwrite."""
    q = make_queue(tmp_path)
    output_dir = tmp_path / "output" / "harvest-idem"
    task_id = _post_minimal(q, task_id="harvest-idem", output_path=str(output_dir))
    q.claim()
    q.complete(task_id, {})

    _harvest_done(q)
    mtime1 = (output_dir / "summary.md").stat().st_mtime

    # Wait a tiny bit so mtime would differ if file was rewritten
    time.sleep(0.01)
    _harvest_done(q)
    mtime2 = (output_dir / "summary.md").stat().st_mtime

    assert mtime1 == mtime2  # file not rewritten


# ---------------------------------------------------------------------------
# Dependency gate
# ---------------------------------------------------------------------------


def test_dispatch_respects_depends_on(tmp_path):
    """Task with unsatisfied depends_on is not dispatched."""
    q = make_queue(tmp_path)

    # Post a prerequisite task (pending, not done)
    prereq_id = _post_minimal(q, task_id="prereq")

    # Post a dependent task
    dep_id = _post_minimal(q, task_id="dependent", depends_on=["prereq"])

    dispatched_ids = []

    def fake_launch(task_id: str, db_path: str):
        dispatched_ids.append(task_id)
        return MagicMock()

    with patch("saturate.scheduler._launch_runner", side_effect=fake_launch), \
         patch("saturate.scheduler.psutil") as mock_psutil:
        mock_psutil.cpu_percent.return_value = 0.0
        now = datetime.now(tz=timezone.utc)
        count = _dispatch(q, now)

    # Only prereq should be dispatched; dependent must wait
    assert count == 1
    assert prereq_id in dispatched_ids
    assert dep_id not in dispatched_ids


def test_dispatch_with_satisfied_depends_on(tmp_path):
    """Task with all deps done IS dispatched."""
    q = make_queue(tmp_path)

    # Post and complete prereq
    prereq_id = _post_minimal(q, task_id="prereq-done")
    q.claim()
    q.complete(prereq_id, {})

    # Post dependent task
    dep_id = _post_minimal(q, task_id="dep-ready", depends_on=["prereq-done"])

    dispatched_ids = []

    def fake_launch(task_id: str, db_path: str):
        dispatched_ids.append(task_id)
        return MagicMock()

    with patch("saturate.scheduler._launch_runner", side_effect=fake_launch), \
         patch("saturate.scheduler.psutil") as mock_psutil:
        mock_psutil.cpu_percent.return_value = 0.0
        now = datetime.now(tz=timezone.utc)
        count = _dispatch(q, now)

    assert count == 1
    assert dep_id in dispatched_ids


# ---------------------------------------------------------------------------
# earliest_start gate
# ---------------------------------------------------------------------------


def test_dispatch_respects_earliest_start_future(tmp_path):
    """Task with future earliest_start is NOT dispatched."""
    q = make_queue(tmp_path)
    future = _future_iso(3600)  # 1 hour from now
    _post_minimal(q, task_id="future-task", earliest_start=future)

    dispatched_ids = []

    def fake_launch(task_id: str, db_path: str):
        dispatched_ids.append(task_id)
        return MagicMock()

    with patch("saturate.scheduler._launch_runner", side_effect=fake_launch), \
         patch("saturate.scheduler.psutil") as mock_psutil:
        mock_psutil.cpu_percent.return_value = 0.0
        now = datetime.now(tz=timezone.utc)
        count = _dispatch(q, now)

    assert count == 0
    assert len(dispatched_ids) == 0


def test_dispatch_respects_earliest_start_passed(tmp_path):
    """Task with past earliest_start IS dispatched."""
    q = make_queue(tmp_path)
    past = _old_iso(10)  # 10 seconds ago
    task_id = _post_minimal(q, task_id="past-start-task", earliest_start=past)

    dispatched_ids = []

    def fake_launch(tid: str, db_path: str):
        dispatched_ids.append(tid)
        return MagicMock()

    with patch("saturate.scheduler._launch_runner", side_effect=fake_launch), \
         patch("saturate.scheduler.psutil") as mock_psutil:
        mock_psutil.cpu_percent.return_value = 0.0
        now = datetime.now(tz=timezone.utc)
        count = _dispatch(q, now)

    assert count == 1
    assert task_id in dispatched_ids


# ---------------------------------------------------------------------------
# CPU saturation gate
# ---------------------------------------------------------------------------


def test_dispatch_stops_when_cpu_saturated(tmp_path):
    """Dispatch stops launching when CPU usage >= 80%."""
    q = make_queue(tmp_path)
    for i in range(5):
        _post_minimal(q, task_id=f"cpu-task-{i}")

    dispatched_ids = []
    call_count = [0]

    def fake_launch(task_id: str, db_path: str):
        dispatched_ids.append(task_id)
        return MagicMock()

    def saturated_cpu(interval):
        # First call returns low; after first dispatch return saturated
        if len(dispatched_ids) >= 1:
            return 90.0
        return 10.0

    with patch("saturate.scheduler._launch_runner", side_effect=fake_launch), \
         patch("saturate.scheduler.psutil") as mock_psutil:
        mock_psutil.cpu_percent.side_effect = saturated_cpu
        now = datetime.now(tz=timezone.utc)
        count = _dispatch(q, now)

    # Should have dispatched exactly 1 (CPU was 10 for first check, then 90)
    assert count == 1


# ---------------------------------------------------------------------------
# Full tick integration
# ---------------------------------------------------------------------------


def test_full_tick_with_goals(tmp_path):
    """Full scheduler_tick: seeds goals, dispatches, returns correct count."""
    q = make_queue(tmp_path)
    goals_dir = tmp_path / "goals"
    goals_dir.mkdir()

    for i in range(2):
        (goals_dir / f"goal-{i}.yaml").write_text(
            yaml.dump({"name": f"goal {i}", "kind": "task-execution"})
        )

    dispatched_ids = []

    def fake_launch(task_id: str, db_path: str):
        dispatched_ids.append(task_id)
        return MagicMock()

    with patch("saturate.scheduler._launch_runner", side_effect=fake_launch), \
         patch("saturate.scheduler.psutil") as mock_psutil:
        mock_psutil.cpu_percent.return_value = 0.0
        count = scheduler_tick(q, str(goals_dir))

    assert count == 2
    assert len(dispatched_ids) == 2
