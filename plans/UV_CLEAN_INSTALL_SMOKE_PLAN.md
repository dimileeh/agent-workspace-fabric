# UV Clean Install Smoke Plan

## Summary

Replace the skip-prone `python -m venv` + `pip install` clean wheel smoke with
the same `uv venv` + `uv pip install --python` pattern used by the CI
release-artifacts wheel smoke. This preserves the clean package-artifact guard
without requiring Debian/Ubuntu's `python3.12-venv` / `ensurepip` package.

## Implementation

- Rename the clean install test to make the `uv` install lane explicit.
- Create the temp environment with `uv venv --python 3.12 <tmp>/venv`.
- Install the built wheel with `uv pip install --python <venv>/bin/python <wheel>`.
- Run the installed `<venv>/bin/awf` help commands from outside the checkout.
- Preserve provenance verification by importing `awf` from the temp environment.
- Keep environmental skips for missing `uv`, unavailable Python setup, or
  dependency fetch/cache failures.
- Keep non-environmental wheel install failures as test failures.

## Validation

- Target the renamed clean install smoke directly.
- Run the full clean install smoke module.
- Run ruff on touched test/helper files.
- Rerun the full coverage suite with `-n 20`.
