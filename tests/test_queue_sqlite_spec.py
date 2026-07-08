"""Tests for spec-aware SqliteQueue additions:
   - post() derives name/kind/human_gated from spec_path
   - complete() enforces HUMAN_GATED via confirmed_by_human
   - cancel() moves task to done with terminal_reason
   - find() returns task_id by name/kind
   - HumanGatedViolation is raised correctly
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

from saturate.queue_sqlite import HumanGatedViolation, SqliteQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_queue(tmp_path: Path) -> SqliteQueue:
    return SqliteQueue(base_dir=str(tmp_path / "saturate"))


def _write_spec(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "spec.yaml"
    p.write_text(yaml.dump(data))
    return str(p)


def _metric_spec(tmp_path: Path) -> str:
    return _write_spec(tmp_path, {
        "name": "coverage-opt",
        "kind": "MetricOptimizationKind",
        "direction": "higher_is_better",
        "metric": "test coverage",
    })


def _clarification_spec(tmp_path: Path) -> str:
    return _write_spec(tmp_path, {
        "name": "requirements-clarification",
        "kind": "ClarificationKind",
    })


# ---------------------------------------------------------------------------
# post() derives fields from spec
# ---------------------------------------------------------------------------

def test_post_derives_name_from_spec(tmp_path):
    q = _make_queue(tmp_path)
    spec_path = _metric_spec(tmp_path)
    task_id = q.post({"spec_path": spec_path})
    task = q.get(task_id)
    assert task["name"] == "coverage-opt"


def test_post_derives_kind_from_spec(tmp_path):
    q = _make_queue(tmp_path)
    spec_path = _metric_spec(tmp_path)
    task_id = q.post({"spec_path": spec_path})
    task = q.get(task_id)
    assert task["kind"] == "MetricOptimizationKind"


def test_post_human_gated_false_for_metric_optimization(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"spec_path": _metric_spec(tmp_path)})
    task = q.get(task_id)
    assert task.get("human_gated", 0) == 0


def test_post_human_gated_true_for_clarification(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"spec_path": _clarification_spec(tmp_path)})
    task = q.get(task_id)
    assert task.get("human_gated") == 1


def test_post_without_spec_path_still_works(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"name": "manual", "kind": "TaskExecutionKind"})
    assert q.get(task_id)["name"] == "manual"


# ---------------------------------------------------------------------------
# complete() HUMAN_GATED enforcement
# ---------------------------------------------------------------------------

def test_complete_human_gated_raises_without_confirmation(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"spec_path": _clarification_spec(tmp_path)})
    with pytest.raises(HumanGatedViolation):
        q.complete(task_id, {"terminal_reason": "done"})


def test_complete_human_gated_succeeds_with_confirmation(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"spec_path": _clarification_spec(tmp_path)})
    q.complete(task_id, {"terminal_reason": "HumanConfirmed"}, confirmed_by_human=True)
    task = q.get(task_id)
    assert task["status"] == "done"


def test_complete_non_gated_does_not_require_confirmation(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"spec_path": _metric_spec(tmp_path)})
    q.complete(task_id, {"terminal_reason": "stalled"})
    assert q.get(task_id)["status"] == "done"


# ---------------------------------------------------------------------------
# cancel()
# ---------------------------------------------------------------------------

def test_cancel_moves_task_to_done(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"name": "t", "kind": "TaskExecutionKind"})
    q.cancel(task_id)
    assert q.get(task_id)["status"] == "done"


def test_cancel_records_reason(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"name": "t", "kind": "TaskExecutionKind"})
    q.cancel(task_id, reason="user aborted")
    task = q.get(task_id)
    assert "user aborted" in (task.get("terminal_reason") or "")


def test_cancel_default_reason(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"name": "t", "kind": "TaskExecutionKind"})
    q.cancel(task_id)
    task = q.get(task_id)
    assert "user request" in (task.get("terminal_reason") or "")


# ---------------------------------------------------------------------------
# find()
# ---------------------------------------------------------------------------

def test_find_returns_task_id_by_name(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"name": "my-loop", "kind": "TaskExecutionKind"})
    assert q.find("my-loop") == task_id


def test_find_returns_none_when_not_found(tmp_path):
    q = _make_queue(tmp_path)
    assert q.find("nonexistent") is None


def test_find_with_kind_filter(tmp_path):
    q = _make_queue(tmp_path)
    task_id = q.post({"name": "x", "kind": "TaskExecutionKind"})
    q.post({"name": "x", "kind": "ConsensusKind"})
    assert q.find("x", kind="TaskExecutionKind") == task_id


def test_find_returns_most_recent(tmp_path):
    q = _make_queue(tmp_path)
    q.post({"name": "same", "kind": "TaskExecutionKind"})
    task_id2 = q.post({"name": "same", "kind": "TaskExecutionKind"})
    assert q.find("same") == task_id2


def test_find_from_spec_name(tmp_path):
    q = _make_queue(tmp_path)
    spec_path = _metric_spec(tmp_path)
    task_id = q.post({"spec_path": spec_path})
    # name derived from spec = "coverage-opt"
    assert q.find("coverage-opt", kind="MetricOptimizationKind") == task_id
