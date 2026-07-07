"""Tests for saturate.queue — file-based work queue."""

from __future__ import annotations

import json

import pytest

from saturate.queue import Queue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_queue(tmp_path) -> Queue:
    return Queue(base_dir=str(tmp_path / "saturate"))


# ---------------------------------------------------------------------------
# post
# ---------------------------------------------------------------------------


def test_post_creates_pending(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "test"})
    pending = tmp_path / "saturate" / "queue" / "pending" / f"{task_id}.json"
    assert pending.exists(), "post() must create file in pending/"


def test_post_assigns_task_id(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "no id given"})
    assert task_id, "post() must return a non-empty task_id"


def test_post_preserves_existing_task_id(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"task_id": "my-id", "goal": "test"})
    assert task_id == "my-id"


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------


def test_claim_returns_task(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "claim me"})
    task = q.claim()
    assert task is not None
    assert task["task_id"] == task_id
    assert task["goal"] == "claim me"


def test_claim_empty_returns_none(tmp_path):
    q = make_queue(tmp_path)
    assert q.claim() is None


def test_claim_atomic(tmp_path):
    """claim() moves task from pending/ to running/."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "atomic check"})

    pending = tmp_path / "saturate" / "queue" / "pending" / f"{task_id}.json"
    running = tmp_path / "saturate" / "queue" / "running" / f"{task_id}.json"

    assert pending.exists()
    assert not running.exists()

    q.claim()

    assert not pending.exists(), "pending/ file should be gone after claim()"
    assert running.exists(), "running/ file should exist after claim()"


# ---------------------------------------------------------------------------
# write_state / read_state
# ---------------------------------------------------------------------------


def test_write_and_read_state(tmp_path):
    q = make_queue(tmp_path)
    state = {"baseline": 1.23, "turn_count": 0, "stagnation_n": 0}
    q.write_state("abc123", state)
    result = q.read_state("abc123")
    assert result == state


def test_read_state_missing_returns_none(tmp_path):
    q = make_queue(tmp_path)
    assert q.read_state("nonexistent") is None


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def test_complete_moves_to_done(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "finish me"})
    q.claim()

    running = tmp_path / "saturate" / "queue" / "running" / f"{task_id}.json"
    done = tmp_path / "saturate" / "queue" / "done" / f"{task_id}.json"

    assert running.exists()
    q.complete(task_id, {"status": "success"})

    assert not running.exists(), "running/ file should be gone after complete()"
    assert done.exists(), "done/ file should exist after complete()"


def test_complete_merges_output(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "merge test"})
    q.claim()
    q.complete(task_id, {"status": "success", "score": 42})

    done = tmp_path / "saturate" / "queue" / "done" / f"{task_id}.json"
    data = json.loads(done.read_text())
    assert data["goal"] == "merge test"
    assert data["status"] == "success"
    assert data["score"] == 42


# ---------------------------------------------------------------------------
# requeue
# ---------------------------------------------------------------------------


def test_requeue_moves_to_pending(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "try again"})
    q.claim()

    running = tmp_path / "saturate" / "queue" / "running" / f"{task_id}.json"
    pending = tmp_path / "saturate" / "queue" / "pending" / f"{task_id}.json"

    assert running.exists()
    assert not pending.exists()

    q.requeue(task_id)

    assert not running.exists(), "running/ file should be gone after requeue()"
    assert pending.exists(), "pending/ file should exist after requeue()"


# ---------------------------------------------------------------------------
# counts
# ---------------------------------------------------------------------------


def test_counts(tmp_path):
    q = make_queue(tmp_path)

    id1 = q.post({"goal": "one"})
    id2 = q.post({"goal": "two"})

    # claim one and complete it
    q.claim()  # id1 (sorted first by uuid, but deterministic enough)
    # Find which was claimed
    running_files = list((tmp_path / "saturate" / "queue" / "running").glob("*.json"))
    assert len(running_files) == 1
    claimed_id = running_files[0].stem
    q.complete(claimed_id, {"status": "done"})

    counts = q.counts()
    assert counts["pending"] == 1
    assert counts["running"] == 0
    assert counts["done"] == 1


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_finds_pending(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "find me"})
    result = q.get(task_id)
    assert result is not None
    assert result["task_id"] == task_id


def test_get_finds_running(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "find in running"})
    q.claim()
    result = q.get(task_id)
    assert result is not None


def test_get_finds_done(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "find in done"})
    q.claim()
    q.complete(task_id, {})
    result = q.get(task_id)
    assert result is not None


def test_get_missing_returns_none(tmp_path):
    q = make_queue(tmp_path)
    assert q.get("does-not-exist") is None


# ---------------------------------------------------------------------------
# turn_history
# ---------------------------------------------------------------------------


def test_turn_history(tmp_path):
    q = make_queue(tmp_path)
    task_id = "hist-test"

    q.record_turn(task_id, 0, {"outcome": "unchanged", "value": 1.0})
    q.record_turn(task_id, 1, {"outcome": "improved", "value": 0.8})
    q.record_turn(task_id, 2, {"outcome": "regressed", "value": 1.2})

    history = q.turn_history(task_id)

    assert len(history) == 3
    assert history[0]["outcome"] == "unchanged"
    assert history[1]["outcome"] == "improved"
    assert history[2]["outcome"] == "regressed"


def test_turn_history_empty(tmp_path):
    q = make_queue(tmp_path)
    assert q.turn_history("no-turns") == []
