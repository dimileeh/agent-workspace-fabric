# T10 — No-Token Local Proof and Mocked Smoke Path

Backlog: `TODO/awf-full-installer-first-run-setup-backlog.md` → **T10** (depends on T04, T05; both merged on `development`).

## Scope

Make the local "smoke proof" a credible, **provider-free** demonstration that local
AWF Core is actually healthy, runnable after `awf start` **without** GitHub write
access or a paid LLM provider token, and point first-run output at it.

The smoke service (`src/awf/service/smoke.py`) and `awf smoke run` CLI already exist
with a `--mocked-local` mode. T10 closes the four specific gaps that block the
acceptance criteria:

1. **False-green in mocked mode (the headline bug).** Today `--mocked-local`
   downgrades an *unreachable/unhealthy local service* from `fail` to `warn`
   (`_phase_service_readiness`), so a no-token run with dead Core still returns
   overall `warn` and exit code 0. A skeptic reads that as "fine." The no-token
   path must keep **local Core health as a hard signal** and only relax the
   *provider/PR (token + GitHub)* requirements.
2. **Liveness ≠ readiness.** The default service probe hits only `/healthz`
   (dependency-free liveness). That proves "the API process answered," not
   "Core is healthy." The proof must consult a **real API + worker-DB-substrate
   signal** (the worker's required DB dependency via `/readyz`) without requiring
   provider tokens, since `/readyz` overall returns 503 when providers are
   unconfigured. This proves the worker's substrate is reachable, **not**
   worker-process liveness — a provider-free HTTP probe cannot observe the worker
   container, and the real worker-container probe (`awf service doctor`'s
   `docker compose ps worker`) is Docker-dependent and outside this proof.
3. **First-run output doesn't point to the proof.** `awf start` success
   `next_steps` currently lead with `awf init` / `awf service status`. After
   `awf start`, first-run output must point at the **provider-free** proof
   (`awf smoke run --mocked-local`).
4. **Output must prove health, not print a URL.** Enrich the smoke service phase
   evidence so the report visibly demonstrates Core health (API up + worker DB
   substrate reachable).

Preserve the existing live (real-provider) smoke path and the existing console
"false-green" coverage unchanged.

## Non-goals (explicit boundaries)

- **No clean-install / source-lane E2E harness** — T14 owns that.
- **No provider setup flows** — T07 owns provider orchestration.
- **No full docs lane rewrite** — T15 owns README/Quickstart/etc. Only the
  narrow next-command / `--help` text needed for the smoke path is in scope here.
- No new credential handling, no MCP smoke tool (not requested by T10).
- Do not run the full AWF/CI validation suite in-agent (AWF/GitHub own that).

## Key facts established during investigation

- `src/awf/service/smoke.py::_phase_service_readiness` downgrades `fail`→`warn`
  when `mocked_local` is true. Regression-locked today by
  `tests/unit/service/test_smoke_parts/test_smoke_part_002.py::
  test_service_readiness_warns_in_mocked_with_unreachable_status` — this test
  encodes the bug and **must be updated** to assert `fail`.
- `_default_service_collector(settings)` does a single `GET /healthz` and returns
  `{"status": "ok"|"unreachable"}`. The phase only reads `result.get("status")`.
- `/readyz` (`src/awf/api/routes/health.py`) is **unauthenticated**, returns its
  JSON body even on 503, and includes a per-dependency `checks.db` result. Its
  *overall* status is `fail` whenever `agent_readiness.status != "ok"` (i.e. no
  provider token), so the collector must read the **`db` sub-check**, not the
  overall status, to get a token-free worker-substrate signal. `db` is the
  worker's poll/claim substrate (`ControlWorker` is a DB poll loop).
- `awf start` success panel is built in
  `src/awf/cli/start_commands.py::_start_success_payload` (`next_steps` tuple).
  `awf setup`'s post-success next step (`Run awf start ...`) lives in
  `src/awf/host_setup/system_checks/__init__.py::_readiness_next_steps` and is
  already provider-free — leave it, but cover it with a chain assertion.
- `SMOKE_*` reason codes are free-form dict literals in `smoke.py`; they are
  **not** scanned by `tests/unit/docs/test_catalog_coverage.py` (that test only
  matches `error_code`, not `reason_code`), so a new `SMOKE_WORKER_*` code needs
  no `docs/REASON_CATALOG.md` entry.
- Console false-green coverage already exists
  (`test_console_unavailable_reports_reason_code`,
  `test_configured_console_url_reports_unavailable_when_probe_fails`) — preserve.

## Intended files to touch

Source:

- `src/awf/service/smoke.py`
  - `_phase_service_readiness`: **remove the `mocked_local` warn-downgrade** — an
    unreachable/unhealthy Core is always `fail`. Interpret a richer collector
    result: treat `status == "ok"` as API-up, and if the result carries a
    `worker` sub-signal that is not healthy, fail with a worker-specific reason.
    Backward compatible: a plain `{"status": "ok"}` (no `worker` key) stays `ok`
    so existing injected collectors keep passing. Enrich `evidence` with `api`
    and `worker_db_substrate` sub-statuses so the report proves health rather than
    a URL. The `worker_db_substrate` signal is the worker's DB *dependency*, not
    worker-process liveness.
  - New reason code `SMOKE_WORKER_UNAVAILABLE` (API reachable, worker DB
    substrate not). Keep `SMOKE_SERVICE_READY` / `SMOKE_SERVICE_UNREACHABLE`.
  - `_default_service_collector`: probe `GET /healthz` (API liveness) **and**
    `GET /readyz` (parse JSON regardless of status code), reading `checks.db.ok`
    as the worker-DB-substrate signal. Return e.g.
    `{"status":"ok","api":"ok","worker_db_substrate":"ok"}` when both succeed;
    `{"status":"degraded","api":"ok","worker_db_substrate":"fail", "reason": ...}`
    when the API is up but the DB sub-check is down/unreachable;
    `{"status":"unreachable", ...}` when `/healthz` is non-200 or raises.
    Provider/`agent_readiness` parts of `/readyz` are intentionally ignored
    (token-gated) so the probe stays provider-free. Keep the bounded httpx
    timeouts; `/readyz` failures degrade gracefully (substrate→`unknown`/`fail`,
    never a crash). Document that docker-dependent `/readyz` checks are *not*
    required for the no-token proof (the substrate proven here is the DB;
    the live path covers docker/provisioning).
- `src/awf/cli/start_commands.py`
  - `_start_success_payload`: lead `next_steps` with the provider-free proof,
    e.g. `"Run awf smoke run --mocked-local to prove local AWF Core health
    without provider tokens or GitHub access."`, keeping `awf init` and the
    console hint afterward. URLs stay, but the first action is the health proof.
- `src/awf/cli/profile_smoke_commands.py`
  - Narrow `smoke run` `--help` / `--mocked-local` help text to describe the
    no-token local proof (in-scope per boundary: "narrow next-command/help text").

Tests (written/updated first — see TDD order below):

- `tests/unit/service/test_smoke_parts/test_smoke_part_002.py`
- `tests/unit/cli/test_start_commands.py`
- `tests/unit/cli/test_smoke.py`
- `tests/unit/cli/test_setup_commands.py` (chain assertion only)

## Tests to write first (strict TDD)

1. **Regression: no false-green (AC#4).** Rewrite
   `test_service_readiness_warns_in_mocked_with_unreachable_status` →
   `test_service_readiness_fails_in_mocked_with_unreachable_status`: in
   `mocked_local=True` with a collector returning `{"status":"down"}`, the
   `service_readiness` phase is `fail` with `SMOKE_SERVICE_UNREACHABLE`, and the
   overall report `status == "fail"`. This is the core behavior change and the
   regression lock that the no-token path cannot claim readiness without a real
   health signal.
2. **Mocked-local success keeps Core health real (AC#1/#2).** With
   `mocked_local=True`, a collector returning `{"status":"ok","api":"ok",
   "worker_db_substrate":"ok"}` → `service_readiness` `ok` / `SMOKE_SERVICE_READY`,
   evidence exposes `api` and `worker_db_substrate` sub-statuses; PR phase still
   `SMOKE_PR_MOCKED_LOCAL`; provider phase may warn — overall not `fail`.
3. **Worker substrate down fails even in mocked mode (AC#4).** Collector returns
   `{"status":"degraded","api":"ok","worker_db_substrate":"fail"}` →
   `service_readiness` `fail` / `SMOKE_WORKER_UNAVAILABLE`; overall `fail`.
4. **Default collector unit tests** (patch `httpx.AsyncClient`, mirroring the
   existing `_default_service_collector` tests):
   - `/healthz` 200 + `/readyz` body `{"checks":{"db":{"ok":true}}}` → `ok`,
     `worker_db_substrate == "ok"`.
   - `/healthz` 200 + `/readyz` body `{"checks":{"db":{"ok":false}}}` (overall
     503, providers missing) → `degraded`, `worker_db_substrate == "fail"`,
     `api == "ok"` (proves the provider-free substrate signal works while overall
     /readyz is 503).
   - `/healthz` non-200 → `unreachable` (no readyz dependence).
   - `/readyz` raises/unreachable but `/healthz` ok → substrate `fail`/`unknown`,
     still a real (non-green) signal.
5. **Backward compatibility.** Existing tests passing `{"status":"ok"}` plain
   collectors must stay green (assert in-place; no `worker` key ⇒ `ok`).
6. **First-run points at provider-free proof (AC#1).** In
   `test_start_commands.py`, update `test_start_success_json_payload` /
   `test_start_success_pretty_panel` and add a focused test asserting the
   **leading** `awf start` next step contains `awf smoke run --mocked-local`,
   contains no `token`/secret wording, and that `_start_success_payload` puts it
   first.
7. **CLI help is provider-free.** In `test_smoke.py`, assert `awf smoke run
   --help` mentions the no-token / `--mocked-local` local proof.
8. **Setup→start→smoke chain is provider-free.** In `test_setup_commands.py`,
   assert the setup success next step (`Run awf start ...`) is provider-free and
   leads into the provider-free proof (guards the documented first-run chain
   without duplicating T07 provider logic).

Each new behavior gets focused positive + negative coverage so changed lines and
branches in `smoke.py` / `start_commands.py` stay covered (hard 99% gate is owned
by AWF post-agent; reason about coverage as I implement).

## Validation commands (focused; AWF/GitHub own the broad gate)

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev ruff format --check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_smoke_parts \
  tests/unit/cli/test_smoke.py \
  tests/unit/cli/test_start_commands.py \
  tests/unit/cli/test_setup_commands.py -q
```

(Plus `tests/unit/docs/test_catalog_coverage.py` as a sanity check that the new
`SMOKE_WORKER_UNAVAILABLE` code needs no catalog entry.) Note: the suggested
`tests/unit/service/test_smoke.py` path in the task maps to the split
`tests/unit/service/test_smoke_parts/` directory in this repo.

## Risks and assumptions

- **Risk: changing the mocked warn→fail behavior is a contract change.** It is
  the intended T10 fix (AC#4). Only one existing test encodes the old behavior;
  it is updated, not silently broken. No other caller depends on the warn
  downgrade (verified: `_phase_service_readiness` warn path has no other test).
- **Assumption: DB readiness is the right token-free worker *substrate* signal.**
  The worker is a DB poll/claim loop; `/readyz.checks.db` is reachable without
  provider tokens and degrades gracefully. It proves the worker's DB dependency,
  not worker-process liveness (the real worker-container probe lives in
  `awf service doctor` and is Docker-dependent, outside this provider-free proof).
  Docker/provisioning health remains the live path's job (documented in code), so
  the no-token proof is not coupled to docker availability (the workspace notes
  docker may be absent).
- **Assumption: `/readyz` JSON is readable on 503.** Confirmed in
  `routes/health.py` — the body is returned with the 503 status, so the collector
  reads sub-checks regardless of the status line.
- **Risk: scope creep into docs/E2E/providers.** Mitigated by the non-goals;
  only narrow help + next-command text changes are made.
- **Assumption: new `SMOKE_WORKER_UNAVAILABLE` needs no REASON_CATALOG entry.**
  Verified the catalog coverage test only scans `error_code`, not `reason_code`.

## After implementation

Write `plans/T10_NO_TOKEN_SMOKE_VALIDATION.md` recording the focused commands run
and their results, and note that broad coverage/CI validation is owned by AWF
after agent completion.
