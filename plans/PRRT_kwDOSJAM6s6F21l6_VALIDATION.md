# PRRT_kwDOSJAM6s6F21l6 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F21l6_PLAN.md`

## Requirement Status

- `docs/awf-plans/ws_123.json` is classified as an internal plan artifact:
  Complete. Covered by `tests/unit/common/test_owned_paths.py`.
- `docs/awf-plans/ws_*.json` is filtered out of inter-workspace owned paths:
  Complete. Covered by `tests/unit/common/test_owned_paths.py`.
- Existing generated `ws_*.md` and `ws_*.conformance.json` paths remain
  internal: Complete. Existing common helper coverage still passes.
- Real documentation paths under or near `docs/awf-plans/` remain ordinary
  owned paths: Complete. `docs/awf-plans/**`, `docs/awf-plans/README.md`,
  nested paths, and dotted non-generated JSON names remain negative cases.
- Focused tests and lint cover the touched helper: Complete.

## Evidence

Changed files:

- `src/awf/common/owned_paths.py`
- `tests/unit/common/test_owned_paths.py`

Focused failing check before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q
```

Result before implementation: failed as expected with 3 failures for
`docs/awf-plans/ws_123.json`, `docs/awf-plans/ws_*.json`, and inter-workspace
filtering of plain `.json` generated reports.

Focused passing checks after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q
```

Result: 20 passed.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/common/owned_paths.py tests/unit/common/test_owned_paths.py
```

Result: All checks passed.

Full AWF/GitHub validation was not run inside the agent phase; AWF owns broad
validation, provenance, and merge gating after agent completion.
