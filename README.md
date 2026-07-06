# Saturate

Distributed loop execution fabric — keeps a heterogeneous CPU fleet
continuously running useful agentic loops toward declared goals.

---

## What It Is

Saturate takes a `<name>-loop.md` specification (produced by
[oh-my-hermes](https://github.com/witt3rd/oh-my-hermes) deliberation), schedules
it onto an idle node in your fleet, and runs it:

```
goal declared → loop designed (OMH) → loop spec committed → Saturate runs it
```

A loop is not a task. It iterates: generate a hypothesis, apply it tentatively,
measure whether the metric improved, keep or revert, repeat. Loops run in the
background across idle CPU cores, making incremental progress toward declared
objectives. The meta-loop keeps the fleet saturated — dispatching new loops
when nodes go idle, reaping stalled ones, spawning child loops from completed
work.

The world moved to GPU. CPU is underutilized. Agentic orchestration —
coordination, tool calls, code execution, hypothesis generation — is CPU and
network bound. A seven-machine fleet with idle CPU cores is seven machines that
could be running useful loops right now.

---

## Key Concepts

| Concept | Description |
|---|---|
| **Loop** | A goal + metric + hypothesis engine + terminal condition, running until stopped or stalled |
| **Loop spec** | `<name>-loop.md` — the interface between OMH and Saturate |
| **Goal registry** | `goals/` directory of active loop specs |
| **Meta-loop** | The orchestrator: surveys fleet, dispatches loops, harvests output |
| **LoopRunner** | A Ray Actor — one per active loop, owns hypothesis/measure/keep-or-revert |
| **omh_measure** | Returns a scalar metric from a command, not a pass/fail |

---

## Project Structure

```
saturate/
├── VISION.md              the thesis and direction
├── ARCHITECTURE.md        this file
├── README.md              project overview
│
├── goals/                 active loop specifications
│   ├── index.yaml         priority ordering, active/paused status
│   └── <name>-loop.md     one spec per declared objective
│
├── saturate/              Python package
│   ├── meta.py            meta-loop orchestrator
│   ├── runner.py          LoopRunner Ray Actor
│   ├── measure.py         omh_measure — scalar metric primitive
│   ├── registry.py        in-memory loop registry (backed by Kanban)
│   └── goals.py           goal registry reader
│
├── docs/
│   └── Saturate  Architecture Reference.md   original research reference
│
└── tests/
```

---

## Dependencies

- [Ray](https://www.ray.io) — distributed compute substrate
- [Tailscale](https://tailscale.com) — fleet networking mesh
- [Hermes Kanban](https://github.com/NousResearch/hermes-agent) — durable loop
  state substrate
- [oh-my-hermes](https://github.com/witt3rd/oh-my-hermes) — loop design and
  deliberation (upstream, not a runtime dependency)

---

## Related Projects

- **[oh-my-hermes](https://github.com/witt3rd/oh-my-hermes)** — designs loops;
  Saturate runs them. The handoff is a `<name>-loop.md` spec file.
- **[continuum](https://github.com/witt3rd/continuum)** — persistent cognitive
  presence; natural fit for driving the meta-loop.

---

## Docs

- [VISION.md](VISION.md) — thesis, goals, and direction
- [ARCHITECTURE.md](ARCHITECTURE.md) — components, lifecycle, design decisions
- [docs/Saturate Architecture Reference.md](docs/Saturate%20%20Architecture%20Reference.md) — original research reference (Ray, Nalar, fleet hardware)
