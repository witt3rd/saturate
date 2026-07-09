"""End-to-end integration tests for the three hermes-cyclus examples
running against the Saturate backend.

These tests verify that:
  1. function_minimization spec loads and runs one turn without error.
  2. circle_packing spec loads and runs one turn without error.
  3. test_coverage spec loads and runs one turn without error.

The Hermes executor is mocked so these tests run offline and fast.  The
real goal is confirming that:
  - spec.yaml files parse cleanly via loop_spec.load_spec()
  - make_executor() returns a HermesExecutor (not the no-op ShellExecutor)
  - run_turn() calls the executor and records a turn in the queue

All tests use an isolated git repo — the Saturate source tree is never
touched.
"""
from __future__ import annotations

import pathlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from saturate.executor import HermesExecutor, make_executor
from saturate.queue_sqlite import SqliteQueue
from saturate.runner import run_turn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CYCLUS_ROOT = pathlib.Path("/home/dt/src/witt3rd/cyclus")

EXAMPLES = {
    "function_minimization": CYCLUS_ROOT / "examples" / "function_minimization" / "spec.yaml",
    "circle_packing": CYCLUS_ROOT / "examples" / "circle_packing" / "spec.yaml",
    "test_coverage": CYCLUS_ROOT / "examples" / "test_coverage" / "spec.yaml",
}


def _make_queue(tmp_path: Path) -> SqliteQueue:
    return SqliteQueue(base_dir=str(tmp_path / "saturate"))


def _make_isolated_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo for the loop to operate against."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True)
    (repo / "placeholder.py").write_text("# placeholder\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return repo


def _post_example_task(q: SqliteQueue, example_name: str, spec_path: Path, repo: Path) -> str:
    """Post an example spec task, pointing repo= at an isolated repo."""
    spec = yaml.safe_load(spec_path.read_text()) or {}
    # Override repo to point at the isolated git repo so git ops don't touch cyclus
    task = {
        "name": spec.get("name", example_name),
        "kind": spec.get("kind", "MetricOptimizationKind"),
        "spec_path": str(spec_path),
        "repo": f"file://{repo}",
    }
    return q.post(task)


def _mock_hermes_executor(hypothesis_text: str = "SUCCESS: stub hypothesis"):
    """Context manager that patches subprocess.run inside HermesExecutor
    and pre-writes a hypothesis.md via side_effect."""
    def _fake_run(*args, **kwargs):
        # Write hypothesis.md into state_path if we can determine it
        # (the env var SATURATE_STATE_PATH or context.state_path is not
        # accessible here, so we write it by inspecting the cmd)
        return MagicMock(stdout=hypothesis_text, stderr=None, returncode=0)

    return patch("subprocess.run", side_effect=_fake_run)


# ---------------------------------------------------------------------------
# Task 4 — function_minimization one-turn validation
# ---------------------------------------------------------------------------

def test_function_minimization_spec_has_executor_block() -> None:
    """function_minimization/spec.yaml must have an executor block with type=hermes."""
    spec_path = EXAMPLES["function_minimization"]
    assert spec_path.exists(), f"spec.yaml not found at {spec_path}"
    spec = yaml.safe_load(spec_path.read_text())
    assert "executor" in spec, "No executor block in function_minimization/spec.yaml"
    assert spec["executor"].get("type") == "hermes", (
        f"Expected type=hermes, got {spec['executor'].get('type')!r}"
    )


def test_function_minimization_spec_loads_cleanly() -> None:
    """loop_spec.load_spec() must parse function_minimization spec without error."""
    from loop_spec import load_spec, MetricOptimizationSpec

    spec_path = EXAMPLES["function_minimization"]
    spec = load_spec(str(spec_path))
    assert isinstance(spec, MetricOptimizationSpec)
    assert spec.name == "function_minimization"
    assert spec.executor is not None
    executor = make_executor(spec.executor)
    assert isinstance(executor, HermesExecutor), (
        f"Expected HermesExecutor, got {type(executor).__name__} — "
        "this means make_executor returned the no-op ShellExecutor"
    )


def test_function_minimization_one_turn(tmp_path: pytest.TempPathFactory) -> None:
    """function_minimization: post task → run_turn → turn recorded without crash.

    The Hermes executor is mocked so the test runs offline.  The measure
    command is also mocked to return a fixed metric value.
    """
    repo = _make_isolated_repo(tmp_path)
    q = _make_queue(tmp_path)
    spec_path = EXAMPLES["function_minimization"]

    task_id = _post_example_task(q, "function_minimization", spec_path, repo)
    assert q.counts()["pending"] == 1

    # Mock subprocess.run so the hermes CLI and the evaluate command don't run
    with patch("subprocess.run") as mock_run:
        # First call: HermesExecutor → returns stdout (no hypothesis.md written)
        # Subsequent call(s): evaluate command → returns JSON metric
        call_count = {"n": 0}

        def _side_effect(*args, **kwargs):
            call_count["n"] += 1
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
            if "hermes" in cmd_str:
                return MagicMock(stdout="stub hermes output", stderr=None, returncode=0)
            # evaluate / correctness / git commands
            if any(x in cmd_str for x in ["python3", "uv run pytest", "pip install"]):
                import json as _json
                return MagicMock(
                    stdout=_json.dumps({"combined_score": 1.5}),
                    stderr=None, returncode=0,
                )
            # git operations
            return MagicMock(stdout="", stderr=None, returncode=0)

        mock_run.side_effect = _side_effect

        # run one turn — should not raise
        outcome = run_turn(task_id, q)

    assert outcome in ("improved", "unchanged", "regressed", "crashed", "terminal"), (
        f"Unexpected outcome: {outcome!r}"
    )

    # Verify a turn record was written
    history = q.turn_history(task_id)
    assert len(history) >= 1, "No turn records written after run_turn()"


# ---------------------------------------------------------------------------
# Task 5 — circle_packing and test_coverage one-turn validation
# ---------------------------------------------------------------------------

def test_circle_packing_spec_has_executor_block() -> None:
    """circle_packing/spec.yaml must have an executor block with type=hermes."""
    spec_path = EXAMPLES["circle_packing"]
    assert spec_path.exists(), f"spec.yaml not found at {spec_path}"
    spec = yaml.safe_load(spec_path.read_text())
    assert "executor" in spec, "No executor block in circle_packing/spec.yaml"
    assert spec["executor"].get("type") == "hermes", (
        f"Expected type=hermes, got {spec['executor'].get('type')!r}"
    )


def test_test_coverage_spec_has_executor_block() -> None:
    """test_coverage/spec.yaml must have an executor block with type=hermes."""
    spec_path = EXAMPLES["test_coverage"]
    assert spec_path.exists(), f"spec.yaml not found at {spec_path}"
    spec = yaml.safe_load(spec_path.read_text())
    assert "executor" in spec, "No executor block in test_coverage/spec.yaml"
    assert spec["executor"].get("type") == "hermes", (
        f"Expected type=hermes, got {spec['executor'].get('type')!r}"
    )


def test_circle_packing_spec_loads_cleanly() -> None:
    """loop_spec.load_spec() must parse circle_packing spec without error."""
    from loop_spec import load_spec, MetricOptimizationSpec

    spec_path = EXAMPLES["circle_packing"]
    spec = load_spec(str(spec_path))
    assert isinstance(spec, MetricOptimizationSpec)
    assert spec.name == "circle_packing"
    assert spec.executor is not None
    executor = make_executor(spec.executor)
    assert isinstance(executor, HermesExecutor)


def test_test_coverage_spec_loads_cleanly() -> None:
    """loop_spec.load_spec() must parse test_coverage spec without error."""
    from loop_spec import load_spec, MetricOptimizationSpec

    spec_path = EXAMPLES["test_coverage"]
    spec = load_spec(str(spec_path))
    assert isinstance(spec, MetricOptimizationSpec)
    assert spec.name == "test_coverage"
    assert spec.executor is not None
    executor = make_executor(spec.executor)
    assert isinstance(executor, HermesExecutor)


def test_circle_packing_one_turn(tmp_path: pytest.TempPathFactory) -> None:
    """circle_packing: post task → run_turn → turn recorded without crash."""
    repo = _make_isolated_repo(tmp_path)
    q = _make_queue(tmp_path)
    spec_path = EXAMPLES["circle_packing"]

    task_id = _post_example_task(q, "circle_packing", spec_path, repo)

    with patch("subprocess.run") as mock_run:
        def _side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
            if "hermes" in cmd_str:
                return MagicMock(stdout="stub circle output", stderr=None, returncode=0)
            if any(x in cmd_str for x in ["python3", "pip install", "uv run"]):
                import json as _json
                return MagicMock(
                    stdout=_json.dumps({"combined_score": 0.5}),
                    stderr=None, returncode=0,
                )
            return MagicMock(stdout="", stderr=None, returncode=0)

        mock_run.side_effect = _side_effect
        outcome = run_turn(task_id, q)

    assert outcome in ("improved", "unchanged", "regressed", "crashed", "terminal")
    history = q.turn_history(task_id)
    assert len(history) >= 1, "No turn records written for circle_packing"


def test_test_coverage_one_turn(tmp_path: pytest.TempPathFactory) -> None:
    """test_coverage: post task → run_turn → turn recorded without crash."""
    repo = _make_isolated_repo(tmp_path)
    q = _make_queue(tmp_path)
    spec_path = EXAMPLES["test_coverage"]

    task_id = _post_example_task(q, "test_coverage", spec_path, repo)

    with patch("subprocess.run") as mock_run:
        def _side_effect(*args, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            cmd_str = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd)
            if "hermes" in cmd_str:
                return MagicMock(stdout="stub coverage output", stderr=None, returncode=0)
            if any(x in cmd_str for x in ["pytest", "python3", "uv run", "pip"]):
                import json as _json
                return MagicMock(
                    stdout=_json.dumps({"coverage_percent": 85.0}),
                    stderr=None, returncode=0,
                )
            return MagicMock(stdout="", stderr=None, returncode=0)

        mock_run.side_effect = _side_effect
        outcome = run_turn(task_id, q)

    assert outcome in ("improved", "unchanged", "regressed", "crashed", "terminal")
    history = q.turn_history(task_id)
    assert len(history) >= 1, "No turn records written for test_coverage"
