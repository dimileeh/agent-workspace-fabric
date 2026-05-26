# LLM Usage Console Accuracy Validation

## Result

Implemented.

- ccusage `cachedInputTokens` and `reasoningOutputTokens` are now normalized,
  baseline-subtracted, persisted in usage snapshots, exposed through workspace
  API responses, and rendered by the console when present.
- The console no longer shows "pricing not configured" when the usage payload
  already has a concrete `cost_estimate` from ccusage or another source.
- OpenAPI was regenerated for the expanded LLM usage schema.

## Live Diagnosis

For `ws_a73b2c4c7b7e47e484da61b2`, ccusage reports totals where:

- `totalTokens = inputTokens + cachedInputTokens + outputTokens`
- `reasoningOutputTokens` is a visible subset of output accounting, not an
  additional total component.

The old UI showed only input/output/total, which made the total look wrong
whenever cached input was nonzero.

## Validation Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/service/test_workspaces_observability_parts tests/unit/api/test_tasks.py tests/unit/api/test_workspaces_parts/test_workspaces_part_00{1,2,3,4,5,6,7}.py -q
```

Result: `373 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/usage_store.py src/awf/service/usage_collection.py src/awf/service/workspace_observability.py src/awf/api/schemas.py tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_003.py tests/unit/api/test_tasks.py tests/unit/api/test_workspaces_parts/test_workspaces_part_00{1,2,3,4,5,6,7}.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/usage_store.py src/awf/service/usage_collection.py src/awf/service/workspace_observability.py src/awf/api/schemas.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
npm --prefix apps/console run build
```

Result: passed.

```bash
npm --prefix apps/console exec -- playwright test dashboard-usage.spec.ts --config <temporary existing-server config>
```

Result: `4 passed`.
