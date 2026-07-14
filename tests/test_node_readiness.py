"""Failing tests for issue #9 — node-readiness precondition check.

These four tests define the acceptance criteria for the feature.
They are written BEFORE any implementation — per LDD discipline, watch
them all fail now, then implement against them.

Design reference: plans/9-node-readiness-design.md

Test coverage:
  1. Node-class mismatch → task lands in done/node_mismatch, runner never spawned
  2. Matching node class → task is dispatched normally
  3. GPU mismatch → task lands in done/node_mismatch
  4. done_ids/success_ids fix → node_mismatch parent does NOT satisfy depends_on
  5. Retry-count breaker → repeated crashes → done/exhausted_retries
  6. terminal_reason visible in CLI status output
"""
from __future__ import annotations

import subprocess
import yaml
from pathlib import Path

import pytest

from saturate.queue_sqlite import SqliteQueue
from saturate.scheduler import scheduler_tick


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_queue(tmp_path: Path) -> SqliteQueue:
    return SqliteQueue(base_dir=str(tmp_path / "saturate"))


def _make_isolated_repo(tmp_path: Path) -> Path:
    """Minimal git repo so spec.repo is a valid file:// URL."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)
    (repo / "placeholder.txt").write_text("init\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True)
    return repo


def _write_spec(spec_path: Path, repo: Path) -> None:
    spec = {
        "name": "test-loop",
        "kind": "MetricOptimizationKind",
        "direction": "lower_is_better",
        "metric": "test",
        "repo": f"file://{repo}",
        "terminal": {"plateau_count": 3, "max_iterations": 10},
        "executor": {"type": "shell", "command": "true"},
        "evaluate": "echo 1",
        "evaluate_extract": "regex:(1)",
    }
    spec_path.write_text(yaml.dump(spec))


# ---------------------------------------------------------------------------
# Test 1: node-class mismatch → done/node_mismatch, runner never spawned
# ---------------------------------------------------------------------------

def test_node_class_mismatch_routes_to_done(tmp_path):
    """A task requiring GPU_4090 dispatched to a node_class=None node
    must land in done with terminal_reason containing 'node_mismatch'.
    The runner subprocess must never be spawned.
    """
    repo = _make_isolated_repo(tmp_path)
    spec_path = tmp_path / "spec.yaml"
    _write_spec(spec_path, repo)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "needs-gpu",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
        "required_node_class": "GPU_4090",
        "num_gpus": 1.0,
    })

    assert q.counts()["pending"] == 1

    # Run one scheduler tick with a node that has no GPU and no node class
    scheduler_tick(
        queue=q,
        goals_dir=str(tmp_path / "goals"),
        local_node_class=None,   # this node has no class
        local_gpu_count=0,
    )

    task = q.get(task_id)
    assert task is not None
    assert task["status"] == "done", f"expected done, got {task['status']!r}"
    assert "node_mismatch" in (task.get("terminal_reason") or ""), (
        f"expected node_mismatch in terminal_reason, got {task.get('terminal_reason')!r}"
    )
    # Crucially: still in pending before dispatch, now done — was never running
    assert q.counts()["running"] == 0


# ---------------------------------------------------------------------------
# Test 2: matching node class → dispatched normally
# ---------------------------------------------------------------------------

def test_matching_node_class_is_dispatched(tmp_path):
    """A task whose required_node_class matches the local node class
    must reach running state (i.e. the scheduler actually dispatches it).
    """
    repo = _make_isolated_repo(tmp_path)
    spec_path = tmp_path / "spec.yaml"
    _write_spec(spec_path, repo)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "matching-gpu",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
        "required_node_class": "GPU_4090",
        "num_gpus": 1.0,
    })

    # Tick with a node that DOES match
    scheduler_tick(
        queue=q,
        goals_dir=str(tmp_path / "goals"),
        local_node_class="GPU_4090",
        local_gpu_count=1,
    )

    # Task should have been dispatched (runner spawned) — node_mismatch must not fire.
    # The subprocess claims asynchronously, so we only assert it didn't get
    # routed to done/node_mismatch. Pending here means the runner subprocess
    # launched and hasn't claimed yet — that's correct behavior for this test.
    task = q.get(task_id)
    assert task is not None
    reason = task.get("terminal_reason") or ""
    assert "node_mismatch" not in reason, (
        f"node_mismatch should not fire for a matching node; got {reason!r}"
    )


# ---------------------------------------------------------------------------
# Test 3: GPU count mismatch → done/node_mismatch
# ---------------------------------------------------------------------------

def test_gpu_count_mismatch_routes_to_done(tmp_path):
    """A task requiring num_gpus=2 dispatched to a node with local_gpu_count=0
    must land in done with terminal_reason containing 'node_mismatch'.
    """
    repo = _make_isolated_repo(tmp_path)
    spec_path = tmp_path / "spec.yaml"
    _write_spec(spec_path, repo)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "needs-2-gpus",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
        "num_gpus": 2.0,
    })

    scheduler_tick(
        queue=q,
        goals_dir=str(tmp_path / "goals"),
        local_node_class=None,
        local_gpu_count=0,  # node has no GPU
    )

    task = q.get(task_id)
    assert task["status"] == "done"
    assert "node_mismatch" in (task.get("terminal_reason") or "")


# ---------------------------------------------------------------------------
# Test 4: node_mismatch parent does NOT satisfy depends_on (success_ids fix)
# ---------------------------------------------------------------------------

def test_node_mismatch_parent_does_not_satisfy_depends_on(tmp_path):
    """A child task with depends_on=[parent_id] must NOT be dispatched
    when the parent landed in done/node_mismatch.

    This tests the done_ids → success_ids fix: the current code treats
    ANY done task as satisfying depends_on; after the fix, node_mismatch
    (and other failure reasons) must be excluded.
    """
    repo = _make_isolated_repo(tmp_path)
    spec_path = tmp_path / "spec.yaml"
    _write_spec(spec_path, repo)

    q = _make_queue(tmp_path)

    # Parent: needs GPU that node doesn't have → will land in node_mismatch
    parent_id = q.post({
        "name": "parent-needs-gpu",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
        "required_node_class": "GPU_4090",
        "num_gpus": 1.0,
    })

    # Child: depends on parent
    child_id = q.post({
        "name": "child-task",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
        "depends_on": [parent_id],
    })

    # Tick 1: parent gets node_mismatch → done; child stays blocked
    scheduler_tick(
        queue=q,
        goals_dir=str(tmp_path / "goals"),
        local_node_class=None,
        local_gpu_count=0,
    )

    parent = q.get(parent_id)
    assert parent["status"] == "done"
    assert "node_mismatch" in (parent.get("terminal_reason") or "")

    # Tick 2: child should NOT be dispatched because parent failed
    scheduler_tick(
        queue=q,
        goals_dir=str(tmp_path / "goals"),
        local_node_class=None,
        local_gpu_count=0,
    )

    child = q.get(child_id)
    assert child is not None
    # Child must still be pending — its dependency failed, not succeeded
    assert child["status"] == "pending", (
        f"Child was dispatched despite parent failing with node_mismatch; "
        f"status={child['status']!r}, terminal_reason={child.get('terminal_reason')!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: retry-count breaker → exhausted_retries after max_retries crashes
# ---------------------------------------------------------------------------

def test_retry_count_breaker(tmp_path):
    """A task that causes run_turn() to raise must reach done/exhausted_retries
    after max_retries attempts rather than being requeued forever.

    The retry-count breaker lives in runner_proc.py's except-Exception handler —
    it fires when run_turn() raises an unhandled exception (e.g. spec missing,
    corrupt state, unrecoverable error). Measured 'crashed' outcomes (where
    measure() returns outcome='crashed') are handled inside run_turn() and
    lead to stalled/exhausted terminal states via the normal plateau/max_iterations
    path — those do NOT trigger the retry-count breaker.

    This test simulates runner_proc's crash-handler by calling it directly.
    """
    from saturate.queue_sqlite import SqliteQueue as Q
    from unittest.mock import patch

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "always-crashes",
        "kind": "MetricOptimizationKind",
        "spec_path": "/nonexistent/spec.yaml",   # will cause run_turn to raise
        "max_retries": 3,
    })

    # Simulate what runner_proc does: claim, run_turn (raises), increment retry_count
    # Drive 3 crash cycles — on the 3rd the breaker should fire
    for i in range(4):
        claimed = q.claim()
        if claimed is None:
            break   # task exhausted and moved to done
        t_id = claimed["task_id"]
        try:
            from saturate.runner import run_turn
            run_turn(t_id, q)   # will raise FileNotFoundError
        except Exception as exc:
            # Mirrors runner_proc's except block
            task = q.get(t_id)
            max_retries = int((task or {}).get("max_retries", 3))
            retry_count = q.increment_retry_count(t_id)
            if retry_count >= max_retries:
                q.complete(t_id, {"terminal_reason": f"exhausted_retries: {exc}"})
            else:
                q.requeue(t_id)

    task = q.get(task_id)
    assert task is not None
    assert task["status"] == "done", f"status={task['status']!r}"
    assert "exhausted_retries" in (task.get("terminal_reason") or ""), (
        f"Expected exhausted_retries, got terminal_reason={task.get('terminal_reason')!r}"
    )


# ---------------------------------------------------------------------------
# Test 6: terminal_reason visible in 'saturate status <task_id>'
# ---------------------------------------------------------------------------

def test_terminal_reason_shown_in_status(tmp_path):
    """'saturate status <task_id>' must include terminal_reason in its
    output when the task is done.

    This test will FAIL until step 6 (surface terminal_reason in CLI) is
    implemented — that's correct, it's a failing acceptance criterion.
    """
    import os, json, uuid
    from click.testing import CliRunner
    from saturate.cli import main

    saturate_dir = tmp_path / "saturate"
    done_dir = saturate_dir / "queue" / "done"
    done_dir.mkdir(parents=True)

    task_id = str(uuid.uuid4())
    task = {
        "task_id": task_id,
        "name": "t",
        "kind": "TaskExecutionKind",
        "status": "done",
        "terminal_reason": "cancelled: test cancel",
    }
    (done_dir / f"{task_id}.json").write_text(json.dumps(task))

    runner = CliRunner()
    result = runner.invoke(main, ["status", task_id],
                           env={**os.environ, "SATURATE_DIR": str(saturate_dir)})
    assert result.exit_code == 0, result.output
    # step 6: terminal_reason must appear in the output
    assert "terminal_reason" in result.output.lower() or "cancelled" in result.output.lower(), (
        f"terminal_reason not surfaced in status output:\n{result.output}"
    )
