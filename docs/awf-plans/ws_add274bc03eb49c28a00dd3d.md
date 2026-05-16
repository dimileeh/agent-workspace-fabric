# Plan: P1 Local Control-Plane Container UID/GID Strategy Decision

## Scope

Implement TODO line 506-513 of `TODO/pre-gke-industrial-readiness.md`:

> Research and decide the local control-plane container UID/GID strategy.
> Compare keeping API/worker as root with explicit post-provision ownership
> repair versus running local control-plane containers as the host UID/GID.
> Acceptance: document Docker socket, SSH/auth mounts, bind-mounted AWF state,
> linked worktree metadata, Linux/macOS behavior, cleanup permissions, and
> migration path for existing root-owned state; choose the default local
> setup and add regression coverage proving workspace containers can run
> `git status`, `git add`, and `git commit` in `/workspace`.

The slice has two halves:

1. A **decision document** in `docs/` that captures the analysis and the
   chosen default. This is the primary deliverable — the task is named
   "research and decide".
2. A small **regression coverage** addition that proves a workspace agent
   container provisioned by a root control-plane can run the three required
   git commands in its `/workspace` worktree on Linux. Code in
   `src/awf/node/git_manager.py` is touched only if the regression test
   shows a real gap; otherwise the existing `aa866959` repair is sufficient
   and the new test is the gate that keeps it sufficient.

## Background And Current State

Established by reading the codebase before planning (no implementation
files are touched in this phase):

- Control-plane image (`docker/control-plane.Dockerfile`) ships with no
  `USER` directive, so `api`, `worker`, and `migrate` containers run as
  **root** inside the container. Image base: `ghcr.io/astral-sh/uv:python3.12-bookworm`.
- Agent-runtime image (`docker/agent-runtime.Dockerfile` line 141-145)
  creates an `agent` user via `useradd --create-home` (default UID 1000,
  GID 1000) and sets `USER agent`.
- `docker/compose/local-service.yml` mounts into the control-plane:
  - `/var/run/docker.sock` (Docker daemon socket; root-owned on the host)
  - `/run/host-services/ssh-auth.sock` (Docker Desktop SSH-agent forwarder)
  - `${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}` at the same absolute path
    inside the container (the bind-mounted AWF state — mirrors, worktrees,
    artifacts, logs, compose projects, auth)
  - host-home dot-dirs read-only (`.config/gh`, `.config/gcloud`, `.gitconfig`,
    `.ssh`, `.codex`, `.claude`, `.claude.json`, `.gemini`,
    `.config/opencode`, `.ollama`)
- `src/awf/service/worker.py:50-51, 73-74, 122-123` hardcodes
  `_AGENT_RUNTIME_UID = 1000` and `_AGENT_RUNTIME_GID = 1000`, passes them
  to `GitManager(worktree_owner_uid=..., worktree_owner_gid=...)` and to
  `ServiceAuthMountResolver(workspace_owner_uid=..., workspace_owner_gid=...)`.
- `src/awf/node/git_manager.py:_prepare_agent_writable_worktree` runs
  post-provision `chown` only when the control-plane process is root
  (`os.geteuid() == 0`) and both UID/GID are set. After
  `git worktree add`, it chowns the worktree subtree (rw), the linked
  worktree git dir, the mirror dir (non-recursive), `objects/`
  directories-only (the `aa866959` fix — loose object files stay
  root-owned because Docker Desktop on macOS rejects the chown), `refs/`,
  `logs/`, and the `worktrees/` admin dir.
- `src/awf/node/auth_mounts.py:_chown_workspace_auth_sources` chowns the
  per-workspace seeded auth directories (Codex, Claude, Gemini, OpenCode,
  Ollama) so the agent user can read/write them.
- The two recent fixes (`3964bed2` "avoid chowning shared mirrors" and
  `aa866959` "skip chowning shared object files") show that the
  ownership-repair model is fragile on Docker Desktop/macOS specifically
  for the bare git mirror; everything outside that bare mirror has been
  stable.

## Intended Files And Modules

Documentation (primary deliverable):

- `docs/AWF_LOCAL_CONTAINER_UID_STRATEGY.md` (new)
  - The decision record. Captures both options, the per-pillar analysis
    required by the acceptance criteria, the chosen default, and the
    migration path for existing root-owned state directories.
- `docs/AWF_CORE_TRUST_MODEL.md`
  - Add a brief subsection (or extend the "Local Boundary" / "Secrets And
    Credentials" sections) cross-linking to the new strategy doc, since
    UID/GID handling materially affects the local trust boundary.
- `README.md`
  - Add a short pointer in the existing local-service / troubleshooting
    area linking to the strategy doc and listing the host UID/GID
    expectation (1000:1000 by default).

Code (touched only if the integration test shows a real gap):

- `src/awf/node/git_manager.py`
  - No structural change planned. If the new integration test reveals a
    file that the agent UID still cannot write, extend
    `_agent_writable_git_targets` or add a directory-mode normalization
    step (e.g. `chmod g+s` on the worktrees admin dir). Any change must
    be the smallest one that makes the failing test pass.
- `docker/compose/local-service.yml`
  - No functional change planned. Add a short comment documenting that the
    `api`/`worker`/`migrate` services run as root by design and pointing
    to the new strategy doc. Optionally surface an `AWF_LOCAL_AGENT_UID`
    / `AWF_LOCAL_AGENT_GID` operator override only if the strategy doc
    decides that the override is part of the chosen default; otherwise
    skip to keep the surface area minimal.

Tests:

- `tests/unit/node/test_git_manager.py`
  - Add a new `TestAgentWorktreeWritable` class (or extend
    `TestAddWorktree`) that, after `add_worktree(..., worktree_owner_uid,
    worktree_owner_gid)`, runs `git status`, `git add`, and `git commit`
    in the worktree using the configured owner UID — driven through the
    real `git` CLI under the current test user. The test runs as the
    invoking user (no `chown` to a foreign UID would succeed in CI
    anyway), exercising the realistic mode where the controlling process
    is the same UID as the prepared worktree. This proves the
    chown/preparation layout produces a worktree where the three git
    commands succeed without "dubious ownership" or permission errors.
  - Add a focused unit assertion that mirror-bare directories required
    for a downstream commit (`refs/`, `logs/`, `worktrees/<id>`) are all
    listed in `_agent_writable_git_targets` and that loose object files
    are NOT listed (locks in the `aa866959` regression).
- `tests/integration/test_workspace_agent_git_in_workspace.py` (new)
  - Real Docker integration test gated behind `_docker_available()` and
    `AWF_SKIP_DOCKER_TESTS`, mirroring the pattern in
    `tests/integration/test_compose_manager_docker.py`. Steps:
    1. Build `awf-agent-runtime:latest` if missing (or skip if the image
       is not present and the env opts out of building).
    2. Use `GitManager(work_dir=tmp, worktree_owner_uid=1000,
       worktree_owner_gid=1000)` from a process simulating the root
       control-plane (skipped when the test runs unprivileged — the test
       documents the gap and runs the agent-side container assertions
       only).
    3. Render the workspace base compose file with
       `worktree_host_path` pointing at the prepared worktree.
    4. `docker compose up -d agent` (no profile-owned services for
       speed).
    5. `docker compose exec -u agent agent git status`,
       `git add` of a sentinel file, and `git commit -m ...` — all must
       exit 0.
    6. Tear the project down with `docker compose down -v`.
  - When run unprivileged, the test still exercises the agent-container
    side by chowning the temporary worktree to the calling user (which
    matches UID 1000 on most Linux CI runners) and re-using the same
    `docker compose exec -u agent` assertions; it skips with a clear
    reason when neither UID 0 nor UID 1000 is available.

TODO ledger (last):

- `TODO/pre-gke-industrial-readiness.md`
  - Flip the unchecked box on line 506 to `[x]` only after the docs and
    tests above are merged. Add a one-line rationale that links to the
    new strategy doc.

## TDD Sequence

1. Write the new unit tests in `tests/unit/node/test_git_manager.py`
   first. They should fail or be redundant against the current code only
   if `_agent_writable_git_targets` regresses. The intent is regression
   coverage that locks in the post-provision ownership repair contract.

2. Write the integration test in
   `tests/integration/test_workspace_agent_git_in_workspace.py`. Initially
   it must fail when run against an unprepared worktree (no chown), and
   pass when run against a worktree prepared by `GitManager` with the
   agent UID/GID — proving the repair is what makes the agent commands
   work.

3. Run only the failing tests, confirm red, then make the smallest
   change that turns them green:
   - Default expectation: no source change is needed because the
     existing repair already produces a working worktree; the test
     simply locks the contract.
   - If a real gap surfaces (e.g., a directory mode that prevents
     `git commit` because the agent cannot create a lock file under
     `worktrees/<id>/`), add the minimum change to
     `_agent_writable_git_targets` or a sibling helper.

4. Write the strategy doc in `docs/AWF_LOCAL_CONTAINER_UID_STRATEGY.md`,
   referencing the locked-in tests as the regression evidence. The doc
   captures the per-pillar analysis required by the acceptance criteria
   (Docker socket, SSH/auth mounts, bind-mounted AWF state, linked
   worktree metadata, Linux/macOS behavior, cleanup permissions,
   migration path) and records the chosen default.

5. Cross-link the trust model doc and add the README pointer.

6. Flip the TODO checkbox last and commit.

## Strategy Doc Outline

The new `docs/AWF_LOCAL_CONTAINER_UID_STRATEGY.md` will have the
following sections, each carrying the analysis the acceptance criteria
require:

- **Decision summary.** One-sentence recommendation plus the chosen
  default. Expected direction (subject to the analysis below): keep the
  control-plane as root with explicit post-provision ownership repair to
  the fixed `1000:1000` agent UID/GID. This is the model that already
  ships, has just been hardened twice (`3964bed2`, `aa866959`), and is
  generic across Linux and Docker Desktop/macOS without per-host image
  rebuilds.
- **Pillar analysis.**
  - Docker socket: root inside the control-plane is the simplest path
    on both Linux (where socket group is host-specific) and Docker
    Desktop (where the socket is owned by `root:root` inside the VM
    bind-mount).
  - SSH/auth mounts: the host SSH-agent forwarder
    (`/run/host-services/ssh-auth.sock`) is owned by `root:0` on
    Docker Desktop; non-root containers need additional setup. The
    read-only host-home credential mounts work for both root and
    non-root, but per-workspace seeded auth directories
    (`work_dir/auth/<id>/...`) are chowned to the agent UID so the
    agent can read/write them — this works only if the control-plane
    can chown, i.e. is root.
  - Bind-mounted AWF state (`AWF_HOST_WORK_DIR`): created on first run
    by whichever process touches it. If the control-plane is root, the
    state directory ends up root-owned on Linux (gid varies); the
    operator running `awf service teardown` needs `sudo` to clean it
    up. Docker Desktop on macOS hides this behind userland VM mapping.
  - Linked worktree metadata: linked worktrees keep their git dir
    inside `mirror/.git/worktrees/<id>/`, which the existing
    `_linked_worktree_git_dir` helper resolves and chowns. The
    `worktrees/` admin dir must be writable by the agent for
    `git worktree remove`/`git commit` to succeed.
  - Linux vs macOS: Linux gives true UID-based file ownership; macOS
    Docker Desktop maps host files to the container with metadata
    quirks that broke the recursive chown twice — the regression
    coverage must account for both.
  - Cleanup permissions: with root control-plane, cleanup runs as
    root and can always remove worktrees; with host-UID control-plane,
    cleanup runs as the host user and can fail if a runaway agent
    process left root-owned files.
  - Migration path for existing root-owned state: documents the
    one-shot `chown -R` (or `awf service repair-state`) operators must
    run if they switch defaults later, and confirms the chosen default
    needs zero migration today.
- **Rejected option: control-plane as host UID/GID.** Captures the
  trade-offs (image rebuild per host, docker-group GID portability,
  SSH-agent socket on Docker Desktop, host-home read-only mount
  ergonomics) and the reason it loses for an open-source local Core
  default.
- **Operator override (optional).** If kept, document the
  `AWF_LOCAL_AGENT_UID` / `AWF_LOCAL_AGENT_GID` env variables and how
  they flow into `worker.py`. If dropped, explicitly say so and link to
  the issue that would re-open the discussion.
- **Test contract.** Names the new unit and integration tests as the
  evidence that the chosen default keeps `git status`/`git add`/`git
  commit` working in `/workspace`.

## Validation Commands

Targeted (run during TDD):

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py -q
uv run --python 3.12 --extra dev pytest tests/integration/test_workspace_agent_git_in_workspace.py -q
```

Broader (before commit):

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit/node -q
```

Full integration run (only when a Docker daemon is reachable; skipped
otherwise via the existing `AWF_SKIP_DOCKER_TESTS` pattern):

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_workspace_agent_git_in_workspace.py tests/integration/test_compose_manager_docker.py -q
```

## Risks And Assumptions

- **Test environment privilege.** The integration test cannot count on
  running as root; it must skip gracefully and document the skip when
  neither UID 0 nor UID 1000 is available. The unit test must drive the
  ownership preparation through monkeypatched `os.chown`/`os.geteuid`,
  consistent with the existing `test_prepares_linked_worktree_git_paths_for_agent_user`
  pattern.
- **Docker Desktop file ownership quirks.** Linux CI cannot reproduce
  the macOS Docker Desktop metadata-missing case that caused
  `aa866959`. The unit test reproduces it by raising `PermissionError`
  from a fake `os.chown` (already done in
  `test_agent_owner_repair_skips_unwritable_loose_object_files`). The
  strategy doc must call out that macOS-only regressions need to be
  caught by operator dogfooding plus the `AWF_SKIP_DOCKER_TESTS=0`
  contributor smoke.
- **`safe.directory` interaction.** The control-plane sets
  `safe.directory=*` via `_service_git_environment` so root-side git
  commands accept worker-owned worktrees. The agent-side commands run as
  UID 1000 inside the agent container; if the worktree files end up
  owned by a UID the agent cannot match, git emits "dubious ownership"
  even with chown, because the parent dirs may still be root. The new
  test must therefore verify the worktree's parent chain (mirror dir,
  worktrees admin dir, linked git dir) is correctly prepared.
- **Hardcoded `1000:1000`.** The agent-runtime image fixes the agent
  user at UID/GID 1000. If we ever support a non-1000 host UID via
  override, the image must be parameterized too. This plan defers that
  to a follow-up unless the strategy doc decides to ship the override
  now.
- **Docs do not block code.** If the integration test surfaces a code
  bug, the doc still ships in the same PR — the decision is "current
  model with the gap closed", not "current model assumed correct".
- **No GKE scope.** The plan stays scoped to local Core. GKE pod
  security context, fsGroup, runAsUser, and node-level cleanup are
  intentionally out of scope and are referenced only as forward
  pointers in the strategy doc.

## Non-Goals

- Do not change the agent-runtime image's `agent` user UID/GID away from
  1000 in this slice.
- Do not introduce a per-host image rebuild path.
- Do not change the Docker socket mount, SSH-agent socket mount, or the
  set of host-home credential mounts.
- Do not redesign `auth_mounts.py`, `secret_mounts.py`, cleanup
  semantics, or compose project naming.
- Do not introduce a Kubernetes/GKE pod security context discussion.
- Do not switch git branches, push, rebase, or run any AWF PR commands
  manually — AWF owns branch and PR lifecycle.
- Do not modify any file outside the configured plan artifact during
  this planning phase.
