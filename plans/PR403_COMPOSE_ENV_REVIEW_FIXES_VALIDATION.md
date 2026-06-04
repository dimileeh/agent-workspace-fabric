# PR403 Compose Env Review Fixes Validation

## Result

The review-comment fixes are implemented.

- Legacy migration no longer emits unsafe single-quoted values when a value
  contains both `$` and `'`; it emits double-quoted values with escaped dollars
  instead.
- Host-side Compose env interpolation now gives the caller environment
  precedence over previously parsed env-file values.
- Host-side Compose env interpolation now raises
  `ComposeEnvInterpolationError` for failed `:?` and `?` mandatory operators.
- Double-quoted escape handling preserves `\n`, `\r`, `\t`, `\\`, `\"`, and
  `\$` semantics before interpolation.

## Validation

Passed:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_env_migration.py tests/unit/service/test_environment.py -q
```

Passed as part of the broader touched-file set:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_common_helpers.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_service_gc_cli.py tests/unit/service/test_env_migration.py tests/unit/service/test_environment.py -q
```

Passed:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/cli/common.py src/awf/service/env_migration.py src/awf/service/environment.py tests/unit/cli/test_common_helpers.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/service/test_env_migration.py tests/unit/service/test_environment.py
uv run --python 3.12 --extra dev ruff format --check src/awf/cli/common.py src/awf/service/env_migration.py src/awf/service/environment.py tests/unit/cli/test_common_helpers.py tests/unit/cli/test_workspace_commands_helpers.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/service/test_env_migration.py tests/unit/service/test_environment.py
uv run --python 3.12 --extra dev mypy src/awf
```

## Remaining Risk

Full sharded coverage was not run locally because shard 1 takes roughly 18
minutes in CI, but the exact CI failure and touched behavior surface were
reproduced and validated locally.
