# Comment 4454303511 Plan

## Problem Statement and Scope

Address PR review comment `issue:4454303511` about
`_looks_like_uv_dependency_setup_command` in `src/awf/runtime/validation.py`.
The reviewer reports that the matcher scans every token for `uv`, so wrapper
commands such as `./bootstrap.sh uv sync` can be classified as direct uv
dependency setup commands.

Scope is limited to anchoring the uv dependency setup command matcher to the
first non-environment-assignment command token and adding regression coverage.

## Requirements Checklist

- Add a regression test proving a wrapper command that passes `uv sync` as
  arguments is not treated as a direct uv dependency setup command.
- Preserve classification for direct uv setup commands, including commands
  prefixed by environment assignments.
- Keep unknown-wrapper retry behavior dependent on package/index evidence in
  command output rather than the presence of `uv` in wrapper arguments.
- Run the narrow relevant unit test(s).
- Commit only the files changed for this review comment.

## Implementation Steps

1. Add a failing unit test in `tests/unit/runtime/test_validation.py` for
   `./bootstrap.sh uv sync` with a transient DNS failure lacking package/index
   evidence.
2. Update `_looks_like_uv_dependency_setup_command` to start scanning at
   `_first_non_assignment_token_index(tokens)`.
3. Re-run the new test and a nearby direct uv classifier test.
4. Create `plans/COMMENT_4454303511_VALIDATION.md` with requirement status and
   evidence.
5. Stage the touched files and commit with the requested conventional message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "wrapper_uv_argument or extracts_uv_pypi_dns_failure"`
  must pass.
