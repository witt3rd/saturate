# Saturate

**Distributed loop execution fabric for agentic work.**

You have machines sitting idle. Workstations sleep overnight. A DGX Spark runs
inference for a few hours then idles at 5%. The agentic loops that could be
running — build optimizers, research agents, test generators — need someone to
babysit them. You start one, watch it, restart it when it stalls.

Most agentic loops don't need a GPU. They coordinate, call frontier AI APIs,
run tools, measure results. That's CPU and network. The compute sitting idle
across your fleet is sufficient for hundreds of concurrent loops. Nobody built
the distributed layer.

**Saturate keeps your fleet running useful agentic loops continuously.**

---

## How it works

Declare a goal as a [loop-spec](https://github.com/witt3rd/loop-spec) YAML file.
Saturate schedules it onto available compute, drives the
hypothesis → measure → keep/revert cycle, tracks every iteration, and handles
failures and restarts automatically.

```
goal declared → loop spec validated → Saturate schedules it
    → loop runs on idle CPU → hypothesis → measure → keep/revert
    → terminal condition hit → harvest output → spawn follow-on loops?
    → fleet picks up the next pending loop
```

---

## Quick start

```bash
pip install -e ".[dev]"
```

Write a loop spec:

```yaml
# goals/coverage-optimizer.yaml
name: coverage-optimizer
kind: MetricOptimizationKind
direction: higher_is_better
metric: test coverage
repo: https://github.com/you/yourproject.git
evaluate: cd examples/yourproject && python -m pytest --co -q 2>/dev/null | tail -1
evaluate_extract: "regex:(\\d+)"
correctness: cd examples/yourproject && python -m pytest -q
terminal:
  max_iterations: 50
  plateau_count: 8
executor:
  type: hermes
  profile: forge
output_dir: ./output/coverage-optimizer/
```

Submit and run:

```bash
# Drop the spec in goals/ — scheduler picks it up automatically
saturate start --goals-dir goals/

# Or run one turn manually
saturate run <task_id>
```

---

## Loop kinds

Six typed loop kinds. Each has its own spec schema and terminal conditions.
All are declared in [loop-spec](https://github.com/witt3rd/loop-spec).

| Kind | What one turn does | Terminates when |
|------|--------------------|-----------------| 
| `MetricOptimizationKind` | Hypothesis → apply → measure → keep/revert | Target hit, plateau, or max_iterations |
| `TaskExecutionKind` | Execute next task from a plan file | All plan tasks complete |
| `InformationSeekingKind` | Search until sufficiency gate passes | Sufficient evidence |
| `ClarificationKind` | Socratic dialogue | Human explicitly confirms (HUMAN_GATED) |
| `ConsensusKind` | Multi-role deliberation | All roles approve |
| `SelectionKind` | Generate candidates, score, converge | Convergence or budget |

---

## Executor types

Every loop spec declares how its agent is invoked:

```yaml
executor:
  type: hermes       # Hermes agent profile
  profile: forge

executor:
  type: shell        # Any executable; context via SATURATE_* env vars
  command: ./my-agent.sh

executor:
  type: http         # POST TurnContext JSON, receive TurnResult JSON
  url: http://localhost:9000/turn
```

Workers don't import a Saturate SDK. Any language, any framework.

---

## The `repo` field

Loops that commit and revert hypotheses declare the target git repository as a
URL. Saturate clones it into an isolated worktree — the Saturate source tree
is never touched.

```yaml
repo: https://github.com/you/yourproject.git
# or: git@github.com:you/yourproject.git
# or: file:///home/dt/src/myproject  (local, for testing)
```

Absolute local paths are rejected. The spec is machine-agnostic.

---

## Architecture

- **Durable queue** — embedded SQLite (single-node, zero config); PostgreSQL
  for fleet mode (`SELECT FOR UPDATE SKIP LOCKED` for concurrent workers)
- **Spec-aware queue** — `post()` loads the loop spec and derives `name`,
  `kind`, and `human_gated` from it; the spec is the source of truth
- **HUMAN_GATED enforcement** — `ClarificationKind` tasks raise
  `HumanGatedViolation` if `complete()` is called without `confirmed_by_human=True`
- **Fleet scheduler** — routes loops to idle nodes by resource requirements
  and priority; Phase 2 uses Nomad for node management
- **`saturate.measure`** — scalar metric primitive: runs a command, extracts
  a number, returns `improved / regressed / crashed / unchanged`
  (`crashed ≠ regressed` — never contaminates the baseline)
- **Networking** — Tailscale mesh across heterogeneous nodes

→ [DOCTRINE.md](DOCTRINE.md) — the founding claim: isolation is what makes
  a fabric a fabric, not a participant
→ [PRINCIPLES.md](PRINCIPLES.md) — system-design principles descending from
  doctrine; the ralplan/design-review frame
→ [ARCHITECTURE.md](ARCHITECTURE.md) — components, loop taxonomy, design decisions

---

## Companion projects

- [**loop-spec**](https://github.com/witt3rd/loop-spec) — the open loop spec
  standard. Neither Saturate nor Cyclus owns it. Changes land there first.
- [**hermes-cyclus**](https://github.com/witt3rd/hermes-cyclus) — deliberation
  layer that designs work and produces loop specs. Cyclus designs; Saturate executes.
