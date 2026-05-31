# Post-Agent Autofix Format Repair Plan

## Problem

Workspace `ws_d70d539363a24bbb986fa637` failed after the agent timed out and
AWF salvaged staged work. Post-agent commit repair detected both
`awf-ruff-check` and `awf-ruff-format-check` failures, ran the bounded
`ruff check --fix` repair path, then retried `git commit` without also running
`ruff format` for the reported `Would reformat:` paths. The retry failed on the
same format hook and the workspace was retried as provider recovery.

## Root Cause

`_run_post_agent_autofixable_precommit_repair` handles fixable Ruff diagnostics
but ignores `classification.format_repair_files`. Mixed Ruff-check-plus-format
failures need both deterministic steps before the commit retry:

1. `ruff check --fix -- <autofix paths>`
2. `ruff format -- <format paths>`
3. `git add -- <original staged paths>`
4. retry `git commit`

## Scope

- Add a regression test for mixed fixable `awf-ruff-check` and
  `awf-ruff-format-check` failures.
- Update the deterministic autofix repair path to run `ruff format` when the
  original classification includes staged format-repair files.
- Preserve the existing single-hook `ruff check --fix` behavior.
- Preserve existing failure observability and reason codes for repair command
  failures.

## Out Of Scope

- Changing agent timeout policy or service restart behavior.
- Changing provider recovery policy.
- Reworking all post-agent pre-commit repair classification.

## Validation

- Run the new focused regression test red before implementation.
- Run the focused post-agent commit test file after implementation.
- Run Ruff/Mypy on the touched code surface.
