# Comment 4547301384 Plan

## Problem Statement and Scope

PR review comment `issue:4547301384` requests preventing cached-token double-counting
if a single ccusage record contains both unified cached-token keys and split
cache-token keys. The scope is limited to `usage_store.py` token extraction and its
existing unit tests.

## Requirements Checklist

- Enforce a single-path parsing rule for cached input tokens so unified cached token
  keys (`cachedInputTokens` / `cached_input_tokens`) take precedence when present.
- Add regression coverage ensuring mixed-key records use the unified field and do not
  sum split-key values on top of it.
- Keep the change localized to `src/awf/service/usage_store.py` and
  `tests/unit/service/test_usage_store.py`.
- Create/update corresponding plan and validation artifacts in `plans/` and keep the
  diff review-comment-scoped.
- Commit locally with a conventional message for this fix.

## Implementation Steps

1. Introduce explicit cached-token parsing logic that prefers unified keys and only
   falls back to split keys when unified keys are absent.
2. Replace `_usage_from_record` cached input assignment to use the new logic.
3. Add a regression test covering a record containing both unified and split cache
   key families.
4. Stage only touched files and commit with a conventional comment-fix message.

## Verification Commands and Pass Criteria

- Review the changed unit test and code path to ensure mixed-key normalization now
  resolves to a single source.
- (No local validation execution is performed in this workspace pass per agent policy; full
  verification is handled by AWF after agent handoff.)
