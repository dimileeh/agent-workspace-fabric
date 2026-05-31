# Base URL Unification Plan

## Problem Statement And Scope

AWF currently has host CLI URL configuration split across a static CLI default
(`http://localhost:8000`) and the deprecated `AWF_CLI_BASE_URL` override. That
creates first-run friction when local Compose publishes the API on a customized
host port such as `AWF_API_HOST_PORT=8800`: the API is reachable on `:8800`,
but the host `awf` CLI still targets `:8000`.

This change introduces `AWF_BASE_URL` as the single operator-facing host CLI
knob while preserving `AWF_API_BASE_URL` as the API/service self-reference URL
used by service-side doctor, smoke, and status flows. It does not collapse the
two network addresses, does not make the server read `AWF_BASE_URL`, and does
not add MCP HTTP configuration.

## Requirements Checklist

- Host CLI base URL precedence is:
  `--base-url` > `AWF_BASE_URL` > deprecated `AWF_CLI_BASE_URL` >
  `http://localhost:${AWF_API_HOST_PORT:-8000}` > `http://localhost:8000`.
- `AWF_API_HOST_PORT=8800` with no other host CLI URL makes the CLI target
  `http://localhost:8800`.
- `AWF_CLI_BASE_URL` remains supported and emits one deprecation notice per
  process when `AWF_BASE_URL` is absent.
- `AWF_BASE_URL` usage does not emit the deprecated-variable notice.
- `AWF_API_BASE_URL` / `Settings.api_base_url` resolution remains unchanged
  and is explicitly tested against accidental `AWF_BASE_URL` coupling.
- URL normalization continues to use existing `normalize_api_url` call sites;
  the resolver only selects the base URL string.
- Docs and examples present `AWF_BASE_URL` as the operator-facing host CLI/API
  knob, mark `AWF_CLI_BASE_URL` deprecated, and explain that
  `AWF_API_BASE_URL` is the service/in-cluster self-reference URL.
- No branch switching, pushing, rebasing, or local commits.

## Implementation Steps

1. Add CLI tests covering full precedence, `AWF_API_HOST_PORT` derivation,
   fallback behavior, and the `AWF_CLI_BASE_URL` one-time deprecation notice.
2. Add service config regression tests proving `AWF_BASE_URL` is ignored by
   `resolve_service_settings` and `_resolve_service_api_base_url`, while
   `AWF_API_BASE_URL=http://api:8000` is still honored.
3. Implement CLI resolver changes in `src/awf/cli/common.py` with a small
   process-local deprecation notice flag.
4. Clarify CLI/config docstrings without changing service resolution behavior.
5. Update `.env.example` and the requested docs:
   `docs/GETTING_STARTED.md`, `docs/CONCEPTS.md`,
   `docs/TROUBLESHOOTING.md`, `docs/CLI_REFERENCE.md`, and
   `docs/PR_MONITOR_ADOPTION.md`.
6. Write `plans/BASE_URL_UNIFICATION_VALIDATION.md` with requirement status and
   command evidence.

## Assumptions/Changes During Execution

- The requested full `pytest` gate exposed order-sensitive import cleanup
  failures outside the base-URL behavior: metrics, workspace retry, and GitHub
  client import-cycle tests removed modules from `sys.modules` without restoring
  the original objects. To make the requested broad gate meaningful, update
  those import-order tests to use pytest `monkeypatch` cleanup. This is a test
  isolation fix only and does not change AWF runtime behavior.

## Verification Commands And Pass Criteria

Focused TDD checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestBaseUrlResolution -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_001.py -k "api_base_url or base_url" -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/common.py src/awf/cli/__init__.py src/awf/common/config.py src/awf/service/config.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/service/test_config_parts/test_config_part_001.py
uv run --python 3.12 --extra dev mypy src/awf/cli/common.py src/awf/common/config.py src/awf/service/config.py
```

The execution request also explicitly asks for the local broad gate. After the
focused checks pass, run the requested commands if practical in this workspace:

```bash
ruff check .
ruff format --check .
mypy
pytest
```

Pass criteria: focused tests prove the resolver precedence and service
regression behavior, lint/type checks pass for changed Python surfaces, docs no
longer describe `AWF_API_BASE_URL` as the host CLI knob, and any broad-gate
results are recorded in validation.
