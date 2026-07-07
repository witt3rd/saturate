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

    # 5. Route by loop kind
    kind = spec.get("kind", "metric-optimization")
    if kind == "task-execution":
        return _run_task_execution_turn(task_id, spec, state, context, turn_n, queue)

    # --- metric-optimization (and all other kinds for now) ---

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
        hyp_path = result.hypothesis_path
        hypothesis = (
            pathlib.Path(hyp_path).read_text()
            if os.path.exists(hyp_path)
            else "no hypothesis"
        )
        _git_commit(turn_n, hypothesis, measure_result)
        new_baseline = measure_result.value
    else:
        git_root = _find_git_root()
        subprocess.run(
            ["git", "checkout", "--", "."],
            capture_output=True,
            cwd=git_root,
        )
        prior = state.get("baseline")
        new_baseline = measure_result.value if prior is None else prior

    # 9. Update state and write audit record
    new_stagnation = (
        0 if kept else state.get("stagnation_n", 0) + 1
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

    queue.requeue(task_id)
    return measure_result.outcome


def _run_task_execution_turn(
    task_id: str,
    spec: dict,
    state: dict,
    context: TurnContext,
    turn_n: int,
    queue: Queue,
) -> str:
    """One turn of a task-execution loop.

    Reads the plan file, finds the next incomplete task, routes it to the
    executor, records the result, and checks for plan completion.
    No metric measurement — done when all tasks are marked complete.
    """
    plan_path = spec.get("plan_path", "")
    if not os.path.isabs(plan_path):
        # Resolve relative to spec file location
        plan_path = os.path.join(
            os.path.dirname(os.path.abspath(spec.get("_spec_path", "."))),
            plan_path,
        )

    # Task completion is tracked in state under 'completed_tasks' (list of task titles)
    completed = state.get("completed_tasks", [])
    tasks = _parse_plan_tasks(plan_path)

    # Find next incomplete task (simple sequential execution for Phase 0)
    next_task = None
    for t in tasks:
        if t["title"] not in completed:
            next_task = t
            break

    if next_task is None:
        # All tasks done — terminal success
        queue.complete(task_id, {"terminal_reason": "success", "completed_tasks": completed})
        return "terminal"

    # Build an augmented context with the specific task to execute
    executor = make_executor(
        spec.get("executor", {"type": "shell", "command": "echo no-executor"})
    )
    # Inject current task into spec for the executor's message builder
    spec_with_task = {**spec, "_current_task": next_task, "_completed_tasks": completed}
    result = executor.execute_turn(spec_with_task, state, context)

    # Read the executor's report — did it succeed?
    hyp_path = result.hypothesis_path
    report = (
        pathlib.Path(hyp_path).read_text() if os.path.exists(hyp_path) else ""
    )
    # Simple heuristic: executor signals failure by writing FAILED in the report
    task_failed = "FAILED" in report.upper() and "SUCCESS" not in report.upper()

    outcome = "regressed" if task_failed else "improved"

    if not task_failed:
        completed = completed + [next_task["title"]]
        _git_commit_task(next_task["title"], turn_n, report)

    new_state = {
        "turn_count": turn_n + 1,
        "stagnation_n": 0,
        "recent_turns": state.get("recent_turns", [])[-4:] + [
            {"turn_n": turn_n, "outcome": outcome, "value": float(len(completed))}
        ],
        "completed_tasks": completed,
        "baseline": float(len(completed)),
    }
    queue.write_state(task_id, new_state)
    queue.record_turn(task_id, turn_n, {
        "turn_n": turn_n,
        "outcome": outcome,
        "task_title": next_task["title"],
        "task_failed": task_failed,
        "hypothesis_path": hyp_path,
    })

    # Check max_turns safety net
    max_turns = spec.get("max_turns")
    if max_turns and new_state["turn_count"] >= max_turns:
        queue.complete(task_id, {"terminal_reason": "exhausted", "final_state": new_state})
        return "terminal"

    # Check if all tasks now done
    remaining = [t for t in tasks if t["title"] not in completed]
    if not remaining:
        queue.complete(task_id, {"terminal_reason": "success", "completed_tasks": completed})
        return "terminal"

    queue.requeue(task_id)
    return outcome


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_spec(spec_path: str) -> dict:
    with open(spec_path) as f:
        spec = yaml.safe_load(f)
    spec["_spec_path"] = spec_path   # carry path for relative resolution
    return spec


def _parse_plan_tasks(plan_path: str) -> list[dict]:
    """Parse a markdown plan file into a list of task dicts.

    Looks for '## Task N — <title>' headings and extracts title + body.
    Returns [{"title": str, "body": str}, ...] in document order.
    """
    import re
    try:
        text = pathlib.Path(plan_path).read_text()
    except FileNotFoundError:
        return []
    tasks = []
    # Match ## Task N — Title  or  ## Task N: Title
    pattern = re.compile(r'^## Task \d+\s*[—:-]+\s*(.+)$', re.MULTILINE)
    matches = list(pattern.finditer(text))
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        tasks.append({"title": title, "body": body})
    return tasks


def _git_commit_task(task_title: str, turn_n: int, report: str) -> None:
    """Commit completed task-execution work."""
    git_root = _find_git_root()
    msg = (
        f"task(turn {turn_n}): {task_title[:72]}\n\n"
        f"{report[:500]}"
    )
    subprocess.run(["git", "add", "-A"], cwd=git_root, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", msg,
         "--author=Donald Thompson <witt3rd@witt3rd.com>"],
        cwd=git_root, capture_output=True,
    )


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
