# Host Setup Config Parent Permissions Plan

## Problem Statement and Scope

An unresolved PR review thread reports that `write_host_setup_config()` chmods
the parent directory of every explicit config path to `0700`. That is too broad
for caller-owned paths such as a repository, current directory, or shared temp
directory. The change is scoped to host setup config write permissions and its
unit tests.

## Requirements Checklist

- Preserve conservative `0700` parent permissions for the helper-owned default
  AWF config directory.
- Do not chmod the parent directory for an explicit caller-supplied config path.
- Continue writing config files atomically with `0600` file permissions.
- Add a regression test for the explicit-path parent permission behavior.
- Run focused tests for `tests/unit/service/test_host_setup_config.py` only;
  full AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a failing unit test that writes to an explicit path in a caller-owned
   directory and asserts the parent mode is unchanged on POSIX hosts.
2. Update `write_host_setup_config()` so parent chmod is conditional on using
   the default path (`path is None`).
3. Adjust existing default-path coverage, if needed, so it exercises the
   helper-owned default path directly.
4. Run the focused host setup config unit test file.
5. Record validation evidence in
   `plans/HOST_SETUP_CONFIG_PARENT_PERMISSIONS_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  must pass.
- No broad repository validation or full coverage gate will be run in the
  agent phase.
