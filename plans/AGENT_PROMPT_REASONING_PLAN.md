# Agent prompt reasoning upgrades — PLAN

**Topic:** Teach AWF's coding agents to (1) reason deliberately about each review
comment, (2) fix CI/coverage failures the disciplined way, and (3) stop exploding
PRs with unrelated changes. Distilled from the hands-on landing of PR #328 (#304).

**Scope:** 2 source files + 2 test files. No new classes/abstractions. Reuses the
existing shared-constant prompt pattern (`_SAFETY_POLICY`, `_protected_file_policy`).

---

## Problem

When AWF dispatches a coding agent to address review comments or fix CI, the prompts
in `runtime/monitor_prompts.py` already emit a 4-way verdict vocabulary
(FIXED / FALSE POSITIVE / NEEDS_HUMAN / DEFER) and a safety policy — but they give no
**reasoning framework** for *how* to choose, no **coverage-specific** discipline, and
**no scope guardrail**. PR #328 is the cautionary tale: the agent, while chasing the
99% coverage gate, refactored `base.py → _scheduler.py` and ballooned the PR to ~15.7k
insertions / 114 files / 297 commits. The deliverable (host-port detection, +383 lines)
was buried in churn.

## What already exists (reused, not rebuilt)

| Sub-problem | Existing code | Gap this plan closes |
|---|---|---|
| #1 per-comment reasoning | `address_thread_prompt`, `address_review_comment_prompt` already emit the FIXED/FALSE POSITIVE/NEEDS_HUMAN/DEFER tree + `_SAFETY_POLICY` | No *how-to-decide* framework (verify-against-code-first; evidence for FP; don't reflexively comply/dismiss) |
| #2 CI/coverage reasoning | `fix_ci_prompt` says "treat every failure as a real bug, don't weaken the check, focused repro first"; preamble forbids broad local coverage runs | No coverage-specific reasoning (gap vs flaky vs env; behavior tests; anti-theater; exact gate) |
| #3 scope discipline | `quality_gates._build_post_agent_precommit_repair_prompt` ("fix only the hook failures, then stop; don't touch unrelated files"); `remote_prompt_ops` ("smallest corrective edit") | The **preamble / initial build prompt** and the **fix_ci / comment prompts** carry no scope rule — exactly where #328 exploded |

## Locked decisions (from /plan-eng-review)

- **D1 — Scope-discipline home: the cross-cutting preamble.** Add it as rule #6 in
  `_AWF_PROMPT_PREAMBLE` (`adapters/base.py`), which is prepended to *every* agent run
  (initial build, every fix-cycle iteration, monitor). One DRY home covers the full
  #328 failure surface. The #2 coverage block phrases its own "add tests, don't refactor
  unrelated code" nuance at the fix site, so there's no literal duplication.
- **D2 — Concise shared blocks** (style of `_SAFETY_POLICY`), not verbose step-by-step
  frameworks — respects the module's deliberate "terse / finite CLI context" constraint.

## Changes

### 1. `src/awf/adapters/base.py` — `_AWF_PROMPT_PREAMBLE` gains rule #6

```
6. **Keep changes minimal and scoped.** Fix only what THIS task asks.
   Do not refactor, rename, or restructure unrelated code, split files,
   or introduce new abstractions unless the fix strictly requires it. A
   reviewer should see a small, obviously-correct diff; sprawling diffs
   get rejected and waste the run.
```

### 2. `src/awf/runtime/monitor_prompts.py` — new shared `_COMMENT_VERDICT_GUIDANCE`

Reused by both `address_thread_prompt` and `address_review_comment_prompt`, inserted
right before each existing numbered decision list:

```
How to decide (deliberate, do not act reflexively):
  - Verify the claim against the actual code first: locate the exact
    line(s) the reviewer points at and read the surrounding code. The
    feedback is only a false positive if you can point to code that
    already refutes it.
  - Weigh whether it is a real defect or reviewer breadth-conservatism.
    Do not change code to satisfy a wrong review, and do not dismiss
    valid feedback to avoid work.
  - Pick the verdict that fits: FIXED for a genuine correctness/security/
    logic bug or a clearly correct improvement; FALSE POSITIVE only with
    concrete evidence; DEFER for valid but out-of-scope follow-ups;
    NEEDS_HUMAN for a design/taste/protected-file call you cannot make.
  - Keep any fix minimal: change only what THIS comment requires; do not
    refactor unrelated code or expand the PR.
```

### 3. `src/awf/runtime/monitor_prompts.py` — coverage block in `fix_ci_prompt`

Inserted after the existing "Run focused repro commands first…" guidance:

```
If a coverage gate is failing, diagnose the root cause before writing
anything: distinguish a genuine uncovered branch from a flaky/non-
deterministic test or an environment-specific failure that passes in CI.
Read the missing-lines report; do not guess. Close real gaps with tests
that assert BEHAVIOR (inputs -> outputs/effects) — never coverage-theater
(tests that only execute lines to move the number), never weakened
assertions, and never `# pragma: no cover` on a live, reachable path. The
gate is an exact threshold, so a near-miss still fails; prefer one
meaningful test over many shallow ones.
```

### 4. Tests (additive — no existing assertion breaks; 192 are substring `in`)

- `tests/unit/runtime/test_monitor_prompts.py`:
  - `_COMMENT_VERDICT_GUIDANCE` phrases present in **both** comment prompts
    ("Verify the claim against the actual code", "breadth-conservatism",
    "do not refactor unrelated code").
  - Coverage block present in `fix_ci_prompt` ("coverage-theater", "assert BEHAVIOR",
    "pragma: no cover", "exact threshold").
- `tests/unit/adapters/test_adapters.py`:
  - `_AWF_PROMPT_PREAMBLE` contains rule #6 ("Keep changes minimal and scoped",
    "refactor … unrelated").

## NOT in scope

- **Machine-enforced scope guard** (AWF rejecting diffs over N files / blocking
  unrelated-file edits) — a policy/enforcement feature, separate from prompt guidance.
  Defer; revisit if prompt guidance alone proves insufficient.
- **Changing the verdict vocabulary** or adding new AWF-VERDICT codes — the existing
  four are sufficient; only the *reasoning* around them changes.
- **`operator_hint_prompt` / `sync_base_conflict_prompt`** reasoning — not about
  comments or CI; the new preamble rule #6 already covers their scope discipline.
- **`quality_gates` precommit-repair prompt** — already disciplined; left as-is (it is
  the pattern we are matching).

## Failure modes

Pure string builders — no runtime/IO failure path. The only realistic failure is
**guidance drifting from reality** (e.g. the "gate is exact" claim). It is true today
(exact-99% boundary, per the `coverage-gate-boundary` learning). Tests assert the text
is present; correctness of the *claims* is a doc-review concern, not a silent runtime bug.

## Parallelization

Sequential — two files but one logical change; trivial. No worktree split needed.

## Implementation Tasks

- [ ] **T1 (P1, human: ~20min / CC: ~3min)** — adapters — add scope-discipline rule #6 to `_AWF_PROMPT_PREAMBLE`
  - Surfaced by: Architecture (D1) — PR #328 exploded in build + fix cycle
  - Files: `src/awf/adapters/base.py`
  - Verify: `pytest tests/unit/adapters/test_adapters.py -q`
- [ ] **T2 (P1, human: ~30min / CC: ~4min)** — runtime — add `_COMMENT_VERDICT_GUIDANCE` shared constant + wire into both comment prompts
  - Surfaced by: request #1 — no reasoning framework for the existing verdict tree
  - Files: `src/awf/runtime/monitor_prompts.py`
  - Verify: `pytest tests/unit/runtime/test_monitor_prompts.py -q`
- [ ] **T3 (P1, human: ~25min / CC: ~3min)** — runtime — add coverage-failure block to `fix_ci_prompt`
  - Surfaced by: request #2 — no coverage-specific reasoning
  - Files: `src/awf/runtime/monitor_prompts.py`
  - Verify: `pytest tests/unit/runtime/test_monitor_prompts.py -q -k fix_ci`
- [ ] **T4 (P1, human: ~30min / CC: ~4min)** — tests — additive substring assertions for T1–T3
  - Surfaced by: Test review — 4 new content surfaces, all closable
  - Files: `tests/unit/runtime/test_monitor_prompts.py`, `tests/unit/adapters/test_adapters.py`
  - Verify: full clean-env coverage run stays ≥99%

## Follow-up #4 — coverage-as-you-go discipline (added on PR #360 by request)

Every coverage/TDD mention in the prompts was **reactive** (only when CI is already
red). Two additions make it **proactive**, while preserving the rule that agents must
not run the full coverage gate locally:

- **`_AWF_PROMPT_PREAMBLE` rule #7 (`adapters/base.py`)** — cover each behavior you add
  or change with focused tests (test-first when practical) so total coverage does not
  drop; reason about which new lines/branches need a test; *but* for genuinely
  non-behavioral / unreachable / type-only code (e.g. a Protocol stub), add a justified
  coverage exclusion instead of a hollow test. A line-executing test that asserts
  nothing is coverage-theater and is rejected.
- **`_COVERAGE_FAILURE_GUIDANCE` (`monitor_prompts.py`)** — paired the "never `# pragma:
  no cover` on a live, reachable path" rule with its complement: "when the uncovered
  code is genuinely non-behavioral/unreachable/type-only, a justified exclusion is the
  right fix rather than a hollow test." Resolves the exact tension that surfaced in #358.

- [ ] **T5 (P1, human: ~25min / CC: ~3min)** — adapters + runtime — proactive TDD+coverage rule and exclusion-over-theater nuance
  - Surfaced by: user follow-up — TDD must combine with coverage-thinking; prefer exclusions over pointless tests
  - Files: `src/awf/adapters/base.py`, `src/awf/runtime/monitor_prompts.py`, both test files
  - Verify: `pytest tests/unit/runtime/test_monitor_prompts.py tests/unit/adapters/test_adapters.py -q`

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run (internal prompt change, no product scope) |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | not run |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 0 arch / 0 quality / 0 perf issues; 4 test surfaces, all closable |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (no UI) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **UNRESOLVED:** 0 — both decisions (D1 scope-rule home, D2 reasoning depth) locked.
- **VERDICT:** ENG CLEARED — ready to implement. 2 source files, 2 test files, no new
  abstractions, coverage stays ≥99%.
