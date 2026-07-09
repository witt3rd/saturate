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

These tests require a sibling checkout of hermes-cyclus.  When the repo
is not present (e.g. in CI without the sibling), the whole module is
skipped via ``pytestmark``.
"""

from __future__ import annotations

import json as _json
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
# Paths
# ---------------------------------------------------------------------------

CYCLUS_ROOT = pathlib.Path(
    __import__("os").environ.get("CYCLUS_ROOT", "/home/dt/src/witt3rd/cyclus")
)

EXAMPLES = {
    "function_minimization": CYCLUS_ROOT
    / "examples"
    / "function_minimization"
    / "spec.yaml",
    "circle_packing": CYCLUS_ROOT / "examples" / "circle_packing" / "spec.yaml",
    "test_coverage": CYCLUS_ROOT / "examples" / "test_coverage" / "spec.yaml",
}

# Skip all tests in this module when the cyclus repo is not available.
pytestmark = pytest.mark.skipif(
    not CYCLUS_ROOT.exists(),
    reason=f"hermes-cyclus repo not found at {CYCLUS_ROOT} (set CYCLUS_ROOT env var)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_queue(tmp_path: Path) -> SqliteQueue:
    return SqliteQueue(base_dir=str(tmp_path / "saturate"))


def _make_isolated_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo for the loop to operate against."""
    repo = tmp_path / "target_repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    (repo / "placeholder.py").write_text("# placeholder\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, capture_output=True, check=True
    )
    return repo


def _post_example_task(
    q: SqliteQueue, example_name: str, spec_path: Path, repo: Path
) -> str:
    """Post an example spec task, pointing repo= at an isolated repo."""
    spec = yaml.safe_load(spec_path.read_text()) or {}
    task = {
        "name": spec.get("name", example_name),
        "kind": spec.get("kind", "MetricOptimizationKind"),
        "spec_path": str(spec_path),
        "repo": f"file://{repo}",
    }
    return q.post(task)


def _metric_side_effect(metric_key: str, metric_value: float):
    """Return a subprocess.run side_effect that:
    - For hermes CLI calls (list-based, shell=False): returns stub stdout
    - For shell=True calls (evaluate/correctness): returns JSON with metric_key
    - For all other calls (git, etc.): returns empty stdout
    This is keyed on shell=True rather than substring matching, so it stays
    stable even if the exact evaluate/correctness command strings change.
    """

    def _side_effect(*args, **kwargs):
        shell = kwargs.get("shell", False)
        cmd = args[0] if args else kwargs.get("args", [])
        # Hermes executor: list command, no shell
        if isinstance(cmd, list) and cmd and cmd[0] == "hermes":
            return MagicMock(stdout="stub hermes output", stderr=None, returncode=0)
        # evaluate / correctness: shell=True
        if shell:
            return MagicMock(
                stdout=_json.dumps({metric_key: metric_value}),
                stderr=None,
                returncode=0,
            )
        # git and other list commands
        return MagicMock(stdout="", stderr=None, returncode=0)

    return _side_effect


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
    from loop_spec import MetricOptimizationSpec, load_spec

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


def test_function_minimization_one_turn(tmp_path: Path) -> None:
    """function_minimization: post task → run_turn → turn recorded without crash."""
    repo = _make_isolated_repo(tmp_path)
    q = _make_queue(tmp_path)
    spec_path = EXAMPLES["function_minimization"]

    task_id = _post_example_task(q, "function_minimization", spec_path, repo)
    assert q.counts()["pending"] == 1

    with patch(
        "subprocess.run", side_effect=_metric_side_effect("combined_score", 1.5)
    ):
        outcome = run_turn(task_id, q)

    assert outcome in ("improved", "unchanged", "regressed", "crashed", "terminal"), (
        f"Unexpected outcome: {outcome!r}"
    )
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
    from loop_spec import MetricOptimizationSpec, load_spec

    spec_path = EXAMPLES["circle_packing"]
    spec = load_spec(str(spec_path))
    assert isinstance(spec, MetricOptimizationSpec)
    assert spec.name == "circle_packing"
    assert spec.executor is not None
    executor = make_executor(spec.executor)
    assert isinstance(executor, HermesExecutor)


def test_test_coverage_spec_loads_cleanly() -> None:
    """loop_spec.load_spec() must parse test_coverage spec without error."""
    from loop_spec import MetricOptimizationSpec, load_spec

    spec_path = EXAMPLES["test_coverage"]
    spec = load_spec(str(spec_path))
    assert isinstance(spec, MetricOptimizationSpec)
    assert spec.name == "test_coverage"
    assert spec.executor is not None
    executor = make_executor(spec.executor)
    assert isinstance(executor, HermesExecutor)


def test_circle_packing_one_turn(tmp_path: Path) -> None:
    """circle_packing: post task → run_turn → turn recorded without crash."""
    repo = _make_isolated_repo(tmp_path)
    q = _make_queue(tmp_path)
    spec_path = EXAMPLES["circle_packing"]

    task_id = _post_example_task(q, "circle_packing", spec_path, repo)

    with patch(
        "subprocess.run", side_effect=_metric_side_effect("combined_score", 0.5)
    ):
        outcome = run_turn(task_id, q)

    assert outcome in ("improved", "unchanged", "regressed", "crashed", "terminal")
    history = q.turn_history(task_id)
    assert len(history) >= 1, "No turn records written for circle_packing"


def test_test_coverage_one_turn(tmp_path: Path) -> None:
    """test_coverage: post task → run_turn → turn recorded without crash."""
    repo = _make_isolated_repo(tmp_path)
    q = _make_queue(tmp_path)
    spec_path = EXAMPLES["test_coverage"]

    task_id = _post_example_task(q, "test_coverage", spec_path, repo)

    with patch(
        "subprocess.run", side_effect=_metric_side_effect("coverage_percent", 85.0)
    ):
        outcome = run_turn(task_id, q)

    assert outcome in ("improved", "unchanged", "regressed", "crashed", "terminal")
    history = q.turn_history(task_id)
    assert len(history) >= 1, "No turn records written for test_coverage"
