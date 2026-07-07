from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable


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
class TurnResult:
    hypothesis_path: str  # path to the hypothesis.md the executor wrote


@runtime_checkable
class Executor(Protocol):
    def execute_turn(
        self,
        spec: dict,  # the full parsed loop spec
        state: dict,  # current queue state dict
        context: TurnContext,
    ) -> TurnResult: ...


class HermesExecutor:
    """Invokes: hermes -p {profile} run --message "{task_message}"

    Writes the formatted task message to state_path/task_message.md, then
    shells out to the hermes CLI.  If the agent does not write
    state_path/hypothesis.md itself, the executor falls back to capturing
    stdout and writing it there.
    """

    def __init__(self, profile: str) -> None:
        self.profile = profile

    def execute_turn(self, spec: dict, state: dict, context: TurnContext) -> TurnResult:
        import pathlib

        os.makedirs(context.state_path, exist_ok=True)
        msg = self._build_message(spec, context)

        msg_path = os.path.join(context.state_path, "task_message.md")
        pathlib.Path(msg_path).write_text(msg)

        # hermes -p <profile> chat -q "<message>"
        # Stream stderr live (shows Forge's progress); capture stdout for fallback.
        result = subprocess.run(
            ["hermes", "-p", self.profile, "chat", "-q", msg],
            stdout=subprocess.PIPE,
            stderr=None,  # inherit — streams directly to the terminal
            text=True,
        )

        hyp_path = os.path.join(context.state_path, "hypothesis.md")
        if not os.path.exists(hyp_path):
            # Fall back to whatever the CLI printed
            output = (
                result.stdout or result.stderr or "(no output from hermes executor)"
            )
            pathlib.Path(hyp_path).write_text(output)

        return TurnResult(hypothesis_path=hyp_path)

    def _build_message(self, spec: dict, context: TurnContext) -> str:
        kind = spec.get("kind", "metric-optimization")

        if kind == "task-execution":
            return self._build_task_execution_message(spec, context)

        # Default: metric-optimization message
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

    def _build_task_execution_message(self, spec: dict, context: TurnContext) -> str:
        goal = spec.get("goal", "")
        current_task = spec.get("_current_task", {})
        completed = spec.get("_completed_tasks", [])
        plan_path = spec.get("plan_path", "(no plan)")
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
            f"   Example: 'SUCCESS: Implemented SaturateTask dataclass in saturate/task.py. All 8 new tests pass.'\n"
            f"3. Commit your work with git before exiting\n"
            f"\n"
            f"Do NOT work on any other task. Focus only on: {task_title}"
        )


class ShellExecutor:
    """Invokes an arbitrary shell command with SATURATE_* environment variables.

    The command is expected to write state_path/hypothesis.md describing
    what it changed.  If it does not, a fallback placeholder is written so
    the loop can continue.
    """

    def __init__(self, command: str) -> None:
        self.command = command

    def execute_turn(self, spec: dict, state: dict, context: TurnContext) -> TurnResult:
        import pathlib

        os.makedirs(context.state_path, exist_ok=True)

        env = {
            **os.environ,
            "SATURATE_GOAL": spec.get("goal", ""),
            "SATURATE_KIND": spec.get("kind", ""),
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

        return TurnResult(hypothesis_path=hyp_path)


def make_executor(executor_spec: dict) -> Executor:
    """Factory: return the right Executor from a spec's ``executor:`` block.

    Supported types:
      - ``"hermes"``  — requires ``profile`` key
      - ``"shell"``   — requires ``command`` key (default when type is absent)
    """
    kind = executor_spec.get("type", "shell")
    if kind == "hermes":
        return HermesExecutor(profile=executor_spec["profile"])
    if kind == "shell":
        return ShellExecutor(command=executor_spec["command"])
    raise ValueError(f"Unknown executor type: {kind!r}")
