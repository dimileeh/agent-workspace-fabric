# PR403 Compose Env Nested And Single Quote Fix Validation

## Result

The two new PR #403 review comments are addressed.

- Single-quoted env-file values now honor escaped quotes such as
  `PHRASE='Let\'s go!'`.
- Single-quoted values remain uninterpolated after decoding escaped quotes, so
  `AWF_API_TOKEN='sup\'$er'` becomes `sup'$er` instead of expanding `$er`.
- Env-file interpolation now scans balanced nested `${...}` expressions, so
  `${CUSTOM_DIR:-${HOME}/.awf/service}` resolves like Compose.

## Validation

Initially failed as expected:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_environment.py::test_compose_env_file_values_honors_escaped_quote_in_single_quoted_values tests/unit/service/test_environment.py::test_compose_env_file_values_expands_nested_default_interpolation -q
```

Passed after the fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_environment.py tests/unit/service/test_env_migration.py -q
```

Passed:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/environment.py tests/unit/service/test_environment.py
uv run --python 3.12 --extra dev ruff format --check src/awf/service/environment.py tests/unit/service/test_environment.py
uv run --python 3.12 --extra dev mypy src/awf
```
