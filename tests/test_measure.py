"""
Tests for saturate.measure — four-outcome scalar metric primitive.

Strategy:
  - wall_clock tests use real subprocess timing with carefully chosen
    baselines that make outcomes deterministic regardless of machine speed.
  - regex / json tests use echo commands with predictable output.
  - crashed test uses 'exit 1' to guarantee non-zero exit.
"""
import json
import math
import sys

import pytest

from saturate.measure import MeasureResult, measure


# ---------------------------------------------------------------------------
# wall_clock — basic
# ---------------------------------------------------------------------------

def test_wall_clock_basic():
    """First measurement (baseline=None) → outcome='unchanged', value > 0."""
    result = measure("echo hi", extract="wall_clock", direction="minimize", baseline=None)
    assert isinstance(result, MeasureResult)
    assert result.outcome == "unchanged"
    assert result.value > 0
    assert result.unit == "s"


# ---------------------------------------------------------------------------
# wall_clock — improved
# ---------------------------------------------------------------------------

def test_wall_clock_improved():
    """
    baseline=10.0s, 'echo hi' runs in ~0.01s.
    0.01 < 10.0 * 0.99 = 9.9 → improved.
    """
    result = measure(
        "echo hi",
        extract="wall_clock",
        direction="minimize",
        baseline=10.0,
    )
    assert result.outcome == "improved", (
        f"Expected 'improved' but got {result.outcome!r} (value={result.value:.4f}s, baseline=10.0s)"
    )


# ---------------------------------------------------------------------------
# wall_clock — regressed
# ---------------------------------------------------------------------------

def test_wall_clock_regressed():
    """
    baseline=0.000001s (1 µs), any subprocess call takes >> 1 µs → regressed.
    Using 'sleep 0.05' for a reliable ~50ms measurement.
    """
    result = measure(
        "sleep 0.05",
        extract="wall_clock",
        direction="minimize",
        baseline=0.000001,    # 1 microsecond — guaranteed to be beaten by any subprocess
    )
    assert result.outcome == "regressed", (
        f"Expected 'regressed' but got {result.outcome!r} (value={result.value:.4f}s, baseline=0.000001s)"
    )


# ---------------------------------------------------------------------------
# crashed — not regressed
# ---------------------------------------------------------------------------

def test_crashed_not_regressed():
    """
    Non-zero exit → outcome='crashed', never 'regressed'.
    For minimize, crashed value = float('inf').
    """
    result = measure(
        "exit 1",
        extract="wall_clock",
        direction="minimize",
        baseline=1.0,
    )
    assert result.outcome == "crashed", (
        f"Expected 'crashed' but got {result.outcome!r}"
    )
    assert result.outcome != "regressed"
    assert math.isinf(result.value)


# ---------------------------------------------------------------------------
# baseline=None always → unchanged
# ---------------------------------------------------------------------------

def test_baseline_none():
    """baseline=None → outcome='unchanged' regardless of measured value."""
    result = measure(
        "sleep 0.1",      # relatively slow command
        extract="wall_clock",
        direction="minimize",
        baseline=None,
    )
    assert result.outcome == "unchanged", (
        f"Expected 'unchanged' (first measurement) but got {result.outcome!r}"
    )


# ---------------------------------------------------------------------------
# direction=maximize — improved
# ---------------------------------------------------------------------------

def test_direction_maximize():
    """
    direction='maximize', baseline=5.0.
    echo JSON with value=6.5 → 6.5 > 5.0 * 1.01 = 5.05 → improved.
    """
    result = measure(
        "echo '{\"score\": 6.5}'",
        extract="json:score",
        direction="maximize",
        baseline=5.0,
    )
    assert result.outcome == "improved", (
        f"Expected 'improved' but got {result.outcome!r} (value={result.value})"
    )
    assert abs(result.value - 6.5) < 1e-9


# ---------------------------------------------------------------------------
# regex extraction
# ---------------------------------------------------------------------------

def test_regex_extract():
    """Command prints a float; regex extracts it correctly."""
    result = measure(
        "echo 'elapsed: 3.14 seconds'",
        extract=r"regex:(\d+\.\d+)",
        direction="minimize",
        baseline=None,
    )
    assert result.outcome == "unchanged"
    assert abs(result.value - 3.14) < 1e-9
    assert result.unit == ""      # not wall_clock


# ---------------------------------------------------------------------------
# json extraction
# ---------------------------------------------------------------------------

def test_json_extract():
    """Command prints JSON; 'json:<key>' extracts the named field."""
    result = measure(
        "echo '{\"duration\": 2.5}'",
        extract="json:duration",
        direction="minimize",
        baseline=None,
    )
    assert result.outcome == "unchanged"
    assert abs(result.value - 2.5) < 1e-9
    assert result.unit == ""


# ---------------------------------------------------------------------------
# multi-run averaging
# ---------------------------------------------------------------------------

def test_multi_run_averages():
    """runs=3 averages values across calls; outcome still classified correctly."""
    result = measure(
        "echo '{\"val\": 4.0}'",
        extract="json:val",
        direction="maximize",
        baseline=None,
        runs=3,
    )
    assert result.outcome == "unchanged"
    assert abs(result.value - 4.0) < 1e-9


def test_multi_run_crashed_on_first_bad():
    """If any run crashes, immediately return 'crashed' without averaging."""
    result = measure(
        "exit 2",
        extract="wall_clock",
        direction="maximize",
        baseline=None,
        runs=3,
    )
    assert result.outcome == "crashed"
    assert result.value == 0.0    # maximize crash sentinel


# ---------------------------------------------------------------------------
# unchanged — within noise threshold
# ---------------------------------------------------------------------------

def test_unchanged_within_noise_minimize():
    """
    direction='minimize', value exactly at baseline → unchanged (no movement).
    Use json extraction so we control the exact value.
    """
    result = measure(
        "echo '{\"x\": 5.0}'",
        extract="json:x",
        direction="minimize",
        baseline=5.0,
    )
    # 5.0 is NOT < 5.0*0.99=4.95 (improved) and NOT > 5.0 (regressed)
    assert result.outcome == "unchanged"


def test_unchanged_within_noise_maximize():
    """direction='maximize', value at baseline * 1.005 (0.5% up) → unchanged."""
    result = measure(
        "echo '{\"x\": 5.05}'",     # 1% above baseline exactly = boundary
        extract="json:x",
        direction="maximize",
        baseline=5.0,
    )
    # 5.05 = 5.0 * 1.01 exactly — NOT strictly greater, so unchanged
    assert result.outcome == "unchanged"
