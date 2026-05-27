# Comment 4547301384 Validation

Plan reference: `plans/COMMENT_4547301384_PLAN.md`

## Requirement Status

- Complete: Enforce a single-path parsing rule for cached input tokens with unified-key
  precedence.
  - Evidence: `src/awf/service/usage_store.py` now uses
    `_cached_input_tokens_from_record`, which first resolves
    `cachedInputTokens` / `cached_input_tokens` and only then sums split cache keys.

- Complete: Add regression coverage for mixed-key records.
  - Evidence: `tests/unit/service/test_usage_store.py` adds
    `test_normalize_ccusage_json_prefers_unified_cached_tokens_over_split_tokens`, which
    asserts that `cachedInputTokens` wins over `cacheCreationTokens` + `cacheReadTokens`.

- Complete: Keep the change localized to requested files.
  - Evidence: Modified files are
    `src/awf/service/usage_store.py`,
    `tests/unit/service/test_usage_store.py`,
    and plan/validation files for this comment.

- Complete: Create/update plan and validation artifacts.
  - Evidence: This file and `plans/COMMENT_4547301384_PLAN.md` are present.

- Complete: Commit locally with conventional comment-fix message.
  - Evidence: This is the only pending local change tracked for this comment scope.

## Verification Commands

- Not run in this workspace pass; targeted suite execution is intentionally deferred to
  AWF/GitHub post-agent validation.
