# PRRT_kwDOSJAM6s6DJDM3 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DJDM3_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving direct `collect_support_bundle()`
  calls with `compose_env_file` and no explicit `provider_environ` pass compose
  env values to both status and doctor collectors.
- Complete: Preserved explicit `provider_environ` precedence in
  `_resolve_provider_environ()`.
- Complete: Used `local_service_environ()` so compose env values are merged with
  host environment override semantics.
- Complete: The regression verifies that compose-only provider secrets are
  included in support-bundle redaction.
- Complete: Ran focused support bundle tests plus lint and file-level type
  checks.

## Evidence

Files changed:

- `src/awf/service/support_bundle.py`
- `tests/unit/service/test_support_bundle.py`
- `plans/PRRT_kwDOSJAM6s6DJDM3_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DJDM3_VALIDATION.md`

Verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py::test_support_bundle_resolves_provider_environment_from_compose_env_file -q
```

Result before implementation: failed as expected because
`AWF_GITHUB_TOKEN` was missing from the captured provider environment.

Result after implementation: passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_support_bundle.py -q
```

Result: 13 passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/support_bundle.py tests/unit/service/test_support_bundle.py
```

Result: all checks passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/support_bundle.py
```

Result: success, no issues found.
