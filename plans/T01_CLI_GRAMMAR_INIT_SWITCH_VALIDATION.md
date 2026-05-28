# T01 CLI Grammar Init Switch Validation

Plan reference: `plans/T01_CLI_GRAMMAR_INIT_SWITCH_PLAN.md`

Source contract:

- `docs/awf-plans/ws_a829bac6193d48be8b2a4f14.md`
- `plans/AWF_FULL_INSTALLER_FIRST_RUN_SETUP_PLAN.md`
- `TODO/awf-full-installer-first-run-setup-backlog.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Register `awf setup` and `awf start` as real Typer surfaces | Complete | `src/awf/cli/setup_commands.py`, `src/awf/cli/start_commands.py`, registration in `src/awf/cli/main.py` |
| Add setup help and placeholder tests | Complete | `tests/unit/cli/test_setup_commands.py` |
| Add start help and placeholder tests | Complete | `tests/unit/cli/test_start_commands.py` |
| Preserve `awf init <repo>` project onboarding | Complete | Existing path-mode init tests remain green in `tests/unit/cli/test_init_parts/test_init_part_001.py` and `test_init_part_004.py` |
| Change no-path `awf init` to non-zero migration error | Complete | New pretty and JSON migration tests in `test_init_part_001.py`; implementation emits `AWF_INIT_REQUIRES_PROJECT_PATH` |
| Remove or reject bootstrap-only flags from no-path init | Complete | Legacy bootstrap flags are hidden from `awf init --help`, parsed only for compatibility, and rejected with migration guidance |
| Preserve bootstrap helper coverage without public no-path init | Complete | Test-only wrapper in `tests/unit/cli/test_init_parts/_bootstrap_helper.py` keeps private helper regression coverage out of the public CLI contract |
| Add docs tests preventing no-path init-as-bootstrap guidance | Complete | `tests/unit/docs/test_public_docs_status.py` checks public docs for forbidden no-path init bootstrap wording |
| Update public docs and shared CLI help to new grammar | Complete | `docs/QUICKSTART.md`, `docs/GETTING_STARTED.md`, `docs/PROJECT_ONBOARDING.md`, `src/awf/cli/service_commands.py`, `src/awf/cli/workspace_commands.py` |
| Preserve H01-H04 locked decisions and keep scope to T01 | Complete | No installer, host config, credential, MCP, real setup, or real start behavior implemented |

## Verification Evidence

Focused pytest:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_setup_commands.py \
  tests/unit/cli/test_start_commands.py \
  tests/unit/cli/test_init_parts/test_init_part_001.py \
  tests/unit/cli/test_init_parts/test_init_part_002.py \
  tests/unit/cli/test_init_parts/test_init_part_003.py \
  tests/unit/cli/test_init_parts/test_init_part_004.py \
  tests/unit/docs/test_public_docs_status.py \
  -q
```

Result: `173 passed in 3.62s`.

Focused lint:

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/cli/main.py \
  src/awf/cli/setup_commands.py \
  src/awf/cli/start_commands.py \
  src/awf/cli/service_commands.py \
  src/awf/cli/workspace_commands.py \
  tests/unit/cli/test_setup_commands.py \
  tests/unit/cli/test_start_commands.py \
  tests/unit/cli/test_init_parts/test_init_part_001.py \
  tests/unit/cli/test_init_parts/test_init_part_002.py \
  tests/unit/cli/test_init_parts/test_init_part_003.py \
  tests/unit/cli/test_init_parts/test_init_part_004.py \
  tests/unit/cli/test_init_parts/_bootstrap_helper.py \
  tests/unit/docs/test_public_docs_status.py
```

Result: `All checks passed!`.

Focused type check:

```bash
uv run --python 3.12 --extra dev mypy \
  src/awf/cli/main.py \
  src/awf/cli/setup_commands.py \
  src/awf/cli/start_commands.py
```

Result: `Success: no issues found in 3 source files`.

## Gaps

No T01 requirement gaps remain.

Full AWF/GitHub validation, full coverage, whole-repository pytest, frontend
builds, PR monitoring, and merge gating are intentionally left to AWF after
agent completion per the workspace contract.
