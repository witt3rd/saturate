"""SaturateTask dataclass and dispatch_score function.

SaturateTask is the work item type for the Saturate loop execution fabric.
Every task in the queue -- root scheduler, sub-loop, single-shot -- is an
instance of this class. Hierarchy is expressed through the task graph
(spawned_by / depends_on), not through type distinctions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


@dataclass
class SaturateTask:
    # ------------------------------------------------------------------
    # Identity (required at construction time)
    # ------------------------------------------------------------------
    task_id: str                        # UUID string
    name: str
    kind: str                           # one of the loop kind strings

    # ------------------------------------------------------------------
    # Execution paths (required at construction time)
    # ------------------------------------------------------------------
    spec_path: str                      # path to loop spec file (read-only)
    state_path: str                     # agreed location for intermediate state
    output_path: str                    # where completed output is written

    # ------------------------------------------------------------------
    # Scheduling (all optional / have sensible defaults)
    # ------------------------------------------------------------------
    priority: int = 50                  # 0 (highest) to 100 (lowest)
    deadline: Optional[datetime] = None
    earliest_start: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Hierarchy
    # ------------------------------------------------------------------
    spawned_by: Optional[str] = None    # parent task_id; None = root
    depends_on: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Resources
    # ------------------------------------------------------------------
    num_cpus: float = 1.0               # fractional OK -- e.g. 0.5
    num_gpus: float = 0.0               # 0 = CPU-only (the common case)
    required_node_class: Optional[str] = None  # e.g. "GPU_4090"
    estimated_duration_seconds: int = 0

    # ------------------------------------------------------------------
    # Budget controls
    # ------------------------------------------------------------------
    max_retries: int = 3
    max_turns: Optional[int] = None
    budget_tokens: Optional[int] = None
    stagnation_n: Optional[int] = None  # stop after N turns with no improvement

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    tags: List[str] = field(default_factory=list)
    submitted_by: str = "user"
    submitted_at: datetime = field(default_factory=_now)


def dispatch_score(task: SaturateTask, now: datetime) -> float:
    """Compute a dispatch priority score for *task* at reference time *now*.

    Returns a float in [0.0, 2.0].  Higher score = dispatch sooner.

    The score has two independent components that each contribute up to 1.0:

        priority_score = (100 - priority) / 100
            * Priority 0  → 1.0  (highest urgency)
            * Priority 50 → 0.5
            * Priority 100 → 0.0 (lowest urgency)

        deadline_score  (urgency relative to how close the deadline is)
            * No deadline  → 0.0
            * remaining ≤ 0 (already past deadline) → 1.0
            * remaining > 0 → min(1.0, estimated / remaining)
              where estimated = max(1, estimated_duration_seconds)
              This yields 1.0 when time remaining equals the estimated
              duration (must start NOW to finish on time) and decays
              smoothly toward 0.0 for far-future deadlines.

    Total = priority_score + deadline_score, clipped to [0.0, 2.0].
    """
    # Priority component: invert so that priority=0 → 1.0
    priority_score = (100 - max(0, min(100, task.priority))) / 100.0

    # Deadline urgency component
    deadline_score = 0.0
    if task.deadline is not None:
        # Ensure both datetimes are comparable (both aware or both naive)
        deadline = task.deadline
        reference = now

        # Make aware if task.deadline is naive but now is aware, or vice versa
        if deadline.tzinfo is None and reference.tzinfo is not None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        elif deadline.tzinfo is not None and reference.tzinfo is None:
            reference = reference.replace(tzinfo=timezone.utc)

        remaining_seconds = (deadline - reference).total_seconds()
        if remaining_seconds <= 0:
            deadline_score = 1.0
        else:
            estimated = max(1, task.estimated_duration_seconds)
            deadline_score = min(1.0, estimated / remaining_seconds)

    total = priority_score + deadline_score
    return max(0.0, min(2.0, total))
