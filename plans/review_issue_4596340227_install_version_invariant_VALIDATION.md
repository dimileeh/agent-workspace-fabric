# Review issue 4596340227 install version invariant validation

Plan reference: `review_issue_4596340227_install_version_invariant_PLAN.md`

## Requirement Status

- Move `INSTALL_VERSION="$expected_version"` after the wheel version comparison:
  Complete. `packaging/install.sh` now assigns it only after the
  `VERSION_MISMATCH` guard has passed.
- Preserve the unpinned, versionless-manifest path:
  Complete. The existing early return still assigns `INSTALL_VERSION` from the
  wheel version when there is no manifest version or pinned version to compare.
- Preserve installer regression coverage:
  Complete. Focused installer tests covering artifact version checks and PATH
  fallback identity/runnability passed.
- Commit the focused fix locally:
  Complete. The commit was created after this validation document was written.

## Evidence

Files changed:

- `packaging/install.sh`
- `plans/review_issue_4596340227_install_version_invariant_PLAN.md`
- `plans/review_issue_4596340227_install_version_invariant_VALIDATION.md`

Focused validation run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/installer/test_install_sh_artifact_name.py tests/unit/installer/test_install_sh_install.py::test_default_install_rejects_stale_path_awf_when_bin_dir_empty tests/unit/installer/test_install_sh_install.py::test_default_install_accepts_matching_path_awf_when_bin_dir_empty tests/unit/installer/test_install_sh_install.py::test_default_install_matching_path_awf_that_fails_help_is_not_reachable -q
```

Result: `12 passed in 1.02s`.

Full AWF/GitHub validation was not run inside the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
