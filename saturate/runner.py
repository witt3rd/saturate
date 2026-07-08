"""saturate.runner — one-turn hypothesis/measure/keep-or-revert cycle.

run_turn(task_id, queue) -> str

The caller (CLI's ``saturate run``) is responsible for claim()ing the task
before calling run_turn.  This function:

1. Loads the task manifest from running/.
2. Reads the loop spec from task['spec_path'] via loop_spec.load_spec().
3. Initialises or restores loop state.
4. Builds TurnContext and calls executor.execute_turn().
5. Measures the metric via saturate.measure.measure().
6. Runs the correctness gate on improvements.
7. Commits (git add -A && git commit) on improved+correct, else reverts.
   All git operations are scoped to spec.repo — never the Saturate source tree.
8. Persists state and writes a per-turn audit record.
9. Checks terminal conditions (max_iterations, plateau_count).
10. Returns one of: 'improved', 'regressed', 'crashed', 'unchanged', 'terminal'.

Phase 0: single-node, single-process, no parallelism.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

from loop_spec import (
    ClarificationSpec,
    LoopSpec,
    MetricOptimizationSpec,
    TaskExecutionSpec,
    load_spec,
)

from saturate.executor import TurnContext, TurnResult, TurnSummary, make_executor
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

    # 2. Load loop spec via loop_spec standard
    spec: LoopSpec = load_spec(task["spec_path"])

    # 3. Load or initialise state
    state = queue.read_state(task_id) or _initial_state()
    turn_n = state.get("turn_count", 0)

    # 4. Build TurnContext
    state_dir = queue._state  # root/state
    context = TurnContext(
        turn_number=turn_n,
        baseline_metric=state.get("baseline"),
        recent_turns=[TurnSummary(**t) for t in state.get("recent_turns", [])[-5:]],
        stagnation_n=state.get("stagnation_n", 0),
        state_path=str(state_dir / task_id),
        output_path=spec.output_dir,
    )

    # 5. Route by loop kind
    if isinstance(spec, (ClarificationSpec, TaskExecutionSpec)):
        return _run_task_execution_turn(task_id, spec, state, context, turn_n, queue)

    # --- MetricOptimizationSpec (and all other kinds for now) ---
    assert isinstance(spec, MetricOptimizationSpec)

    executor = make_executor(spec.executor)
    result = executor.execute_turn(spec, state, context)

    # 6. Measure the metric
    measure_result = measure(
        command=spec.evaluate or "echo 0",
        extract=spec.evaluate_extract,
        direction="minimize" if spec.direction == "lower_is_better" else "maximize",
        baseline=state.get("baseline"),
    )

    # 7. Correctness gate (only runs when metric improved)
    correct = True
    if measure_result.outcome == "improved" and spec.correctness:
        r = subprocess.run(spec.correctness, shell=True, capture_output=True)
        correct = r.returncode == 0

    # 8. Keep or revert — scoped strictly to spec.repo
    kept = measure_result.outcome == "improved" and correct
    if kept:
        hyp_path = result.hypothesis_path
        hypothesis = (
            pathlib.Path(hyp_path).read_text()
            if os.path.exists(hyp_path)
            else "no hypothesis"
        )
        _git_commit(turn_n, hypothesis, measure_result, repo=spec.repo)
        new_baseline = measure_result.value
    else:
        _git_revert(repo=spec.repo)
        prior = state.get("baseline")
        new_baseline = measure_result.value if prior is None else prior

    # 9. Update state and write audit record
    new_stagnation = 0 if kept else state.get("stagnation_n", 0) + 1
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
    max_iter = spec.terminal.max_iterations
    plateau = spec.terminal.plateau_count

    if new_state["turn_count"] >= max_iter:
        queue.complete(
            task_id,
            {"terminal_reason": "exhausted", "final_state": new_state},
        )
        return "terminal"

    if new_stagnation >= plateau:
        queue.complete(
            task_id,
            {"terminal_reason": "stalled", "final_state": new_state},
        )
        return "terminal"

    queue.requeue(task_id)
    return measure_result.outcome


def _run_task_execution_turn(
    task_id: str,
    spec: TaskExecutionSpec,
    state: dict,
    context: TurnContext,
    turn_n: int,
    queue: Queue,
) -> str:
    """One turn of a task-execution loop."""
    plan_path = spec.plan_path or ""
    if plan_path and not os.path.isabs(plan_path):
        plan_path = os.path.join(os.getcwd(), plan_path)

    completed = state.get("completed_tasks", [])
    tasks = _parse_plan_tasks(plan_path)

    next_task = None
    for t in tasks:
        if t["title"] not in completed:
            next_task = t
            break

    if next_task is None:
        queue.complete(
            task_id, {"terminal_reason": "success", "completed_tasks": completed}
        )
        return "terminal"

    executor = make_executor(spec.executor)
    spec_with_task = {
        **spec.model_dump(),
        "_current_task": next_task,
        "_completed_tasks": completed,
    }
    result = executor.execute_turn(spec_with_task, state, context)

    hyp_path = result.hypothesis_path
    report = pathlib.Path(hyp_path).read_text() if os.path.exists(hyp_path) else ""
    task_failed = not report.upper().startswith("SUCCESS:")
    outcome = "regressed" if task_failed else "improved"

    if not task_failed:
        completed = completed + [next_task["title"]]
        _git_commit_task(next_task["title"], turn_n, report, repo=spec.repo)

    new_state = {
        "turn_count": turn_n + 1,
        "stagnation_n": 0,
        "recent_turns": state.get("recent_turns", [])[-4:]
        + [{"turn_n": turn_n, "outcome": outcome, "value": float(len(completed))}],
        "completed_tasks": completed,
        "baseline": float(len(completed)),
    }
    queue.write_state(task_id, new_state)
    queue.record_turn(
        task_id,
        turn_n,
        {
            "turn_n": turn_n,
            "outcome": outcome,
            "task_title": next_task["title"],
            "task_failed": task_failed,
            "hypothesis_path": hyp_path,
        },
    )

    if new_state["turn_count"] >= spec.terminal.max_iterations:
        queue.complete(
            task_id, {"terminal_reason": "exhausted", "final_state": new_state}
        )
        return "terminal"

    remaining = [t for t in tasks if t["title"] not in completed]
    if not remaining:
        queue.complete(
            task_id, {"terminal_reason": "success", "completed_tasks": completed}
        )
        return "terminal"

    queue.requeue(task_id)
    return outcome


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_plan_tasks(plan_path: str) -> list[dict]:
    """Parse a markdown plan file into a list of task dicts."""
    import re

    try:
        text = pathlib.Path(plan_path).read_text()
    except FileNotFoundError:
        return []
    tasks = []
    pattern = re.compile(r"^## Task \d+\s*[—:-]+\s*(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        tasks.append({"title": title, "body": body})
    return tasks


def _git_revert(repo: str | None) -> None:
    """Revert uncommitted changes in the target repo.

    If repo is None, no-op — the loop does not manage a git working tree.
    """
    if repo is None:
        return
    subprocess.run(
        ["git", "checkout", "--", "."],
        capture_output=True,
        cwd=repo,
    )


def _git_commit_task(task_title: str, turn_n: int, report: str, repo: str | None) -> None:
    """Commit completed task-execution work to the target repo."""
    if repo is None:
        return
    msg = f"task(turn {turn_n}): {task_title[:72]}\n\n{report[:500]}"
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", msg, "--author=Donald Thompson <witt3rd@witt3rd.com>"],
        cwd=repo,
        capture_output=True,
    )


def _initial_state() -> dict:
    return {
        "baseline": None,
        "turn_count": 0,
        "stagnation_n": 0,
        "recent_turns": [],
    }


def _git_commit(turn_n: int, hypothesis: str, result: MeasureResult, repo: str | None) -> None:
    """Stage everything and commit with a structured message to the target repo."""
    if repo is None:
        return
    msg = (
        f"turn {turn_n}: {result.outcome} (value={result.value:.4f})\n\n"
        f"{hypothesis[:500]}"
    )
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            msg,
            "--author=Donald Thompson <witt3rd@witt3rd.com>",
        ],
        cwd=repo,
        capture_output=True,
    )
