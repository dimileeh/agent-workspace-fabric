# Review Thread PRRT_kwDOSJAM6s6CjWoa Plan

## Problem Statement And Scope

The review thread reports that `docker/compose/local-service.yml` mounts
host-home credential paths through the shared control-plane service anchor,
which grants those mounts to every service using the anchor. The local service
docs say the compatibility exception is for API readiness and worker
provisioning, not migrations.

Scope is limited to the local-service Compose contract, static regression
coverage, and directly related documentation wording. This does not implement
a full secret broker or remove the existing local-development compatibility
mounts needed by API readiness and worker auth seeding.

## Requirements Checklist

- Prove the shared `x-awf-service` anchor no longer carries host-home auth
  mounts.
- Preserve the existing read-only host credential compatibility mounts for
  `api` and `worker`.
- Ensure `migrate` does not receive host-home auth mounts.
- Keep the Docker socket, SSH-agent socket, and host work directory mounts
  available to all control-plane services.
- Align docs with the per-service API/worker mount boundary.

## Implementation Steps

1. Update `tests/integration/test_local_service_compose.py` first so the
   current Compose file fails by expecting host auth mounts to be absent from
   the shared anchor and `migrate`.
2. Move credential mounts out of the shared `x-awf-service` volume list in
   `docker/compose/local-service.yml`.
3. Add explicit per-service auth volume entries to `api` and `worker`, while
   leaving `migrate` with only the shared base volumes.
4. Update the local UID strategy document wording from all control-plane
   containers to the API/worker services that need the mounts.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_local_service_compose.py -q
uv run --python 3.12 --extra dev ruff check tests/integration/test_local_service_compose.py
```

Pass criteria: the focused integration test passes, and ruff reports no
issues for the touched test.
