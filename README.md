# Saturate

## The Problem

You have machines sitting idle.

Your workstations sleep overnight. Your laptops sit at low CPU between meetings.
That DGX Spark runs inference for a few hours and then idles at 5% utilization.
Meanwhile, the agentic loops that could be running — build optimizers, research
agents, code synthesis, test generators — need someone to babysit them. You start
one, watch it, restart it when it stalls. Scale stops at one loop per terminal
window.

The world moved to GPU and forgot about CPU. Most agentic loops don't need a GPU —
they coordinate, call frontier AI APIs, run tools, measure results. That's CPU and
network. The compute sitting idle across your fleet is sufficient for hundreds of
concurrent loops. Nobody has built the distributed layer.

## The Solution

Saturate keeps your fleet running useful agentic loops continuously, without
babysitting.

You declare goals. Saturate schedules loops onto idle nodes, runs the
hypothesis/measure/keep-or-revert cycle, tracks every iteration, handles
failures and retries, and keeps the fleet saturated. Loops spawn child loops.
The fleet self-directs toward declared objectives.

```
goal declared → loop spec committed → Saturate schedules it
    → loop runs on idle CPU → hypothesis → measure → keep/revert
    → terminal condition hit → harvest output → spawn follow-on loops?
    → fleet picks up the next pending loop
```

## Why use this?

- **Scales what loop engineering already proves.** The hypothesis/measure/keep-or-revert
  pattern produces real results. Saturate takes it from one machine and one terminal
  to every idle CPU you have.
- **CPU-first.** Most agentic loops call frontier AI APIs — they need coordination,
  not GPUs. GPU is one scheduling tag for the minority that need local inference.
  Your idle CPU fleet is the bottleneck nobody talks about.
- **Any agent, any framework.** Declare your executor in the loop spec — a Hermes
  profile, a shell script, an HTTP endpoint. Saturate launches it, monitors it,
  and recovers from crashes. No SDK to import.
- **Self-similar hierarchy.** Loops spawn loops. A literature survey that completes
  spawns refinement loops on its most promising threads. The fleet self-directs.
- **Zero idle cycles by design.** The scheduler tick continuously surveys the
  fleet, dispatches eligible loops to idle nodes, and harvests completions. When
  there's work to do, nothing sits idle.

---

## Getting Started

```bash
pip install -e ".[dev]"

# Drop a loop spec into goals/
cat > goals/build-optimizer.yaml << 'EOF'
name: build-optimizer
kind: metric-optimization
goal: Reduce CI build time by at least 20%
metric:
  command: npm run build
  extract: wall_clock
  direction: minimize
correctness:
  command: npm test
executor:
  type: shell
  command: ./agents/optimizer.sh
max_turns: 100
stagnation_n: 10
memory: ./output/build-optimizer/
EOF

# Submit and run
saturate submit goals/build-optimizer.yaml
saturate run --loop <task_id>

# Or run the scheduler continuously (picks up goals/ automatically)
saturate start
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full loop spec format, executor
types, and fleet configuration.

---

## Loop Kinds

Six typed loop kinds, each with its own spec schema and terminal conditions:

| Kind | What one turn does | Terminates when |
|---|---|---|
| `metric-optimization` | Hypothesis → apply → measure → keep/revert | Target hit, stagnation, budget |
| `task-execution` | Execute next task from a plan file | All plan tasks complete |
| `information-seeking` | Search until sufficiency gate passes | Sufficient evidence |
| `clarification` | Socratic dialogue | Human calls `complete()` |
| `consensus` | Multi-role deliberation | Agreement reached |
| `selection` | Generate candidates, score, converge | Convergence or budget |

---

## Executor Types

Every loop spec declares how its agent is invoked:

```yaml
executor:
  type: hermes       # Hermes agent profile
  profile: forge

executor:
  type: shell        # Any executable, context via SATURATE_* env vars
  command: ./my-agent.sh

executor:
  type: http         # POST TurnContext JSON, receive TurnResult JSON
  url: http://localhost:9000/turn
```

Workers don't import a Saturate SDK. Any language, any framework.

---

## Architecture

- **Durable queue** — embedded SQLite (single-node, zero config) → PostgreSQL
  (fleet mode, atomic `SELECT FOR UPDATE SKIP LOCKED`)
- **Fleet scheduler** — routes loops to idle nodes by resource requirements and
  priority; Phase 2+ uses [Nomad](https://www.nomadproject.io) for node management
- **`saturate.measure`** — scalar metric primitive: runs a command, returns
  `improved / regressed / crashed / unchanged` (crashed ≠ regressed, never
  contaminates the baseline)
- **Networking** — [Tailscale](https://tailscale.com) mesh across heterogeneous
  nodes (Linux, macOS, ARM, x86)

→ [VISION.md](VISION.md) — the full thesis  
→ [ARCHITECTURE.md](ARCHITECTURE.md) — components, loop taxonomy, design decisions

---

## Status

**Phase 1 complete** — single-node loop execution, SQLite queue, scheduler tick,
`saturate start` command, 139 tests.

**Phase 2 in progress** — PostgreSQL fleet queue, Nomad node management,
multi-node concurrent loop execution.
