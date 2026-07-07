"""Tests for saturate.queue_sqlite — SQLite-backed work queue.

Covers the six named acceptance criteria:
    test_sqlite_post
    test_sqlite_claim_atomic
    test_sqlite_complete
    test_sqlite_requeue
    test_sqlite_counts
    test_sqlite_turn_history

Plus auxiliary coverage for state management and edge cases.
"""

from __future__ import annotations

import pytest

from saturate.queue_sqlite import SqliteQueue


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_queue(tmp_path) -> SqliteQueue:
    """Create an isolated SqliteQueue under tmp_path."""
    return SqliteQueue(base_dir=str(tmp_path / "saturate"))


# ===========================================================================
# test_sqlite_post
# ===========================================================================


def test_sqlite_post_assigns_task_id(tmp_path):
    """post() without task_id generates and returns a non-empty UUID string."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "test"})
    assert task_id, "post() must return a non-empty task_id"
    assert len(task_id) == 36, "Expected UUID format (36 chars)"


def test_sqlite_post_preserves_explicit_task_id(tmp_path):
    """post() with an explicit task_id preserves it."""
    q = make_queue(tmp_path)
    task_id = q.post({"task_id": "my-fixed-id", "goal": "test"})
    assert task_id == "my-fixed-id"


def test_sqlite_post_stores_task_as_pending(tmp_path):
    """Newly posted task appears in pending count."""
    q = make_queue(tmp_path)
    q.post({"goal": "store me"})
    counts = q.counts()
    assert counts["pending"] == 1
    assert counts["running"] == 0
    assert counts["done"] == 0


def test_sqlite_post_retrieves_all_fields(tmp_path):
    """get() after post() returns all posted fields, including extras."""
    q = make_queue(tmp_path)
    task_id = q.post({
        "goal": "retrieve me",
        "priority": 30,
        "spec_path": "/tmp/spec.yaml",
    })
    task = q.get(task_id)
    assert task is not None
    assert task["task_id"] == task_id
    assert task["goal"] == "retrieve me"
    assert task["spec_path"] == "/tmp/spec.yaml"


def test_sqlite_post_multiple_tasks(tmp_path):
    """Multiple posts increment pending count correctly."""
    q = make_queue(tmp_path)
    ids = [q.post({"goal": f"task {i}"}) for i in range(5)]
    assert len(set(ids)) == 5, "All task_ids must be unique"
    assert q.counts()["pending"] == 5


# ===========================================================================
# test_sqlite_claim_atomic
# ===========================================================================


def test_sqlite_claim_returns_task(tmp_path):
    """claim() returns the posted task dict."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "claim me"})
    task = q.claim()
    assert task is not None
    assert task["task_id"] == task_id
    assert task["goal"] == "claim me"


def test_sqlite_claim_empty_queue_returns_none(tmp_path):
    """claim() on an empty queue returns None."""
    q = make_queue(tmp_path)
    assert q.claim() is None


def test_sqlite_claim_atomic(tmp_path):
    """claim() transitions task from pending → running atomically."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "atomic check"})

    counts_before = q.counts()
    assert counts_before["pending"] == 1
    assert counts_before["running"] == 0

    task = q.claim()
    assert task is not None
    assert task["task_id"] == task_id

    counts_after = q.counts()
    assert counts_after["pending"] == 0
    assert counts_after["running"] == 1


def test_sqlite_claim_no_double_claim(tmp_path):
    """A second claim() after the first drains the queue returns None."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "once only"})

    claimed1 = q.claim()
    claimed2 = q.claim()  # queue is now empty

    assert claimed1 is not None
    assert claimed1["task_id"] == task_id
    assert claimed2 is None  # second claim finds nothing


def test_sqlite_claim_priority_order(tmp_path):
    """Lower priority number = higher urgency → claimed first."""
    q = make_queue(tmp_path)
    id_low = q.post({"goal": "low priority", "priority": 80})
    id_high = q.post({"goal": "high priority", "priority": 10})

    first = q.claim()
    assert first is not None
    assert first["task_id"] == id_high, (
        "Task with priority=10 must be claimed before priority=80"
    )


def test_sqlite_claim_fifo_when_same_priority(tmp_path):
    """When priorities are equal, earlier submitted_at wins (FIFO)."""
    import time
    q = make_queue(tmp_path)

    id_first = q.post({"goal": "first submitted"})
    time.sleep(0.01)                    # ensure distinct timestamps
    id_second = q.post({"goal": "second submitted"})

    claimed = q.claim()
    assert claimed is not None
    assert claimed["task_id"] == id_first


def test_sqlite_claim_sets_status_running(tmp_path):
    """Claimed task's returned dict shows status='running'."""
    q = make_queue(tmp_path)
    q.post({"goal": "check status"})
    task = q.claim()
    assert task is not None
    assert task.get("status") == "running"


# ===========================================================================
# test_sqlite_complete
# ===========================================================================


def test_sqlite_complete_moves_to_done(tmp_path):
    """complete() transitions task from running → done."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "complete me"})
    q.claim()
    q.complete(task_id, {"result": "ok"})

    counts = q.counts()
    assert counts["done"] == 1
    assert counts["running"] == 0
    assert counts["pending"] == 0


def test_sqlite_complete_merges_output(tmp_path):
    """complete() merges output dict into retrievable task data."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "merge test"})
    q.claim()
    q.complete(task_id, {"score": 42, "result": "success"})

    task = q.get(task_id)
    assert task is not None
    assert task["goal"] == "merge test"    # original field preserved
    assert task["score"] == 42            # output merged in
    assert task["result"] == "success"    # output merged in


def test_sqlite_complete_stores_terminal_reason(tmp_path):
    """complete() with terminal_reason makes it accessible via get()."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "finish"})
    q.claim()
    q.complete(task_id, {"terminal_reason": "exhausted", "final_metric": 0.85})

    task = q.get(task_id)
    assert task is not None
    # terminal_reason is its own column; final_metric goes into metadata
    assert task["final_metric"] == 0.85


def test_sqlite_complete_task_still_retrievable(tmp_path):
    """get() finds a done task by task_id."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "find me after done"})
    q.claim()
    q.complete(task_id, {})

    task = q.get(task_id)
    assert task is not None
    assert task["task_id"] == task_id


def test_sqlite_complete_unknown_task_raises(tmp_path):
    """complete() on a non-existent task_id raises ValueError."""
    q = make_queue(tmp_path)
    with pytest.raises(ValueError, match="not found"):
        q.complete("does-not-exist", {})


# ===========================================================================
# test_sqlite_requeue
# ===========================================================================


def test_sqlite_requeue_moves_to_pending(tmp_path):
    """requeue() transitions task from running → pending."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "try again"})
    q.claim()

    assert q.counts()["running"] == 1
    assert q.counts()["pending"] == 0

    q.requeue(task_id)

    assert q.counts()["pending"] == 1
    assert q.counts()["running"] == 0


def test_sqlite_requeue_can_be_reclaimed(tmp_path):
    """A requeued task is claimable again."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "requeue and reclaim"})
    q.claim()
    q.requeue(task_id)

    task = q.claim()
    assert task is not None
    assert task["task_id"] == task_id


def test_sqlite_requeue_preserves_task_data(tmp_path):
    """Original task fields survive a requeue cycle."""
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "preserve me", "custom_field": "hello"})
    q.claim()
    q.requeue(task_id)

    task = q.get(task_id)
    assert task is not None
    assert task["goal"] == "preserve me"
    assert task["custom_field"] == "hello"


# ===========================================================================
# test_sqlite_counts
# ===========================================================================


def test_sqlite_counts_empty_queue(tmp_path):
    """counts() on a fresh queue returns all zeros."""
    q = make_queue(tmp_path)
    assert q.counts() == {"pending": 0, "running": 0, "done": 0}


def test_sqlite_counts(tmp_path):
    """counts() reflects the correct distribution across states."""
    q = make_queue(tmp_path)
    q.post({"goal": "one"})
    q.post({"goal": "two"})
    q.post({"goal": "three"})

    # Claim one
    claimed = q.claim()
    assert claimed is not None

    # Complete the claimed task
    q.complete(claimed["task_id"], {})

    # Claim another (leave it running)
    q.claim()

    counts = q.counts()
    assert counts["pending"] == 1
    assert counts["running"] == 1
    assert counts["done"] == 1


def test_sqlite_counts_all_done(tmp_path):
    """All tasks done → pending and running are 0."""
    q = make_queue(tmp_path)
    ids = [q.post({"goal": f"task {i}"}) for i in range(3)]
    for _ in ids:
        t = q.claim()
        q.complete(t["task_id"], {})

    counts = q.counts()
    assert counts["pending"] == 0
    assert counts["running"] == 0
    assert counts["done"] == 3


# ===========================================================================
# test_sqlite_turn_history
# ===========================================================================


def test_sqlite_turn_history(tmp_path):
    """turn_history() returns all turns in ascending turn_n order."""
    q = make_queue(tmp_path)
    task_id = "hist-test"

    q.record_turn(task_id, 0, {"outcome": "unchanged", "value": 1.0})
    q.record_turn(task_id, 1, {"outcome": "improved",  "value": 0.8})
    q.record_turn(task_id, 2, {"outcome": "regressed", "value": 1.2})

    history = q.turn_history(task_id)

    assert len(history) == 3
    assert history[0]["outcome"] == "unchanged"
    assert history[1]["outcome"] == "improved"
    assert history[2]["outcome"] == "regressed"


def test_sqlite_turn_history_empty(tmp_path):
    """turn_history() on a task with no turns returns []."""
    q = make_queue(tmp_path)
    assert q.turn_history("no-turns") == []


def test_sqlite_turn_history_preserves_values(tmp_path):
    """record_turn() stores and turn_history() returns outcome and value."""
    q = make_queue(tmp_path)
    task_id = "value-check"

    q.record_turn(task_id, 0, {
        "outcome": "improved",
        "value": 0.42,
        "correct": True,
        "hypothesis_path": "/tmp/hyp.md",
        "raw": "some raw text",
    })

    history = q.turn_history(task_id)
    assert len(history) == 1
    rec = history[0]
    assert rec["outcome"] == "improved"
    assert abs(rec["value"] - 0.42) < 1e-9
    assert rec["correct"] == 1          # stored as INT
    assert rec["hypothesis_path"] == "/tmp/hyp.md"
    assert rec["raw"] == "some raw text"


def test_sqlite_turn_history_multiple_tasks_isolated(tmp_path):
    """Turns for task A do not appear in task B's history."""
    q = make_queue(tmp_path)
    q.record_turn("task-a", 0, {"outcome": "improved", "value": 1.0})
    q.record_turn("task-b", 0, {"outcome": "regressed", "value": 2.0})

    hist_a = q.turn_history("task-a")
    hist_b = q.turn_history("task-b")

    assert len(hist_a) == 1
    assert hist_a[0]["outcome"] == "improved"

    assert len(hist_b) == 1
    assert hist_b[0]["outcome"] == "regressed"


# ===========================================================================
# write_state / read_state (parity with file-based Queue)
# ===========================================================================


def test_sqlite_write_and_read_state(tmp_path):
    """write_state() persists state; read_state() returns it exactly."""
    q = make_queue(tmp_path)
    state = {"baseline": 1.23, "turn_count": 0, "stagnation_n": 0}
    q.write_state("abc123", state)
    result = q.read_state("abc123")
    assert result == state


def test_sqlite_read_state_missing_returns_none(tmp_path):
    """read_state() returns None for a task that never had write_state()."""
    q = make_queue(tmp_path)
    assert q.read_state("nonexistent") is None


def test_sqlite_state_dir_accessible_as_path(tmp_path):
    """queue._state is a Path — required by runner.py internals."""
    from pathlib import Path
    q = make_queue(tmp_path)
    assert isinstance(q._state, Path)
    assert q._state.exists()


# ===========================================================================
# get() edge cases
# ===========================================================================


def test_sqlite_get_missing_returns_none(tmp_path):
    """get() returns None for an unknown task_id."""
    q = make_queue(tmp_path)
    assert q.get("does-not-exist") is None


def test_sqlite_get_finds_pending(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "find me"})
    result = q.get(task_id)
    assert result is not None
    assert result["task_id"] == task_id


def test_sqlite_get_finds_running(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "find in running"})
    q.claim()
    result = q.get(task_id)
    assert result is not None
    assert result["task_id"] == task_id


def test_sqlite_get_finds_done(tmp_path):
    q = make_queue(tmp_path)
    task_id = q.post({"goal": "find in done"})
    q.claim()
    q.complete(task_id, {})
    result = q.get(task_id)
    assert result is not None
    assert result["task_id"] == task_id
