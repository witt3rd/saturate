# Saturate — Doctrine

> The founding claim: what Saturate **is** and the boundary it never crosses.
> Everything else — principles, architecture, code — descends from here.
> Doctrine rarely changes; when a design decision contradicts it, the design
> is wrong, not the doctrine.
>
> Forged at the bench. Donald + Forge, 2026-07.

## The founding claim

**Saturate is the execution fabric that never becomes part of what it executes.**

A fabric that saturates a fleet with useful loops must sit at a hard remove
from the work those loops do. The moment the fabric's own state — its
source tree, its process environment, its identity — bleeds into the work
it is running, the fabric has stopped being a fabric and started being a
participant. A participant can corrupt the thing it is supposed to be
running impartially. The fabric cannot be allowed to.

This is not a preference for tidiness. It is the property that makes
"distributed" and "fleet" and "self-directing" mean anything at all — a
fabric that leaks itself into its workers cannot be trusted to run
hundreds of them unattended.

## The claim unfolds

The founding claim is not a slogan; it is generative. Every real incident
Saturate has survived is this claim being violated in a specific way — and
each violation names an article.

### I — Filesystem isolation

**The fabric's source tree is never the working directory of the work it
runs.** Loops that commit and revert hypotheses declare a `repo` — a git
URL, not a local path — and the fabric clones it into an isolated worktree.
Every git operation (add, commit, checkout, revert) is scoped to that
worktree. `_find_git_root()` walking up from ambient `cwd` was the original
crime: a runner invoked from inside the Saturate repo will find the Saturate
`.git` and revert the Saturate repo's own uncommitted changes. The fix was
not "be more careful" — it was to make the ambient-cwd path structurally
unreachable and require an explicit target.

### II — Environmental isolation

**A spawned worker subprocess never inherits a signal meant for a different
context.** When a Saturate-driven `HermesExecutor` subprocess is launched
from inside a session that is itself running under Kanban (a nested
dispatch — the live-e2e test surfaced this), the ambient environment can
carry `HERMES_KANBAN_TASK` from the *outer* context. If that variable
passes through unfiltered, the inner subprocess's own backend-detection
logic misreads it and routes to the wrong backend — Kanban instead of
Saturate — silently. The fabric must construct each subprocess's
environment deliberately: pass through what belongs, clear what doesn't.
Inheriting `os.environ` wholesale is not neutral; it is how contexts bleed.

### III — Conformance, not assumption

**Saturate verifies the external contracts it depends on; it does not
assume their shape.** loop-spec is the source of truth for the loop
schema — Saturate imports from it rather than reimplementing it. But an
import assumption can be wrong: `TurnResult` was assumed exported from
`loop_spec` and was not, in every installed version. The fix is not to
silently work around a broken assumption forever — it is to notice the gap,
define the fallback explicitly, and name the drift so it gets closed at the
source (loop-spec) rather than papered over indefinitely at the consumer.

### IV — Auditability

**Every claim, every dollar and token spent, and every outcome is
reconstructable from the queue alone.** The queue is not a convenience
cache in front of some other source of truth — it *is* the source of
truth. State that only exists in a Python object, a log line, or a
worker's memory is state that disappears the moment that process exits.
If it matters, it is in the queue.

### V — Backend-agnosticism

**The same loop runs identically regardless of which queue implementation
executes it.** File-based, SQLite, Kanban, PostgreSQL fleet mode — a worker
or producer written against the four-operation interface (`post`, `claim`,
`write_state`, `complete`) never needs to know or care which one is live.
The moment a skill or worker special-cases a backend, this article has
been violated.

## The refusals

**Not a workflow orchestrator.** Temporal, Airflow, Prefect manage
deterministic task DAGs that must complete exactly once. Saturate runs
non-converging metric-optimization loops that run until externally
stopped. Different problem, different design — never collapse the two.

**Not a distributed training framework.** Ray and Horovod pool GPU
capacity across nodes for one large model run. Saturate routes
*independent* loops to *individual* nodes. It never aggregates resources
across nodes for a single workload.

**Not tied to any agent framework.** Any conforming producer can submit a
loop spec; any conforming executor can run a turn. Saturate defines the
contract, not the implementation behind it. A design that requires workers
to import a Saturate SDK has already broken this.

**Not the owner of the loop spec.** loop-spec is a shared standard that
Cyclus and Saturate both consume and both extend. When Saturate needs a new
field, the field is proposed in loop-spec first. Saturate never forks the
schema locally to route around a gap — a local fork is exactly the kind of
silent divergence Article III exists to prevent.

---

⚒️ Isolation is the claim; the rest descends.
