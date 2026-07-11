# Saturate — Principles

> System-design principles. They **descend from `DOCTRINE.md`** — each names
> the article it compiles from, so the descent is visible and checkable.
> Include this file in every design review and every ralplan run on this
> repo. A proposal that crosses a principle is wrong until the principle
> itself is rectified; a proposal that crosses *doctrine* is wrong, full stop.
>
> These are the ground, not a checklist — the place they live is the *next
> decision*. If one stops being true under lived weight, rectify it here and
> say who and why.

## P1 — Git operations are always scoped to an explicit target, never ambient cwd

*(descends from I — Filesystem isolation.)* Every git operation the runner
performs (`add`, `commit`, `checkout`, revert) takes an explicit working
directory: the cloned worktree if `spec.repo` is set, or a no-op if it is
not. There is no code path that walks up from the process's current working
directory to find *a* git root and operate on whatever it finds. If a
change would reintroduce ambient-cwd git discovery anywhere in the runner
or executor, it is wrong regardless of how convenient it looks.

**Failure it prevents:** a runner invoked from inside the Saturate repo
finding and reverting the Saturate repo's own uncommitted work — the
original incident this doctrine was written to close.

## P2 — Subprocess environments are constructed explicitly, never inherited wholesale

*(descends from II — Environmental isolation.)* When Saturate spawns a
worker subprocess (a `HermesExecutor` invocation, a `shell` executor), the
environment it passes is built deliberately: the Saturate signals the
subprocess needs (`SATURATE_TASK`, `SATURATE_TASK_ID`, `SATURATE_QUEUE_DIR`)
are set explicitly, and signals belonging to a different context — a
Kanban dispatch this call happens to be nested inside, for instance — are
cleared, not passed through by default. `**os.environ` alone is not a safe
starting point; it is how one context's routing signal becomes another
context's misrouting bug.

**Failure it prevents:** `HERMES_KANBAN_TASK` bleeding from an outer Kanban
context into an inner Saturate-routed subprocess, causing the subprocess's
own backend detection to route to the wrong queue silently — no error, just
wrong behavior.

## P3 — Verify external contracts at the boundary; never assume their shape

*(descends from III — Conformance, not assumption.)* Before importing a
name from `loop_spec` (or any dependency Saturate does not own), confirm it
actually exports what is expected. If an assumption turns out wrong — a
class documented but never implemented, a field renamed upstream — the fix
is a local, explicitly-named fallback with a comment stating the gap, not a
silent local reimplementation that quietly diverges forever. The gap gets
reported upstream (loop-spec) so the fallback can eventually retire.

**Failure it prevents:** `TurnResult` assumed importable from `loop_spec`
when it was not in every installed version — caught, and fixed with a
locally-defined class explicitly labeled as a stopgap, not silently patched
around with no trace of the divergence.

## P4 — The queue is the only source of truth; nothing state-bearing lives elsewhere

*(descends from IV — Auditability.)* Task status, turn history, cumulative
spend, terminal reason — every one of these is a column or row the queue
persists, never a value held only in a running process's memory or emitted
only to a log line. A scheduler restart must be able to reconstruct the
entire fleet's state from the queue alone.

**Failure it prevents:** state that silently disappears on a crash because
it was never durable in the first place — an audit trail with holes in it
is not an audit trail.

## P5 — Budget enforcement happens at claim(), never inside the worker

*(descends from IV — Auditability, and the distributed-multiplier
constraint.)* A worker cannot police its own aggregate spend when N workers
are running concurrently — N independent workers each checking a soft
budget can each pass the check and collectively blow through the ceiling
simultaneously. The queue is the only vantage point that sees every
claimant; `claim()` is therefore the only place enforcement can be correct.

**Failure it prevents:** the distributed multiplier — soft, worker-side
budget checks that are individually correct and collectively meaningless.

## P6 — The four-operation interface is the only contract a consumer needs

*(descends from V — Backend-agnosticism.)* `post`, `claim`, `write_state`,
`complete` are the complete surface a producer or worker should ever need
to touch. A backend swap (file-based → SQLite → PostgreSQL) is
configuration, never a rewrite. If a skill or worker starts special-casing
"when running on Kanban, do X; when running on SQLite, do Y," the interface
has already leaked.

**Failure it prevents:** consumer code that only works against one backend
implementation, defeating the entire point of the published interface.

## P7 — `HUMAN_GATED` is enforced structurally at the queue, not by convention

*(descends from IV — Auditability, and directly from the loop-spec
`ClarificationKind` contract.)* A task whose spec is `ClarificationKind`
has `human_gated=1` set automatically at `post()` time by reading the spec
— not by the caller remembering to pass a flag. `complete()` raises
`HumanGatedViolation` unless `confirmed_by_human=True` is explicit. No
prose instruction, no worker discipline, no code review catches this by
convention as reliably as a structural raise does.

**Failure it prevents:** a `ClarificationKind` loop terminating without an
actual human confirming it — the exact violation the loop kind exists to
make impossible.

## P8 — Every subprocess call's cwd is the isolated worktree, with no exceptions

*(descends from I — Filesystem isolation, extended.)* Not just git
operations — the `evaluate` command, the `correctness` command, and any
future subprocess the runner spawns all receive an explicit `cwd`: the
cloned worktree, or the task's own state directory as fallback. This
principle exists because the *first* fix for Article I closed the git path
but a *second* incident found the same class of bug in `measure()` and
`correctness` subprocess calls, which were still running in ambient cwd
and dirtying `uv.lock`. One instance of a class-level bug does not mean the
class is closed — grep for every subprocess call before declaring isolation
done.

**Failure it prevents:** declaring isolation solved after fixing the first
discovered instance, while sibling call sites carry the identical bug.

## P9 — loop-spec is a shared contract; Saturate proposes, never forks

*(descends from the refusal — "not the owner of the loop spec.") When
Saturate needs a capability the current schema doesn't have (`repo` as a
git URL, `BudgetSpec`, the `human` executor type), the field is proposed as
a loop-spec PR first — reviewed, merged, released — and Saturate then
depends on the new version. Saturate never adds a Saturate-only field to a
locally-forked copy of the schema to route around a temporary gap.

**Failure it prevents:** loop-spec and Saturate's understanding of the
schema silently diverging, so that a spec valid for one is invalid for the
other — the exact drift a shared open standard exists to prevent.

---

*Authored 2026-07-11, grounded in incidents from the PR #4, #6, #7, #8 arcs
(git isolation, TurnResult conformance, environment leakage across nested
Kanban/Saturate dispatch). Forge ⚒️ + Donald.*
