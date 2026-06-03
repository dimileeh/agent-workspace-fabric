# T07 — Provider Setup Orchestration With GitHub First-Class (Validation)

Plan reference: `plans/T07_PROVIDER_SETUP_ORCHESTRATION_PLAN.md`
(authoritative contract `docs/awf-plans/ws_23a0b3e2fa15420c826d4b86.md`).

## Files changed

- **New** `src/awf/host_setup/providers.py` — orchestration module: frozen
  `ProviderSpec` registry (GitHub first-class; AWF Cloud stub; the six agent
  providers), `ProviderSetupResult`/`ProviderSetupSummary`
  (`extra="forbid"`, frozen, no raw-secret fields), `orchestrate_provider_setup`,
  `render_provider_summary`, GitHub `gh`/env-ref helper, captured-secret→ref
  conversion, per-provider bounded recheck, non-blocking failure isolation, and
  the `INTERACTIVE_INPUT_REQUIRED` signal.
- **New seam** `src/awf/service/provider_readiness.py` —
  `check_single_provider_readiness` (probes exactly one provider; existing
  callers unchanged) + `__all__` entry.
- **Modified** `src/awf/host_setup/__init__.py` — export new public symbols.
- **Modified** `src/awf/cli/setup_commands.py` — replaced the T04 placeholder:
  non-dry-run + host-ready runs orchestrate, folds the secret-free summary into
  `details["providers"]` (labelled `targeted_recheck`/`all_providers`), persists
  the returned config, and fires the interactive guard only for a *selected*
  provider needing capture under `--non-interactive`.
- **New tests** `tests/unit/service/test_host_setup_providers.py`,
  `tests/unit/service/test_provider_readiness.py`; **extended**
  `tests/unit/cli/test_setup_commands.py`.

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | GitHub ready via `gh`/env ref, no raw token | Complete | `test_github_ready_via_gh_without_token`, `test_github_ready_via_env_ref_when_gh_absent` |
| 2 | One failed provider isolated | Complete | `test_mixed_provider_partial_readiness_is_independent`, `test_provider_invalid_credential_marks_unavailable`, CLI `test_setup_failed_provider_is_non_blocking` |
| 3 | Summary renderable by setup/start | Complete | `to_details()`/`render_provider_summary` (start-compatible shape), CLI `test_setup_provider_github_targeted_recheck_summary` |
| 4 | Bounded probes | Complete | `test_all_probes_are_bounded`, `test_single_provider_seam_http_probe_is_bounded` |
| 5 | Selected leaves others unchanged + `targeted_recheck` label | Complete | `test_selected_provider_configure_does_not_touch_others`, `test_selected_github_recheck_probes_only_github` |
| 6 | Captured/discovered creds → refs | Complete | `test_provider_success_stores_ref_and_rechecks_ready`, `test_captured_secret_degrades_to_env_ref_without_reinjecting` |
| 7 | No raw secrets surfaced | Complete | `test_no_raw_secret_in_summary_or_results`, CLI `test_setup_provider_github_pretty_prints_no_token` |
| 8 | AWF Cloud stub; `docker` excluded | Complete | `test_awf_cloud_is_a_deterministic_stub`, `test_registry_covers_every_known_setup_provider` (registry == `KNOWN_SETUP_PROVIDERS`, no `docker`) |
| 9 | CLI dispatch replaces placeholder, minimal | Complete | `tests/unit/cli/test_setup_commands.py` (50 tests incl. all prior regressions still green) |

Acceptance criteria from the task card all map to Complete rows above; the
expected-tests list (success / missing / invalid / mixed; selected isolate +
recheck; GitHub via `gh` + env ref; no raw secret) is fully covered, plus the
non-interactive capture-needed and bounded-probe cases.

## Commands run (focused — AWF/CI owns the broad gate)

```
ruff check src/awf tests/unit/service/test_host_setup_providers.py \
  tests/unit/service/test_provider_readiness.py tests/unit/cli/test_setup_commands.py
    -> All checks passed!
ruff format --check src/awf/host_setup/providers.py src/awf/cli/setup_commands.py
    -> already formatted
mypy            (pyproject files = ["src/"])
    -> Success: no issues found in 327 source files
pytest tests/unit/service/test_host_setup_providers.py \
       tests/unit/service/test_provider_readiness.py \
       tests/unit/cli/test_setup_commands.py -q
    -> 66 passed
pytest <above> + test_provider_readiness_parts + test_host_setup_credentials_parts
       + tests/unit/cli/test_start_commands.py -q
    -> 321 passed (no regressions in adjacent suites)
coverage (focused) of src/awf/host_setup/providers.py and src/awf/cli/setup_commands.py
    -> providers.py 100.00% (167 stmts / 40 branches), setup_commands.py 100.00%
```

## Notes / boundaries

- **No new reason codes.** `PROVIDER_SETUP_AUTH_INVALID` and
  `INTERACTIVE_INPUT_REQUIRED` (already in the catalog) are consumed; per-provider
  `not_configured` results carry descriptive data-only reason strings (e.g.
  `AWF_CLOUD_STUB`, `<PROVIDER>_NOT_CONFIGURED`) that are summary data, not
  `FirstRunIssue` catalog codes, so no catalog regeneration is required.
- A narrow `provider_readiness` seam (`check_single_provider_readiness`) was added
  because `collect_agent_readiness` probes *all* providers, which would violate
  the "unselected providers are never probed" requirement; the seam is additive
  and independently tested, with no behavior change to existing readiness callers.
- The `awf setup` non-dry-run path resolves real `ServiceSettings` and runs the
  bounded provider probes; CLI tests keep this hermetic by stubbing
  `orchestrate_provider_setup`/`_resolve_provider_settings`, and the orchestration
  itself is exhaustively tested with injected subprocess/HTTP/keyring fakes.
- **Out of scope (untouched):** client config helpers (T08), MCP tools (T09),
  support-bundle/log redaction hardening (T17), AWF Cloud backend/OAuth.
- Full AWF/GitHub validation — the 99% coverage gate, OpenAPI drift check, and
  console build — is owned by AWF + CI after agent completion, per the workspace
  contract.
