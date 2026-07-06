# Saturate

Distributed loop execution fabric — keeps a heterogeneous CPU fleet
continuously running useful agentic loops toward declared goals.

---

## What It Is

Saturate takes a loop spec (a plain YAML file), schedules it onto an idle node
in your fleet, and runs the hypothesis/measure/keep-or-revert cycle until a
terminal condition is met:

```
loop spec committed → Saturate schedules it → loop runs on idle CPU
```

A loop is not a task. It iterates: generate a hypothesis, apply it tentatively,
measure whether the metric improved, keep or revert, repeat. Loops run across
idle CPU cores on whatever machines you have, making continuous progress toward
declared goals. When loops complete, they can spawn child loops. The fleet
self-directs.

---

## Key Concepts

| Concept | Description |
|---|---|
| **Loop spec** | Plain YAML file declaring goal, metric, budget controls, terminal states |
| **Loop kind** | The typed iteration pattern: `metric-optimization`, `task-execution`, `information-seeking`, `clarification`, `consensus`, `selection` |
| **Goal directory** | `goals/` — drop a spec file, Saturate picks it up |
| **SaturateTask** | The runtime work item — self-similar, hierarchical via `depends_on` / `spawned_by` |
| **saturate.measure** | Built-in primitive: runs a command, returns a scalar with four outcomes |
| **Meta-loop** | The scheduler tick: survey fleet, dispatch eligible tasks, harvest completions |

---

## Project Structure

```
saturate/
├── VISION.md              the thesis and direction
├── ARCHITECTURE.md        components, loop taxonomy, design decisions
├── README.md              this file
│
├── goals/                 active loop specifications
│   ├── index.yaml         priority ordering, active/paused status
│   └── <name>.yaml        one spec per declared objective
│
├── saturate/              Python package
│   ├── meta.py            scheduler / meta-loop
│   ├── runner.py          loop runner (one turn per invocation)
│   ├── measure.py         saturate.measure — scalar metric primitive
│   ├── queue.py           durable queue (SQLite → PostgreSQL)
│   └── goals.py           goal directory reader
│
├── docs/
│   ├── Saturate  Architecture Reference.md   original research reference
│   └── distributed_compute_requirements.md  technology evaluation (complete)
│
└── tests/
```

---

## Dependencies

- [Ray](https://www.ray.io) — distributed compute (Phase 1: optional; Phase 2+: fleet scheduling)
- [Nomad](https://www.nomadproject.io) — fleet node management (Phase 2+)
- [Tailscale](https://tailscale.com) — fleet networking mesh
- SQLite (embedded, Phase 1) / PostgreSQL (fleet mode, Phase 2+)

---

## Docs

- [VISION.md](VISION.md) — thesis, goals, and direction
- [ARCHITECTURE.md](ARCHITECTURE.md) — loop taxonomy, components, design decisions
- [docs/distributed_compute_requirements.md](docs/distributed_compute_requirements.md) — technology evaluation (SQLite/PostgreSQL + Nomad selected)
