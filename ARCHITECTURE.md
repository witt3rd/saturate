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

## The Loop Taxonomy

**Types are semantic.** A `ConsensusLoopSpec` is not a `MetricOptimizationSpec`.
A function that accepts one cannot be passed the other. A runner that handles
`Accepted | Discarded` turn results cannot be confused with one that handles
`ConsensusReached | RoundComplete | Deadlocked`. The kind is not a string tag —
it shapes every other type in the system.

### Loop Kinds (sealed hierarchy)

```python
# Sealed — no other subclasses permitted
class LoopKind: pass

class MetricOptimizationKind(LoopKind): pass  # hypothesis → measure → keep/revert
class ConsensusKind(LoopKind):          pass  # agents deliberate until agreement
class TaskExecutionKind(LoopKind):      pass  # tasks executed until plan verified
class InformationSeekingKind(LoopKind): pass  # search until gaps close
class ClarificationKind(LoopKind):
    HUMAN_GATED: ClassVar[bool] = True        # only human call to complete() ends this
class SelectionKind(LoopKind):          pass  # spawn N candidates, keep top-k, repeat

# Structural (non-loop) task kinds
class BatchKind: pass   # fan-out to parallel workers, collect and synthesize
class OnceKind:  pass   # single execution to completion

TaskKind = Union[LoopKind, BatchKind, OnceKind]
```

### Per-Kind Spec Types

Each kind has a spec type that carries exactly the fields required to run it.
Producers must supply a typed spec — not a generic YAML blob.

```python
@dataclass(frozen=True)
class MetricOptimizationSpec:
    goal:           str
    metric:         MetricSpec         # command, extract mode, direction, baseline
    correctness:    CorrectnessSpec    # command that must pass after every hypothesis
    max_turns:      int
    stagnation_n:   int
    budget_tokens:  Optional[int]
    memory:         str                # output directory
    spawn:          Optional[SpawnPolicy]

@dataclass(frozen=True)
class ConsensusSpec:
    goal:               str
    roles:              List[Role]     # e.g. [Planner, Architect, Critic]
    max_rounds:         int
    consensus_fn:       ConsensusFn   # e.g. AllApprove | MajorityApprove
    memory:             str

@dataclass(frozen=True)
class TaskExecutionSpec:
    plan_source:        str            # path to plan file
    max_iterations:     int
    circuit_breaker_n:  int            # consecutive same-error → block task
    memory:             str

@dataclass(frozen=True)
class InformationSeekingSpec:
    question:           str
    max_iterations:     int
    sufficiency_fn:     SufficiencyFn  # evaluator that judges "enough evidence"
    memory:             str

@dataclass(frozen=True)
class ClarificationSpec:
    dimensions:         List[Dimension]
    coverage_threshold: CoverageLevel
    max_rounds:         int
    memory:             str
    # No sufficiency_fn — terminal condition is always human-confirmed

@dataclass(frozen=True)
class SelectionSpec:
    goal:               str
    population_size:    int
    selection_k:        int            # survivors carried to next generation
    fitness_spec:       FitnessSpec    # command + extraction + direction
    max_generations:    int
    memory:             str
    spawn:              Optional[SpawnPolicy]

LoopSpec = Union[
    MetricOptimizationSpec,
    ConsensusSpec,
    TaskExecutionSpec,
    InformationSeekingSpec,
    ClarificationSpec,
    SelectionSpec,
]
```

### Per-Kind Turn Result Algebras

Each kind's runner returns a typed result. Pattern-match is exhaustive —
the compiler (or type checker) forces handling every case.

```python
# MetricOptimizationKind
@dataclass(frozen=True)
class Accepted:
    hypothesis:     str
    previous_value: float
    new_value:      float

@dataclass(frozen=True)
class Discarded:
    hypothesis:     str
    reason:         DiscardReason

@dataclass(frozen=True)
class Regressed:  previous_value: float; observed_value: float
@dataclass(frozen=True)
class Crashed:    exit_code: int;        stderr: str
@dataclass(frozen=True)
class Unchanged:  observed_value: float; noise_threshold: float

DiscardReason       = Union[Regressed, Crashed, Unchanged]
MetricTurnResult    = Union[Accepted, Discarded]

# ConsensusKind
@dataclass(frozen=True)
class RoundComplete:
    round:    int
    verdicts: Dict[Role, Verdict]   # Approve | RequestChanges | Reject

@dataclass(frozen=True)
class ConsensusReached:
    proposal: str
    rounds:   int

ConsensusTurnResult = Union[RoundComplete, ConsensusReached]

# TaskExecutionKind
@dataclass(frozen=True)
class TaskPassed:   task_id: str; learning: str
@dataclass(frozen=True)
class TaskFailed:   task_id: str; error_fingerprint: ErrorFingerprint
@dataclass(frozen=True)
class TaskBlocked:  task_id: str; reason: str
@dataclass(frozen=True)
class AllTasksPassed: summary: str

TaskTurnResult = Union[TaskPassed, TaskFailed, TaskBlocked, AllTasksPassed]

# InformationSeekingKind
@dataclass(frozen=True)
class FindingsAdded:  new_findings: List[Finding]; remaining_gaps: List[Gap]
@dataclass(frozen=True)
class Sufficient:     findings: List[Finding]

InformationTurnResult = Union[FindingsAdded, Sufficient]

# ClarificationKind
@dataclass(frozen=True)
class CoverageUpdated: dimension: Dimension; new_level: CoverageLevel
@dataclass(frozen=True)
class HumanConfirmed:  spec: str

ClarificationTurnResult = Union[CoverageUpdated, HumanConfirmed]

# SelectionKind
@dataclass(frozen=True)
class GenerationComplete:
    generation:    int
    survivors:     List[Candidate]   # top-k by fitness
    best_fitness:  float

@dataclass(frozen=True)
class Converged:
    best_candidate: Candidate
    generations:    int

SelectionTurnResult = Union[GenerationComplete, Converged]
```

### Per-Kind Terminal State Algebras

Terminal states carry the evidence that explains *why* the loop ended.

```python
# MetricOptimizationKind
@dataclass(frozen=True)
class MetricSuccess:   achieved_value: float; turns: int
@dataclass(frozen=True)
class MetricStalled:   best_value: float; stagnation_count: int
@dataclass(frozen=True)
class MetricExhausted: best_value: float; turns_used: int; budget_used: int

MetricTerminal = Union[MetricSuccess, MetricStalled, MetricExhausted, Blocked, Cancelled]

# ConsensusKind
@dataclass(frozen=True)
class ConsensusTerminalReached:  proposal: str; rounds: int
@dataclass(frozen=True)
class Deadlocked:                rounds: int; final_verdicts: Dict[Role, Verdict]

ConsensusTerminal = Union[ConsensusTerminalReached, Deadlocked, Blocked, Cancelled]

# TaskExecutionKind
@dataclass(frozen=True)
class PlanComplete:   tasks_completed: int; learnings: List[str]
@dataclass(frozen=True)
class PlanBlocked:    blocked_tasks: List[str]; reasons: List[str]

TaskTerminal = Union[PlanComplete, PlanBlocked, Exhausted, Cancelled]

# InformationSeekingKind
@dataclass(frozen=True)
class InformationSufficient:  findings: List[Finding]; iterations: int
@dataclass(frozen=True)
class InformationExhausted:   findings: List[Finding]; open_gaps: List[Gap]

InformationTerminal = Union[InformationSufficient, InformationExhausted, Cancelled]

# ClarificationKind — HUMAN_GATED: no automatic terminal predicate
# The scheduler NEVER marks a ClarificationKind task terminal.
# Only an explicit human call to complete() ends it.
@dataclass(frozen=True)
class ClarificationConfirmed: spec: str; coverage: Dict[Dimension, CoverageLevel]

ClarificationTerminal = Union[ClarificationConfirmed, Cancelled]

# SelectionKind
@dataclass(frozen=True)
class SelectionConverged:   best: Candidate; generations: int
@dataclass(frozen=True)
class SelectionExhausted:   best: Candidate; generations: int

SelectionTerminal = Union[SelectionConverged, SelectionExhausted, Blocked, Cancelled]

# Shared terminal states (any kind)
@dataclass(frozen=True)
class Blocked:    reason: str
@dataclass(frozen=True)
class Cancelled:  requested_by: str
@dataclass(frozen=True)
class Exhausted:  turns_used: int; budget_used: int
```

### The Typed Runner Contract

The kind type determines the runner signature. Runners are dispatched by kind —
the scheduler matches the task's kind to the runner that handles it.

```python
# Type-indexed dispatch — each overload handles exactly one kind
@overload
def run_turn(task: SaturateTask[MetricOptimizationKind],
             spec: MetricOptimizationSpec,
             state: MetricLoopState) -> MetricTurnResult: ...

@overload
def run_turn(task: SaturateTask[ConsensusKind],
             spec: ConsensusSpec,
             state: ConsensusLoopState) -> ConsensusTurnResult: ...

@overload
def run_turn(task: SaturateTask[TaskExecutionKind],
             spec: TaskExecutionSpec,
             state: TaskExecutionLoopState) -> TaskTurnResult: ...

# ... and so on per kind

# Terminal check: only fires for non-human-gated kinds
@overload
def should_terminate(spec: MetricOptimizationSpec,
                     state: MetricLoopState) -> Optional[MetricTerminal]: ...
# NOTE: no overload for ClarificationKind — terminal is always human-gated
```

The key guarantee: **a runner that handles `MetricTurnResult` cannot be
accidentally dispatched for a `ConsensusKind` task.** The type mismatch is
caught before execution, not at runtime.

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
    kind: str                             # one of the six loop kinds above

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

## saturate.measure — The Metric Primitive

`saturate.measure` runs a command and returns a scalar. It is Saturate's
built-in measurement primitive — usable by any runner directly.

```python
saturate.measure(
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

## The Executor — What Actually Does the Work

The loop runner knows *when* to call an agent and *what to do with the result*.
The **Executor** is the adapter that knows *how to call a specific agent
implementation*. These are separate concerns.

Saturate defines a protocol. Every agent framework is an implementation of it.
Saturate has zero knowledge of what is inside an executor.

### The Executor Protocol

```python
class Executor(Protocol):
    def execute_turn(
        self,
        spec:    LoopSpec,        # the full loop specification (read-only)
        state:   LoopState,       # current baseline, turn count, stagnation count
        context: TurnContext,     # abbreviated history of prior turns
    ) -> TurnResult: ...
```

`TurnResult` carries one thing: the path to a `hypothesis.md` file the executor
wrote describing what change it made (or proposes to make). Saturate reads
that file, runs `saturate.measure`, runs the correctness gate, and keeps or
reverts. The executor reasons; Saturate bookkeeps.

### Declared in the Loop Spec

Every loop spec declares its executor:

```yaml
kind:     metric-optimization
goal:     Reduce CI build time by 20%
metric:
  command:   npm run build
  extract:   wall_clock
  direction: minimize
executor:
  type:    hermes
  profile: forge            # Hermes profile to invoke
memory:   ./output/build-optimizer/
```

```yaml
executor:
  type:    shell
  command: ./agents/my-optimizer.sh   # arbitrary executable
```

```yaml
executor:
  type:    http
  url:     http://localhost:9000/turn  # POST endpoint speaking the protocol
```

### What the Executor Receives

Saturate constructs a `TurnContext` before each invocation. The executor sees
exactly what it needs — no more:

```python
@dataclass
class TurnContext:
    turn_number:     int
    baseline_metric: Optional[float]     # None on turn 0
    recent_turns:    List[TurnSummary]   # last N accepted/discarded with metric
    stagnation_n:    int                 # consecutive discards since last accept
    state_path:      str                 # where to write hypothesis.md
    output_path:     str                 # where to write final output
```

### v1 Executor Implementations

#### HermesExecutor

Invokes a Hermes profile via the `hermes` CLI. Saturate constructs a
self-contained task message from the spec and turn context, then invokes:

```bash
hermes -p {profile} --once "{task_message}"
```

The task message Forge receives on each turn:

```
You are executing turn {n} of a {kind} loop.

Goal: {spec.goal}
Current baseline: {context.baseline_metric}
Recent turns: {context.recent_turns}
Stagnation: {context.stagnation_n} consecutive turns with no improvement

Your job this turn:
1. Generate ONE hypothesis — a concrete change that might improve the metric
2. Apply it (edit files, run commands, whatever is needed)
3. Write a description of what you changed to: {context.state_path}/hypothesis.md
   Format: one paragraph, concrete, describing exactly what changed and why

Saturate will measure the result and keep or revert automatically.
Do not loop. Do not measure. Execute exactly one hypothesis and exit.
```

Forge does the reasoning and the work. Saturate does everything else.

#### ShellExecutor

Invokes an arbitrary executable. Saturate passes context as environment
variables; the script writes `hypothesis.md` to `$SATURATE_STATE_PATH`.

```python
class ShellExecutor:
    def execute_turn(self, spec, state, context) -> TurnResult:
        env = {
            "SATURATE_GOAL":          spec.goal,
            "SATURATE_TURN":          str(context.turn_number),
            "SATURATE_BASELINE":      str(context.baseline_metric or ""),
            "SATURATE_STATE_PATH":    context.state_path,
            "SATURATE_OUTPUT_PATH":   context.output_path,
        }
        subprocess.run([self.command], env={**os.environ, **env}, check=True)
        return TurnResult(hypothesis_path=f"{context.state_path}/hypothesis.md")
```

Any executable — Python script, shell script, compiled binary — is a valid
executor. No SDK required.

#### HTTPExecutor

POSTs the `TurnContext` as JSON to an endpoint, receives `TurnResult` as JSON.
Allows remote executors, microservice agents, or any language that speaks HTTP.

```python
class HTTPExecutor:
    def execute_turn(self, spec, state, context) -> TurnResult:
        resp = requests.post(self.url, json={
            "spec":    asdict(spec),
            "context": asdict(context),
        })
        return TurnResult(**resp.json())
```

### Future Executor Implementations

Any agent framework can be wrapped as an executor. The protocol is stable;
implementations are additive and never require changes to the scheduler or
runner.

```python
class ClaudeCodeExecutor:  ...   # cc CLI
class LangChainExecutor:   ...   # LangChain agent
class CrewAIExecutor:      ...   # CrewAI crew
class MCPExecutor:         ...   # any MCP-compatible tool server
```

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
  3. instantiate executor (from spec.executor)
  4. build TurnContext from state
  5. executor.execute_turn(spec, state, context)  ← agent does the work
  6. measure               ← saturate.measure → scalar
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
| Designing loops (goal → verifiable spec) | Producer's concern — Saturate is agnostic |
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
- `saturate.measure`: wall_clock, regex, json extraction; four-outcome classification
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
