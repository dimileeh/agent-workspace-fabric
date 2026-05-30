# T06 — Credential ref backends (keyring / env_ref / plain_file)

Workspace: `ws_4e7c7fdbaa324a019cca1f1c`
Task source: `TODO/awf-full-installer-first-run-setup-backlog.md` → **T06: Add keychain/env/plain-file credential ref backends**
Target branch: `development` (auto-merge enabled for this feature PR)
Launch override: Claude Code / `claude-opus-4-8` / xhigh.
Dependencies: **T02** (host-setup config schema) and **T03** (first-run error/reason
contract) are verified merged on `development`; **H02** is locked.

> **Ground truth (read at planning time — build on it, do not re-create it):**
> - `src/awf/host_setup/` is a package: `__init__.py`, `config.py`, `rendering.py`,
>   `source_assets.py`. Host-setup tests live under `tests/unit/service/`
>   (`test_host_setup_config.py`, `test_host_setup_rendering.py`).
> - **Reason codes already exist (added by T03)** in `host_setup/rendering.py` and
>   `service/doctor/reasons.py`, grouped as `FIRST_RUN_CREDENTIAL_REASON_CODES`:
>   `INTERACTIVE_INPUT_REQUIRED`, `CREDENTIAL_BACKEND_UNAVAILABLE`,
>   `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`, `CREDENTIAL_REF_INVALID`. **Reuse these
>   verbatim — do not mint new ones.**
> - **`ProviderConfig.credential_ref` already exists** in `config.py`:
>   `str | None`, `min_length=1, max_length=512`, with a `_validate_credential_ref`
>   field validator that rejects secret-looking values and **requires the ref to use
>   `keyring://`, `env://`, or `plain-file://`**. Credential refs are persisted as
>   these **scheme-prefixed strings**, not as a nested model.
> - **`ConsentConfig.plain_file_secrets: bool = False` already exists** — the
>   plain-file consent flag is already part of the config.
> - Config already enforces "no raw secrets" via `_ensure_no_secret_payload` /
>   `_SecretPayloadError` / `_looks_like_secret_value`, and `HostSetupConfigError`
>   carries a `reason_code` + secret-free `.details()`.
> - Redaction already exists and already covers these ref schemes: `redact_secrets`
>   (`common/redaction.py`), `redact_audit_value`/`redact_audit_text`
>   (`common/audit.py`), the `keyring://`/`env://`/`plain-file://` `PROVIDER_REF_PATTERN`
>   + helpers in `common/token_patterns.py`, and `redact_first_run_value` /
>   `_redact_provider_refs` in `host_setup/rendering.py`. **Reuse; add no new regexes.**
> - `keyring` is **not** yet a dependency in `pyproject.toml` — T06 adds it.

---

## 1. Goal & acceptance criteria

Add a credential **backend abstraction** for first-run host setup that turns a provided
provider secret into a stored **reference** (`keyring://` / `env://` / `plain-file://`)
recorded on `ProviderConfig.credential_ref`, **never the raw value**. Three backends:

- `keyring` — OS keychain (default when available).
- `env_ref` — records only the environment variable **name** (e.g. `OPENAI_API_KEY`, `GH_TOKEN`).
- `plain_file` — explicit, gated opt-in; Linux + headless only (H02).

Acceptance criteria (the test oracles):

1. Keyring backend is the default when available.
2. Env ref stores only variable names (e.g. `OPENAI_API_KEY` / `GH_TOKEN`) → `env://NAME`, never a value.
3. Plain file storage requires **both** `--allow-plain-secrets` **and** approved consent
   (`ConsentConfig.plain_file_secrets`).
4. Plain file storage is **rejected on non-Linux or non-headless hosts** even when
   `--allow-plain-secrets` and consent are present.
5. Headless Linux without a keychain offers env ref **or** explicit plain-file opt-in.
6. Raw tokens never appear in stdout, stderr, config, logs, or test snapshots.
7. Non-interactive behavior is enforced for missing secret input (no prompts — raise the
   reason-coded `INTERACTIVE_INPUT_REQUIRED`).

H02 (locked): plain-file provider secrets are allowed **only** for Linux/headless
setups, **after explicit warning and consent**; keyring and env refs remain preferred.

---

## 2. Scope & boundaries (hard limits)

In scope:
- Credential backend abstraction (`keyring`, `env_ref`, `plain_file`).
- Backend selection/policy (default keyring; fallback offers; plain-file gating).
- Host-fact detection (OS + headless), injectable for tests.
- Producing/parsing the `keyring://` / `env://` / `plain-file://` ref strings that the
  **existing** `ProviderConfig.credential_ref` validator accepts (refs + metadata only).
- Reusing existing redaction for credential/token-shaped data.
- Tests with **fake** backends (no real OS keychain; no real provider secret persistence).

Out of scope (do **not** touch):
- **T07** owns provider setup orchestration / provider flows — expose only the minimal
  store/resolve seam T07 will consume; no provider-specific logic here.
- **T17** owns support-bundle hardening and broader log/doctor redaction — only the
  redaction reuse credential data itself needs.
- **T04** owns the setup CLI surface — avoid broad CLI changes. Keep this PR to pure
  service-layer functions callable without the CLI; add a narrow integration point only
  if a test demands it.

---

## 3. Intended files / modules to touch

### New: `src/awf/host_setup/credentials.py`
Mirrors the expected test path `tests/unit/service/test_host_setup_credentials.py`.

- `BackendName` — `Literal["keyring", "env_ref", "plain_file"]` (dependency-free literal).
- Scheme mapping (the contract `ProviderConfig._validate_credential_ref` enforces):
  `keyring` → `keyring://<service>/<account>`, `env_ref` → `env://<VAR_NAME>`,
  `plain_file` → `plain-file://<absolute-path>`.
- `SecretValue` — thin wrapper around `str` whose `__repr__`/`__str__` return a redacted
  placeholder; `.reveal()` is the only raw accessor. Prevents accidental
  logging/formatting of a secret.
- `CredentialRef` — in-memory value object (frozen dataclass or Pydantic model) with
  `backend: BackendName`, `key: str`, `metadata: dict[str, str]`, plus:
  - `to_ref_string() -> str` → emits the scheme-prefixed string for storage; the output
    **must** pass `ProviderConfig._validate_credential_ref` and `_ensure_no_secret_payload`.
  - `from_ref_string(value) -> CredentialRef` → parses a stored ref; raises a
    `CREDENTIAL_REF_INVALID`-coded error on unknown scheme / raw-secret-shaped input.
  - It never carries the value; add a test that `to_ref_string()`/repr cannot contain
    value-shaped data.
- `HostFacts` (frozen dataclass) — `os_name`, `is_linux`, `is_headless`; plus
  `detect_host_facts()` reading `platform.system()` + display env
  (`DISPLAY`/`WAYLAND_DISPLAY` absence ⇒ headless). Injectable so platform branches are
  pure functions of injected facts (deterministic tests). No centralized platform helper
  exists today (config/source_assets use `os.name == "posix"`); keep these local.
- `CredentialBackend` (`typing.Protocol`): `name`, `is_available(facts) -> bool`,
  `store(logical_name, secret) -> CredentialRef`, `resolve(ref) -> SecretValue | None`.
- `KeyringBackend` — **lazy** `import keyring` inside methods; `ImportError` or a
  `keyring.backends.fail`/unusable backend ⇒ `is_available() == False`. `store` →
  `keyring.set_password(service, account, secret.reveal())`; ref = `keyring://service/account`.
  Default when available.
- `EnvRefBackend` — `is_available()` always `True`; `store` validates the var name
  (`^[A-Z][A-Z0-9_]*$`) and records only the name as `env://NAME`; never reads/persists
  the value; `resolve` reads `os.environ`.
- `PlainFileBackend` — `is_available(facts)` is `True` only when
  `facts.is_linux and facts.is_headless`; `store` writes the secret to a `0600` file
  under the setup dir (create+`os.chmod` to `0600` before write, or open with mode) and
  returns `plain-file://<abs-path>`. Gated by allow + consent (enforced in the policy
  function, re-asserted here).
- `select_credential_backend(...)` — single policy entry point:
  - default keyring when available;
  - keyring unavailable ⇒ return the offered set `{env_ref, plain_file?}` (plain_file
    present only when Linux+headless+allow+consent);
  - reject plain_file on non-Linux / non-headless even with allow+consent;
  - non-interactive: if a chosen backend needs the value and none was supplied, raise
    `INTERACTIVE_INPUT_REQUIRED` (never prompt).
- Reason-coded errors — **reuse the existing T03 constants** imported from
  `awf.host_setup.rendering` (or re-exported from `awf.host_setup`):
  | Condition | Reason code |
  |---|---|
  | non-interactive, secret required but absent | `INTERACTIVE_INPUT_REQUIRED` |
  | keyring/backend unavailable with no usable fallback | `CREDENTIAL_BACKEND_UNAVAILABLE` |
  | plain_file selected without `--allow-plain-secrets` or consent | `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED` |
  | plain_file rejected on non-Linux / non-headless (allow+consent present) | `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED` (its catalog text already says "only on an approved headless Linux path") |
  | malformed ref / raw-secret-shaped ref | `CREDENTIAL_REF_INVALID` |
  - Raise a reason-coded exception in the shape of `HostSetupConfigError`
    (carries `.reason_code` + secret-free `.details()`): add a sibling
    `HostSetupCredentialError(RuntimeError)` in `credentials.py` (or reuse
    `HostSetupConfigError` if it fits) so T04/T07 can map it uniformly.

### Existing: `src/awf/host_setup/config.py`
- Persist credential refs via the **existing** `ProviderConfig.credential_ref` string
  field — do **not** add a new credentials model. The backend's `to_ref_string()`
  produces a value that already satisfies `_validate_credential_ref`.
- Add small helpers (here or in `credentials.py`) to set/get a provider's credential ref
  on a `HostSetupConfig` (mind the frozen/immutable providers mapping —
  `_freeze_providers`/`_serialize_providers`; rebuild rather than mutate in place).
- Read the existing `ConsentConfig.plain_file_secrets` flag for the plain-file gate; do
  not add a new consent field.
- Confirm refs round-trip cleanly through `read_host_setup_config` /
  `write_host_setup_config` and the `_ensure_no_secret_payload` guard.

### Existing: `src/awf/host_setup/__init__.py`
- Export the new public symbols (`CredentialRef`, `SecretValue`, backend classes,
  `select_credential_backend`, `HostFacts`, `detect_host_facts`, the credential error
  type) and add them to `__all__`, matching the existing export style.

### Redaction (reuse only — add no new regexes)
- Live logs: `awf.common.redaction.redact_secrets()`.
- Persisted/audit payloads: `awf.common.audit.redact_audit_value()` / `redact_audit_text()`.
- First-run output: `awf.host_setup.rendering.redact_first_run_value` /
  `_redact_provider_refs` (already understands the three ref schemes).
- Only touch `common/token_patterns.py` if a genuinely missing provider token shape is
  required — broader redaction hardening is **T17**.

### Reason catalog
- No new reason codes are expected (all four already exist with catalog text in
  `service/doctor/reasons.py`). Only if a genuinely new code is added, regenerate
  `docs/REASON_CATALOG.md` via `scripts/generate_reason_catalog.py`.

### Dependencies
- `pyproject.toml` — add `keyring` to `[project] dependencies` (review notes call for
  "Python `keyring` as the boring credential abstraction"). Keep the **lazy import** so
  its absence degrades to "keyring unavailable" (testable) rather than an import-time
  crash. Refresh `uv.lock` (`uv sync --extra dev`). Tests simulate availability/absence
  via a fake, so the real lib is not required to exercise selection logic.

---

## 4. Tests to write first (strict TDD)

Primary file: `tests/unit/service/test_host_setup_credentials.py` — match existing
host-setup test style: `@pytest.mark.unit`, `monkeypatch`/`tmp_path` fixtures, posix perm
assertions guarded by `os.name == "posix"` using `stat.S_IMODE(...)`, fixed datetime
constant for any metadata timestamps (mirror `_FIXED_NOW` in `test_host_setup_config.py`).

Fakes/fixtures (no real keychain, no real provider secrets):
- `FakeKeyring` — in-memory `dict` + `available: bool`, injected where the lazy
  `import keyring` resolves (prefer a backend-factory seam over patching internals).
- `make_host_facts(os_name=..., headless=...)` — drives platform branches deterministically.
- Token-shaped fixtures: `sk-...`, `ghp_...`, `github_pat_...`, a long base64 blob.

Test cases (each ↔ an acceptance criterion):

1. `test_keyring_is_default_when_available`.
2. `test_keyring_unavailable_offers_env_or_plain_on_headless_linux` — offered set has
   `env_ref`, and `plain_file` only with allow+consent.
3. `test_env_ref_stores_only_variable_name` — store `OPENAI_API_KEY`/`GH_TOKEN` ⇒ ref is
   `env://OPENAI_API_KEY`; no value in `to_ref_string()`/repr; backend never read the value.
4. `test_plain_file_requires_allow_flag` — allow `False` ⇒ `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`.
5. `test_plain_file_requires_consent` — `ConsentConfig.plain_file_secrets` False ⇒ same code.
6. `test_plain_file_rejected_on_non_linux` — Darwin/Windows + headless + allow + consent ⇒
   `CREDENTIAL_PLAIN_FILE_CONSENT_REQUIRED`.
7. `test_plain_file_rejected_on_non_headless_linux` — Linux + not headless + allow + consent ⇒ same.
8. `test_plain_file_allowed_on_headless_linux_with_allow_and_consent` — writes a `0600`
   file (`stat().st_mode & 0o777 == 0o600`), ref = `plain-file://<path>`, secret absent
   from captured logs.
9. `test_missing_secret_input_is_non_interactive` — value-requiring backend, no secret ⇒
   `INTERACTIVE_INPUT_REQUIRED`; monkeypatch `builtins.input` to raise and assert never called.
10. `test_keyring_unavailable_no_fallback_selected` — keyring unavailable and no fallback
    chosen ⇒ `CREDENTIAL_BACKEND_UNAVAILABLE`.
11. Ref/redaction:
    - `test_credential_ref_never_serializes_raw_value` — token-shaped strings absent from
      `to_ref_string()`, repr, and any dump.
    - `test_secret_value_repr_is_redacted` — `repr(SecretValue("sk-..."))` shows a placeholder.
    - `test_ref_string_round_trips_through_provider_config` — `to_ref_string()` is accepted
      by `ProviderConfig(credential_ref=...)` and `from_ref_string()` recovers it.
    - `test_raw_token_ref_is_rejected` — a raw token passed as a ref raises
      `CREDENTIAL_REF_INVALID`, and storing it in config trips
      `_validate_credential_ref` / `_ensure_no_secret_payload`.
    - `test_logs_redact_token_shaped_inputs` — captured structlog output shows
      `<redacted>`, not the token (use the repo's standard log-capture fixture).
12. Config persistence (focused; may extend `test_host_setup_config.py`):
    - `test_credential_ref_persisted_in_host_setup_config` — store → `write/read` round-trip
      with conservative perms and **no** raw value; providers mapping rebuilt, not mutated.

Write each test first and confirm it fails, then implement the smallest green change
(AGENTS.md strict TDD; every fix carries a regression test).

---

## 5. Validation commands (focused; AWF/CI owns broad gates)

Per the workspace contract, run only focused checks; AWF + GitHub CI own the full suite,
the 99% coverage gate, and merge gating.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_credentials.py -q
# plus the config test if extended:
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
```

If `pyproject.toml`/`uv.lock` changed (adding `keyring`), confirm `uv sync --extra dev`
resolves cleanly. Do **not** run the full suite or `--cov` here.

Coverage: the new module must satisfy the repo's 99% gate; the matrix above covers every
branch (each backend's `is_available`, every rejection path, the non-interactive path,
ref round-trip + reject, redaction, config round-trip). Use `# pragma: no cover` only for
genuinely unreachable defensive branches, sparingly.

---

## 6. Design decisions & rationale

- **Refs as scheme strings, reusing the existing field.** Backends emit
  `keyring://`/`env://`/`plain-file://` strings that the already-shipped
  `ProviderConfig.credential_ref` validator accepts; only `SecretValue` ever holds a raw
  value, redacted in `repr`/`str` and never persisted. With the existing
  `_ensure_no_secret_payload` guard, this satisfies AC#6.
- **Lazy keyring import.** Keeps AWF core import-safe without `keyring` and makes
  "keyring unavailable" a first-class testable state (AC#1/#5).
- **Injectable `HostFacts`.** Platform/headless branches become pure functions of injected
  facts ⇒ deterministic non-Linux/non-headless rejection tests (AC#4).
- **One policy function.** `select_credential_backend(...)` centralizes default + fallback
  + gating so T04 (CLI) and T07 (providers) consume a single decision.
- **Reuse T03 contract end-to-end.** Existing reason codes, existing consent flag,
  existing redaction — no parallel hierarchy, no new regexes, no catalog churn.

---

## 7. Risks & assumptions

Assumptions (all verified at planning time):
- The four credential reason codes, `ProviderConfig.credential_ref` + its validator,
  `ConsentConfig.plain_file_secrets`, the secret-payload guard, and the ref-scheme
  redaction patterns already exist — confirmed by reading the source.

Risks & mitigations:
- *Real keyring/OS coupling in tests* → `FakeKeyring` + injection; never touch the real keychain.
- *Secret leakage via ref string / exception text* → `SecretValue` wrapper +
  `to_ref_string()` assertion + `_validate_credential_ref`/`_ensure_no_secret_payload`
  guards + explicit "no raw value in ref/repr/logs" tests.
- *Frozen providers mapping* → rebuild the providers mapping when adding a ref; don't
  mutate the immutable mapping (`_freeze_providers`).
- *plain_file perms* → create `0600` from the start; assert mode; never log file content.
- *Scope creep into T04/T07/T17* → service-layer abstraction + existing-field ref storage
  + redaction reuse + tests only.
- *Coverage 99% gate* → branch-complete matrix; sparing `# pragma: no cover`.
- *Dependency/lockfile churn* → lazy-import `keyring`; refresh `uv.lock` and note it.

---

## 8. Non-goals (explicit)

- No provider setup orchestration or provider-specific credential flows (**T07**).
- No support-bundle hardening / broad log/doctor redaction beyond credential reuse (**T17**).
- No broad setup-CLI surface changes (**T04**) — narrow, test-only integration at most.
- No new reason codes, no new config models, no new redaction regexes (all already exist).
- No runtime consumption/wiring of stored credentials into providers here.
- No migration of existing secrets; no changes to `WorkspaceSecretLease`. No encrypted
  plain-file backend (explicitly out of scope per the backlog).

---

## 9. Execution order

1. Author `plans/T06_CREDENTIAL_BACKENDS_PLAN.md` (PLAN_EXECUTION_PROTOCOL artifact; this
   `docs/awf-plans/` file is the AWF-tracked copy).
2. Write failing tests (§4) first.
3. Implement `host_setup/credentials.py` (`SecretValue`, `CredentialRef` + scheme
   to/from, `HostFacts`/`detect_host_facts`, three backends, `select_credential_backend`,
   reason-coded error reusing the T03 codes) — smallest green change.
4. Add config helpers to set/get `ProviderConfig.credential_ref` on `HostSetupConfig`
   (rebuild providers mapping); read `ConsentConfig.plain_file_secrets` for the gate.
5. Export new symbols from `host_setup/__init__.py`.
6. Add `keyring` to `pyproject.toml` (lazy import) and refresh `uv.lock`.
7. Run focused validation (§5); iterate to green.
8. Write `plans/T06_CREDENTIAL_BACKENDS_VALIDATION.md` recording the focused checks and
   stating that full AWF/GitHub validation runs after agent completion.
