# PR295 Lint-And-Type Mypy Unused Ignore Validation

## Result

Satisfied. The `lint-and-type` failure was caused by a version-sensitive
`type: ignore[no-untyped-call]` on the PyYAML object-construction wrapper in
`src/awf/host_setup/config.py`.

The fix replaces the ignore with a small `_YamlObjectConstructor` protocol and
casts the custom safe loader to that protocol before calling
`construct_object`. This preserves runtime behavior while avoiding both:

- CI's newer mypy/types-PyYAML `unused-ignore` failure.
- The workspace's older mypy/types-PyYAML direct-package `no-untyped-call`
  failure when the ignore is removed outright.

## Focused Evidence

```bash
uv run --python 3.12 --extra dev mypy src/awf/host_setup
# Success: no issues found in 3 source files

uv run --python 3.12 --extra dev ruff check src/awf/host_setup tests/unit/service/test_host_setup_config.py
# All checks passed!

uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup tests/unit/service/test_host_setup_config.py
# 4 files already formatted

uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
# 59 passed in 1.09s

uvx --python 3.12 --with mypy==2.1.0 --with pydantic==2.13.4 --with pydantic-settings==2.14.1 --with PyYAML==6.0.3 --with types-PyYAML==6.0.12.20260518 mypy --config-file pyproject.toml src/awf/host_setup
# Success: no issues found in 3 source files
```

## CI Status Notes

GitHub Actions run `26608385324` showed:

- `lint-and-type`: failed at mypy with the unused ignore.
- `console`: passed.
- `release-artifacts`: passed.
- `python-full-coverage`: still in progress when inspected; broad coverage
  validation is AWF/GitHub-owned after agent completion.

Attempting to fetch the in-progress coverage job log returned a GitHub
`BlobNotFound` response, so no additional coverage failure evidence was
available during this focused fix cycle.
