# Saturate — Vision

## The Problem

Loop engineering is having a moment. The pattern is everywhere: an agent
generates a hypothesis, applies it, measures the result, keeps or reverts, and
repeats. Metric-driven, autonomous, continuous improvement toward a declared
goal. It works. Teams are shipping real results with it.

But every implementation runs the same way: **one orchestrator, sub-agents, one
machine.** The loop lives on a single box. When it stalls, you restart it. When
you want to run two loops, you open another terminal. When you want to run a
hundred loops across your engineering organization's idle compute — you're on
your own.

That is the gap Saturate fills.

---

## The Thesis

**Current loop engineering scales down. Saturate scales up.**

The same loop spec that runs on your laptop today runs across a thousand nodes
tomorrow — without re-architecture, without rewriting your agents, without
learning a new framework. You declare goals. Saturate keeps the fleet running
toward them, continuously, at whatever scale you have.

And here is the insight the GPU narrative buried: **most agentic loops don't
need a GPU.** They coordinate, call frontier AI APIs (Claude, GPT, Gemini),
execute tools, measure results. That is CPU work and network I/O. GPU is one
scheduling characteristic for the minority of loops that need local inference.
The bottleneck the field is ignoring is CPU capacity — and across any
organization, it is enormous and almost entirely idle.

---

## What Saturate Is

Saturate is an **open source distributed loop execution fabric**.

You define a loop goal — what you are optimizing, how to measure it, when to
stop. Saturate schedules that loop onto available compute, runs the
hypothesis/measure/keep-or-revert cycle, tracks every iteration, manages
failures and retries, and keeps the fleet saturated with useful work toward
your declared goals.

```
define goal + metric
submit loop spec
              ↓
   Saturate schedules across fleet
              ↓
   loop runs: hypothesis → apply → measure → keep/revert → repeat
              ↓
   terminal condition hit → harvest output → spawn follow-on loops?
              ↓
   fleet picks up the next pending loop
```

The fleet never idles as long as there is work to do.

---

## The Loop Is the Unit of Work

Not a task. Not a job. A **loop** — self-contained, iterating, measuring,
rolling back bad hypotheses, running until it hits a declared terminal
condition:

| Terminal state | Meaning |
|---|---|
| `success` | Optional target threshold reached |
| `stalled` | N consecutive turns with no accepted improvement |
| `exhausted` | Hard iteration or budget ceiling hit |
| `blocked` | Unresolvable dependency |
| `cancelled` | Operator stopped it |

Loops spawn child loops. A literature survey loop that completes might spawn
three refinement loops on its most promising threads. A build-optimization loop
that plateaus might spawn a goal-decomposition loop to find a better angle. The
fleet self-directs toward the declared objectives.

---

## The Scale Spectrum

The same loop spec runs at every scale. You decide how much compute to bring:

| Scale | Setup | Loops |
|---|---|---|
| Solo developer, one machine | `saturate start` | tens |
| Home lab or small team | Tailscale mesh, a few nodes | hundreds |
| Engineering organization | Fleet of workstations + cloud VMs | thousands |

No re-architecture between levels. No rewriting your agents. Add a node,
it joins the fleet. Remove one, in-flight work is reclaimed and rescheduled.

---

## What Saturate Is Not

**Not a workflow orchestrator** (Temporal, Airflow, Prefect). Those manage
deterministic task DAGs for business logic that must complete exactly once.
Saturate runs non-converging metric-optimization loops that run until
externally stopped. Different problem, different design.

**Not a distributed ML training framework** (Ray, Horovod). Those pool GPU
capacity across nodes for a single large model run. Saturate routes independent
loops to individual nodes. It does not aggregate resources across nodes for one
workload.

**Not tied to any specific agent framework**. Saturate defines a loop spec
format and a four-operation API. Anything that produces a conforming spec can
submit work: a deliberation tool, a shell script, a Python function, a custom
agent framework. Saturate runs it.

---

## The Interface

Saturate exposes four operations. Any tool that implements these can act as a
worker; any tool that calls `post()` can submit work:

```
post(loop_spec)    →  submit a loop to the queue
claim()            →  a worker atomically claims the next available loop
write_state(item)  →  record iteration progress
complete(item)     →  mark terminal, attach output and metadata
```

Workers are external processes — arbitrary executables, Python scripts,
compiled binaries. Saturate launches them, tracks them, recovers from their
crashes. Workers do not import a Saturate SDK.

---

## The Loop Spec

A loop is declared in a plain YAML file. Any tool can produce one:

```yaml
name:           build-optimizer
goal:           Reduce CI build time by at least 20%
metric:
  command:      npm run build
  extract:      wall_clock
  direction:    minimize
correctness:
  command:      npm test
max_turns:      100
budget_tokens:  500000
stagnation_n:   10
terminal_states: [success, stalled, exhausted]
memory:         ./output/build-optimizer/
```

Saturate reads this spec, manages the execution, and writes findings to
`memory`. The spec is immutable during execution — Saturate never modifies it.

---

## Who Is This For

**Individual engineers** running multi-day optimization loops on their own
hardware without babysitting a terminal.

**Small teams** who want to saturate idle workstations with continuous research,
code synthesis, or experiment-design loops overnight.

**Engineering organizations** who want to run hundreds of concurrent autonomous
improvement loops across their fleet — build optimization, test synthesis,
documentation generation, security scanning — all running continuously toward
declared quality goals.

**Agent framework builders** who want a battle-tested, open source execution
backend for loop-shaped workloads without building distributed infrastructure
from scratch.

---

## Why Now

Three things converged in 2026:

1. **Loop engineering proved out.** The hypothesis/measure/keep-or-revert
   pattern produces real results on real codebases. It is no longer speculative.

2. **Frontier AI became an API.** Most loops call Claude, GPT, or Gemini.
   They do not need local GPU. They need CPU to coordinate and network to call
   the API. The compute sitting idle in every engineering organization is
   sufficient.

3. **Nobody built the distributed layer.** Every loop engineering system today
   is single-machine. The distributed execution problem is unsolved and the
   opportunity is wide open.

Saturate is the distributed layer.
