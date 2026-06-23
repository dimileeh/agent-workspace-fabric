# Review PRRT_kwDOSJAM6s6Lfbtq Code-Formatted Verdicts Plan

## Problem Statement and Scope

The PR monitor verdict parser matches whole stripped stdout lines against the AWF verdict regex. A reviewer reported that an agent can print the required verdict line as Markdown code, such as `` `AWF-VERDICT: NEEDS_HUMAN: maintainer decision` ``, causing the parser to miss the blocking verdict and fall back to `fix_committed`.

Scope is limited to parsing optional Markdown code formatting around a whole verdict line in `src/awf/runtime/pr_monitor_runner/helpers.py` and focused parser regression tests.

## Requirements Checklist

- Preserve whole-line verdict matching.
- Accept an AWF verdict line wrapped as a Markdown inline code span.
- Accept an AWF verdict line wrapped as a one-line Markdown code fence.
- Keep blocking verdicts such as `NEEDS_HUMAN` and `DEFER` from falling through to `fix_committed`.
- Do not broaden unrelated parser behavior or change PR monitor workflow logic.

## Implementation Steps

1. Add focused failing parser tests for code-formatted AWF verdict lines.
2. Add a small helper that yields the stripped line and, only when the whole line is wrapped in matching backticks, the inner stripped line.
3. Use those line variants for the existing AWF and bare whole-line regex matches.
4. Run the focused parser tests only.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_017.py -q`
- Pass criteria: the focused verdict parser tests pass.

Full AWF/GitHub validation is intentionally left to AWF after agent completion per workspace contract.
