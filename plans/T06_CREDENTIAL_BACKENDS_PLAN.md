# T06 — Credential Ref Backends Plan

PLAN_EXECUTION_PROTOCOL artifact for T06 (keychain / env_ref / plain_file
credential ref backends). The AWF-tracked copy is
`docs/awf-plans/ws_4e7c7fdbaa324a019cca1f1c.md`; this file is the in-repo plan.

## 1. Problem statement and scope

Add a credential **backend abstraction** for first-run host setup that turns a
provided provider secret into a stored **reference**
(`keyring://` / `env://` / `plain-file://`) recorded on
`ProviderConfig.credential_ref`, **never the raw value**. Three backends:

- `keyring` — OS keychain, default when available.
- `env_ref` — records only the environment variable **name**.
- `plain_file` — explicit, gated opt-in; Linux + headless only (H02).

Build on the ground truth already shipped by T02/T03 (see the AWF-tracked plan):
existing reason codes, `ProviderConfig.credential_ref` + validator,
`ConsentConfig.plain_file_secrets`, the secret-payload guard, and the ref-scheme
redaction patterns. Reuse them verbatim — no new reason codes, config models, or
redaction regexes.

Out of scope: provider orchestration (T07), support-bundle hardening (T17), and
broad setup-CLI surface (T04). Expose only minimal service-layer store/resolve
seams.

## 2. Requirements checklist (test oracles)

1. Keyring backend is the default when available.
2. Env ref stores only variable names (`OPENAI_API_KEY` / `GH_TOKEN`) → `env://NAME`.
3. Plain-file storage requires **both** `--allow-plain-secrets` (a flag passed
   into the policy) **and** approved consent (`ConsentConfig.plain_file_secrets`).
4. Plain-file storage is **rejected on non-Linux or non-headless** even with
   allow + consent.
5. Headless Linux without a keychain offers env ref **or** explicit plain-file opt-in.
6. Raw tokens never appear in stdout, stderr, config, logs, or test snapshots.
7. Non-interactive: missing required secret input raises `INTERACTIVE_INPUT_REQUIRED`
   (never prompt).

## 3. Implementation steps

1. Write failing tests in `tests/unit/service/test_host_setup_credentials.py`
   (fakes for keyring; injectable host facts; token-shaped fixtures).
2. New module `src/awf/host_setup/credentials.py`:
   - `SecretValue` — wrapper whose `repr`/`str` are redacted; `.reveal()` only raw accessor.
   - `CredentialRef` — frozen dataclass (`backend`, `key`, `metadata`) with
     `to_ref_string()` / `from_ref_string()`. Validate refs through
     `ProviderConfig(credential_ref=...)` so output/inputs satisfy the existing
     `_validate_credential_ref` + secret guards; reject raw-secret/unknown scheme
     with `CREDENTIAL_REF_INVALID`. Construction rejects secret-shaped keys.
   - `HostFacts` + `detect_host_facts()` (platform.system + DISPLAY/WAYLAND env),
     injectable.
   - `CredentialBackend` Protocol (`name`, `requires_secret_value`,
     `is_available`, `store`, `resolve`).
   - `KeyringBackend` (lazy `import keyring` via injectable module loader; fail
     backend ⇒ unavailable; default when available).
   - `EnvRefBackend` (always available; var-name regex; never reads/persists value).
   - `PlainFileBackend` (available only Linux+headless; writes `0600` file; ref =
     `plain-file://<abs-path>`).
   - `offered_credential_backends(...)` — fallbacks `{env_ref, plain_file?}`.
   - `select_credential_backend(...)` — default keyring; fallback gating;
     plain-file allow+consent+host gate; non-interactive secret gate.
   - `store_provider_credential(...)` / `resolve_credential_ref(...)` — minimal
     store/resolve seam for T07.
   - `HostSetupCredentialError(RuntimeError)` mirroring `HostSetupConfigError`
     shape; reason-coded constructors reusing the four T03 codes.
   - Log credential-ref rejections through `redact_secrets` (AC#6 evidence).
3. Config helpers in `credentials.py`: `set_provider_credential_ref` /
   `get_provider_credential_ref` — rebuild the providers mapping (no in-place
   mutation), read `ConsentConfig.plain_file_secrets` for the gate.
4. Export new public symbols from `host_setup/__init__.py` `__all__`.
5. Add `keyring` to `pyproject.toml` dependencies (lazy import) and refresh `uv.lock`.

## 4. Verification commands and pass criteria

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_credentials.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
```

Pass criteria: all four requirements-mapped test groups green; ruff + mypy clean;
focused config tests still pass; the new module is branch-complete for the 99%
gate (each backend `is_available`, every rejection path, the non-interactive path,
ref round-trip + reject, redaction, config round-trip). AWF + GitHub CI own the
full suite, the 99% coverage gate, and merge gating.

## 5. Assumptions / Changes

- The non-interactive secret gate lives inside `select_credential_backend`
  (via a `secret_available` flag) and is exercised through
  `store_provider_credential`, keeping a single source for the gate while
  honoring the AWF-tracked plan intent.
- `store_provider_credential` / `resolve_credential_ref` are the minimal seams
  T07 will consume; they contain no provider-specific logic.
