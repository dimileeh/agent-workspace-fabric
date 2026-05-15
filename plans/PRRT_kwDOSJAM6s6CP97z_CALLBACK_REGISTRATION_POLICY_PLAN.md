# PRRT_kwDOSJAM6s6CP97z Callback Registration Policy Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6CP97z` reports that callback registration can persist a target that violates `AWF_CALLBACKS_REQUIRE_HTTPS` or `AWF_CALLBACKS_ALLOWED_HOSTS`; those checks currently run during delivery, so the target later fails every attempt with `CALLBACK_TARGET_INVALID`.

Scope is limited to registration-time enforcement of the existing static callback target policy. DNS resolution and delivery-time defense-in-depth behavior stay unchanged.

## Requirements Checklist

- Add regression coverage proving `POST /v1/callbacks` rejects `http://` targets when callbacks require HTTPS.
- Add regression coverage proving `POST /v1/callbacks` rejects non-allowlisted hosts when callback allowed hosts are configured.
- Ensure rejected registration requests do not create callback subscription rows.
- Keep delivery-time validation in place for legacy or manually edited rows.
- Preserve idempotency conflict behavior and existing URL/event schema validation.

## Implementation Steps

1. Add API tests that override callback settings and assert policy-rejected registrations return 422 without inserts.
2. Run the new tests before implementation and confirm they fail with the current 201 behavior.
3. Add a registration-time static policy validator shared with delivery validation where practical.
4. Pass route settings into `CallbackService` and translate static policy failures into a 422 response.
5. Run the focused tests, then the relevant callback test surface.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py -q`
  - Passes all callback API tests, including the new registration policy regressions.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passes existing callback service/delivery behavior.
