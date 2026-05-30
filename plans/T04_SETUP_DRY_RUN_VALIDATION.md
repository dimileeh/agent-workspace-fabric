# T04 — `awf setup --dry-run` system checks and readiness payload (VALIDATION)

Plan reference: `plans/T04_SETUP_DRY_RUN_PLAN.md` (and the AWF planning artifact
`docs/awf-plans/ws_4c144afc35444a9bbf88e5c6.md`).

This document validates the **actual code diff** against the plan,
requirement-by-requirement. Status values: `Complete`, `Partial`, `Missing`.

## Diff under validation

Created:

- `src/awf/host_setup/system_checks.py` — stdlib-only host system-check engine,
  provider normalization, interactive guard, readiness payload builder.
- `tests/unit/service/test_host_setup_system_checks.py` — fixture-driven check,
  provider, guard, payload, and default-probe tests (**owned path; verified to exist**).
- `plans/T04_SETUP_DRY_RUN_PLAN.md`, `plans/T04_SETUP_DRY_RUN_VALIDATION.md`.

Modified:

- `src/awf/cli/setup_commands.py` — placeholder replaced with the real option
  surface and dispatch flow.
- `src/awf/cli/main.py` — `awf setup` help text describes the readiness pass.
- `src/awf/host_setup/rendering.py` — added `first_run_report_payload` (+ status
  derivation helper); added to `__all__`.
- `src/awf/host_setup/__init__.py` — re-exports `first_run_report_payload`.
- `tests/unit/cli/test_setup_commands.py` — placeholder tests replaced with T04
  behavior/edge/error coverage.
- `tests/unit/service/test_host_setup_rendering.py` — coverage for
  `first_run_report_payload` status/reason derivation.

No edits outside setup CLI / host setup system checks / rendering integration /
tests (matches the task boundary and avoids the prior attempt's offending paths:
no `doctor/reasons.py`, `REASON_CATALOG.md`, or generator changes).

## Acceptance-criteria traceability

| # | Acceptance criterion | Implemented evidence | Tests | Status |
|---|----------------------|----------------------|-------|--------|
| A1 | `--dry-run` never writes secrets, never starts Core | `setup_commands._run_setup` guards `_persist_safe_config` behind `if not dry_run`; no Compose/bootstrap entrypoint is ever called; checks are read-only and never handle secret material | `test_setup_dry_run_never_writes_config`, `test_setup_allow_plain_secrets_forwarded_in_dry_run`, `test_setup_dry_run_json_success_shape` | Complete |
| A2 | `--provider github --dry-run` accepts and forwards the selector | `normalize_providers` validates+orders; recorded in `details["selected_providers"]` | `test_setup_provider_github_forwarded`, `test_setup_repeated_providers_deduped_and_ordered`, `test_normalize_providers_dedupes_and_orders` | Complete |
| A3 | `--allow-plain-secrets` forwards consent without defaulting plain-file | forwarded as non-secret `details["plain_file_consent"]`; non-dry-run sets `consent.plain_file_secrets` only when flagged; default leaves it `False` | `test_setup_allow_plain_secrets_forwarded_in_dry_run`, `test_setup_non_dry_run_persists_plain_secret_consent` | Complete |
| A4 | Unknown provider → reason-coded diagnostic, no all-provider fallback | `normalize_provider` raises `SetupCheckError(SETUP_PROVIDER_UNKNOWN)` before any check runs; exit 2 | `test_setup_unknown_provider_rejected_without_fallback` (asserts checks not run), `test_normalize_provider_unknown_raises_reason_coded` | Complete |
| A5 | Missing Docker / stopped daemon → readiness failure with next actions | `check_docker` returns BLOCKED (binary-missing vs daemon-unreachable sub-cases, each with a fix); payload `SETUP_READINESS_FAILED`, `status="blocked"`, `next_steps`, exit 1 | `test_setup_docker_missing_returns_readiness_failure`, `test_check_docker_*` (4 cases) | Complete |
| A6 | Source-checkout dry-run works from a clone and `--source-checkout .` | `validate_source_checkout` (T02) invoked; verified root recorded in details; invalid → `SOURCE_CHECKOUT_INVALID` blocker with `missing_markers` | `test_setup_source_checkout_valid_resolves`, `test_setup_source_checkout_invalid_blocks_with_missing_markers`; **manual smoke** `awf setup --dry-run --source-checkout .` → `source_checkout.root=/workspace`, no invalid issue | Complete |
| A7 | Pretty output includes status, blockers, warnings, docs links, next command | `build_setup_readiness_payload` → `first_run_report_payload` → `render_first_run_pretty` emits Status / Command / Reason / per-issue Problem·Cause·Fix·Docs / Next | `test_setup_dry_run_pretty_includes_status_blockers_docs_next` | Complete |
| A8 | Non-interactive secret-needed path → `INTERACTIVE_INPUT_REQUIRED` | `require_interactive` raises under `--non-interactive`; wired so a non-dry-run `--provider` selection (which would need interactive credential entry, deferred to T07) returns the signal; exit 2; dry-run skips the step | `test_setup_non_interactive_provider_requires_input`, `test_require_interactive_raises_only_when_non_interactive` | Complete |

## Plan §4 "files to touch" traceability

| Planned change | Implemented? | Evidence |
|---|---|---|
| Create `system_checks.py` (stdlib only) | Yes | imports limited to `os/shutil/subprocess/sys/socket/pathlib/dataclasses/enum/typing` + sibling host_setup modules; no FastAPI/SQLAlchemy/control-plane imports |
| Create owned `test_host_setup_system_checks.py` | Yes | file exists; 41 tests |
| Modify `setup_commands.py` (real surface + dispatch) | Yes | option surface + `_run_setup` flow |
| Modify `main.py` help | Yes | readiness-pass help text; keeps "Prepare this machine for AWF" |
| Add `first_run_report_payload` to `rendering.py` | Yes | derives status/reason from issue severities |
| Export from `host_setup/__init__.py` | Yes | minimal append (shrinks the `OWNED_PATH_OVERLAP_RISK` surface vs `ws_aa7727aa41e545b6906f548b`) |
| Rewrite `test_setup_commands.py` | Yes | placeholder tests replaced; stable `--help` test retained |
| Extend `test_host_setup_rendering.py` | Yes | 3 status-derivation tests |

## Reason codes

All reused, catalog untouched: `SETUP_READINESS_FAILED`,
`SETUP_PROVIDER_UNKNOWN`, `INTERACTIVE_INPUT_REQUIRED` (T03),
`SOURCE_CHECKOUT_INVALID` (T02). Per-check problem/cause/fix/docs supplied via
`first_run_issue_from_reason_code` overrides. `HOST_SETUP_CONFIG_CORRUPT` (T02)
is surfaced defensively by the CLI when the host config cannot be read.

## Engineering-rule compliance

- **TDD**: tests written/adjusted first for each slice; behavior + edge + error
  coverage present.
- **No bare excepts / no hidden retries**: every subprocess/socket/disk/sysconf
  probe catches specific exceptions (`FileNotFoundError`,
  `subprocess.TimeoutExpired`, `OSError`, `ValueError`) and bounds subprocess
  calls with `timeout=`.
- **No secrets written/logged**: dry-run writes nothing; non-dry-run persists
  only consent flags + source metadata via the secret-rejecting
  `write_host_setup_config`; the forwarded consent detail is named
  `plain_file_consent` (no "secret" substring) so the redaction layer renders it
  truthfully; token-shaped check data is redacted on render
  (`test_build_payload_redacts_token_shaped_check_data`).
- **Core stays generic**: no provider/runtime/service specifics hard-coded;
  `host_setup` stays dependency-light (stdlib probes).

## Commands run (evidence)

```text
uv run --python 3.12 --extra dev ruff check src/awf tests              # All checks passed
uv run --python 3.12 --extra dev ruff format --check <touched files>   # already formatted
uv run --python 3.12 --extra dev mypy src/awf                          # Success: no issues (289 files)
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_setup_commands.py \
  tests/unit/service/test_host_setup_system_checks.py \
  tests/unit/service/test_host_setup_rendering.py \
  tests/unit/service/test_host_setup_config.py -q                      # passed
# Focused coverage of new code:
#   src/awf/cli/setup_commands.py        100.00%
#   src/awf/host_setup/system_checks.py  100.00%
# Manual read-only smoke (no Core start, no writes):
uv run --python 3.12 --extra dev awf setup --dry-run --source-checkout . --format json
uv run --python 3.12 --extra dev awf setup --provider github --dry-run --format json
```

Broad/CI coverage gates (whole-repo 99% coverage, OpenAPI drift, console) are
owned by AWF/GitHub after the agent phase and are not run locally per the
workspace contract.

## Gaps / deferrals

- No requirement is `Partial` or `Missing`.
- Docs still describe `awf setup` as a placeholder (`AWF_SETUP_PLACEHOLDER` in
  `docs/...`); updating first-run docs is **T15**'s scope and drift tests are
  **T18**'s — intentionally out of scope here, so `tests/unit/docs/
  test_public_docs_status.py` remains green against unchanged docs.
- Provider **orchestration** (T07), credential **storage** (T06), and client
  config writers (T08) are forwarded-only here, by design.
