# PRRT_kwDOSJAM6s6F504S Companion Pre-build Timeout Plan

## Problem Statement and Scope

A review thread on PR #330 (companion image build caching, issue #298) points out
that the cache **pre-build** path always uses the fixed
`COMPANION_BUILD_CAPTURE_TIMEOUT_SECONDS` (1800s) and never honors the configured
`compose_up_timeout_seconds` knob, while the inline `docker compose up` path caps
its build+readiness subprocess at `_compose_up_capture_timeout_seconds(effective,
wait=True)` = `2*effective + 60`.

This decoupling produces two real divergences:

- When `effective > 1800` (a profile/companion raises the documented cold-cache
  build budget), the pre-build false-fails at 1800s, returns `None`, and falls
  back to an inline rebuild — the "fail pre-build early then rebuild inline"
  behavior the reviewer named, defeating the cache for exactly the slow-build
  companions caching targets.
- When `effective < 1800` (e.g. default 300), the pre-build can run a build far
  longer than the configured bring-up budget ("runs longer than configured").

`docs/CONCEPTS.md` (L954-958) explicitly documents `compose_up_timeout_seconds` as
the knob to raise "when its Dockerfile needs extra cold-cache build time" — and
the pre-build **is** the cold-cache build, so it must honor that knob.

Scope: thread the effective compose-up build budget into the companion pre-build;
focused regression coverage only. No change to clamp bounds, the cache-fallback
contract, or `up()`/`ensure_project_up()`.

## Decision

Make the pre-build capture timeout equal the inline `up(wait=True)` subprocess cap:
`_compose_up_capture_timeout_seconds(effective, wait=True)` (= `2*effective + 60`),
where `effective = effective_compose_up_timeout_seconds(profile, companions)` — the
same stack-wide value used by the launch path and `monitor_handoff.py`.

Rejected alternatives:
- `effective + 60` (`wait=False`): strictly below the inline cap, so it would
  re-introduce false-fail-then-rebuild for builds taking ~1.5*effective.
- `max(1800, ...)`: keeps the flat floor; does not honor a lowered knob and does
  not address "runs longer than configured".

The single stack-wide `effective` (not a per-companion raw value) is correct because
the inline path applies one stack-wide budget to one shared `docker compose up`.

## Requirements Checklist

- Pre-build capture timeout tracks `effective` and equals the inline `up()` cap.
- Pre-build never false-fails a build the inline path would have completed.
- Keep `COMPANION_BUILD_CAPTURE_TIMEOUT_SECONDS=1800.0` as the
  `build_companion_image` default-arg safety floor (do not remove).
- Preserve the build-failure → inline `build:` fallback contract.
- Strict TDD: failing regression first; keep 99% coverage intact.

## Implementation Steps

1. (Test, red) Add regression coverage that the launcher forwards
   `capture_timeout_seconds == 2*effective + 60` to the builder, and that
   `CompanionImageBuilder.ensure(..., capture_timeout_seconds=X)` forwards `X` to
   `build_companion_image`.
2. `src/awf/node/companion_images.py`: add required keyword-only
   `capture_timeout_seconds: float` to `CompanionImageBuilder.ensure()` and forward
   it into `build_companion_image(capture_timeout_seconds=...)`.
3. `src/awf/node/stack_launcher.py`: hoist the `effective_compose_up_timeout_seconds`
   computation above `_build_companion_services`, compute
   `_compose_up_capture_timeout_seconds(effective, wait=True)`, and thread it through
   `_build_companion_services(..., capture_timeout_seconds=...)` into `ensure()`.
   Import `_compose_up_capture_timeout_seconds` from `compose_manager`.
4. Update test fakes (`_FakeCompose.build_companion_image`, `_RecordingBuilder.ensure`)
   to accept and record the new keyword — signature alignment only, no assertion
   weakening.
5. `docs/CONCEPTS.md`: note the cache pre-build now honors the same effective
   `compose_up_timeout_seconds` budget as the inline `up`.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_images.py tests/unit/node/test_stack_launcher_companion_images.py tests/unit/node/test_stack_launcher.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_images.py src/awf/node/stack_launcher.py`
- `uv run --python 3.12 --extra dev mypy`

Full AWF/GitHub validation (whole suite + coverage gate) remains post-agent owned.
