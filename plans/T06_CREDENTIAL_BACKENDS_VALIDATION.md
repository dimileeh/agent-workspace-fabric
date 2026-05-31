# T06 — Validation: Keychain / Env / Plain-File Credential Ref Backends

Plan reference: `plans/T06_CREDENTIAL_BACKENDS_PLAN.md`
AWF planning artifact: `docs/awf-plans/ws_0ea630b2f5144402adecf84e.md`
Backlog task: T06 in `TODO/awf-full-installer-first-run-setup-backlog.md`

This document validates the **actual code diff** against the plan,
requirement-by-requirement. Status legend: `Complete`, `Partial`, `Missing`.

## 1. Files changed (the diff under validation)

| File | Change | Lines |
| --- | --- | --- |
| `src/awf/host_setup/credentials.py` | **New** module: `CredentialError`, `CredentialRef`, `HostCredentialCapabilities` + `detect_host_credential_capabilities`, `CredentialRequest`, `CredentialBackend`/`KeyringModule` protocols, `KeyringCredentialBackend`, `EnvRefCredentialBackend`, `PlainFileCredentialBackend`, `select_credential_backend`, `store_provider_credential`, plus private helpers. | +219 stmts |
| `src/awf/host_setup/config.py` | Add optional `ProviderConfig.backend: str \| None` (default `None`) + `_validate_backend` validator; `_CREDENTIAL_BACKEND_KINDS` constant. | +15 |
| `src/awf/host_setup/__init__.py` | Additive re-export of the new public credential symbols (owned-path overlap R3: additive only). | +30 |
| `pyproject.toml` | Add `keyring>=24` to `[project] dependencies`. | +1 |
| `uv.lock` | Regenerated: additive only (+92 lines, 0 deletions) — adds `keyring`, `jaraco-*`, `jeepney`, `secretstorage` (Linux-gated), `more-itertools`, `pywin32-ctypes`; no unrelated pins bumped. | +92 |
| `tests/unit/service/test_host_setup_credentials.py` | **New** suite — 44 test functions / 61 parametrized cases. | new |
| `plans/T06_CREDENTIAL_BACKENDS_PLAN.md`, `plans/T06_CREDENTIAL_BACKENDS_VALIDATION.md` | AGENTS.md plan + this validation. | new |

No files owned by other tasks were touched (no `cli/*`, no `providers.py`, no
`support_bundle.py`). `__init__.py` edits are purely additive (R3).

## 2. Validation commands run (all green)

```text
ruff check src/awf tests                                         → All checks passed!
ruff format --check src/awf/host_setup <new test>               → 6 files already formatted
mypy (pyproject files=["src/"], 289 files)                       → Success: no issues found
pytest tests/unit/service/test_host_setup_credentials.py -q      → 61 passed
pytest .../test_host_setup_config.py .../test_host_setup_rendering.py → 125 passed
uv lock --check                                                  → Resolved 104 packages (consistent)
pytest tests/unit/cli/test_packaging.py -q                       → 4 passed (dependency-sensitive)
```

Focused coverage of the changed modules (the repo enforces 99%):

```text
src/awf/host_setup/config.py        100.00%  (0 miss / 62 branch / 0 partial)
src/awf/host_setup/credentials.py   100.00%  (0 miss / 46 branch / 0 partial)
TOTAL                               100.00%
```

Broad coverage / full-suite / OpenAPI gates are left to AWF + GitHub CI per the
workspace contract.

## 3. Acceptance-criteria traceability (task card / plan §2)

| # | Requirement | Implemented evidence (file · symbol) | Tests | Status | Gaps |
| --- | --- | --- | --- | --- | --- |
| R1 | Keyring backend is default when available | `credentials.select_credential_backend` (preferred `None`/`"keyring"` → keyring when `is_available()`); `KeyringCredentialBackend.is_available` rejects fail/null backends | `test_keyring_is_default_backend_when_available`, `test_keyring_ref_uses_explicit_account` | Complete | — |
| R2 | Env ref stores only a variable name (`OPENAI_API_KEY`, `GH_TOKEN`); no value | `EnvRefCredentialBackend.create_ref` returns `env://NAME`, stores nothing; `_ENV_VAR_NAME_RE` | `test_env_ref_stores_only_variable_name[OPENAI_API_KEY/GH_TOKEN]`, `test_env_ref_select_with_explicit_preference` | Complete | — |
| R3 | Plain-file requires `--allow-plain-secrets` **and** consent | `PlainFileCredentialBackend.create_ref` → `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED` when flag/consent missing | `test_plain_file_requires_flag_and_consent[3 combos]`, `test_plain_file_consent_gating_uses_default_backend` | Complete | — |
| R4 | Plain-file rejected on non-Linux / non-headless even with flag+consent; no file | `create_ref` platform gate after consent → `CREDENTIAL_BACKEND_UNAVAILABLE`; `HostCredentialCapabilities.supports_plain_file` | `test_plain_file_rejected_on_unsupported_hosts[Darwin/Windows/desktop-Linux]` (asserts `not secrets_dir.exists()`) | Complete | — |
| R5 | Headless Linux without keychain offers env ref or explicit plain-file opt-in | auto selection falls back to `env_ref` when keyring unavailable; plain-file remains a gated explicit choice | `test_unavailable_keyring_falls_back_to_env_ref[fail/raise]`, `test_explicit_keyring_preference_falls_back_to_env_ref_when_unavailable`, `test_select_plain_file_returns_gated_backend` | Complete | — |
| R6 | Raw tokens never appear in stdout/stderr/config/logs/refs/snapshots | module never prints/logs; errors built from non-secret fields; `CredentialRef` rejects secret-like refs; `_looks_like_secret` | `test_token_inputs_never_appear_in_outputs` (capsys + `redact_first_run_value`), `test_credential_ref_rejects_secret_like_value`, `*_is_reason_coded` token-absence asserts | Complete | — |
| R7 | Non-interactive enforcement for missing input (`INTERACTIVE_INPUT_REQUIRED`); never prompts | `_pull_secret` / `_interactive_input_required`; env_ref missing `env_var` | `test_keyring_missing_secret_*`, `test_keyring_empty_secret_*`, `test_plain_file_missing_secret_*`, `test_env_ref_missing_variable_*` | Complete | — |
| R8 | Refs + non-secret backend metadata persist in `ProviderConfig`; raw-secret guard holds | `CredentialRef.to_provider_config_fields`; `ProviderConfig.backend` + `_validate_backend`; existing `_validate_credential_ref` unchanged | `test_provider_config_round_trips_backend_metadata`, `test_provider_config_backend_metadata_is_optional`, `test_provider_config_rejects_unknown_backend`, `test_provider_config_still_rejects_raw_secret_credential_ref`, `test_credential_backends_match_provider_config_vocabulary` | Complete | — |

## 4. Plan §6 test-group traceability

| Plan test group | Test(s) | Status |
| --- | --- | --- |
| 1. Keyring default when available | `test_keyring_is_default_backend_when_available` | Complete |
| 2. Unavailable backend → env_ref fallback | `test_unavailable_keyring_falls_back_to_env_ref`, `test_explicit_keyring_preference_falls_back_to_env_ref_when_unavailable` | Complete |
| 3. Env ref name-only + rejections | `test_env_ref_stores_only_variable_name`, `test_env_ref_rejects_invalid_or_token_shaped_names[6 cases]` | Complete |
| 4. Plain-file consent/flag gating | `test_plain_file_requires_flag_and_consent`, `test_plain_file_consent_gating_uses_default_backend` | Complete |
| 5. Plain-file permissions | `test_plain_file_writes_secret_with_conservative_permissions` (0600 file / 0700 dir / path-only ref) | Complete |
| 6. Non-Linux rejection | `test_plain_file_rejected_on_unsupported_hosts[Darwin/Windows]` | Complete |
| 7. Non-headless rejection | `test_plain_file_rejected_on_unsupported_hosts[desktop-Linux]` | Complete |
| 8. Non-interactive enforcement | keyring/plain-file/env_ref missing-input tests | Complete |
| 9. Redaction | `test_token_inputs_never_appear_in_outputs`, `test_credential_error_to_dict_round_trips_without_details` | Complete |
| 10. Capabilities detection | `test_detect_host_credential_capabilities[5 hosts]`, `test_detect_host_credential_capabilities_uses_live_defaults` | Complete |
| 11. Config integration | provider-config round-trip / optional / unknown-backend / raw-secret-guard / vocabulary tests | Complete |

Additional hardening tests (edge/error coverage to 100%): keyring set_password
failure, missing keyring module, module without `get_keyring`, backend resolving
to `None`, `_keyring_runtime_errors` without an errors namespace, plain-file
dir-creation failure, non-POSIX chmod no-op, unsafe provider identifiers,
mismatched ref prefix, unknown backend preference, optional keyring import
present/absent, and additive re-export presence.

## 5. Decisions / assumptions (per PLAN_EXECUTION_PROTOCOL §2)

- **Keyring dependency (plan R1): RESOLVED by adding it.** `keyring>=24` was
  added to `[project] dependencies` and `uv lock` regenerated. The lock change is
  minimal and additive (+92 lines, 0 deletions; only the keyring subtree —
  `cryptography` was already present, `secretstorage` is `sys_platform == 'linux'`
  gated), and `uv lock --check` confirms consistency. The R1 fallback (optional
  import only) was therefore **not** needed. The code still uses lazy optional
  import + dependency injection, so behavior and tests do not depend on the real
  library being installed.
- **Test hermeticity with keyring installed.** Because `keyring` is now installed
  in CI, every test exercises keyring through injected fakes or by forcing
  `_import_keyring_module` to `None`/a fake via `monkeypatch`; no test ever calls
  the real `keyring.set_password`, so the real OS keychain is never touched. This
  was verified by running the suite with `keyring` present (180 passing across the
  credentials/config/rendering suites).
- **Non-interactive semantics.** The module never prompts, so missing required
  input always raises `INTERACTIVE_INPUT_REQUIRED`; `CredentialRequest.non_interactive`
  is recorded as advisory, secret-free detail (defaults to `True`).
- **No new reason codes.** All four codes
  (`CREDENTIAL_BACKEND_UNAVAILABLE`, `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`,
  `CREDENTIAL_REF_INVALID`, `INTERACTIVE_INPUT_REQUIRED`) already existed in
  `rendering.py` and `service/doctor/reasons.py`; the reason-catalog coverage gate
  needs no change.

## 6. Scope boundaries honored (no leakage into other tasks)

- T04 (setup CLI `--allow-plain-secrets`/`--provider`/`--non-interactive`): consumed
  only as function arguments; no CLI surface changed.
- T07 (provider orchestration / `providers.py`): not touched.
- T17 (support-bundle / broader redaction): only the credential-local redaction
  hooks here; `support_bundle.py` not touched.
- No encrypted plain-file backend; no MCP credential entry (both out of scope per
  backlog "NOT In Scope").

## 7. Outstanding gaps

None. All planned requirements (R1–R8) and all plan §6 test groups are
`Complete`, the changed modules are at 100% line/branch coverage, and ruff,
ruff-format, and mypy are clean. Broad AWF/GitHub CI validation (full suite, full
coverage gate, OpenAPI drift, console) runs after the agent phase per the
workspace contract.
