# Design Stance — Node Readiness Precondition Check (saturate#9)

**Revision 2.2 (final).** Round 1 (this Planner writing directly after a
subagent timeout, then Architect + Critic review) produced a unanimous
REQUEST_CHANGES with ten prioritized items. Round 2 rewrote the design
around the Critic's S1 (relocate the check from `claim()` to `_dispatch()`).
A Round 2 Critic re-review (APPROVE_WITH_RESERVATIONS) confirmed S1 was
correctly incorporated and all ten Round 1 items resolved in substance, but
caught a genuine bug in this revision's own fix for must-fix #5 (N1: the
`done_ids`→`success_ids` filter, as first drafted, would have silently
excluded every converged `MetricOptimizationKind` task) and flagged F3 (the
`extra:forbid` schema-authoring trap) as under-enforced — both corrected in
revision 2.1. **Two consecutive Round 2 Architect subagent dispatches then
timed out at the 600s ceiling with zero output** (a real pattern: Round 1's
Architect pass succeeded cleanly at 390s with a comparable brief). Per
P-TIMEOUT discipline, this Planner completed the Architect's Round 2.1
verification-scoped brief directly rather than dispatch a third blind
attempt, and wrote the review itself, holding it to the same adversarial
standard: it found a real, previously-uncaught completeness gap
(`saturate submit` has no way to declare `required_node_class`/`num_gpus`
at all — the CLI a human would actually use to test this feature manually
cannot invoke it) and a small textual gap in the `FAILURE_REASONS` denylist
(`"cancelled"` was omitted from the set literal despite the prose already
stating intent to prefix-match it). Both corrected in this document
(revision 2.2) — see Minimum-viable steps 4, 8, and dimension 5's Interfaces
section.

## What changed from Round 1, and why

Round 1 put the check inside `claim()`. Both reviewers confirmed the factual
precedent (`claim()`'s existing `budget` gate) was real, but the Critic (S1)
showed `claim()` is the wrong *place*: `claim()` runs inside the already-
spawned `runner_proc` subprocess (`runner_proc.py:53`), so a claim()-time
check cannot avoid the subprocess-spawn cost it was designed to avoid (the
Architect's own A4 already noted this; the Critic showed it's not cosmetic,
it's load-bearing). Worse, `claim()`'s contract on mismatch was genuinely
underspecified (A1): raise reproduces an existing, unrelated, unhandled
`BudgetExhausted` crash-loop bug; return `None` silently discards the signal
that a mismatch occurred; return the task dict with `done` pre-set has no
shape in the current interface.

**`_dispatch()` (scheduler.py:178-215) is the right place.** It already runs
a `continue`-on-fail eligibility filter with the identical shape this needs
(the `depends_on` and `earliest_start` gates), it has `now` in scope, it
holds every pending task as a flat dict already carrying whatever columns
exist, and — critically — it runs *before* `_launch_runner()` calls
`subprocess.Popen`. Moving the check here:

- Resolves A1 completely by removing its premise: `claim()` is never touched,
  its contract, callers, and existing tests stay exactly as they are today.
- Actually delivers the cost-avoidance the design claims (closing A4 for
  real, not just correcting its wording).
- Better honors DOCTRINE.md Article IV/backend-agnosticism (P6): the queue
  interface (`claim`, `complete`, `cancel`) stays completely node-agnostic;
  node identity becomes a property the *scheduler* owns, which is where it
  actually belongs — a generic, backend-swappable primitive should not need
  to know about node capability at all.

## Premise (revised)

Saturate already has two mechanisms this problem needs, in two different
places: `_dispatch()`'s eligibility-filter loop (`depends_on`/`earliest_start`
gates, `continue`-on-fail, runs before subprocess spawn) and `cancel()`'s
terminate-via-`done`+`terminal_reason` write. The minimal design extends both
with one more eligibility gate and one more `terminal_reason` value — no new
queue status, no new probe subsystem, no change to `claim()` at all.

## Dimensions

### 1. Requirement declaration — where authors actually set these fields

`SaturateTask` already declares `num_gpus: float` and
`required_node_class: Optional[str]` (task.py:52-54) — unused, but the right
shape: reuse as-is, do not add a third field for "Python package presence"
(deferred, see Open Questions and Article VI note below).

**Round 2 addition, closing the Critic's F3:** the only implemented producer
path other than a raw `queue.post()` call is `_seed_goals()`
(scheduler.py:130-175), which reads a goal spec's YAML via `yaml.safe_load()`
and passes every top-level key not in its exclusion list straight into
`queue.post()`'s task dict. That same YAML file is *also* the `spec_path`
loaded by `loop_spec.load_spec()` inside both `post()` and every turn of
`runner.py`. `LoopSpec.model_config = {"extra": "forbid"}` means putting
`required_node_class` at the top level of that file — the intuitive place,
next to `name`/`kind`/`terminal`/`executor` — fails Pydantic validation on
every turn. This stance states explicitly: **`required_node_class`/
`num_gpus` must never be set inside the loop-spec YAML body.** For the
`_seed_goals()` path, this means documenting that the pass-through only
works because these keys don't collide with the loop-spec schema's own
required fields — not because anyone designed for this — and that a task
author using the intuitive location will get a schema-validation crash, not
a helpful error. This is a doc-and-convention fix (name where the fields go),
not a code fix; there is currently no other producer path to fix.

### 2. Local capability probing — unchanged from Round 1

Phase 1 has exactly one node. "Probing" degenerates to two cheap checks
computed once at scheduler startup: GPU count and a `node_class` string from
an env var, defaulting to `None`/"any" if unset. Not re-probed per-dispatch —
process-lifetime constant, matching P4 (nothing changes between dispatches,
nothing to cache durably). Unchanged by the placement move: `_dispatch()`
computes this once at scheduler-process start exactly as `claim()` would
have.

### 3. The mismatch verdict — now lives in `_dispatch()`, reuses `cancel()`'s exact mechanism

When the new eligibility gate in `_dispatch()`'s `for task in pending:` loop
fails (task needs a GPU the node lacks, or names a `required_node_class` that
doesn't match), write `status='done'`, `terminal_reason='node_mismatch: ...'`
— the identical `UPDATE` shape `cancel()` already performs
(queue_sqlite.py:529-539) — then `continue` past the task, exactly the same
control-flow shape the `depends_on` gate already uses two lines above it in
the same loop. **This eliminates A1's entire raise-vs-return ambiguity: there
is no `claim()` return contract to specify, because `claim()` is never
called for a mismatched task in the first place.** `done` remains general
enough to carry this; no new status, no new column beyond the two named in
dimension 1 and the Open Questions resolution below.

### 4. Failure classification at the crash site — unchanged position, one addition

Build the pre-check (now in `_dispatch()`) for the declared/known case; do
not build a second crash-classification taxonomy. `runner_proc.py`'s
existing `except Exception` handler gets one narrow addition: increment
`retry_count` (new column, same `ALTER TABLE` pattern as `human_gated` etc.)
on `requeue()`, and when `retry_count >= max_retries` (already-declared,
unused field), route to `terminal_reason='exhausted_retries: <last error>'`
instead of requeuing forever. Same reasoning as Round 1: the pre-check
handles the known case cheaply; the retry-count breaker is a blunt,
sufficient backstop for everything else, not a failure taxonomy.

**Round 2 addition, closing the Critic's P5 concern (must-fix #10):** this
placement is worker-side (`runner_proc.py`), which is exactly where budget
enforcement is forbidden by Article V/P5 — but for a different reason that
must be stated explicitly so a future extension doesn't conflate the two.
Budget enforcement lives at `claim()` because budget is a **cross-claimant
aggregate** — N concurrent workers could each independently believe they're
under budget while collectively blowing past it, which is exactly the
distributed-multiplier problem Article V exists to prevent. `retry_count` is
**task-serial, not aggregate**: `claim()`'s `BEGIN EXCLUSIVE` already
guarantees only one subprocess can hold a given `task_id` in `running` at a
time, so increments to *this task's* `retry_count` are inherently serial —
there is no aggregate ceiling multiple workers could collectively exceed,
because only one worker is ever touching this counter at a time. Worker-side
placement is safe here specifically because the thing being counted is
per-task, not per-fleet. This distinction — task-serial counter vs.
cross-claimant aggregate — is the reason this design does not reintroduce
the exact problem P5 exists to prevent, and it must stay named so nothing
downstream copies "worker-side is fine" onto a genuinely aggregate concern.

### 5. loop-spec composability (P9) — unchanged, with a forward-scoped caveat

`required_node_class`/`num_gpus` stay `SaturateTask`-local; no loop-spec
change. Unchanged reasoning from Round 1: this is a placement concern
(where can this run) distinct from `ExecutorSpec`'s execution concern (who
acts).

**Round 2 addition, closing the Critic's O1 and must-fix #9:** the deferred
half (package/dependency-presence, e.g. the literal `datasets` incident) is
**not simply Phase 2 of this same concept** — it is very likely a different
mechanism entirely, and this stance now says so explicitly rather than
implying a smooth continuation. Hardware capability (GPU count, node class)
is legitimately a process-lifetime constant for the scheduler process — the
"probe once" model in dimension 2 is sound *because* hardware doesn't change
mid-run. Package/dependency presence is **not** a process-lifetime constant
in the same sense: `runner.py`'s `_resolve_worktree()` clones `spec.repo`
into a per-task isolated worktree, and whatever environment that worktree's
own executor needs is a property of *that task's clone* — checked fresh at
or after worktree preparation, not a property of the node checked once at
startup. Two different tasks on the identical node, same scheduler process,
could have different dependency outcomes depending on their own repo's
lockfile. The deferred half will need to live at a different lifecycle
point (inside `run_turn()`, after `_resolve_worktree()`, closer to how the
`measure()`/`correctness()` gate already works) and a different mechanism (a
subprocess-run check command, not a scalar comparison) — not a claim-time or
dispatch-time gate at all. "GPU/node-class now, deps as fast-follow" is two
features sharing one doctrine slot, not two phases of one feature. This
matters directly for Article VI (see below): it must not imply the deferred
half is a smooth continuation of this design.

## Coherence

The three moves (`_dispatch()`-time gate, `terminal_reason` reuse,
retry-count circuit breaker) compose without tension, now more cleanly than
Round 1: the gate in `_dispatch()` handles the declared case *before*
subprocess spawn, genuinely avoiding that cost; the circuit breaker in
`runner_proc.py` handles everything else as a bounded fallback *after* spawn,
using a counter that is safe specifically because it is task-serial (dimension
4's Round 2 addition). Both terminate through the same `done` +
`terminal_reason` mechanism. No interleaving produces double-processing: a
task either fails the `_dispatch()` gate (never reaches `claim()`/spawn at
all) or passes it and is spawned, in which case only the crash-handler path
can apply the retry-count breaker. The two mechanisms are mutually exclusive
by construction, not by convention.

**Accepted tension, unchanged from Round 1:** the circuit breaker treats
every kind of repeated crash identically. Deliberate simplification; the
gate is what should catch known-cause incidents before they reach this
coarser fallback.

## Keep/reshape/retire verdict for existing scaffolding

- **`num_gpus`, `required_node_class` — KEEP, activate.** Read in
  `_dispatch()`'s eligibility loop against the process-lifetime probe from
  dimension 2. Requires new DB columns (see Open Questions resolution) *and*
  an edit to `post()`'s INSERT statement (see Minimum-viable shape step 2 —
  this was Round 1's A2, carried forward unchanged and now made explicit as
  its own numbered step).
- **`max_retries` — KEEP, activate.** Circuit-breaker ceiling for the new
  `retry_count` column.
- **`requeue()` — KEEP, unchanged for the transient case.**
- **New: `retry_count INTEGER NOT NULL DEFAULT 0`, `required_node_class TEXT`,
  `num_gpus REAL DEFAULT 0.0`** — three columns, one migration, one `ALTER
  TABLE` pattern already used three times in `_ensure_schema`.
- **No changes to `dispatch_score()` or the CPU-saturation/sort logic** — the
  new gate is a `continue`-on-fail filter added to the *existing*
  `for task in pending:` loop in `_dispatch()`, before the `eligible.append`
  call, not a change to scoring.

## Interfaces

**With `queue_sqlite.py`'s state machine:** no new status value, and now
`claim()` is untouched entirely — it never sees a node-mismatched task,
because `_dispatch()` filters it out before any `claim()` call happens for
that task. `pending` → `done` directly (via the new `_dispatch()`-side write)
is a new edge that bypasses `running` altogether, distinct from the existing
`pending → running → done` path. This is the correct shape: a task that can
never run on this node never transitions to `running` at all.

**Round 2 addition, closing must-fix #4 (P6/backend-agnosticism):**
`_dispatch()` lives in `scheduler.py`, which already imports and operates
against `SqliteQueue` specifically — it is not written against the
four-operation backend-agnostic interface Article V names (`post`, `claim`,
`write_state`, `complete`). Placing the gate in `_dispatch()` does not
introduce a *new* backend-agnosticism violation — `scheduler.py` was already
`SqliteQueue`-specific before this design — but it does mean the file-based
`Queue` (`queue.py`), which has zero precondition checks of any kind
(confirmed: no budget check either), will not get node-mismatch handling for
free by virtue of this design, and neither would it have if the gate had
stayed in `claim()` (queue.py's `claim()` has no equivalent either). This is
named explicitly, not silently inherited: **the file-based backend's lack of
parity with `SqliteQueue` on preconditions generally (budget, and now
node-mismatch) is a pre-existing, tracked gap this design does not close and
does not worsen.** Recommend a follow-up issue scoped to "bring `queue.py`
to precondition-parity with `queue_sqlite.py`" rather than folding it into
#9.

**With `loop-spec`:** none, unchanged. Zero new fields proposed upstream.

**Round 2 addition, closing must-fix #5 (the `_dispatch()` `done_ids` bug):**
`_dispatch()`'s own dependency-satisfaction check
(`done_ids: set[str] = {t["task_id"] for t in queue.list_tasks("done") ...}`,
scheduler.py:185-187) currently treats **any** `done` task — `success`,
`exhausted`, `stalled`, `cancelled: ...`, and now this design's own
`node_mismatch`/`exhausted_retries` — as satisfying a child's `depends_on`.
This bug pre-exists this design (it already mis-treats `exhausted`/`stalled`/
`cancelled` tasks as satisfied dependencies today) but this design adds two
new, plausibly-common ways for a parent to land in `done`-without-succeeding,
meaningfully raising the odds a dependent child proceeds under a false
premise — directly relevant to the `BatchKind`/`SpawnPolicy` swarm primitive
`ARCHITECTURE.md:334-337` names as a Phase 2 target, built on `depends_on`.

**This stance makes an explicit, stated decision rather than leaving it an
accident, corrected once in Round 2 review:** the first Round 2 draft of
this fix used an allowlist (`terminal_reason in (None, "success")`), which a
Round 2 Critic re-review (N1) caught as itself broken — confirmed by reading
every production `terminal_reason` write in `runner.py`:
`MetricOptimizationKind`'s two terminal writes (lines 150, 156) are
`"exhausted"` and `"stalled"` — `"success"` is written only by
`_run_task_execution_turn()` (186, 240), the `TaskExecutionKind` path.
`"stalled"` (plateau reached, no further improvement found) is
`MetricOptimizationKind`'s actual convergence signal — the closest thing this
loop kind has to "succeeded." An allowlist keyed on the literal string
`"success"` would have permanently excluded every converged
`MetricOptimizationKind` parent from ever satisfying a child's `depends_on`,
inverting Round 1's bug (too-permissive) into a new one (too-restrictive) on
exactly the loop kind the `BatchKind`/`SpawnPolicy` swarm primitive is most
likely to use. **Corrected design: define the failure set explicitly and
treat everything else as satisfying** —
`FAILURE_REASONS = {"node_mismatch", "exhausted_retries", "cancelled"}`
(prefix-matched
for `node_mismatch: ...`/`cancelled: ...` free-text variants) — rather than
an allowlist of success values. A denylist degrades more gracefully than an
allowlist here: a future loop kind that introduces a new terminal reason
correctly satisfies `depends_on` by default, rather than silently failing to
until someone remembers to add it to an allowlist — which is exactly the
class of silent gap this fix exists to close in the first place. Rename
`done_ids` to `success_ids`, built as
`{t["task_id"] for t in queue.list_tasks("done") if "task_id" in t and not
_is_failure_reason(t.get("terminal_reason"))}`, where `_is_failure_reason`
`_is_failure_reason` checks membership/prefix against `FAILURE_REASONS`.
`FAILURE_REASONS` includes `"exhausted"`: in both loop kinds, `exhausted`
fires specifically when `max_iterations` is hit *while work remains*
(`TaskExecutionKind`: `remaining` tasks not yet completed, `runner.py:233`;
`MetricOptimizationKind`: no plateau reached and no completion signal,
`runner.py:150`) — it means "ran out of budget before finishing," a genuine
failure-of-completion, not a graceful stop. A child depending on an
`exhausted` parent should not proceed as if the parent's work is done, because
it isn't. This is a stated decision, not left open: `FAILURE_REASONS =
{"node_mismatch", "exhausted_retries", "exhausted", "cancelled"}`, everything
else (`None`, `"success"`, `"stalled"`, and any future terminal reason not
yet invented) satisfies `depends_on` by default. `"cancelled"` is included
because `cancel()`'s existing terminal reason (`f"cancelled: {reason}"`,
queue_sqlite.py:529-539) is prefix-matched the same way `node_mismatch:` is
— a cancelled task's work is not done, and a child should not treat it as
satisfied.
fix that belongs in this same PR precisely because this design is what makes
the ambiguity consequential rather than theoretical.

## Minimum-viable first shape

1. **Migration:** add three columns via `ALTER TABLE` (same pattern as
   `human_gated`/`tokens_used`/`cost_usd`): `required_node_class TEXT`,
   `num_gpus REAL DEFAULT 0.0`, `retry_count INTEGER NOT NULL DEFAULT 0`.
   No hedging on storage mechanism — this is now fully resolved (see Open
   Questions).
2. **Edit `post()`'s INSERT** (queue_sqlite.py:253-274): add
   `required_node_class` and `num_gpus` to both the column list and the
   `VALUES` tuple, reading `task.get("required_node_class")` /
   `task.get("num_gpus", 0.0)` from the caller's dict — the same pattern
   `human_gated` etc. already use. Without this step the two new columns
   are always `NULL`/default and the gate below silently never fires. (This
   is Round 1's A2, now a named, mandatory step rather than an implicit
   assumption.)
3. **In `_dispatch()`'s eligibility loop** (scheduler.py, inside the existing
   `for task in pending:` block, alongside the `depends_on`/`earliest_start`
   `continue`-on-fail checks): compare `task.get("required_node_class")` /
   `task.get("num_gpus")` against the process-lifetime local capability
   probe from dimension 2. On mismatch: write
   `UPDATE tasks SET status='done', terminal_reason=? WHERE task_id=?` with
   reason `f"node_mismatch: needs {required_node_class or 'gpu:'+str(num_gpus)}, "
   f"local node is {local_node_class!r} with {local_gpu_count} gpu(s)"`,
   then `continue` — do not append to `eligible`. No `claim()` change of any
   kind.
4. **Fix `_dispatch()`'s `done_ids` set** to `success_ids`, built as a
   denylist (`FAILURE_REASONS = {"node_mismatch", "exhausted_retries",
   "exhausted", "cancelled"}`, prefix-matched for free-text variants;
   everything else, including `None`/`"success"`/`"stalled"`, satisfies
   `depends_on`) rather
   than an allowlist keyed on the literal string `"success"` — a
   `MetricOptimizationKind` task's genuine convergence signal is
   `"stalled"`, not `"success"` (see dimension 5/Interfaces section for why
   this matters and how a first-draft allowlist got this wrong in Round 2
   review).
5. **In `runner_proc.py`'s `except Exception` handler:** increment
   `retry_count` before calling `requeue()`; if the new count
   `>= max_retries`, write the done+`exhausted_retries` terminal state
   instead of requeuing.
6. **Surface `terminal_reason` in `saturate status <task_id>`** (cli.py's
   `status` command): add `click.echo(f"Terminal reason: {task.get('terminal_reason')}")`
   guarded on `status == 'done'`. Confirmed by direct read: the command
   currently prints only `Status: {status}` and turn history —
   `terminal_reason` is invisible to a human running the one command
   documented for checking on a task. Without this, the design's own stated
   value proposition ("a human reading `terminal_reason` has enough
   information") doesn't reach anyone.
7. **Add a seed-time guard in `_seed_goals()`** (scheduler.py:130-175,
   right after `spec = yaml.safe_load(...)` at line 148): raise a specific,
   actionable error if `required_node_class`/`num_gpus` appear in the raw
   YAML dict, before the pass-through to `queue.post()`. A stated convention
   ("never put these fields in the loop-spec YAML body," dimension 1) with
   zero enforcement at the one place an author would naturally violate it is
   exactly the failure class this stance's own text names — without this
   guard, the resulting failure is a `Pydantic ValidationError` on every
   turn, `runner.py:43` uncaught, bounded only by the retry-count breaker
   this same design adds — a human reading `exhausted_retries: <ValidationError:
   required_node_class Extra inputs are not permitted>` would reasonably
   read that as a real hardware/environment problem, not an authoring
   mistake. This guard converts a delayed, misleading crash into an
   immediate, correctly-attributed seed-time error.
8. **Add `--node-class`/`--num-gpus` options to `saturate submit`**
   (cli.py:28-45, the same pattern as the existing `--output` option),
   populated into the task dict passed to `q.post()`. **Round 2.1 Architect
   pass caught this gap:** `_seed_goals()`'s blind YAML pass-through is not
   the only producer path — `submit`'s task dict only ever extracts
   `name`/`kind`/`spec_path`/`output_path`, with no field and no CLI flag
   for `required_node_class`/`num_gpus` at all. `submit` cannot hit the
   `extra:forbid` crash (it never passes through arbitrary YAML keys), but
   it also cannot exercise this feature — the CLI a human would actually run
   to test node-readiness directly has no way to invoke it. Without this,
   the feature is reachable only through the automated `_seed_goals()` path,
   not through direct manual submission.
9. **Add named tests**, at minimum: `test_dispatch_skips_node_mismatch`
   (asserts a task with an unsatisfiable `required_node_class` lands in
   `done`/`node_mismatch` and is never claimed), `test_post_persists_node_fields`
   (asserts `post()` actually writes the two new columns, closing A2/step 2
   at the test level), `test_retry_count_exhaustion` (asserts N crashes
   route to `exhausted_retries` rather than requeuing a fourth time),
   `test_dispatch_success_ids_excludes_failure_reasons` (asserts a child
   `depends_on` a `node_mismatch`/`exhausted_retries`/`exhausted`/
   `cancelled`-terminated parent does not proceed), and
   `test_dispatch_success_ids_includes_stalled` (asserts a child
   `depends_on` a `stalled`-terminated `MetricOptimizationKind` parent *does*
   proceed — the specific case a naive allowlist would have gotten wrong).

That's the entire shape: one migration, one `post()` edit, one `_dispatch()`
addition (plus its `done_ids` fix), one crash-handler edit, one CLI status
line, one seed-time guard, two new `submit` flags, named tests. Zero changes
to `claim()`.

## What the current code lacked that this enables

Today: a task whose environment can never satisfy it retries forever,
consuming a scheduler tick and a subprocess spawn every cycle. After this
design: (a) the known case (declared GPU/node-class requirement unmet) is
caught in `_dispatch()`, genuinely *before* any subprocess spawns — this is
now factually accurate, correcting Round 1's overstatement (the Architect's
A4) that a `claim()`-time check would have this property; (b) the unknown
case (any other repeated crash) is bounded by `max_retries`. Both close with
the queue as the single source of truth for "why did this stop," now also
correctly surfaced to a human via `saturate status` (step 6 above).

## Open questions

1. **~~Storage mechanism~~ — RESOLVED, and now placement is resolved too.**
   Round 1 resolved that `required_node_class`/`num_gpus` need real DB
   columns (confirmed via `_DDL`, queue_sqlite.py:90-124 — not present).
   Round 2 resolves the remaining open question from Round 1 (where the
   check lives): `_dispatch()`, not `claim()`, per the Critic's S1. Nothing
   remains open on either storage or placement.
2. **Phase 2 / Nomad extension point — sharpened.** The Critic's review
   agreed with this stance's own self-assessment that dimension 2's "probe
   once, process-lifetime constant" is the seam Phase 2 breaks first, and
   added a precision: `_dispatch()`/`claim()` today have no notion of *which
   node* is asking. A multi-node Phase 2 will most likely **replace** this
   design's `_dispatch()`-side check rather than extend it in place (either
   threading a node-identity parameter through every relevant call site, or
   moving capability-matching into a routing/selection layer upstream of
   any single scheduler's `_dispatch()`). Named explicitly as "likely
   replacement, not extension" so Phase 2's designer doesn't spend effort
   preserving a shape that should be cleanly superseded.
3. **Package/dependency-level requirements (the literal `datasets`
   incident) — still deferred, now explicitly a different mechanism, not a
   phase-2-of-this-feature.** See dimension 5's Round 2 addition: this is
   likely a per-task, post-worktree-resolution check (closer to the
   `measure()`/`correctness()` gate), not a claim-time or dispatch-time
   scalar comparison. **The concrete incident in the context package
   (missing `datasets` package) is still NOT closed by this design** — only
   the GPU/node-class half. Named honestly, unchanged from Round 1.

## Article VI — recommendation, not a decision this stance makes unilaterally

`DOCTRINE.md`'s Article VI slot (lines 86-100) is reserved and explicitly
cites *this exact incident* — "A real finetuning run on gb10 crashed on a
missing Python package that nothing checked for before dispatch" — as its
motivating example, with the stated rule: "When #9 closes, this section
gets written from what the fix actually established." Given Open Question 3
above, this design does **not** close that incident; it closes a different,
related one (hardware/node-class mismatch). Writing Article VI as fully
resolved from this design alone would cite an incident it did not actually
close — the same discipline violation Article VI's own text warns against
applying to itself. **Recommendation:** either leave Article VI reserved
until the dependency-presence follow-up also lands, or write it narrowly and
explicitly two-part: "Article VI, part one: hardware/node-class
conformance" with a stated forward-pointer to the still-open dependency
half — never implying, by silence, that one check covers both concerns.
This is a doctrine-authorship decision, named here for whoever closes #9 and
writes the article, not resolved unilaterally by this stance.

## Verdict: REQUEST_CHANGES (Round 1) → APPROVE_WITH_RESERVATIONS (Round 2 — final)

Mapping, for traceability:

1. Relocate `claim()` → `_dispatch()` — done (S1), see "What changed" above
   and Minimum-viable step 3. **Round 2 Critic re-review confirmed correct
   against real `scheduler.py` source, not just gestured at.**
2. State declaration location explicitly, name the `extra: forbid` collision
   — done (dimension 1, F3). **Round 2 Critic re-review found "document it"
   alone was under-enforced; corrected in revision 2.1 with a seed-time
   guard (Minimum-viable step 7). Round 2.1 Architect pass then found the
   guard covers only one of two producer paths — `saturate submit` has no
   way to set these fields at all, correct or otherwise — corrected in
   revision 2.2 with new `--node-class`/`--num-gpus` CLI options
   (Minimum-viable step 8).**
3. Reconcile `ARCHITECTURE.md`'s stale `FAILED` diagram — **resolved.** This
   stance's Round 2 position (retire the phantom `FAILED` box, replace with
   `done` disambiguated by `terminal_reason`, fix shipped in this same PR)
   was independently confirmed by the Round 2 Critic re-review, reasoning
   from `DOCTRINE.md` Article III's own discipline (don't let a documented,
   known contradiction in canonical architecture docs sit unaddressed once
   the fix that resolves it is in hand). Doc fix to `ARCHITECTURE.md:263-267`
   is part of this PR's scope, not deferred.
4. `queue.py` backend-parity gap — named explicitly as pre-existing and
   scoped out via a follow-up issue recommendation — done (Interfaces
   section). **Round 2 Critic re-review confirmed this is consistent with
   Article V/III discipline, with one sharpening: when Article VI is
   eventually written, it should state its scope as "SqliteQueue backend
   only" so it doesn't silently overstate a guarantee `queue.py` doesn't
   provide.**
5. Fix `_dispatch()`'s `done_ids`/success semantics — done (Interfaces
   section, Minimum-viable step 4), **but revision 2.0's first attempt was
   itself wrong and had to be corrected twice.** The Round 2 Critic re-review
   (N1) caught that an allowlist keyed on `terminal_reason in (None,
   "success")` would have silently excluded every converged
   `MetricOptimizationKind` task, since that loop kind's genuine convergence
   signal is `"stalled"`, not `"success"` — confirmed by reading every
   production `terminal_reason` write in `runner.py`. Corrected to a
   denylist. The Round 2.1 Architect pass then caught that the denylist as
   first drafted omitted `"cancelled"` despite the prose already stating
   intent to prefix-match it. Final, corrected set: `FAILURE_REASONS =
   {"node_mismatch", "exhausted_retries", "exhausted", "cancelled"}`.
6. Surface `terminal_reason` in `saturate status` — done (Minimum-viable
   step 6).
7. Carry forward A2 (post() edit) and A3 (stale hedge language) — both done;
   A2 is now an explicit numbered step (Minimum-viable step 2), A3's stale
   text has been fully rewritten (this document no longer contains the
   pre-resolution hedge language Round 1 left behind).
8. Add named tests — done (Minimum-viable step 9), extended in revision 2.1
   with `test_dispatch_success_ids_includes_stalled` to cover the exact case
   N1 caught, and the excludes-test widened in revision 2.2 to cover
   `cancelled` alongside the other three failure reasons.
9. Sharpen Article VI language (two checks, two lifecycle points) — done
   (dimension 5 Round 2 addition, Article VI section). **Round 2 Critic
   re-review confirmed this was a faithful restatement, not flattened, and
   called it "the strongest single Round 2 addition."**
10. P5 precision note (task-serial vs. cross-claimant) — done (dimension 4
    Round 2 addition). **Independently re-verified against `claim()`'s
    `BEGIN EXCLUSIVE` source directly (this Planner, standing in for the
    timed-out Round 2 Architect re-reviews) — holds.**

## Round 2 process note: two consecutive Architect re-review timeouts

Two separate Round 2 Architect subagent dispatches timed out at the 600s
ceiling with zero output (18 API calls, then 10 API calls) — a real,
repeated pattern distinct from Round 1's clean 390s Architect success on a
comparable brief. Per P-TIMEOUT discipline, rather than a third blind
dispatch, this Planner completed both Architect briefs directly:

**First timeout (Round 2.0's Architect brief):** verified `_dispatch()`'s
eligibility-filter shape (`scheduler.py:178-243`), `post()`'s hardcoded
`INSERT` requiring the explicit edit (`queue_sqlite.py:253-274`), and the P5
task-serial reasoning against `claim()`'s `BEGIN EXCLUSIVE`
(`queue_sqlite.py:284-352`). All three held.

**Second timeout (Round 2.1's Architect brief, verification-scoped after the
Critic's N1/F3 findings):** re-verified N1's fix independently by re-grepping
every `terminal_reason` write in `runner.py` — confirmed the corrected
denylist classifies the full, closed vocabulary correctly, but found the set
literal itself had a gap (`"cancelled"` missing despite stated intent).
Verified F3's fix (`_seed_goals()`'s seed-time guard) against the actual
producer-path surface by reading `cli.py` directly, and found a second,
previously-uncaught producer path (`saturate submit`) that isn't protected
by the guard because it doesn't need to be (no blind pass-through) — but
also has no way to invoke the feature at all, a real completeness gap
neither the Critic nor the earlier Architect pass had surfaced. Both fixes
(the `"cancelled"` addition, the new `submit` CLI options) are applied in
this document as revision 2.2.

**Honest limitation, named rather than hidden:** this closes the specific
verification gaps both timed-out Architect briefs were assigned, and in the
second case produced a genuine new finding under the same adversarial
standard the brief asked for. It is still not equivalent to two fully
independent adversarial reads from separate contexts — the design has now
been checked by one Planner (twice, self-correcting) and one Critic (twice),
but never by a truly separate Architect voice in Round 2. This is named
explicitly as residual risk, not resolved by asserting it away. The pattern
of two consecutive timeouts on this specific verification-shaped task (vs.
one clean success on a similarly-scoped Round 1 brief) is worth a note for
whoever runs the next `cyclus-plan` session against this codebase.

Self-grading APPROVE_WITH_RESERVATIONS rather than APPROVE for two remaining,
named reasons: (1) the dependency-presence half of "node readiness" remains
open — this design does not close the literal incident that motivated #9,
only the GPU/node-class half, named honestly since Round 1; (2) the Round 2
Architect role was completed by the Planner directly rather than by an
independent adversarial pass, named above as residual risk rather than
resolved.


