# Upgrade Guide

Use the upgrade path that matches the lane you used for first run. Keep the
same project path for mocked smoke; the examples below use
`$HOME/awf-eval-project`.

Do not generate replacement service secrets during upgrade. Reuse the `.env`
created during first run, or export the same `AWF_API_TOKEN`,
`AWF_POSTGRES_PASSWORD`, and related local service values used by the running
Core.

The public curl installer lane is release-gated until the hosted installer URL,
manifest, checksums, and release artifacts are published and verified.

## Provider foundation compatibility floor

The oldest compatible rollback build for provider connection schema and
execution contract v1 is implementation commit **`09793d89fcd27cac2cdbcf2aa6777c703a8c4570`**.
It includes migration **85ce3f9b2a61**, worker capability enforcement, isolated
runtime support and complete snapshot recovery. Package version `0.1.0` is not
a unique rollback floor; baseline `3aae1ac6e` predates this contract and is not
compatible with the expanded schema. Hosted checkpoint cut/restore now expects
schema head `85ce3f9b2a61` exactly.

Required rollout order:

1. Keep `AWF_THIRD_PARTY_AGENT_PROVIDERS_ENABLED=false`. Through the operator/AWF
   lifecycle, drain and stop pre-floor API/worker processes before migration.
   Capability metadata cannot make an already-running old worker safe.
2. Apply migration `85ce3f9b2a61` and deploy API/workers at or above the floor.
   Build/deploy the corresponding agent runtime image containing
   `/opt/awf/provider_config.py`; CLI version pins alone do not prove that this
   image enforces the contract.
3. Verify native behavior, fresh worker heartbeats with
   `execution_contract_version=1`, and runtime `python /opt/awf/provider_config.py --contract-version`
   reporting `1`. Mixed/unknown live queue consumers must
   not admit expanded work. Preserve the schema-ahead startup guard.
4. Complete T4–T6: catalog/diagnostic interfaces, actual agent-network admission,
   two dependent tool turns with typed evidence, all six third-party paths plus
   native baselines, and real monitor repair/restart identity dogfood. The
   [foundation evidence](AGENT_MODEL_PROVIDERS.md#remaining-release-gates-and-preliminary-evidence)
   contains failures/inconclusive paths and the missing OpenRouter key, not
   release passes. Keep admission disabled while any required gate is open.
5. Only then explicitly enable third-party admission on the compatible fleet.
   In this foundation, enabling the flag alone still returns an explicit
   unsupported network verification result; readiness overrides cannot bypass
   that check.

Immediate rollback starts by disabling admission, including new third-party
fallback promotions. Drain or park already-selected workspaces on compatible
code without replacing their selection with native defaults. Keep the expanded
schema, frozen execution snapshots, cooldown history and events intact. Roll
back binaries only to this floor or a later compatible commit. Never deploy
pre-floor binaries while keeping the expanded schema, or use schema downgrade
as an immediate rollback shortcut. The migration refuses downgrade when scoped
cooldown history would be discarded.

Parked work may resume only after compatible workers/runtime, selected
credential references and the selected network are restored. Any deliberate
change of connection must use an explicit complete fallback/new selection;
do not edit a snapshot to make an old runtime silently accept it.

## Orphan Resource Cleanup

`auto_cleanup_orphans` now defaults to enabled. After upgrade, the first worker
sweep may perform a one-time catch-up reap of terminal or missing AWF Docker
volumes and managed worktrees older than the 168h orphan retention window. Set
`AWF_AUTO_CLEANUP_ORPHANS=false` before starting the service to keep orphan
cleanup in report-only mode.

If you reuse a `.env` from an earlier AWF install, check whether it already
contains `AWF_AUTO_CLEANUP_ORPHANS=false`. That old seeded value overrides the
new default; remove the line or change it to `AWF_AUTO_CLEANUP_ORPHANS=true`
before `awf start` if you want the upgraded service to reap stale orphan
resources automatically.

## uv tool

```bash
uv tool upgrade agent-workspace-fabric
awf start
awf service status --format pretty
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

## pipx

```bash
pipx upgrade agent-workspace-fabric
awf start
awf service status --format pretty
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

## Virtualenv / pip

Activate the virtualenv that owns `awf`, then upgrade the package:

```bash
. .venv/bin/activate
pip install --upgrade agent-workspace-fabric
awf start
awf service status --format pretty
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

## Source Checkout With Global Tool Install

Run from the AWF checkout:

```bash
git pull --ff-only
uv tool install . --force
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

## Source Checkout With No Global Install

Run from the AWF checkout:

```bash
git pull --ff-only
uv sync --extra dev
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

## Local Service Stack

Before upgrading a service stack with live control-plane data, follow the
[Local Service Upgrade](CONCEPTS.md#local-service-upgrade) runbook. It captures a
pre-upgrade Postgres backup, runs migrations through `awf service bootstrap`,
and points migration failures at the migrate logs before changing volumes or
state. Treat rollback after data migrations as a restore from that backup, as
covered in [Local Service Rollback](CONCEPTS.md#local-service-rollback).

Use `awf service readiness --format pretty` or
`awf service release-readiness --format pretty` only when you need the Core
release gate. That gate checks historical PRD SLO evidence and can fail even
when local health is green.

## Rollback

If a local upgrade blocks development:

1. Return to the previous package version or Git revision for the lane you use.
2. Run the matching `awf start` command from the sections above.
3. Check `awf service status --format pretty`.
4. Rerun `awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty`.

AWF service bootstrap is designed to be idempotent for local development. Do not
delete `.awf` state or Docker volumes unless a specific troubleshooting step
requires it.
