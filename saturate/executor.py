from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Any, List, Optional, Protocol, runtime_checkable

from loop_spec import ExecutorSpec, LoopSpec, TaskExecutionSpec
from loop_spec import TurnResult as LoopSpecTurnResult


@dataclass
class TurnSummary:
    turn_n: int
    outcome: str  # improved / regressed / crashed / unchanged
    value: float


@dataclass
class TurnContext:
    turn_number: int
    baseline_metric: Optional[float]  # None on turn 0
    recent_turns: List[TurnSummary]  # last 5 accepted/discarded turns
    stagnation_n: int  # consecutive non-improved turns
    state_path: str  # executor writes hypothesis.md here
    output_path: str


@dataclass
class ExecutorResult:
    """Internal result from a Saturate executor turn.

    Carries the hypothesis.md path (implementation detail) alongside
    the loop-spec TurnResult (the published outcome contract).
    """

    hypothesis_path: str  # path to the hypothesis.md the executor wrote
    turn_result: LoopSpecTurnResult  # published outcome for audit trail


@runtime_checkable
class Executor(Protocol):
    def execute_turn(
        self,
        spec: Any,  # LoopSpec or dict envelope (task-execution)
        state: dict,
        context: TurnContext,
    ) -> ExecutorResult: ...


class HermesExecutor:
    """Invokes: hermes -p {profile} chat -q "{task_message}" """

    def __init__(self, profile: str) -> None:
        self.profile = profile

    def execute_turn(
        self, spec: Any, state: dict, context: TurnContext
    ) -> ExecutorResult:
        import pathlib

        os.makedirs(context.state_path, exist_ok=True)
        msg = self._build_message(spec, context)

        msg_path = os.path.join(context.state_path, "task_message.md")
        pathlib.Path(msg_path).write_text(msg)

        result = subprocess.run(
            ["hermes", "-p", self.profile, "chat", "-q", msg],
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            # Pass through SATURATE_* vars so the hermes worker subprocess
            # can detect the Saturate context via cyclus_queue._active_backend().
            # Only inject vars that are actually set — never pass empty strings.
            env={
                **os.environ,
                **{
                    k: v
                    for k, v in {
                        "SATURATE_TASK": os.environ.get("SATURATE_TASK"),
                        "SATURATE_TASK_ID": (
                            os.environ.get("SATURATE_TASK_ID")
                            or os.environ.get("SATURATE_TASK")
                        ),
                        "SATURATE_QUEUE_DIR": os.environ.get("SATURATE_QUEUE_DIR"),
                    }.items()
                    if v
                },
            },
        )

        hyp_path = os.path.join(context.state_path, "hypothesis.md")
        if not os.path.exists(hyp_path):
            output = (
                result.stdout or result.stderr or "(no output from hermes executor)"
            )
            pathlib.Path(hyp_path).write_text(output)

        if os.path.exists(hyp_path):
            with open(hyp_path) as _f:
                notes: str | None = _f.read(500)
        else:
            notes = None
        return ExecutorResult(
            hypothesis_path=hyp_path,
            turn_result=LoopSpecTurnResult(outcome="applied", notes=notes),
        )

    def _build_message(self, spec: Any, context: TurnContext) -> str:
        if isinstance(spec, LoopSpec):
            if isinstance(spec, TaskExecutionSpec):
                return self._build_task_execution_message(spec, context)
            goal = getattr(spec, "metric", None) or spec.name
            kind = spec.kind
        else:
            kind = spec.get("kind", "metric-optimization")
            if kind in ("task-execution", "TaskExecutionKind"):
                return self._build_task_execution_message(spec, context)
            goal = spec.get("goal", "")

        recent = (
            "\n".join(
                f"  Turn {t.turn_n}: {t.outcome} (value={t.value})"
                for t in context.recent_turns
            )
            or "  (none yet)"
        )
        return (
            f"You are executing turn {context.turn_number} of a {kind} loop.\n"
            f"\n"
            f"Goal: {goal}\n"
            f"Current baseline: {context.baseline_metric}\n"
            f"Recent turns:\n"
            f"{recent}\n"
            f"Stagnation: {context.stagnation_n} consecutive turns with no improvement\n"
            f"\n"
            f"Your job this turn:\n"
            f"1. Generate ONE hypothesis — a concrete change that might improve the metric\n"
            f"2. Apply it (edit files, run commands, whatever is needed)\n"
            f"3. Write a description of what you changed to: {context.state_path}/hypothesis.md\n"
            f"   Format: one paragraph, concrete, describing exactly what changed and why\n"
            f"\n"
            f"Saturate will measure the result and keep or revert automatically.\n"
            f"Do not loop. Do not measure. Execute exactly one hypothesis and exit."
        )

    def _build_task_execution_message(self, spec: Any, context: TurnContext) -> str:
        if isinstance(spec, TaskExecutionSpec):
            goal = spec.name
            plan_path = spec.plan_path or "(no plan)"
            current_task: dict = {}
            completed: list = []
        else:
            goal = spec.get("goal", "")
            plan_path = spec.get("plan_path", "(no plan)")
            current_task = spec.get("_current_task", {})
            completed = spec.get("_completed_tasks", [])

        task_title = current_task.get("title", "(unknown task)")
        task_body = current_task.get("body", "")
        completed_list = "\n".join(f"  ✓ {t}" for t in completed) or "  (none yet)"

        return (
            f"You are executing a task-execution loop.\n"
            f"\n"
            f"Overall goal: {goal}\n"
            f"Plan: {plan_path}\n"
            f"\n"
            f"Completed tasks so far:\n"
            f"{completed_list}\n"
            f"\n"
            f"YOUR TASK FOR THIS TURN:\n"
            f"  {task_title}\n"
            f"\n"
            f"{task_body}\n"
            f"\n"
            f"Instructions:\n"
            f"1. Implement this task completely — write the code, run the tests, fix any issues\n"
            f"2. When done, write a completion report to: {context.state_path}/hypothesis.md\n"
            f"   Format: start with 'SUCCESS: ' or 'FAILED: ', then describe what you did\n"
            f"3. Commit your work with git before exiting\n"
            f"\n"
            f"Do NOT work on any other task. Focus only on: {task_title}"
        )


class ShellExecutor:
    """Invokes an arbitrary shell command with SATURATE_* environment variables."""

    def __init__(self, command: str) -> None:
        self.command = command

    def execute_turn(
        self, spec: Any, state: dict, context: TurnContext
    ) -> ExecutorResult:
        import pathlib

        os.makedirs(context.state_path, exist_ok=True)

        if isinstance(spec, LoopSpec):
            goal = getattr(spec, "metric", None) or spec.name
            kind = spec.kind
        else:
            goal = spec.get("goal", "")
            kind = spec.get("kind", "")

        env = {
            **os.environ,
            "SATURATE_GOAL": goal,
            "SATURATE_KIND": kind,
            "SATURATE_TURN": str(context.turn_number),
            "SATURATE_BASELINE": str(context.baseline_metric or ""),
            "SATURATE_STAGNATION": str(context.stagnation_n),
            "SATURATE_STATE_PATH": context.state_path,
            "SATURATE_OUTPUT_PATH": context.output_path,
        }

        subprocess.run(self.command, shell=True, env=env, check=False)

        hyp_path = os.path.join(context.state_path, "hypothesis.md")
        if not os.path.exists(hyp_path):
            pathlib.Path(hyp_path).write_text("(executor produced no hypothesis.md)")

        if os.path.exists(hyp_path):
            with open(hyp_path) as _f:
                notes: str | None = _f.read(500)
        else:
            notes = None
        return ExecutorResult(
            hypothesis_path=hyp_path,
            turn_result=LoopSpecTurnResult(outcome="applied", notes=notes),
        )


def make_executor(executor_spec: ExecutorSpec | None) -> Executor:
    """Factory: return the right Executor from a loop spec's executor block.

    None  → no-op ShellExecutor
    hermes → HermesExecutor (requires profile)
    shell  → ShellExecutor (requires command)
    """
    if executor_spec is None:
        return ShellExecutor(command="echo no-executor")
    if executor_spec.type == "hermes":
        if not executor_spec.profile:
            raise ValueError("HermesExecutor requires 'profile' in executor spec")
        return HermesExecutor(profile=executor_spec.profile)
    if executor_spec.type == "shell":
        return ShellExecutor(command=executor_spec.command or "echo no-executor")
    raise NotImplementedError(
        f"Executor type {executor_spec.type!r} not yet implemented"
    )
