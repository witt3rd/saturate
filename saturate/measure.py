"""
saturate.measure — four-outcome scalar metric primitive

Runs a shell command, extracts a numeric value, and classifies the result
relative to a baseline as: improved, regressed, crashed, or unchanged.

Key invariant: crashed != regressed.
A hypothesis that crashes the benchmark does NOT pollute baseline statistics.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Literal, Optional

Outcome = Literal["improved", "regressed", "crashed", "unchanged"]


@dataclass
class MeasureResult:
    value: float
    unit: str
    outcome: Outcome
    raw: str


def _run_once(command: str, extract: str, timeout: int) -> tuple[float, str]:
    """
    Run command once and return (extracted_value, raw_stdout).

    Raises:
        subprocess.CalledProcessError: on non-zero exit
        subprocess.TimeoutExpired:     on timeout
        ValueError:                    on extraction failure
    """
    start = time.perf_counter()
    proc = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - start

    if proc.returncode != 0:
        raise subprocess.CalledProcessError(
            proc.returncode, command, proc.stdout, proc.stderr
        )

    raw = proc.stdout

    if extract == "wall_clock":
        return elapsed, raw

    if extract.startswith("regex:"):
        pattern = extract[len("regex:"):]
        m = re.search(pattern, raw)
        if m is None:
            raise ValueError(
                f"Pattern {pattern!r} did not match command stdout: {raw!r}"
            )
        return float(m.group(1)), raw

    if extract.startswith("json:"):
        key = extract[len("json:"):]
        data = json.loads(raw)
        return float(data[key]), raw

    raise ValueError(
        f"Unknown extract mode {extract!r}. "
        "Expected: 'wall_clock', 'regex:<pattern>', or 'json:<key>'"
    )


def _classify(value: float, baseline: Optional[float], direction: str) -> Outcome:
    """
    Classify a measured value relative to baseline.

    Noise threshold: within 1% of baseline on the improvement side → unchanged.
    No margin on the regression side (any degradation counts).
    baseline=None means this is the first measurement → unchanged.
    """
    if baseline is None:
        return "unchanged"

    if direction == "minimize":
        # Better = smaller value
        if value < baseline * 0.99:   # at least 1% improvement
            return "improved"
        if value > baseline:          # any degradation
            return "regressed"
        return "unchanged"

    if direction == "maximize":
        # Better = larger value
        if value > baseline * 1.01:   # at least 1% improvement
            return "improved"
        if value < baseline:          # any degradation
            return "regressed"
        return "unchanged"

    raise ValueError(f"Unknown direction {direction!r}. Expected: 'minimize' or 'maximize'")


def measure(
    command: str,
    extract: str,           # 'wall_clock' | 'regex:<pattern>' | 'json:<key>'
    direction: str,         # 'minimize' | 'maximize'
    baseline: Optional[float] = None,
    runs: int = 1,
    timeout: int = 300,
) -> MeasureResult:
    """
    Run ``command`` (via shell), extract a scalar, and classify against baseline.

    Parameters
    ----------
    command:   Shell command to run.
    extract:   How to extract the metric:
                 'wall_clock'        — wall-clock seconds, unit='s'
                 'regex:<pattern>'   — first capture group of regex applied to stdout
                 'json:<key>'        — JSON key from stdout
    direction: 'minimize' (lower is better) or 'maximize' (higher is better).
    baseline:  Prior best value. None means this is the first measurement.
    runs:      Number of times to run the command; result is the average.
               Crashes on any single run immediately return outcome='crashed'.
    timeout:   Per-run timeout in seconds.

    Returns
    -------
    MeasureResult with outcome in {'improved', 'regressed', 'crashed', 'unchanged'}.
    """
    unit = "s" if extract == "wall_clock" else ""

    values: list[float] = []
    last_raw = ""

    for _ in range(runs):
        try:
            val, raw = _run_once(command, extract, timeout)
            values.append(val)
            last_raw = raw
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            # crashed — value represents "worst possible" for the direction
            crash_value = float("inf") if direction == "minimize" else 0.0
            raw = getattr(exc, "stdout", "") or ""
            return MeasureResult(
                value=crash_value,
                unit=unit,
                outcome="crashed",
                raw=raw,
            )

    avg = sum(values) / len(values)
    outcome = _classify(avg, baseline, direction)
    return MeasureResult(value=avg, unit=unit, outcome=outcome, raw=last_raw)
