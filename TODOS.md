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
