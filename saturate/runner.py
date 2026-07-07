"""saturate.runner — one-turn hypothesis/measure/keep-or-revert cycle.

run_turn(task_id, queue) -> str

The caller (CLI's ``saturate run``) is responsible for claim()ing the task
before calling run_turn.  This function:

1. Loads the task manifest from running/.
2. Reads the loop spec from task['spec_path'].
3. Initialises or restores loop state.
4. Builds TurnContext and calls executor.execute_turn().
5. Measures the metric via saturate.measure.measure().
6. Runs the correctness gate on improvements.
7. Commits (git add -A && git commit) on improved+correct, else reverts.
8. Persists state and writes a per-turn audit record.
9. Checks terminal conditions (max_turns, stagnation_n).
10. Returns one of: 'improved', 'regressed', 'crashed', 'unchanged', 'terminal'.

Phase 0: single-node, single-process, no parallelism.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
from typing import Optional

import yaml

from saturate.executor import TurnContext, TurnSummary, make_executor
from saturate.measure import MeasureResult, measure
from saturate.queue import Queue


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_turn(task_id: str, queue: Queue) -> str:
    """Execute one turn of a loop task.  Returns outcome string.

    Return values:
        'improved'   — metric improved; change committed.
        'regressed'  — metric regressed; change reverted.
        'crashed'    — metric command failed; change reverted.
        'unchanged'  — metric within noise band; change reverted.
        'terminal'   — a stop condition was reached; task moved to done/.
    """
    # 1. Load task manifest (must already be in running/ when called)
    task = queue.get(task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found in queue")

    # 2. Load loop spec
    spec = _load_spec(task["spec_path"])

    # 3. Load or initialise state
    state = queue.read_state(task_id) or _initial_state()
    turn_n = state.get("turn_count", 0)

    # 4. Build TurnContext
    # Queue stores its state directory as queue._state (a pathlib.Path).
    # We derive the base root from queue._state.parent to build output_path.
    state_dir = queue._state           # root/state
    base_root = state_dir.parent       # root

    context = TurnContext(
        turn_number=turn_n,
        baseline_metric=state.get("baseline"),
        recent_turns=[
            TurnSummary(**t) for t in state.get("recent_turns", [])[-5:]
        ],
        stagnation_n=state.get("stagnation_n", 0),
        state_path=str(state_dir / task_id),
        output_path=task.get(
            "output_path", str(base_root / "output" / task_id)
        ),
    )

    # 5. Instantiate executor and run one turn
    executor = make_executor(
        spec.get("executor", {"type": "shell", "command": "echo no-executor"})
    )
    result = executor.execute_turn(spec, state, context)

    # 6. Measure the metric
    metric_spec = spec.get("metric", {})
    measure_result = measure(
        command=metric_spec.get("command", "echo 0"),
        extract=metric_spec.get("extract", "wall_clock"),
        direction=metric_spec.get("direction", "minimize"),
        baseline=state.get("baseline"),
    )

    # 7. Correctness gate (only runs when metric improved)
    correct = True
    if measure_result.outcome == "improved":
        correctness_spec = spec.get("correctness", {})
        cmd = correctness_spec.get("command")
        if cmd:
            r = subprocess.run(cmd, shell=True, capture_output=True)
            correct = r.returncode == 0

    # 8. Keep or revert
    kept = measure_result.outcome == "improved" and correct
    if kept:
        # Commit the hypothesis to git
        hyp_path = result.hypothesis_path
        hypothesis = (
            pathlib.Path(hyp_path).read_text()
            if os.path.exists(hyp_path)
            else "no hypothesis"
        )
        _git_commit(turn_n, hypothesis, measure_result)
        new_baseline = measure_result.value
    else:
        # Revert all working-tree changes back to HEAD
        git_root = _find_git_root()
        subprocess.run(
            ["git", "checkout", "--", "."],
            capture_output=True,
            cwd=git_root,
        )
        new_baseline = state.get("baseline")  # unchanged

    # 9. Update state and write audit record
    new_stagnation = (
        0
        if kept
        else state.get("stagnation_n", 0) + 1
    )
    recent = state.get("recent_turns", [])[-4:] + [
        {
            "turn_n": turn_n,
            "outcome": measure_result.outcome,
            "value": measure_result.value,
        }
    ]
    new_state = {
        "baseline": new_baseline,
        "turn_count": turn_n + 1,
        "stagnation_n": new_stagnation,
        "recent_turns": recent,
    }
    queue.write_state(task_id, new_state)
    queue.record_turn(
        task_id,
        turn_n,
        {
            "turn_n": turn_n,
            "outcome": measure_result.outcome,
            "value": measure_result.value,
            "correct": correct,
            "hypothesis_path": result.hypothesis_path,
            "raw": measure_result.raw,
        },
    )

    # 10. Check terminal conditions
    max_turns = spec.get("max_turns")
    stagnation_limit = spec.get("stagnation_n")

    if max_turns and new_state["turn_count"] >= max_turns:
        queue.complete(
            task_id,
            {"terminal_reason": "exhausted", "final_state": new_state},
        )
        return "terminal"

    if stagnation_limit and new_stagnation >= stagnation_limit:
        queue.complete(
            task_id,
            {"terminal_reason": "stalled", "final_state": new_state},
        )
        return "terminal"

    # Not terminal — requeue for the next turn
    queue.requeue(task_id)
    return measure_result.outcome


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_spec(spec_path: str) -> dict:
    with open(spec_path) as f:
        return yaml.safe_load(f)


def _initial_state() -> dict:
    return {
        "baseline": None,
        "turn_count": 0,
        "stagnation_n": 0,
        "recent_turns": [],
    }


def _find_git_root() -> str:
    """Walk up from cwd until we find a .git directory."""
    p = pathlib.Path.cwd()
    while p != p.parent:
        if (p / ".git").exists():
            return str(p)
        p = p.parent
    # Fallback: treat cwd as git root (harmless if there is no git repo)
    return str(pathlib.Path.cwd())


def _git_commit(turn_n: int, hypothesis: str, result: MeasureResult) -> None:
    """Stage everything and commit with a structured message."""
    git_root = _find_git_root()
    msg = (
        f"turn {turn_n}: {result.outcome} (value={result.value:.4f})\n\n"
        f"{hypothesis[:500]}"
    )
    subprocess.run(["git", "add", "-A"], cwd=git_root, capture_output=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            msg,
            "--author=Donald Thompson <witt3rd@witt3rd.com>",
        ],
        cwd=git_root,
        capture_output=True,
    )
