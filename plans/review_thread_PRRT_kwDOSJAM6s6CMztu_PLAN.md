# Review Thread PRRT_kwDOSJAM6s6CMztu Plan

## Problem Statement And Scope

PR review feedback reports that setup dependency network retry classification can
misclassify a chained setup command such as `pip install -r requirements.txt &&
./bootstrap`. The install-shaped first command currently makes the whole shell
command look like dependency setup, so transient DNS or timeout text from a
later non-dependency step can consume the dependency retry budget and finish
with `SETUP_DEPENDENCY_NETWORK_FAILURE`.

Scope is limited to setup dependency network classification in
`src/awf/runtime/validation.py` and focused unit regression coverage in
`tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Add a regression test proving a chained install command with only later
  bootstrap network output is not classified as a setup dependency network
  failure.
- Preserve classification for chained commands when the failing output itself
  contains dependency/index evidence.
- Keep existing deterministic failure and package-manager verb behavior intact.
- Do not change retry-loop accounting or executor behavior outside this
  classifier.

## Implementation Steps

1. Add failing unit coverage for the chained non-dependency bootstrap failure.
2. Add positive unit coverage for a chained command whose output identifies a
   dependency/index fetch failure.
3. Update command-shape matching so compound shell commands cannot rely solely
   on the first install verb; compound commands must be classified from failing
   output evidence.
4. Run the narrow validation command for the touched tests, then broaden to the
   runtime validation unit file if needed.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`

Pass criteria: all tests in the touched runtime validation unit file pass, and
the regression test fails before the implementation change when practical.
