---
name: awf-scheduler
description: |
  Operate AWF (Agent Workspace Fabric) correctly from an agent or the CLI —
  onboard a repo into a workspace profile, dispatch a coding task that opens a
  PR, adopt an existing PR into the autonomous review→fix→merge monitor, and
  diagnose failures. Read this before you drive AWF (create or adopt a
  workspace, write a task prompt, declare owned_paths for protected files) so
  you get a usable PR on the first try instead of the fifth. Repo-agnostic:
  applies whether the coding agent works on a Python backend, a Next.js
  frontend, a Rust CLI, etc.
---

# awf-scheduler

A **skill for invoking AWF correctly**. Read this before you create or adopt a
workspace, especially the first time in a new environment. AWF is powerful but
it's an infrastructure service — the failure modes are mostly at the edges
(service not started, profile mis-resolved, auth, protected-file scope, port
mapping, image choice). This skill captures those edges.

AWF itself is repo-agnostic. Where I need to mention a host project I use
neutral names like `app-backend` / `app-web` / `owner/repo`; nothing about AWF
is tied to any particular repository.

> **The one-line mental model** (from `docs/CONCEPTS.md`): *AWF owns the
> lifecycle and policy. The coding agent owns the code changes. **Workspace
> profiles own the project-specific setup.*** Almost everything you might be
> tempted to jam into a task spec actually belongs in a profile.

## 1 — What AWF does, in one paragraph

AWF turns a short spec (repo, task prompt, agent, base branch) plus a resolved
**workspace profile** into a live Docker stack: a fresh git worktree on a new
feature branch at `/workspace`, any sidecar services the profile declares
(database, app-under-test, Redis, …), and a coding CLI that executes the prompt
against that worktree. File edits are auto-committed; the profile's validation
phases run against the real stack; if they pass, AWF pushes the branch and opens
a PR against the base branch — then **keeps running** in a `monitoring_pr`
state, resolving review comments, fixing CI, and syncing the base until the PR
merges (feature flow) or is declared ready for a human (release flow). If any
step fails, the workspace is left in `failed` with a structured `failure_reason`
and the stack stays up for post-mortem.

## 2 — First: AWF is a persistent local service

AWF runs as a long-lived local service (API + worker + DB + web console). Before
you can create a workspace, the service must be up:

```bash
awf setup --dry-run          # read-only host readiness check (Docker, ports, auth)
awf start                    # brings up API + worker + Postgres + web console
awf service status --format pretty
```

`awf start` serves the API on `127.0.0.1:8000` and the operator console on
<http://127.0.0.1:3000>. Use `awf start --headless` to skip the console. From a
source checkout, prefix commands with
`uv run --python 3.12 --extra dev awf ...` (or `uv tool install . --force`
once to put `awf` on `PATH`).

The worker provisions and drives every workspace. If a monitor process dies, the
worker **resumes the persisted monitor row** on restart — recovery is built in.

## 3 — Onboard the target project: `awf init <path>` → `.awf/workspace.yml`

A workspace profile (`.awf/workspace.yml` in the target repo) is where
project-specific setup lives: which services to run, how to validate, what
network egress is allowed, which secrets to lease, the DB env. Generate one by
pointing AWF at a checked-out repo:

```bash
awf init <path-to-repo>            # inspects the repo, drafts/previews .awf/workspace.yml
awf init <path> --write-profile --yes
awf profile preview <path>         # see the resolved profile without writing
```

`awf init` inspects the repo **without** calling the API or launching a
workspace; it picks a template (`generic`, `python`, `node-nextjs`,
`docker-compose`, `python-postgres`, `node-playwright`, `multi-service`) and
reports gaps (missing services / secrets / ports / validation commands /
healthchecks). A real profile (this repo's own self-dogfood profile, abridged):

```yaml
awf:
  name: app-backend
  version: 1
  docker:
    mode: none                       # 'dind' if the project runs its own compose
  runtime:
    environment:                     # env injected into the agent container
      DATABASE_URL: "postgresql+asyncpg://app:${AWF_POSTGRES_PASSWORD}@postgres:5432/app"
  services:                          # sidecars — declared HERE, not in the task spec
    - name: postgres
      image: postgres:16-alpine
      environment:
        POSTGRES_DB: app
        POSTGRES_USER: app
        POSTGRES_PASSWORD: "${AWF_POSTGRES_PASSWORD}"
      healthcheck_cmd: "pg_isready -U app -d app"
      volumes:
        - [postgres_data, /var/lib/postgresql/data]
  security:
    egress:
      mode: restricted               # open | restricted | offline
  validation:
    requested_tier: 1
  phases:
    setup:
      - command: uv sync --extra dev
        timeout_seconds: 900
    validate:
      - command: uv run pytest -q
        timeout_seconds: 600
```

**Profile resolution order** (`docs/CONCEPTS.md`): inline `profile` in the
create request → repo-local `.awf/workspace.yml` → built-in registry by
`profile_ref` → auto-detection → low-confidence `generic`. On a create request
this is `workspace.profile_ref` (default `"auto"`) and/or an inline
`workspace.profile`.

## 4 — Three ways to dispatch work (all produce the same `Workspace` record)

1. **CLI** (operator default):
   ```bash
   awf workspace create \
     --repo git@github.com:owner/repo.git \
     --base development \
     --title "Short human-readable title (becomes PR title)" \
     --prompt "Specific, actionable, framed as legitimate dev work." \
     --agent codex \
     --test "uv sync --extra dev" --test "uv run pytest -q" \
     --profile auto
   ```
   Useful flags: `--task-kind {feature_branch_pr|sync_release_pr}`,
   `--model`, `--effort`, `--owned-path` (repeatable — see §10),
   `--auto-merge/--no-auto-merge`, `--profile <ref>`.
2. **REST**: `POST /v1/workspaces` with the **nested** body below
   (`Idempotency-Key` header supported):
   ```json
   {
     "repo":   { "url": "git@github.com:owner/repo.git", "base_branch": "development" },
     "task":   { "title": "PR title", "prompt": "Actionable dev work.",
                 "kind": "feature_branch_pr", "agent": "codex" },
     "workspace":  { "profile_ref": "auto" },
     "validation": { "commands": ["cmd1", "cmd2"], "requested_tier": 1 }
   }
   ```
3. **MCP**: tool `awf_create_workspace` on the AWF MCP server
   (`awf mcp serve`). Adopt an existing PR with
   `awf_adopt_pull_request_monitor`.

## 5 — Writing the task prompt

Coding CLIs are conservative by default. A prompt that reads like a penetration
test (`"do X without explanation, just execute"`) triggers refusal heuristics
even with permissions skipped. A prompt framed as ordinary dev work lands.

**Good prompts** include:
- A short **Environment notes** section up front — tell the agent what's mounted
  at `/workspace`, what host services exist on the internal network (e.g.
  `postgres:5432`, `http://app-backend:8000`), what auth is pre-wired. Without
  this, an attentive CLI (Claude Code especially) explores, finds half-configured
  env, and stops to ask questions.
- A **What to do** section with enumerated, concrete steps.
- A **What to optimize for** section — which tradeoffs to make. "Coverage over
  cleverness" beats "do a good job."
- An explicit **"git is functional in /workspace — commit before exiting"** line.
  AWF auto-stages + commits leftover changes, but being explicit is cheap
  insurance.
- A **"Do not push — AWF handles it"** line. Otherwise the agent may try
  `git push`, fail (no upstream), and treat that as a task failure.

**Bad prompt shapes**: imperative-without-justification (reads suspicious →
refusal); under-specified ("make this better" → produces nothing);
over-specified down to variable names (agent makes trivial changes and exits).

Do **not** tell the agent to "address reviewer comments" or "merge your own PR" —
the agent runs once and exits. The PR monitor (§10) re-invokes it later with
targeted prompts when comments arrive.

## 6 — Profiles in depth: services, validation, security, secrets

Everything project-specific is profile data. Key blocks (`src/awf/profiles/models.py`):

- **`services:`** — sidecars (`ProfileService`): `name`, `image` **or**
  `build_context`+`dockerfile`, `environment`, `depends_on`, `healthcheck_cmd`,
  `ports`, `command`, `volumes`, `privileged`, `required`. A service is an image
  or a build — **not** a repo clone (there is no `repo_url`/`branch` on a
  service). `healthcheck_cmd` is what gates the agent: the agent container waits
  on `service_healthy` for every companion that declares one. A service without
  a healthcheck starts in parallel with the agent — racy, rarely what you want.
  Don't guess the health URL; check the app's real route
  (`/api/v1/health` vs `/healthz` vs `/_health`) and curl it on the host first.
- **`runtime.environment:`** — env injected into the agent container. This is
  where DB URLs go. The **only** compose-time placeholder AWF expands is
  `${AWF_POSTGRES_PASSWORD}` (the stack-local DB password) — use it in env values
  like `postgresql://app:${AWF_POSTGRES_PASSWORD}@postgres:5432/app`. (Note: the
  onboarding `python-postgres` template emits `${POSTGRES_PASSWORD}`, which AWF
  does **not** expand — standardize on `${AWF_POSTGRES_PASSWORD}`.)
- **`phases:`** — `setup` (deps, migrations, hooks) and `validate` (the gate)
  command lists, each with `timeout_seconds`. The validation gate lives here; CLI
  `--test` and request `validation.commands` feed the same `validate` list.
- **`security.egress.mode:`** — `open` | `restricted` | `offline`. Onboarding
  defaults to `restricted`; set `open` only for trusted single-owner repos and
  write an `open_explanation`.
- **`secrets:`** — declarative secret leases (`kind: mount|env`, `provider`,
  `ref`). The safe way to inject extra credentials (env → `${VAR}` placeholders,
  ro file/auth mounts, e.g. Bitbucket git-over-HTTPS askpass) — prefer this over
  hardcoding anything in `environment`.

**Postgres is not an AWF default** — declare it as a service if your project
needs it. Most templates use `postgres:16-alpine` mounted at
`/var/lib/postgresql/data`; the built-in `aira` compatibility profile uses
`pgvector/pgvector:pg18` at `/var/lib/postgresql` (pg18 changed the data-dir
convention — pg16 wants `/var/lib/postgresql/data`, pg18 wants
`/var/lib/postgresql`, and the wrong one makes the container exit(1)).

## 7 — Auth: AWF resolves it; you don't declare it

You do **not** pass auth bind mounts — AWF resolves and prepares auth
per-workspace (`src/awf/node/auth_mounts.py`):

- **rw provider auth** (codex, claude, gemini, opencode, grok, ollama) is
  **seeded from your host into per-workspace isolated dirs** under
  `${AWF_HOST_WORK_DIR:-~/.awf/service}/auth/<workspace_id>/<tool>/` — not bind-
  mounted live from `~`. (Claude uses an overlayfs scheme plus the single-file
  `/home/agent/.claude.json` mount it needs to find its own config on token
  refresh.)
- **ro auth**: `~/.config/gh`, `~/.config/gcloud`, `~/.gitconfig`, `~/.ssh`, plus
  `GOOGLE_APPLICATION_CREDENTIALS` if set. Cursor is env-key-only (no mount).
- For anything non-default, use **profile secret leases** (§6), not raw mounts.

The container user is `agent` (UID 1000); AWF chowns the per-workspace auth dirs
to the configured workspace-owner uid/gid. The prerequisite is simply that
**the host operator is already logged into** the coding CLI and `gh` —
AWF inherits whatever those are authenticated as.

## 8 — Validation commands: ordering, venv, env assumptions

- **First command MUST be dependency install** (`uv sync --extra dev`, `npm ci`,
  …). Assume nothing is preinstalled beyond the base agent runtime.
- **Venv auto-activation**: AWF sources `/workspace/.venv/bin/activate` before
  each validation command (and any migration) when present. This is why
  `uv sync` / `python -m venv` in step 1 makes `pytest`/`ruff`/`alembic` in later
  steps see the right site-packages — no `ModuleNotFoundError: No module named
  '<app>'`. Write commands naturally.
- **DB env is profile-dependent.** AWF does **not** universally alias a DB var.
  The `aira` profile sets `DATABASE_URL` + `AIRA_DATABASE_URL`; a generic profile
  sets nothing. If your Alembic `env.py` reads a custom var, declare it in
  `runtime.environment`.
- Commands run inside the agent container via
  `docker compose exec -T -w /workspace agent sh -lc <command>`. Pipes, `&&`,
  `$VAR` work; the first failing command stops the sequence.
- **Agent runtime ships**: Python 3.12, Node 22, git, jq, ripgrep, tini, Docker
  CLI + Compose + Buildx, GitHub CLI, alembic/pytest/uv, **six coding CLIs**
  (Codex, Claude Code, Cursor, Gemini, OpenCode, Grok), and the system libs
  Playwright's chromium needs. The **browser binaries are NOT pre-installed** —
  add `npx playwright install chromium` as a validation command (~30 s). Do
  **NOT** use `--with-deps`: it tries to `su root` + `apt install`, and the
  container runs as unprivileged `agent` with no root password (`su:
  Authentication failure`).

## 9 — Model selection

By default, **let each CLI read its own config**. Do NOT hardcode model names
unless you know the CLI supports that exact model under the host's account tier.
Real example: `--model gpt-5.1` to `codex exec` fails with *"the 'gpt-5.1' model
is not supported when using Codex with a ChatGPT account"* — that model is
API-tier only. Override per-workspace with `--model` / `--effort` only when you
explicitly need to A/B.

## 10 — PR monitor, adoption, and owned_paths

Once a PR opens, the workspace enters `monitoring_pr` and stays there until the
PR is **merged** (feature flow) or **declared ready for a human** (release flow).
Monitoring is automatic — nothing in the task spec opts in.

### Adopt an existing PR

To attach the monitor to a PR that already exists (no re-running of the coding
agent on the initial diff):

```bash
awf workspace adopt-pr \
  --pr-url https://github.com/owner/repo/pull/123 \
  --agent claude_code --model claude-opus-4-8 --effort high \
  --auto-merge \
  --owned-path '.github/workflows/**' --owned-path 'pyproject.toml'
```

Selector is **exclusive**: exactly one of `--pr-url` OR (`--repo` + `--pr`).
Also available as `POST /v1/workspaces/adopt-pr` and MCP
`awf_adopt_pull_request_monitor`. Adoption is idempotent on (repo, PR); changing
policy on a live adoption returns `PR_ADOPTION_POLICY_CONFLICT`.

### owned_paths / protected files — do not skip this

AWF protects build/test/coverage/CI-policy files. Protected examples:
`pyproject.toml`, `.github/workflows/**`, `.awf/workspace.yml`
(`docs/PROTECTED_FILES.md`). Without declared ownership, the monitor's agent may
make only a narrow allowlist of deterministic edits (add a dependency, bump a
pinned `uses:` ref); editing `[tool.pytest.*]`/`[tool.coverage.*]`, lowering
`fail_under`, adding `continue-on-error`, or removing jobs is **blocked**.

If the agent edits a protected file **without** ownership, AWF performs a
**transactional rollback before push** (`monitor.protected_scope_transactional_
rollback`, `rollback_strategy: git_reset_hard_to_operation_start`, `pushed:
False`) — the whole fix is reverted. At merge time, changes outside declared
`owned_paths` produce `NotifyHuman("OUT_OF_SCOPE_CHANGE …")`. **The fix: declare
each path the review/CI repair is expected to touch via repeatable
`--owned-path`** when you create or adopt. (This is the single most common
self-inflicted monitor failure — a clean adopt-pr that surprise-rolls-back its
own work almost always means a missing `--owned-path`.)

### What the prompt does NOT need to cover — the monitor owns the gates

The monitor drives ~10 ordered gates; the five that matter to a prompt author:
inline (diff) comments resolved, review-level comments resolved, CI green (every
required check SUCCESS/SKIPPED), no merge conflicts, and base branch merged into
the feature branch before merge.

### Per-comment verdict grammar the CLI produces

When the monitor hands a thread to the CLI, it expects one of these markers
(canonical form is the `AWF-VERDICT:` prefix):

| Marker | Meaning | What AWF does |
|---|---|---|
| `AWF-VERDICT: FIXED: <summary>` (or any output without a recognized marker) | CLI committed the fix locally | pushes after the burst settles, then resolves the thread |
| `AWF-VERDICT: FALSE POSITIVE: <reason>` | CLI disagrees, replies inline | resolves with the reply posted |
| `AWF-VERDICT: DEFER: <what to track>` | needs follow-up, not blocking | captures a tracking note, resolves |
| `AWF-VERDICT: NEEDS_HUMAN: <what you need>` | CLI cannot proceed safely | **blocks merge, notifies a human** (also the sink for empty/garbled output) — respond via `awf workspace guide` (§10, "Responding to a human escalation") |

The CLI also posts the reply on GitHub itself; AWF's resolve happens after the
reply is visible.

### Commit-then-push-on-settle

The monitor does NOT push after every fix. A reviewer bot drops 5–20 inline
comments in one burst; the monitor addresses them all locally, waits a ~30 s
settle window for more, and pushes once quiet. One push → one CI run.

### Release (`development → main`) vs feature

The `--auto-merge`/`--no-auto-merge` flag selects the monitor: `--auto-merge`
(default) → feature monitor (squash-merges + deletes branch on green);
`--no-auto-merge` (or `--task-kind sync_release_pr`) → release/manual monitor
that **never calls `gh pr merge`** — when all gates are green it posts
`✅ PR #N is ready to merge at commit <sha>` and notifies a human. Dev→main PRs
must never be auto-merged.

### Recovery & directives

- `awf workspace remonitor <ws_id> --reason ...` requests monitor recovery for a
  `monitoring_pr` workspace (or `POST /v1/workspaces/{id}/remonitor`).
- To inject an instruction into a **live** monitor, use the purpose-named
  directive channel `awf workspace guide <ws_id> --directive "..."` rather than
  cancel+re-adopt.
- **Branch-protection fallback**: if `gh pr merge` is rejected (token perms,
  required reviews), the monitor posts the ready-to-merge notification, **stays
  in `monitoring_pr`, and keeps re-polling** until a human merges/closes — it
  does not silently exit.

#### Responding to a human escalation

When the monitor emits `NEEDS_HUMAN` it posts a `⚠️ PR #N needs human attention …`
comment and **stops auto-merging** — typically a `NotifyHuman` the CLI raised, an
`OUT_OF_SCOPE_CHANGE`, or a security/policy decision the agent will not make on its
own. Respond, do not restart:

1. **Read it** — the `needs human attention` comment plus the cited review thread say
   exactly what decision or input it needs.
2. **Decide** — make the scope/security/policy call (or supply the missing answer).
3. **Inject it** — `awf workspace guide <ws_id> --directive "<instruction>"` (the
   directive is capped at 1024 chars; keep it tight). It reaches the agent on its next
   monitor cycle; the agent acts on it, resolves the thread, and resumes toward merge.
   Do **NOT** cancel+re-adopt, and do **NOT** push the fix from the worktree yourself
   (§11) — `guide` is the sanctioned channel.

`guide --directive` is also the lever for a review that **will not converge** (each fix
drawing one more incremental bot comment over many commits): direct the monitor to
clear low-value/incremental comments via `DEFER`/`FALSE POSITIVE` *replies* instead
of code edits (every edit triggers another CI + re-review cycle), and to defer anything
needing files outside `owned_paths` — so it stops looping and merges on green CI. Mind
which reply actually unblocks merge: `DEFER` breaks the loop only for **bot** comments
and for inline threads the monitor can auto-resolve (it files a tracking issue, posts an
explanatory comment, and resolves the thread). A `DEFER` on a **human-authored
review-level comment** has no thread to auto-resolve and still routes to `NotifyHuman`,
blocking auto-merge — there, use `FALSE POSITIVE` (only when it genuinely is one; never
to dismiss valid feedback) as the non-blocking reply, or answer the comment. `NEEDS_HUMAN`
always blocks, regardless of author.

> Operational note: pushing directly to a branch the monitor is actively pushing
> to (e.g. landing a small fix on `development` while a `development→main`
> monitor runs) causes one benign non-fast-forward rejection
> (`monitor.push_rejected_resyncing_local`); the monitor resyncs onto your commit
> and continues. Safe, but expect that one log line.

## 11 — When validation fails

**Do NOT manually intervene** — do NOT `git push` from the worktree, do NOT
`gh pr create` yourself. The value of AWF is autonomous PR production; a manual
shortcut defeats the test. If the root cause is a bug in AWF itself, **fix AWF
first and re-run** — a one-off bypass will bite every future user.

Diagnose **CLI-first** (these beat raw `docker` poking):

```bash
awf workspace show <id>          # status + failure_reason + failure_message
awf workspace events <id>        # lifecycle timeline
awf workspace logs <id>          # all streams; or: awf workspace log <id> <stream>
awf workspace operations <id>    # per-operation history
awf service logs --service worker   # service-level (api|worker|migrate|postgres)
```

`failure_reason` vocabulary: `agent_failure`, `validation_failure`,
`infrastructure_failure`, `policy_failure`, `cleanup_failure`,
`profile_resolution_failure`, `service_startup_failure`, `phase_timeout`,
`health_check_failure` (detail codes in `docs/REASON_CATALOG.md`). Low-level
fallback: `docker logs awf-<workspace_id>-<service>` and
`docker exec -it awf-<workspace_id>-agent bash`.

Common failure modes + fixes:

| Symptom | Root cause | Fix |
|---|---|---|
| Monitor fix-commit silently rolled back (`protected_scope_transactional_rollback`) | Agent edited a protected file without ownership | Re-create/adopt with `--owned-path` for that path (§10) |
| `OUT_OF_SCOPE_CHANGE` NotifyHuman at merge | Change landed outside declared `owned_paths` | Declare the path, or make the operator scope decision |
| `alembic upgrade head` → `ModuleNotFoundError: <app>` | Migration ran before deps installed, or used system python | Put dep-install as `phases.setup[0]`; AWF auto-activates `/workspace/.venv` |
| Companion never `service_healthy` | Wrong health URL | Curl the real service's health route on the host; fix `healthcheck_cmd` |
| `gh pr create: No commits between X and Y` | Agent made no changes (or didn't commit) | Widen prompt scope; check `git log base..HEAD` in the worktree |
| `failed to parse compose.yml` | Hand-rolled compose with mixed quoting | The template uses `tojson`; declare via the profile, don't hand-roll |
| CLI crashes "Read-only file system" | A provider auth dir landed ro | Provider auth (codex/claude/gemini/…) must be writable; AWF handles this — if you see it, you're on a custom mount |

## 12 — Cleanup

A **failed** workspace is left up on purpose — stack running, `failure_reason`
on the row — so you can post-mortem; AWF does not auto-clean it. A workspace
that reaches a terminal state *cleanly* (merged / released / cancelled) has its
runtime reclaimed automatically (next bullet).

- **Per-ws runtime + auth overlays auto-reclaim, worker-side.** The biggest disk
  consumer is each workspace's provider-auth overlay (`auth/<id>/…`, up to
  ~1–2 GB). On a clean terminal transition the worker eagerly tears the runtime
  down — `compose down` of the agent+postgres containers and the `-net`
  network, then unmount + remove the auth overlay, emitting
  `terminal_runtime_released` (#583/#584) — with a periodic scan as backstop.
  Unmounting the overlay needs `CAP_SYS_ADMIN`, which **only the worker holds**,
  so this reclaim always runs in the worker — never a host `rm` or a synchronous
  API call. Failed-workspace auth dirs are deliberately preserved.
- **On-demand bulk reclaim — `awf service gc`** (or `POST /v1/service/gc`).
  Dry-run by default (plans, deletes nothing); `--execute` to act; filters
  `--status`, `--min-age-hours N`, `--limit`. The API container can't unmount the
  capability-gated paths itself, so on `--execute` it **delegates the per-ws auth
  overlays + `_shared/claude-base` reclaim to the worker** (the only
  `CAP_SYS_ADMIN` context), waits for it, and folds the worker's real reclamation
  into the response (#582/#590). So `gc --execute` **is** the on-demand way to
  reclaim auth-dir disk under pressure — it routes those dirs through the worker
  rather than skipping them. (It does not delete workspace DB rows — use
  `DELETE /v1/workspaces/{id}` for a single record.)
- **Single workspace:** `DELETE /v1/workspaces/{id}`.
- **Manual fallback (stack only):**
  `docker compose --project-name awf_<ws_id> down -v`. Find stacks by label, not
  name prefix: `docker ps -a --filter 'label=com.docker.compose.project=awf_<ws_id>'`.
  Volumes are `awf-<ws_id>-*`, network `awf-<ws_id>-net`; per-ws state lives
  under `${AWF_HOST_WORK_DIR:-~/.awf/service}/`.

## 13 — Anti-patterns

1. **Manual `git push` / `gh pr create` to "rescue" a failed run.** Fix AWF
   instead; autonomous PR production is the whole point.
2. **Adopting/creating with monitor work expected on protected files but no
   `--owned-path`.** Guarantees a transactional rollback (§10).
3. **Hardcoding secrets in `environment`.** Use profile `secrets:` leases or
   `${AWF_POSTGRES_PASSWORD}`. Task-spec secrets land in the workspace DB row.
4. **Reusing one spec for many runs without an idempotency-key.** Each call makes
   a new workspace; with a key, replays return the existing one.
5. **Hardcoding model names.** The CLI's config already has the right one for the
   host's account tier.
6. **Running parallel tasks against the same repo with overlapping file scopes.**
   Workspaces are isolated but the PRs collide at merge. Use `awf locks` to see
   overlap risk; scope non-overlapping or run sequentially.
7. **Treating re-review as redundant.** The feature→dev and dev→main reviews
   catch different corner cases; let the monitor run as many cycles as the
   reviewers want.

## 14 — Minimal end-to-end proof

First, the built-in smoke (no real PR, validates the whole pipeline — service +
auth/provider readiness, profile, validation commands, request shape, PR/monitor
path, all mocked):

```bash
awf smoke run --project <path> --mocked-local --format pretty
```

If that's green, dispatch a real one-liner against a repo you can push to:

```bash
awf workspace create \
  --repo git@github.com:you/repo-with-readme.git \
  --base main \
  --title "docs: add AWF verification note" \
  --prompt "Append one line to README.md: '> Touched by AWF at <ISO-timestamp>' (current UTC). Commit message 'docs(awf): pipeline verification'. Do not touch other files; do not push — AWF handles push." \
  --agent codex \
  --test "grep -q 'Touched by AWF at' README.md"
```

Then poll `awf workspace show <id>` until it reaches `monitoring_pr` with a PR
URL. If the smoke doesn't pass, AWF is broken in your environment — don't try a
real task until it does.
