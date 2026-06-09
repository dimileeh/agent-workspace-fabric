# Local Control-Plane Container UID/GID Strategy

This is the decision record for the local AWF Core control-plane container
UID/GID strategy. It is scoped to local Docker / Docker Desktop. GKE pod
security context, `fsGroup`, and `runAsUser` are explicitly out of scope and
are referenced only as forward pointers below.

## Decision Summary

The local AWF Core control plane (`api`, `worker`, `migrate` services in
`docker/compose/local-service.yml`) **runs as `root` inside its container by
default**. It then performs explicit post-provision ownership repair to chown
each per-workspace worktree, the linked worktree git directory, and the
agent-writable subset of the bare mirror admin metadata to UID/GID `1000`,
matching the `agent` user baked into `awf-agent-runtime`. The agent container
itself runs as the unprivileged `agent` user (UID/GID `1000`). The repair
helper lives in `src/awf/node/git_manager.py` (`_prepare_agent_writable_worktree`,
`_agent_writable_git_targets`).

Rejected: making the local control-plane run as the host UID/GID. The
trade-offs (per-host image rebuild, docker-group GID portability, SSH-agent
socket on Docker Desktop, host-home read-only mount ergonomics) lose for an
open-source local Core default. See "Rejected option" below for the full
analysis.

## Why This Is The Default

- The repair model is what already ships, and it has been hardened twice:
  `3964bed2` ("avoid chowning shared mirrors") and `aa866959` ("skip chowning
  shared object files"). Neither incident required redesigning the model;
  both narrowed the chown surface so the agent gets writability without
  Docker Desktop on macOS rejecting per-file metadata writes.
- It is generic across Linux and Docker Desktop / macOS without per-host
  image rebuilds. The agent UID is a fixed contract (`1000`) baked into
  `awf-agent-runtime`, and the host UID is irrelevant inside the
  control-plane container.
- The Docker socket and Docker Desktop SSH-agent socket are owned by `root:0`
  inside the control-plane container; running as `root` removes a class of
  permission failures that would otherwise need per-platform group
  resolution.
- The agent container stays unprivileged. The control-plane chown is the
  smallest seam that gives the agent writability without elevating the
  agent's container privileges.

## Pillar Analysis

These are the per-pillar considerations the acceptance criteria ask the
decision to capture.

### Docker Socket

The control plane mounts `/var/run/docker.sock:/var/run/docker.sock` so it
can drive the host Docker daemon for per-workspace stacks.

- **Linux**: the host socket is typically owned by `root:docker`. The
  control-plane container would need to be in the host `docker` group to use
  the socket as non-root, but the host docker GID is host-specific (e.g.
  `999` on Debian, `998` on some Fedora setups, `1001` on others). Baking a
  host-specific GID into the image breaks portability; resolving the GID at
  container start adds an entrypoint that has to be rewritten per platform.
  Running as root sidesteps the question.
- **Docker Desktop (macOS / Windows)**: the socket inside the userland VM
  bind-mount is owned by `root:0`. There is no portable group to add a
  non-root user to. Root inside the container is the simplest path.

### SSH And Auth Mounts

The control plane mounts the operator's SSH-agent socket into each
control-plane service. API and worker also opt in to a curated set of
read-only credential paths:

- `${AWF_HOST_SSH_AUTH_SOCK:-${SSH_AUTH_SOCK:-/run/host-services/ssh-auth.sock}}`
  is mounted at `/run/host-services/ssh-auth.sock` inside the control-plane
  containers. Docker Desktop keeps using its SSH-agent forwarder by default;
  Linux hosts can use the shell's `$SSH_AUTH_SOCK` or set
  `AWF_HOST_SSH_AUTH_SOCK` explicitly. The Docker Desktop forwarder is owned by
  `root:0`; a non-root container would need additional setup (running
  `ssh-agent` inside, or chowning the socket - both fragile). Running as root
  reads the forwarder directly.
- The read-only host-home credential mounts (`~/.gitconfig`, `~/.ssh`,
  `~/.config/gh`, `~/.config/gcloud`, `~/.codex`, `~/.claude`,
  `~/.claude.json`, `~/.gemini`, `~/.config/opencode`, `~/.ollama`) are
  granted only to the API and worker services. They work for both root and
  non-root because read-only access only needs file mode bits to allow
  other-readable.
- Cursor auth is env-only (`CURSOR_API_KEY`) and does not require a host-home
  credential mount.
- Per-workspace seeded auth directories (`work_dir/auth/<workspace_id>/...`)
  are copied from the read-only host-home sources and then chowned to the
  agent UID by `src/awf/node/auth_mounts.py:_chown_workspace_auth_sources`
  so the agent can read and write them in the per-workspace stack. This
  chown step requires the control plane to be `root`, because the source
  files were copied from read-only mounts and now need to be owned by an
  arbitrary UID different from the calling process.

### Bind-Mounted AWF State (`AWF_HOST_WORK_DIR`)

The local stack mounts `${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}` at the
same absolute path inside the control-plane container, so AWF state
(mirrors, worktrees, artifacts, logs, compose projects, auth) is host-visible
and can be reused by the host Docker daemon when launching per-workspace
stacks.

- **Linux**: with the control plane running as root, files created under
  `AWF_HOST_WORK_DIR` are owned by `root:root` on the host. The agent UID
  (`1000`) is repaired in by the chown step for the worktree subtree, the
  linked worktree git dir, and the agent-writable mirror admin dirs (see
  `_agent_writable_git_targets` for the exact set). Operator-side cleanup
  paths that need to remove AWF state require `sudo` on Linux. This is an
  accepted trade-off; cleanup commands the operator runs through `awf` (the
  in-container worker) act as root and do not need sudo. The bare git
  object database under `mirror/objects/` keeps file-level root ownership;
  only the directory entries are repaired so the agent can add new objects
  without forcing the chown step to walk loose object files (the
  `aa866959` fix).
- **Docker Desktop (macOS)**: file ownership is mediated by the userland VM
  bind-mount. Files created by a root container appear root-owned inside
  the VM; on the macOS host they appear owned by the operator (this is a
  Docker Desktop file-sharing quirk, not a UID translation we control).
  The chown step still runs and is a no-op for files that already lack
  Docker Desktop ownership metadata; loose object files in particular are
  excluded because the chown surfaced as `PermissionError` on macOS
  (`aa866959`).

### Linked Worktree Metadata

`git worktree add` creates a linked worktree whose git directory lives
inside the bare mirror at `mirror/.git/worktrees/<id>/`. The linked
worktree directory contains `HEAD`, `commondir`, `gitdir`, and a per-worktree
`logs/HEAD` reflog.

- The repair helper resolves the linked git dir via `_linked_worktree_git_dir`
  and chowns it recursively. This is cheap (one worktree per workspace) and
  avoids chowning the entire mirror.
- The same per-worktree repair is also re-run immediately before profile setup
  and around PR-monitor host-side commits. Runtime directories such as `.venv`
  may be created after provisioning by root-owned control-plane work, so
  ownership repair is a recurring setup/commit invariant, not only a
  post-worktree-add step.
- The bare mirror's `worktrees/` admin directory is chowned non-recursively
  so `git worktree remove` and `git worktree prune` can mutate the registry
  from either side (control plane or agent), without forcing a recursive
  chown over every per-worktree subtree.
- The bare mirror's `refs/` and `logs/` (when present) are chowned
  recursively so commit-side ref updates and ref-log appends succeed.

### Linux vs macOS Behavior

- **Linux**: true UID-based file ownership. Root in the container can chown
  to any UID; the host filesystem records that UID directly. The chown
  step is exhaustive and reliable.
- **Docker Desktop (macOS)**: the file-sharing layer can return `EPERM` for
  individual files when their host metadata predates the container's
  visibility. The chown helper catches this for the loose object files
  (which always existed before the linked worktree was created) by
  excluding them from the recursive walk; everything else lives inside the
  per-workspace subtree the control plane just created and chowns cleanly.
- macOS-only regressions cannot be reproduced under Linux CI. The unit
  tests reproduce the known modes by raising `PermissionError` from a fake
  `os.chown` (see `test_agent_owner_repair_skips_unwritable_loose_object_files`
  in `tests/unit/node/test_git_manager.py`). New macOS regressions must be
  caught by operator dogfooding plus the contributor smoke run with
  `AWF_SKIP_DOCKER_TESTS=0`.

### Cleanup Permissions

- With a root control plane, AWF cleanup runs as root inside its container
  and can always remove worktrees and the bare mirror. `awf service gc`
  and the per-workspace cleanup paths in
  `src/awf/node/cleanup.py` therefore do not need elevated host privileges
  — they use the worker's normal Docker socket access plus filesystem
  writes inside `AWF_HOST_WORK_DIR`.
- An operator who wants to remove `AWF_HOST_WORK_DIR` from the host shell
  needs `sudo rm -rf` on Linux because the directory is root-owned. The
  recommended path is the in-container `awf service gc` command, not host
  `rm`.
- A host-UID control plane would let the operator clean up from the host
  shell without `sudo`, but it would also leak host UID into the chown
  surface and break the SSH-agent / Docker-socket assumptions above.

### Migration Path For Existing Root-Owned State

Today's default is the same as the chosen default. **No migration is
required for existing AWF installs.** The decision keeps the model that has
been in production locally; it does not change the on-disk ownership layout.

If a future revision flips the default to a host-UID control plane, the
migration shape is:

1. Stop the local stack from the AWF root: `docker compose stop api worker`.
2. Repair host ownership of the AWF work dir to the operator UID/GID:
   `sudo chown -R "$(id -u)":"$(id -g)" "${AWF_HOST_WORK_DIR:-$HOME/.awf/service}"`.
3. Re-bootstrap with the new image: `awf service bootstrap`.

If we ever ship that flip, this doc will gain a "Migration" section with
the exact `awf service repair-state` command (currently not implemented).
The repair will need to walk the mirror's `objects/` tree carefully to
avoid the macOS metadata pitfall that motivated `aa866959`.

## Rejected Option: Control-Plane As Host UID/GID

The alternative is to run `api`, `worker`, and `migrate` as the operator's
host UID/GID. This was rejected for the local Core default for the
following reasons:

- **Per-host image rebuild.** A `USER 1000:1000` baked into the image only
  matches operators whose host UID is `1000`. Operators on macOS Docker
  Desktop who use a non-1000 UID would need to rebuild the image with their
  UID, or AWF would need a per-host build step. That breaks the
  open-source-friendly default of "pull the published image, run
  bootstrap".
- **Docker-group GID portability.** The host docker group GID is
  host-specific. A non-root container needs to be in that group to use
  the socket. Resolving the GID at container start (entrypoint that
  inspects the socket and `groupadd`s) adds a per-platform path that has
  to be tested on Linux + Docker Desktop and is fragile when Docker
  upgrades shift the socket ownership.
- **SSH-agent socket on Docker Desktop.** `/run/host-services/ssh-auth.sock`
  is owned by `root:0` inside the Docker Desktop VM. A non-root container
  cannot use it without additional setup (chmod the socket, run an
  in-container ssh-agent, or fall back to env-injected keys). The
  control-plane's git operations (`git fetch`, `git push`) need a working
  SSH agent for repos that authenticate over SSH; root sidesteps the
  problem.
- **Host-home read-only mount ergonomics.** The credential mounts
  themselves work fine read-only for non-root, but the per-workspace
  seeded auth directories (`work_dir/auth/<id>/...`) need to be writable
  by the agent UID. Today the control plane chowns those after copying
  them from the read-only host-home sources; that chown only succeeds if
  the calling process can chown to an arbitrary UID, i.e. is root. A
  host-UID control plane would either need to keep the auth dirs owned by
  the host UID (and rebuild the agent image to use that UID) or run the
  chown step out of band (a fresh privilege seam).

The rejection is for the **default**, not forever. A future Linux-only
"single-user developer machine" mode could plausibly run the control plane
as the host UID with a parameterized agent image. That is a follow-up
whose value is mostly cosmetic (cleaner host ownership of `AWF_HOST_WORK_DIR`)
and whose cost is real (per-host image build, SSH-agent on Docker Desktop,
docker-group resolution).

## Operator Override

There is no operator override today. The agent runtime UID/GID is the
hard-coded constant `AGENT_RUNTIME_UID = 1000` / `AGENT_RUNTIME_GID = 1000`
in `src/awf/node/git_manager.py`, imported by `worker.py` and wired into
`GitManager` and `ServiceAuthMountResolver`.

If we ship an `AWF_LOCAL_AGENT_UID` / `AWF_LOCAL_AGENT_GID` override later,
the agent-runtime image must be parameterized to create the `agent` user at
the requested UID/GID. The current image (`docker/agent-runtime.Dockerfile`
line 236) hard-codes the UID/GID via `useradd --create-home --shell /bin/bash
agent`, which uses the next available UID (typically `1000`). Until that
parameterization ships, an override would silently mismatch and the agent
container would be unable to write its mounted worktree.

## Forward Pointers (Out Of Scope For This Decision)

- **GKE pod security context.** Cloud control-plane pods will likely set
  `runAsUser`, `runAsGroup`, and `fsGroup` explicitly. The on-disk
  ownership repair model used locally does not translate directly to
  Kubernetes; the GKE design will use volume-level `fsGroup` semantics or
  init containers that chown the mounted volume on pod start. That work
  belongs in the GKE readiness backlog, not here.
- **Multi-tenant isolation.** Local Core does not enforce cross-workspace
  UID isolation; all agent containers share UID `1000`. A future per-tenant
  UID scheme would parameterize the agent image and the chown helper, and
  would coordinate with the GKE design.

## Test Contract

The decision is locked by the following tests:

- `tests/unit/node/test_git_manager.py`
  - `TestAddWorktree::test_prepares_linked_worktree_git_paths_for_agent_user`
    — locks the chown surface produced by `_agent_writable_git_targets`
    when the control plane runs as root, including the worktree subtree,
    the linked worktree git dir, the bare mirror's `objects/`, `refs/`,
    and `worktrees/` entries.
  - `TestAddWorktree::test_agent_owner_repair_skips_unwritable_loose_object_files`
    — locks the `aa866959` regression: loose object files that the chown
    cannot write (Docker Desktop / macOS metadata case) do not stop the
    repair.
  - `TestAgentWorktreeWritable::test_agent_writable_targets_lists_required_paths_excluding_loose_objects`
    — locks the explicit per-pillar contract: `refs/`, `worktrees/`,
    `objects/` are in the target set; loose object files are not.
  - `TestAgentWorktreeWritable::test_agent_writable_targets_omits_logs_when_mirror_lacks_it`
    — locks that the helper does not synthesize a chown for a
    non-existent `logs/` directory (bare mirrors default to no
    top-level `logs/`).
  - `TestAgentWorktreeWritable::test_prepared_worktree_supports_agent_git_status_add_commit`
    — proves the prepared worktree accepts `git status`, `git add`, and
    `git commit` when the controlling user is the agent UID.
- `tests/integration/test_workspace_agent_git_in_workspace.py`
  - `test_agent_container_can_git_status_add_commit_in_workspace` — full
    Docker integration: the rendered workspace stack lets the
    `awf-agent-runtime` container's `agent` user run the three required
    git commands in `/workspace`. Skipped on developer machines when
    Docker is unavailable, the agent-runtime image is not present, or the
    test process is neither UID `0` nor UID `1000` *and* passwordless
    sudo is not available. GitHub Actions ubuntu-latest runs as UID
    `1001` with passwordless sudo, so the CI integration job exercises
    this path via `sudo chown` instead of skipping; if the UID/sudo
    precondition is unmet under `CI=true`, the test fails loudly rather
    than skipping silently (defensive against runner-image changes).

## See Also

- [`docs/AWF_CORE_TRUST_MODEL.md`](AWF_CORE_TRUST_MODEL.md) — local Core
  trust boundary; the Docker daemon plus root-in-control-plane combination
  is the privileged local seam this strategy depends on.
- `src/awf/node/git_manager.py` — `AGENT_RUNTIME_UID`/`AGENT_RUNTIME_GID`
  constants, `_prepare_agent_writable_worktree`, `_agent_writable_git_targets`,
  `_chown_targets`, `_chown_tree`.
- `src/awf/node/auth_mounts.py` — `_chown_workspace_auth_sources`.
- `src/awf/service/worker.py` — imports `AGENT_RUNTIME_UID`/`AGENT_RUNTIME_GID`
  and wires them into `GitManager` and `ServiceAuthMountResolver`.
- `docker/agent-runtime.Dockerfile` — the `agent` user (UID/GID `1000`)
  baked into the agent runtime image.
- `docker/compose/local-service.yml` — the local control-plane stack with
  no `USER` directive (root by design).
