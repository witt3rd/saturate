# Critic Round 2 Review — Node Readiness Precondition Check (saturate#9)

## Verdict: APPROVE_WITH_RESERVATIONS

The relocation to `_dispatch()` is correctly incorporated and cross-checked
clean against the real `scheduler.py`. Every one of my ten Round 1 must-fix
items is addressed in substance. But the M1 fix — the one change this
revision introduces beyond what I asked for — has a real correctness bug of
its own, not caught by either prior pass, and F3's "document it" resolution
is thinner than the stated risk warrants. Neither requires re-architecting;
both are small, scoped edits. This does not need a Round 3 — it needs two
named line-item fixes before merge.

## S1 incorporation check: correct

I cross-read the revised dimension 3 and "What changed from Round 1" against
`scheduler.py:178-243` directly. `_dispatch()`'s real shape matches every
claim: a `for task in pending:` loop (191-211) with existing `continue`-on-
fail gates for `depends_on` (196) and `earliest_start` (206-208), `now` in
scope as a parameter, each `task` a flat dict from `queue.list_tasks
("pending")` (180), and `_launch_runner()` called later (240) — strictly
after `eligible.append(task)` (211), CPU-saturation check (228-233), and
scoring/sort (216-223). The stance's proposed placement — "inside the
existing `for task in pending:` block, alongside the `depends_on`/
`earliest_start` continue-on-fail checks" — is not a paraphrase of my S1, it
is the identical mechanism, correctly cited by line. `claim()` is genuinely
untouched: `runner_proc.py:53` still calls the plain `queue.claim()` with no
new parameters. This is the label AND the mechanics, both correct.

## Round 1 findings — resolution status

**F1 (ARCHITECTURE.md `FAILED` contradiction) — resolved as a stated
position, correctly deferred to this re-review.** Confirmed
`ARCHITECTURE.md:263-267` still reads exactly as I cited it. The stance's
position (retire the phantom `FAILED` box, replace with `done` disambiguated
by `terminal_reason`, ship the doc fix in this same PR) is the right call —
see my independent verdict below.

**F2 (placement was never contested) — resolved.** Superseded by S1's
adoption; there is no longer a `claim()`-time placement to contest.

**F3 (extra:forbid schema-authoring trap) — partially resolved; needs
enforcement, not just documentation.** I confirmed `LoopSpec.model_config =
{"extra": "forbid"}` at `loop-spec/python/loop_spec/__init__.py:193`. The
stance's fix is a stated convention only: "`required_node_class`/`num_gpus`
must never be set inside the loop-spec YAML body." This is the *correct*
convention, but a convention with zero enforcement in the exact place an
author would naturally violate it (`_seed_goals()`, `scheduler.py:130-175`)
is precisely the failure class the brief warned about, and the stance's own
prose names the consequence: the resulting crash looks like a hardware
problem, not an authoring mistake. There is a cheap, concrete enforcement
point available and unused: `_seed_goals()` already parses the raw YAML dict
(`spec = yaml.safe_load(...)`, line 148) before doing the pass-through. A
one-line guard there — raise or log a clear, specific error if
`required_node_class`/`num_gpus` appear in that dict — converts a delayed,
misleading `ValidationError` buried inside a turn crash into an immediate,
actionable seed-time error. "Document it" is not enough when the failure
mode it's guarding against is actively misleading; the fix is small enough
that deferring it isn't justified by cost.

**O1 (hardware vs. dependency-presence category error) — resolved, not
flattened.** Dimension 5's Round 2 addition is a faithful restatement: same
distinction (process-lifetime constant vs. per-task-clone-dependent),
same specific lifecycle point named for the deferred half (inside
`run_turn()`, after `_resolve_worktree()`), same mechanism named (subprocess
check command, not scalar comparison), same explicit statement that this is
two features sharing one doctrine slot, not two phases of one feature. This
is the strongest single Round 2 addition in the document.

**O2 (four-and-counting free-text reasons) — resolved as originally scoped;
its one real consequence (M1) is where the fix landed short.** See M1 below.

**M1 (`done_ids` doesn't distinguish success) — partially resolved; the
specific filter introduced is itself buggy for `MetricOptimizationKind`.**
See new finding N1.

**M2 (reaper/retry_count race) — untouched, correctly left as an
acknowledged low-probability risk.** Not addressed in this revision and
doesn't need to be; I flagged it as "worth one sentence, not worth
blocking" in Round 1 and stand by that.

**M3 (named tests) — resolved, one gap.** Step 7 lists four named tests.
`test_dispatch_success_ids_excludes_non_success` as described ("a child
`depends_on` a `node_mismatch`-terminated parent does not proceed") tests
correct exclusion but would not catch N1 below — it needs a sibling case
asserting a `MetricOptimizationKind` parent that terminates via `stalled`
*does* satisfy a child's dependency, once N1 is fixed.

**M4 (CLI doesn't surface `terminal_reason`) — resolved.** Step 6 adds the
`click.echo` guarded on `status == 'done'`.

## New finding

**N1 — the `success_ids` filter (`terminal_reason in (None, "success")`)
silently excludes every `MetricOptimizationKind` task, including ones that
converged correctly, because `"success"` is exclusively a
`TaskExecutionKind` literal.** I grepped every production write of
`terminal_reason` in `runner.py`: the `MetricOptimizationKind` path (lines
37-161, the main body of `run_turn()`) writes exactly two terminal reasons —
`"exhausted"` (149-151, ran out of `max_iterations`) and `"stalled"`
(155-157, hit `plateau_count`) — and never writes `"success"` anywhere.
`"success"` is written only by `_run_task_execution_turn()` (186, 240), the
`TaskExecutionKind`/`ClarificationSpec` path. For `TaskExecutionKind` the
proposed filter is exactly right — `"success"` genuinely means success,
`"exhausted"` genuinely means an incomplete plan. For
`MetricOptimizationKind`, there is no terminal reason meaning "success" in
the vocabulary at all: `"stalled"` (plateau reached, no further improvement
found) is the loop's actual convergence signal — the closest thing this
codebase has to "the optimization succeeded" — and under the literal filter
as written, it would be treated as a *non*-success, permanently excluding
any `MetricOptimizationKind` parent from ever satisfying a child's
`depends_on`, even the ones that converged well. This inverts the failure
mode M1 was written to close: Round 1's bug was "everything satisfies
`depends_on`, including failures" (too permissive); this fix, as specified,
would make "no `MetricOptimizationKind` task ever satisfies `depends_on`,
including successes" (too restrictive) — and it lands directly on the
`BatchKind`/`SpawnPolicy` swarm primitive (`ARCHITECTURE.md:334-337`) that
M1 itself cites as the motivating downstream consumer, which is precisely
where `MetricOptimizationKind` parents with dependent children are most
likely to appear. Fix is small and doesn't require redesign: either (a)
widen the set to `terminal_reason in (None, "success", "stalled")` and
document that `stalled` means "reached its terminal condition without
finding further improvement," which is Saturate's stated definition of
convergence for this loop kind, or (b) invert the polarity — define the
*failure* set explicitly (`{"node_mismatch", "exhausted_retries",
"cancelled"}`-prefixed reasons plus bare `"exhausted"` if that's meant to
signal incompleteness) and treat everything else as satisfying, which is
more robust against future terminal-reason additions than an allowlist. I
lean toward (b): an allowlist (`success_ids`) requires every new terminal
reason a future loop kind introduces to be manually added to stay
`depends_on`-satisfying, which is exactly the kind of silent gap M1 itself
exists to close. A denylist of known-bad reasons degrades more gracefully.
Either way, this needs to be decided and named explicitly, not left as the
current filter's implicit (and wrong) answer.

## Instruction #5 follow-ups, briefly

**5(a) — scoping out `queue.py` (must-fix #4) is consistent with doctrine,
with one sharpening.** `scheduler.py` was already `SqliteQueue`-specific
before this design (it type-hints `queue: "SqliteQueue"`, reads
`queue._db_path` directly) — this design doesn't make *that* worse. The
genuine Article V concern is narrower than "scheduler.py isn't
backend-agnostic": it's that the *behavioral guarantee* (bounded retries,
hardware-mismatch detection) is backend-dependent, so identical loop specs
behave differently under `queue.py` vs. `queue_sqlite.py`. Naming this and
deferring the fix satisfies Article III's actual discipline (name the gap,
don't paper over it). One addition worth making when Article VI is
eventually written: it should state its scope explicitly as "SqliteQueue
backend only" — the same discipline the stance already applies to the
hardware-vs-dependency split should apply here too, so Article VI doesn't
silently overstate a guarantee that `queue.py` doesn't yet provide.

**5(b) — the "mutually exclusive by construction" claim is correct as
written, and the race it doesn't claim to cover is a separate, pre-existing
concern.** Confirmed `_dispatch()` runs in the scheduler's own process and
`claim()` runs inside the spawned `runner_proc` subprocess
(`_launch_runner()` at `scheduler.py:290` calls `subprocess.Popen`;
`claim()` fires at `runner_proc.py:53`). Under the Round 2 design, `claim()`
never reads node capability at all — the capability probe is read exactly
once, by `_dispatch()`, before spawn — so there is no race window where a
second reader could see a different capability value; there is only one
reader. The specific claim in the stance is about two *mechanisms* (the
node-mismatch gate and the retry-count breaker) not colliding on the same
task, and that holds: a task either fails the gate and is marked `done`
before ever reaching `claim()`/spawn, or passes the gate and is spawned, in
which case only the crash-handler path can touch `retry_count`. This is
airtight for a single task. What it does *not* address, and doesn't claim
to: nothing in `_dispatch()` marks a task as "already dispatched" before
`_launch_runner()` returns, so if `scheduler_tick()` is invoked again before
the first spawned subprocess actually calls `claim()`, the same still-
`pending` task could be dispatched a second time. This is a pre-existing
scheduler-tick-cadence race, not introduced or worsened by #9, and out of
scope for this design — but worth one acknowledging sentence somewhere so a
future reader doesn't conflate "the two new mechanisms don't race" with "no
double-dispatch is possible."

## Final verdict on ARCHITECTURE.md fix — my own, independent of the Architect

Yes, fix it in this same PR. The Architect didn't weigh in on this in Round
1 (F1 was Critic-only). My own reasoning: this design is the artifact that
actually settles the `done`+`terminal_reason` vs. `FAILED` question — the
fix is small (retire one state-diagram box, replace with a sentence about
`terminal_reason` disambiguation), and DOCTRINE.md's own Article III
discipline argues against letting a known, documented contradiction in the
project's canonical architecture doc sit unaddressed once it's been named
and the exact PR that resolves the ambiguity is in hand. Deferring it to a
follow-up doc PR would leave `ARCHITECTURE.md` actively wrong about the
system's real behavior for an indeterminate span, for no real savings.

## Recommendation

Ready for consensus after two named fixes, neither requiring another full
Architect+Critic round:

1. **N1 — fix the `success_ids` filter** to account for `stalled` as
   `MetricOptimizationKind`'s genuine convergence signal (widen the
   allowlist, or better, invert to a denylist of known-failure reasons).
   Add the missing test case: a `MetricOptimizationKind` parent terminating
   via `stalled` satisfies a child's `depends_on`.
2. **F3 — add the seed-time guard** in `_seed_goals()` rejecting
   `required_node_class`/`num_gpus` in the raw YAML dict with a specific,
   actionable error, rather than relying on stated convention alone.

Both are mechanical, both are scoped inside code this design already
touches, and neither changes the shape the Planner and Architect have
already converged on. Recommend the Planner apply these two fixes directly
and move to consensus rather than spending a Round 3 on re-review — the
architecture is sound; what remains is tightening two lines of logic this
revision itself introduced.
