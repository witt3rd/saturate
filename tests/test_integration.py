"""Integration tests for Saturate Phase 1.

End-to-end test using a ShellExecutor spec:
  - Create an isolated git repo in tmp_path (never the Saturate source tree)
  - The spec declares repo= pointing at that isolated repo
  - The metric command always returns the same value → stagnation terminates
  - Submit it, run turns in a loop, verify terminal in <= 5 turns

Also verifies that `saturate start --help` works.
"""
from __future__ import annotations

import subprocess
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


def _make_isolated_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo that the loop can commit/revert against."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    (repo / "placeholder.txt").write_text("initial\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return repo


def _write_stagnating_spec(spec_path: Path, repo: Path) -> None:
    """Write a shell-executor metric-optimization spec that never improves.

    The metric command 'echo 1' always outputs '1', so every measured value
    is 1.0.  With plateau_count=3 the loop must reach 'terminal' after
    exactly 3 turns (baseline seeded on turn 0, two unchanged turns, third
    turn sees stagnation_n == 3 and returns 'terminal').
    """
    spec = {
        "name": "integration-test",
        "kind": "MetricOptimizationKind",
        "direction": "lower_is_better",
        "metric": "stagnation test",
        "repo": f"file://{repo}",
        "terminal": {
            "plateau_count": 3,
            "max_iterations": 100,
        },
        "executor": {
            "type": "shell",
            "command": "true",
        },
        "evaluate": "echo 1",
        "evaluate_extract": "regex:(1)",
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

    The spec declares an isolated git repo — Saturate never touches its own
    source tree.

    Acceptance criteria
    -------------------
    - Loop reaches 'terminal' via stagnation.
    - Terminates in <= 5 calls to run_turn.
    - Task ends with status='done' in the queue.
    - At least one turn record exists with outcome='unchanged'.
    """
    repo = _make_isolated_repo(tmp_path)
    spec_path = tmp_path / "loop-spec.yaml"
    _write_stagnating_spec(spec_path, repo)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "integration-test",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
    })

    assert q.counts()["pending"] == 1

    outcome: str | None = None
    turns_taken = 0
    for _ in range(5):
        outcome = run_turn(task_id, q)
        turns_taken += 1
        if outcome == "terminal":
            break

    assert outcome == "terminal", (
        f"Loop did not reach 'terminal' in 5 turns; last outcome={outcome!r}"
    )
    assert turns_taken <= 5, f"Took {turns_taken} turns, expected <= 5"

    task = q.get(task_id)
    assert task is not None
    assert task.get("status") == "done", f"Task status is {task.get('status')!r}"
    assert task.get("terminal_reason") == "stalled"

    history = q.turn_history(task_id)
    assert len(history) > 0, "No turn records written"
    outcomes = [t["outcome"] for t in history]
    assert "unchanged" in outcomes, f"Expected 'unchanged' turns; got {outcomes}"


def test_integration_turn_count_within_bounds(tmp_path):
    """The loop terminates in exactly 3 turns for plateau_count=3.

    Turn-by-turn trace:
      Turn 0: baseline=None → 'unchanged', stagnation_n=1
      Turn 1: baseline=1.0  → 'unchanged', stagnation_n=2
      Turn 2: baseline=1.0  → 'unchanged', stagnation_n=3 → 'terminal'
    """
    repo = _make_isolated_repo(tmp_path)
    spec_path = tmp_path / "loop-spec.yaml"
    _write_stagnating_spec(spec_path, repo)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "turn-count-test",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
    })

    outcomes = []
    for _ in range(5):
        outcome = run_turn(task_id, q)
        outcomes.append(outcome)
        if outcome == "terminal":
            break

    assert outcomes[-1] == "terminal"
    assert len(outcomes) == 3, (
        f"Expected 3 turns for plateau_count=3, got {len(outcomes)}: {outcomes}"
    )


def test_integration_queue_counts_after_terminal(tmp_path):
    """After terminal, pending=0 running=0 done=1."""
    repo = _make_isolated_repo(tmp_path)
    spec_path = tmp_path / "loop-spec.yaml"
    _write_stagnating_spec(spec_path, repo)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "counts-test",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
    })

    for _ in range(5):
        outcome = run_turn(task_id, q)
        if outcome == "terminal":
            break

    counts = q.counts()
    assert counts["pending"] == 0
    assert counts["running"] == 0
    assert counts["done"] == 1


def test_integration_does_not_touch_saturate_repo(tmp_path):
    """The loop's git operations must never affect the Saturate source tree.

    Specifically: files outside the isolated worktree must not be modified
    by git operations (add/commit/revert) that the runner performs.
    Excluded from the dirty-tree check: uv.lock (package manager), .pyc
    files and __pycache__ directories (Python bytecode from the test runner).
    """
    import os
    saturate_root = Path(__file__).parent.parent

    repo = _make_isolated_repo(tmp_path)
    spec_path = tmp_path / "loop-spec.yaml"
    _write_stagnating_spec(spec_path, repo)

    q = _make_queue(tmp_path)
    task_id = q.post({
        "name": "isolation-test",
        "kind": "MetricOptimizationKind",
        "spec_path": str(spec_path),
    })

    for _ in range(5):
        outcome = run_turn(task_id, q)
        if outcome == "terminal":
            break

    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=saturate_root,
        capture_output=True,
        text=True,
    )
    # Filter out files legitimately touched by the test environment:
    # uv.lock (package manager), *.pyc/__pycache__ (bytecode)
    excluded_patterns = {"uv.lock", ".pyc", "__pycache__"}
    dirty_lines = [
        line for line in result.stdout.splitlines()
        if line.strip() and not any(pat in line for pat in excluded_patterns)
    ]
    assert dirty_lines == [], (
        f"Saturate source tree was dirtied by the loop runner:\n"
        + "\n".join(dirty_lines)
    )
