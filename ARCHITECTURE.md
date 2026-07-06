# Saturate — Architecture

> **Status:** Design / Early Draft — 2026-07-06

---

## Overview

Saturate is a distributed **loop execution fabric**: a system that keeps a
heterogeneous fleet of CPU-capable machines continuously running useful
agentic loops toward declared goals.

The key architectural decisions in one sentence each:

- **The unit of work is a loop, not a task.** Loops are self-contained,
  iterating, measuring, rolling back, and running until stopped.
- **GPU distribution is out of scope.** Individual loops run on whatever node
  fits; we route work to available capacity, not pool capacity for one
  workload.
- **The substrate is CPU.** Inference is an API call or a local model call;
  orchestration — the part that starves — is pure Python/CPU.
- **Ray is the execution runtime.** One Actor per loop, scheduled across nodes,
  with the fleet appearing as a single resource pool.
- **Kanban is the state substrate.** Loop lifecycle, parent/child graph, audit
  trail — all in the durable Kanban board.
- **OMH produces loop specs; Saturate runs them.** Deliberation and execution
  are separated by a `<name>-loop.md` file.

---

## The Fleet

| Node | OS | CPU | GPU | Role |
|---|---|---|---|---|
| DGX Spark (GB10) | Linux | ARM | 128 GB unified (Blackwell) | Heavy loops + local inference |
| Linux Workstation × 2 | Linux | x86 | RTX 4090, RTX 3090 | CPU loops + GPU inference when needed |
| MacBook Pro | macOS | Apple Silicon | MPS | CPU loops + dev |
| Mac Pro | macOS | Intel/ARM | AMD | Background CPU loops |
| Linux Laptop | Linux | x86 | iGPU | CPU loops |

All nodes connected via **Tailscale** mesh. Windows/Surface nodes deferred.

GPU use in Saturate is opportunistic: a loop that needs local model inference
routes to a node that has one. Saturate does not pool or split GPU capacity
across nodes for a single workload.

---

## Core Components

### 1. Loop Spec — `<name>-loop.md`

The interface between OMH and Saturate. A structured document produced by the
`omh-loop-design` deliberation skill. Saturate treats it as a read-only
specification — it never modifies it.

Required fields:
```
goal:          what the loop is trying to achieve
metric:        the scalar to measure (command + extraction)
direction:     minimize | maximize
correctness:   the command that must still pass after every hypothesis
max_turns:     hard iteration ceiling
budget:        token/cost ceiling
stagnation:    stop after N iterations with no accepted improvement
terminal_states: success | stalled | exhausted | blocked
driver:        cron | kanban-goal
memory:        where outputs/findings are written
```

A loop spec without all of these is not schedulable. The `omh-loop-design`
deliberation enforces this before handing off.

---

### 2. Loop Runner — `saturate/runner.py`

A Ray Actor. One instance per active loop. Owns the hypothesis/measure/keep-or-
revert cycle.

```python
@ray.remote(num_cpus=1)
class LoopRunner:
    def __init__(self, spec: LoopSpec, kanban_task_id: str): ...

    def run_iteration(self) -> IterationResult:
        hypothesis = self.generate()       # delegate_task → optimizer role
        self.apply_tentatively(hypothesis) # git stash / temp branch
        metric = self.measure()            # omh_measure → scalar
        correct = self.gate()              # correctness command → bool
        if metric.improved and correct:
            self.commit(hypothesis)
            return IterationResult.ACCEPTED
        else:
            self.revert()
            return IterationResult.DISCARDED

    def should_stop(self) -> StopReason | None: ...
```

The runner does not own its own scheduling. It executes one iteration when
invoked, then reports back to the meta-loop. The meta-loop decides whether to
invoke again.

---

### 3. omh_measure — `saturate/measure.py`

The metric primitive. Runs a command, extracts a scalar, classifies the result.

```python
omh_measure(
    command="npm run build",
    extract="duration_seconds",   # wall_clock | regex:<pattern> | json:<key>
    direction="minimize",
    runs=1,                        # N-run averaging for noisy metrics
) -> MeasureResult(value=19.1, unit="s", status="ok" | "crash" | "timeout")
```

Returns one of four statuses: `improved`, `regressed`, `crashed`, `unchanged`.
Only `improved` (relative to the current baseline) causes a hypothesis to be
kept. `crashed` is not treated as `regressed` — it is a separate outcome so
the loop can distinguish "got worse" from "broke entirely."

---

### 4. Loop Registry — `saturate/registry.py`

The in-memory view of all active loops: what is running, on which node, at
what iteration, with what baseline metric. Backed by Kanban for durability —
the in-memory registry is rebuilt from Kanban rows on restart.

Each loop is one Kanban task. Child loops are sub-tasks. The parent/child
relationship is a first-class field, not a naming convention — so the full
spawn graph is queryable.

---

### 5. Meta-Loop — `saturate/meta.py`

The orchestrator. Runs on the head node. Surveys the fleet every
`POLL_INTERVAL` seconds (default: 30).

```python
def meta_loop_tick(registry: LoopRegistry, fleet: Fleet):
    # 1. reap completed or stalled loops
    for loop in registry.active():
        result = loop.poll()
        if result.terminal:
            harvest(loop, result)
            if result.should_spawn:
                registry.enqueue(result.spawn_specs)

    # 2. dispatch pending loops to idle nodes
    idle = fleet.idle_nodes()            # CPU utilization below threshold
    pending = registry.pending()
    for node, spec in match(idle, pending):
        runner = LoopRunner.options(
            resources={node.resource_key: 1}
        ).remote(spec, kanban_task_id=spec.task_id)
        registry.mark_running(spec, node, runner)
```

The meta-loop itself is stateless between ticks — all durable state lives in
Kanban. A meta-loop restart picks up exactly where it left off.

---

### 6. Goal Registry — `saturate/goals.py`

The set of active objectives. Stored as `goals/<name>-loop.md` specs in the
repo, plus a `goals/index.yaml` that declares priority ordering and
active/paused status.

The meta-loop reads the goal registry on each tick. Adding a new goal is a
file commit. Pausing a goal is a YAML edit. No runtime API needed for goal
management in v1.

---

## Loop Lifecycle

```
                    ┌─────────────────────┐
                    │   omh-loop-design   │  ← human + OMH deliberation
                    └──────────┬──────────┘
                               │  <name>-loop.md
                    ┌──────────▼──────────┐
                    │   goals/ directory  │  pending
                    └──────────┬──────────┘
                               │  meta-loop picks up
                    ┌──────────▼──────────┐
                    │    LoopRunner       │  running on a node
                    │  (Ray Actor)        │
                    │                     │
                    │  hypothesis →       │
                    │  apply →            │
                    │  measure →          │
                    │  keep/revert        │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼─────────────────┐
              ▼                ▼                  ▼
          ACCEPTED          DISCARDED           TERMINAL
         (commit +        (revert +          (stalled /
          continue)        continue)         exhausted /
                                             success)
                                                 │
                                    ┌────────────┼───────────┐
                                    ▼            ▼           ▼
                                harvest       record      spawn
                                output       dead end    children?
```

Terminal states — every loop must name them before it is schedulable:

| State | Meaning |
|---|---|
| `success` | Optional target threshold hit |
| `stalled` | N consecutive iterations with no accepted improvement |
| `exhausted` | Hard iteration cap or budget ceiling reached |
| `blocked` | Loop encountered a dependency it cannot resolve |
| `cancelled` | Human stopped it via `kanban_block` → cancel |

---

## Kanban as State Substrate

Every loop is a Kanban task. This is not a metaphor — the durable SQLite
Kanban board (`~/.hermes/kanban.db`) is the single source of truth for loop
state.

| Loop concept | Kanban equivalent |
|---|---|
| Loop in queue | task in `todo` |
| Loop running | task in `running` |
| Loop blocked | `kanban_block` |
| Loop complete | `kanban_complete(metadata={metric, iterations, output_path})` |
| Child loop | sub-task linked to parent task |
| Iteration log | `kanban_comment` thread on the task |
| Spawn graph | parent/child task relationships |

A meta-loop restart reads `kanban_list` and rebuilds the in-memory registry.
No sidecar files, no bespoke lock management — the same substrate OMH v18
re-grounds onto.

---

## Node Idle Detection

Idle detection is intentionally simple in v1:

```python
import psutil
import subprocess

def is_idle(node: Node, cpu_threshold=30.0) -> bool:
    usage = psutil.cpu_percent(interval=1)
    return usage < cpu_threshold
```

For GPU nodes, NVIDIA DCGM exports per-GPU compute utilization via its metrics
endpoint. A node is considered idle for loop dispatch if CPU is below threshold
AND any running GPU work is P0 (user-facing) — we do not preempt inference that
is actively serving.

Preemption in v1 is limited: a P0 task arriving while a background loop is
running causes the loop to checkpoint (commit current baseline, record state to
Kanban) and yield the node. Full preemption mechanics are deferred.

---

## Ray Integration

Each LoopRunner is a Ray Actor. The meta-loop runs as a long-lived Ray Actor
on the head node. Ray handles:

- Scheduling actors onto nodes with available resources
- Custom resource routing (`{"DGX_SPARK": 1}`, `{"APPLE_SILICON": 1}`)
- Crash detection and restart
- Cross-node object transfer (loop specs, measurement results)

Ray's fractional CPU resources (`num_cpus=0.5`) allow multiple lightweight
loops to share a node. A Surface-class node might run 4 concurrent light loops
at 25% CPU each; the DGX Spark might run 2 heavier loops at 50% each alongside
its inference load.

---

## Monitoring

- **Ray Dashboard** (port 8265) — per-node utilization, actor state,
  task throughput across the fleet
- **Kanban audit trail** — every loop's full iteration history, accepted and
  discarded hypotheses, terminal state and reason
- **`goals/index.yaml` + `saturate status`** — human-readable fleet status:
  active loops, their current metrics, iterations to date

---

## OMH Integration

Saturate does not implement deliberation. It does not design goals, challenge
verification strategies, or check for reward-hacking risk. That is OMH's job.

The contract:

1. Human runs `omh-loop-design` (OMH deliberation skill)
2. OMH produces `goals/<name>-loop.md`
3. Human reviews and commits the spec
4. Meta-loop picks it up on the next tick

Saturate treats loop specs as immutable inputs. If a spec needs revision, the
human modifies it via OMH and re-commits. Saturate never writes back to a
loop spec.

---

## Implementation Roadmap

### Phase 1 — Substrate (Week 1–2)
- Ray cluster on DGX Spark + Linux workstations
- Tailscale mesh validated for Ray object store
- `omh_measure` implemented and tested
- Basic `LoopRunner` Actor: hypothesis → measure → keep/revert
- Kanban integration: loop lifecycle in native board

### Phase 2 — Meta-Loop (Week 3–4)
- Meta-loop tick: idle detection, dispatch, harvest
- Goal registry: `goals/` directory + `index.yaml`
- Parent/child loop spawning
- `saturate status` CLI

### Phase 3 — Fleet (Week 5–6)
- Add Mac nodes and Linux laptop to Ray cluster
- Per-node resource tagging
- Multi-loop concurrency per node (fractional CPU)
- Stagnation detection + clean terminal-state handling

### Phase 4 — OMH Loop Design Integration (Week 7–8)
- `omh-loop-design` skill producing Saturate-compatible specs
- End-to-end: design a loop → commit spec → watch it run overnight
- Audit trail review workflow

---

## What Is Not Saturate

To keep the scope clean:

- **Deliberation** — that is OMH (`omh-loop-design`, `omh-autoresearch`)
- **Inference serving** — that is vLLM / llama.cpp / whatever the node runs;
  Saturate calls inference as a tool, it does not serve it
- **Multi-GPU model sharding** — deferred; Saturate routes *loops* to nodes
  that have GPUs, it does not pool GPU capacity across nodes for one workload
- **Continuum** — Continuum is a cognitive presence that can *drive* the
  meta-loop; Saturate exposes a clean interface for it but does not depend on it
