# T10 — No-Token Local Proof and Mocked Smoke Path (PLAN)

Full implementation contract: `docs/awf-plans/ws_bcb857fd33ea4c1dbd4d3962.md`.
Backlog: `TODO/awf-full-installer-first-run-setup-backlog.md` → T10 (depends on T04, T05).

## Problem statement and scope

Make the local smoke proof a credible, **provider-free** demonstration that local
AWF Core is actually healthy after `awf start`, without GitHub write access or a
paid LLM provider token. Point first-run output at it. Close four gaps:

1. False-green in mocked mode: `--mocked-local` downgrades an unreachable/unhealthy
   local Core from `fail`→`warn`. Remove that downgrade; local Core health stays a
   hard signal. Only provider/PR (token + GitHub) requirements are relaxed.
2. Liveness ≠ readiness: default probe hits only `/healthz`. Add a token-free
   worker **DB-substrate** signal via `/readyz` `checks.db.ok` (readable even on
   503). This proves the worker's required DB dependency is reachable — it is
   *not* worker-process liveness (a provider-free HTTP probe cannot observe the
   worker container; the real worker-container probe lives in `awf service doctor`
   and is Docker-dependent, outside this proof).
3. First-run output doesn't point at the proof: `awf start` `next_steps` must lead
   with `awf smoke run --mocked-local`.
4. Output must prove health, not print a URL: enrich service phase evidence with
   `api` and `worker_db_substrate` sub-statuses.

## Requirements checklist

- [ ] `_phase_service_readiness`: remove `mocked_local` warn-downgrade — unreachable
      Core is always `fail`. Interpret richer collector result (`api`/`worker_db_substrate`
      sub-signals); enrich evidence. Backward compatible: plain `{"status":"ok"}` ⇒ ok.
- [ ] New reason code `SMOKE_WORKER_UNAVAILABLE` (API up, worker DB substrate down).
- [ ] `_default_service_collector`: probe `/healthz` AND `/readyz` (parse JSON
      regardless of status), read `checks.db.ok` as the worker DB-substrate signal
      (not worker-process liveness); degrade gracefully.
- [ ] `_start_success_payload`: lead `next_steps` with provider-free proof.
- [ ] `smoke run` `--help` / `--mocked-local` help text: describe no-token local proof.
- [ ] Tests first (TDD): regression fail-not-warn, mocked success keeps health real,
      worker-down fails, default collector unit tests, backward compat, start next
      step provider-free, CLI help provider-free, setup→start→smoke chain.

## Implementation steps

1. Update `tests/unit/service/test_smoke_parts/test_smoke_part_002.py` regression
   test (warn→fail) + add new collector/phase tests.
2. Update `tests/unit/cli/test_start_commands.py`, `test_smoke.py`,
   `test_setup_commands.py`.
3. Implement `src/awf/service/smoke.py` changes.
4. Implement `src/awf/cli/start_commands.py` next_steps change.
5. Implement `src/awf/cli/profile_smoke_commands.py` help text.

## Verification commands

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev ruff format --check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_smoke_parts \
  tests/unit/cli/test_smoke.py tests/unit/cli/test_start_commands.py \
  tests/unit/cli/test_setup_commands.py tests/unit/docs/test_catalog_coverage.py -q
```

Broad coverage/CI validation owned by AWF after agent completion.
