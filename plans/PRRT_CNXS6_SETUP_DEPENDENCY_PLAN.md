# PRRT_CNXS6 Setup Dependency Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6CNXs6` reports that setup dependency network
classification can misattribute a transient network error from a later step in a
compound shell command to an earlier dependency install step. The narrow scope is
`src/awf/runtime/validation.py` setup dependency classification and focused unit
coverage in `tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Add a regression test for a compound command where dependency install output
  succeeds or progresses, then a later bootstrap command emits the transient
  network error.
- Preserve retry classification for compound commands when the dependency/index
  fetch failure itself contains package or index evidence.
- Keep existing deterministic and secret-redaction behavior unchanged.
- Run the narrow unit tests that prove the classifier behavior.

## Implementation Steps

1. Add the failing regression test to `tests/unit/runtime/test_validation.py`.
2. Update the setup dependency context guard so compound commands require
   dependency/index evidence in the same output fragment as the transient
   network failure, instead of anywhere in combined output.
3. Run the targeted tests for setup dependency classification.
4. If the narrow tests expose a gap, iterate on the implementation and rerun.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q
```

Pass criteria: all tests in the focused validation test module pass, including
the new compound-command regression and the existing positive compound
dependency-fetch case.
