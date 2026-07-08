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


# ---------------------------------------------------------------------------
# record_spend() and budget enforcement in claim()
# ---------------------------------------------------------------------------

def _write_budget_spec(tmp_path: Path, max_tokens: int | None = None, max_cost: float | None = None) -> str:
    from loop_spec import BudgetSpec
    spec: dict = {
        "name": "budget-test",
        "kind": "MetricOptimizationKind",
        "direction": "lower_is_better",
        "metric": "test",
    }
    budget: dict = {}
    if max_tokens is not None:
        budget["max_tokens_total"] = max_tokens
    if max_cost is not None:
        budget["max_cost_usd"] = max_cost
    if budget:
        spec["budget"] = budget
    p = _write_spec(tmp_path, spec)
    return str(p)


def test_record_spend_accumulates(tmp_path):
    q = _make_queue(tmp_path)
    spec_path = _write_budget_spec(tmp_path, max_tokens=10000)
    task_id = q.post({"spec_path": spec_path})
    q.record_spend(task_id, tokens=1000, cost_usd=0.05)
    q.record_spend(task_id, tokens=500, cost_usd=0.02)
    task = q.get(task_id)
    assert task["tokens_used"] == 1500
    assert abs(task["cost_usd"] - 0.07) < 1e-9


def test_claim_raises_budget_exhausted_on_token_ceiling(tmp_path):
    q = _make_queue(tmp_path)
    spec_path = _write_budget_spec(tmp_path, max_tokens=1000)
    task_id = q.post({"spec_path": spec_path})
    # Exhaust the budget
    q.record_spend(task_id, tokens=1000)
    with pytest.raises(BudgetExhausted, match="max_tokens_total"):
        q.claim()


def test_claim_raises_budget_exhausted_on_cost_ceiling(tmp_path):
    q = _make_queue(tmp_path)
    spec_path = _write_budget_spec(tmp_path, max_cost=5.00)
    task_id = q.post({"spec_path": spec_path})
    q.record_spend(task_id, cost_usd=5.00)
    with pytest.raises(BudgetExhausted, match="max_cost_usd"):
        q.claim()


def test_claim_succeeds_when_under_budget(tmp_path):
    q = _make_queue(tmp_path)
    spec_path = _write_budget_spec(tmp_path, max_tokens=10000)
    task_id = q.post({"spec_path": spec_path})
    q.record_spend(task_id, tokens=9999)
    result = q.claim()
    assert result is not None
    assert result["task_id"] == task_id


def test_claim_no_budget_field_succeeds(tmp_path):
    """Tasks without a budget spec are never budget-exhausted."""
    q = _make_queue(tmp_path)
    task_id = q.post({"name": "no-budget", "kind": "TaskExecutionKind"})
    q.record_spend(task_id, tokens=999999)
    result = q.claim()
    assert result is not None


def test_budget_exhausted_exported(tmp_path):
    from saturate import BudgetExhausted as BE
    assert issubclass(BE, Exception)
