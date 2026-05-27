# Companion Dependency Cycle Rejection Validation

Plan reference:
`plans/REVIEW_PRRT_kwDOSJAM6s6FKbQk_COMPANION_CYCLES_PLAN.md`

## Requirement Status

- Add regression coverage for a self-dependent companion: Complete.
- Add regression coverage for a cycle between requested companions: Complete.
- Preserve existing unknown dependency validation behavior and reason code:
  Complete.
- Raise `ProfileResolutionError` before Compose launch for circular dependency
  graphs, with a stable reason code: Complete.
- Keep validation local to companion/profile service graph validation: Complete.

## Evidence

Files changed:

- `src/awf/node/companion_services.py`
- `tests/unit/node/test_companion_services.py`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
```

Result: failed with the two new circular dependency regressions not raising
`ProfileResolutionError`.

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q
```

Result: `9 passed in 0.41s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py tests/unit/node/test_companion_services.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/node/companion_services.py
```

Result: `Success: no issues found in 1 source file`.

Full AWF/GitHub validation was not run during the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after agent completion.

## Gaps

No known gaps.
