# AGENT.md — Saturate

Operational guide for any agent (or human) picking up this repo cold.
Read this before touching anything. Then read `DOCTRINE.md`.

---

## What this repo is

**Saturate** is a distributed loop execution fabric — it runs
`loop-spec`-conformant loops across idle compute, manages the
hypothesis/measure/keep-or-revert cycle, and keeps a fleet saturated with
useful agentic work. It is not an agent framework. It is not a workflow
orchestrator. See `DOCTRINE.md` for the founding claim.

Companion repos:
| Repo | Local path | What |
|------|-----------|------|
| **`witt3rd/saturate`** | `~/src/witt3rd/saturate/` | This repo |
| `witt3rd/loop-spec` | `~/src/witt3rd/loop-spec/` | Shared loop schema (neither owns it) |
| `witt3rd/hermes-cyclus` | `~/src/witt3rd/cyclus/` | Deliberation layer — Cyclus designs, Saturate executes |

---

## Governance

Read in order:
1. `DOCTRINE.md` — the founding claim and its articles. When a proposal
   contradicts doctrine, the proposal is wrong.
2. `PRINCIPLES.md` — nine design principles descending from doctrine. Include
   in every design review and every `cyclus-plan` run on this repo.
3. `ARCHITECTURE.md` — components, loop taxonomy, queue interface, swarm topology.

---

## Development

```bash
pip install -e ".[dev]"
python -m pytest -q        # must be green before any commit
```

CI requires all three gates: `Test (Python 3.11)`, `Test (Python 3.12)`, `Lint`.
Ruff format is enforced — run `ruff format saturate/` before pushing.

**Commit before running pytest.** The integration tests exercise `run_turn`
end-to-end; the runner's `_git_revert()` operates on the isolated worktree
declared in `spec.repo`, but uncommitted changes to the Saturate source tree
will trip `test_integration_does_not_touch_saturate_repo`. Commit first,
then test.

---

## How work gets done here — Loop-Driven Development

**All non-trivial implementation work is loop-driven using Cyclus.**

This is not advisory. The design doc `plans/9-node-readiness-design.md` was
produced by a `cyclus-plan` (ConsensusKind) run. Every implementation arc
from that design onwards uses `cyclus-loop` (TaskExecutionKind) with the
**Kanban backend** as the dispatcher.

### The pattern

1. **Design first** — run `cyclus-plan` to produce a `DOCTRINE.md`-grounded
   design before touching code. Every design issue should have a plan in
   `plans/<issue>-design.md` before implementation starts.

2. **Write failing tests first** — before any implementation, write the tests
   that prove the feature actually works. These go in `tests/` alongside
   existing tests. Watch them fail. Then implement against them.

3. **Loop the implementation** — use `cyclus-loop` with `TaskExecutionKind`
   and the Kanban backend. Each task in the plan is one `delegate_task`
   invocation. No task larger than one subagent's 600s budget.

4. **Kanban backend, not file-based** — Saturate implementations use Cyclus
   wired to Kanban so the loop state is durable and the dispatcher handles
   stale-task reclaim automatically. See `cyclus-saturate-integration` skill
   for the env contract.

### Practical setup

```bash
# Check Cyclus is available and Kanban is reachable
hermes cyclus_queue action=status mode=saturate-impl instance_id=test

# Load the plan into the loop
hermes kanban create \
  --title "saturate: <issue name>" \
  --body "spec: plans/<issue>-design.md" \
  --goal-mode

# Dispatch the first task
hermes kanban dispatch
```

The `cyclus-saturate-integration` skill (in the forge profile) has the full
env contract, the `HERMES_KANBAN_TASK` isolation requirement for nested
dispatch, and the known bugs from the 2026-07-09 integration arc.

### Bypass declaration

If direct work is correct (active pairing session, single-line fix), declare
it explicitly in your commit message or session notes — per `docs/ldd.md`:

```
# Direct — active pairing, fix is < 5 lines
```

Undeclared bypasses are invisible failures of the LDD discipline.

---

## Repository layout

```
AGENT.md              This file — read first
DOCTRINE.md           Founding doctrine — articles I-VI (VI reserved → #9)
PRINCIPLES.md         Nine design principles
ARCHITECTURE.md       Components, loop taxonomy, swarm topology, design decisions
README.md             User-facing overview

saturate/             The Python package
  __init__.py         Public exports (load_spec, Queue, SqliteQueue, run_turn, ...)
  cli.py              saturate start / run / submit / status
  queue.py            File-based queue (four-operation interface)
  queue_sqlite.py     SQLite queue — spec-aware post(), HUMAN_GATED, BudgetExhausted
  runner.py           One-turn hypothesis/measure/keep-or-revert cycle
  runner_proc.py      Subprocess entry point (_launch_runner spawns this)
  executor.py         HermesExecutor, ShellExecutor, ExecutorResult
  measure.py          saturate.measure — scalar metric primitive
  scheduler.py        Tick-based scheduler: seed, dispatch, reclaim, harvest
  task.py             SaturateTask dataclass + dispatch_score()

tests/                Pytest suite — run with `python -m pytest -q`
plans/                Design docs produced by cyclus-plan runs
  9-node-readiness-design.md   Design for issue #9 — ready for implementation
goals/                Loop spec files — drop here to seed the scheduler
```

---

## Open issues

| # | Title | Status |
|---|-------|--------|
| [#9](https://github.com/witt3rd/saturate/issues/9) | Node readiness / precondition check | Design complete (`plans/9-node-readiness-design.md`), implementation pending |
| [#2](https://github.com/witt3rd/saturate/issues/2) | Multi-loop collision detection for overlapping target_files | Phase 2, blocked on concurrent fleet workers |

**DOCTRINE.md Article VI** is reserved for #9 — write it when #9 closes,
from what the fix actually established, not before.

---

## Key invariants (from DOCTRINE.md)

- **Git ops always use `spec.repo`** — never ambient cwd. If you see
  `_find_git_root()` or `cwd=os.getcwd()` in runner/executor code, that is a
  doctrine violation.
- **Subprocess envs are constructed explicitly** — never `**os.environ` alone.
  Clear `HERMES_KANBAN_TASK` when spawning HermesExecutor subprocesses.
- **Verify external contracts** — don't assume `loop_spec` exports a name
  without confirming it. See `TurnResult` incident in PR #6.
- **The queue is the only source of truth** — state in a Python object or log
  line is not durable. If it matters, it is in the queue.
- **Four-operation interface only** — `post`, `claim`, `write_state`,
  `complete`. No backend-specific calls in skills or workers.
