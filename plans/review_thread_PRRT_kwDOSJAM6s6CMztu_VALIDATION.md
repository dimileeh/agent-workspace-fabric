# Review Thread PRRT_kwDOSJAM6s6CMztu Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6CMztu_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a chained install command with only
  later bootstrap network output is not classified as a setup dependency network
  failure.
- Complete: Preserved classification for chained commands when the failing
  output itself contains dependency/index evidence.
- Complete: Kept existing deterministic failure and package-manager verb
  behavior intact by running the full runtime validation unit file.
- Complete: Did not change retry-loop accounting or executor behavior outside
  the classifier.

## Evidence

- Changed `src/awf/runtime/validation.py` so compound shell commands cannot rely
  solely on the first dependency-install verb; they require dependency/index
  evidence from failing output.
- Changed `tests/unit/runtime/test_validation.py` with chained-command
  regression coverage.
- Confirmed TDD failure before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k 'chained'`
  failed the new bootstrap regression and passed the positive dependency-output
  case.
- Verification passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  with `184 passed`.

## Gaps

None.
