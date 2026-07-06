# Saturate: Heterogeneous Compute Saturation Architecture

## Vision

**Saturate** is a personal compute fabric that continuously fills idle CPU, GPU, and memory capacity across a heterogeneous fleet of machines — Linux workstations, Windows Surfaces, Apple Silicon, and NVIDIA GPU nodes — with useful autonomous work: agent reasoning, model inference, research, simulation, and fine-tuning. The goal is zero squandered cycles: every watt of compute should be solving something at all times.

The architecture rests on two foundational layers: **Ray** as the distributed compute substrate, and a **Nalar-inspired** agent-serving control plane built atop it. Together they form a meta-OS that presents the fleet as one unified, continuously saturated resource pool.

***

## The Hardware Fleet

| Node | OS | CPU | GPU/Accelerator | Role |
|---|---|---|---|---|
| DGX Spark (GB10) | Linux | ARM | 128GB unified GPU/CPU memory (Blackwell) | Primary inference + training |
| Linux Workstation ×2 | Linux | x86 | RTX 4090, RTX 3090 | GPU compute workers |
| MacBook Pro | macOS | Apple Silicon | MPS | Inference + dev |
| Mac Pro (tower) | macOS | Intel/ARM | AMD GPU | Background CPU+GPU agent work |
| Surface ×2 | Windows 11 | x86 | iGPU | CPU-only orchestration + light agents |
| Linux Laptop | Linux | x86 | iGPU | CPU worker + dev |

This fleet represents enormous aggregate capacity that is idle most of the time — Ray's heterogeneous scheduling model is specifically designed to treat exactly this kind of mixed CPU/GPU fleet as a unified resource pool.[^1][^2]

***

## Layer 1: Ray — The Compute Substrate

Ray is the foundational runtime that federates all seven machines into one schedulable cluster. Every node joins via `ray start --address` and advertises its resources; from that point forward, the cluster appears as a single pool of CPUs, GPUs, and custom resources to anything running atop it.[^3][^4]

### Core Primitives

- **Tasks** (`@ray.remote` functions): stateless, parallel, ephemeral work units — model inference calls, data processing, tool execution by agents.
- **Actors** (`@ray.remote` classes): stateful, long-running processes — ideal for representing individual agents that maintain memory and session state.
- **Object Store**: zero-copy distributed shared memory for passing large data (tensors, embeddings, datasets) between nodes without serialization overhead.
- **Placement Groups**: colocate related actors/tasks on nodes with specific resource profiles (e.g., keep an inference actor and its memory actor on the same DGX Spark node).

### Resource Scheduling for a Heterogeneous Fleet

Ray treats CPU, GPU, memory, and custom resource types as first-class schedulables. Key behaviors relevant to Saturate:[^5]

- **Fractional resources**: `num_cpus=0.25` or `num_gpus=0.5` let multiple lightweight agents share a single core or GPU rather than monopolizing it — critical for keeping Surfaces and Mac Pro active without overcommitting.[^5]
- **Custom resources**: declare `{"DGX_SPARK": 1}` or `{"APPLE_SILICON": 1}` so the scheduler can route model-size-appropriate tasks to the right hardware class automatically.
- **Autoscaler**: Ray's autoscaler can be configured to fill idle capacity on existing nodes, triggering new workloads when utilization drops below a threshold — the primitive mechanism for Saturate's saturation loop.
- **Backfill scheduling**: Ray queues excess tasks and executes them as capacity frees, processing thousands of queued tasks across the fleet without manual management.[^6]

### Multi-Backend Inference Routing

Since inference is the most GPU-intensive workload Saturate will run, Ray integrates with:
- **llama.cpp RPC** for weight distribution across DGX Spark + workstation GPUs[^7]
- **Exo** for model sharding across Apple Silicon nodes (MacBook Pro, Mac Pro)[^8]
- **vLLM** (Ray-native) for production-grade continuous batching on CUDA nodes

All three can be wrapped as Ray Actors, letting the scheduler route inference requests to whichever backend has idle capacity.

***

## Layer 2: The Nalar-Inspired Agent Control Plane

Nalar (UT Austin/Cisco, 2026) is an agent-serving framework that identified core failure modes in using generic distributed runtimes for long-running, stateful, LLM-driven agent workflows. Its code is not publicly available, but its architecture is a clear blueprint for what to build on top of Ray.[^9]

### Where Ray Falls Short for Agents

Ray's scheduling model is event-driven and assignment-final: once a task is placed on a node, it stays there. This works for batch ML pipelines but breaks for agentic workloads because:[^9]

1. **Dynamic control flow**: an LLM's output decides what happens next — the task graph is unknown at submission time.
2. **Long-running state**: an agent may run for hours/days, accumulating memory and context that must survive node failures or migrations.
3. **Unpredictable latencies**: tool calls (web search, code execution, file I/O) have wildly variable duration, causing head-of-line blocking if not handled explicitly.
4. **Policy re-evaluation**: optimal placement decisions made at task-start become suboptimal as load shifts across the cluster.

### Nalar's Four Key Ideas

**1. Transparent Futures**
In Ray, developers explicitly call `ray.get()` and `ray.wait()` to resolve distributed futures. Nalar auto-generates lightweight stubs so that calling a remote agent or tool looks identical to calling a local function — the distributed future is invisible to the agent developer. The stub carries dependency and context metadata used by the runtime for scheduling.[^9]

**2. Managed State Layer**
Nalar decouples logical agent state from physical node placement. Agent memory (conversation history, retrieved knowledge, tool outputs) lives in a managed state object that the runtime can migrate between nodes without the agent's code knowing. In Saturate terms: an agent running on a workstation can be migrated to the DGX Spark when a GPU-intensive reasoning step begins, preserving full context.[^9]

**3. Two-Level Control Architecture**
A global controller periodically recomputes routing and priority policies across the whole fleet, while per-node local controllers enforce those policies reactively in real time. This two-level design prevents both the staleness of purely periodic scheduling and the myopia of purely reactive scheduling.[^9]

**4. Futures as Dependency Graph**
Nalar tracks which futures depend on which other futures, allowing the runtime to pipeline overlapping computations, prioritize the critical path, and opportunistically execute independent sub-tasks in parallel — critical for multi-agent research workflows with many concurrent tool calls.

### Nalar-Inspired Implementation on Ray

Since no Nalar code is available, these patterns can be implemented using Ray's own primitives:

```python
# Transparent stub pattern — wraps ray.remote into a local-looking call
def agent_stub(agent_actor):
    def call(*args, **kwargs):
        return ray.get(agent_actor.run.remote(*args, **kwargs))
    return call

# Managed state actor — decoupled from any specific worker node
@ray.remote
class AgentStateStore:
    def __init__(self): self.state = {}
    def get(self, key): return self.state.get(key)
    def set(self, key, val): self.state[key] = val
    def migrate(self, new_state): self.state = new_state

# Two-level controller sketch
@ray.remote
class GlobalController:
    def rebalance(self, cluster_state):
        # Periodic: reassign task priorities, migrate hot agents to idle nodes
        ...

@ray.remote  
class LocalController:
    def on_task_complete(self, task_id, result):
        # Reactive: immediately dispatch next task in dependency chain
        ...
```

***

## Layer 3: The Saturation Loop

The core behavioral loop that makes Saturate actually "saturate" compute continuously:

```
while True:
    utilization = poll_all_nodes()           # DCGM for GPU, psutil for CPU
    idle_nodes  = find_idle(utilization)     # below threshold (e.g. GPU < 20%)
    tasks       = select_backfill(idle_nodes) # from priority queue
    dispatch(tasks, idle_nodes)              # via Ray scheduler
    sleep(POLL_INTERVAL)                     # e.g. 30s
```

**Priority tiers for task selection:**

| Priority | Task type | Example |
|---|---|---|
| P0 | Interactive / user-facing | Active agent session, model serving |
| P1 | Foreground research | Agent-driven literature review, code synthesis |
| P2 | Background research | Continuous experiment runs, fine-tuning |
| P3 | Idle-fill | Folding@Home, BOINC protein folding, open science |

When a P0 task arrives, the scheduler preempts lower-priority backfill tasks using Ray's `ray.cancel()` and reallocates resources within seconds.

***

## Layer 4: Agent Workloads

With the compute substrate saturated, the workloads running on it define what Saturate actually produces. For a research/invention objective, candidate continuous workloads include:

- **Autonomous literature agents**: crawl arXiv, Semantic Scholar, and preprint servers; summarize, cluster, and surface novel findings in domains of interest.
- **Code synthesis agents**: given a problem statement or API spec, continuously draft, test, and refine implementations; commit passing solutions to a local research repo.
- **Experiment design agents**: propose and queue ML experiments (hyperparameter sweeps, architecture ablations), execute them on GPU nodes, and analyze results.
- **Fine-tuning pipelines**: continuous LoRA fine-tuning of local models on curated domain corpora, using idle GPU time on the DGX Spark and workstations.
- **Simulation and evolutionary search**: CPU-bound physics or optimization workloads dispatched to Surfaces, Mac Pro, and Linux laptop using Ray tasks with `num_gpus=0`.[^10]

***

## Networking and Identity Layer

All seven machines must share a flat, low-latency network for Ray's inter-node communication and object store to function correctly. **Tailscale** (WireGuard-based mesh VPN) is the recommended substrate: it assigns stable IPs to every device regardless of physical location or OS, and its performance is sufficient for the 10–40 Gbps bandwidth Ray's object store prefers for large tensor transfers.

For the DGX Spark specifically, NVIDIA's multi-node clustering announced at Computex 2026 should be leveraged directly where it provides tighter GB10-to-GB10 interconnect than a general-purpose VPN can offer.[^11]

***

## Monitoring and Observability

Ray ships a built-in dashboard (Ray Dashboard, port 8265) exposing per-node CPU/GPU utilization, task throughput, actor state, and object store usage across the entire cluster. For GPU-specific metrics, **NVIDIA DCGM** exports per-GPU compute, VRAM, power draw, and error metrics, which can feed directly into the saturation loop's idle-detection logic. The combination gives a single pane of glass over all seven heterogeneous nodes.[^12]

***

## Implementation Roadmap

### Phase 1 — Foundation (Week 1–2)
- Install Ray on DGX Spark + both Linux workstations (CUDA nodes first)
- Add Mac Pro and MacBook Pro as CPU/MPS nodes
- Add Linux laptop and Surfaces as CPU-only workers
- Validate cluster with a simple `ray.get()` test task dispatched from every node
- Stand up Ray Dashboard as the initial monitoring surface

### Phase 2 — Inference Layer (Week 3–4)
- Wrap llama.cpp RPC server on each GPU node as a Ray Actor
- Route inference requests through Ray scheduler based on model size and GPU availability
- Test cross-node model sharding for 70B+ models across DGX Spark + workstation GPUs

### Phase 3 — Agent Control Plane (Week 5–6)
- Implement transparent stub layer (wrapping `ray.remote` calls)
- Build `AgentStateStore` Ray Actor for decoupled, migratable agent memory
- Implement two-level controller: global (30s period) + local (event-driven)
- Connect to existing Hermes/Claude Code agent frameworks via Ray Actor wrappers

### Phase 4 — Saturation Loop (Week 7–8)
- Integrate DCGM + psutil telemetry into idle-detection poller
- Implement priority-tiered task queue (P0–P3)
- Add Folding@Home as P3 backfill on all nodes
- Tune saturation thresholds per node class

### Phase 5 — Research Workloads (Ongoing)
- Deploy autonomous literature, code synthesis, and experiment-design agents
- Run continuous fine-tuning pipelines on idle GPU time
- Iterate on agent workloads based on actual research output quality

***

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                         SATURATE                                │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │              Agent Workloads (P0–P3)                    │   │
│  │  Literature · Code Synthesis · Fine-tuning · Science   │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │                                     │
│  ┌────────────────────────▼────────────────────────────────┐   │
│  │         Nalar-Inspired Control Plane (built on Ray)     │   │
│  │  Transparent Stubs · Managed State · Two-Level Control  │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │                                     │
│  ┌────────────────────────▼────────────────────────────────┐   │
│  │                  Ray Cluster                             │   │
│  │  Tasks · Actors · Object Store · Fractional GPU Sched   │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │                                     │
│  ┌────────────────────────▼────────────────────────────────┐   │
│  │              Tailscale Mesh Network                      │   │
│  │  DGX Spark · RTX4090/3090 WS · MacBP · MacPro          │   │
│  │  Surface×2 · Linux Laptop                               │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

---

## References

1. [Ray Clusters for AI: Distributed Computing Architecture | Introl Blog](https://introl.com/blog/ray-clusters-distributed-ai-computing-infrastructure-guide-2025) - Heterogeneous compute: AI pipelines mix CPU-intensive data processing with GPU-accelerated training ...

2. [Ray: Distributed Computing for All, Part 1 | Towards Data Science](https://towardsdatascience.com/ray-distributed-computing-for-all-part-1/) - Ray Core is designed for scaling CPU-intensive general-purpose Python applications. It's designed to...

3. [Ray Distributed Agent Orchestration: From Local Prototype to Cluster Deployment | AgentList](https://www.agentlist.top/en/articles/ray-distributed-agent-orchestration/) - Using Ray's distributed runtime to scale agent prototypes from a single machine to horizontally scal...

4. [Scale Machine Learning & AI Computing | Ray by Anyscale](https://www.ray.io) - Ray is an open source framework for managing, executing, and optimizing compute needs. Unify AI work...

5. [Resources — Ray 2.56.0 - Ray Docs](https://docs.ray.io/en/latest/ray-core/scheduling/resources.html) - Ray supports fractional resource requirements. For example, if your task or actor is IO bound and ha...

6. [How does Ray handles a number of jobs higher than the number of resources?](https://stackoverflow.com/questions/71138987/how-does-ray-handles-a-number-of-jobs-higher-than-the-number-of-resources) - Pretty basic question, but I wasn't able to find the answer in the docs. I am developing a computati...

7. [llama.cpp/tools/rpc/README.md at master · ggml-org/llama ... - GitHub](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md) - By default, llama.cpp distributes model weights and the KV cache across all available devices -- bot...

8. [exo-explore/exo: Run frontier AI locally. - GitHub](https://github.com/exo-explore/exo) - exo connects all your devices into an AI cluster. Not only does exo enable running models larger tha...

9. [Nalar: A Serving Framework for Agent Workflows - arXiv](https://arxiv.org/html/2601.05109v1)

10. [Combining GPU and CPU for accelerating evolutionary computing workloads](http://www.arxiv.org/abs/2502.11129) - Evolutionary computing (EC) has proven to be effective in solving complex optimization and robotics ...

11. [Run Local AI Agents with Faster Models and Multi-Node Clustering ...](https://developer.nvidia.com/blog/run-local-ai-agents-with-faster-models-and-multi-node-clustering-on-nvidia-dgx-spark/) - NVIDIA DGX Spark is designed to build and run autonomous agents locally. At Computex 2026, NVIDIA is...

12. [Why Your GPU and CPU Clusters are 80% Idle and How to Fix Them](https://www.youtube.com/watch?v=hZOBeWankQw) - If you’re running AI workloads on Kubernetes, chances are your average GPU/CPU utilization is below ...

