# PRRT_kwDOSJAM6s6Djswo Literal Pathspec Validation

Plan reference: `PRRT_kwDOSJAM6s6Djswo_LITERAL_PATHSPEC_PLAN.md`

## Requirement Status

- Treat recovered refspec paths as literal Git paths for both index and tree
  missing-path probes: Complete. `_git_refspec_missing_path_is_recoverable`
  now passes `:(literal)<path>` to both `git ls-files` and `git ls-tree`.
- Preserve the existing exact returned-path comparison: Complete.
  `_git_z_listing_contains_path` still compares returned records against the
  original unmodified path.
- Add regression coverage for metacharacter paths on both `HEAD:path` and
  `:path` refspec forms: Complete. Added focused tests for tree and index
  probes with pathspec metacharacters.
- Run narrow unit tests for protected-file diff loading: Complete.
- Do not switch branches, push, or alter unrelated files: Complete.

## Evidence

Files changed:

- `src/awf/control/protected_file_diffs.py`
- `tests/unit/control/test_protected_file_diffs.py`
- `plans/PRRT_kwDOSJAM6s6Djswo_LITERAL_PATHSPEC_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6Djswo_LITERAL_PATHSPEC_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q -k 'literal_pathspec'
```

Initial TDD result: failed because recovery probes passed raw paths to Git.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py -q
```

Result: `18 passed in 2.28s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/protected_file_diffs.py tests/unit/control/test_protected_file_diffs.py
```

Result: `All checks passed!`.

## Gaps

None.
