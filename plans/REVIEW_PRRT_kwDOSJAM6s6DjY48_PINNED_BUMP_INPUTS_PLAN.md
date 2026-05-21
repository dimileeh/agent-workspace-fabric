# Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6DjY48` reports that allowed pinned workflow
action bumps are rejected when an unchanged sensitive `with` input is already
present. The current final safety sweep checks all new inputs, so a
`python-version` bump on `actions/setup-python` can fail because an unchanged
`token` input still has a sensitive name.

Scope is limited to the pinned workflow bump input safety check in
`src/awf/control/quality_gates.py` and a focused regression test.

# Requirements Checklist

- Add a regression test proving an allowed `python-version` bump is accepted
  when an existing sensitive input is unchanged.
- Keep additions, removals, and modifications of unapproved or sensitive inputs
  blocked.
- Keep unsafe GitHub Actions expressions in changed allowed input values
  blocked.
- Validate with the focused regression test and the quality-gates unit tests.

# Implementation Steps

1. Add the failing regression test in `tests/unit/control/test_quality_gates.py`.
2. Update `_workflow_pinned_bump_with_inputs_are_safe` to run the final
   name/value safety check only on changed inputs after the allowed-key and
   add/remove checks pass.
3. Run the new single test before and after implementation, then run the
   quality-gates test module.

# Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_allows_unchanged_sensitive_with_input -q`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  must pass.
