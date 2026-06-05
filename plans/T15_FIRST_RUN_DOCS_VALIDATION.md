# T15 First-Run Docs Validation

Plan reference: `plans/T15_FIRST_RUN_DOCS_PLAN.md`

## Result

Complete. The PR branch has been reduced back to the T15 docs scope: public
first-run docs, focused docs tests, and concise plan/validation artifacts.

## Scope Check

- Removed the generated dotenv-parser guidance from Quickstart, Upgrade, and
  Uninstall.
- Removed the monitor-created docs helper/test sprawl from the effective diff.
- Kept the curl installer lane release-gated until hosted installer URL,
  manifest, checksums, and release artifacts are published and verified.
- Documented setup, start, `awf init <path>`, mocked smoke, upgrade, and
  uninstall paths for the runnable lanes.
- Added a regression test that rejects reintroducing the parser-script blocks.

## Validation Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Passed: `26 passed in 0.49s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Passed: `1 passed in 0.14s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init_parts/test_init_part_004.py::test_getting_started_recommends_setup_start_then_project_init -q
```

Passed: `1 passed in 0.14s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs -q
```

Passed: `48 passed in 1.83s`.

Rerun after the final Quickstart root-env note:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs -q
```

Passed: `48 passed in 1.62s`.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_public_docs_status.py tests/unit/cli/test_init_parts/test_init_part_004.py
```

Passed: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli_parts/test_service_cli_part_001.py tests/unit/cli/test_packaging.py -q
```

Passed: `46 passed in 1.03s`.
