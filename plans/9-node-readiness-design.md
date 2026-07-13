# Design: Node Readiness Precondition Check (#9)

**Status:** Design complete, `APPROVE_WITH_RESERVATIONS` — ready for implementation.
**Audience:** Whoever picks up #9 next (a fresh Forge instance in this repo,
or any implementer). You don't need any prior conversation to act on this —
everything below is source-verified and self-contained.

**How this doc came to be:** Produced by a `cyclus-plan` (ConsensusKind)
design session — one Planner + adversarial Architect + adversarial Critic
review, run across two full rounds plus a narrow verification pass. Every
claim below is cross-referenced against real line numbers in this repo as
of commit `965a6a2` (main, post PR #10). Nothing here is paraphrase or
recollection — where a citation is given, it was read directly, not assumed.
The session log (if useful for provenance, not required reading) lives in
the original design session's `.cyclus/plans/` directory outside this repo.

**Before you touch code:** read this whole doc once. It corrects itself
twice in public (Round 2 caught a bug in Round 1's fix; a verification pass
caught a smaller bug in Round 2's fix) — that history is preserved below
because it's load-bearing: it shows *why* the final shape is what it is,
and it flags two things you should not re-litigate (see "Settled, don't
re-open" at the bottom) plus two things that are genuinely still open (see
"Open questions").

---

## What changed from the original issue framing, and why

The issue as filed treats "node readiness" as roughly: check `claim()`
before granting a claim, similar to the existing `budget` precondition.
That's where the design started. It's not where it ended up.

`claim()` already has a real precedent worth reusing: it loads the task's
spec via `loop_spec.load_spec()` and gate-checks a precondition (`budget`)
*before* granting the claim, raising a typed exception if the precondition
fails (`queue_sqlite.py:284-352`, `BEGIN EXCLUSIVE` transaction). The first
design draft extended this pattern directly. Adversarial review found two
problems with that placement:

1. `claim()` runs *inside* the already-spawned `runner_proc` subprocess
   (`runner_proc.py:53`) — so a claim()-time check cannot avoid the
   subprocess-spawn cost it exists to avoid.
2. `claim()`'s contract on a mismatch was genuinely underspecified: raise
   reproduces an existing, unrelated, unhandled `BudgetExhausted` crash-loop
   bug that lives in this codebase today (confirmed: `BudgetExhausted` is
   raised at `queue_sqlite.py:329,338`, re-raised by `claim()`, and never
   handled further up the stack — not in `runner_proc.py`, not in
   `scheduler.py`; it bubbles out and causes a crash-loop); return `None` silently
   discards the signal that a mismatch occurred; return the task dict with
   `done` pre-set has no shape in the current interface. Whichever an
   implementer picked by following the letter of a claim()-based design,
   two of the three readings are either a crash or a swallowed signal.

**The corrected placement: `_dispatch()` (scheduler.py:178-215), not
`claim()`.** It already runs a `continue`-on-fail eligibility filter with
the identical shape this needs (the `depends_on` and `earliest_start`
gates), it has `now` in scope, it holds every pending task as a flat dict
already carrying whatever columns exist, and — critically — it runs
*before* `_launch_runner()` calls `subprocess.Popen`. Moving the check here:

- Removes the `claim()` ambiguity entirely: `claim()` is never touched, its
  contract, callers, and existing tests stay exactly as they are today.
- Actually delivers the cost-avoidance the feature is for (a check inside
  `claim()` cannot avoid a subprocess spawn that already happened; a check
  in `_dispatch()` runs strictly before that spawn).
- Keeps the queue interface (`claim`, `complete`, `cancel`) completely
  node-agnostic — node identity becomes a property the *scheduler* owns,
  which is where it belongs. A generic, backend-swappable primitive should
  not need to know about node capability at all (this also matters for
  `DOCTRINE.md` Article V — see "Known gap this does not close" below).

## The design

Saturate already has two mechanisms this problem needs, in two different
places: `_dispatch()`'s eligibility-filter loop (`depends_on`/
`earliest_start` gates, `continue`-on-fail, runs before subprocess spawn)
and `cancel()`'s terminate-via-`done`+`terminal_reason` write. The design
extends both with one more eligibility gate and one more `terminal_reason`
value — no new queue status, no new probe subsystem, no change to `claim()`
at all.

### 1. Requirement declaration — where authors actually set these fields

`SaturateTask` already declares `num_gpus: float` and
`required_node_class: Optional[str]` (`task.py:52-54`) — unused today, but
the right shape: reuse as-is. Do not add a third field for "Python package
presence" — that's a different concern, deliberately deferred (see "Known
gap this does not close").

**Where NOT to put these fields, and why it matters:** the only implemented
producer path other than a raw `queue.post()` call is `_seed_goals()`
(`scheduler.py:130-175`), which reads a goal spec's YAML via
`yaml.safe_load()` and passes every top-level key not in its exclusion list
straight into `queue.post()`'s task dict. That same YAML file is *also* the
`spec_path` loaded by `loop_spec.load_spec()` inside both `post()` and every
turn of `runner.py`. `LoopSpec.model_config = {"extra": "forbid"}` means
putting `required_node_class` at the top level of that file — the intuitive
place, next to `name`/`kind`/`terminal`/`executor` — fails Pydantic
validation on *every turn*. **`required_node_class`/`num_gpus` must never
be set inside the loop-spec YAML body.** This is documented, not just
enforced — see step 7 below for the actual guard.

### 2. Local capability probing

Phase 1 has exactly one node. "Probing" degenerates to two cheap checks
computed once at scheduler startup: GPU count and a `node_class` string
from an env var, defaulting to `None`/"any" if unset. Not re-probed
per-dispatch — a process-lifetime constant is correct here because nothing
changes between dispatches within one scheduler process's lifetime. If the
scheduler restarts, it re-probes once at the next startup — correct and
free.

### 3. The mismatch verdict

When the new eligibility gate in `_dispatch()`'s `for task in pending:` loop
fails (task needs a GPU the node lacks, or names a `required_node_class`
that doesn't match), write `status='done'`, `terminal_reason='node_mismatch:
...'` — the identical `UPDATE` shape `cancel()` already performs
(`queue_sqlite.py:529-539`) — then `continue` past the task, exactly the
same control-flow shape the `depends_on` gate already uses two lines above
it in the same loop. `done` remains general enough to carry this; no new
status, no new column beyond the ones named in step 1 below.

### 4. Failure classification at the crash site

Build the pre-check above for the declared/known case; do not build a
second crash-classification taxonomy. `runner_proc.py`'s existing `except
Exception` handler gets one narrow addition: increment `retry_count` (new
column, same `ALTER TABLE` pattern as `human_gated` etc.) on `requeue()`,
and when `retry_count >= max_retries` (already-declared, unused field),
route to `terminal_reason='exhausted_retries: <last error>'` instead of
requeuing forever. The pre-check handles the known case cheaply; the
retry-count breaker is a blunt, sufficient backstop for everything else —
not a failure taxonomy that tries to distinguish "unsatisfiable" from
"transient."

**Why worker-side placement here does not violate the budget-enforcement
principle:** `DOCTRINE.md`/Article V requires budget enforcement to live at
`claim()`, never inside a worker, because budget is a **cross-claimant
aggregate** — N concurrent workers could each independently believe they're
under budget while collectively blowing past it (the distributed-multiplier
problem). `retry_count` is different in kind: `claim()`'s `BEGIN EXCLUSIVE`
already guarantees only one subprocess can hold a given `task_id` in
`running` at a time, so increments to *this task's* `retry_count` are
inherently serial — there is no aggregate ceiling multiple workers could
collectively exceed, because only one worker is ever touching this counter
at a time. Worker-side placement is safe here specifically because the
thing being counted is per-task, not per-fleet. Keep this distinction
explicit in code comments — a future extension should not read "worker-side
is fine here" and misapply it to something that's actually a cross-claimant
aggregate concern.

### 5. loop-spec composability

`required_node_class`/`num_gpus` stay `SaturateTask`-local; no loop-spec
change, no new field proposed upstream. This is a placement concern (where
can this run) distinct from `ExecutorSpec`'s execution concern (who acts) —
`ExecutorSpec.type` answers "who acts"; `SaturateTask.required_node_class`
answers "where they're allowed to act." Conflating the two would be a real
`loop-spec` ownership violation (Article IX): loop-spec is a shared standard
Cyclus and Saturate both consume, and Saturate never forks it locally to
route around a gap.

## Coherence

The three moves (`_dispatch()`-time gate, `terminal_reason` reuse,
retry-count circuit breaker) compose without tension: the gate handles the
declared, known-in-advance case *before* subprocess spawn, genuinely
avoiding that cost; the circuit breaker handles everything else as a
bounded fallback *after* spawn, using a counter that is safe specifically
because it is task-serial. Both terminate through the same `done` +
`terminal_reason` mechanism. No interleaving produces double-processing: a
task either fails the `_dispatch()` gate (never reaches `claim()`/spawn at
all) or passes it and is spawned, in which case only the crash-handler path
can apply the retry-count breaker. The two mechanisms are mutually
exclusive by construction, not by convention.

**Accepted tension:** the circuit breaker treats every kind of repeated
crash identically (three GPU mismatches and three genuinely-transient
network blips both eventually hit `exhausted_retries`). Deliberate
simplification for Phase 1 — a human reading `terminal_reason` has enough
information to judge for themselves whether to requeue manually. Building a
taxonomy of exception types to distinguish "unsatisfiable" from "transient"
is real engineering effort the retry-count breaker already captures at much
lower cost.

## Implementation steps (in order)

1. **Migration:** add three columns via `ALTER TABLE` (same pattern as
   `human_gated`/`tokens_used`/`cost_usd`): `required_node_class TEXT`,
   `num_gpus REAL DEFAULT 0.0`, `retry_count INTEGER NOT NULL DEFAULT 0`.

2. **Edit `post()`'s INSERT** (`queue_sqlite.py:253-274`): `post()`'s
   `INSERT` is a hardcoded, non-dynamic statement with an explicit `VALUES`
   tuple — adding DB columns via the migration alone does *nothing* unless
   `post()` is also edited. Add `required_node_class` and `num_gpus` to
   both the column list and the `VALUES` tuple, reading
   `task.get("required_node_class")` / `task.get("num_gpus", 0.0)` from the
   caller's dict — the same pattern `human_gated` etc. already use. Skip
   this step and the two new columns stay `NULL` forever, and the gate
   below silently never fires — it looks shipped and tested (schema exists,
   code compiles) while doing nothing.

3. **In `_dispatch()`'s eligibility loop** (`scheduler.py`, inside the
   existing `for task in pending:` block, alongside the
   `depends_on`/`earliest_start` `continue`-on-fail checks): compare
   `task.get("required_node_class")` / `task.get("num_gpus")` against the
   process-lifetime local capability probe (the `local_node_class` /
   `local_gpu_count` variables set once at worker startup — see design
   dimension 2 above for the probe spec). On mismatch: write `UPDATE tasks SET status='done',
   terminal_reason=? WHERE task_id=?` with reason
   `f"node_mismatch: needs {required_node_class or 'gpu:'+str(num_gpus)}, "
   f"local node is {local_node_class!r} with {local_gpu_count} gpu(s)"`,
   then `continue` — do not append to `eligible`. No `claim()` change of
   any kind.

4. **Fix `_dispatch()`'s `done_ids` set.** This is a pre-existing bug the
   feature above makes materially worse, so it must land in the same PR.
   `_dispatch()`'s dependency-satisfaction check
   (`done_ids: set[str] = {t["task_id"] for t in queue.list_tasks("done")
   ...}`, `scheduler.py:185-187`) currently treats **any** `done` task —
   `success`, `exhausted`, `stalled`, `cancelled: ...`, and now this
   design's own `node_mismatch`/`exhausted_retries` — as satisfying a
   child's `depends_on`. This bug pre-exists this design (it already
   mis-treats `exhausted`/`stalled`/`cancelled` tasks as satisfied
   dependencies today) but this design adds two new, plausibly-common ways
   for a parent to land in `done`-without-succeeding — directly relevant to
   the `BatchKind`/`SpawnPolicy` swarm primitive (`ARCHITECTURE.md:334-337`)
   that's built on `depends_on`.

   **Use a denylist, not an allowlist** — this is the one place this
   design's own first draft got it wrong, twice, and it's worth
   understanding why so you don't reintroduce the bug. An allowlist keyed
   on `terminal_reason in (None, "success")` looks obviously right and is
   *not*: `MetricOptimizationKind`'s two terminal writes (`runner.py:150,
   156`) are `"exhausted"` and `"stalled"` — `"success"` is written only by
   `_run_task_execution_turn()` (`runner.py:186, 240`), the
   `TaskExecutionKind` path. `"stalled"` (plateau reached, no further
   improvement found) is `MetricOptimizationKind`'s actual convergence
   signal — the closest thing that loop kind has to "succeeded." An
   allowlist on the literal string `"success"` would permanently exclude
   every converged `MetricOptimizationKind` parent from ever satisfying a
   child's `depends_on` — inverting the bug (too-permissive) into a new one
   (too-restrictive) on exactly the loop kind most likely to use this.

   Correct shape: `FAILURE_REASONS = {"node_mismatch", "exhausted_retries",
   "exhausted", "cancelled"}` (prefix-matched, so `node_mismatch: ...` and
   `cancelled: ...` free-text variants both match), rename `done_ids` to
   `success_ids`, build it as `{t["task_id"] for t in
   queue.list_tasks("done") if "task_id" in t and not
   _is_failure_reason(t.get("terminal_reason"))}`. Everything not in the
   denylist — `None`, `"success"`, `"stalled"`, and any future terminal
   reason not yet invented — satisfies `depends_on` by default. A denylist
   degrades more gracefully than an allowlist: a future loop kind that
   introduces a new terminal reason correctly satisfies `depends_on`
   without anyone having to remember to add it anywhere.

   `"exhausted"` belongs in the failure set: in both loop kinds it fires
   specifically when `max_iterations` is hit *while work remains*
   (`TaskExecutionKind`: `remaining` tasks not yet completed, `runner.py:
   233`; `MetricOptimizationKind`: no plateau reached and no completion
   signal, `runner.py:150`) — it means "ran out of budget before
   finishing," a genuine failure-of-completion, not a graceful stop.
   `"cancelled"` belongs there too: `cancel()`'s terminal reason
   (`f"cancelled: {reason}"`, `queue_sqlite.py:529-539`) means the task's
   work is not done.

5. **In `runner_proc.py`'s `except Exception` handler:** increment
   `retry_count` before calling `requeue()`; if the new count `>=
   max_retries`, write the done+`exhausted_retries` terminal state instead
   of requeuing.

6. **Surface `terminal_reason` in `saturate status <task_id>`** (`cli.py`'s
   `status` command): add `click.echo(f"Terminal reason: {task.get('terminal_reason')}")` guarded on `status == 'done'`. The
   command currently prints only `Status: {status}` and turn history —
   `terminal_reason` is invisible to a human running the one command
   documented for checking on a task. Without this, this whole feature's
   value proposition ("a human reading `terminal_reason` has enough
   information") doesn't reach anyone.

7. **Add a seed-time guard in `_seed_goals()`** (`scheduler.py:130-175`,
   right after `spec = yaml.safe_load(...)` at line 148): raise a specific,
   actionable error if `required_node_class`/`num_gpus` appear in the raw
   YAML dict, before the pass-through to `queue.post()`. Without this
   guard, the resulting failure is a `Pydantic ValidationError` on every
   turn (`runner.py:43`, uncaught), bounded only by the retry-count breaker
   this same design adds — a human reading `exhausted_retries:
   <ValidationError: required_node_class Extra inputs are not permitted>`
   would reasonably read that as a real hardware/environment problem, not
   an authoring mistake. This guard converts a delayed, misleading crash
   into an immediate, correctly-attributed seed-time error.

8. **Add `--node-class`/`--num-gpus` options to `saturate submit`**
   (`cli.py:28-45`, the same pattern as the existing `--output` option),
   populated into the task dict passed to `q.post()`. `_seed_goals()`'s
   blind YAML pass-through is not the only producer path — `submit`'s task
   dict only ever extracts `name`/`kind`/`spec_path`/`output_path`, with no
   field and no CLI flag for `required_node_class`/`num_gpus` at all.
   `submit` cannot hit the `extra:forbid` crash (it never passes through
   arbitrary YAML keys), but it also cannot exercise this feature at all —
   the CLI a human would actually run to test node-readiness manually has
   no way to invoke it without this.

9. **Add named tests**, at minimum:
   - `test_dispatch_skips_node_mismatch` — a task with an unsatisfiable
     `required_node_class` lands in `done`/`node_mismatch` and is never
     claimed.
   - `test_post_persists_node_fields` — `post()` actually writes the two
     new columns (step 2's test-level check).
   - `test_retry_count_exhaustion` — N crashes route to `exhausted_retries`
     rather than requeuing a fourth time.
   - `test_dispatch_success_ids_excludes_failure_reasons` — a child
     `depends_on` a `node_mismatch`/`exhausted_retries`/`exhausted`/
     `cancelled`-terminated parent does not proceed.
   - `test_dispatch_success_ids_includes_stalled` — a child `depends_on` a
     `stalled`-terminated `MetricOptimizationKind` parent *does* proceed
     (the specific case a naive allowlist gets wrong — this test exists to
     catch a regression back to that shape).

10. **Fix `ARCHITECTURE.md`'s stale state diagram.** `ARCHITECTURE.md:
    263-267` documents a `FAILED` status distinct from `DONE` for "crash,
    max retries exceeded" — the actual scenario this design implements via
    `done`+`terminal_reason='exhausted_retries: ...'`. `FAILED` was never
    built (the real `_DDL`/status column comment shows only `pending |
    running | done`). Correct the diagram to show `done` disambiguated by
    `terminal_reason`, not a real `FAILED` status — inventing one now would
    be a bigger, riskier schema change for no behavioral gain, and nothing
    else in the codebase treats status values programmatically beyond the
    three that exist. Ship this doc fix in the same PR.

That's the entire shape: one migration, one `post()` edit, one
`_dispatch()` addition (plus its `done_ids` fix), one crash-handler edit,
one CLI status line, one seed-time guard, two new `submit` flags, a doc
fix, named tests. **Zero changes to `claim()`.**

## Known gap this does not close

**The dependency-presence half of "node readiness" is NOT closed by this
design.** The literal incident that opened #9 was a missing Python package
(`datasets`) crashing a finetuning run — that is a *different kind of
problem* from GPU/node-class mismatch, not a second phase of the same
concept, and conflating them would be a category error:

- Hardware capability (GPU count, node class) is legitimately a
  process-lifetime constant for the scheduler process — a GPU doesn't
  appear or vanish mid-run. The "probe once at startup" model above is
  sound *because* hardware doesn't change mid-run.
- Package/dependency presence is **not** a process-lifetime constant in the
  same sense: `runner.py`'s `_resolve_worktree()` clones `spec.repo` into a
  *per-task* isolated worktree, and whatever environment that worktree's
  own executor needs is a property of *that task's clone* — checked fresh
  at or after worktree preparation, not a property of the node checked once
  at scheduler startup. Two different tasks on the identical node, same
  scheduler process, could have different dependency outcomes depending on
  their own repo's lockfile.

The dependency-presence half will need to live at a different lifecycle
point (inside `run_turn()`, after `_resolve_worktree()` — closer to how the
`measure()`/`correctness()` gate already works) and a different mechanism
(a subprocess-run check command, not a scalar comparison) — not a
claim-time or dispatch-time gate at all. This is real, separable follow-on
work, not something to bolt onto this PR under time pressure.

`DOCTRINE.md`'s Article VI slot (lines 86-100) is reserved and explicitly
cites this exact incident — "A real finetuning run on gb10 crashed on a
missing Python package that nothing checked for before dispatch" — with the
stated rule: "When #9 closes, this section gets written from what the fix
actually established." **This design does not close that incident** — it
closes a different, related one (hardware/node-class mismatch). Writing
Article VI as fully resolved from this design alone would cite an incident
it did not actually close. **Recommendation for whoever closes #9:** either
leave Article VI reserved until the dependency-presence follow-up also
lands, or write it narrowly and explicitly two-part — "Article VI, part
one: hardware/node-class conformance" — with a forward-pointer to the
still-open dependency half. Do not imply, by silence, that this design
covers both concerns. This is a doctrine-authorship call for whoever
actually closes the issue, not pre-decided here.

**Also out of scope, named explicitly:** the file-based `Queue` backend
(`queue.py`) has zero precondition checks of any kind today (confirmed: no
budget check either). This design does not bring it to parity with
`SqliteQueue` — that's a pre-existing gap this design doesn't create and
doesn't worsen. Recommend a separate follow-up issue scoped to "bring
`queue.py` to precondition-parity with `queue_sqlite.py`" rather than
folding it into #9. If Article VI is eventually written narrowly per the
above, it should state its scope as "SqliteQueue backend only" so it
doesn't overstate a guarantee `queue.py` doesn't provide.

## Phase 2 / Nomad extension point

Named explicitly so whoever designs Phase 2 doesn't spend effort preserving
a shape that should be cleanly replaced: `_dispatch()`/`claim()` today have
no notion of *which node* is asking — Phase 1 has exactly one node. A
multi-node Phase 2 will most likely **replace** this design's
`_dispatch()`-side check rather than extend it in place (either threading a
node-identity parameter through every relevant call site, or moving
capability-matching into a routing/selection layer upstream of any single
scheduler's `_dispatch()`). This is a reasonable, bounded cost for a
two-function, single-migration Phase 1 change — don't try to generalize
this now.

## Settled, don't re-open

Two things were contested and resolved during design review — don't
re-litigate them without new evidence:

1. **Placement is `_dispatch()`, not `claim()`.** This was the single
   biggest correction the design went through and it's the right call for
   the reasons in "What changed" above. If you find yourself reaching for
   `claim()` again, re-read that section first.
2. **The `success_ids`/`FAILURE_REASONS` denylist, not an allowlist.** Two
   separate review passes independently corrected an allowlist-shaped first
   draft back to a denylist. If you're tempted to simplify this to `if
   terminal_reason == "success"` — don't; that's the exact bug that got
   caught twice.

## Open questions for the implementer

1. Should the seed-time guard (step 7) `raise` or log-and-skip when it
   catches `required_node_class`/`num_gpus` in a loop-spec YAML body? The
   design assumes `raise` (fail loud, fail at seed time) but this wasn't
   pressure-tested against how `_seed_goals()`'s caller handles exceptions
   today — check that before assuming.
2. Exact wording/format of the `node_mismatch` and `exhausted_retries`
   `terminal_reason` strings is a suggestion above, not gospel — keep them
   parseable (the prefix-matching in step 4 depends on the `node_mismatch:`
   and `cancelled:` prefixes existing) but the human-readable detail after
   the prefix is yours to shape.
