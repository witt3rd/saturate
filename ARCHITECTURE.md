# Saturate — Architecture

> **Companion:** [loop-spec](https://github.com/witt3rd/loop-spec) · [hermes-cyclus](https://github.com/witt3rd/hermes-cyclus)

---

## The One-Line Summary

> **Cyclus designs work. Saturate executes it. The handoff is a typed loop spec.**

Saturate is the execution fabric. It owns no deliberation logic — it runs what
it is given. The loop spec is the contract between producer and fabric.

---

## The Loop Spec (Source of Truth)

The canonical schema lives at [witt3rd/loop-spec](https://github.com/witt3rd/loop-spec).
Saturate depends on `loop-spec-py` for validation. It does not own the schema.

Six loop kinds — each has its own required fields:

| Kind | Terminal condition |
|------|--------------------|
| `MetricOptimizationKind` | target hit OR plateau_count OR max_iterations |
| `TaskExecutionKind` | all plan tasks pass |
| `ConsensusKind` | all roles APPROVE (incl. DRI) |
| `InformationSeekingKind` | sufficiency gate passes |
| `ClarificationKind` | human confirms (HUMAN_GATED — never auto-terminates) |
| `SelectionKind` | best candidate identified |

### Spec fields common to all kinds

```yaml
name:       string          # human-readable identifier
kind:       string          # one of the six kinds above
level:      L1 | L2 | L3   # L1=report-only, L2=assisted, L3=autonomous
terminal:
  max_iterations: int       # hard ceiling (default 100)
  plateau_count:  int       # consecutive no-improvement turns (default 10)
  target_score:   float?    # optional explicit goal value
executor:                   # how the agent is invoked (see Executor section)
output_dir: string          # where completed artifacts accumulate
repo:       string?         # git URL of the project being optimized
```

`load_spec()` validates the spec before any work is dispatched. A
`ValidationError` halts immediately — malformed specs never reach the runner.

### The `repo` field

Specs that commit/revert hypotheses (`MetricOptimizationKind`,
`TaskExecutionKind`) declare the target as a **git URL**:

```yaml
repo: https://github.com/org/project.git
# git@github.com:org/project.git and file:// also valid
```

The runner clones this URL into an isolated worktree on first turn, reuses it
on subsequent turns. All git operations (commit, revert) are scoped to that
clone — the Saturate source tree is never touched. Absolute local paths are
rejected by the validator; the spec is machine-agnostic.

---

## The Four-Operation Interface

Saturate's published contract. Any conforming producer submits work; any
conforming worker claims and executes it.

```
post(item)         →  enqueue a loop spec as a task
claim()            →  atomically claim the next available task
write_state(item)  →  persist iteration progress
complete(item)     →  mark terminal, attach output
```

Additional operations:

```
cancel(task_id, reason)          →  cancel a pending or running task
find(name, kind=None)            →  locate task_id by name + kind
                                    (idempotent post patterns)
```

**HUMAN_GATED enforcement.** When `post()` loads a spec and finds
`ClarificationKind`, it sets `human_gated=1` on the task. `complete()` then
raises `HumanGatedViolation` unless `confirmed_by_human=True` is passed
explicitly. This is enforced at the queue level — no caller convention required.

**Spec-aware `post()`.** When `spec_path` is present in the task dict,
`post()` loads the spec via `load_spec()` and derives `name`, `kind`, and
`human_gated` from it. The spec is the source of truth; caller-supplied values
for these fields are overridden.

---

## The Durable Queue

### Embedded SQLite (single-node mode)

The default. Zero config. A single SQLite file holds all task records, state
transitions, turn logs, and output metadata. Survives process crashes, reboots,
and arbitrary restarts.

`claim()` uses `BEGIN EXCLUSIVE` — two concurrent workers cannot claim the same
task simultaneously. A claimed task automatically reverts to `pending` after a
configurable heartbeat timeout if the worker crashes.

### PostgreSQL (fleet mode — Phase 2)

When multiple nodes claim tasks from the same queue, `SELECT FOR UPDATE SKIP
LOCKED` provides the atomic claim guarantee across concurrent workers. Same
schema, same four operations, different backing store. No code changes in
workers or producers.

---

## saturate.measure

The metric primitive. Runs a shell command, extracts a scalar, classifies
relative to baseline.

```python
measure(
    command="pytest --tb=no -q",
    extract="regex:(\\d+) passed",
    direction="maximize",   # or "minimize"
    baseline=87.0,
    cwd="/path/to/isolated/worktree",
) -> MeasureResult(value=91.0, outcome="improved", ...)
```

Four outcomes:

| Outcome | Meaning | Effect |
|---------|---------|--------|
| `improved` | Moved in correct direction | Commit hypothesis, advance baseline |
| `regressed` | Moved in wrong direction | Revert |
| `crashed` | Command failed or timed out | Revert, **do not update baseline** |
| `unchanged` | Within noise threshold | Revert |

`crashed ≠ regressed` — a crashing hypothesis never pollutes baseline statistics.

The `correctness` spec field provides a second gate: a hypothesis that improves
the metric but fails correctness is rejected. This is the reward-hacking guard.

---

## The Executor

Every loop spec declares how its agent is invoked. The runner knows *when* to
call an agent; the executor knows *how*.

```python
class Executor(Protocol):
    def execute_turn(
        self,
        spec:    LoopSpec,
        state:   dict,
        context: TurnContext,
    ) -> TurnResult: ...
```

`TurnResult` carries the path to a `hypothesis.md` the executor wrote. The
runner reads it, measures, gates on correctness, keeps or reverts.

### Declared in the spec

```yaml
executor:
  type: hermes     # Hermes agent profile
  profile: forge

executor:
  type: shell      # arbitrary executable
  command: ./agents/optimizer.sh

executor:
  type: http       # POST TurnContext JSON → TurnResult JSON
  url: http://localhost:9000/turn
```

### What the executor receives

```python
@dataclass
class TurnContext:
    turn_number:     int
    baseline_metric: float | None   # None on turn 0
    recent_turns:    list[TurnSummary]
    stagnation_n:    int
    state_path:      str   # write hypothesis.md here
    output_path:     str
```

---

## The Loop Runner

Executes one turn of the hypothesis/measure/keep-or-revert cycle. One turn per
invocation — the scheduler re-invokes on the next tick.

```
1.  load task manifest from queue
2.  load_spec(task.spec_path)                  ← ValidationError halts here
3.  clone spec.repo into isolated worktree     ← on first turn; reuse after
4.  load or initialise loop state
5.  executor.execute_turn(spec, state, ctx)    ← agent does the work
6.  measure(spec.evaluate, cwd=work_dir)       ← scalar extraction
7.  correctness gate (spec.correctness)        ← reward-hacking guard
8.  keep (commit to worktree) or revert
9.  write_state(updated)
10. check terminal conditions
    → terminal: complete(task, output)
    → not terminal: requeue for next turn
```

All subprocess calls (eval, correctness, git) run in `work_dir` — never the
Saturate source tree.

---

## SaturateTask

```python
@dataclass
class SaturateTask:
    task_id:           str
    name:              str
    kind:              str
    priority:          int          # 0 highest … 100 lowest
    deadline:          datetime | None
    earliest_start:    datetime | None
    spawned_by:        str | None   # parent task_id; None = root
    depends_on:        list[str]
    num_cpus:          float        # fractional OK
    num_gpus:          float
    estimated_duration_seconds: int
    spec_path:         str
    max_retries:       int
    submitted_by:      str
    submitted_at:      datetime
```

Hierarchy is the dependency graph, not a type distinction. Serial vs parallel
is `depends_on` — no dependencies means freely parallel.

---

## Task Lifecycle

```
PENDING ──── dependency unmet ──► BLOCKED
   │                                  │
   │  all deps done                   │ deps resolved
   ▼                                  ▼
ELIGIBLE ◄─────────────────────────────┘
   │
   │  idle node, runner launched
   ▼
RUNNING ──── heartbeat timeout ──► ELIGIBLE  (crash recovery)
   │
   ├── turn complete, not terminal   ──► ELIGIBLE (requeued)
   ├── complete() called             ──► DONE
   └── crash, max retries exceeded   ──► FAILED
```

---

## Scheduling

```python
def dispatch_score(task: SaturateTask, now: datetime) -> float:
    base    = (100 - task.priority) / 100          # 0.0 .. 1.0
    urgency = 0.0
    if task.deadline and task.estimated_duration_seconds > 0:
        remaining = (task.deadline - now).total_seconds()
        window    = task.estimated_duration_seconds * 2
        urgency   = max(0.0, 1.0 - remaining / window)
    return base + urgency  # higher = dispatch first
```

A P0 task scores 1.0 by priority alone. A background loop approaching its
deadline accrues urgency and competes with P1 work.

---

## The Scheduler (Meta-Loop)

Single process, stateless between ticks. All durable state is in the queue.

```python
def scheduler_tick(queue, fleet, goals_dir):
    # 1. harvest terminal tasks; spawn follow-on loops
    for task in queue.done():
        harvest(task)

    # 2. reclaim stale running tasks
    for task in queue.running():
        if task.heartbeat_expired():
            queue.requeue(task)

    # 3. seed from goals/ if fleet is underutilised
    for spec in seed_goals(goals_dir, queue):
        queue.post(spec)

    # 4. dispatch eligible tasks to idle nodes
    for node in fleet.idle():
        task = next_eligible(queue, node)
        if task:
            node.launch_runner(task)
```

A scheduler restart recovers completely from the queue database.

---

## Swarm Topology (Phase 2)

Serial loops use one worker at a time. Swarm fans out N parallel workers, each
trying a different hypothesis, with a verifier/synthesizer picking the winner.

```
               ┌─ Worker A (hypothesis α) ─┐
Dispatcher ────┼─ Worker B (hypothesis β) ─┼──► Verifier → best result → baseline
               └─ Worker C (hypothesis γ) ─┘
```

Swarm is the serial pattern with `N > 1` at dispatch. The worker skill is
identical across both topologies.

**Saturate swarm primitive:** `BatchKind` tasks fan out to N child
`MetricOptimizationKind` workers via `depends_on` edges and a synthesizer
collects the winner. `BatchKind` and `SpawnPolicy` are Phase 2 design targets —
not yet implemented.

---

## Fleet Management

### Single-node (Phase 1)

Scheduler and runners share one machine. No distributed framework required.

### Multi-node (Phase 2): Nomad

[HashiCorp Nomad](https://www.nomadproject.io) — single binary, Linux/macOS
native, Tailscale-friendly. Handles node registration, fractional CPU/GPU
allocation, hardware routing, and crash detection. Saturate's scheduler submits
jobs to Nomad's HTTP API; PostgreSQL is the state layer, Nomad is the execution
layer.

---

## What Saturate Is Not

| Concern | Owner |
|---------|-------|
| Loop design (goal → typed spec) | Producer (Cyclus, human) |
| Agent framework / LLM orchestration | Worker's concern |
| Inference serving | Node's own runtime |
| Multi-GPU model sharding | Out of scope |
| Kubernetes | Explicitly excluded |
| Business logic DAG (exactly-once) | Temporal, Airflow, Prefect |
| Long-horizon cognitive direction | Continuum |
