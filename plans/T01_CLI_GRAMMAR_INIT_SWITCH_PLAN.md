# T01 CLI Grammar Init Switch Plan

## Problem Statement And Scope

Task T01 locks the public first-run CLI grammar around:

```text
awf setup -> awf start -> awf init <repo>
```

The current CLI still documents and executes no-path `awf init` as local
service bootstrap. This task must make that path a non-zero migration error,
reserve `awf setup` and `awf start` as real top-level Typer command surfaces,
and preserve existing `awf init <repo>` project onboarding behavior.

Scope is limited to T01. `awf setup` and `awf start` get stable placeholder
behavior only; real setup checks, host config, credential capture, and service
bootstrap wrapping belong to later backlog tasks.

## Requirements Checklist

- Register `awf setup` and `awf start` as real Typer commands or command groups.
- Add `tests/unit/cli/test_setup_commands.py` covering setup help and
  placeholder behavior.
- Add `tests/unit/cli/test_start_commands.py` covering start help and
  placeholder behavior.
- Preserve `awf init <repo>` project onboarding behavior and project-mode
  options.
- Change no-path `awf init` to a clear non-zero migration error pointing users
  to `awf setup`, `awf start`, and `awf init <path>`.
- Remove or reject bootstrap-only flags on the public no-path init path.
- Update existing init tests for no-path migration and project-path
  compatibility.
- Add/update docs tests so public docs fail if no-path `awf init` is described
  as service bootstrap.
- Update user-facing CLI and public docs copy to the new grammar.
- Preserve locked human decisions H01-H04; H04 preflight remains complete and
  is not repeated here.
- Do not switch branches, push, rebase, run full coverage, whole-repo tests,
  full frontend builds, or CI-equivalent broad validation.

## Implementation Steps

1. Add failing TDD tests for:
   - `awf setup --help` and `awf setup` placeholder output.
   - `awf start --help` and `awf start` placeholder output.
   - no-path `awf init` migration output in pretty and JSON formats.
   - no-path `awf init` not invoking service bootstrap internals.
   - bootstrap-only public init flags producing migration/rejection behavior.
   - project-path `awf init <path>` compatibility and bootstrap-flag rejection.
   - public docs banning no-path init-as-bootstrap guidance.
2. Implement the smallest CLI changes:
   - Create `src/awf/cli/setup_commands.py`.
   - Create `src/awf/cli/start_commands.py`.
   - Register both modules in `src/awf/cli/main.py`.
   - Replace no-path init bootstrap dispatch with a migration renderer.
   - Hide or reject old bootstrap-only init flags while keeping project-mode
     compatibility.
   - Update first-run guidance in shared CLI help text.
3. Update public docs so the canonical flow is `awf setup`, `awf start`, then
   `awf init <path>`, while keeping lower-level `awf service bootstrap`
   references clearly framed as service operations.
4. Run focused validation only for touched CLI/docs tests and narrow lint/type
   checks.
5. Create `plans/T01_CLI_GRAMMAR_INIT_SWITCH_VALIDATION.md` with
   requirement-by-requirement status and evidence.
6. Commit the scoped local changes on the current AWF-managed branch.

## Verification Commands And Pass Criteria

Focused pytest command:

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

Pass criteria:

- Setup/start help exits 0 and exposes stable first-run placeholder surfaces.
- Setup/start direct invocation exits non-zero with stable reason-code output
  and no traceback.
- No-path `awf init` exits non-zero with migration guidance and no bootstrap
  side effects.
- `awf init <path>` path-mode tests still pass.
- Docs tests fail on no-path `awf init` service-bootstrap guidance and pass with
  the updated grammar.

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
  tests/unit/docs/test_public_docs_status.py
```

Focused type check:

```bash
uv run --python 3.12 --extra dev mypy \
  src/awf/cli/main.py \
  src/awf/cli/setup_commands.py \
  src/awf/cli/start_commands.py
```

Full AWF/GitHub validation, full coverage, full repository pytest, and merge
gating are intentionally left to AWF after agent completion.

## Assumptions And Non-Goals

- Target branch is `development`; this workspace must keep all work on the
  current AWF-managed branch.
- Existing private init bootstrap helpers can remain for reuse by lower-level
  service paths or future T05 work, but public no-path `awf init` must not call
  them.
- Do not implement T02+ host setup config, T03 rendering framework, T04 setup
  checks, T05 real start bootstrap, credentials, MCP tools, installer work, or
  release packaging changes.
