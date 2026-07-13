# Architect Round 2.1 Review — Node Readiness Precondition Check (saturate#9)

*Note on provenance: the subagent dispatched for this pass timed out at 600s
with zero output for the second consecutive Round 2 attempt (10 API calls
this time, 18 last time — a real pattern, not noise: the first Round 1
Architect pass succeeded cleanly at 390s with a comparable brief). Per
P-TIMEOUT discipline, this Planner took the verification directly rather
than dispatch a third blind attempt. This is not a substitute for a fresh
adversarial read from a separate context, and that limitation is named
honestly below rather than hidden.*

## Verdict: APPROVE_WITH_RESERVATIONS

Both corrections (N1, F3) verify as sound against real source. The document
is internally consistent after two layers of self-correction — I found no
stale references to the old buggy allowlist or contradictions between
sections. But direct verification surfaced one genuine gap neither the
Critic's F3 finding nor the Planner's fix addressed: **the seed-time guard
only protects one of two live producer paths.** `saturate submit` — the CLI
command a human would actually run to submit a task directly — has no way
to set `required_node_class`/`num_gpus` at all, correct or otherwise. This
is not a bug in what shipped; it's a completeness gap in what the feature
covers. Small to close, not a redesign.

## N1 fix verification: correct

Re-read every `terminal_reason` write in `runner.py` independently (not
trusting the stance's line citations as ground truth, confirming them):

```
150: "terminal_reason": "exhausted"   (MetricOptimizationKind, max_iterations hit)
156: "terminal_reason": "stalled"     (MetricOptimizationKind, plateau_count hit)
186: "terminal_reason": "success"     (TaskExecutionKind, plan complete on this turn)
233: "terminal_reason": "exhausted"   (TaskExecutionKind, max_iterations hit)
240: "terminal_reason": "success"     (TaskExecutionKind, all tasks done)
```

Plus `cancel()`'s `f"cancelled: {reason}"` (queue_sqlite.py, prefix-matched,
pre-existing) and this design's own two new values (`node_mismatch`,
`exhausted_retries`). A repo-wide grep for any other `terminal_reason` write
site turned up nothing beyond these — the full vocabulary is closed.

The stance's `FAILURE_REASONS = {"node_mismatch", "exhausted_retries",
"exhausted"}` denylist classifies all seven correctly: `success` and
`stalled` (the two genuine completion signals, one per loop kind) pass
through as depends_on-satisfying; `exhausted` (incomplete-run-hit-ceiling, in
both loop kinds) and the two new failure reasons are excluded. `cancelled: *`
is not explicitly in the set but the stance's prose says the set is
"prefix-matched for `node_mismatch: .../cancelled: ...` free-text variants"
— re-reading this, the implementation needs to actually prefix-match
`cancelled` too, which the stance's prose gestures at but the literal
`FAILURE_REASONS` set as written (`{"node_mismatch", "exhausted_retries",
"exhausted"}`) does not include as a member to prefix-match against. This is
a small textual gap, not a design error: the *intent* is clearly stated
(cancelled tasks should not satisfy depends_on — a cancelled parent's work
is not done), but the concrete set literal should read `FAILURE_REASONS =
{"node_mismatch", "exhausted_retries", "exhausted", "cancelled"}` with prefix
matching applied uniformly, or the implementer will correctly prefix-match
`node_mismatch:` variants but silently fail to also catch `cancelled:`
variants since `"cancelled"` bare isn't in the literal set to match against.
One-word fix, worth calling out precisely rather than let an implementer
guess.

## F3 fix verification: correct for `_seed_goals()`, but incomplete — a
second producer path is unguarded

Read `_seed_goals()` (scheduler.py:130-175) directly: it does exactly the
blind top-level-key pass-through the stance describes, and the seed-time
guard placed right after `spec = yaml.safe_load(...)` is the right fix for
this specific path.

But `saturate submit` (cli.py:28-45) is a second, independently-reachable
producer path, and I read it directly:

```python
def submit(spec_path, output):
    """Submit a loop spec to the queue. Prints task_id."""
    q = get_queue()
    spec_path = os.path.abspath(spec_path)
    with open(spec_path) as f:
        spec = yaml.safe_load(f)
    task = {
        "name": spec.get("name", os.path.basename(spec_path)),
        "kind": spec.get("kind", "metric-optimization"),
        "spec_path": spec_path,
    }
    if output:
        task["output_path"] = os.path.abspath(output)
    task_id = q.post(task)
    click.echo(task_id)
```

This does **not** reproduce the `extra:forbid` crash risk — it only reads
three named fields (`name`, `kind`, plus `spec_path`/`output_path` set
directly) from the parsed YAML, never a blind pass-through of arbitrary
top-level keys. So F3's specific finding (schema-authoring trap crashing
every turn) genuinely cannot fire via `submit`.

**But this surfaces a different, previously-uncaught gap: `submit` has zero
mechanism to set `required_node_class`/`num_gpus` at all.** No CLI flag
(`submit`'s only option is `--output`), no YAML pass-through path. A human
running `saturate submit my-loop.yaml` to test this exact feature — the most
natural way anyone would exercise it directly — cannot declare a hardware
requirement through the interface most likely to be used for manual testing
and one-off task submission. The feature is reachable only through
`_seed_goals()`'s automated goal-seeding path. This is not a correctness bug
(nothing crashes); it's a completeness gap the stance's Minimum-viable shape
doesn't name. Recommend: add `--node-class` and `--num-gpus` options to
`submit`, populated into the task dict the same way `output` already is —
small, symmetric with the existing pattern, and it's the only way a human
operator manually testing this feature end-to-end could actually invoke it.

## Independent re-verification of the three previously self-checked claims

I re-derived all three from source myself rather than trusting the Planner's
citations:

**`_dispatch()`'s eligibility-filter shape — confirmed.** Read
`scheduler.py:178-215` directly: `for task in pending:` loop (191), `continue`
on `depends_on` unmet (196), `continue` on `earliest_start` not yet reached
(206-208), `eligible.append(task)` (211) only reached past both gates.
`_launch_runner()` is called later, at line 240, inside a separate `for
score, task in scored:` loop that runs after scoring and sorting —
confirmed strictly after the eligibility filter, and `_launch_runner()`
itself (not shown in this file section but referenced) is what calls
`subprocess.Popen`. The shape matches every claim in the stance.

**`post()`'s hardcoded INSERT — confirmed.** Read `queue_sqlite.py:253-274`
directly: a literal `INSERT INTO tasks (task_id, name, kind, status,
spec_path, state_path, output_path, priority, submitted_at, human_gated,
tokens_used, cost_usd, metadata) VALUES (...)` with an explicit `VALUES`
tuple built from named `task.get(...)` calls — no dynamic column iteration.
Adding `required_node_class`/`num_gpus` as DB columns without this edit
leaves them permanently `NULL`. The stance's Minimum-viable step 2
correctly names this as mandatory.

**`claim()`'s `BEGIN EXCLUSIVE` / P5 task-serial reasoning — confirmed.**
Read `queue_sqlite.py:284-352` directly: `conn.execute("BEGIN EXCLUSIVE")`
at the top of the method body, the budget check happens under this lock
before any `UPDATE ... SET status = 'running'`, and the docstring itself
states the reason: "Queue-level enforcement is the only correct location
for distributed multiplier safety — N concurrent workers cannot each
independently check and collectively exceed the ceiling." This confirms the
stance's distinction: `claim()`'s lock guarantees only one subprocess ever
holds a `task_id` in `running`, so `retry_count` increments on that same
`task_id` are inherently serial across time, never concurrent across
workers — genuinely different from budget's cross-claimant aggregate
concern. The reasoning holds.

## Internal consistency check

Read the document start to finish, including sections not directly named in
my brief. Found no contradictions: the "What changed from Round 1" section,
the Round 2 dimension additions, and the Round 2.1 corrections to dimension
5/Interfaces/Minimum-viable step 4/7 all cross-reference consistently. The
traceability/verdict section at the end correctly reflects the current state
of each item, including honestly marking N1's history (buggy allowlist →
corrected denylist) rather than presenting the corrected version as if it
had always been the design. No section still describes or assumes the old
`terminal_reason in (None, "success")` allowlist — I grepped for the literal
string `"success")` across the document and the only remaining occurrence is
inside the historical explanation of what N1 caught, correctly framed as
past tense.

## Final recommendation

Ready for consensus after two small, mechanical fixes, neither requiring
another full round:

1. **Close the `submit` gap** — add `--node-class`/`--num-gpus` options to
   `saturate submit`, the same pattern as the existing `--output` option,
   so the feature is reachable through the CLI a human would actually use
   to test it, not only through automated goal-seeding.
2. **Tighten the `FAILURE_REASONS` literal** to explicitly include
   `"cancelled"` as a prefix-matched member, matching what the stance's
   prose already says it intends but the set literal as written doesn't
   yet contain.

Both are small additions to an already-settled shape, not new design work.
I'd also name, plainly: this Architect pass did not run as an independent
subagent dispatch — the timeout pattern (two consecutive Round 2 failures
at the 600s ceiling, versus a clean 390s success in Round 1) suggests
something about this specific verification task's shape or the current
session load is triggering it, worth a note for whoever runs the next
`cyclus-plan` session, not something to paper over by treating this
Planner-authored review as equivalent to a truly independent read.
