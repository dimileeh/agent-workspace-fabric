# UV Clean Install Smoke Validation

## Result

Complete. The clean install smoke now uses `uv venv` plus
`uv pip install --python` instead of `python -m venv` plus `pip install`, so it
does not depend on stdlib `ensurepip` / the Debian `python3.12-venv` package.

## Evidence

The targeted smoke that previously skipped now passes locally:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_clean_install_smoke.py::test_uv_venv_install_help -rs -vv
```

Passed: `1 passed`.

The full clean install smoke module passes without skips:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_clean_install_smoke.py -q
```

Passed: `2 passed`.

Touched-file lint passed:

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_clean_install_smoke.py tests/packaging_build.py
```

Full lint passed:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
```

Full coverage passed:

```bash
uv run --python 3.12 --extra dev pytest -n 20 --cov=awf --cov-report=term-missing
```

Passed: `11674 passed`; required coverage `99.0%` reached with `99.01%`
total coverage.

## Notes

Environmental skips remain possible when `uv` is missing or dependency
resolution cannot fetch/cache runtime dependencies. Non-environmental wheel
install failures still fail loudly so malformed metadata, incompatible
`Requires-Python`, malformed archives, or missing `awf` entry points remain
package-artifact regressions.
