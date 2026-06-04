# Root Docker Compose Cold-Start Plan

## Summary

Complete the current root runtime work so a fresh AWF source checkout can run:

```bash
docker compose up --build
```

The command should build and start Postgres, migrations, API, worker,
`awf-agent-runtime:latest`, and the local console. `awf setup` / `awf start`
remain the guided path; root Compose is the expert/source-inspection path.

## Implementation

- Preserve root `compose.yaml` as the public entrypoint and keep it packaged.
- Pin root `compose.yaml` to the same Compose project name used by guided
  service startup so root Compose reuses the same persisted control-plane
  Postgres volume instead of creating a fresh sibling DB.
- Update `docker/compose/local-service.yml` so local Compose defaults
  `AWF_API_TOKEN=local-dev-token` and `AWF_POSTGRES_PASSWORD=awf_dev`, binds
  API/console/Postgres to loopback, builds `awf-agent-runtime:latest`, and adds
  the console service.
- Add `apps/console/Dockerfile` using `npm ci` from `package-lock.json`, builds
  Next, and starts on `0.0.0.0:3000`.
- Update env/readiness handling so CLI setup/start mirrors local Compose
  defaults and does not block solely because those defaults are absent from
  `.env`.
- Update docs and validation notes so raw source checkout is a single
  `docker compose up --build` path, with provider credentials documented as
  optional for real PR/provider workflows.

## Tests

- Static Compose config with an empty env file succeeds and includes
  `postgres`, `migrate`, `api`, `worker`, `agent-runtime`, and `console`.
- Static Compose images include `postgres:16-alpine`,
  `awf-control-plane:local`, `awf-agent-runtime:latest`, and
  `awf-console:local`.
- Unit tests cover local Compose defaults in env resolution and the required
  service env setup check.
- Docs tests cover the single-command root Compose lane.

## Assumptions

- `docker compose up --build` is the cold-checkout contract; `docker compose up`
  may only work after images already exist.
- The local placeholder token is acceptable only for loopback-bound local
  Compose with `AWF_ENV=local`; production config must continue rejecting it.
