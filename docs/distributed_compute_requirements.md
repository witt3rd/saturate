# Saturate — Distributed Compute Requirements

> **Purpose:** Technology-agnostic requirements written for third-party evaluation.
> **Status:** Evaluation complete — 2026-07-06.
> Technology selected: **SQLite/PostgreSQL** (durable queue) + **Nomad** (fleet
> management). See [ARCHITECTURE.md](../ARCHITECTURE.md) for the resulting design.
>
> **Date:** 2026-07-06
> **Priority notation:** MUST (hard requirement), SHOULD (strong preference), MAY (nice to have)

---

## 1. Context and Scale

Saturate is a **personal compute orchestration system** for a single engineer
operating a heterogeneous fleet of ~7 machines. It is explicitly **not**:

- A multi-tenant or enterprise cluster management system
- A cloud-hosted service
- A datacenter or Kubernetes-native workload
- A real-time or sub-second latency system

The fleet is personal, always-on, connected via a Tailscale WireGuard mesh VPN.
Nodes span Linux (x86 and ARM), macOS (Apple Silicon and Intel), with possible
future Windows support. The operator is a single user who owns and administers
all nodes.

**Scheduling cadence:** tens of seconds to minutes, not milliseconds. The system
polls for idle capacity on a configurable interval (target: 30–60 seconds).

---

## 2. Fleet and Node Management

**R2.1** MUST support scheduling work across a heterogeneous fleet of machines
connected by a standard TCP/IP network (not requiring specialized datacenter
fabric or InfiniBand).

**R2.2** MUST support nodes with different hardware profiles: x86 CPU-only nodes,
ARM CPU-only nodes, nodes with discrete NVIDIA GPUs, and nodes with Apple
Silicon unified memory.

**R2.3** MUST allow nodes to advertise available resources at join time, including:
- CPU cores (with fractional allocation, e.g. 0.5 CPUs)
- GPU count and class (e.g. "RTX 4090", "Blackwell")
- System memory (GB)
- Custom capability tags (e.g. "HAS_DGX_SPARK", "HAS_APPLE_SILICON")

**R2.4** MUST support routing specific work items to specific node classes based
on declared capability tags, without requiring manual node assignment.

**R2.5** MUST allow multiple concurrent work items to share a single node through
fractional resource allocation (e.g. four workers each using 25% of CPU).

**R2.6** MUST support adding and removing nodes from the fleet without restarting
the scheduler or losing in-flight work state.

**R2.7** SHOULD support cross-platform worker execution: workers run on Linux,
macOS, and eventually Windows from a single scheduler.

**R2.8** MUST function correctly over a Tailscale WireGuard mesh VPN (stable IPs,
standard TCP/UDP ports, no requirement for datacenter-grade networking).

---

## 3. Work Queue and Scheduling

**R3.1** MUST provide a persistent work queue that survives scheduler restarts,
process crashes, and node reboots without losing work items.

**R3.2** MUST support a rich work item record including at minimum:
- Unique identifier
- Human-readable name and description
- Priority score (numeric, e.g. 0–100 where 0 = highest)
- Optional hard deadline (datetime)
- Optional not-before constraint (earliest start datetime)
- Dependency list (IDs of work items that must complete before this one starts)
- Resource requirements (CPUs, GPUs, memory, node class tag)
- Estimated duration (used for scheduling lookahead and deadline urgency)
- Retry count on failure
- Submitter identity (agent, user, or system name)
- Arbitrary metadata payload

**R3.3** MUST implement priority-based dispatch: higher-priority items are
dispatched before lower-priority items when multiple items are eligible.

**R3.4** MUST implement deadline-aware scheduling: a lower-priority item
approaching its deadline relative to its estimated duration should gain urgency
score and compete with higher-priority items.

**R3.5** MUST implement dependency resolution: a work item becomes eligible for
dispatch only when all items in its dependency list have reached a successful
terminal state.

**R3.6** MUST support concurrent execution of independent work items (no
`depends_on` relationship) on different nodes simultaneously.

**R3.7** SHOULD support preemption: a high-priority (P0) item arriving while
lower-priority items are running should be able to preempt them within a
configurable time window, causing the preempted item to checkpoint and yield
its node.

**R3.8** SHOULD support work item groups or tags for bulk operations (pause all
items tagged "background-research", cancel all items in a given session).

---

## 4. Worker Model

**R4.1** MUST support long-running stateful workers: a worker may run for
minutes, hours, or days without natural termination, accumulating state
across iterations.

**R4.2** MUST support short-lived ephemeral workers: a worker may complete in
seconds and produce a single output.

**R4.3** MUST provide crash detection: if a worker process dies unexpectedly, the
scheduler must detect this within a configurable timeout, mark the work item as
failed or retriable, and free the node's resources.

**R4.4** MUST support automatic retry with configurable retry count per work item.

**R4.5** MUST allow workers to submit new work items to the same queue during
execution (agent self-submission / dynamic work spawning). This is the mechanism
by which a running worker discovers sub-problems and creates child work items.

**R4.6** MUST support a parent/child relationship between work items: a child
item knows the ID of the parent that spawned it, forming a queryable spawn tree.

**R4.7** MUST support human-in-the-loop intervention: pausing a running work
item, inspecting its current state, and resuming it — without losing in-flight
progress.

**R4.8** Workers are **external processes** (not compiled into the scheduler).
The scheduler MUST be able to launch and manage workers that are arbitrary
executables (Python scripts, shell scripts, compiled binaries) rather than
requiring workers to be written in a specific language or import a specific SDK.

**R4.9** SHOULD support passing structured data to workers at launch time (the
work item's payload/spec) and receiving structured output back at completion.

---

## 5. The Iterative Loop Execution Pattern

This is the primary worker shape Saturate must support well. It is distinct
from a standard batch job.

**R5.1** MUST efficiently support a **metric-optimization loop** pattern where
a single work item executes many sequential iterations, each following this
cycle:

```
1. generate hypothesis    (worker calls external agent/API)
2. apply tentatively      (worker modifies state, e.g. edits files)
3. measure metric         (worker runs a command, extracts a scalar)
4. evaluate correctness   (worker runs a validation command, gets pass/fail)
5. if improved AND correct:  commit the change, advance baseline
   else:                     revert the change, record the dead end
6. check terminal conditions (budget, iteration cap, stagnation)
7. repeat or stop
```

**R5.2** MUST support externally enforced budget controls on a running work item
without requiring the worker to implement its own loop: hard iteration ceiling,
wall-clock time limit, and a stagnation detector (N consecutive iterations with
no improvement).

**R5.3** MUST support external metric measurement: the scheduler (or a scheduler-
provided helper) runs an arbitrary shell command and extracts a numeric scalar
using one of: wall-clock elapsed time, regex match against stdout, or a JSON
field from structured output.

**R5.4** MUST distinguish four metric outcomes clearly:
- `improved` — metric moved in the correct direction vs. baseline
- `regressed` — metric moved in the wrong direction
- `crashed` — measurement command exited non-zero or timed out
- `unchanged` — metric within noise threshold

`crashed` must not be treated as equivalent to `regressed` — it is a separate
outcome that does not update the running baseline.

**R5.5** MUST maintain a per-work-item iteration log: every turn's hypothesis,
measurement outcome, and keep/revert decision, queryable after the fact as the
audit trail.

**R5.6** MUST support named terminal states with explicit stop reasons:
`success`, `stalled`, `exhausted`, `blocked`, `cancelled`. A work item must
declare which terminal states it recognizes before it is scheduled.

---

## 6. Idle Detection and Saturation

**R6.1** MUST poll each node's resource utilization on a configurable interval
to determine whether it has idle capacity available for new work.

**R6.2** MUST support per-node-class configurable idle thresholds (e.g. CPU
nodes at 40% utilization threshold, GPU nodes at a separate threshold).

**R6.3** MUST treat a node running a P0 (interactive/user-facing) work item as
not idle for background work dispatch, regardless of CPU/GPU utilization metrics.

**R6.4** SHOULD integrate with OS-level CPU utilization reporting (e.g. via
`psutil` or equivalent) and, for NVIDIA GPU nodes, with DCGM or `nvidia-smi`
for per-GPU compute and memory utilization.

**R6.5** SHOULD support a fallback "idle-fill" tier: when no declared work items
are pending, the scheduler dispatches a configurable low-priority background
task (e.g. a science volunteer computing job) to prevent true idle cycles.

---

## 7. State and Durability

**R7.1** MUST persist all work item state durably: a full scheduler restart must
recover the complete queue state including in-flight items, completed items, and
their audit logs.

**R7.2** In-flight work items that were running when the scheduler crashed MUST
be recoverable: either resumed from their last checkpoint, or re-queued for
retry from the beginning, within a configurable reclaim timeout.

**R7.3** MUST maintain a durable audit trail for every work item: state
transitions, iteration logs, spawned child items, terminal outcome and reason.
The audit trail must be queryable without loading the full scheduler into memory.

**R7.4** MUST support worker-side checkpointing: a running worker can write
intermediate state to a declared location, which survives the worker being
preempted or crashed, allowing it to resume from that checkpoint rather than
from scratch.

**R7.5** The state store SHOULD be embeddable (no separate server process
required in single-node development mode) and SHOULD support a standalone
server mode for fleet operation.

---

## 8. Work Item Submission Interface

**R8.1** MUST provide a programmatic API (callable from Python at minimum) for
submitting work items, querying state, cancelling items, and retrieving output.

**R8.2** MUST provide a CLI for the same operations, usable in shell scripts and
by the operator directly.

**R8.3** MUST support the four-operation abstract interface:
- `post(item)` — submit a work item
- `claim()` — a worker atomically claims the next available item
- `write_state(item, state)` — update running state
- `complete(item, output)` — mark terminal, attach output and metadata

**R8.4** The claim operation MUST be atomic and crash-safe: two workers on
different nodes cannot claim the same item simultaneously; a claimed item
automatically reverts to claimable if the claiming worker crashes without
completing.

---

## 9. Observability

**R9.1** MUST provide a real-time view of the fleet: per-node utilization,
running work items, queue depth by priority tier.

**R9.2** MUST provide a work item history view: completed and failed items with
actual vs. estimated durations, terminal reasons, and metric trajectories.

**R9.3** MUST provide a dependency graph view: for any work item, show its
parent, its children, and the status of all items in its dependency chain.

**R9.4** SHOULD expose metrics in a standard format consumable by a dashboard
(e.g. Prometheus-compatible endpoint or equivalent).

**R9.5** SHOULD provide a notification mechanism: when a long-running work item
reaches a terminal state, the operator is notified (webhook, message, or
equivalent).

---

## 10. Operational Constraints

**R10.1** MUST NOT require Kubernetes or any container orchestration platform.
The system must run on bare-metal or VM Linux and macOS machines.

**R10.2** MUST be operationally manageable by a single engineer. Setup,
monitoring, and maintenance must not require a dedicated ops team or
enterprise infrastructure expertise.

**R10.3** MUST have a viable single-node development mode: the full system (
scheduler, queue, workers) runs on one machine with no external dependencies,
for local development and testing of loop logic before fleet deployment.

**R10.4** MUST be open source or have a self-hosted license model. No
per-seat or per-compute-hour SaaS fees acceptable for a personal fleet.

**R10.5** SHOULD have an active maintainer community and a track record of
stability on heterogeneous Linux/macOS deployments.

**R10.6** SHOULD have a Python client library (workers are Python; native
Python integration reduces friction).

**R10.7** MAY provide a Rust client library or be implemented in Rust, for
future integration with Rust-native cognitive loop components.

**R10.8** Scheduler process memory overhead SHOULD be low enough to run
comfortably on a node that is simultaneously executing loop workloads (target:
< 500 MB resident for the scheduler process itself).

---

## 11. Out of Scope (Explicit Non-Requirements)

The evaluating party should NOT optimize for the following — they are
explicitly out of scope for Saturate:

- **Multi-GPU model sharding** — pooling GPU capacity across nodes for a single
  work item is not required. Work items run on a single node; Saturate routes
  work *to* capable nodes, it does not aggregate their GPUs.
- **Sub-second scheduling latency** — the scheduling loop runs on a 30–60 second
  cadence; millisecond dispatch is not a requirement.
- **Multi-tenancy** — single operator, no user isolation, no RBAC.
- **Kubernetes-native operation** — explicitly excluded (see R10.1).
- **Datacenter networking** — Tailscale mesh is the fabric; InfiniBand, RDMA,
  and datacenter-grade switching are not required.
- **Gang scheduling** — simultaneous resource reservation across multiple nodes
  for a single work item is not required.
- **Enterprise compliance** — no audit logging for compliance purposes, no SSO,
  no SOC2.
- **Windows support in v1** — deferred; the evaluation should note Windows
  support as a future consideration but it is not a v1 requirement.

---

## 12. Evaluation Criteria Summary

| Requirement Area | Priority |
|---|---|
| Heterogeneous fleet scheduling (R2) | MUST |
| Fractional resource allocation (R2.5) | MUST |
| Durable priority work queue with deadlines and dependencies (R3) | MUST |
| Worker crash detection and retry (R4.3, R4.4) | MUST |
| Agent self-submission / dynamic spawning (R4.5, R4.6) | MUST |
| External process workers, no SDK lock-in (R4.8) | MUST |
| Metric-optimization loop pattern support (R5) | MUST |
| Durable state, crash recovery (R7) | MUST |
| Atomic claim operation (R8.4) | MUST |
| No Kubernetes requirement (R10.1) | MUST |
| Single-node dev mode (R10.3) | MUST |
| Open source / self-hosted (R10.4) | MUST |
| Preemption (R3.7) | SHOULD |
| Human-in-the-loop pause/resume (R4.7) | SHOULD |
| Embeddable state store (R7.5) | SHOULD |
| Python client library (R10.6) | SHOULD |
| Rust client library (R10.7) | MAY |
| Windows support (future) | MAY |
