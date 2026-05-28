# T02 Host Setup Config Source Assets Plan

## Problem Statement And Scope

Implement the T02 foundation slice from
`plans/AWF_FULL_INSTALLER_FIRST_RUN_SETUP_PLAN.md` and
`TODO/awf-full-installer-first-run-setup-backlog.md`.

This slice adds the host setup config schema, safe `~/.awf/config.yml` IO
helpers, and an explicit verified AWF source-checkout asset model. Later
setup/start/provider/client tasks must be able to consume the stored config and
verified source asset handoff without rediscovering paths or guessing whether a
source checkout is valid.

In scope:

- New `src/awf/host_setup/` package.
- Non-secret host setup config models and YAML read/write helpers.
- Conservative config directory/file permissions.
- Secret-value rejection at config construction/load/write boundaries.
- Source-checkout marker validation and reason-coded diagnostics.
- Tests in `tests/unit/service/test_host_setup_config.py`.

Out of scope:

- `awf setup`, `awf start`, or `awf init` CLI orchestration.
- Credential backend implementation.
- Installer manifest, shell installer, packaging asset changes, or docs tasks.
- Changes to AWF/GitHub PR monitoring or broad validation gates.

Locked backlog decisions H01-H04 remain preserved. H04 preflight is complete
per the workspace prompt.

## Requirements Checklist

- [ ] Config read/write round-trips through an injected config path.
- [ ] Config parent directory is created with owner-only permissions where the
      platform supports POSIX modes.
- [ ] Config file is written with owner-only permissions where the platform
      supports POSIX modes.
- [ ] Config stores only non-secret settings: install channel, API host port,
      work dir, provider credential refs/status, client integration status,
      consent flags, and optional source checkout metadata.
- [ ] Raw secret-looking values and secret-bearing keys are rejected before a
      config object is returned or written.
- [ ] Corrupt or schema-invalid YAML fails with
      `HOST_SETUP_CONFIG_CORRUPT` and structured path/details.
- [ ] Valid AWF source checkout validation returns an immutable handoff with
      resolved asset paths.
- [ ] Invalid source checkout fails with `SOURCE_CHECKOUT_INVALID` and exact
      missing marker details.
- [ ] Unreadable source paths fail with `SOURCE_CHECKOUT_INVALID` and
      structured diagnostics.
- [ ] Stored source asset metadata is revalidated before use, and stale metadata
      fails with `SOURCE_CHECKOUT_ASSETS_STALE`.
- [ ] No package-asset fallback is used after an explicit source checkout is
      selected or stored.

## Implementation Steps

1. Add failing unit tests first in
   `tests/unit/service/test_host_setup_config.py` for config round-trip,
   secret rejection, corrupt config diagnostics, valid source checkout,
   missing marker details, unreadable paths, and stale metadata.
2. Run the new focused test file to confirm the expected import/test failure.
3. Add `src/awf/host_setup/__init__.py`,
   `src/awf/host_setup/config.py`, and
   `src/awf/host_setup/source_assets.py`.
4. Implement minimal Pydantic config models and reason-coded exceptions.
5. Implement YAML read/write helpers with atomic replace and `0700`/`0600`
   best-effort POSIX permissions.
6. Implement centralized AWF source marker definitions, verified asset handoff,
   persisted metadata conversion, and stale metadata revalidation.
7. Re-run the focused test file until green.
8. Run focused lint/type checks only for touched modules and tests.
9. Create `plans/T02_HOST_SETUP_CONFIG_SOURCE_ASSETS_VALIDATION.md` with
   requirement-by-requirement evidence and note that AWF/GitHub own broad
   validation after agent completion.

## Verification Commands And Pass Criteria

Focused commands for this agent phase:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
uv run --python 3.12 --extra dev ruff check src/awf/host_setup tests/unit/service/test_host_setup_config.py
uv run --python 3.12 --extra dev mypy src/awf/host_setup
```

Pass criteria:

- The new targeted test file passes.
- Focused lint for touched code passes.
- Focused mypy for `src/awf/host_setup` passes.

Full repository validation, coverage gates, frontend builds, OpenAPI drift
checks, PR creation, push, and merge monitoring are owned by AWF/GitHub after
agent completion and are intentionally not run in this workspace phase.
