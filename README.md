# Saturate

**Personal compute fabric that continuously fills idle CPU, GPU, and memory capacity across a heterogeneous fleet with useful autonomous work.**

Zero squandered cycles: every watt of compute solving something at all times — agent reasoning, model inference, research, simulation, fine-tuning.

---

## Architecture Overview

Four layers, each building on the one below:

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

### Layer 1 — Ray Cluster

[Ray](https://www.ray.io) federates all nodes into one schedulable pool. Key capabilities used:

- **Tasks / Actors** — stateless parallel work units and stateful long-running agent processes
- **Object Store** — zero-copy distributed shared memory for tensors and embeddings
- **Fractional resources** — `num_gpus=0.5` lets multiple agents share a GPU without monopolizing it
- **Custom resources** — `{"DGX_SPARK": 1}` routes model-appropriate tasks to the right hardware class
- **Backfill scheduling** — queues excess tasks and drains them as capacity frees

Multi-backend inference routing: **llama.cpp RPC** (weight distribution across CUDA nodes), **Exo** (Apple Silicon sharding), **vLLM** (continuous batching, Ray-native).

### Layer 2 — Nalar-Inspired Control Plane

Addresses the gaps in using a generic distributed runtime for long-running stateful LLM agent workflows ([Nalar paper, arXiv 2601.05109](https://arxiv.org/html/2601.05109v1)):

- **Transparent stubs** — remote agent/tool calls look identical to local calls; distributed futures are invisible to agent code
- **Managed state** — agent memory decoupled from physical node placement; full context survives node migration
- **Two-level control** — global controller (periodic, cluster-wide rebalancing) + local controllers (reactive, event-driven per node)
- **Dependency graph** — futures track inter-dependencies for pipelining, critical-path prioritization, and parallel sub-task execution

### Layer 3 — Saturation Loop

```
while True:
    utilization = poll_all_nodes()            # DCGM for GPU, psutil for CPU
    idle_nodes  = find_idle(utilization)      # below threshold (e.g. GPU < 20%)
    tasks       = select_backfill(idle_nodes) # from priority queue
    dispatch(tasks, idle_nodes)               # via Ray scheduler
    sleep(POLL_INTERVAL)                      # e.g. 30s
```

| Priority | Task type | Example |
|---|---|---|
| P0 | Interactive / user-facing | Active agent session, model serving |
| P1 | Foreground research | Agent-driven literature review, code synthesis |
| P2 | Background research | Continuous experiment runs, fine-tuning |
| P3 | Idle-fill | Folding@Home, BOINC protein folding, open science |

P0 tasks preempt lower-priority backfill via `ray.cancel()` within seconds.

### Layer 4 — Agent Workloads

- **Autonomous literature agents** — crawl arXiv, Semantic Scholar, preprint servers; summarize and surface findings
- **Code synthesis agents** — draft, test, and refine implementations from problem statements or API specs
- **Experiment design agents** — propose and queue ML experiments; execute and analyze results
- **Fine-tuning pipelines** — continuous LoRA fine-tuning on curated domain corpora using idle GPU time
- **Simulation / evolutionary search** — CPU-bound optimization workloads dispatched to Surfaces, Mac Pro, Linux laptop

---

## Hardware Fleet

| Node | OS | CPU | GPU/Accelerator | Role |
|---|---|---|---|---|
| DGX Spark (GB10) | Linux | ARM | 128GB unified GPU/CPU memory (Blackwell) | Primary inference + training |
| Linux Workstation ×2 | Linux | x86 | RTX 4090, RTX 3090 | GPU compute workers |
| MacBook Pro | macOS | Apple Silicon | MPS | Inference + dev |
| Mac Pro (tower) | macOS | Intel/ARM | AMD GPU | Background CPU+GPU agent work |
| Surface ×2 | Windows 11 | x86 | iGPU | CPU-only orchestration + light agents |
| Linux Laptop | Linux | x86 | iGPU | CPU worker + dev |

All nodes connected via **Tailscale** WireGuard mesh for stable IPs and sufficient bandwidth for Ray's object store.

---

## Implementation Roadmap

### Phase 1 — Foundation (Week 1–2)
- Install Ray on DGX Spark + both Linux workstations (CUDA nodes first)
- Add Mac Pro and MacBook Pro as CPU/MPS nodes
- Add Linux laptop and Surfaces as CPU-only workers
- Validate cluster with a simple `ray.get()` test task dispatched from every node
- Stand up Ray Dashboard (port 8265) as initial monitoring surface

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

---

## Monitoring

- **Ray Dashboard** — per-node CPU/GPU utilization, task throughput, actor state, object store usage across the whole cluster
- **NVIDIA DCGM** — per-GPU compute, VRAM, power draw, and error metrics fed into the saturation loop's idle-detection logic

---

## References

- [Nalar: A Serving Framework for Agent Workflows — arXiv 2601.05109](https://arxiv.org/html/2601.05109v1)
- [Ray Documentation](https://docs.ray.io)
- [llama.cpp RPC README](https://github.com/ggml-org/llama.cpp/blob/master/tools/rpc/README.md)
- [exo — Run frontier AI locally](https://github.com/exo-explore/exo)
- [NVIDIA DGX Spark Multi-Node Clustering](https://developer.nvidia.com/blog/run-local-ai-agents-with-faster-models-and-multi-node-clustering-on-nvidia-dgx-spark/)
