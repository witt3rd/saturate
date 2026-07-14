# Architect Review — Node Readiness Precondition Check (saturate#9)

## Verdict: REQUEST_CHANGES

The overall shape is right — reuse `claim()`'s precondition-gate pattern and
`cancel()`'s terminal-state pattern rather than invent new machinery — and
every load-bearing factual citation I checked against the actual source is
accurate. But I found three concrete, must-fix gaps in the plan as written,
at least one of which is severe enough that implementing the stance
literally could reproduce the exact bug it exists to close (an unhandled
crash loop, or a silently no-op check). These are specific and fixable
without re-architecting; this is not a request to redesign, it's a request
to close three named holes before implementation starts.

## Strengths

**The central precedent claim is accurate, and it matters that it is.** The
entire "minimize bespoke" argument rests on `claim()` already doing "load
spec, gate a precondition, raise before granting the claim" for `budget`.
I read `claim()` (`queue_sqlite.py:284-366`) directly: it does exactly
this — `_load_spec(spec_path)` at 319, budget check at 320-341, `ROLLBACK`
+ `raise BudgetExhausted` at 328-332/337-341, all strictly before the
`UPDATE ... SET status = 'running'` at 348-351. The stance did not
misremember this. Same for `cancel()` (529-539): `UPDATE tasks SET
status='done', terminal_reason=?` with `f"cancelled: {reason}"` — an exact
match to the stance's citation. And `terminal_reason` values `"exhausted"`,
`"stalled"`, `"success"` are real, used in `runner.py:150,156,186,233,240`
across both the `MetricOptimizationSpec` and `TaskExecutionSpec` turn
paths. This stance was written from the actual code, not a recollection of
it, and it shows.

**The open-question-1 self-correction is a live demonstration of P3.** The
stance initially assumed `required_node_class`/`num_gpus` might already be
columns, then went and read `_DDL` (`queue_sqlite.py:90-124`) directly and
found they are not — confirmed: the real `_DDL` schema is `task_id, name,
kind, status, spec_path, state_path, output_path, priority, submitted_at,
completed_at, terminal_reason, metadata`, and `_TASK_COLUMNS`
(`queue_sqlite.py:71-88`) — the frozenset that decides what lands in a
dedicated column versus the metadata JSON blob — also omits both fields.
This is exactly P3's discipline ("verify external contracts at the
boundary... rather than assuming their shape") applied reflexively to the
stance's own design, and applied correctly.

**P9 compliance is real, not just claimed.** I read `loop_spec/__init__.py`
directly. `ExecutorSpec` (line 42) has fields `type, who, profile, command,
url` — nothing GPU- or node-related. `LoopSpec` and its six subclasses
carry no scheduling/placement concept at all. `required_node_class` and
`num_gpus` are genuinely Saturate-local (`task.py:52-54`), and the
axis-separation the stance draws — "who acts" (`ExecutorSpec.type`) versus
"where they're allowed to act" (`SaturateTask.required_node_class`) — holds
up against the real schema. This is the cleanest possible reading of
Article "not the owner of the loop spec": recognizing a concern was never
loop-spec's to carry, rather than either forking the schema or proposing an
unnecessary upstream field.

**The scope cut (defer a `requires: list[str]` DSL) is the right
engineering call, held to a real standard.** Two scalar fields that already
exist and are trivially comparable is meaningfully less design surface than
a shell-checkable-string requirements language (what's the failure mode if
the check command itself errors? version pinning? timeout?). Punting that
to a named follow-up rather than cramming it in is the "radically simpler
shape" test working as intended.

## Concerns

**A1 — `claim()`'s post-mismatch control flow is unspecified, and the
obvious literal reading of "reuse the existing precedent" reproduces the
bug this design exists to fix.** The stance's own cited precedent
(`BudgetExhausted`) is not actually closed anywhere in the codebase — I
grepped for it: it's raised at `queue_sqlite.py:329,338`, tested directly
against `pytest.raises` in `tests/test_queue_sqlite_spec.py:220,229`, and
**caught nowhere in production code** — not in `runner_proc.py`, not in
`scheduler.py`. Critically, `runner_proc.main()` calls `queue.claim()` at
line 53, *outside* the `try/except Exception` block that only wraps
`run_turn()` (lines 77-89). If `claim()` raises `BudgetExhausted` today,
that's an unhandled exception that crashes the subprocess with a Python
traceback, and — because budget rolls back rather than committing — the
task stays `pending` and gets re-attempted (and re-crashes) on every
subsequent tick. This is the same shape of unbounded-loop bug #9 exists to
fix, just for budget instead of node-mismatch, and it's live in the
codebase today, unnoticed.

The stance's step 2 says the node-mismatch path should mirror `cancel()`'s
DB write (`UPDATE ... status='done'`, which *commits*, unlike budget's
rollback) — that part is the right choice. But it never says what `claim()`
*returns or raises* after that commit. Three readings are all consistent
with the stance's text, and they behave very differently:
- **Raise** (mirroring budget's control flow) → propagates uncaught at
  `runner_proc.py:53`, same crash-traceback shape as the existing
  unclosed `BudgetExhausted` gap, even though the DB state is correctly
  terminal.
- **Return `None`** (as if no pending task existed) → handled gracefully
  by the existing `if claimed is None: ...; return 1` path, but silently
  discards the signal that a mismatch was actually resolved this cycle —
  an operator watching `runner_proc` stdout sees "no pending task" and
  "mismatch resolved" as identical output.
- **Return the task dict with `status: 'done'` already set** — the
  cleanest option, but the interface has no shape for "claim() looked at a
  task, found it can't be claimed to run, resolved it terminally, and
  nothing is running now" distinct from "there was nothing to claim."

Why it matters: whichever of these an implementer picks by following the
letter of the stance, two of the three are either a crash or a swallowed
signal. This is the single highest-leverage thing to nail down before
implementation, because it's exactly the seam where the design either
works cleanly or reproduces the incident it's supposed to close.

*Fix:* the stance should explicitly specify that `claim()` returns `None`
after committing the node-mismatch write, and that this is a *deliberate,
named* divergence from `BudgetExhausted`'s raise-based control flow (not
an accidental omission) — with a one-line note on why: node-mismatch is
resolved-to-terminal in this same call, so there is nothing left for the
caller to do except try the next tick, whereas budget's raise exists
because nothing else in the call chain currently handles the "this claim
attempt failed for a reason other than emptiness" case at all. While at
it: flag the existing unhandled `BudgetExhausted` propagation as a
separate, real, pre-existing bug worth its own follow-up issue — it is not
in scope for #9, but this review surfaced it as a direct consequence of
reading the same code path this design touches.

**A2 — `post()`'s hardcoded INSERT statement needs an explicit edit that
the minimum-viable shape never lists as a step.** I read `post()`
(`queue_sqlite.py:219-282`) directly. It is *not* a dynamic
insert-whatever's-in-`_TASK_COLUMNS` statement — it's a literal, hardcoded
`INSERT INTO tasks (task_id, name, kind, status, spec_path, state_path,
output_path, priority, submitted_at, human_gated, tokens_used, cost_usd,
metadata) VALUES (...)` (253-274) with an explicit `VALUES` tuple built
from named `task.get(...)` calls. Adding a column to `_DDL`/`_TASK_COLUMNS`
does *not* make `post()` populate it — the field will simply always be
`NULL` (or the schema default) for every task ever posted unless `post()`
is *also* edited to add the new field to both the column list and the
`VALUES` tuple.

This is directly evidenced by the stance's own cited precedent: when
`human_gated`/`tokens_used`/`cost_usd` were added (the pattern the stance
points to for `retry_count`), they required *both* the `ALTER TABLE`
migration *and* an edit to `post()`'s literal INSERT — which is exactly
why they appear in that statement today. The stance cites this precedent
approvingly but doesn't draw the conclusion that its own new columns need
the same second edit. Left as-is, the "minimum-viable first shape" section
would ship a migration and a `claim()` check that always evaluates against
`NULL`/default values — the columns exist, the check runs, and it never
finds a mismatch, silently. That's a worse failure mode than a crash: it
looks shipped and tested (schema exists, code compiles) while doing
nothing.

Note: `retry_count` itself does *not* need this — it's populated purely by
a `DEFAULT 0` at schema level and incremented later, never set at
`post()` time, so the stance is correct not to worry about it there.

*Fix:* add an explicit fourth step (or fold into step 2) naming exactly
this: edit `post()`'s column list and `VALUES` tuple to read
`task.get("required_node_class")` / `task.get("num_gpus", 0.0)` from the
caller's dict, the same way `human_gated` etc. are read today.

**A3 — The "minimum-viable first shape" section was never actually revised
to match its own self-correction, exactly the internal-consistency
question I was asked to check.** Open question 1 says: "This is now a
concrete, no-longer-uncertain part of the minimum-viable shape — see step
1 there, revise to name both new columns explicitly." But step 1, as
written, still reads: "Add `retry_count` column (migration, same pattern
as existing ALTERs)" — it names only `retry_count`, not
`required_node_class`/`num_gpus`. Step 2 still carries the *original*,
*pre-resolution* hedge verbatim: "...or add these as real DB columns the
same way `human_gated` etc. are — they need to be queryable at claim time,
which means they need to be columns, not just spec-file fields; confirm
this during implementation, it's the one place this stance is not fully
certain of the storage mechanism and flags it as an open question below."
That sentence is describing the *unresolved* state the open question
claims to have already resolved. The resolution happened in the
investigation; it did not make it back into the authoritative section of
the document. Not fatal — the correct technical content exists elsewhere
in the same file — but it's a real defect in the artifact, and precisely
the kind of thing that causes an implementer working from the
"Minimum-viable first shape" section in isolation (a very plausible way to
read this document) to miss the two-new-columns requirement that A2 above
also independently surfaces from a different angle.

*Fix:* mechanical — rewrite steps 1 and 2 to state plainly: two new
columns (`required_node_class TEXT`, `num_gpus REAL DEFAULT 0.0`) plus
`retry_count INTEGER NOT NULL DEFAULT 0`, three `ALTER TABLE` statements
in one migration, no hedging language remaining.

**A4 (minor) — "caught before a subprocess ever spawns" overstates where
the cost saving actually happens.** I traced the dispatch path in
`scheduler.py`: `_dispatch()` (178-243) selects a task and calls
`_launch_runner()` (276-295), which is the `subprocess.Popen` call — this
happens in the *scheduler's own process*. `claim()` runs *inside the
spawned child* (`runner_proc.main()`, line 53). So by the time the
node-mismatch gate inside `claim()` fires, the subprocess has already been
spawned — the gate cannot prevent that cost, because `claim()` doesn't run
until after it. What the gate *does* avoid is the expensive body of
`run_turn()` (git clone into a worktree, executor invocation — which may
be an LLM call — `measure()`/`correctness()` subprocess spawns). That's a
real and worthwhile saving, just not the one the stance's "What the
current code lacked" section names. This doesn't change the design, only
the accuracy of the cost argument in the writeup — worth correcting since
someone reading only that section might expect a leaner win than the
mechanism actually delivers (subprocess-spawn cost is cheap; `run_turn()`'s
body is where the real money is, and the gate does save that).

## Load-bearing decisions validated

- **Reuse `cancel()`'s exact `done`+`terminal_reason` mechanism rather than
  inventing a new queue status.** Confirmed the DB write shape is
  identical to existing code; four routes into one terminal state
  disambiguated by a free-text reason is a sound, minimal state machine
  and matches P4/P6's spirit (single vantage point for "why did this
  stop").
- **Keep `required_node_class`/`num_gpus` and any future
  requirement-DSL out of `loop_spec`.** Verified against the real
  `ExecutorSpec`/`LoopSpec` schema — there is no placement concept there
  to conflate with, and adding one would be the actual P9 violation the
  stance correctly avoids.
- **Process-lifetime-constant node probe, not per-dispatch re-probing.**
  The P4 reasoning holds: environment capability is re-derivable at any
  time from `nvidia-smi`/env var, so nothing is durably lost on a
  scheduler restart the way a task's history would be. This is not a
  cached fact masquerading as durable state; it's correctly treated as
  ambient environment, re-computed on demand.
- **Retry-count circuit breaker as a blunt, sufficient backstop rather
  than a crash-cause taxonomy.** Given the pre-check handles the
  known/declared case, distinguishing "unsatisfiable" from "transient" at
  the crash site is real, deferrable engineering effort for marginal
  additional benefit. Accepting the coarser tension here is the right
  call for Phase 1.

## Load-bearing decisions to reconsider

- **Deferring dependency-presence checking (the `datasets` package
  incident) as fast-follow, versus this same pass.** I give my own
  verdict on this, as asked: engineering-wise, deferring it is correct —
  a shell-checkable `requires: list[str]` is real, separable design
  surface, not something to bolt onto this stance under time pressure.
  But there is a doctrine-shaped complication the stance doesn't address:
  `DOCTRINE.md`'s reserved Article VI cites *this exact incident*
  ("A real finetuning run on gb10 crashed on a missing Python package
  that nothing checked for before dispatch") as its motivating example,
  and says "This slot stays empty until #9 lands a fix... When #9
  closes, this section gets written from what the fix actually
  established." If Article VI gets written from a fix that closes only
  the GPU/node-class half, it will be written *citing* an incident it
  did not actually close. My recommendation: split the doctrine-closure
  question from the engineering-scope question. Ship GPU/node-class now
  (engineering scope is right); do **not** write Article VI yet — either
  leave it reserved until the dependency-presence follow-up lands too, or
  write it narrowly ("Article VI, part one: hardware conformance") with
  an explicit forward-pointer to the still-open dependency half. Writing
  Article VI as fully closed off this pass alone would be dishonest to
  the doctrine's own stated discipline about not writing articles before
  the incident that motivates them is actually closed.
- **The `claim()` control-flow contract on mismatch (see A1) is currently
  a decision the stance implicitly makes by silence, not by stated
  choice.** This needs to be an explicit, named decision in the stance
  before it's a decision an implementer inherits by accident.

## Interface soundness — Phase 2 extensibility

The stance is honest that dimension 2's "probe once, process-lifetime
constant" assumption is the seam Phase 2 breaks first, and names it as an
explicit open question rather than either designing Phase 2 or ignoring
it. I agree with that self-assessment, and want to sharpen where the
friction actually lands: `claim()` today has no notion of *which node* is
asking — it's a bare, argument-less call. A multi-node Phase 2 will need
either (a) a node-identity parameter threaded through every `claim()`
call site across the whole system, or (b) the capability check moved
entirely out of `claim()` into a different layer (a per-node worker
requesting only tasks it can run, or a routing/selection step upstream of
`claim()`). Either path likely means Phase 2 *replaces* this stance's
`claim()`-internal check rather than extending it in place — that's a
reasonable and bounded cost for a two-function, single-migration Phase 1
change, and the stance is right not to pay for that generality now. I'd
only add: name this explicitly as "likely replacement, not extension" in
the open question, so whoever designs Phase 2 doesn't spend effort trying
to preserve this shape when replacing it cleanly is the right move.

One additional, low-severity note on interfaces: nothing in the codebase
structurally prevents two `scheduler.py` processes from running against
the same queue DB (no PID lock, no singleton check) — `claim()`'s `BEGIN
EXCLUSIVE` protects against double-claiming a *row*, not against two
schedulers with *different* node identities both writing `node_mismatch`
decisions derived from their own, different, process-lifetime probes.
This is not new risk the stance introduces — it's a pre-existing gap in an
explicitly single-node Phase — but it's worth a one-line acknowledgment
that the "process-lifetime constant" is only coherent as long as "one
scheduler, one node" holds, which nothing enforces today.

## Recommended changes, ordered

1. **Specify `claim()`'s post-mismatch return contract explicitly** (A1):
   commit the `done`/`terminal_reason` write, then `return None` — name
   this as a deliberate divergence from `BudgetExhausted`'s raise-based
   flow, and file the existing unguarded `BudgetExhausted` propagation at
   `runner_proc.py:53` as a separate follow-up bug.
2. **Add the `post()` INSERT-statement edit as an explicit step** (A2) —
   without it the whole mechanism silently no-ops.
3. **Actually rewrite "Minimum-viable first shape" steps 1–2** to name
   both new columns plainly, removing the stale hedge language (A3).
4. **Decide and state the Article VI doctrine-closure question**
   separately from the engineering-scope question — recommend: don't
   write it yet, or write it explicitly partial.
5. **Correct the "before a subprocess ever spawns" framing** (A4) to
   "avoids `run_turn()`'s expensive body" — cosmetic, but worth precision
   in a doctrine-adjacent document.
6. Optional, not blocking: one-line acknowledgment of the
   single-scheduler-process assumption's lack of structural enforcement,
   deferred to Phase 2 alongside the node-probe generalization.

None of these require re-architecting. Items 1–3 are the ones that
determine whether the shipped feature actually does what the stance
claims it does; 4–6 are precision and doctrine-hygiene.
