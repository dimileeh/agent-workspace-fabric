## Problem Statement And Scope

The mirror hooks repair path probes and unsets `core.hooksPath` with direct
`git config` subprocesses. Review feedback reports those subprocesses inherit
ambient Git object lookup overrides such as `GIT_OBJECT_DIRECTORY` and
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, which can make otherwise valid bare-mirror
config commands fail before repair.

Scope is limited to `repair_mirror_hooks_path` and its focused regression tests.

## Requirements Checklist

- Add a regression test showing mirror hooks repair ignores inherited Git object
  lookup environment overrides.
- Apply the existing Git object lookup env cleanup to both `git config`
  subprocesses in `repair_mirror_hooks_path`.
- Preserve existing repair/no-op/error behavior.

## Implementation Steps

1. Add a focused unit test under `tests/unit/node/test_git_manager.py`.
2. Confirm the new test fails before the implementation change when practical.
3. Pass `git_env_without_object_lookup_overrides()` to both direct config
   subprocesses in `repair_mirror_hooks_path`.
4. Run the focused node git-manager tests relevant to this behavior.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py -q -k 'RepairMirrorHooksPath or VerifyHeadObjectExists'`

Full AWF/GitHub validation is managed by AWF after agent completion.
