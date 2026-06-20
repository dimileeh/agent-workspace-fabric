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
