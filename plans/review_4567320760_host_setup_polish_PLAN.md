# Review 4567320760 Host Setup Polish Plan

## Problem Statement And Scope

Review comment `issue:4567320760` calls out two low-risk host setup follow-ups:

- Document that the duplicate-key YAML loader intentionally rejects
  merge-then-override YAML patterns because it checks keys after PyYAML
  `flatten_mapping`.
- Replace the immutable source-checkout marker tuple `default_factory` with a
  direct `Field(default=...)`.

The fix is limited to `src/awf/host_setup/config.py`,
`src/awf/host_setup/source_assets.py`, and this plan/validation evidence. No
GitHub writes, branch changes, pushes, protected-file edits, or broad
AWF/GitHub validation are in scope.

## Requirements Checklist

- Add a concise code comment or docstring note explaining the intentional YAML
  merge-key behavior before future maintainers infer it is accidental.
- Use `Field(default=SOURCE_CHECKOUT_REQUIRED_MARKER_PATHS)` for immutable
  source-checkout marker metadata.
- Preserve existing host setup config and source-checkout behavior.
- Keep local checks focused to the changed host setup files and their existing
  unit-test surface.
- Document validation evidence in
  `plans/review_4567320760_host_setup_polish_VALIDATION.md`.

## Implementation Steps

1. Update the duplicate-key mapping constructor documentation in
   `src/awf/host_setup/config.py`.
2. Update `SourceCheckoutAssetMetadata.markers` in
   `src/awf/host_setup/source_assets.py` to use a direct immutable default.
3. Run focused host setup tests and targeted static checks for the touched
   Python files.
4. Record validation status and evidence.
5. Stage and commit only the files changed for this review comment.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py src/awf/host_setup/source_assets.py tests/unit/service/test_host_setup_config.py
uv run --python 3.12 --extra dev mypy src/awf/host_setup/config.py src/awf/host_setup/source_assets.py
```

Pass criteria: the focused host setup unit file passes, ruff reports no issues
for the touched Python/test files, and mypy accepts the touched host setup
modules. Full AWF/GitHub validation is intentionally left to AWF after agent
completion.
