"""Tests for saturate.task — SaturateTask dataclass and dispatch_score."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from saturate.task import SaturateTask, dispatch_score


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_minimal(
    *,
    task_id: str | None = None,
    priority: int = 50,
    deadline: datetime | None = None,
    estimated_duration_seconds: int = 0,
) -> SaturateTask:
    """Return a SaturateTask built from the minimal required fields."""
    return SaturateTask(
        task_id=task_id or str(uuid.uuid4()),
        name="test-task",
        kind="metric-optimization",
        spec_path="/tmp/spec.yaml",
        state_path="/tmp/state",
        output_path="/tmp/output",
        priority=priority,
        deadline=deadline,
        estimated_duration_seconds=estimated_duration_seconds,
    )


NOW = datetime(2026, 7, 6, 19, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------


def test_instantiates_with_minimal_fields():
    """SaturateTask must construct with only the six required fields."""
    task = SaturateTask(
        task_id="abc-123",
        name="my-task",
        kind="task-execution",
        spec_path="/specs/plan.yaml",
        state_path="/state/abc",
        output_path="/output/abc",
    )
    assert task.task_id == "abc-123"
    assert task.name == "my-task"
    assert task.kind == "task-execution"
    assert task.spec_path == "/specs/plan.yaml"
    assert task.state_path == "/state/abc"
    assert task.output_path == "/output/abc"


def test_default_priority_is_fifty():
    task = make_minimal()
    assert task.priority == 50


def test_default_num_gpus_is_zero():
    task = make_minimal()
    assert task.num_gpus == 0.0


def test_default_estimated_duration_is_zero():
    task = make_minimal()
    assert task.estimated_duration_seconds == 0


def test_default_max_retries_is_three():
    task = make_minimal()
    assert task.max_retries == 3


def test_default_submitted_by_is_user():
    task = make_minimal()
    assert task.submitted_by == "user"


def test_default_submitted_at_is_set():
    task = make_minimal()
    assert isinstance(task.submitted_at, datetime)


def test_default_deadline_is_none():
    task = make_minimal()
    assert task.deadline is None


def test_default_depends_on_is_empty_list():
    task = make_minimal()
    assert task.depends_on == []


def test_default_tags_is_empty_list():
    task = make_minimal()
    assert task.tags == []


def test_depends_on_not_shared_between_instances():
    """Mutable default must not be shared across instances."""
    t1 = make_minimal()
    t2 = make_minimal()
    t1.depends_on.append("other")
    assert t2.depends_on == [], "depends_on must not be shared across instances"


def test_tags_not_shared_between_instances():
    t1 = make_minimal()
    t2 = make_minimal()
    t1.tags.append("important")
    assert t2.tags == [], "tags must not be shared across instances"


def test_full_field_instantiation():
    """All fields should be assignable."""
    deadline = NOW + timedelta(hours=2)
    earliest = NOW + timedelta(minutes=10)
    task = SaturateTask(
        task_id="full-id",
        name="full-task",
        kind="consensus",
        spec_path="/specs/full.yaml",
        state_path="/state/full",
        output_path="/output/full",
        priority=10,
        deadline=deadline,
        earliest_start=earliest,
        spawned_by="parent-id",
        depends_on=["dep-1", "dep-2"],
        num_cpus=2.0,
        num_gpus=1.0,
        required_node_class="GPU_4090",
        estimated_duration_seconds=3600,
        max_retries=5,
        max_turns=100,
        budget_tokens=50000,
        stagnation_n=10,
        tags=["ml", "important"],
        submitted_by="agent",
        submitted_at=NOW,
    )
    assert task.deadline == deadline
    assert task.earliest_start == earliest
    assert task.spawned_by == "parent-id"
    assert task.depends_on == ["dep-1", "dep-2"]
    assert task.num_cpus == 2.0
    assert task.num_gpus == 1.0
    assert task.required_node_class == "GPU_4090"
    assert task.estimated_duration_seconds == 3600
    assert task.max_retries == 5
    assert task.max_turns == 100
    assert task.budget_tokens == 50000
    assert task.stagnation_n == 10
    assert task.tags == ["ml", "important"]
    assert task.submitted_by == "agent"


# ---------------------------------------------------------------------------
# dispatch_score — return value range
# ---------------------------------------------------------------------------


def test_dispatch_score_returns_float():
    task = make_minimal()
    score = dispatch_score(task, NOW)
    assert isinstance(score, float)


def test_dispatch_score_in_range_0_to_2():
    """Score must always be in [0.0, 2.0]."""
    for priority in [0, 25, 50, 75, 100]:
        task = make_minimal(priority=priority)
        score = dispatch_score(task, NOW)
        assert 0.0 <= score <= 2.0, f"score {score} out of range for priority={priority}"


def test_dispatch_score_p0_no_deadline():
    """P0 (highest priority) with no deadline should score 1.0."""
    task = make_minimal(priority=0)
    score = dispatch_score(task, NOW)
    assert score == pytest.approx(1.0)


def test_dispatch_score_p100_no_deadline():
    """P100 (lowest priority) with no deadline should score 0.0."""
    task = make_minimal(priority=100)
    score = dispatch_score(task, NOW)
    assert score == pytest.approx(0.0)


def test_dispatch_score_p50_no_deadline():
    """P50 with no deadline should score 0.5."""
    task = make_minimal(priority=50)
    score = dispatch_score(task, NOW)
    assert score == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# dispatch_score — priority ordering
# ---------------------------------------------------------------------------


def test_p0_scores_higher_than_p50():
    """A P0 task must score strictly higher than a P50 task (no deadline)."""
    t0 = make_minimal(priority=0)
    t50 = make_minimal(priority=50)
    assert dispatch_score(t0, NOW) > dispatch_score(t50, NOW)


def test_p50_scores_higher_than_p100():
    t50 = make_minimal(priority=50)
    t100 = make_minimal(priority=100)
    assert dispatch_score(t50, NOW) > dispatch_score(t100, NOW)


def test_priority_ordering_monotonic():
    """Score decreases monotonically as priority number increases."""
    scores = [dispatch_score(make_minimal(priority=p), NOW) for p in range(0, 101, 10)]
    for i in range(len(scores) - 1):
        assert scores[i] >= scores[i + 1], f"score not monotone at index {i}"


# ---------------------------------------------------------------------------
# dispatch_score — deadline urgency
# ---------------------------------------------------------------------------


def test_imminent_deadline_scores_higher_than_no_deadline():
    """A task with a deadline seconds away should score higher than same task without."""
    deadline = NOW + timedelta(seconds=5)
    with_deadline = make_minimal(priority=50, deadline=deadline, estimated_duration_seconds=60)
    without_deadline = make_minimal(priority=50)
    assert dispatch_score(with_deadline, NOW) > dispatch_score(without_deadline, NOW)


def test_past_deadline_scores_maximum():
    """A task whose deadline has already passed should get full deadline score (1.0)."""
    deadline = NOW - timedelta(hours=1)
    task = make_minimal(priority=100, deadline=deadline)
    # priority=100 → priority_score=0.0, deadline past → deadline_score=1.0, total=1.0
    score = dispatch_score(task, NOW)
    assert score == pytest.approx(1.0)


def test_no_deadline_adds_zero_urgency():
    task_no_dl = make_minimal(priority=50)
    task_far_dl = make_minimal(
        priority=50,
        deadline=NOW + timedelta(days=365),
        estimated_duration_seconds=1,
    )
    # Far-future deadline should add negligible urgency (very small, but >= 0)
    score_no = dispatch_score(task_no_dl, NOW)
    score_far = dispatch_score(task_far_dl, NOW)
    assert score_far >= score_no, "far deadline should add non-negative urgency"
    # Both should be close to 0.5 (priority-only)
    assert score_far < 0.6, f"far deadline should add minimal urgency, got {score_far}"


def test_deadline_urgency_at_exactly_estimated_duration():
    """When remaining == estimated_duration_seconds, deadline_score should be 1.0."""
    task = make_minimal(
        priority=100,   # priority_score = 0.0
        deadline=NOW + timedelta(seconds=3600),
        estimated_duration_seconds=3600,
    )
    score = dispatch_score(task, NOW)
    # priority_score=0.0 + deadline_score=1.0 = 1.0
    assert score == pytest.approx(1.0)


def test_deadline_urgency_increases_as_deadline_approaches():
    """Score should increase as the deadline moves closer."""
    far = make_minimal(priority=50, deadline=NOW + timedelta(hours=24), estimated_duration_seconds=3600)
    close = make_minimal(priority=50, deadline=NOW + timedelta(minutes=30), estimated_duration_seconds=3600)
    assert dispatch_score(close, NOW) > dispatch_score(far, NOW)


def test_score_capped_at_2_0():
    """Total score must not exceed 2.0 even with priority=0 and past deadline."""
    deadline = NOW - timedelta(hours=1)  # past deadline → deadline_score = 1.0
    task = make_minimal(priority=0, deadline=deadline)  # priority_score = 1.0
    score = dispatch_score(task, NOW)
    assert score == pytest.approx(2.0)
    assert score <= 2.0


def test_score_floored_at_0_0():
    """Score must not go below 0.0."""
    task = make_minimal(priority=100)
    score = dispatch_score(task, NOW)
    assert score >= 0.0


# ---------------------------------------------------------------------------
# dispatch_score — timezone handling
# ---------------------------------------------------------------------------


def test_aware_deadline_with_aware_now():
    deadline = NOW + timedelta(hours=1)
    task = make_minimal(priority=50, deadline=deadline, estimated_duration_seconds=3600)
    score = dispatch_score(task, NOW)
    assert 0.0 <= score <= 2.0


def test_naive_deadline_with_naive_now():
    naive_now = datetime(2026, 7, 6, 19, 0, 0)
    deadline = naive_now + timedelta(hours=1)
    task = make_minimal(priority=50, deadline=deadline, estimated_duration_seconds=3600)
    score = dispatch_score(task, naive_now)
    assert 0.0 <= score <= 2.0


def test_naive_deadline_with_aware_now():
    """Should not raise when mixing naive deadline with aware now."""
    naive_deadline = datetime(2026, 7, 6, 20, 0, 0)  # no tzinfo
    task = make_minimal(priority=50, deadline=naive_deadline, estimated_duration_seconds=3600)
    # Should not raise
    score = dispatch_score(task, NOW)
    assert 0.0 <= score <= 2.0
