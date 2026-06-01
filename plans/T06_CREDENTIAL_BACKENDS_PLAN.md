# T06 — Keychain / Env / Plain-File Credential Ref Backends

Source AWF planning artifact: `docs/awf-plans/ws_0ea630b2f5144402adecf84e.md`
(this file mirrors it as the AGENTS.md-required task plan).

Backlog task: T06 in `TODO/awf-full-installer-first-run-setup-backlog.md`.
Target branch: `development` (auto-merge enabled). Depends on T02, T03, H02 (all
satisfied/merged).

## 1. Problem statement and scope

Add a credential **backend abstraction** for AWF first-run setup that produces
safe credential *references* (never raw secret values) for provider
configuration. Three backends:

- `keyring` — store the secret in the OS keychain; ref is `keyring://…`.
- `env_ref` — store only an environment-variable *name*; ref is `env://NAME`.
- `plain_file` — explicit opt-in fallback that writes the secret to a
  `0600` file on **headless Linux only**, after `--allow-plain-secrets` and
  recorded consent; ref is `plain-file://<abs-path>`.

Refs + non-secret backend metadata are persisted in host setup config
(`ProviderConfig`), never raw provider values. The module never prompts, never
prints, and never logs or returns secret material. Missing required input in a
non-interactive run fails with `INTERACTIVE_INPUT_REQUIRED`.

In scope: backend abstraction, capabilities detection, selection/orchestration,
the optional `ProviderConfig.backend` metadata field, redaction hooks needed for
credential data, and tests. **Out of scope:** provider setup orchestration (T07),
setup CLI flag plumbing (T04), support-bundle / broader redaction hardening
(T17), encrypted plain-file backend, MCP credential entry.

## 2. Requirements checklist (acceptance criteria)

- [ ] R1 Keyring backend is the default when available (auto selection).
- [ ] R2 Env ref stores only a variable name (e.g. `OPENAI_API_KEY`, `GH_TOKEN`);
  no value stored.
- [ ] R3 Plain-file storage requires `--allow-plain-secrets` **and** approved
  consent, else `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`.
- [ ] R4 Plain-file storage is rejected on non-Linux or non-headless hosts even
  with flag + consent (`CREDENTIAL_BACKEND_UNAVAILABLE`); no file written.
- [ ] R5 Headless Linux without a keychain offers env ref or explicit plain-file
  opt-in (auto selection falls back to env_ref; plain_file remains gated).
- [ ] R6 Raw tokens never appear in stdout, stderr, config, logs, refs, error
  details, `to_dict()`, or test snapshots.
- [ ] R7 Non-interactive behavior is enforced for missing secret input
  (`INTERACTIVE_INPUT_REQUIRED`); the module never prompts.
- [ ] R8 Refs + non-secret backend metadata persist in `ProviderConfig`; the
  existing raw-secret guard still holds.

## 3. Design

New self-contained module `src/awf/host_setup/credentials.py`. Reuses existing
seams from T02/T03: `ProviderConfig` (rejects raw refs), the four credential
reason codes already defined in `rendering.py`
(`CREDENTIAL_BACKEND_UNAVAILABLE`, `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`,
`CREDENTIAL_REF_INVALID`, `INTERACTIVE_INPUT_REQUIRED`) and full catalog entries
in `service/doctor/reasons.py` (**no new reason codes**), and the shared
redaction primitives in `common/redaction.py` / `common/token_patterns.py` /
`rendering.redact_first_run_value`.

### Types
- `CredentialError(RuntimeError)` — mirrors `HostSetupConfigError` /
  `SourceCheckoutError`: `reason_code`, `message`, secret-free `details`,
  `to_dict()`. Details built from non-secret fields only.
- `CredentialRef(frozen pydantic model, extra="forbid")` — `backend` (Literal of
  the three kinds) + `ref` (validated to match the backend's safe prefix and not
  resemble a secret). `to_provider_config_fields(*, status="ready")` returns the
  kwargs to build/update a `ProviderConfig` (config stays the single writer).
- `HostCredentialCapabilities(frozen model)` — `os_name`, `is_headless`, property
  `supports_plain_file` (== Linux **and** headless).
  `detect_host_credential_capabilities(*, system=None, environ=None)` derives it
  from `platform.system()` and absence of `DISPLAY`/`WAYLAND_DISPLAY` on Linux;
  both inputs injectable for deterministic tests. Non-Linux ⇒ `is_headless=False`
  and `supports_plain_file=False`.
- `CredentialRequest(frozen dataclass)` — `provider`, `account="default"`,
  `env_var: str | None`, `secret_source: Callable[[], str | None] | None`,
  `non_interactive: bool = True`. `secret_source` is lazy: only pulled when a
  backend needs it (keyring/plain_file); env_ref never pulls a value.

### Backends (`CredentialBackend` Protocol: `kind`, `is_available()`, `create_ref()`)
- `KeyringCredentialBackend` — lazy optional `import keyring` via
  `importlib.import_module` (injectable `keyring_module`; tests inject a fake and
  never touch the real keychain). `is_available()`: module importable **and** the
  resolved keyring is not a `fail`/`null` no-op backend. `create_ref()`: pulls the
  secret (missing ⇒ `INTERACTIVE_INPUT_REQUIRED`), `set_password(service,
  account, secret)` (service/account carry only AWF/provider identifiers), returns
  `keyring://<service>/<account>`; keyring runtime errors ⇒
  `CREDENTIAL_BACKEND_UNAVAILABLE`.
- `EnvRefCredentialBackend` — `is_available()` always `True`. `create_ref()`:
  requires `env_var` (missing ⇒ `INTERACTIVE_INPUT_REQUIRED`); rejects
  token-shaped values and names not matching `^[A-Z][A-Z0-9_]*$` ⇒
  `CREDENTIAL_REF_INVALID`. Returns `env://<NAME>`; stores no value.
- `PlainFileCredentialBackend` — built with `capabilities`,
  `allow_plain_secrets`, `consent`, injectable `secrets_dir` (default
  `~/.awf/secrets`). `is_available()` == flag and consent and
  `capabilities.supports_plain_file`. `create_ref()` enforces, in order:
  (a) consent+flag else `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`; (b) Linux+headless
  else `CREDENTIAL_BACKEND_UNAVAILABLE` (secret-free detail); (c) pull secret
  (missing ⇒ `INTERACTIVE_INPUT_REQUIRED`); then atomic `0600` write into a `0700`
  dir (mirrors `config.write_host_setup_config`). Returns `plain-file://<abs-path>`.

### Selection / orchestration
- `select_credential_backend(*, preferred, capabilities, allow_plain_secrets,
  plain_file_consent, keyring_backend=None, env_backend=None,
  plain_file_backend=None)`:
  - `preferred` `None`/`"keyring"` ⇒ keyring if available else env_ref (keyring
    default when available; headless-Linux-no-keychain gets the env-ref offer).
  - `"env_ref"` ⇒ env_ref.
  - `"plain_file"` ⇒ the gated plain-file backend (consent/flag/platform enforced
    in `create_ref`).
  - unknown ⇒ `CREDENTIAL_REF_INVALID` with a secret-free `valid_backends` detail.
- `store_provider_credential(request, *, …)` = select + `create_ref`. Backends
  injectable so tests run end-to-end with fakes.

### Config metadata
- Add optional `backend: str | None = None` to `ProviderConfig`, validated against
  the three kinds when present (default `None` preserves existing construction and
  back-compat). No new top-level fields; consent already lives in
  `ConsentConfig.plain_file_secrets`. Re-export new public symbols additively from
  `host_setup/__init__.py` (keep the `__init__.py` edit purely additive per the
  owned-path overlap risk R3 below).

### Non-interactive semantics
The module never prompts. Missing required input (`secret_source` returns
empty/`None`, or `env_var` absent) always raises `INTERACTIVE_INPUT_REQUIRED`;
`non_interactive` is recorded as advisory, secret-free detail.

## 4. Implementation steps

1. Create this plan file (done).
2. Write failing `tests/unit/service/test_host_setup_credentials.py` (§6).
3. Add `credentials.py` to make tests pass with the smallest change; add the
   optional `ProviderConfig.backend` field; re-export from `__init__.py`.
4. Decide the `keyring` dependency: attempt `keyring>=24` in `pyproject.toml` +
   `uv lock`; if the lockfile churn is broad/infeasible, apply R1 fallback (rely on
   the optional import only) and record the decision in the validation doc. Either
   way no behavior/test depends on the real library.
5. Run the §5 focused validation; iterate to green.
6. Confirm `tests/unit/service/test_host_setup_credentials.py` exists and passes;
   write `plans/T06_CREDENTIAL_BACKENDS_VALIDATION.md` with a requirement-by-
   requirement traceability table.

## 5. Verification commands and pass criteria

Focused (AWF/CI own broad validation per the workspace contract):

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup tests/unit/service/test_host_setup_credentials.py
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_credentials.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
```

Pass criteria: all commands green; new credential suite covers every R1–R8
acceptance criterion; no token-shaped string appears in any ref, error detail,
`to_dict()`, or captured output.

## 6. Tests to write first (strict TDD)

`tests/unit/service/test_host_setup_credentials.py`, `@pytest.mark.unit`, fakes
only (`FakeKeyringModule`, injected capabilities, `secrets_dir=tmp_path`). All
assertions check for the **absence** of a fixed fake token
(`_FAKE_TOKEN = "sk-proj-" + "a"*48`, `_FAKE_GH_TOKEN = "ghp_" + "b"*36`).

1. Keyring default when available (auto ⇒ keyring; ref `keyring://…`).
2. Backend unavailable ⇒ env_ref fallback (`env://NAME`).
3. Env ref stores only a name; token-shaped / lowercase / invalid names ⇒
   `CREDENTIAL_REF_INVALID`.
4. Plain-file consent/flag gating ⇒ `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`.
5. Plain-file permissions (`0600` file in `0700` dir; ref is a path; ref/details
   carry no secret; file holds the secret).
6. Non-Linux rejection ⇒ `CREDENTIAL_BACKEND_UNAVAILABLE` (no file).
7. Non-headless rejection ⇒ `CREDENTIAL_BACKEND_UNAVAILABLE` (no file).
8. Non-interactive enforcement ⇒ `INTERACTIVE_INPUT_REQUIRED` (keyring/plain_file
   missing secret; env_ref missing env_var); module never prompts.
9. Redaction: token-shaped inputs never appear in ref, `details`, `to_dict()`,
   `capsys`, or a `redact_first_run_value`-rendered payload.
10. Capabilities detection classifies headless Linux, desktop Linux, macOS,
    Windows correctly.
11. Config integration: `to_provider_config_fields()` builds a `ProviderConfig`;
    write→read round-trips `backend` metadata; raw-secret guard still holds.

## 7. Risks and mitigations

- R1 keyring dependency & lockfile regen → optional import + DI means the code and
  tests work with or without keyring; fall back to not declaring it if `uv lock`
  is broad/infeasible, recording the decision in the validation doc.
- R2 `ProviderConfig.backend` compatibility → optional, default `None`; round-trip
  + back-compat tests.
- R3 owned-path overlap (`ws_4c144afc35444a9bbf88e5c6` also owns
  `host_setup/__init__.py`) → keep `__init__.py` edits purely additive; keep host
  capability detection inside `credentials.py`; tests import from
  `awf.host_setup.credentials` directly.
- R4 secret leakage → module never prints/logs secrets; errors from non-secret
  fields only; explicit `capsys` + redaction regression tests.
- R5 plain-file atomic write / perms portability → reuse the config write pattern
  (`os.open(..., 0o600)` + temp + `replace`, best-effort chmod); gate to Linux.
