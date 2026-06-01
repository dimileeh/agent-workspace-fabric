# Comment 3336985598 Version Globbing Plan

## Problem Statement

The PR review thread reports that `awf_version_matches_install` uses
`for token in $reported`, allowing shell word splitting and pathname expansion
when comparing `awf --version` output to the installed wheel version.

## Scope

- Keep the change local to installer version-token parsing.
- Preserve the existing fallback behavior that accepts a PATH `awf` only when
  its reported version matches the verified install version.
- Add a regression proving glob characters in version output are treated as
  literal output, not expanded filenames.

## Requirements

- [x] Replace unquoted token iteration with whitespace tokenization that does
      not perform pathname expansion.
- [x] Add a focused regression test for glob expansion in reported version
      output.
- [x] Run only the targeted installer regression locally.
- [x] Record validation evidence and note that broad AWF/GitHub validation is
      managed after agent completion.

## Implementation Steps

1. Add a failing test under `tests/unit/installer/test_install_sh_install.py`
   that creates a cwd file named like the expected version and an `awf
   --version` output containing `*`.
2. Update `packaging/install.sh` to normalize reported whitespace into newline
   tokens and read them without glob expansion.
3. Re-run the targeted test and document the result in a validation artifact.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_install.py::test_default_install_rejects_glob_expansion_in_path_awf_version -q`

Pass criteria: the targeted test passes after implementation. Full repository
validation, broad lint/type checks, coverage gates, and CI-equivalent commands
are left to AWF/GitHub after this agent phase.
