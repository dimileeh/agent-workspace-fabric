# Review 4491715538 GitHub Script Input Keys Plan

## Problem Statement and Scope

Review comment `issue:4491715538` reports that protected workflow
classification rejects safe `actions/github-script` comment/notify steps when
their `with:` mapping contains legitimate non-script inputs such as
`result-encoding`, `retries`, `retry-exempt-status-codes`, or `debug`.

Scope is limited to the `actions/github-script` comment/notify input allowlist,
focused unit regression coverage, and required plan/validation artifacts.

## Requirements Checklist

- Allow pinned `actions/github-script` comment/notify steps that include a safe
  comment script plus non-security inputs supported by the action.
- Keep unknown `actions/github-script` `with:` keys blocked for unowned
  protected workflow changes.
- Keep unsafe scripts and unsafe GitHub Actions expressions blocked.
- Preserve existing SHA-to-SHA pinned action behavior and documentation; this
  limitation is already covered by docs/tests.
- Preserve existing coverage-policy behavior where a `fail_under` change plus
  another coverage policy change reports both violations.

## Implementation Steps

1. Add a focused failing regression in
   `tests/unit/control/test_quality_gates.py` for an added comment step using
   `actions/github-script` with a safe script and additional allowed inputs.
2. Run the new regression before implementation and confirm it fails where
   practical.
3. Add an explicit `actions/github-script` allowed-`with` key set in
   `src/awf/control/quality_gates.py`.
4. Update `_github_script_comment_notify_inputs_are_safe` to accept allowed
   non-script keys while still requiring and validating `script` when `with:`
   is present.
5. Run focused tests, then ruff on the touched Python files.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_added_github_script_step_with_comment_script_and_safe_options_is_allowed -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "github_script_step"
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py
```

Pass criteria: the new regression fails before implementation, passes after the
classifier change, and nearby GitHub-script regression tests remain green.
