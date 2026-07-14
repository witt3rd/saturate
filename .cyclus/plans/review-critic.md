# Critic Review — Node Readiness Precondition Check (saturate#9)

## Verdict: REQUEST_CHANGES

The Architect's three findings (A1–A3) are real, confirmed by my own independent
read of the same source, and correctly ranked. But the Architect signed off on
the *shape* of the design — claim()-time gate, `done`+`terminal_reason` reuse,
worker-side retry counter — without contesting whether that shape is actually
the right cut. I found a placement error the whole review missed (the check
should live in `_dispatch()`, not `claim()`), a documentation contradiction
neither reviewer noticed (ARCHITECTURE.md's own state diagram names a `FAILED`
status this design silently declines to build), a concrete second crash-loop
this feature would introduce via its own authoring surface, a real P6
(backend-agnosticism) violation the Architect never checked for because they
never opened `queue.py`, and a pre-existing correctness bug (`_dispatch()`'s
`done_ids` set) this design will make meaningfully more likely to fire. None
of this requires abandoning the stance's core instinct (reuse existing
mechanisms) — it requires relocating one piece of it and closing gaps neither
prior pass surfaced.

## Framing contests

**F1 — The stance's central claim ("no new state; `done` was always general
enough") never checks whether the project's own canon agrees, and it doesn't.**
`ARCHITECTURE.md:262-268` documents the task lifecycle as:
```
RUNNING ── crash, max retries exceeded ──► FAILED
```
`FAILED` is a status distinct from `DONE` in the project's own architecture
doc — for the *exact* scenario ("crash, max retries exceeded") this design
implements. The actual `_DDL` and `status` column comment (`-- pending |
running | done`) confirm `FAILED` was never built. Neither the stance nor the
Architect noticed the contradiction, despite the stance's epigraph claiming
"written from firsthand reading of `main`'s actual current source" and the
Architect explicitly praising the stance's P3 discipline elsewhere. I lean
toward agreeing `done`+`terminal_reason` is the *right* call substantively —
inventing a real `FAILED` column now would be a bigger, riskier change for
no real gain, and nothing else in the schema treats status values
programmatically beyond the three that exist. But the stance states its
position with more confidence than it earned. Must-fix: either correct
`ARCHITECTURE.md`'s stale diagram as part of this change (retire the phantom
`FAILED` box, replace with "`done`, disambiguated by `terminal_reason`"), or
add an explicit sentence in the stance naming the contradiction and arguing
why the diagram is aspirational/superseded. Silence on a documented
contradiction in your own canon — discovered just by reading the file the
stance cites everywhere else — is exactly the discipline P3 exists to
prevent, applied to this project's own doctrine instead of `loop_spec`.

**F2 — `claim()`-time was never contested as a *placement* choice, only
patched around.** The Architect's A4 treats "subprocess already spawned by
the time `claim()` runs" as a wording nitpick ("cosmetic... doesn't change
the design"). I think it changes the design. See S1 below: `_dispatch()`
(scheduler.py) already runs an eligibility-filter loop with the *identical*
shape this feature needs (`depends_on`/`earliest_start` gates, each a
`continue`-on-fail check inside a `for task in pending:` loop), already has
direct access to `now`, already holds each pending task as a flat dict with
every field this check needs, and genuinely runs *before* `_launch_runner()`
— unlike `claim()`, which the Architect correctly notes runs *inside the
already-spawned subprocess*. The stance anchored on the wrong existing
precedent (`claim()`'s budget gate) because it was the first matching shape
it found, not because it was the best-fitting one. This is the specific
failure mode the brief warned about: staying too close to the code shape
you found first.

**F3 — Neither document says where a task author actually declares
`required_node_class`/`num_gpus`, and the only currently-documented answer
is broken.** `_seed_goals()` (scheduler.py:130-175) is the *only* implemented
producer path other than a raw `queue.post()` call. It reads a goal spec's
YAML file with a bare `yaml.safe_load()` and passes every top-level key not
in a short exclusion list straight into `queue.post()`'s task dict — so if
an author puts `required_node_class: GPU_4090` at the top level of their
goal spec.yaml (the intuitive place, right next to `name`/`kind`/`terminal`/
`executor`), it *does* land in the new DB column correctly (once A2's fix is
applied). But that exact same file is *also* the `spec_path` passed to
`load_spec()` — inside `post()` (swallowed via `except Exception: pass`,
with the comment "spec unreadable at post time — runner will surface it")
and, uncaught, inside `runner.py:43` on *every single turn*. `LoopSpec.
model_config = {"extra": "forbid"}` in loop-spec means that exact file will
fail Pydantic validation on every turn — crashing the turn, requeuing,
crashing again — until the new retry-count breaker bounds it to
`exhausted_retries: <ValidationError: required_node_class Extra inputs are
not permitted>`. A human reading that terminal_reason would reasonably
conclude they hit a real hardware/environment problem. They'd have hit a
schema-authoring trap this feature introduced. This is not hypothetical: it
is the *only* natural, documented way to declare the field, and `post()`'s
own comment already names the crash-and-surface pattern as expected
behavior for bad specs — this stance's own feature is a new way to trigger
it. This deserves the same weight as the Architect's A1: implementing the
stance literally, and then having anyone actually *use* the feature the
obvious way, reproduces the exact unbounded-crash pathology #9 exists to
close, just bounded now instead of infinite. Fix: the stance must state
explicitly that `required_node_class`/`num_gpus` are *not* to be set inside
the loop-spec YAML body (they'd violate `extra: forbid`), and must be passed
as `queue.post()`-level dict keys sitting *alongside*, not inside, the spec
file's own schema — which for the goal-seeding path means documenting that
`_seed_goals()`'s pass-through only works because these keys happen not to
collide with the loop-spec schema's own required fields, not because
anyone designed for this.

## Ontology contests

**O1 — Hardware and dependency-presence are not the same concept phased
over time; they are two different concepts that happen to share a doctrine
slot.** The stance frames `num_gpus`/`required_node_class` (shipped now) and
`requires: list[str]` (deferred) as two halves of one "node readiness"
concept, closed in two passes. I think this conflates two genuinely
different properties. Hardware capability *is* legitimately a
process-lifetime constant for a given scheduler process — a GPU doesn't
appear or vanish mid-run, which is exactly why the stance's "probe once at
startup" model is sound for it. Package/dependency presence is *not* a
process-lifetime constant in the same sense: `runner.py`'s
`_resolve_worktree()` clones `spec.repo` into a *per-task* isolated
worktree, and whatever environment that worktree's own executor needs
(`uv sync`, a specific lockfile, `datasets` installed) is a property of
*that task's clone*, checked fresh at (or after) worktree preparation — not
a property of the node, checked once at scheduler startup. Two different
`MetricOptimizationKind` tasks on the *same* node, same scheduler process,
could have different dependency outcomes depending on their own repo's
lockfile. This means the deferred half will need to live at a different
lifecycle point entirely (inside `run_turn()`, after `_resolve_worktree()`,
with `cwd=work_dir` — genuinely closer to how `measure()`'s `correctness`
gate already works) and a different mechanism (a subprocess-run check
command, not a scalar-comparison). "GPU/node-class now, deps as fast-follow"
isn't phase 1 and phase 2 of one feature — it's two features that happen to
share Article VI's reserved slot. I'd sharpen the Architect's recommendation
("write Article VI narrowly, part one: hardware conformance") further: the
doctrine language should say explicitly that these are two checks at two
different lifecycle points, not imply a smooth continuation, or whoever
designs the deferred half will inherit the "claim()-time, process-lifetime
constant" assumption this design correctly scopes to hardware and
incorrectly risks generalizing.

**O2 — dumping four-and-counting free-text reasons into one column doesn't
break anything programmatically today, but the design never checks the one
place it actually should have.** I grepped the whole repo for anything that
pattern-matches on specific `terminal_reason` *values* to make a decision
(not just display them) — nothing does. So the "does this erase a
distinction the codebase cares about" question the brief posed has a clean
answer for existing consumers: no. But `_dispatch()`'s dependency-completion
check absolutely *should* care and doesn't — see M1. That's not a
`terminal_reason`-vocabulary problem, it's a `done`-semantics problem the
stance's own words ("`done` is not a synonym for success") state correctly
but never trace through to its one real consequence in the codebase.

## Missing dimensions

**M1 — `_dispatch()`'s dependency-satisfaction check does not distinguish
success from any other terminal reason.** Confirmed directly:
`done_ids: set[str] = {t["task_id"] for t in queue.list_tasks("done") if
"task_id" in t}` (scheduler.py:185-187) treats *every* `done` task —
`success`, `node_mismatch`, `exhausted_retries`, `exhausted`, `stalled`,
`cancelled: ...` — as satisfying a child's `depends_on`. This bug already
exists today for `exhausted`/`stalled`/`cancelled`. This design adds two new,
plausibly-common ways for a task to land in `done`-without-succeeding,
raising the odds a hierarchy loop (the `BatchKind`/`SpawnPolicy` swarm
primitive `ARCHITECTURE.md:334-337` names as a Phase 2 target, built directly
on `depends_on`) silently proceeds a child whose parent never actually did
its work. Neither reviewer caught this. Fix scope is small — a
`success_ids` filter (`terminal_reason in (None, "success")`) or an explicit,
documented decision that dependency-completion means "parent reached a
terminal state, full stop, check `terminal_reason` yourself if you care" —
but it must be a *stated* decision, not an accident this design happens to
make more likely.

**M2 — the reaper and the retry-count breaker can race on the same row,
and nobody says who wins.** `_reap_stale()` requeues any `running` task
whose heartbeat is >120s stale, entirely independent of whether the
`runner_proc` subprocess that owns it is truly dead or just slow. If it's
merely slow, it can still be alive when the reaper acts, and can later
write its own `retry_count` increment + terminal decision against a row a
*different*, newer subprocess has already reclaimed. This is a pre-existing
race (today it only corrupts `status`, arguably harmlessly, since a
reclaimed task just gets requeued again); introducing `retry_count` as
persistent counter state the circuit breaker depends on for correctness
makes a zombie-writer scenario newly consequential — a lost or duplicated
increment shifts when `exhausted_retries` fires. Low probability, real.
Worth one acknowledging sentence, parallel to the Architect's own note on
the single-scheduler-process assumption; not worth blocking on a fix.

**M3 — the stance names zero tests.** Not a single acceptance criterion, not
a named test function. Compare to `plans/phase1.md`, this project's own
prior planning doc, which lists explicit `**Acceptance criteria:**` and
named test functions for every task. A Round 1 stance whose whole argument
is "reuse mechanisms that are already tested" should hold itself to the
standard its own prior art already set. Cheap to add; the absence is itself
informative that the observable behavior hasn't been fully thought through
(e.g., what does `test_dispatch_skips_node_mismatch` actually assert?).

**M4 — the CLI doesn't surface `terminal_reason` at all.** I read
`cli.py`'s `status` command directly: it prints `Status: {status}` and turn
history — `terminal_reason` is never referenced. This whole design's value
proposition is "a human reading `terminal_reason` has enough information to
judge for themselves" (the stance's own words, dimension 4/coherence
section). Run `saturate status <task_id>` on a node-mismatched task today
and you'd see `Status: done` and nothing else. The mechanism this design
depends on for legibility is invisible at the one place documented for a
human to look. This is a small, concrete fix (`click.echo(f"Terminal
reason: {task.get('terminal_reason')}")` guarded on `status == 'done'`) that
should ship in the same PR, or the feature's central promise doesn't
actually reach a human.

## Simplicity challenges

**S1 — move the check into `_dispatch()`, not `claim()`.**
`scheduler.py:_dispatch()` already runs a `for task in pending:` loop with
exactly the shape this needs: a `continue`-on-fail eligibility filter,
already checking `depends_on` and `earliest_start` the same way. It has
`now` in scope, and every task it iterates is already a flat dict (from
`queue.list_tasks("pending")`) carrying whatever columns exist — including
the two new ones this design adds. A node-capability check here needs *no*
new spec-load, no reconstruction of a `SaturateTask`, and — critically —
runs strictly *before* `_launch_runner()` calls `subprocess.Popen`, so it
genuinely avoids the cost the stance's own writeup claims to avoid (which
the Architect's A4 correctly shows claim()-time does not). On mismatch,
write the `done`+`terminal_reason='node_mismatch: ...'` UPDATE (identical
shape to `cancel()`, as the stance proposes) and `continue` past the task —
the exact same control-flow shape the `depends_on` gate already uses. This
eliminates A1's entire ambiguity (raise vs. return-None vs. return-task-dict)
because `claim()` is never touched at all — its contract, its callers, its
existing tests all stay exactly as they are today. It also better honors P6
(see below): the queue interface stays completely node-agnostic; node
identity is a property the *scheduler* legitimately owns, not something
`claim()` — a generic, backend-swappable four-operation primitive — should
need to know about. I recommend this as the placement, not a `claim()`-time
gate with a carefully-specified return contract.

**S2 — I evaluated the brief's own suggested simplification (scheduler-side
"N consecutive failures," no `retry_count` column) and it's worse, not
better.** Holding failure counts only in scheduler-process memory would
violate P4 outright — a scheduler restart silently forgets all failure
history, handing every previously-failing task a fresh unbounded-retry
budget across the restart boundary, which is exactly the bug this feature
exists to close. Deriving it from `turns` history instead doesn't work
either: today, a crash in `run_turn()` happens *before* `queue.record_turn()`
is ever called, so no turn record exists for a crash — you'd have to add
one, which is more invasive than the stance's actual `retry_count` column.
The stance's choice here is the right one; I'm flagging that I checked the
brief's alternative independently rather than assuming it and finding it
wanting.

## Principle walk (all 9)

- **P1, P2, P8** — not touched by this design. No violation, not applicable.
- **P3** — applied well to `loop_spec`/`_DDL` (Architect confirmed); *not*
  applied reflexively to the project's own `ARCHITECTURE.md` (F1). Partial
  miss on the exact discipline being praised elsewhere.
- **P4** — mostly honored: `retry_count` is a real column, not
  scheduler-memory (correctly defends against S2's alternative). M2's
  reaper race is a live, if low-probability, threat to "reconstructable from
  the queue alone" — a near-miss, not a clean pass.
- **P5** — I don't think this is violated, and I want to say precisely why
  rather than assert it: `claim()`'s `BEGIN EXCLUSIVE` already guarantees
  only one subprocess can hold a given `task_id` in `running` at a time, so
  `retry_count` increments are inherently task-serial, never concurrent
  across workers — there's no aggregate ceiling N workers could
  collectively blow past, unlike budget. But the stance's own language
  ("a generic circuit breaker... using the field that already exists for
  exactly this") glosses over *why* worker-side placement is safe here when
  P5 forbids it for budget. Add one sentence naming the distinction
  explicitly (task-serial counter vs. cross-claimant aggregate) so a future
  extension doesn't copy "worker-side is fine" onto something that actually
  needs claim()'s vantage point.
- **P6 — real violation, previously uncaught.** The design touches only
  `queue_sqlite.py`. `queue.py` (the file-based `Queue`, per Article V meant
  to be interchangeable, "no code changes in workers or producers") has
  *zero* precondition checks in its `claim()` — not even the existing
  budget check, which I confirmed by reading it directly. This design would
  add node-mismatch enforcement to a second backend-specific place that
  already diverges silently from the file-based backend today. The
  Architect never opened `queue.py` at all. Must be named, and either fixed
  (extend `Queue.claim()` with equivalent semantics — or move the check to
  `_dispatch()` per S1, where it applies uniformly regardless of which
  backend is live) or explicitly scoped as a documented, tracked gap.
- **P7** — the node-mismatch path (mirroring `cancel()`'s raw UPDATE)
  bypasses `complete()`'s `HumanGatedViolation` check, exactly as `cancel()`
  already does today. Pre-existing gap, reused rather than introduced fresh
  — worth a one-line acknowledgment, not a blocker, since a `ClarificationKind`
  task also declaring `required_node_class` is an edge case the schema
  permits but nothing in this codebase's actual usage suggests is common.
- **P9** — solid for the shipped half (Architect verified against real
  `ExecutorSpec`/`LoopSpec`; I agree). Forward risk for the deferred half
  per O1: package-presence checking may legitimately need to touch
  `spec.repo`'s own build/lockfile, which is arguably a loop-spec-relevant
  concern in a way node-class is not — the "keep it Saturate-local" argument
  that's correct here may not transfer.

## Agreements with the Architect

A1, A2, and A3 are all real, and I confirmed each independently against the
same source lines. A1 in particular is correctly identified as the single
highest-leverage gap in the stance-as-written. A4 is factually accurate.

## Disagreements with the Architect

The Architect's fix for A1 (specify `claim()` returns `None` after
committing) patches the symptom without questioning the placement; S1 makes
the whole question moot by moving the check out of `claim()` entirely. The
Architect's Article VI recommendation is right in spirit but should be
sharpened per O1 — not "write it partially," but "name explicitly that these
are two different checks at two different lifecycle points." Most
significantly: the Architect's review only reads `queue_sqlite.py` and never
opens `queue.py`, missing the P6 violation (M/O-adjacent finding above) and
never surfaces `_dispatch()`'s `done_ids` bug (M1) or the CLI's missing
`terminal_reason` display (M4) — all three are concrete, grep-confirmed, and
none were flagged.

## Must-fix before consensus, prioritized

1. Relocate the check from `claim()` to `_dispatch()` (S1) — resolves A1 as
   a side effect, avoids the subprocess-spawn cost for real (closing A4
   properly rather than cosmetically), and keeps the queue interface
   backend-agnostic.
2. State explicitly where `required_node_class`/`num_gpus` are declared —
   never inside the loop-spec YAML body — and name the `extra: forbid`
   collision this stance would otherwise silently reintroduce (F3).
3. Reconcile `ARCHITECTURE.md`'s `FAILED`-state diagram with the actual
   `done`+`terminal_reason` design, or explicitly argue it's superseded (F1).
4. Either extend `queue.py` with equivalent semantics or explicitly document
   the file-based backend's non-parity as a tracked gap (P6).
5. Fix `_dispatch()`'s `done_ids` set to distinguish success from other
   terminal reasons, or explicitly document that it doesn't and why that's
   acceptable (M1).
6. Surface `terminal_reason` in `saturate status` (M4).
7. Carry forward A2 (post() INSERT edit) and A3 (rewrite steps 1-2 to match
   the resolved open question) unchanged — both still must-fix.
8. Add named test functions / acceptance criteria (M3).
9. Sharpen the Article VI language per O1 (two checks, two lifecycle
   points — not one check in two phases).
10. Add the one-line P5 precision note distinguishing task-serial retry
    counting from cross-claimant aggregate enforcement.

## Things I'd let slide

- A3's stale-hedge-language cleanup — real, trivial, doesn't need another
  review round to land correctly.
- A4's exact wording ("before a subprocess spawns" vs. "avoids run_turn()'s
  expensive body") — cosmetic once S1 is adopted (the claim becomes
  literally true rather than needing correction).
- M2 (reaper/retry_count race) — real, low-probability, worth one sentence,
  not worth blocking a design pass on.
- P7's human-gated bypass — worth one sentence acknowledging it's a reused
  pre-existing gap, not worth redesigning `cancel()`'s pattern in this pass.
