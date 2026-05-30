# Comment 4571563982 Redacted Key Suffix Plan

## Problem Statement and Scope

PR review comment `issue:4571563982` reports that first-run rendering can
generate `#N` collision keys that clobber user-supplied keys already shaped like
`[redacted]#N` when those user-supplied keys appear later in the mapping.

Scope is limited to first-run rendering redaction/deduplication behavior and its
unit regression coverage.

## Requirements Checklist

- Add a regression test where generated redacted-key collisions appear before a
  literal `[redacted]#2` key and the literal key remains unchanged.
- Update redacted mapping key deduplication so generated suffixes skip natural
  keys present in the source mapping, independent of iteration order.
- Preserve existing behavior for ordinary collision suffixing and JSON-safe key
  coercion.
- Run focused unit tests for `tests/unit/service/test_host_setup_rendering.py`.
- Do not run AWF/GitHub-owned broad validation; record that AWF handles the full
  validation surface after agent completion.

## Implementation Steps

1. Add the failing regression in `tests/unit/service/test_host_setup_rendering.py`.
2. Update `src/awf/host_setup/rendering.py` to reserve natural transformed keys
   before assigning generated `#N` collision suffixes.
3. Run the focused test before and after the implementation where practical.
4. Create `plans/COMMENT_4571563982_REDACTED_KEY_SUFFIX_VALIDATION.md` with
   requirement-by-requirement evidence.
5. Commit the scoped changes locally with a conventional commit message.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q
```

Pass criteria: the focused host setup rendering unit tests pass, including the
new suffix reservation regression. Full AWF/GitHub validation is intentionally
left to AWF after the agent phase.
