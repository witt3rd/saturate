"""Integration tests for Saturate Phase 1.

End-to-end test using a ShellExecutor spec:
  - Create a spec with kind=metric-optimization, executor type=shell
  - The metric command always returns the same value → stagnation terminates the loop
  - Submit it, run turns in a loop, verify terminal in <= 5 turns

Also verifies that `saturate start --help` works.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from click.testing import CliRunner

from saturate.cli import main
from saturate.queue_sqlite import SqliteQueue
from saturate.runner import run_turn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_queue(tmp_path: Path) -> SqliteQueue:
    return SqliteQueue(base_dir=str(tmp_path / "saturate"))


def _write_stagnating_spec(spec_path: Path) -> None:
    """Write a shell-executor metric-optimization spec that never improves.

    The metric command 'echo 1' always outputs '1', so every measured value
    is 1.0.  With stagnation_n=3 the loop must reach 'terminal' after
    exactly 3 turns (baseline seeded on turn 0, two unchanged turns, third
    turn sees stagnation_n == 3 and returns 'terminal').
    """
    spec = {
        "name": "integration-test",
        "kind": "metric-optimization",
        "stagnation_n": 3,
        "executor": {
            "type": "shell",
            # No-op command; ShellExecutor auto-creates hypothesis.md placeholder
            "command": "true",
        },
        "metric": {
            # Always prints the integer 1 — value never changes
            "command": "echo 1",
            "extract": "regex:(1)",
            "direction": "minimize",
        },
    }
    spec_path.write_text(yaml.dump(spec))


# ---------------------------------------------------------------------------
# CLI: saturate start --help
# ---------------------------------------------------------------------------

def test_start_help_exits_ok():
    """saturate start --help must exit 0 and mention --interval."""
    runner = CliRunner()
    result = runner.invoke(main, ["start", "--help"])
    assert result.exit_code == 0, result.output


def test_start_help_shows_interval_option():
    """`--interval` appears in the help output."""
    runner = CliRunner()
    result = runner.invoke(main, ["start", "--help"])
    assert "--interval" in result.output


def test_start_help_shows_goals_dir_option():
    """`--goals-dir` appears in the help output."""
    runner = CliRunner()
    result = runner.invoke(main, ["start", "--help"])
    assert "--goals-dir" in result.output


def test_start_help_shows_default_interval():
    """Default interval value (30) appears in the help output."""
    runner = CliRunner()
    result = runner.invoke(main, ["start", "--help"])
    assert "30" in result.output


# ---------------------------------------------------------------------------
# Integration: shell-executor spec stagnates to terminal
# ---------------------------------------------------------------------------

def test_integration_shell_stagnates_to_terminal(tmp_path):
    """End-to-end: submit a shell-executor spec whose metric never improves.

    Acceptance criteria
    -------------------
    - Loop reaches 'terminal' via stagnation.
    - Terminates in <= 5 calls to run_turn.
    - Task ends with status='done' in the queue.
    - At least one turn record exists with outcome='unchanged'.
    """
    # 1. Write the spec
    spec_path = tmp_path / "loop-spec.yaml"
    _write_stagnating_spec(spec_path)

    # 2. Create queue and submit the task
    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "integration-test",
        "kind": "metric-optimization",
        "spec_path": str(spec_path),
        # state_path and output_path are optional; queue derives them if absent
    })

    assert q.counts()["pending"] == 1

    # 3. Run turns until terminal (runner.py uses queue.get() so no pre-claim needed)
    outcome: str | None = None
    turns_taken = 0
    for _ in range(5):
        outcome = run_turn(task_id, q)  # type: ignore[arg-type]
        turns_taken += 1
        if outcome == "terminal":
            break

    # 4. Verify termination
    assert outcome == "terminal", (
        f"Loop did not reach 'terminal' in 5 turns; last outcome={outcome!r}"
    )
    assert turns_taken <= 5, f"Took {turns_taken} turns, expected <= 5"

    # 5. Task must be marked done
    task = q.get(task_id)
    assert task is not None
    assert task.get("status") == "done", f"Task status is {task.get('status')!r}"
    assert task.get("terminal_reason") == "stalled"

    # 6. Turn history recorded
    history = q.turn_history(task_id)
    assert len(history) > 0, "No turn records written"
    outcomes = [t["outcome"] for t in history]
    assert "unchanged" in outcomes, f"Expected 'unchanged' turns; got {outcomes}"


def test_integration_turn_count_within_bounds(tmp_path):
    """The loop terminates in exactly 3 turns for stagnation_n=3.

    Turn-by-turn trace:
      Turn 0: baseline=None → 'unchanged', stagnation_n=1
      Turn 1: baseline=1.0  → 'unchanged', stagnation_n=2
      Turn 2: baseline=1.0  → 'unchanged', stagnation_n=3 → 'terminal'
    """
    spec_path = tmp_path / "loop-spec.yaml"
    _write_stagnating_spec(spec_path)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "turn-count-test",
        "kind": "metric-optimization",
        "spec_path": str(spec_path),
    })

    outcomes = []
    for _ in range(5):
        outcome = run_turn(task_id, q)  # type: ignore[arg-type]
        outcomes.append(outcome)
        if outcome == "terminal":
            break

    assert outcomes[-1] == "terminal"
    # Should hit terminal exactly on the 3rd call
    assert len(outcomes) == 3, (
        f"Expected 3 turns for stagnation_n=3, got {len(outcomes)}: {outcomes}"
    )


def test_integration_queue_counts_after_terminal(tmp_path):
    """After terminal, pending=0 running=0 done=1."""
    spec_path = tmp_path / "loop-spec.yaml"
    _write_stagnating_spec(spec_path)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "counts-test",
        "kind": "metric-optimization",
        "spec_path": str(spec_path),
    })

    for _ in range(5):
        outcome = run_turn(task_id, q)  # type: ignore[arg-type]
        if outcome == "terminal":
            break

    counts = q.counts()
    assert counts["pending"] == 0
    assert counts["running"] == 0
    assert counts["done"] == 1
