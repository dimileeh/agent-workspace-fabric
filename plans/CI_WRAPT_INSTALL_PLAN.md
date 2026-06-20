# CI wrapt install plan

## Problem statement and scope

PR #614 has a failing GitHub Actions `lint-and-type` check. The job fails during
`uv pip install -e ".[dev]"` before lint or type checks run because the unlocked
dev dependency resolution selects `wrapt-2.2.1` and the metadata fetch from PyPI
fails with a broken pipe. Other Python jobs use `uv sync --extra dev` and install
from `uv.lock`, where `wrapt` is already pinned to `2.1.2`.

Do not edit protected workflow files. Keep the fix scoped to project dependency
metadata and lock consistency.

## Requirements checklist

- [ ] Preserve AWF branch ownership: do not switch branches, push, rebase, or run
  broad AWF/GitHub validation locally.
- [ ] Do not touch protected workflow or quality-gate files.
- [ ] Make the lint/type job's unlocked dev install avoid resolving a newer
  unverified `wrapt` than the checked-in lock.
- [ ] Keep `uv.lock` consistent with `pyproject.toml`.
- [ ] Run focused dependency/lint verification only.
- [ ] Record validation evidence in a matching validation document.
- [ ] Commit the focused fix locally with a conventional commit message.

## Implementation steps

1. Add a narrow dev extra constraint for `wrapt` matching the locked dependency
   line already used by CI coverage shards.
2. Refresh `uv.lock` without upgrading unrelated packages.
3. Run focused checks that exercise dependency resolution and affected metadata.
4. Create `plans/CI_WRAPT_INSTALL_VALIDATION.md` with requirement status and
   command evidence.
5. Commit the plan, dependency metadata, lockfile, and validation document.

## Verification commands and pass criteria

- `uv lock --check`: passes, proving `pyproject.toml` and `uv.lock` agree.
- `uv pip install -e ".[dev]"`: passes locally, matching the failed CI install
  step as closely as possible without running the full lint/type job.
- A focused lint command over touched metadata is not applicable; no Python
  behavior changed. Full AWF/GitHub validation remains managed by AWF after agent
  completion.
