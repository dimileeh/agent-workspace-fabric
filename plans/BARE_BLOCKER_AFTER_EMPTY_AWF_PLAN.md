# Bare Blocker After Empty AWF Plan

## Problem Statement and Scope

An empty or placeholder `AWF-VERDICT: FIXED:` line can currently suppress a later bare fallback verdict such as `NEEDS_HUMAN:` or `DEFER:` because the parser ignores all bare verdicts as soon as any AWF-prefixed verdict is present. This can incorrectly mark unresolved review feedback as fixed.

Scope is limited to verdict parsing in `src/awf/runtime/pr_monitor_runner/helpers.py` and focused regression coverage for that parser behavior.

## Requirements Checklist

- Add a focused regression proving an empty non-blocking AWF verdict does not suppress a later bare `NEEDS_HUMAN` fallback.
- Preserve the existing contract that reasoned AWF-prefixed verdicts remain canonical over bare fallback lines.
- Keep the implementation narrow, without changing PR monitor flow outside verdict parsing.
- Run only targeted tests for the changed parser behavior; broad AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a unit test in the existing verdict parser test module for `AWF-VERDICT: FIXED:` followed by bare `NEEDS_HUMAN: ...`.
2. Run the targeted test and confirm it fails against the current parser.
3. Update the AWF verdict selection logic so a reasonless non-blocking AWF result may fall back to a later bare blocking/follow-up verdict.
4. Run the targeted parser tests affected by this behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py -q`

Pass criteria: the focused verdict parser tests pass, including the new regression. Full suite, coverage, and CI-equivalent validation are intentionally not run in the agent phase.
