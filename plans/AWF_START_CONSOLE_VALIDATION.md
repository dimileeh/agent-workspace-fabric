# AWF Start Console Validation

## Result

Complete. `awf start` now starts the local web console by default, supports
`--headless`, supports `--console-port <PORT>`, reports the overridden console
URL, and keeps lower-level service bootstrap console startup opt-in.

## Plan Coverage

- CLI flags added: `--headless` and `--console-port`.
- `awf start` passes `ServiceBootstrapOptions(start_console=True)` by default.
- `awf start --headless` passes `start_console=False` and omits console-open
  success guidance.
- `awf start --console-port <PORT>` injects `AWF_CONSOLE_HOST_PORT` before
  settings resolution and bootstrap delegation.
- Bootstrap appends the `console` Compose stage only when requested.
- `ServiceSettings.console_url` derives from `AWF_CONSOLE_HOST_PORT` unless an
  explicit `AWF_CONSOLE_URL` is configured.
- Explicit `AWF_CONSOLE_URL` precedence is covered for host environment,
  service environment, and settings-derived values.
- Public first-run docs describe default console startup, headless mode, and
  console port override.

## TDD Evidence

Before implementation, the new tests failed because:

- `awf start` did not expose `--headless` or `--console-port`.
- `ServiceBootstrapOptions` had no `start_console` field.
- `AWF_CONSOLE_HOST_PORT` did not derive `ServiceSettings.console_url`.
- Invalid `AWF_CONSOLE_HOST_PORT` values were not rejected.

## Passing Validation

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_start_commands.py -q
```

Passed: `47 passed`.

Final rerun after branch-coverage additions: `48 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap_parts/test_bootstrap_part_003.py -q
```

Passed: `10 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_001.py -q
```

Passed: `92 passed`.

Final rerun after branch-coverage additions: `95 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_public_docs_status.py -q
```

Passed: `26 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/command_grammar_drift_parts -q
```

Passed: `23 passed`.

```bash
uv run --python 3.12 --extra dev pytest -n 20 --cov=awf --cov-report=term-missing
```

Passed: `11673 passed, 1 skipped`; required coverage `99.0%` reached with
`99.01%` total coverage.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
```

Passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Passed: `Success: no issues found in 354 source files`.

## Notes

The raw Docker Compose public-doc example now uses
`docker compose up -d --build`; the docs contract test was corrected to require
the detached single-command raw Compose path.
