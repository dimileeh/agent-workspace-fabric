# Plan — Issue #361 (WS-A): kill the 178 GB per-workspace `~/.claude` copy

## Diagnosis (goes in the PR description)

`du -sh ~/.claude/*` on this machine (excluding the historical dirs already
excluded by `_CLAUDE_USAGE_HISTORY_DIRS`):

| path | size |
|------|------|
| `skills/` | **1.6 GB** |
| `file-history/` | 47 MB |
| `plugins/` | 38 MB |
| `session-env/` | 11 MB |
| everything else (config, caches, settings, history.jsonl, …) | < 2 MB total |
| **total `~/.claude`** | **1.7 GB** |

`skills/` is **94 %** of the copy and is exactly the read-only content the
in-workspace Claude Code agent invokes. Together with `plugins/` and the small
config/settings files it is static, read-only, identical across workspaces →
belongs in the **shared read-only base (lowerdir)**. The agent's runtime writes
(history, caches, file-history, anything it mutates) belong in the small
**per-workspace writable upper**. The `projects/todos/shell-snapshots/statsig`
dirs stay excluded from the base exactly as today (ccusage attribution).

Across ~100 workspaces the per-workspace full copy is ~178 GB; a single shared
base + tiny upper per workspace drops that to ~1.7 GB total + a few MB/upper.

## Locked design (do not re-litigate)

Replace the per-workspace `shutil.copytree` of `~/.claude` with:

- **lowerdir** — one shared, read-only base snapshot of `~/.claude` at
  `work_dir/auth/_shared/claude-base/.claude`, built/refreshed once on the host
  (same exclusions). Lives *outside* any `auth/<workspace_id>` dir so GC never
  reaps it (GC enumerates candidates from DB workspace rows and only deletes
  `work_dir/auth/<workspace_id>`; `_shared` is never a candidate).
- **upperdir** — `work_dir/auth/<workspace_id>/claude/upper` (writable, chowned
  uid/gid 1000).
- **overlay workdir** — `work_dir/auth/<workspace_id>/claude/work` (overlay's
  own scratch; chowned 1000).
- **merged** — `work_dir/auth/<workspace_id>/claude/merged`, the overlay
  mountpoint, bind-mounted into the agent container at `/home/agent/.claude`
  (rw), exactly via the existing `AuthMount(source=merged, target=..., mode=rw)`.
- `~/.claude.json` stays a tiny per-workspace file copy — unchanged.

Control-plane (root worker) sets up and tears down the mount; agent stays
unprivileged; upper owned by 1000.

## Intended files / modules to touch

1. **`src/awf/node/auth_mounts.py`** — core change.
   - Add a module-level injectable mount seam (so unit tests don't need root):
     a small `OverlayMounter` protocol / default impl wrapping
     `subprocess.run(["mount","-t","overlay",...])` and `umount`. Default wired
     in `ServiceAuthMountResolver`; tests inject a fake.
   - `_prepare_isolated_claude_auth(...)` gains an overlay branch:
     - ensure/refresh the shared base via `_ensure_shared_claude_base(host_home,
       work_dir)` — copytree once with the existing
       `_CLAUDE_USAGE_HISTORY_DIRS` exclusions and `ignore_dangling_symlinks`;
       guarded so concurrent provisions don't double-build (build into a temp
       dir, atomic `os.replace`; a simple version/marker file controls refresh).
     - create `upper/`, `work/`, `merged/`; chown `upper`+`work` to 1000.
     - attempt the overlay mount; on success return
       `AuthMount(source=merged, target=_CLAUDE_DIR_TARGET, mode="rw")`.
       **Do not** chown the merged tree (the `_chown_workspace_auth_sources`
       walk must skip it — chowning through a live overlay would write into the
       shared lower's inodes; only `upper`/`work` get chowned, done explicitly
       in the overlay branch, and the merged mount is excluded from the generic
       rw-chown walk).
     - on overlay-unavailable (see fallback), fall back to the **current**
       full-copy behavior into `auth/<id>/claude/.claude` and return that mount
       unchanged (so the generic chown still applies to the copy).
   - Add `teardown_workspace_auth_overlay(work_dir, workspace_id)`:
     unmount-before-remove. Idempotent: only `umount` paths that are current
     mountpoints (`os.path.ismount`), swallow "not mounted", raise/log only on a
     real busy/error. Does **not** rmtree (GC owns removal); it only guarantees
     the mount is gone so GC's `shutil.rmtree(auth/<id>)` can't hit `EBUSY`.
   - Fallback detection: attempt the mount and catch the **specific** failures —
     `PermissionError`/`OSError` (no `CAP_SYS_ADMIN`), overlay not in
     `/proc/filesystems`, `mount` non-zero exit — never bare `except Exception`.
     Clean up the half-built `merged`/`work` dirs on fallback. Log one
     structured line with a reason code (e.g. `CLAUDE_AUTH_OVERLAY_UNAVAILABLE`)
     — no secrets.
   - Add a tiny capability/label helper `claude_auth_isolation_label(...)` →
     `"per_workspace_overlay"` when overlay is usable on this host, else
     `"per_workspace_copy"`, reused by provider_readiness.

2. **`src/awf/cli/common.py`** — `_run_terminal_workspace_compose_teardown`
   (the GC-time compose-teardown callback, which runs *before* GC's
   `_delete_gc_path` rmtree of `auth/<id>`). After `docker compose down`
   succeeds (or is a no-op), call `teardown_workspace_auth_overlay(work_dir,
   workspace_id)` so the overlay is unmounted before the auth dir is removed.
   This is the allowed unmount-before-remove seam — **gc.py is not touched.**
   Needs `work_dir` from `resolve_service_settings()` (already used by the
   sibling worktree-remove callback).

3. **`src/awf/service/provider_readiness.py`** — `_check_claude`: report the new
   isolation posture. Where it currently hardcodes
   `isolation="per_workspace_copy"` / `credential_scope="isolated_workspace"`
   for file auth, use `claude_auth_isolation_label(...)` so the readiness report
   reads `per_workspace_overlay` when overlay is available, `per_workspace_copy`
   on fallback. `isolated_workspace` credential_scope is unchanged (still
   isolated). Only the Claude file-auth branch changes.

4. **`docker/compose/local-service.yml`** — REQUIRED for the fix to actually
   take effect (without it the overlay path always falls back to copy and 0 GB
   is saved). The `worker` service needs:
   - `cap_add: [SYS_ADMIN]` so the root worker can call `mount(2)`.
   - the `${AWF_HOST_WORK_DIR}` bind given `:rshared` propagation so an overlay
     the worker mounts under it is visible to the sibling agent container the
     host daemon launches (default `rprivate` would leave the agent's bind
     empty). Host side must be a shared mount; documented in the PR.
   This is a worker security-posture change (flagged in the PR; provider
   readiness already surfaces isolation downgrades). The api service does not
   provision, so it is left unchanged. **Decision point for review:** if
   reviewers prefer to keep this PR to pure mechanism, ship 1–3 (fallback keeps
   everything correct) and land the compose enablement as an immediate
   follow-up; the code is written so flipping caps/propagation is the only
   switch needed.

   The **per-workspace** compose template
   (`docker/compose/workspace.base.yml.j2`) needs **no change** — it already
   renders `auth_mounts` as `source:target:mode`, and `source` is just the
   merged path.

## Tests to write first (TDD), all in existing `tests/unit/...` layout

`tests/unit/node/test_service_auth_mounts.py` (extend) +
`tests/unit/node/test_claude_auth_overlay.py` (new, focused) — inject a fake
mounter so no root is needed:

1. **Overlay happy path:** with a fake mounter that "succeeds", the claude mount
   `source` == `auth/<id>/claude/merged`, `mode == "rw"`; the mount command uses
   `lowerdir=<shared base>`, `upperdir=auth/<id>/claude/upper`,
   `workdir=auth/<id>/claude/work`.
2. **Shared base built once / reused:** two workspaces resolve → base copytree
   runs once; second workspace's mount lowerdir is the same shared path; base is
   not re-copied (assert via a copy-counter / mtime).
3. **Base content:** base contains `skills/…` and `settings.json`; excludes
   `projects/todos/shell-snapshots/statsig`; dangling skill symlinks skipped
   (port the existing `test_..._skip_dangling_claude_skill_links` expectation).
4. **Isolation guarantee:** upper(A) ≠ upper(B); both share one read-only base;
   the shared base dir is never written/chowned during resolution (assert no
   writes land under `_shared` after the first build and that A's upper path is
   never B's). The chown walk touches only `upper`/`work`, never `merged` or the
   shared base (assert recorded chown paths).
5. **Fallback:** fake mounter raises `PermissionError` (and a second case:
   overlay absent) → resolver returns the legacy copied `.claude` mount
   (`source == auth/<id>/claude/.claude`, contents copied, history dirs
   excluded), logs the reason code, leaves no stray `merged` mountpoint.
6. **Teardown unmount-before-remove:** `teardown_workspace_auth_overlay`
   `umount`s `merged` when `os.path.ismount` is true; is a no-op when not
   mounted; idempotent across two calls; raises/logs (not silently) on a real
   umount error.
7. **GC-safety location:** assert the shared base path is *not* under any
   `auth/<workspace_id>` dir (so GC candidate enumeration can never target it).

`tests/unit/cli/...` (the file covering `common.py` callbacks): assert
`_run_terminal_workspace_compose_teardown` invokes the overlay unmount with the
right `(work_dir, workspace_id)` after a successful compose down, and still
returns the existing result shape (compose-down success/failure unchanged).

`tests/unit/service/test_provider_readiness_parts/…`: claude file-auth readiness
reports `isolation == "per_workspace_overlay"` when overlay is available and
`per_workspace_copy` on fallback; existing non-claude assertions unchanged.

## Validation commands (focused — full suite/coverage owned by AWF+CI)

```bash
uv run --python 3.12 --extra dev ruff check src/awf/node/auth_mounts.py \
  src/awf/cli/common.py src/awf/service/provider_readiness.py tests/unit/node \
  tests/unit/cli tests/unit/service/test_provider_readiness_parts
uv run --python 3.12 --extra dev ruff format --check <same paths>
uv run --python 3.12 --extra dev mypy          # pyproject pins files = ["src/"]
uv run --python 3.12 --extra dev pytest -q tests/unit/node/test_service_auth_mounts.py \
  tests/unit/node/test_claude_auth_overlay.py \
  tests/unit/cli tests/unit/service/test_provider_readiness_parts
```

Per the AWF workspace contract, the full `pytest --cov` 99% gate, whole-repo
suite, and OpenAPI drift gate run under AWF/GitHub CI after the agent finishes;
I will reason about coverage of the new overlay/fallback/teardown branches as I
write them rather than running the broad gate here.

## Risks & assumptions

- **Mount visibility across containers (highest risk).** The worker mounts the
  overlay in its own mount namespace; the host daemon resolves the agent
  container's bind source on the host. Without `:rshared` on the work_dir bind
  the agent sees an empty `/home/agent/.claude`. Mitigations: (a) compose change
  #4; (b) the resolver only commits to the overlay mount after the mount call
  succeeds, and the fallback keeps provisioning correct everywhere the
  propagation/caps aren't present. Documented as a known prerequisite.
- **`CAP_SYS_ADMIN` requirement / security posture.** Granting it to the worker
  is a least-privilege downgrade; flagged in the PR and reflected in
  provider_readiness isolation labelling.
- **Unit tests cannot really mount overlayfs** (no root/caps in CI). Covered by
  injecting a mount seam and asserting command/layout/chown/fallback/teardown
  behavior; true kernel overlay semantics are validated operationally + by the
  fallback safety net, noted explicitly so this isn't mistaken for full e2e.
- **Concurrent base build** (multiple provisions racing the first build):
  handled by build-to-temp + atomic `os.replace` and a marker file; assumption
  is that an already-present, non-stale base is reused as-is.
- **GC unmount only fires through the wired compose-teardown callback.** GC
  invocations that don't wire `compose_teardown` would not unmount; mitigated
  because (a) that callback is the standard `service gc` wiring and runs before
  rmtree, and (b) `teardown_workspace_auth_overlay` is idempotent so it's safe
  to also call from a destroy path later. Not expanding into gc.py /
  lifecycle.py keeps WS-A scoped.
- Assumes `host_home`/`work_dir` plumbing in `worker.py` and `cli/common.py`
  (`resolve_service_settings`) is unchanged; only the resolver internals and the
  teardown callback gain behavior.

## Explicit non-goals

- **Not** touching `src/awf/service/gc.py` or `src/awf/node/git_manager.py`
  (WS-B1/B2/B3). **Update:** `src/awf/runtime/pr_monitor_runner/lifecycle.py`
  was also a declared non-goal but was later modified (commit `f103db47c`) to
  address review comment `PRRT_kwDOSJAM6s6Gyua_` — the PR-monitor's own GC path
  must unmount the Claude auth overlay before `rmtree` or it strands the merged
  mount (EBUSY). The change is additive and regression-tested; coordinate with
  WS-B before merge as this file is shared with that workstream.
- **Not** changing codex/gemini/opencode/ollama auth (still per-workspace copies
  — they are small; out of scope for the 178 GB driver).
- **Not** changing the per-workspace compose Jinja template (no change needed).
- **Not** introducing a new generic overlay abstraction beyond what `~/.claude`
  needs; keep it local to `auth_mounts.py`.
- **Not** altering ccusage / usage-history exclusion semantics.
- **Not** building a host-side systemd/global base refresher; base build stays
  lazy inside the resolver.
