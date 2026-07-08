"""saturate.runner — one-turn hypothesis/measure/keep-or-revert cycle.

run_turn(task_id, queue) -> str

1. Loads the task manifest from running/.
2. Reads the loop spec from task['spec_path'] via loop_spec.load_spec().
3. Clones spec.repo (a git URL) into an isolated worktree if set.
4. Initialises or restores loop state.
5. Builds TurnContext and calls executor.execute_turn().
6. Measures the metric via saturate.measure.measure().
7. Runs the correctness gate on improvements.
8. Commits/reverts against the cloned worktree — never the Saturate source tree.
9. Persists state and writes a per-turn audit record.
10. Checks terminal conditions (max_iterations, plateau_count).
11. Returns one of: 'improved', 'regressed', 'crashed', 'unchanged', 'terminal'.
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

from saturate.executor import TurnContext, TurnSummary, make_executor
from saturate.measure import MeasureResult, measure
from saturate.queue import Queue


def run_turn(task_id: str, queue: Queue) -> str:
    """Execute one turn of a loop task.  Returns outcome string."""
    task = queue.get(task_id)
    if task is None:
        raise ValueError(f"Task {task_id} not found in queue")

    spec: LoopSpec = load_spec(task["spec_path"])
    state = queue.read_state(task_id) or _initial_state()
    turn_n = state.get("turn_count", 0)

    state_dir = queue._state
    task_state_path = str(state_dir / task_id)

    # Clone spec.repo (git URL) into an isolated worktree once; reuse thereafter
    work_dir = _resolve_worktree(spec.repo, task_state_path)

    context = TurnContext(
        turn_number=turn_n,
        baseline_metric=state.get("baseline"),
        recent_turns=[TurnSummary(**t) for t in state.get("recent_turns", [])[-5:]],
        stagnation_n=state.get("stagnation_n", 0),
        state_path=task_state_path,
        output_path=spec.output_dir,
    )

    if isinstance(spec, (ClarificationSpec, TaskExecutionSpec)):
        return _run_task_execution_turn(
            task_id, spec, state, context, turn_n, queue, work_dir
        )

    assert isinstance(spec, MetricOptimizationSpec)

    executor = make_executor(spec.executor)
    result = executor.execute_turn(spec, state, context)

    measure_result = measure(
        command=spec.evaluate or "echo 0",
        extract=spec.evaluate_extract,
        direction="minimize" if spec.direction == "lower_is_better" else "maximize",
        baseline=state.get("baseline"),
    )

    correct = True
    if measure_result.outcome == "improved" and spec.correctness:
        r = subprocess.run(spec.correctness, shell=True, capture_output=True)
        correct = r.returncode == 0

    kept = measure_result.outcome == "improved" and correct
    if kept:
        hyp_path = result.hypothesis_path
        hypothesis = (
            pathlib.Path(hyp_path).read_text()
            if os.path.exists(hyp_path)
            else "no hypothesis"
        )
        _git_commit(turn_n, hypothesis, measure_result, work_dir=work_dir)
        new_baseline = measure_result.value
    else:
        _git_revert(work_dir=work_dir)
        prior = state.get("baseline")
        new_baseline = measure_result.value if prior is None else prior

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

    if new_state["turn_count"] >= spec.terminal.max_iterations:
        queue.complete(
            task_id, {"terminal_reason": "exhausted", "final_state": new_state}
        )
        return "terminal"

    if new_stagnation >= spec.terminal.plateau_count:
        queue.complete(
            task_id, {"terminal_reason": "stalled", "final_state": new_state}
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
    work_dir: str | None,
) -> str:
    plan_path = spec.plan_path or ""
    if plan_path and not os.path.isabs(plan_path):
        plan_path = os.path.join(os.getcwd(), plan_path)

    completed = state.get("completed_tasks", [])
    tasks = _parse_plan_tasks(plan_path)
    next_task = next((t for t in tasks if t["title"] not in completed), None)

    if next_task is None:
        queue.complete(
            task_id, {"terminal_reason": "success", "completed_tasks": completed}
        )
        return "terminal"

    executor = make_executor(spec.executor)
    result = executor.execute_turn(
        {
            **spec.model_dump(),
            "_current_task": next_task,
            "_completed_tasks": completed,
        },
        state,
        context,
    )

    hyp_path = result.hypothesis_path
    report = pathlib.Path(hyp_path).read_text() if os.path.exists(hyp_path) else ""
    task_failed = not report.upper().startswith("SUCCESS:")
    outcome = "regressed" if task_failed else "improved"

    if not task_failed:
        completed = completed + [next_task["title"]]
        _git_commit_task(next_task["title"], turn_n, report, work_dir=work_dir)

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


def _resolve_worktree(repo_url: str | None, task_state_path: str) -> str | None:
    """Clone repo_url into task_state_path/repo/ on first turn; reuse thereafter.

    Returns the local checkout path, or None if repo_url is None.
    This is the ONLY path ever passed to git operations — the Saturate
    source tree is never touched.
    """
    if repo_url is None:
        return None
    checkout = pathlib.Path(task_state_path) / "repo"
    if checkout.exists():
        return str(checkout)
    checkout.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["git", "clone", repo_url, str(checkout)], capture_output=True, text=True
    )
    if r.returncode != 0:
        raise RuntimeError(f"Failed to clone {repo_url!r}:\n{r.stderr}")
    return str(checkout)


def _git_revert(work_dir: str | None) -> None:
    if work_dir is None:
        return
    subprocess.run(["git", "checkout", "--", "."], capture_output=True, cwd=work_dir)


def _git_commit(
    turn_n: int, hypothesis: str, result: MeasureResult, work_dir: str | None
) -> None:
    if work_dir is None:
        return
    msg = f"turn {turn_n}: {result.outcome} (value={result.value:.4f})\n\n{hypothesis[:500]}"
    subprocess.run(["git", "add", "-A"], cwd=work_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", msg, "--author=Donald Thompson <witt3rd@witt3rd.com>"],
        cwd=work_dir,
        capture_output=True,
    )


def _git_commit_task(
    task_title: str, turn_n: int, report: str, work_dir: str | None
) -> None:
    if work_dir is None:
        return
    msg = f"task(turn {turn_n}): {task_title[:72]}\n\n{report[:500]}"
    subprocess.run(["git", "add", "-A"], cwd=work_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", msg, "--author=Donald Thompson <witt3rd@witt3rd.com>"],
        cwd=work_dir,
        capture_output=True,
    )


def _parse_plan_tasks(plan_path: str) -> list[dict]:
    import re

    try:
        text = pathlib.Path(plan_path).read_text()
    except FileNotFoundError:
        return []
    pattern = re.compile(r"^## Task \d+\s*[—:-]+\s*(.+)$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    return [
        {
            "title": m.group(1).strip(),
            "body": text[
                m.end() : matches[i + 1].start() if i + 1 < len(matches) else len(text)
            ].strip(),
        }
        for i, m in enumerate(matches)
    ]


def _initial_state() -> dict:
    return {"baseline": None, "turn_count": 0, "stagnation_n": 0, "recent_turns": []}
