# Review Thread PRRT_kwDOSJAM6s6B6XLF Plan

## Problem Statement

The PR review reports that `load_primary_failure_snapshot` anchors snapshots to
the newest failed `workspace.state_changed` event even when that event does not
carry `primary_failure`. A later cleanup or stale failure event without embedded
primary evidence can therefore hide an older durable primary snapshot.

## Scope

- Verify the reported lookup behavior against current code.
- Add a focused regression proving that an older failed event with
  `primary_failure` wins over a newer failed event without it.
- Keep fallback behavior unchanged when no failed event carries
  `primary_failure`.
- Avoid unrelated failure causality refactors.

## Requirements Checklist

- `load_primary_failure_snapshot` prefers the newest failed state event whose
  payload contains `primary_failure`.
- It falls back to the generic newest failed event when no preserved-primary
  event exists.
- Validation/provider primary fields in the preserved payload remain intact even
  after workspace row failure fields are mutated by a later secondary failure.
- Focused service tests pass.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_failure_causality.py`.
2. Add a narrow failed-event query that filters for `primary_failure` in the
   JSON payload and use it before the generic latest failed event.
3. Run the focused regression suite and static checks justified by the touched
   module.
4. Write `plans/review_thread_PRRT_kwDOSJAM6s6B6XLF_VALIDATION.md` with evidence.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: the focused service suite passes, lint passes for touched files,
and mypy remains clean for `src/awf`.
