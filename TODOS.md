# TODOS

## Up-front owned_paths ergonomics (reduce protected-file pause friction)

- **What:** Help orchestrators declare the right `owned_paths` *before* a workspace spends tokens —
  e.g., AWF suggests likely-needed protected paths at dispatch based on task intent, or fails fast
  before expensive work when the task obviously needs a protected file.
- **Why:** The "protected-file violation → pause for operator" feature (see
  `~/.claude/plans/cached-baking-haven.md`) is *recovery*: it preserves work and lets the operator
  approve/revert. But if agents *commonly* edit protected dep/config files, the pause becomes a
  human-approval queue. Up-front declaration is the complementary *prevention* that keeps the pause
  rare (reserved for genuine surprises).
- **Pros:** Cuts the approval-queue friction; fewer expensive runs that pause; better DX for
  orchestrators (the assistant included — forgetting `owned_paths` is the original pain).
- **Cons:** Heuristics for "likely-needed protected paths" can be wrong (false suggestions); a
  fail-fast gate risks blocking legitimate exploratory work. Distinct effort from the pause feature.
- **Context:** Surfaced by codex during the `/plan-eng-review` outside-voice pass on the pause
  feature. Prevention vs recovery — deliberately kept OUT of the pause feature's scope so it ships
  focused. Revisit if the pause/approval queue becomes noisy in practice.
- **Depends on / blocked by:** Independent of the pause feature, but most useful *after* it ships
  (you'll know the real pause frequency).

## Provider-recovery thundering-herd circuit-breaker (cap concurrent in-place retries)

- **What:** Bound the number of concurrent `recovering` workspaces that hold a warm stack, with a
  circuit-breaker for a provider-wide outage (free slots past a threshold / fall back to terminal when
  the fleet is saturated).
- **Why:** The in-place provider retry (#612, `plans/PROVIDER_INPLACE_RETRY_PLAN.md`) keeps the warm
  stack during the cooldown. For a single transient blip that's fine, but a provider-wide outage would
  idle-timeout many running workspaces at once → all enter `recovering` and hold their slots
  simultaneously → capacity starvation, then a synchronized re-fire into the still-down provider.
- **Pros:** Bounds worst-case capacity starvation; avoids a synchronized retry storm against a down provider.
- **Cons:** Adds a cap + fallback policy (a second failure path next to warm-hold); premature before we
  see the herd in practice.
- **Context:** Surfaced in the `/plan-eng-review` §4 performance pass on #612; deliberately deferred so
  the single-blip core ships right-sized. The warm-hold is consistent with how `blocked` already holds
  slots for operator pauses.
- **Depends on / blocked by:** #612 (in-place retry) shipping first — you'll know the real herd frequency.

## Create the MergeCandidate at PR-adoption time (close the orphan class)

- **What:** Move `MergeCandidate` creation earlier so that any workspace holding a `pr_url` also holds
  a candidate row, instead of creating it only on the `-> monitoring_pr` transition
  (`WorkspaceRepository._sync_merge_candidate_lifecycle`).
- **Why:** This is the root cause behind the stale merge-queue rows fixed in
  `plans/MERGE_QUEUE_TERMINAL_ROWS_PLAN.md`. PR adoption stamps `pr_url` at workspace creation, but the
  candidate is only created at monitor handoff. A workspace that dies in between (5 did, on 2026-07-21,
  all with `MIRROR_HOOKS_PATH_REPAIR_FAILED`) has a PR and no candidate, so the `-> failed` handler's
  `close_open_for_workspace()` finds nothing to close and silently no-ops. The query fix hides the
  symptom; this removes the cause.
- **Pros:** Eliminates the orphan class outright; makes the legacy fallback query genuinely legacy
  rather than a live catch-all; gives failed adoptions a real close reason and audit trail.
- **Cons:** Touches the path currently handling 561 candidate rows correctly (500 merged, 61 closed).
  Attempt canonicality is decided at monitor handoff today and would need care. Real regression risk
  for a problem the query fix already makes invisible.
- **Context:** Deliberately deferred during the `/plan-eng-review` scope gate (decision D1) so the
  narrow read-path fix could ship focused. Orphan rate is currently zero — the causing infra incident
  was a single day and has not recurred. This is prevention, not firefighting.
- **Depends on / blocked by:** The merge-queue query fix landing first, so the operator view is correct
  while this is designed properly.

## Remove the always-null `merged_at` from the merge-queue API response

- **What:** Delete `merged_at` from `MergeQueueItemResponse` (`api/schemas.py:1300`), regenerate
  `openapi.json`, and update the MCP schema mirror plus console types.
- **Why:** Verified during the merge-queue review: `merged_at` is structurally always null on this
  endpoint. `MergeCandidateRepository.mark_workspace_merged` sets `status="merged"`
  and `merged_at` in the same operation, and the queue only ever returns `open` candidates. Live DB
  confirms it: `open|3|0`, `closed|61|0`, `merged|500|500`. The contract advertises a field it can
  never populate.
- **Pros:** Honest contract; removes a field every consumer must handle and none can receive; small
  and mechanical.
- **Cons:** Removing a response field is a contract change — OpenAPI drift gate, MCP mirror, console
  types, existing fixtures. Meaningful churn for a field that is useless rather than harmful.
- **Context:** The merge-queue fix removed the dead console display but deliberately kept the API
  field to avoid dragging contract regeneration into a narrow bugfix. The outside voice called the
  result a "half-cleaned contract," which is fair — this is the other half.
- **Depends on / blocked by:** Nothing. Independently shippable.

## Define merge-queue membership explicitly (retire the `pr_url IS NOT NULL` predicate)

- **What:** Replace the legacy predicate "has a PR URL and has no candidate" with a stated definition
  of what belongs in the operator merge queue, and settle whether pre-monitor adoption workspaces
  (`requested`, `ready`, `running`) count as merge candidates.
- **Why:** Excluding terminal statuses narrows the symptom without ever defining membership.
  `pr_url IS NOT NULL` is the weak predicate that admitted the orphans in the first place, and it
  stays weak after the fix. `docs/REST_API_REFERENCE.md:426` describes the endpoint as listing "merge
  candidates," which a PR that AWF has not started working on arguably is not.
- **Pros:** Turns an accidental definition into a deliberate one; makes the endpoint match its
  documented description; gives the next workspace status an obvious place to be classified.
- **Cons:** A genuine contract decision, not a cleanup. Needs a real answer on whether an
  adopted-but-not-yet-started PR is a merge candidate, and that answer changes operator workflow.
  Getting it wrong costs more than the bug it follows.
- **Context:** Raised by the Codex outside voice during `/plan-eng-review` as the sharpest criticism of
  the narrow fix. Test G5 in that plan was deliberately narrowed (asserting only `blocked`,
  `awaiting_human`, `pushing`, `monitoring_pr` stay visible) specifically so this question stays open
  rather than being settled by accident inside a bugfix.
- **Depends on / blocked by:** Best done after candidate-at-adoption-time, which would make the legacy
  path genuinely legacy and shrink this question considerably.

## Diff-budget scope governor (pause runaway deliveries)

- **What:** Pause a workspace (protected-file-style block → guide resume) when its working diff
  exceeds a configurable multiple of the task's declared footprint (files/lines, workspace.yml).
- **Why:** PR #808's 4-file task became 125 files (+26.7k lines) over 72h with no mechanism noticing.
  Scope is a protected resource; the pause-for-operator pattern already exists and works.
- **Pros:** Bounds runaway deliveries at the source; reuses the proven block/guide flow.
- **Cons:** Footprint declaration is fuzzy (tests legitimately multiply lines); bad thresholds nag.
- **Context:** Lesson 1 of the #808 incident (2026-08-09..12; operator watched a 30x diff with only
  directives available). Minted by the dev→main promotion review.
- **Depends on / blocked by:** nothing.

## Review-thread triage tier: out_of_scope → file issue

- **What:** New agent verdict `out_of_scope` for bot review threads: AWF replies with rationale,
  files a tracking issue, resolves the thread, changes no code. Unlike defer/needs_human it does not
  block merge once the issue exists. Guardrails: per-PR cap + operator-visible audit.
- **Why:** #808's reviewer filed 323 threads, all treated as must-address; threads like "support
  Google web-identity overrides" drove architecture that belonged in issues.
- **Pros:** Breaks the reviewer→scope-growth feedback loop; preserves signal in the tracker.
- **Cons:** Over-use ships under-reviewed code; needs the cap + visibility guardrails.
- **Context:** Lesson 2 of the #808 incident — the arms race ended only when the reviewer ran dry.
- **Depends on / blocked by:** nothing; complements the scope governor.

## CI-starvation priority inversion in the monitor

- **What:** After N consecutive comment-repair cycles with a standing CI failure whose logs exist
  from a completed run, dispatch the CI repair first for one turn, folding open threads into that
  same run's prompt.
- **Why:** #808: 208 comment repairs vs ~2 effective CI-repair turns over 40h — each push cancelled
  the in-flight run, starving the only prompt that carries pytest logs.
- **Pros:** Converges the actual bottleneck; operator directive #2 during the incident was exactly
  this and it worked — productize it.
- **Cons:** Touches decide()'s ordering — the most invariant-sensitive monitor code; gate carefully.
- **Context:** Lesson 3 of the #808 incident.
- **Depends on / blocked by:** nothing.

## Pathology-based operator summons (livelock/thrash detectors)

- **What:** NotifyHuman on delivery pathology, not just rule breaches: sustained pushes/hour above X;
  K consecutive CI runs cancelled pre-completion; same test failing across M repair pushes; monitor
  age beyond Y hours. All computable from existing workspace events.
- **Why:** #808's pathology was metric-visible from hour 6; the summons came at hour 70 via a rule
  breach. A human reading event streams was the actual detector.
- **Pros:** Cheap (events exist); the enriched needs-human comment now carries itemized context.
- **Cons:** Threshold tuning; false summons erode signal trust.
- **Context:** Lesson 4 of the #808 incident; pairs with the scope governor.
- **Depends on / blocked by:** nothing.

## Plan-consistency checking (contradictory requirements)

- **What:** Explicit consistency pass in plan review (gstack skill and/or AWF conformance):
  enumerate binding requirements, check pairwise for contradiction against the codebase before
  execution.
- **Why:** ROOT cause of #808's 72h: "reuse the repair invocation machinery" vs "re-ask must be
  read-only" (repair container mounts the worktree RW) — decidable at review time; two reviews
  checked each requirement individually, neither checked them against each other. The agent resolved
  the contradiction its own way, at maximal scope.
- **Pros:** Kills the incident class furthest upstream; pilotable as a review step immediately.
- **Cons:** Hardest of the five to make deterministic; risks becoming a checkbox.
- **Context:** Lesson 5 of the #808 incident.
- **Depends on / blocked by:** nothing.
