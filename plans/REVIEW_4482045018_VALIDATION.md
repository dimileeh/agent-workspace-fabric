# Review 4482045018 Validation

Plan reference: `plans/REVIEW_4482045018_PLAN.md`

## Requirement Status

- Complete: `_docker_cli_environ` now mirrors `DOCKER_HOST` from the runtime
  `AWF_DOCKER_HOST` value instead of `ServiceSettings.docker_host`.
  Evidence: `src/awf/service/bootstrap.py`.
- Complete: Added regression coverage for divergent
  `ServiceSettings.docker_host` and runtime `AWF_DOCKER_HOST`.
  Evidence:
  `tests/unit/service/test_bootstrap.py::test_bootstrap_mirrors_runtime_awf_docker_host_when_settings_differ`.
- Complete: Added a transition note for non-source-checkout service commands
  that now read `.env` unless run from a verified AWF source checkout.
  Evidence: `docs/GETTING_STARTED.md`.
- Complete: Refactored `collect_support_bundle` to call `_doctor_collector`
  once with optional `compose_file` kwargs.
  Evidence: `src/awf/service/support_bundle.py`.
- Complete: Ran narrow relevant tests and style checks.
  Evidence: commands below.
- Complete: The scoped files are ready for the required local commit.

## Verification Evidence

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py tests/unit/service/test_support_bundle.py -q
```

Result: passed, `40 passed in 4.32s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/bootstrap.py src/awf/service/support_bundle.py tests/unit/service/test_bootstrap.py
```

Result: passed, `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: passed, `Success: no issues found in 155 source files`.

## Gaps

None.
