# Saturate — Architecture

> **Status:** Design / Active — 2026-07-06
> **Companion:** [oh-my-hermes/docs/v18/architecture.md](https://github.com/witt3rd/oh-my-hermes/blob/main/docs/v18/architecture.md)

---

## Position in the Arc

Saturate is the **Tier 3 execution backend** for
[oh-my-hermes](https://github.com/witt3rd/oh-my-hermes).

```
Tier 1  in-turn deliberation       ralplan, deep-interview    delegate_task
Tier 2  durable single-machine     ralph, autopilot           Kanban (Hermes v0.18)
Tier 3  distributed loop fabric    autoresearch, loop goals   Saturate  ←  this
```

It implements the **four-operation work-queue interface** that OMH targets:

```
post(item)         →  submit a work item to the queue
claim()            →  a worker claims the next available item
write_state(item)  →  update the item's running state
complete(item)     →  mark done, attach output/metadata
```

Workers (Ray Actors) see only these four operations. They do not know whether
the backing store is Kanban, Saturate, or a file. That is the whole interface.

**OMH designs work. Saturate runs it. The handoff is a file.**

---

## The Unit of Work: SaturateTask

Everything in Saturate is a `SaturateTask`. There are no special classes for
"meta-loop" vs "worker loop" — the hierarchy is expressed through the task
graph, not through type distinctions.

```python
@dataclass
class SaturateTask:
    # Identity
    task_id: str                         # UUID
    name: str                            # human-readable label
    kind: Literal["loop", "batch", "once"]
    #   loop  — metric-driven, iterative (hypothesis → measure → keep/revert)
    #   batch — fan-out to parallel sub-tasks, collect and synthesize
    #   once  — single execution to completion

    # Scheduling
    priority: int                        # 0 (highest) to 100 (lowest)
    deadline: Optional[datetime]         # hard deadline; None = best-effort
    earliest_start: Optional[datetime]   # not-before constraint

    # Hierarchy  (self-similar — a task spawns tasks)
    spawned_by: Optional[str]            # parent task_id; None = root
    depends_on: List[str]                # task_ids that must complete first

    # Resources
    num_cpus: float                      # fractional OK, e.g. 0.5
    num_gpus: float                      # 0 = CPU-only (the common case)
    required_node_class: Optional[str]   # e.g. "DGX_SPARK", "APPLE_SILICON"
    estimated_duration_seconds: int      # used for deadline urgency scoring

    # Execution
    spec_path: str                       # path to <name>-loop.md or spec file
    max_retries: int                     # automatic retry on crash

    # Budget controls (enforced by the runner)
    max_turns: Optional[int]             # hard iteration ceiling
    budget_tokens: Optional[int]         # token/cost ceiling
    stagnation_n: Optional[int]          # stop after N turns with no improvement

    # State
    state_path: str                      # agreed location for intermediate state
    output_path: str                     # where completed output is written
    kanban_task_id: Optional[str]        # backing Kanban row (when Kanban is live)

    # Metadata
    tags: List[str]
    submitted_by: str                    # agent name, user, or system
    submitted_at: datetime
```

### Self-similar hierarchy

The meta-loop is a SaturateTask with `kind="loop"` and `spawned_by=None`. It
runs until cancelled. It submits child tasks via `post()`. Children submit
grandchildren the same way. The spawn tree is the task graph — queryable,
auditable, bounded by budget controls at every level.

```
root (kind=loop, spawned_by=None)
  ├── literature-sweep (kind=loop, spawned_by=root.task_id)
  │     ├── paper-summarize-001 (kind=once, depends_on=[])
  │     ├── paper-summarize-002 (kind=once, depends_on=[])
  │     └── synthesis (kind=once, depends_on=[summarize-001, summarize-002])
  ├── build-optimizer (kind=loop, spawned_by=root.task_id)
  └── code-synthesis (kind=batch, spawned_by=root.task_id)
```

**Serial and parallel are scheduling properties, not architectural categories.**
`depends_on` expresses serial constraints. No `depends_on` = freely parallel.
The scheduler bins them accordingly — this is the whole of "serial vs parallel"
in the architecture.

---

## Scheduling: Priority and Deadline Scoring

The scheduler ranks dispatchable tasks by a score combining base priority and
deadline urgency:

```python
def dispatch_score(task: SaturateTask, now: datetime) -> float:
    # base: lower priority number = higher urgency
    base = (100 - task.priority) / 100  # 0.0 .. 1.0

    urgency = 0.0
    if task.deadline and task.estimated_duration_seconds > 0:
        remaining = (task.deadline - now).total_seconds()
        window = task.estimated_duration_seconds * 2
        urgency = max(0.0, 1.0 - (remaining / window))  # 0.0 .. 1.0

    return base + urgency  # higher = dispatch first
```

A task approaching its deadline relative to its estimated duration accrues
urgency on top of its base priority. A P0 task with no deadline scores 1.0
base and dispatches first by pure priority. A P2 background loop approaching
its deadline begins to compete with P1 foreground work.

### Dispatchability

A task is dispatchable when:
1. All `depends_on` task_ids are in `COMPLETE` state
2. `earliest_start` has passed (or is None)
3. A node with sufficient `num_cpus`, `num_gpus`, and `required_node_class`
   is idle

---

## Task Lifecycle

```
SUBMITTED
    │
    ▼
PENDING ──── dependency unmet ──► BLOCKED
    │                                 │
    │  deps resolved                  │ deps resolved
    ▼                                 ▼
ELIGIBLE ◄────────────────────────────┘
    │
    │  idle node matched + Actor spawned
    ▼
RUNNING
    │               │
    │  success      │  crash (retriable)
    ▼               ▼
COMPLETE        RETRYING ──► RUNNING
                    │
                    │  max_retries exceeded
                    ▼
                  FAILED
```

Terminal states for `kind=loop`:

| State | Meaning |
|---|---|
| `success` | Optional target threshold was hit |
| `stalled` | `stagnation_n` consecutive turns with no accepted improvement |
| `exhausted` | `max_turns` or `budget_tokens` ceiling reached |
| `blocked` | Runner encountered an unresolvable dependency |
| `cancelled` | Human stopped it via `kanban_block` → cancel |

---

## The Loop Runner (kind=loop)

A Ray Actor. One instance per active loop task. Owns the
hypothesis → apply → measure → keep/revert cycle:

```python
@ray.remote(num_cpus=1)
class LoopActor:
    def __init__(self, task: SaturateTask): ...

    def run_turn(self) -> TurnResult:
        spec = load_spec(self.task.spec_path)
        h = self.generate_hypothesis(spec)   # delegate_task → optimizer role
        self.apply_tentatively(h)            # git stash / temp branch
        m = omh_measure(spec.metric)         # → MeasureResult
        ok = self.correctness_gate(spec)     # → bool
        if m.improved and ok:
            self.commit(h)
            return TurnResult.ACCEPTED
        else:
            self.revert()
            return TurnResult.DISCARDED

    def should_stop(self) -> Optional[StopReason]:
        if self.turns >= self.task.max_turns: return StopReason.EXHAUSTED
        if self.stagnant >= self.task.stagnation_n: return StopReason.STALLED
        return None
```

The actor does not drive its own loop. It executes one turn when the scheduler
invokes it, then reports back. The scheduler decides whether to invoke again —
this is how `max_turns` and budget controls are enforced externally rather than
inside the actor.

---

## omh_measure — the Metric Primitive

Returns a scalar from a command. Not a pass/fail. This is the primitive that
makes non-boolean loops possible.

```python
omh_measure(
    command="npm run build",
    extract="wall_clock",              # wall_clock | regex:<pattern> | json:<key>
    direction="minimize",
    runs=1,                            # N-run averaging for noisy metrics
) -> MeasureResult(
    value=19.1,
    unit="s",
    status="ok" | "crash" | "timeout",
    raw="<captured stdout/stderr>",
)
```

Outcome classification (relative to current baseline):

| Status | Meaning |
|---|---|
| `improved` | Metric moved in the right direction |
| `regressed` | Metric moved in the wrong direction |
| `crashed` | Command exited non-zero or timed out |
| `unchanged` | Metric within noise threshold |

`crashed` ≠ `regressed`. A hypothesis that crashes is discarded without
updating the baseline — it is not treated as "infinitely bad," which would
corrupt running statistics.

Correctness gate: every accepted improvement must also pass the correctness
command (`spec.correctness`) before it is committed. Metric-better + tests-broken
= discard. This is the reward-hacking guard.

---

## The Meta-Loop

The root SaturateTask. `kind=loop`, `spawned_by=None`. Runs as a persistent
Ray Actor on the head node. Its job is fleet orchestration, not hypothesis
generation.

```python
def meta_loop_tick(registry: TaskRegistry, fleet: Fleet, goals: GoalRegistry):
    # 1. harvest completed and failed tasks
    for task in registry.terminal():
        harvest(task)                              # collect output to output_path
        if should_spawn(task):
            for child_spec in next_loops(task):
                registry.post(child_spec, spawned_by=task.task_id)

    # 2. reap stalled tasks
    for task in registry.running():
        if task.actor.should_stop():
            registry.complete(task, reason=task.actor.stop_reason())

    # 3. dispatch eligible tasks to idle nodes
    eligible = sorted(registry.eligible(), key=dispatch_score, reverse=True)
    for node in fleet.idle_nodes():
        for task in eligible:
            if node.fits(task):
                actor = LoopActor.options(
                    num_cpus=task.num_cpus,
                    num_gpus=task.num_gpus,
                    resources={task.required_node_class: 1} if task.required_node_class else {},
                ).remote(task)
                registry.mark_running(task, node, actor)
                eligible.remove(task)
                break

    # 4. seed from goal registry if fleet is underutilized
    if fleet.underutilized():
        for spec in goals.next_pending():
            registry.post(spec)
```

The meta-loop itself is stateless between ticks. All durable state is in Kanban.
A restart rebuilds the in-memory registry from `kanban_list`.

---

## Kanban as State Substrate

Kanban (Hermes v0.18.0) is the durable backing store for task state. It
implements the four-operation interface natively.

| Interface operation | Kanban call |
|---|---|
| `post(task)` | `kanban_create(task_id, spec_path, metadata={priority, deadline, ...})` |
| `claim()` | `kanban_next()` — atomic, crash-safe claim |
| `write_state(task, state)` | `kanban_comment(task_id, state)` — iteration log |
| `complete(task, output)` | `kanban_complete(task_id, metadata={metric, turns, output_path, reason})` |

Spawn relationships: child tasks are Kanban sub-tasks linked to their parent.
The full spawn tree is the Kanban task graph — queryable via `kanban_list`.

---

## The Goal Registry

Active objectives live as loop spec files committed to the repo:

```
goals/
├── index.yaml              priority ordering, active/paused status
├── build-optimizer.md      <name>-loop.md spec
├── literature-sweep.md
└── code-quality.md
```

Adding a goal = commit a `<name>-loop.md`. Pausing = edit `index.yaml`.
No runtime API required in v1. The meta-loop reads the registry on each tick.

### The Loop Spec Format

Produced by `omh-loop-design` deliberation. Read-only to Saturate.

```yaml
goal:             what the loop is trying to achieve
metric:           the scalar to optimize (command + extraction mode)
direction:        minimize | maximize
correctness:      command that must still pass after every accepted hypothesis
max_turns:        hard iteration ceiling
budget_tokens:    token/cost ceiling
stagnation_n:     stop after N turns with no accepted improvement
terminal_states:  [success, stalled, exhausted, blocked]
driver:           cron | kanban-goal | saturate-actor
memory:           where outputs and findings are written
spawn:            conditions under which child loops are created (optional)
```

---

## Ray Integration

Each SaturateTask runs as a Ray Actor. The fleet is one Ray cluster across all
nodes connected via Tailscale.

```python
# Node resource tags — used for required_node_class routing
# Each node joins with its custom resource advertised:
ray start --resources='{"DGX_SPARK": 1, "num_gpus": 1}'
ray start --resources='{"APPLE_SILICON": 1}'
ray start --resources='{"LINUX_WS_4090": 1, "num_gpus": 1}'
```

Fractional CPU allocation: `num_cpus=0.5` allows multiple lightweight loops
to share a node. A CPU-only node with 16 cores might run 8 concurrent loops
at `num_cpus=2` each. The DGX Spark might run 4 CPU loops at `num_cpus=4`
alongside inference work.

GPU use is opportunistic, not pooled. A loop that needs local inference
routes to a node that has a GPU via `required_node_class`. Saturate does not
pool or shard GPU capacity across nodes for a single task — loops run on
whatever single node fits their resource profile.

---

## Idle Detection

```python
import psutil

def node_is_idle(thresholds: NodeThresholds) -> bool:
    cpu = psutil.cpu_percent(interval=1)
    if cpu > thresholds.cpu_pct:          # e.g. 40%
        return False
    mem = psutil.virtual_memory().percent
    if mem > thresholds.mem_pct:          # e.g. 80%
        return False
    return True
```

GPU nodes additionally check DCGM metrics. A node running a P0 interactive
session is not idle regardless of CPU utilization. Thresholds are
per-node-class and tunable via config.

---

## Fleet

| Node | OS | CPU | GPU | Role |
|---|---|---|---|---|
| DGX Spark (GB10) | Linux | ARM | 128 GB unified (Blackwell) | Heavy loops + local inference |
| Linux Workstation × 2 | Linux | x86 | RTX 4090, RTX 3090 | CPU loops + GPU inference |
| MacBook Pro | macOS | Apple Silicon | MPS | CPU loops + dev |
| Mac Pro | macOS | Intel/ARM | AMD | Background CPU loops |
| Linux Laptop | Linux | x86 | iGPU | CPU loops |

All nodes on Tailscale mesh. Windows nodes deferred.

---

## Monitoring

- **Ray Dashboard** (port 8265) — per-node utilization, actor state, task
  throughput, object store across the fleet
- **Kanban audit trail** — every loop's full turn-by-turn history: accepted
  hypotheses, discarded ones, final terminal state and reason, spawn graph
- **`saturate status`** — human-readable fleet view: active loops, current
  metric values, turns to date, stagnation counters

---

## What Is Not Saturate

| Concern | Owner |
|---|---|
| Loop deliberation (goal → verifiable spec) | OMH (`omh-loop-design`) |
| Metric-optimization loop skill | OMH (`omh-autoresearch`) |
| Inference serving | vLLM / llama.cpp on the node |
| Multi-GPU model sharding | Out of scope — Saturate routes loops to nodes, not GPU capacity across nodes |
| Cognitive fleet direction (what to spawn next, long-horizon goals) | Continuum |
| Kubernetes / Kueue / Temporal | Out of scope — Kanban + Ray is the stack |

---

## Implementation Roadmap

### Phase 1 — Single-Node Loop Runner (Week 1–2)
- `omh_measure` primitive: wall_clock, regex, json extraction modes
- `LoopActor`: hypothesis → measure → keep/revert cycle
- Kanban integration: four-operation interface over Kanban
- Single loop, single node, full lifecycle end-to-end

### Phase 2 — Task Graph + Meta-Loop (Week 3–4)
- `SaturateTask` dataclass + registry
- Meta-loop tick: idle detection, dispatch, harvest, reaping
- Goal registry: `goals/` directory + `index.yaml`
- Child task spawning: `post()` from a running actor
- `saturate status` CLI

### Phase 3 — Fleet (Week 5–6)
- Ray cluster across all nodes
- Per-node resource tagging + `required_node_class` routing
- Fractional CPU: multiple loops per node
- Priority + deadline scoring in dispatch
- Stagnation detection + clean terminal-state handling

### Phase 4 — OMH Integration (Week 7–8)
- `omh-loop-design` skill producing Saturate-compatible specs
- `omh-autoresearch` skill as first Tier 3 consumer
- End-to-end: design a loop in OMH → commit spec → Saturate runs it overnight
- Audit trail review workflow + `kanban_list` sprint review
