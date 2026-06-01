# Missing Companion Image Detector Review Fix Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GPTGd` reports that `_compose_up_reports_missing_image`
can treat a Compose failure for a different service image as a missing pre-built
companion image when the same stderr/stdout also mentions the companion tag.

Scope is limited to the missing-image detector in `src/awf/node/stack_launcher.py`
and focused unit coverage in `tests/unit/node/test_stack_launcher_companion_images.py`.

## Requirements Checklist

- Add a regression test proving that a companion tag mentioned elsewhere in
  Compose output does not trigger retry when the missing-image phrase names a
  different image.
- Preserve existing retry behavior when Docker/Compose explicitly reports the
  specific companion tag as missing.
- Keep validation focused to the changed detector tests; full AWF/GitHub
  validation is managed after agent completion.

## Implementation Steps

1. Add the failing regression test beside the existing missing-image detector tests.
2. Tighten `_compose_up_reports_missing_image` to require missing-image wording
   that names the candidate companion tag.
3. Run the focused test module or selected detector tests.
4. Commit the code, tests, plan, and validation document locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_companion_images.py -q`
  should pass.
- No broad workspace validation, full coverage gate, frontend build, push, or
  branch switch will be run in the agent phase.
