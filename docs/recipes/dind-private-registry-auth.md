# Recipe: private-registry Docker auth in a Docker-in-Docker workspace

**Problem.** Your project builds or pulls images from a **private registry**
(e.g. a Bitbucket Java/Gradle build that needs `docker pull registry.example.com/...`
during its Gradle tasks). Inside an AWF workspace running in Docker-in-Docker
(`docker.mode: dind`), `docker`/Gradle talk to the managed DinD daemon at
`tcp://docker:2375`, but that daemon has **no registry credentials**, so private
pulls fail with `unauthorized` / `denied`.

**Solution.** Give the agent container a Docker `config.json` (the same file
`docker login` writes) by mounting the host copy read-only via a profile **secret
lease**, then point `DOCKER_CONFIG` at the directory that contains it. Docker and
Gradle then read `$DOCKER_CONFIG/config.json` and authenticate to the registry.

Everything below uses existing AWF mechanisms — no core code change is required.

## Copy-pasteable `.awf/workspace.yml`

```yaml
# .awf/workspace.yml — DinD workspace authenticating to a private registry.
name: my-project
version: 1

runtime:
  environment:
    # Docker (and Gradle's Docker integration) read $DOCKER_CONFIG/config.json
    # for registry credentials. Point it at the DIRECTORY, not the file.
    DOCKER_CONFIG: /run/awf/secrets/docker
    # DOCKER_HOST is auto-injected for docker.mode: dind; shown here for clarity.
    DOCKER_HOST: tcp://docker:2375

docker:
  mode: dind

secrets:
  - name: docker-registry-auth
    kind: mount
    mode: ro
    provider: local-file        # 'host-file' is an accepted alias.
    # Absolute path to the host Docker config.json holding the registry creds.
    # NOTE: '~', '$HOME', '${AWF_HOST_HOME}', '/home/<user>' and '/Users/<user>'
    # roots are rejected as too broad — use a literal path.
    ref: /home/youruser/.docker/config.json
    target: /run/awf/secrets/docker/config.json
```

A ready-to-run copy of this profile lives at
[`examples/dind-private-registry/.awf/workspace.yml`](examples/dind-private-registry/.awf/workspace.yml).

## How it fits together

- **`provider: local-file`** (alias `host-file`) mounts an existing host file
  read-only into the agent container. Unlike `local-auth` it does **not** restrict
  the mount target to a fixed allowlist, so an arbitrary target such as
  `/run/awf/secrets/docker/config.json` is allowed. Use a `/run/awf/secrets/...`
  target to match AWF's safe-mount convention.
- **`provider: local-auth`** only knows the fixed refs `gh`, `gcloud`, `gitconfig`,
  and `ssh` — there is **no** `docker` ref — so it cannot mount an arbitrary
  `config.json`. Lead with `local-file` for this recipe.
- **`DOCKER_CONFIG` points at a directory**, not the file. Docker reads
  `$DOCKER_CONFIG/config.json`. Mount the file at `/run/awf/secrets/docker/config.json`
  and set `DOCKER_CONFIG=/run/awf/secrets/docker`.
- **`DOCKER_HOST` is auto-injected** for `docker.mode: dind` (AWF sets
  `DOCKER_HOST=tcp://docker:2375` when the profile does not already declare it), so
  the `runtime.environment` line above is optional — included only for clarity.

## Make the host `config.json` visible to the control plane (bundled local-service stack)

AWF validates the lease **inside the control-plane worker container**: the
resolver calls `source.exists()` on the `ref` path *before* the agent container is
created (`src/awf/node/secret_mounts.py`, run from `src/awf/service/worker.py`). In
the bundled `docker/compose/local-service.yml` stack the `api`/`worker` containers
only bind-mount a **fixed allowlist** of host auth paths — `~/.config/gh`,
`~/.config/gcloud`, `~/.gitconfig`, `~/.ssh`, `~/.codex`, `~/.claude`, `~/.gemini`,
`~/.config/opencode`, `~/.grok`, `~/.ollama` — and **`~/.docker` is not one of them.**
So a `ref:` pointing at `~/.docker/config.json` resolves to a path the worker cannot
see, and a `required: true` lease (the default) fails at provision time with
`SECRET_LEASE_SOURCE_MISSING` even though the file exists on the host.

Before using this recipe on that stack, make the host config visible to the control
plane at the **same absolute path** (AWF mounts host auth paths host-equal). Add a
read-only bind mount of the host `config.json` to **both** the `api` and `worker`
services, mirroring the existing auth volumes:

```yaml
# Add to the `api` AND `worker` `volumes:` lists in docker/compose/local-service.yml
# (or a Compose override merged on top), then point `ref:` at the same host path.
- ${AWF_HOST_HOME:-${HOME}}/.docker/config.json:${AWF_HOST_HOME:-${HOME}}/.docker/config.json:ro
```

This applies only to the **bundled local-service stack**. When you run `awf worker`
directly on the host (no control-plane container), the worker already sees
`~/.docker/config.json` on the host filesystem and no extra mount is required.

## `ref` restrictions (read before copying)

`ref` must be a **literal absolute path to an existing file**. AWF deliberately
rejects "too broad" home-rooted sources to avoid mounting an entire home directory:

| Rejected `ref`                        | Reason code (resolution)        |
| ------------------------------------- | ------------------------------- |
| `~/.docker/config.json`               | `SECRET_LEASE_SOURCE_TOO_BROAD` |
| `$HOME/...`, `${HOME}/...`            | `SECRET_LEASE_SOURCE_TOO_BROAD` |
| `$AWF_HOST_HOME/...`, `${AWF_HOST_HOME}/...` | `SECRET_LEASE_SOURCE_TOO_BROAD` |
| `/home/<user>`, `/Users/<user>` (root) | `SECRET_LEASE_SOURCE_TOO_BROAD` |

So write the full path explicitly, e.g. `/home/alice/.docker/config.json`. Other
guards: the source must already **exist** and be a **file** (else
`SECRET_LEASE_SOURCE_INVALID`), and the mount must be **read-only** (`mode: ro`;
`rw` raises `SECRET_LEASE_WRITABLE_UNSUPPORTED`).

## Required vs optional leases

- `required: true` (the default): if the host `config.json` is missing at provision
  time, the workspace fails with `SECRET_LEASE_SOURCE_MISSING` — fail-fast when the
  registry auth is mandatory.
- `required: false`: a missing source is **skipped** and recorded in sanitized lease
  metadata (no secret value), so the workspace still provisions. Use this when the
  private pull is optional or only some contributors have credentials.

## Security notes

- **Never paste token values** into `.awf/workspace.yml`. The lease references a host
  file by path; AWF mounts it read-only and records only **sanitized lease metadata**
  (name, provider, target, reason codes) — never the file contents or any token.
- The mount is **read-only**, so the agent cannot rewrite your host `config.json`.
- This is a local-mode path; it is **not** a cloud secret broker.

## Verify inside the agent

Once the workspace is up, from the agent container:

```bash
# DOCKER_CONFIG is already set from runtime.environment; the config.json is mounted.
docker pull registry.example.com/your-org/private-image:tag

# Or run a Gradle build that performs the private pull:
./gradlew build
```

A successful authenticated pull confirms the mounted `config.json` and
`DOCKER_CONFIG` are wired correctly.
