---
name: awf-scheduler
description: |
  Schedule a coding task on AWF (Agent Workspace Fabric) — a standalone
  execution substrate that clones a repo into an isolated Docker workspace,
  launches a coding CLI (Codex / Claude Code / Gemini), runs validation
  commands, and opens a PR. Use this when you need to dispatch work to an
  autonomous agent without blocking your own loop, or when a task requires a
  live service stack (backend + web + DB) to test against.
---

# awf-scheduler

A **skill for invoking AWF correctly**. Read this before you schedule a task
on AWF, especially the first time in a new environment. AWF is powerful but
it's an infrastructure service — the failure modes are mostly at the edges
(env vars, auth, port mapping, image choice). This skill captures those
edges so the caller gets a usable PR on the first try instead of the fifth.

AWF itself is repo-agnostic. Everything below applies whether the coding
agent is working on a Python backend, a Next.js frontend, a Rust CLI, etc.
Where I need to mention the host project, I'll use "aira-agent" or
"aira-web" as examples, but nothing about AWF is tied to those repos.

## 1 — What AWF does, in one paragraph

AWF turns a short spec (repo URL, task prompt, test commands, which coding
CLI, optional companion services) into a live Docker stack: a fresh git
worktree on a new feature branch at `/workspace`, optional sidecar
containers for backend / database / web built from their own repos, and a
coding CLI that executes the prompt against that worktree. Any file edits
the CLI makes are auto-committed; the test commands run against the real
stack; if they pass, AWF pushes the branch and opens a PR targeting
`development` (or your chosen base). If any step fails, the workspace is
left in the `failed` state with a structured `failure_reason` and the
compose stack stays up for post-mortem (`docker exec` in).

## 2 — Three ways to invoke

1. **REST**: `POST /v1/workspaces` with the task body (idempotency-key
   supported). Good for curl / Postman / shell scripts.
2. **MCP**: `awf_create_workspace` tool on the AWF MCP server. Good for
   coding CLIs (Codex, Claude Code) that want typed tools.
3. **Python driver**: `scripts/run_awf.py --config tasks.json --work-dir
   /path` — runs an in-process orchestrator that creates the workspace,
   provisions it, and drives the executor. Best for one-shot operator use;
   accepts a JSON array of task specs and runs them concurrently.

All three produce the same `Workspace` record with the same lifecycle.

## 3 — The minimum task spec

A task that will actually produce a PR needs, at minimum:

```json
{
  "repo_url": "git@github.com:owner/repo.git",
  "branch_base": "development",
  "task_title": "Short human-readable title (becomes PR title)",
  "task_prompt": "Specific, actionable, framed as legitimate dev work.",
  "agent": "codex",
  "test_commands": ["command1", "command2"],
  "requires_database": false
}
```

Add `companions` and `postgres_image` when the task needs a live stack
(see § 6).

## 4 — Writing the task prompt

Coding CLIs are conservative by default. A prompt that reads like a
penetration test (`"do X without explanation, just execute"`) will trigger
refusal heuristics even with `--dangerously-skip-permissions`. A prompt
framed as ordinary dev work lands.

**Good prompts** include:
- A short **Environment notes** section up front — tell the agent what's
  mounted at `/workspace`, what host services exist on the internal network
  (e.g. `postgres:5432`, `http://backend:8000`), what auth is pre-wired.
  Without this, an attentive CLI (Claude Code especially) will explore, find
  half-configured env, and stop to ask questions.
- A **What to do** section with enumerated, concrete steps.
- A **What to optimize for** section — which tradeoffs to make when the
  agent has to choose. "Coverage over cleverness" beats "do a good job."
- An explicit **"git is functional in /workspace — commit before
  exiting"** line. AWF's executor will auto-stage + commit if the agent
  leaves unstaged changes, but being explicit is cheap insurance.
- A **"Do not push — AWF handles it"** line. Otherwise the agent may try
  `git push`, fail (no upstream set), and treat that as a task failure.

**Bad prompt shapes**:
- Imperative-without-justification ("do X without explanation") — reads
  suspicious, triggers refusal.
- Under-specified ("make this better") — agent produces nothing.
- Over-specified down to variable names — agent feels micromanaged, makes
  trivial changes and exits.

## 5 — Auth mounts: which to pass, which to make rw

The agent container needs the **same auth the host operator already has**
for (a) the coding CLI's model provider, (b) GitHub, (c) git identity.
Pass these as bind mounts:

| Host source | Container target | Mode | Why |
|---|---|---|---|
| `~/.codex` | `/home/agent/.codex` | **rw** | Codex writes model-list cache + refresh tokens; `ro` → `ERROR failed to write models cache: Read-only file system`. |
| `~/.claude` | `/home/agent/.claude` | **rw** | Claude Code session state, backups, skills cache. |
| `~/.claude.json` | `/home/agent/.claude.json` | **rw** | Claude Code's top-level config is a SINGLE FILE alongside the dir above. Without this mount the session dies mid-run with `Claude configuration file not found at: /home/agent/.claude.json` — Claude atomically rewrites the file on token refresh and can't find its own backup without the file-level bind. |
| `~/.gemini` | `/home/agent/.gemini` | **rw** | Same story as Codex/Claude. |
| `~/.config/gh` | `/home/agent/.config/gh` | ro | Stable OAuth/PAT; no state change mid-task. |
| `~/.gitconfig` | `/home/agent/.gitconfig` | ro | Identity + aliases only. |
| `~/.ssh` | `/home/agent/.ssh` | ro | Keys for `git push`. |

The container user is `agent` (UID 1000) to match the typical host user —
file-permission collisions are rare. If the host user is NOT UID 1000,
either rebuild the agent-runtime image with a matching UID or mount with
`:delegated` / `chown` in an init container.

## 6 — Companion services (live backend / web / Redis alongside the agent)

If the test commands need more than Postgres (e.g. HTTP calls to a real
backend, a web app under Playwright), declare `companions` in the task
spec. Each companion becomes a service in the per-workspace compose
project alongside `agent` + `postgres`.

```json
"companions": [
  {
    "name": "backend",
    "repo_url": "git@github.com:owner/backend.git",
    "branch": "development",
    "dockerfile": "Dockerfile",
    "env_file": "/absolute/path/to/backend/.env",
    "environment": {
      "DB_URL": "${POSTGRES_URL}",
      "ENV": "development"
    },
    "depends_on": ["postgres"],
    "healthcheck_cmd": "python -c 'import urllib.request,sys; sys.exit(0 if urllib.request.urlopen(\"http://localhost:8000/api/v1/health\", timeout=2).status==200 else 1)'"
  },
  {
    "name": "web",
    "repo_url": "git@github.com:owner/web.git",
    "env_file": "/absolute/path/to/web/.env.local",
    "environment": {"API_URL": "http://backend:8000"},
    "depends_on": ["backend"],
    "healthcheck_cmd": "wget -qO- http://localhost:3000 >/dev/null || exit 1"
  }
]
```

### Companion gotchas

- **`env_file` must be an absolute host path**. AWF mounts it read-only via
  `docker compose env_file:` semantics (the file is read at compose-up; no
  secrets get baked into images or AWF state).
- **`${POSTGRES_URL}` placeholder** in any `environment` value gets
  expanded by the driver to the stack-local Postgres URL (driver owns the
  password; companions see the right connection string regardless of what's
  in their .env). Use this instead of hardcoding credentials.
- **Healthcheck commands with mixed quoting** (both `'` and `"`) — write
  them naturally in JSON; AWF JSON-encodes them into YAML block form.
  Don't worry about shell-escaping.
- **`healthcheck_cmd` is what gates the agent**. Agent container's
  `depends_on` waits on `service_healthy` for every companion that declares
  a healthcheck. A companion without one starts in parallel with the agent
  — racy and rarely what you want.
- **Healthcheck URL**: don't guess. Check the app's actual route. Common
  mistakes: `/healthz` vs `/api/v1/health`, `/ping` vs `/_health`, etc.
  Test the URL on your host first.
- **Image builds are slow**. First-time compose-up builds every companion
  image; a Python backend compiling asyncpg takes 2-3 min, a Next.js
  production build 3-5 min. Subsequent runs use cached layers.

## 7 — Postgres choice

AWF's default is **`pgvector/pgvector:pg18`** mounted at
`/var/lib/postgresql` (the pg18+ convention, NOT `/var/lib/postgresql/data`
— that path is for pg17 and earlier and will cause pg18 to exit(1) with
"database data in unused mount/volume").

Override via `WorkspaceComposeSpec.postgres_image` if your task doesn't
need the `vector` extension and you want to save ~200 MB of image pull.
For any task whose backend uses pgvector / embeddings, keep the default.

## 8 — Test commands: ordering + env assumptions

- **First command MUST be dependency install**: `npm ci` for Node,
  `uv pip install -e ".[dev]"` for Python, etc. Assume nothing is
  preinstalled beyond the base agent runtime.
- If `requires_database: true`, AWF runs `alembic upgrade head`
  **immediately AFTER the first test command** — not before. Reason: for
  any repo whose Alembic `env.py` imports the app package, migration can't
  run until the app is installed. Running migration after `test_commands[0]`
  sidesteps that without forcing every caller to duplicate an install line.
  - If `test_commands[0]` fails, migration is skipped (there's nothing to
    migrate against).
  - If migration fails, remaining test commands are skipped (they'd run
    against a broken schema, and the output would be noise).
- Make sure `AIRA_DATABASE_URL` (or whatever name your Alembic `env.py`
  reads) is set in the agent container. AWF sets both `DATABASE_URL` and
  `AIRA_DATABASE_URL` to the same stack-local URL.
- Test commands run inside the agent container via `docker compose exec -T
  -w /workspace agent sh -lc <command>`. Shell metacharacters (pipes,
  `&&`, `$VAR`) work. First failing command stops the sequence.
- **Venv auto-activation**: AWF prefixes every validation command (and the
  migration) with a check for `/workspace/.venv/bin/activate` and sources
  it when present. This matters because `uv pip install -e ".[dev]"` (or
  `python -m venv`) creates a `.venv` in the repo root, and subsequent
  calls to `alembic` / `pytest` / `ruff` via `/usr/local/bin/*` wouldn't
  otherwise see the venv's site-packages — classic
  `ModuleNotFoundError: No module named '<your_app>'`. Nothing extra to
  do in your task spec; just write the commands naturally.
- Agent runtime ships: Python 3.12, Node 22, git, jq, ripgrep, tini, the
  three coding CLIs, and the Linux system libraries Playwright's chromium
  browser needs at runtime (libnss3, libatk-bridge2.0-0, libxkbcommon0,
  etc). Playwright's *browser binaries* are NOT pre-installed — add
  `npx playwright install chromium` as a test command (≈30 s download).
  Do NOT use `--with-deps`: it tries to `su root` + `apt install`, and
  the agent container runs as the unprivileged `agent` user with no
  root password (fails with `su: Authentication failure`).

## 9 — Model selection

By default, **let each CLI read its own config** (`~/.<cli>/config.toml`
etc.). Do NOT hardcode model names in task specs unless you know the CLI
supports that exact model under the host's account type.

Real example: passing `--model gpt-5.1` to `codex exec` fails with
`"The 'gpt-5.1' model is not supported when using Codex with a ChatGPT
account"` — because that model is only available on the OpenAI API tier,
not the ChatGPT subscription tier. The user's config already has the
right value for their account.

## 10 — Budgeting wall-clock time

For the task scheduler (you), realistic expectations:

| Stage | First run | Cached run |
|---|---|---|
| Clone mirror (one-time per repo) | 15-30 s | n/a |
| Build agent-runtime image | once, ~10 min | 0 (use image) |
| Build a companion image | 3-5 min per companion | 0 (cached) |
| Compose up (no companions) | 10-15 s | 5 s |
| Compose up (2 companions) | 6-10 min | 20-40 s |
| Coding agent execution (xhigh reasoning) | 5-10 min | same |
| Test suite (depends on repo) | seconds to tens of minutes | same |
| Push + `gh pr create` | < 5 s | same |

Don't set polling timeouts shorter than the expected stage — if you poll
every 30 s and the backend image build takes 5 min, your first status
check should be after 8 min.

## 10.5 — PR monitor (after the PR is opened)

Every AWF workspace that opens a feature-branch PR continues running in
a new state — ``monitoring_pr`` — until the PR is **merged** into its
base branch (feature-PR variant) or **declared ready for the human**
(release-PR variant for ``development → main``).

You don't have to do anything in the task spec to opt in — if an AWF
orchestrator is wired with a ``PullRequestMonitorRunner`` (stock run
launches it automatically), monitoring is the default. What matters is
what the task prompt does and doesn't need to say.

### What the task prompt does NOT need to cover

The monitor owns the 5 post-PR gates:

1. Inline (diff) comments resolved on GitHub (``resolveReviewThread`` GraphQL mutation).
2. Review-level outside-diff comments evaluated + resolved.
3. CI green — every required check SUCCESS or SKIPPED.
4. No merge conflicts.
5. Base branch merged into feature branch before the PR merges into base.

Do NOT tell the agent to "address reviewer comments" or "merge your own
PR" in the prompt — the agent CLI runs once, produces the initial
commits, and exits. AWF re-invokes the same CLI inside the same
container when comments arrive, with targeted prompts the monitor
generates.

### Per-comment decision shape the CLI produces

When the monitor hands a review thread or review-level comment to the
CLI, it expects a reply in one of three shapes:

| Reply prefix | Meaning | What AWF does next |
|---|---|---|
| `fixed in commit <sha>` (or anything that isn't one of the markers below) | CLI made the fix, committed locally | AWF pushes after the comment burst settles, then resolves the thread |
| `FALSE POSITIVE: <reason>` | CLI disagrees with the reviewer and replies inline | AWF resolves the thread with the reviewer's reply already posted |
| `DEFER: <what you need>` | CLI can't address without more info | AWF leaves the thread unresolved and marks the verdict; repeated deferrals stop the monitor |

The CLI also posts the reply on GitHub itself (``gh pr review-thread
reply`` or ``gh pr comment``) — AWF's resolve call happens AFTER the
reply is visible to the reviewer.

### Commit-then-push-on-settle

The monitor does NOT push after every fix. A reviewer bot like
CodeRabbit typically drops 5–20 inline comments in one burst within 30 s
of a push. The monitor addresses every comment in that burst locally,
waits a 30 s settle window for more, and only pushes once the burst is
quiet. One push → one CI run → minimum cost.

### No iteration or wall-clock budget caps

The monitor takes full responsibility for driving a PR to a terminal
action (``Merge`` / ``NotifyHuman`` / ``Abort(pr_closed_externally)``
/ ``ShortCircuitCompleted``). Volume is not a terminal condition — a
PR that attracts 500 review cycles is fine as long as the monitor
keeps making progress.

If the monitor PROCESS dies (OOM, Docker restart), the PR is
stranded. The ``awf-watchdog`` CLI (``awf-watchdog start --work-dir
<dir>``) periodically scans open ``awf/`` PRs and re-attaches the
monitor for any PR whose ``run_awf.py`` process isn't in ``ps``.

### Release PR (``development → main``)

Dev-to-main PRs must NEVER be auto-merged. To open/maintain the release
PR automatically, create a ``sync_release_pr`` task (it opens or reuses
the ``development → main`` PR and attaches the release monitor with
``auto_merge=false``). To monitor an already-open release/manual PR,
adopt it via the PR-adoption flow with ``auto_merge=false`` — that
selects ``build_release_pr_monitor`` instead of
``build_feature_pr_monitor``. (The old ``monitor_release_pr`` task kind
is deprecated and rejected; use PR adoption instead.) Everything else is
identical to the feature flow — comment resolution, CI fixes, base sync
— except:

- No ``gh pr merge`` call. Ever.
- When all 5 gates are green, AWF posts a "✅ Ready to merge at commit
  `<sha>`" comment on the PR and transitions to ``completed``.
- If new commits land after the ready comment, the monitor re-verifies
  all 5 gates and re-posts on the new head SHA.

### Branch-protection fallback

If GitHub's branch protection rejects the ``gh pr merge`` call (token
lacks permission, required reviews not met in the repo's ruleset, etc.),
the monitor falls back to the release-PR behaviour: posts the
"ready to merge" comment and exits ``completed``. Operator sees the PR
flagged as ready and does the final click.

### Auth

Uses the same ``~/.config/gh`` mount as ``gh pr create``. No new auth.
If you need a finer-grained token for release-PR merging, pass it via
``~/.config/gh`` on the host — AWF inherits whatever ``gh`` was logged
in as.

## 11 — What to do when validation fails

**Do NOT manually intervene** — do NOT `git push` from the worktree
yourself, do NOT `gh pr create` yourself. The value of AWF is autonomous
PR production; a manual shortcut defeats the test.

Diagnose:
1. `docker logs awf-<workspace_id>-<service>` for every service in the
   stack. The reason is almost always here.
2. `docker exec -it awf-<workspace_id>-agent bash` to poke inside the
   agent container — `env`, `git status`, `ls /workspace`, etc.
3. Check the workspace row's `failure_reason` and `failure_message` fields
   for the structured cause.

Common failure modes + fixes:
| Symptom | Root cause | Fix |
|---|---|---|
| `fatal: not a git repository` inside container | Mirror not bind-mounted at same absolute host path | Done in stock AWF since `da07637`. If you see it, you're on a stale AWF. |
| `alembic upgrade head` fails with `ModuleNotFoundError: <app>` | Migration ran before the app was installed, or migration used system python instead of the uv-created `/workspace/.venv` | Put the dep-install step as `test_commands[0]` so AWF runs migration AFTER it; stock AWF also auto-activates `/workspace/.venv` for every command so uv-installed packages are visible to alembic. |
| `alembic upgrade head` exits non-zero right after agent | Alembic env-var not set | Alias the app's env-var name → `DATABASE_URL` in the template (stock AWF aliases `AIRA_DATABASE_URL`). |
| Companion healthcheck never goes `service_healthy` | Wrong URL path | Check the app's actual health route (e.g. aira-agent is `/api/v1/health`, not `/healthz`). `curl` the real service from the host to confirm before scheduling. |
| `gh pr create: No commits between X and Y` | Agent made no changes (or made changes but didn't commit AND AWF's auto-commit was a no-op because files weren't staged) | Widen the prompt's scope or check agent refusal; check `git log base..HEAD` in the worktree. |
| `failed to parse compose.yml` | Usually: a healthcheck command with mixed quoting rendered into a flow scalar | AWF template uses `tojson` filter; if you hand-rolled the compose, switch to block form. |
| Healthcheck never goes `service_healthy` | Wrong URL in the healthcheck command | Query the real service for its health path; update the spec. |
| CLI crashes with "Read-only file system" | Mounted its auth dir as `ro` | Mount `.codex` / `.claude` / `.gemini` as `rw`. |

If the root cause is a bug in AWF itself, **fix AWF first, re-run the
task**. A one-off bypass "just for this PR" is a trap — the bug will bite
every future user.

## 12 — Cleanup

AWF does NOT auto-cleanup on failure (intentional, so you can post-mortem).
After a successful run, call `DELETE /v1/workspaces/{id}` (or manually
`docker compose --project-name awf_<ws_id> down -v`) to reclaim containers
+ volumes.

In a long-running environment, budget periodic cleanup:
- `docker ps -a --filter 'name=awf-ws_'` — find zombie stacks
- `docker volume ls --filter 'name=awf-ws_'` — orphaned volumes
- `rm -rf <work_dir>/git/worktrees/*` — abandoned checkouts

## 13 — Anti-patterns

1. **Manual `git push` or `gh pr create` from the worktree to "rescue" a
   failed run.** The whole value is autonomous PR production. If AWF
   fails, fix AWF.
2. **Hardcoding secrets in `environment`.** Use `env_file` (companions)
   or `${POSTGRES_URL}` (stack-local DB). Secrets in the task spec end up
   in the workspace DB row.
3. **Reusing one task spec for many runs without idempotency-key.** Every
   call creates a new workspace; with an idempotency-key, replays return
   the existing one.
4. **Hard-coding model names.** The CLI's config already has the right
   one for the host's account. Override only when you explicitly need to
   A/B a model.
5. **Running multiple tasks against the same repo in parallel with
   overlapping file scopes.** AWF isolates each workspace, but PRs will
   conflict at merge time. That's a separate problem AWF Phase 1.5 (merge
   queue) addresses; for now, either ensure non-overlapping scopes or run
   sequentially.
6. **Using the aira-web Dockerfile (or any production Dockerfile) as a
   dev companion when you need watch-mode.** Production images are static
   — the agent's worktree edits don't reach the running `web` container.
   Either rebuild on demand, run `npm run dev` via `command:`, or scope
   the task so that only test files change (tests run in the agent
   container, not the web container).

## 14 — Minimal end-to-end example

A smoke-test config that verifies the pipeline works against any repo:

```json
[{
  "repo_url": "git@github.com:you/repo-with-readme.git",
  "branch_base": "main",
  "task_title": "docs: add AWF verification note",
  "task_prompt": "Append a single line to the end of README.md that says exactly: `> Touched by AWF at <ISO-timestamp>` (substitute the current UTC time). Commit with message `docs(awf): pipeline verification`. Do not touch other files; do not push — AWF handles push.",
  "agent": "codex",
  "test_commands": ["grep -q 'Touched by AWF at' README.md"],
  "requires_database": false
}]
```

Run: `./scripts/run_awf.py --config smoke.json --work-dir /tmp/awf-smoke`.
Expected wall time: 1-2 min. Expected result: a green workspace with a PR
URL stored on the row.

If this smoke test doesn't produce a PR, AWF is broken in your environment
— don't try a real task until it does.
