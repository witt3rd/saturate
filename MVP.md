# Saturate — MVP Plan

> **Written:** 2026-07-06 17:04 PDT
> **Goal:** Minimum runnable slice that can use itself to build the next layer.

---

## The Bootstrap Thesis

The fastest path to a working Saturate is not to build it completely before
using it. It is to build the smallest slice that can run a loop, then run a
loop that builds the rest.

**Phase 0** (hand-written, ~300 lines) makes Saturate runnable.
**Phase 1** (Saturate-built) makes Saturate production-grade.
Every phase after that is built by Saturate running against its own codebase.

---

## Phase 0: The Hand-Written MVP

Everything in Phase 0 is written by hand. It is the only code we write
manually. Its only job is to be capable of running the Phase 1 loop.

### What it is NOT

- No SQLite — file-based JSON sidecars are correct for Phase 0
- No Nomad — single node only
- No scheduler loop — manual `saturate run` drives each turn
- No priority/deadline scoring — first-in-first-out
- No PostgreSQL — that comes out of the Phase 1 loop
- No web dashboard

### Files

```
saturate/
├── measure.py      ~60 lines    saturate.measure — the metric primitive
├── queue.py        ~80 lines    file-based queue (JSON sidecars, no atomicity)
├── executor.py     ~100 lines   Executor protocol + HermesExecutor + ShellExecutor
├── runner.py       ~80 lines    one-turn runner (reads spec → executor → measure → keep/revert)
└── cli.py          ~60 lines    saturate submit / saturate run / saturate status
```

**Total: ~380 lines.** Nothing more.

### measure.py

```python
@dataclass
class MeasureResult:
    value:    float
    unit:     str
    outcome:  Literal["improved", "regressed", "crashed", "unchanged"]
    raw:      str

def measure(
    command:   str,
    extract:   str,           # "wall_clock" | "regex:<pattern>" | "json:<key>"
    direction: str,           # "minimize" | "maximize"
    baseline:  Optional[float],
    runs:      int = 1,
) -> MeasureResult: ...
```

The four outcomes. `crashed ≠ regressed`. Self-contained, no other deps.
**This is the first file to write and the first to test.**

### queue.py (file-based)

```
~/.saturate/
├── queue/
│   ├── pending/    <task_id>.json   # submitted, not yet claimed
│   ├── running/    <task_id>.json   # claimed, in progress
│   └── done/       <task_id>.json   # completed (success or failure)
└── state/
    └── <task_id>/
        ├── state.json              # current baseline, turn count, stagnation
        ├── hypothesis.md           # written by executor each turn
        └── turns/
            └── <n>.json           # per-turn audit record
```

Four operations over this directory structure. No locking needed on a single
node. `claim()` is an atomic file rename: `pending/ → running/`.

### executor.py

```python
class Executor(Protocol):
    def execute_turn(self, spec, state, context) -> TurnResult: ...

class HermesExecutor:
    """Invokes: hermes -p {profile} --once "{task_message}" """
    profile: str

class ShellExecutor:
    """Invokes arbitrary executable with context as env vars."""
    command: str
```

`HTTPExecutor` deferred to Phase 1 — not needed for the bootstrap.

### runner.py

One function. Does one turn. Exits.

```python
def run_turn(task_id: str, queue_dir: str) -> None:
    task  = queue.load(task_id)
    spec  = load_spec(task.spec_path)
    state = load_state(task)
    ctx   = build_context(state)

    executor = make_executor(spec.executor)
    result   = executor.execute_turn(spec, state, ctx)

    m = measure(spec.metric, baseline=state.baseline)

    if m.outcome == "improved":
        commit_hypothesis(result, state)
        state = advance_baseline(state, m)
    else:
        revert_hypothesis(result)

    state = record_turn(state, m, result)

    if terminal(state, spec):
        queue.complete(task, state)
    else:
        queue.requeue(task, state)   # back to pending for next turn
```

### cli.py

```
saturate submit <spec.yaml>          →  creates task, writes to pending/
saturate run <task_id>               →  runs ONE turn (calls run_turn)
saturate run --loop <task_id>        →  runs turns until terminal (blocking)
saturate status                      →  shows pending/running/done counts
saturate status <task_id>            →  shows turn history for a task
```

`saturate run --loop` is the manual driver for Phase 0. In Phase 1 the
scheduler loop replaces it. For now it is enough.

---

## The First Loop Spec

The moment Phase 0 is running, we write this file and submit it:

```yaml
# goals/phase1-implementation.yaml

name:     phase1-implementation
kind:     task-execution
goal:     >
  Implement Phase 1 of Saturate: replace the file-based queue with a proper
  SQLite-backed queue, add the SaturateTask dataclass with full scheduling
  fields, and implement the scheduler tick with idle detection and priority
  dispatch. All existing tests must continue to pass.
plan_path: ./plans/phase1.md
executor:
  type:    hermes
  profile: forge
memory:   ./output/phase1/
```

And `plans/phase1.md` is the Phase 1 roadmap from ARCHITECTURE.md written as a task list:

```markdown
# Phase 1 Implementation Plan

- [ ] Define SaturateTask dataclass with all scheduling fields
- [ ] Implement SQLite queue (queue.db schema, four operations)
- [ ] Implement atomic claim via SQLite BEGIN EXCLUSIVE transaction
- [ ] Port file-based queue tests to SQLite queue
- [ ] Implement scheduler tick: idle detection, dispatch_score, dispatch loop
- [ ] Implement heartbeat and crash recovery (ELIGIBLE ← RUNNING on timeout)
- [ ] Add `saturate start` CLI command (runs scheduler loop)
- [ ] Write integration test: submit → run → complete full lifecycle
```

Saturate runs Forge. Forge builds Phase 1. We review and merge what lands.

---

## The Build Sequence

```
Today (tonight, optional)
  write measure.py + tests          ~60 lines + ~40 lines tests
  → prove saturate.measure works before anything else touches it

Tomorrow morning
  write queue.py                    ~80 lines
  write executor.py                 ~100 lines (Hermes + Shell)
  write runner.py                   ~80 lines
  write cli.py                      ~60 lines
  → saturate submit + saturate run --loop working end-to-end

Tomorrow afternoon
  write plans/phase1.md             ~15 lines
  write goals/phase1-implementation.yaml
  saturate submit goals/phase1-implementation.yaml
  saturate run --loop <task_id>
  → Forge runs, Phase 1 starts emerging
  → we review each task completion, merge what's good

This week
  Phase 1 complete (via Saturate loop)
  → proper SQLite queue, SaturateTask, scheduler tick

Next week
  Phase 2 built by a Saturate loop running against Phase 1
  → PostgreSQL, Nomad integration
```

---

## Definition of Done for Phase 0

Phase 0 is done when this command works end-to-end on a single machine:

```bash
saturate submit goals/phase1-implementation.yaml
saturate run --loop <task_id>
```

And produces a `./output/phase1/` directory with Forge's work product
from the first turn.

---

## What Phase 0 Does NOT Need to Be

- Correct about concurrent access (single node, single process)
- Fast (30-second poll intervals are fine)
- Pretty (CLI output can be minimal)
- Complete (any spec field not needed for the bootstrap loop is deferred)
- Tested beyond measure.py (the rest gets tested via the Phase 1 loop)

The only quality gate for Phase 0 is: **can it run the Phase 1 loop?**

---

## Files to Write, In Order

1. `saturate/measure.py` + `tests/test_measure.py`
2. `saturate/queue.py`
3. `saturate/executor.py`
4. `saturate/runner.py`
5. `saturate/cli.py`
6. `plans/phase1.md`
7. `goals/phase1-implementation.yaml`

Stop after step 7. Submit the goal. Let Saturate build the rest.
