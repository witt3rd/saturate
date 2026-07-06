# Saturate — Vision

## The Thesis

The world moved to GPU and forgot about CPU.

The accelerator narrative is correct for inference throughput. But autonomous
agentic work — hypothesis generation, code synthesis, research coordination,
measurement, git operations — is **CPU and network bound**, not GPU bound.
Inference tokens come from APIs or from whichever node has a GPU; what starves
is the orchestration layer wrapping them: Python coordination, subprocess
execution, file I/O, tool calls, iterative decision-making. A seven-machine
fleet with mostly-idle CPU cores is seven machines that could be running useful
work right now.

Saturate is the infrastructure that keeps them running it.

---

## The Unit of Work: A Loop

The unit of work in Saturate is not a task. It is a **loop** — a goal, a
metric, a hypothesis engine, and a terminal condition, running until externally
stopped or until it finds a plateau.

```
define goal + baseline metric
loop until stopped:
    generate hypothesis       →  agent proposes a change
    apply tentatively         →  try it
    measure                   →  did the metric improve?
    correctness gate          →  did it break anything?
    better AND correct?       →  commit, advance baseline
    else                      →  revert, record the dead end
```

This is the **autoresearch** pattern (Karpathy / Shopify shape): continuous,
autonomous improvement toward a declared objective, with deterministic
measurement and automatic rollback. Not a one-shot prompt. Not a plan with
finite tasks. A loop that runs in the background, making incremental progress,
filing every dead end in the audit trail.

Loops are the right unit because they map to how real research and engineering
work actually proceeds: iterative hypothesis testing, continuous improvement,
gradual convergence toward a goal that was always a direction rather than a
destination.

---

## The Goal Registry

Objectives are declared explicitly: reduce the build time of this project;
survey this research area and surface relevant findings; continuously refine
this codebase toward a quality threshold; explore this problem space and
generate candidate implementations.

Each objective becomes a loop specification. The **goal registry** is the set
of active objectives. Saturate's job is to keep the fleet running loops toward
those objectives at all times — dispatching new loops when nodes go idle,
reaping stalled ones, harvesting completed work.

---

## The Meta-Loop

Saturate itself is a loop — the **meta-loop** — that orchestrates all the
others.

```
while True:
    survey active loops         →  what's running, what's stalled, what completed
    harvest completed loops     →  collect output, spawn follow-on loops
    find idle CPU capacity      →  which nodes are underutilized
    select and dispatch         →  match pending loops to available nodes
    sleep and repeat
```

New loops enter the fleet two ways: submitted directly by the human (new goal),
or **spawned by a running loop** that discovered a sub-problem worth pursuing.
A literature-survey loop that completes might spawn three refinement loops on
the most promising threads. A loop that plateaus might spawn a
goal-decomposition loop to find a better angle. The fleet self-perpetuates
toward the declared objectives.

---

## The Design Layer: OMH

Running a loop is the easy part. **Designing a loop worth running is the hard
part** — and it is exactly the class of problem OMH exists to solve.

[oh-my-hermes](https://github.com/witt3rd/oh-my-hermes) is the deliberation
layer that produces loop specifications. Its `omh-loop-design` skill runs a
structured adversarial conversation — Socratic goal extraction, verification
strategy, terminal state definitions, blast-radius review, critic challenge —
and produces a `<name>-loop.md` spec that any OMH execution path can run.

**OMH designs loops. Saturate runs them.** The handoff is a file.

The two projects form a complete arc: deliberation upstream (OMH), execution
and distribution downstream (Saturate). Neither duplicates the other's job.

---

## The Continuum Connection

Saturate is the **compute fabric**. [Continuum](https://github.com/witt3rd/continuum)
is the **cognitive presence** that can direct it.

A running Continuum instance — a persistent cognitive loop with a goal survey,
fleet awareness, and judgment about what to spawn next — is a natural fit for
the meta-loop role. Saturate exposes a clean interface (loop spec files in,
harvested output files out, Kanban for state). Continuum can drive that
interface, or the meta-loop can run standalone. The two projects compose without
coupling.

---

## What Success Looks Like

You wake up. The fleet ran overnight. Your build is 12% faster. Your research
queue has 40 new findings tagged and summarized. Three candidate implementations
of the feature you care about were tried, measured, and the best one committed.
The loops that hit plateaus filed their findings and stopped cleanly. The nodes
that finished their loops spawned new ones from the completed work.

Zero idle cycles. Continuous progress. **You stayed the engineer** — you
declared the goals and reviewed the output. The loops did the work.

---

*CPU is the underutilized resource. Loops are the unit of work. Goals are the
direction. Saturate is the fabric that keeps them connected.*
