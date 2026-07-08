"""Tests for saturate.executor — Executor protocol, HermesExecutor, ShellExecutor."""
from __future__ import annotations

import os
import pathlib
import stat
from unittest.mock import MagicMock, patch

import pytest

from saturate.executor import (
    Executor,
    HermesExecutor,
    ShellExecutor,
    TurnContext,
    TurnResult,
    TurnSummary,
    make_executor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(tmp_path: pathlib.Path, turn: int = 1) -> TurnContext:
    state = tmp_path / "state"
    out = tmp_path / "output"
    return TurnContext(
        turn_number=turn,
        baseline_metric=0.75,
        recent_turns=[],
        stagnation_n=0,
        state_path=str(state),
        output_path=str(out),
    )


# ---------------------------------------------------------------------------
# Dataclass field tests
# ---------------------------------------------------------------------------

def test_turn_context_fields() -> None:
    summary = TurnSummary(turn_n=1, outcome="improved", value=0.9)
    ctx = TurnContext(
        turn_number=3,
        baseline_metric=0.5,
        recent_turns=[summary],
        stagnation_n=2,
        state_path="/tmp/state",
        output_path="/tmp/out",
    )
    assert ctx.turn_number == 3
    assert ctx.baseline_metric == 0.5
    assert ctx.recent_turns == [summary]
    assert ctx.stagnation_n == 2
    assert ctx.state_path == "/tmp/state"
    assert ctx.output_path == "/tmp/out"


def test_turn_result_fields() -> None:
    tr = TurnResult(hypothesis_path="/tmp/hypothesis.md")
    assert tr.hypothesis_path == "/tmp/hypothesis.md"


def test_turn_summary_fields() -> None:
    ts = TurnSummary(turn_n=5, outcome="regressed", value=0.3)
    assert ts.turn_n == 5
    assert ts.outcome == "regressed"
    assert ts.value == 0.3


# ---------------------------------------------------------------------------
# Executor Protocol structural check
# ---------------------------------------------------------------------------

def test_executor_protocol_structural() -> None:
    """HermesExecutor and ShellExecutor must satisfy the Executor protocol."""
    assert isinstance(HermesExecutor(profile="forge"), Executor)
    assert isinstance(ShellExecutor(command="echo hi"), Executor)


# ---------------------------------------------------------------------------
# ShellExecutor tests
# ---------------------------------------------------------------------------

def test_shell_executor_writes_env(tmp_path: pathlib.Path) -> None:
    """ShellExecutor sets SATURATE_GOAL in env; script can write hypothesis.md."""
    script = tmp_path / "run.sh"
    script.write_text(
        '#!/usr/bin/env bash\n'
        'echo "$SATURATE_GOAL" > "$SATURATE_STATE_PATH/hypothesis.md"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)

    ctx = _make_context(tmp_path)
    spec = {"goal": "maximise throughput", "kind": "metric-optimization"}
    exe = ShellExecutor(command=f"bash {script}")

    result = exe.execute_turn(spec, {}, ctx)

    assert os.path.exists(result.hypothesis_path)
    content = pathlib.Path(result.hypothesis_path).read_text()
    assert "maximise throughput" in content


def test_shell_executor_creates_fallback(tmp_path: pathlib.Path) -> None:
    """When the command writes nothing, ShellExecutor creates a fallback hypothesis.md."""
    ctx = _make_context(tmp_path)
    exe = ShellExecutor(command="true")   # does nothing

    result = exe.execute_turn({"goal": "x"}, {}, ctx)

    assert os.path.exists(result.hypothesis_path)
    content = pathlib.Path(result.hypothesis_path).read_text()
    assert "no hypothesis" in content.lower() or content.strip() != ""


def test_shell_executor_passes_all_saturate_vars(tmp_path: pathlib.Path) -> None:
    """All SATURATE_* vars should be visible inside the shell command."""
    captured = tmp_path / "env_dump.txt"
    ctx = TurnContext(
        turn_number=7,
        baseline_metric=0.42,
        recent_turns=[],
        stagnation_n=3,
        state_path=str(tmp_path / "state"),
        output_path=str(tmp_path / "out"),
    )
    script = (
        f"env | grep SATURATE_ > {captured} ; "
        f"mkdir -p {ctx.state_path}"
    )
    ShellExecutor(command=script).execute_turn({"goal": "go", "kind": "opt"}, {}, ctx)

    env_text = captured.read_text()
    assert "SATURATE_GOAL=go" in env_text
    assert "SATURATE_TURN=7" in env_text
    assert "SATURATE_BASELINE=0.42" in env_text
    assert "SATURATE_STAGNATION=3" in env_text


def test_shell_executor_does_not_raise_on_nonzero_exit(tmp_path: pathlib.Path) -> None:
    """ShellExecutor uses check=False — a failing command should not raise."""
    ctx = _make_context(tmp_path)
    exe = ShellExecutor(command="exit 1")   # shell exits non-zero
    # Should not raise
    result = exe.execute_turn({}, {}, ctx)
    assert result.hypothesis_path.endswith("hypothesis.md")


# ---------------------------------------------------------------------------
# make_executor factory tests
# ---------------------------------------------------------------------------

def test_make_executor_hermes() -> None:
    from loop_spec import ExecutorSpec
    exe = make_executor(ExecutorSpec(type="hermes", profile="forge"))
    assert isinstance(exe, HermesExecutor)
    assert exe.profile == "forge"


def test_make_executor_shell() -> None:
    from loop_spec import ExecutorSpec
    exe = make_executor(ExecutorSpec(type="shell", command="echo hi"))
    assert isinstance(exe, ShellExecutor)
    assert exe.command == "echo hi"


def test_make_executor_default_is_shell() -> None:
    """When executor_spec is None the factory should default to ShellExecutor."""
    exe = make_executor(None)
    assert isinstance(exe, ShellExecutor)


def test_make_executor_unknown() -> None:
    from loop_spec import ExecutorSpec
    with pytest.raises(NotImplementedError):
        make_executor(ExecutorSpec(type="http"))


# ---------------------------------------------------------------------------
# HermesExecutor._build_message tests
# ---------------------------------------------------------------------------

def test_hermes_builds_message_contains_goal() -> None:
    exe = HermesExecutor(profile="forge")
    spec = {"goal": "reduce latency", "kind": "metric-optimization"}
    ctx = TurnContext(
        turn_number=4,
        baseline_metric=1.23,
        recent_turns=[],
        stagnation_n=0,
        state_path="/tmp/s",
        output_path="/tmp/o",
    )
    msg = exe._build_message(spec, ctx)
    assert "reduce latency" in msg
    assert "4" in msg   # turn number


def test_hermes_builds_message_contains_turn_number() -> None:
    exe = HermesExecutor(profile="forge")
    spec = {"goal": "improve accuracy"}
    ctx = TurnContext(
        turn_number=12,
        baseline_metric=None,
        recent_turns=[],
        stagnation_n=1,
        state_path="/tmp/s",
        output_path="/tmp/o",
    )
    msg = exe._build_message(spec, ctx)
    assert "12" in msg


def test_hermes_builds_message_includes_recent_turns() -> None:
    exe = HermesExecutor(profile="forge")
    spec = {"goal": "go faster"}
    ctx = TurnContext(
        turn_number=3,
        baseline_metric=0.5,
        recent_turns=[
            TurnSummary(turn_n=1, outcome="improved", value=0.6),
            TurnSummary(turn_n=2, outcome="regressed", value=0.4),
        ],
        stagnation_n=1,
        state_path="/tmp/s",
        output_path="/tmp/o",
    )
    msg = exe._build_message(spec, ctx)
    assert "improved" in msg
    assert "regressed" in msg


def test_hermes_builds_message_no_recent_turns_shows_none_yet() -> None:
    exe = HermesExecutor(profile="forge")
    spec = {"goal": "go"}
    ctx = TurnContext(
        turn_number=1,
        baseline_metric=None,
        recent_turns=[],
        stagnation_n=0,
        state_path="/tmp/s",
        output_path="/tmp/o",
    )
    msg = exe._build_message(spec, ctx)
    assert "none yet" in msg.lower()


# ---------------------------------------------------------------------------
# HermesExecutor.execute_turn — mock the CLI call
# ---------------------------------------------------------------------------

def test_hermes_execute_turn_writes_task_message(tmp_path: pathlib.Path) -> None:
    """execute_turn should write task_message.md before calling the CLI."""
    ctx = _make_context(tmp_path)
    spec = {"goal": "my goal", "kind": "metric-optimization"}

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="agent output", returncode=0)
        exe = HermesExecutor(profile="test-profile")
        exe.execute_turn(spec, {}, ctx)

    msg_path = pathlib.Path(ctx.state_path) / "task_message.md"
    assert msg_path.exists()
    assert "my goal" in msg_path.read_text()


def test_hermes_execute_turn_fallback_when_no_hypothesis(tmp_path: pathlib.Path) -> None:
    """If the hermes agent does not write hypothesis.md, fall back to stdout."""
    ctx = _make_context(tmp_path)
    spec = {"goal": "test goal"}

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="cli stdout result", returncode=0)
        exe = HermesExecutor(profile="test-profile")
        result = exe.execute_turn(spec, {}, ctx)

    hyp = pathlib.Path(result.hypothesis_path)
    assert hyp.exists()
    assert "cli stdout result" in hyp.read_text()


def test_hermes_execute_turn_uses_existing_hypothesis(tmp_path: pathlib.Path) -> None:
    """If the agent writes hypothesis.md, the executor must NOT overwrite it."""
    ctx = _make_context(tmp_path)
    spec = {"goal": "test goal"}

    # Pre-create hypothesis.md as if the agent wrote it
    os.makedirs(ctx.state_path, exist_ok=True)
    hyp_path = pathlib.Path(ctx.state_path) / "hypothesis.md"
    hyp_path.write_text("agent-written hypothesis content")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="should not appear", returncode=0)
        exe = HermesExecutor(profile="test-profile")
        result = exe.execute_turn(spec, {}, ctx)

    assert pathlib.Path(result.hypothesis_path).read_text() == "agent-written hypothesis content"
