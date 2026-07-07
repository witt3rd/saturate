# Saturate – Task Queue & Workflow Orchestration Layer

## Overview

This document extends the Saturate architecture with a **task queue and workflow orchestration layer** — a persistent, richly attributed job store that accepts work from any source, models dependencies, enforces priorities, respects deadlines, and continuously dispatches jobs to the Ray compute substrate. The result is a system where you or your agents can post tasks with full scheduling intent, and Saturate handles all orchestration automatically.

***

## The Task Model

Every unit of work submitted to Saturate is a **Task** — a self-describing job record with the following attributes:

```python
@dataclass
class SaturateTask:
    # Identity
    task_id: str                        # UUID
    name: str                           # human-readable label
    description: str                    # what this task does

    # Scheduling intent
    priority: int                       # 0 (highest) to 100 (lowest)
    deadline: Optional[datetime]        # hard deadline, None = best-effort
    earliest_start: Optional[datetime]  # not-before constraint

    # Dependencies
    depends_on: List[str]               # task_ids that must complete first
    parallel_with: List[str]            # task_ids that can/should run concurrently

    # Resource requirements
    num_cpus: float                     # fractional OK (e.g. 0.5)
    num_gpus: float                     # fractional OK (e.g. 0.25)
    memory_gb: float
    required_node_class: Optional[str]  # e.g. "DGX_SPARK", "APPLE_SILICON"
    estimated_duration_seconds: int     # used for scheduling lookahead

    # Execution
    entrypoint: str                     # Ray remote function or Actor method
    args: dict                          # serialized arguments
    max_retries: int                    # automatic retry count
    checkpoint: bool                    # durable checkpoint outputs?

    # Metadata
    tags: List[str]
    submitted_by: str                   # agent name, user, or system
    submitted_at: datetime
```

This schema gives the scheduler everything it needs to make optimal dispatch decisions: what resources are required, when the task must complete, what it depends on, and how to run it.

***

## Layer Architecture

The task queue layer sits between the user/agents (task producers) and the Ray cluster (task executor):

```
┌──────────────────────────────────────────────────────────┐
│                    Task Producers                         │
│  You (CLI/UI)  ·  Autonomous Agents  ·  Scheduled Crons  │
└──────────────────────────┬───────────────────────────────┘
                           │  POST /tasks (HTTP or Python API)
┌──────────────────────────▼───────────────────────────────┐
│              Saturate Task Queue Service                  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Task Store (SQLite / Redis / PostgreSQL)          │  │
│  │  Persistent, survives cluster restarts             │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Scheduler Loop                                    │  │
│  │  · Resolves dependency DAG                        │  │
│  │  · Applies priority + deadline scoring            │  │
│  │  · Matches resource requirements to Ray nodes     │  │
│  │  · Dispatches via Ray Job API or Actor calls      │  │
│  └────────────────────────────────────────────────────┘  │
│                                                          │
│  ┌────────────────────────────────────────────────────┐  │
│  │  Status & Event Bus                                │  │
│  │  · Task lifecycle events (PENDING→RUNNING→DONE)   │  │
│  │  · Triggers downstream dependent tasks            │  │
│  └────────────────────────────────────────────────────┘  │
└──────────────────────────┬───────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────┐
│                    Ray Cluster                            │
│  DGX Spark · RTX4090/3090 WS · Mac Pro · MacBook Pro     │
│  Surfaces (CPU) · Linux Laptop (CPU)                     │
└──────────────────────────────────────────────────────────┘
```

***

## Implementation Options

Three approaches exist, each with different trade-offs:

### Option A: Ray Workflows (Native, Simplest)

Ray ships a built-in Workflows library that provides durable, checkpointed execution of DAGs directly on the Ray cluster. Each workflow step is a Ray task; outputs are checkpointed to persistent storage after each step, enabling resume from the last successful step after a crash.[^1][^2][^3]

**Key capabilities:**
- Durable execution with exactly-once semantics via checkpointing[^1]
- Resume by `workflow_id` — if a multi-hour task fails halfway, it restarts from the checkpoint, not from zero[^2][^4]
- Concurrency control: `workflow.init(max_running_workflows=10, max_pending_workflows=50)` sets global queue limits[^4]
- PENDING/RUNNING/FAILED/RESUMABLE/CANCELED/SUCCESSFUL status model[^4]
- Dynamic DAGs: branching and looping based on runtime data, not just static graphs[^3]

**Limitations:**
- No native priority scheduling within the workflow queue[^5][^6]
- No deadline-aware scheduling
- Ray Workflows was marked experimental/alpha and has been noted as potentially deprecated in future Ray versions[^7]

**Verdict:** Best for getting started fast. Add an external priority layer (see below) for production use.

### Option B: Kueue + KubeRay (Production-Grade Priority Queue)

Kueue is a Kubernetes-native queueing system with first-class RayJob integration. It manages quota reservation, priority-based admission, and preemption — the most complete implementation of the scheduling attributes in the Saturate task model.[^8][^9]

**Key capabilities:**
- `WorkloadPriorityClass` assigns independent scheduling priority to each RayJob, separate from pod-level priority[^10][^9]
- Higher-priority jobs can preempt already-running lower-priority jobs, evicting their pods and suspending the Ray cluster until resources free up[^11][^12]
- Gang scheduling: a job waits until ALL required GPUs are simultaneously available before any pods start — prevents partial allocation waste[^13][^11]
- Resource fungibility: if the preferred GPU node class is full, Kueue can substitute an equivalent flavor[^8]
- StrictFIFO and BestEffortFIFO queue strategies[^8]
- NVIDIA KAI Scheduler integrates natively with KubeRay, bringing enterprise-grade gang scheduling and workload prioritization directly into Ray[^14]

**What Kueue does NOT handle natively:**
- Deadline-aware scheduling (requires custom preemption policy)
- Estimated duration for lookahead scheduling
- Cross-workflow dependency DAGs (these live in Ray Workflows or a workflow engine above Kueue)

**Verdict:** The right choice for the priority + preemption requirements. Kueue handles *admission* scheduling; Ray Workflows handles *execution* DAG logic. They compose cleanly.

### Option C: Temporal + Ray (Richest, Most Complex)

Temporal is a durable workflow orchestration engine that handles long-running, stateful workflows with fine-grained retry control, timeout management, and complete event history. It pairs with Ray by using Temporal to manage workflow lifecycle and dependency orchestration, while delegating heavy compute execution to Ray.[^15][^16][^17]

**Key capabilities:**
- Durable execution: workflows survive worker restarts, cluster failures, and arbitrary delays[^16]
- Configurable timeout and retry at the Activity (step) level — each task in Saturate can have its own retry policy and deadline[^16]
- Complete audit trail: every workflow event is sourced and queryable[^17]
- Temporal's Matching Service manages task queues being polled by workers — essentially a production-grade implementation of the Saturate task model[^16]
- Can model complex dependency patterns (sequential chains, fan-out/fan-in, conditional branches, loops) as ordinary Python code[^17]

**Limitations:**
- Steeper operational complexity: requires running a Temporal server (PostgreSQL backend recommended for durability)[^18][^19]
- Adds another system to maintain alongside Ray

**Verdict:** The most powerful option if workflow durability and auditability are critical — for example, if Saturate is running multi-day research workflows where partial-completion recovery matters enormously.

***

## Recommended Composition for Saturate

Given the goal of continuous saturation with rich scheduling semantics, the recommended stack is:

| Layer | Technology | Responsibility |
|---|---|---|
| Task store | SQLite (dev) → PostgreSQL (prod) | Persistent task records with all attributes |
| Priority queue | Kueue + KubeRay | Admission control, priority, preemption, gang scheduling |
| DAG execution | Ray Workflows | Durable DAG execution with checkpointing |
| Workflow orchestration | Temporal (optional Phase 2) | Multi-day durable workflows, complex retry semantics |
| Compute substrate | Ray Cluster | Actual CPU/GPU task execution |

***

## Priority and Deadline Scheduling

### Priority Scoring

Since Ray has no native task priority, priority must be enforced at the queue/admission layer. Kueue's `WorkloadPriorityClass` provides this for RayJob-level scheduling. Within a workflow, a custom scheduler loop computes a dispatch score combining priority and deadline urgency:[^6][^5][^10]

```python
def dispatch_score(task: SaturateTask) -> float:
    priority_score = (100 - task.priority) / 100   # lower number = higher priority

    if task.deadline:
        time_remaining = (task.deadline - datetime.now()).total_seconds()
        estimated = task.estimated_duration_seconds
        urgency = max(0, 1 - (time_remaining / (estimated * 2)))  # 0..1
    else:
        urgency = 0.0

    return priority_score + urgency   # higher score = dispatch first
```

Tasks approaching their deadline relative to estimated duration get urgency score added on top of base priority.

### Dependency Resolution

The dependency graph is a DAG. The scheduler resolves it in topological order: a task becomes eligible for dispatch only when all tasks in its `depends_on` list have status `SUCCESSFUL`. The `parallel_with` list is advisory — the scheduler uses it to co-schedule tasks on nodes with low inter-task communication cost (locality-aware placement via Ray's data-locality scheduler).[^20]

```python
def get_dispatchable_tasks(store: TaskStore) -> List[SaturateTask]:
    pending = store.get_by_status("PENDING")
    return [
        t for t in pending
        if all(store.get(dep).status == "SUCCESSFUL" for dep in t.depends_on)
        and store.has_matching_resources(t)
    ]
```

***

## Submitting Tasks: The API

Tasks can be submitted from three surfaces:

### 1. Python API (for agents and scripts)

```python
from saturate import SaturateClient

client = SaturateClient("http://saturate-head:8000")

task_id = client.submit(
    name="Literature sweep: transformer memory architectures",
    entrypoint="saturate.agents.literature.sweep",
    args={"query": "transformer memory architectures", "max_papers": 200},
    priority=20,
    deadline=datetime.now() + timedelta(hours=6),
    num_cpus=4,
    num_gpus=0.5,
    required_node_class="DGX_SPARK",
    estimated_duration_seconds=3600,
    tags=["research", "literature"],
)
```

### 2. CLI

```bash
saturate submit \
  --name "Fine-tune Qwen3 on math corpus" \
  --entrypoint saturate.jobs.finetune \
  --priority 10 \
  --num-gpus 1.0 \
  --node-class DGX_SPARK \
  --deadline "2026-07-07T08:00:00" \
  --estimated-duration 14400 \
  --depends-on task_abc123
```

### 3. Agent self-submission

Autonomous agents running inside Saturate can submit sub-tasks to the same queue, creating hierarchical work trees — an agent doing literature review can spawn parallel summarization tasks as children, which fan back in to a synthesis task.

***

## Task Lifecycle

```
SUBMITTED
    │
    ▼
PENDING ──── dependency not met ──► BLOCKED
    │                                   │
    │  deps resolved                    │ deps resolved
    ▼                                   ▼
ELIGIBLE ◄──────────────────────────────┘
    │
    │  resources available + admitted by Kueue
    ▼
RUNNING
    │         │
    │ success  │ failure (retriable)
    ▼         ▼
SUCCESSFUL  RETRYING ──► RUNNING
                │
                │ max_retries exceeded
                ▼
              FAILED
```

***

## Gang Scheduling for Multi-GPU Jobs

For large model fine-tuning or inference jobs that require multiple GPUs simultaneously (e.g., a 70B model sharded across the RTX 4090 + DGX Spark), Kueue's gang scheduling ensures the job waits until ALL required GPUs are available before any pods start. This prevents the pathological state where half a job's workers start, consume GPU memory, and then stall waiting for the other half — wasting exactly the idle capacity Saturate is trying to utilize.[^11][^13]

***

## The Saturation Loop + Task Queue Integration

The saturation loop from the base Saturate architecture integrates with the task queue as follows:

```python
async def saturation_loop():
    while True:
        # 1. Poll cluster utilization
        utilization = await poll_cluster_utilization()  # DCGM + psutil

        # 2. Find idle capacity
        idle_resources = compute_idle_resources(utilization, threshold_gpu=0.20)

        # 3. Get highest-priority dispatchable tasks
        candidates = get_dispatchable_tasks(task_store)
        candidates.sort(key=dispatch_score, reverse=True)

        # 4. Bin-pack tasks against idle resources
        assignments = bin_pack(candidates, idle_resources)

        # 5. Dispatch via Ray
        for task, node in assignments:
            ray_ref = dispatch_to_ray(task, node)
            task_store.update(task.task_id, status="RUNNING", ray_ref=ray_ref)

        # 6. If no tasks queued, fall back to P3 science work
        if not assignments:
            dispatch_folding_at_home(idle_resources)

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
```

***

## Observability

Every task state transition emits an event to the Saturate event bus. A lightweight dashboard (FastAPI + HTMX or a simple React app) renders:

- **Queue view**: all PENDING/BLOCKED tasks, sorted by dispatch score, with estimated wait time
- **Running view**: active tasks per node, with elapsed time, GPU/CPU utilization, and progress where available
- **History view**: completed/failed tasks with duration actuals vs. estimates (feeds back into better future estimates)
- **Dependency graph view**: DAG visualization of in-flight workflow trees

Ray's built-in Dashboard (port 8265) provides complementary per-node metrics (task throughput, object store, actor state), and NVIDIA DCGM exports GPU compute/VRAM/power metrics that feed the saturation loop's idle detection.[^20]

***

## Comparison: Build vs. Adopt

| Approach | Priority | Deadlines | DAG deps | Durable | GPU-aware | Effort |
|---|---|---|---|---|---|---|
| **Custom queue + Ray** | ✅ (custom) | ✅ (custom) | ✅ (custom) | Partial | ✅ | Medium |
| **Ray Workflows only** | ❌ | ❌ | ✅ | ✅ | ✅ | Low |
| **Kueue + Ray Workflows** | ✅ | Partial | ✅ | ✅ | ✅ | Medium |
| **Temporal + Ray** | ✅ | ✅ | ✅ | ✅ | ✅ | High |

For Saturate's initial implementation, **Kueue + Ray Workflows** delivers the best coverage of the required task attributes with manageable operational overhead. Temporal can be added in a later phase if multi-day workflow durability becomes a priority.

---

## References

1. [Advanced Topics#](https://docs.ray.io/en/latest/workflows/advanced.html)

2. [ray.workflow.resume#](https://docs.ray.io/en/latest/workflows/api/doc/ray.workflow.resume.html)

3. [Ray version 1.7 has been released | Anyscale](https://www.anyscale.com/blog/ray-version-1-7-has-been-released) - Anyscale is the leading AI application platform. With Anyscale, developers can build, run and scale ...

4. [Workflow Management#](https://docs.ray.io/en/latest/workflows/management.html)

5. [Priority scheduling of jobs · Issue #16782 · ray-project/ray](https://github.com/ray-project/ray/issues/16782) - I'd like a way to be able to specifically increase the priority of submitted jobs, or at least be ab...

6. [[Dask-on-Ray] How to set task priorities?](https://discuss.ray.io/t/dask-on-ray-how-to-set-task-priorities/8761) - dask.distributed allows setting priorities for tasks (in case we have more work than workers, the sc...

7. [ray/doc/source/workflows/events.rst at master · ray-project/ray](https://github.com/ray-project/ray/blob/master/doc/source/workflows/events.rst) - Ray is an AI compute engine. Ray consists of a core distributed runtime and a set of AI Libraries fo...

8. [Features overview - Kueue - Kubernetes](https://kueue.sigs.k8s.io/docs/overview/) - Why Kueue?

9. [Concepts | Kueue - Kubernetes](https://kueue.sigs.k8s.io/docs/concepts/) - Core Kueue Concepts

10. [Run job with WorkloadPriority - Kueue - Kubernetes](https://kueue.sigs.k8s.io/docs/tasks/manage/run_job_with_workload_priority/) - Run job with WorkloadPriority, which is independent from Pod's priority

11. [Using KubeRay and Kueue to orchestrate Ray applications ...](https://cloud.google.com/blog/products/containers-kubernetes/using-kuberay-and-kueue-to-orchestrate-ray-applications-in-gke) - Learn how KubeRay and Kueue can orchestrate Ray applications running on GKE using either priority or...

12. [Kueue preemption - AI on OpenShift](https://ai-on-openshift.io/odh-rhoai/kueue-preemption/readme/) - The one-stop shop for Data Science and Data Engineering on OpenShift! Tools and applications, patter...

13. [Gang Scheduling with RayJob and Kueue - Ray Docs](https://docs.ray.io/en/latest/cluster/kubernetes/examples/rayjob-kueue-gang-scheduling.html) - This guide demonstrates how to use Kueue for gang scheduling RayJob resources, taking advantage of d...

14. [Enable Gang Scheduling and Workload Prioritization in Ray with NVIDIA KAI Scheduler](https://developer.nvidia.com/blog/enable-gang-scheduling-and-workload-prioritization-in-ray-with-nvidia-kai-scheduler/) - NVIDIA KAI Scheduler is now natively integrated with KubeRay, bringing the same scheduling engine th...

15. [Build Durable Industrial AI Workflows with Fault Recovery Using Temporal and Ray | Atomic Loops](https://www.atomicloops.com/technologies/ai-infrastructure-and-devops/build-durable-industrial-ai-workflows-with-fault-recovery-using-temporal-and-ray) - Discover how to build robust industrial AI workflows with fault recovery using Temporal and Ray. Lea...

16. [Temporal Workflow Orchestration: Features & Comparison](https://zdgroup.com.au/blog/exploring-temporal-features-architecture-and-comparisons/) - Learn about Temporal’s features for durable workflows, retry control, and visibility. Compare it to ...

17. [Temporal's Approach: Durable...](https://temporal.io/blog/temporal-replaces-state-machines-for-distributed-applications) - Discover how Temporal’s Durable Execution model simplifies distributed applications by replacing com...

18. [A Self-Hosted Workflow Engine for Small Teams - Kishan Ray](https://roykishan8.medium.com/orchestrating-reliability-with-temporal-a-self-hosted-workflow-engine-for-small-teams-70f1cd0d8139) - Modern applications often need to coordinate long-running, distributed tasks — think sending campaig...

19. [How to Self-Host Temporal for Reliable Workflow ...](https://www.linkedin.com/posts/kishan-ray_orchestrating-reliability-with-temporal-activity-7391462917411512320-m9QQ) - 🚀 Taming Async Chaos with Temporal — My Guide to Self-Hosting a Reliable Workflow Engine We’ve all b...

20. [Scheduling — Ray 2.55.1](https://docs.ray.io/en/latest/ray-core/scheduling/index.html) - This page provides an overview of how Ray decides to schedule tasks and actors to nodes. Labels: Lab...

