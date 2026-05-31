# Comment 4396834428 Plan

## Problem Statement and Scope

Address CodeRabbit review-level comment `4396834428` for PR #342. The local code
already appears to satisfy the Cursor runtime Dockerfile and provider-readiness
runtime probe portions, so this plan focuses on the still-valid marker precision
and documentation clarity items while preserving existing Cursor auth coverage.

## Requirements Checklist

- Verify existing Dockerfile Cursor installer wiring remains non-root usable and
  fail-fast; do not edit it unless the local code contradicts the review.
- Verify existing Cursor provider readiness checks include runtime CLI probing;
  do not edit it unless the local code contradicts the review.
- Tighten Cursor provider inference so a bare `cursor` substring does not
  attribute unrelated output to the Cursor provider.
- Remove the generic `please authenticate` auth marker unless a provider-specific
  Cursor marker is also present through another signal.
- Preserve classification for concrete Cursor auth failures such as
  `cursor-agent`, `CURSOR_API_KEY`, `cursor api key`, `cursor auth`, and
  `cursor authentication`.
- Clarify the release-readiness provider filter example in
  `docs/REST_API_REFERENCE.md` so readers understand the Cursor example and
  repeated provider query parameters.

## Implementation Steps

1. Add focused regression tests in `tests/unit/adapters/test_provider_failures.py`
   proving unrelated cursor-pagination/auth text is not classified as Cursor
   auth while concrete Cursor markers still infer `cursor`.
2. Run the focused adapter test file to confirm the new regression test fails
   against the existing broad markers, when practical.
3. Update `src/awf/adapters/provider_failures.py` marker lists with stricter
   Cursor phrases and remove the generic auth phrase.
4. Update the REST API release-readiness example/note with a concise repeated
   provider filter example that includes Cursor.
5. Run targeted validation only for the changed behavior/docs surface; full
   AWF/GitHub validation remains owned by AWF after this agent phase.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py -q -k "cursor"`
  passes or is recorded with any environment-specific blocker.
