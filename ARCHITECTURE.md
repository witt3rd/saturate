# Saturate — Architecture

> **Status:** Design / Active — 2026-07-06

---

## What Saturate Provides

Saturate is an open source **distributed loop execution fabric**. It runs
metric-optimization loops across a heterogeneous fleet of machines and keeps
that fleet continuously saturated with useful autonomous work toward declared
goals.

It provides:

- A **durable work queue** (embedded SQLite, no server required in single-node
  mode) that persists loop state across crashes and restarts
- A **fleet scheduler** that routes loops to idle nodes based on resource
  requirements and priority
- A **loop runner** that executes the hypothesis/measure/keep-or-revert cycle,
  enforces budget controls, and maintains the iteration audit trail
- A **published four-operation API** that any agent framework, CLI, or tool
  can use to submit, claim, and complete loops

Saturate has no dependency on Hermes Agent, Kanban, or any specific agent
framework. It is self-contained.

---

## The Four-Operation Interface

This is Saturate's published contract. Any conforming producer can submit work;
any conforming worker can claim and execute it.

```
post(item)         →  submit a loop to the queue
claim()            →  atomically claim the next available loop (worker side)
write_state(item)  →  record iteration progress to the item's state location
complete(item)     →  mark terminal, attach output and metadata
```

Workers are **external processes** — arbitrary executables, Python scripts,
compiled binaries. They do not import a Saturate SDK. Saturate launches them,
monitors them, and recovers from their crashes.

---

## SaturateTask — The Work Item

Everything in Saturate is a `SaturateTask`. There are no special classes for
"meta-loop" vs "worker" — hierarchy is expressed through the task graph, not
through type distinctions.

```python
@dataclass
class SaturateTask:
    # Identity
    task_id: str                          # UUID
    name: str
    kind: Literal["loop", "batch", "once"]
    #  loop  — metric-driven, iterative (hypothesis → measure → keep/revert)
    #  batch — fan-out to parallel workers, collect and synthesize
    #  once  — single execution to completion

    # Scheduling
    priority: int                         # 0 (highest) to 100 (lowest)
    deadline: Optional[datetime]          # hard deadline; None = best-effort
    earliest_start: Optional[datetime]    # not-before constraint

    # Hierarchy
    spawned_by: Optional[str]             # parent task_id; None = root
    depends_on: List[str]                 # must complete before this starts

    # Resources
    num_cpus: float                       # fractional OK — e.g. 0.5
    num_gpus: float                       # 0 = CPU-only (the common case)
    required_node_class: Optional[str]    # e.g. "GPU_4090", "APPLE_SILICON"
    estimated_duration_seconds: int       # used for deadline urgency scoring

    # Execution
    spec_path: str                        # path to loop spec file (read-only)
    max_retries: int

    # Budget controls (enforced externally by the scheduler)
    max_turns: Optional[int]
    budget_tokens: Optional[int]
    stagnation_n: Optional[int]           # stop after N turns with no improvement

    # State
    state_path: str                       # agreed location for intermediate state
    output_path: str                      # where completed output is written

    # Metadata
    tags: List[str]
    submitted_by: str                     # tool, agent, user, or "system"
    submitted_at: datetime
```

### Self-similar hierarchy

The root scheduler task, a literature-survey loop, and a single
paper-summarization job are all `SaturateTask` instances. The hierarchy is the
dependency graph:

```
root (kind=loop, spawned_by=None)
  ├── literature-sweep (kind=loop, spawned_by=root.task_id)
  │     ├── summarize-001 (kind=once, depends_on=[])
  │     ├── summarize-002 (kind=once, depends_on=[])
  │     └── synthesize   (kind=once, depends_on=[summarize-001, summarize-002])
  ├── build-optimizer (kind=loop, spawned_by=root.task_id)
  └── code-quality    (kind=batch, spawned_by=root.task_id)
```

**Serial vs parallel is `depends_on`, not a type distinction.** No dependencies
= freely parallel. The scheduler bins-packs accordingly.

---

## The Durable Queue

Saturate owns its own durable state. It does not depend on any external
queueing system.

### Embedded SQLite (single-node mode)

The default. No server process. A single SQLite file holds all task records,
state transitions, iteration logs, and output metadata. Survives process crashes,
reboots, and arbitrary restarts. The scheduler rebuilds its in-memory registry
from the database on startup.

```
~/.saturate/queue.db       # task records, lifecycle state, spawn graph
~/.saturate/state/         # per-task intermediate state (written by runners)
~/.saturate/output/        # completed task output
```

### PostgreSQL (fleet mode)

When multiple nodes need to claim tasks from the same queue, SQLite's
file-locking model is insufficient. PostgreSQL provides the atomic claim
operation (SELECT FOR UPDATE SKIP LOCKED) that makes concurrent multi-node
workers safe. The same schema, same four operations, different backing store.
Configuration switches between them — no code changes in workers or producers.

### The atomic claim guarantee

`claim()` is implemented as an atomic row update: a task moves from `ELIGIBLE`
to `RUNNING` in a single transaction. Two workers on different nodes cannot
claim the same task simultaneously. A claimed task automatically reverts to
`ELIGIBLE` after a configurable heartbeat timeout if the worker crashes without
completing — no manual intervention required.

---

## The Loop Spec Format

The loop spec is Saturate's native work item format for `kind=loop` tasks.
It is a plain YAML file — any tool can produce one.

```yaml
name:             build-optimizer
goal:             Reduce CI build time by at least 20%
metric:
  command:        npm run build
  extract:        wall_clock        # wall_clock | regex:<pattern> | json:<key>
  direction:      minimize          # minimize | maximize
  baseline:       null              # measured on first turn if null
correctness:
  command:        npm test          # must pass after every accepted hypothesis
max_turns:        100
budget_tokens:    500000
stagnation_n:     10
terminal_states:  [success, stalled, exhausted, blocked]
memory:           ./output/build-optimizer/
spawn:            null              # optional: conditions for child loop creation
```

Saturate treats the spec as **read-only** during execution. Producers own the
spec; Saturate runs it.

---

## omh_measure — The Metric Primitive

`omh_measure` runs a command and returns a scalar. It is the only Saturate
primitive that is also independently useful as an OMH tool.

```python
omh_measure(
    command="npm run build",
    extract="wall_clock",
    direction="minimize",
    runs=1,                     # N-run averaging for noisy metrics
) -> MeasureResult(
    value=19.1,
    unit="s",
    status="ok" | "crash" | "timeout",
    raw="<captured stdout/stderr>",
)
```

Four outcomes, relative to current baseline:

| Outcome | Meaning | Action |
|---|---|---|
| `improved` | Metric moved in correct direction | Commit hypothesis, advance baseline |
| `regressed` | Metric moved in wrong direction | Revert |
| `crashed` | Command failed or timed out | Revert, do not update baseline |
| `unchanged` | Within noise threshold | Revert |

`crashed ≠ regressed` — a crashing hypothesis does not pollute the baseline
statistics. The correctness gate (`spec.correctness`) must pass in addition to
metric improvement — this is the reward-hacking guard.

---

## The Loop Runner

A loop runner executes one turn of the hypothesis/measure/keep-or-revert cycle
for a `kind=loop` task. Runners are external processes — Saturate launches
them, they are not embedded in the scheduler.

```
runner receives: task_id, spec_path, state_path, output_path
runner does:
  1. load spec
  2. load current state (baseline metric, turn count, stagnation count)
  3. generate hypothesis   ← calls external agent/API
  4. apply tentatively     ← modifies files, builds, etc.
  5. measure               ← omh_measure → scalar
  6. correctness gate      ← run spec.correctness command
  7. keep or revert
  8. write_state(updated state)
  9. check terminal conditions
  10. if terminal: complete(task, output)
      else:        exit cleanly (scheduler re-invokes on next tick)
```

The runner executes **one turn per invocation** and exits. The scheduler
re-invokes it on the next tick. This is how budget controls are enforced
externally — the scheduler checks `max_turns`, `stagnation_n`, and
`budget_tokens` before re-invoking. The runner never implements its own while-loop.

---

## Scheduling: Priority and Deadline Scoring

```python
def dispatch_score(task: SaturateTask, now: datetime) -> float:
    base = (100 - task.priority) / 100      # 0.0 .. 1.0

    urgency = 0.0
    if task.deadline and task.estimated_duration_seconds > 0:
        remaining = (task.deadline - now).total_seconds()
        window = task.estimated_duration_seconds * 2
        urgency = max(0.0, 1.0 - (remaining / window))  # 0.0 .. 1.0

    return base + urgency  # higher = dispatch first
```

A P0 task with no deadline scores 1.0 and dispatches first by priority alone.
A background P3 loop approaching its deadline accrues urgency score and begins
competing with P1 work. A task at `remaining == estimated_duration_seconds`
has urgency 0.5, regardless of its base priority.

---

## Task Lifecycle

```
SUBMITTED
    │
    ▼
PENDING ──── dependency unmet ──► BLOCKED
    │                                 │
    │  all deps COMPLETE              │ deps resolved
    ▼                                 ▼
ELIGIBLE ◄────────────────────────────┘
    │
    │  idle node matched, runner launched
    ▼
RUNNING ──── heartbeat timeout ──► ELIGIBLE  (crash recovery)
    │
    ├── runner exits cleanly (turn complete, not terminal)
    │         │
    │         └──► ELIGIBLE  (re-queued for next turn)
    │
    ├── runner calls complete()
    │         │
    │         └──► COMPLETE
    │
    └── runner crashes (no heartbeat within timeout)
              │
              └──► RETRYING ──► ELIGIBLE
                       │
                       │  max_retries exceeded
                       └──► FAILED
```

---

## The Scheduler (Meta-Loop)

The scheduler is a single process on the head node. It polls the queue on a
configurable interval (default: 30s) and drives the fleet.

```python
def scheduler_tick(queue: Queue, fleet: Fleet, goals: GoalDirectory):
    # 1. harvest complete and failed tasks
    for task in queue.terminal():
        harvest(task)
        if task.should_spawn():
            for child_spec in task.next_loops():
                queue.post(child_spec, spawned_by=task.task_id)

    # 2. reclaim crashed runners
    for task in queue.running():
        if task.heartbeat_expired():
            queue.requeue(task)

    # 3. seed from goal directory if fleet is underutilized
    if fleet.underutilized():
        for spec in goals.next_pending():
            queue.post(spec)

    # 4. dispatch eligible tasks to idle nodes
    eligible = sorted(queue.eligible(), key=dispatch_score, reverse=True)
    for node in fleet.idle_nodes():
        for task in eligible:
            if node.fits(task):
                node.launch_runner(task)
                queue.mark_running(task, node)
                eligible.remove(task)
                break
```

The scheduler is **stateless between ticks** — all durable state is in the
queue database. A scheduler restart recovers completely from the database.

---

## Fleet Management

### Single-node (Phase 1)

The scheduler and all runners share one machine. `fleet.idle_nodes()` returns
`[localhost]` with the current CPU/memory headroom. No distributed framework
required.

### Multi-node (Phase 2+): Nomad

[HashiCorp Nomad](https://www.nomadproject.io) is the fleet layer. A single
binary, no Kubernetes, Linux and macOS native, Tailscale-friendly. Nomad
handles:

- Node registration and resource advertisement
- Fractional CPU/GPU allocation per job
- Custom capability tags for hardware routing
- Worker process lifecycle (launch, monitor, kill)
- Crash detection and job failure signaling

Saturate's scheduler submits jobs to Nomad's HTTP API; Nomad places them on
the right node and reports back. The queue database (PostgreSQL in fleet mode)
is the source of truth for task state — Nomad is the execution layer, not the
state layer.

```
Saturate scheduler
  │  reads queue state      PostgreSQL queue.db
  │  submits Nomad jobs ──► Nomad server
  │                              │
  │                         Nomad agents on each node
  │                              │  launch runners
  │                              └─ DGX Spark, RTX WS, MacPro, MacBook, ...
  │  receives job events ◄──────────────────────────────────────────────┘
  └  updates queue state
```

### Node capability tags (examples)

```hcl
# In each node's Nomad client config
meta {
  gpu_class    = "RTX_4090"
  has_gpu      = "true"
  node_class   = "LINUX_WS"
}
```

Saturate loop specs declare `required_node_class` if they need specific
hardware. Most loops leave it null — CPU + network is sufficient.

---

## The Goal Directory

Active loop specs live as files in a watched directory:

```
goals/
├── index.yaml            # priority ordering, active/paused flags
├── build-optimizer.yaml  # loop spec
├── lit-survey-ml.yaml
└── code-quality.yaml
```

Adding a goal = drop a spec file. Pausing = edit `index.yaml`. The scheduler
reads the directory on each tick. No runtime API needed in v1.

---

## Observability

- **`saturate status`** — CLI: active loops, current metric values, turns to
  date, stagnation counters, node utilization
- **Queue database** — full audit trail queryable with any SQLite/Postgres
  client: every task's lifecycle events, every iteration's hypothesis and
  outcome, spawn graph
- **Prometheus endpoint** — `/metrics` exposes queue depth by priority tier,
  active tasks per node, completed/failed task counts, loop metric histories
- **Nomad Dashboard** (Phase 2) — per-node utilization, job placements, failure
  history

---

## What Is Not Saturate

| Concern | Owner |
|---|---|
| Designing loops (goal → verifiable spec) | Calling tool (e.g. oh-my-hermes) |
| Agent framework / LLM orchestration | Worker's concern; Saturate is agnostic |
| Inference serving | The node's own runtime (vLLM, llama.cpp, API call) |
| Multi-GPU model sharding | Out of scope — loops run on one node |
| Kubernetes / container orchestration | Explicitly excluded |
| Business logic workflow (DAG, exactly-once) | Temporal, Airflow, Prefect |
| Cognitive fleet direction (long-horizon planning) | Out of scope — Saturate executes declared goals |

---

## Implementation Roadmap

### Phase 1 — Single-Node Loop Runner
- Embedded SQLite queue with four-operation interface
- `SaturateTask` dataclass + queue CRUD
- `omh_measure`: wall_clock, regex, json extraction; four-outcome classification
- Loop runner: one turn per invocation, hypothesis/measure/keep-or-revert
- Scheduler tick: idle detection, dispatch, harvest, crash recovery
- Goal directory: file-drop interface
- `saturate submit` / `saturate status` CLI
- Full loop lifecycle end-to-end on one machine

### Phase 2 — Multi-Node Fleet
- PostgreSQL queue backend (atomic claim via SELECT FOR UPDATE SKIP LOCKED)
- Nomad integration: job submission, resource tagging, crash events
- Per-node resource advertisement and `required_node_class` routing
- Priority + deadline dispatch scoring
- Fractional CPU allocation: multiple loops per node
- `saturate status --fleet` showing all nodes

### Phase 3 — Loop Ecosystem
- Child loop spawning from a running runner
- Spawn graph querying and visualization
- Prometheus metrics endpoint
- Stagnation detector + configurable terminal-state handling
- `batch` kind: fan-out workers, collect-and-synthesize
- HTTP API (as alternative to CLI for programmatic producers)

### Phase 4 — Hardening
- Multi-node integration test suite
- Preemption: P0 task arrival preempts running P3 loops
- Budget controls at the fleet level (total token spend across all active loops)
- Documentation site
