# AWF Phase 1.5 — PR Monitor: Feature Branch → Development, Fully Autonomous

> Historical design note: this plan predates the current service-backed
> monitor adoption and remonitor surfaces. References to retired watchdog or
> helper-script paths are preserved as implementation history, not current
> operator guidance.

## Context

Today AWF ends the task at "PR opened." The real cost — reviewer comments
(CodeRabbit, Cursor Bugbot, human reviewers), CI failures, base-branch
drift, merge conflicts — falls on a human. For the MVP to deliver its
value ("parallel agent throughput"), each task must own its PR through
**merge into `development`**, not just through PR creation.

**Each task owns its own PR(s).** The monitor is per-task, not a
centralized service. The same workspace that created a PR watches it,
addresses comments by invoking its own coding CLI inside its own
container, resolves threads on GitHub, and merges. When the task is done,
the workspace is torn down and its monitor goes with it.

AWF implements this monitoring itself via the `gh` CLI and the GitHub
GraphQL API — no external dependency (clawdbot, etc.) is pulled in.

Scope split:

| PR type | Who merges | AWF's job |
|---|---|---|
| `feature → development` (task-authored) | **AWF (autonomous)** | Drive through the 5 gates + merge + resolve threads |
| `development → main` (release) | **Human only** | Run the 5 gates, post "ready to merge" notification, stop |

### The 5 gates (verbatim from user requirements)

1. **Inline (diff) comments resolved** — each thread marked resolved on GitHub (either via a fix commit or explicit dismissal as false-positive). Resolution is **in scope** — without it the PR is not mergeable under branch-protection policy.
2. **Outside-diff comments evaluated** — CodeRabbit and similar leave review-level comments without a line anchor; these also must be addressed or dismissed, then marked resolved.
3. **CI green** — every required check SUCCESS or SKIPPED; zero FAILURE.
4. **No merge conflicts** — GitHub's mergeable state is `MERGEABLE`.
5. **Base merged into head** — `development` has been merged into the feature branch first; the PR is then merged into `development`.

### Non-goals for this iteration
- Auto-rebase (we merge base→head, not rebase — squash-merge at the PR level handles final history).
- Merge-queue semantics across multiple task PRs (Phase 2).
- Reviewing / commenting on *other tasks'* PRs.
- Multi-repo PR chains.
- dev→main auto-merge (human-only per user directive).

## Approach

### 1. New workspace state: `monitoring_pr`

Extend `WorkspaceStatus`:

```
ready → provisioning → running → validating → pushing
      → monitoring_pr → completed
                     → failed (with a specific reason_code)
```

The executor's current `pushing → completed` transition — fired as soon
as `gh pr create` succeeds — becomes `pushing → monitoring_pr`. Only
the monitor transitions out of `monitoring_pr`.

### 2. The monitor loop — reactive, commit-then-push

This is the heart of the design and the place it differs from bulk/cron
monitoring. Pseudocode for one workspace's monitor:

```
state = { iter: 0, last_push_sha: <pr_head_after_creation>, threads_addressed_ids: {} }

while True:
    pr = fetch_pr_state(pr_number)              # gh + GraphQL: checks, mergeable, threads

    if terminal_condition(pr, state):           # merged / closed / iter cap / wall-clock
        transition_workspace(…)
        return

    unresolved = pr.unresolved_threads + pr.unresolved_review_comments

    if unresolved:
        # Enter fix cycle. Commits land LOCALLY only — no push yet.
        fix_cycle(pr, state, unresolved)
        # fix_cycle() returns when either (a) no new comments arrived during
        # the last fix pass AND every addressable thread has a local commit
        # or a "false-positive, resolving" marker, or (b) iteration cap hit.
        if state.local_commits_since_last_push > 0:
            git_push(feature_branch)
            state.last_push_sha = git_rev_parse_head()
            # Now resolve the threads on GitHub — push must land first so
            # reviewers can see the fix commit linked to the resolution.
            resolve_threads(state.threads_addressed_ids)
            state.threads_addressed_ids.clear()
        state.iter += 1
        continue

    if pr.checks_state == "PENDING":
        sleep(poll_interval)                    # passive wait — no iter bump
        continue

    if pr.checks_state == "FAILURE":
        invoke_cli_to_fix_ci(pr)                # fix cycle keyed off failing logs
        state.iter += 1
        continue

    if pr.base_behind_count > 0:
        sync_base_into_head()                   # git fetch + merge, commit, push
        state.iter += 1
        continue

    if pr.mergeable != "MERGEABLE":
        # Typically a transient UNKNOWN that resolves after GitHub recomputes,
        # or a conflict we just introduced. Poll a few times before escalating.
        sleep(poll_interval)
        continue

    # All 5 gates green — merge.
    gh_pr_merge(--squash --delete-branch)
    transition_workspace(completed)
    return
```

`fix_cycle` is the subtle part. It implements the user's requirement
("develop, commit, not push; check for new comments; if yes, address them
in a new commit; if no new comments, push"):

```
fix_cycle(pr, state, initial_unresolved):
    batch = initial_unresolved
    while True:
        for thread in batch:
            if thread.id in state.threads_addressed_ids:
                continue
            verdict = invoke_cli_to_address_thread(thread)   # runs coding CLI in container
            # verdict is one of:
            #   "fix_committed"     — CLI made changes and committed locally
            #   "false_positive"    — CLI wrote a reply justifying dismissal
            #   "defer"             — CLI couldn't address (e.g. needs external info)
            record(thread.id, verdict)
            state.threads_addressed_ids[thread.id] = verdict

        # Re-fetch comments. Two things can happen during the fix pass:
        # (1) reviewers added new comments — address them in the next batch
        # (2) no new comments — we're settled; leave the loop so caller pushes
        pr = fetch_pr_state(pr.number)
        fresh = pr.unresolved_threads + pr.unresolved_review_comments
        new_threads = [t for t in fresh if t.id not in state.threads_addressed_ids]
        if not new_threads:
            return  # caller pushes the accumulated local commits
        batch = new_threads
        state.iter += 1
        if state.iter >= ITER_CAP:
            return  # caller still pushes whatever we've got, then the outer loop caps out
```

The "commit but don't push until comments settle" pattern means one push
per *comment burst* instead of one per *comment*. Reviewers (especially
CodeRabbit) drop 5-20 comments within 30 s of a push, each of which may
trigger the next after Cursor/others react. Batching keeps CI from
running on every intermediate state.

**Settle window inside `fix_cycle`**: poll once the CLI reports done, then
wait a short `settle_interval` (default 30 s) and poll again. If NO new
comments arrived in that window, the batch is considered settled.
This handles reviewer-to-reviewer reactions that wouldn't otherwise
appear until after the push.

### 3. Pure decision core

File: `src/awf/runtime/pr_monitor.py`

Pure, side-effect-free logic that decides what to do given a `PRStatus`
snapshot:

```
@dataclass(frozen=True)
class PRStatus:
    number: int
    head_sha: str
    mergeable: Literal["MERGEABLE","CONFLICTING","UNKNOWN"]
    check_state: Literal["SUCCESS","FAILURE","PENDING","NEUTRAL"]
    unresolved_inline_threads: tuple[ReviewThread, ...]
    unresolved_review_comments: tuple[ReviewComment, ...]
    base_behind_count: int
    closed: bool
    merged: bool
```

`decide(status, state) -> MonitorAction` returns one of:

- `AddressComments(batch)`
- `SyncBase`
- `WaitForCI(reason)`
- `Merge`
- `Abort(reason_code)`

The runner wraps this with I/O. Testing the decision core is trivial
table-driven tests — no GitHub, no container.

### 4. The runner (I/O layer)

File: `src/awf/runtime/pr_monitor_runner.py`

Responsibilities:

- **`fetch_pr_state(number) -> PRStatus`**
  - `gh pr view <n> --json ...` for the structured bits.
  - A GraphQL query for review threads (inline + outside-diff) and their `isResolved` + `id` fields — `gh` CLI alone doesn't surface these; GraphQL mutation `resolveReviewThread` needs the thread ID anyway.
- **`invoke_cli_to_address_thread(thread) -> verdict`**
  - `docker compose exec agent <cli>` with a prompt that includes: PR number, thread body, file + line anchor (if inline), existing replies. The CLI is told: if fix is needed, edit + `git commit`; else, write a one-line false-positive reply.
- **`resolve_threads(ids)`**
  - GraphQL mutation `resolveReviewThread(threadId: $id)` per thread. Batch, with exponential backoff on 5xx.
- **`sync_base_into_head()`**
  - `git fetch origin <base>` + `git merge origin/<base>`, commit, push. On conflict, invoke the CLI with a "resolve these conflicts" prompt.
- **`gh_pr_merge(...)`** — `gh pr merge <n> --squash --delete-branch`.
- **State persistence** — iteration count, last-push SHA, resolved-thread IDs, wall-clock start — all on the workspace row, so a crash mid-monitor resumes from DB.

### 5. dev → main: notification-only variant

Selected by `auto_merge=False` (no dedicated task kind). To monitor an
existing release/manual PR, adopt it via the PR-adoption flow with
`auto_merge=false`; to open/maintain the `development → main` release PR
automatically, use the `sync_release_pr` task kind. (The earlier
`monitor_release_pr` task kind is deprecated and rejected.) This variant:

- Does NOT clone a repo or run the initial coding agent.
- Does NOT create a PR when adopting one that already exists.
- Runs the same monitor loop against the supplied PR number, but with `auto_merge=False`.
- On the `Merge` branch of the decision tree: posts a GitHub PR comment "All gates green — ready for human merge", records the ready-SHA, and keeps polling until the PR is actually merged or closed. Never calls `gh pr merge`.
- If new comments arrive AFTER the "ready" notification but before human merge: re-enters the fix cycle as normal. The CLI still addresses them. New commits push. Checks re-run. The "ready" notification re-posts only when the ready-SHA advances, so the human isn't pinged per-poll.

The auto-merge flag is the only functional divergence; the rest of the
code is shared.

### 6. Iteration + time accounting (no caps)

- `iter` increments on `AddressComments`, `SyncBase`, `ReportCiFailure`. `WaitForCI` does NOT increment (it's passive).
- There is NO `iter_cap` or `wall_clock_cap`. The monitor drives each PR until it is merged or closed regardless of how many review cycles or how much wall-clock time it takes. `NotifyHuman` is a live waiting state, not a teardown signal. `iter_count` stays in state for log context only.
- If the monitor PROCESS dies, `awf-watchdog` re-attaches a new monitor to the PR — volume-driven death is handled externally.
- `poll_interval` (default 60 s during CI waits, 30 s during the comment-settle window).

### 7. Auth

The existing `~/.config/gh` mount (read-only) already gives `gh` and
GraphQL access inside the container. For merging into `development`, the
token needs `repo` scope and the branch-protection policy must permit
the token's account to merge — if it doesn't, the merge call fails and
we fall back to the dev→main behavior (post "ready" comment, stop). This
fallback is documented in the README and is the only reason a task PR
would land in "ready but not merged" terminal state.

### 8. Idempotency + crash safety

- `last_push_sha` + `threads_addressed_ids` persisted on the workspace row.
- A crash mid-`fix_cycle` resumes from DB: the monitor re-fetches comments, skips already-addressed threads (by ID), and keeps going.
- Merge is terminal — once `merged: true`, subsequent polls short-circuit to `completed`.
- PR closed externally (human action): `Abort(pr_closed_externally)` — don't force anything open.

## Critical files (new + changed)

| Purpose | File | Status |
|---|---|---|
| Pure monitor decision logic + dataclasses | `src/awf/runtime/pr_monitor.py` | new |
| `PullRequestMonitorRunner` (I/O side) | `src/awf/runtime/pr_monitor_runner.py` | new |
| Thin wrapper over `gh` CLI + GraphQL | `src/awf/common/github_client.py` | new |
| Executor wiring — add `monitoring_pr` stage | `src/awf/control/executor.py` | change |
| New workspace-status enum entry | `src/awf/core/models.py` (or wherever `WorkspaceStatus` lives) | change |
| Persist iteration counter, last-push SHA, resolved-thread IDs, merge SHA | `src/awf/db/models.py` + Alembic migration | change |
| Task-spec `task_kind` field (feature vs release-monitor) | `src/awf/api/schemas.py` + DB model | change |
| Release-PR variant | `src/awf/runtime/release_pr_monitor.py` | new (thin layer over the generic monitor) |
| CLI prompt templates for comment-address / conflict-resolve / CI-fix | `src/awf/runtime/monitor_prompts.py` | new |
| SKILL.md — document `task_kind` + the monitor lifecycle + what task prompts DON'T need to cover (comment resolution is AWF's job, not the task prompt's) | `skills/awf-scheduler/SKILL.md` | change |
| README — auth + branch-protection prerequisites | `README.md` | change |

## TDD approach

### Unit tests (pure logic, no I/O)

1. **`PRStatus → MonitorAction` table-driven tests** in `tests/unit/runtime/test_pr_monitor.py`:
   - All 5 gates green + base up-to-date → `Merge`
   - Gates green but base behind → `SyncBase`
   - Unresolved inline threads → `AddressComments(batch=inline)`
   - Unresolved review comments (no inline) → `AddressComments(batch=review)`
   - Both kinds unresolved → `AddressComments(batch=union)`
   - CI `PENDING` + nothing else blocking → `WaitForCI`
   - CI `FAILURE` → `AddressComments(batch=[ci_failure_pseudo_thread])` or a dedicated `ReportCiFailure` action (TBD during implementation — depends on how cleanly CI-fix reuses the comment-fix code path)
   - PR `closed: true` → `Abort(pr_closed_externally)`
   - PR `merged: true` → short-circuit to `completed`, no further action
   - High `iter_count` (e.g. 1000) still routes by the normal gates — NO abort on volume
   - Edge: `mergeable: UNKNOWN` + PENDING → `WaitForCI`
   - Release-PR variant: all gates green → `NotifyHuman` (never `Merge`)

2. **Iteration accounting tests**:
   - `AddressComments`, `SyncBase` bump `iter`; `WaitForCI` does not.
   - A high `iter_count` does NOT abort — volume isn't a terminal condition.
   - A long wall-clock elapsed time does NOT abort either.

3. **Thread-dedup tests** — `AddressComments` never re-queues a thread whose ID is in `threads_addressed_ids`.

4. **Prompt-template tests** in `tests/unit/runtime/test_monitor_prompts.py`:
   - Inline-thread prompt includes PR number, file path, line number, thread body, existing replies.
   - CI-failure prompt includes the failing check's name + truncated log.
   - Conflict-resolve prompt includes the file list + a pointer to `git status`.

### Integration tests (fake GH, real runner)

5. **`FakeGhClient` + `FakeGraphQLClient`** in `tests/fakes/gh_client.py`:
   - Queueable responses (mirrors `FakeCommandRunner`).
   - Records thread-resolve mutations with their IDs.

6. **Runner loop in `tests/integration/runtime/test_pr_monitor_runner.py`**:
   - Single-comment happy path: comment arrives, CLI fixes, push, resolve, merge → workspace `completed`.
   - Two-burst fix cycle: first burst of 3 threads, CLI addresses them locally; before push, 2 more threads arrive; runner handles them, THEN one push + 5 resolves.
   - False-positive path: CLI returns "false_positive" with a justification reply; runner posts the reply, then resolves the thread.
   - Crash-safe resume: start runner with `threads_addressed_ids = {t1: fix_committed}`; fake GH still shows t1 unresolved; runner does NOT re-invoke CLI for t1 (it'll just push + resolve).
   - CI failure → CLI fix → push → checks green → merge.
   - Base behind → `SyncBase` → push → merge.
   - High iter_count with green gates still merges (no budget abort).
   - PR closed externally → `Abort(pr_closed_externally)`.

7. **Executor integration in `tests/integration/control/test_executor_monitor.py`**:
   - `pushing → monitoring_pr → completed` on first-poll merge.
   - `pushing → monitoring_pr → failed` on abort with the right reason code.

### End-to-end smoke (manual)

8. Throwaway fork + a canned "CodeRabbit simulator" that posts inline comments on PR open. One real run against that fork, observed to completion: PR merged, branch deleted, threads resolved, workspace `completed`.

## Verification

- `pytest tests/ -q` stays green; expect ~195-210 tests (163 today + ~30-40 new).
- `mypy` / `ruff` clean.
- Alembic migration up + down: new columns `pr_merge_sha`, `monitor_iter_count`, `monitor_threads_addressed`, `monitor_started_at` on `workspaces`.
- Manual local run: schedule a small aira-agent task, wait for CodeRabbit + Cursor to comment, observe the workspace cycle through `monitoring_pr` and end in `completed` with the PR merged.
- Manual release run: schedule a `sync_release_pr` task (or adopt an existing dev→main PR with `auto_merge=false`), verify a "ready to merge" comment appears when all gates are green and the PR is NOT merged.

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| CLI makes cosmetic changes that don't actually address feedback → thread re-opens on next review | Iter cap + require a new HEAD SHA between `AddressComments` invocations; if the SHA didn't advance despite a "fix_committed" verdict, force an abort. |
| GH token can't merge due to branch protection | Merge call fails → fall back to `NotifyHuman` behavior (post "ready to merge" comment, keep polling). Operator sees the PR flagged as ready; when it is merged manually, AWF observes the merge and only then completes/cleans up. Documented in README. |
| Cost explosion from repeated CLI invocations | Covered at the coding-CLI / workspace layer (per-workspace cost ceiling, reviewer-bot rate limits). Monitor-level budget caps were removed after stranding PRs that attracted 5 bot reviewers; operator intervention (close PR) is the correct escape for a genuine runaway. |
| CodeRabbit posts comments AFTER initial push during the settle window | `settle_interval` (30 s) in `fix_cycle` specifically to catch this. Worst case the burst arrives just after our push; next outer-loop iteration catches it. |
| Thread-resolve mutation fails (transient) | Retry with exponential backoff; if the thread stays unresolved, next poll re-queues it under `AddressComments` — the CLI sees its own previous fix commit + the already-posted reply and just needs to retry the resolve. |
| Two tasks' PRs against overlapping files conflict at merge time | Out of scope for this iteration (merge queue is Phase 2). For MVP, the second task's monitor hits `SyncBase` → `CONFLICTING`, escalates via `NotifyHuman`-style fallback. |

## Estimate

~5-7 engineering days with TDD:
- Day 1: `PRStatus` + `decide()` pure logic + unit tests.
- Day 2: `gh`/GraphQL client wrapper + `FakeGhClient` + fixture scaffolding.
- Day 3: `fix_cycle` + `PullRequestMonitorRunner` + runner unit tests.
- Day 4: Executor wiring + Alembic migration + executor integration tests.
- Day 5: Release-PR variant + prompt templates + SKILL.md + README.
- Day 6-7: Real-PR shakedown on a live aira-agent task, fix what breaks.

## Out of scope (Phase 2+)

- Merge queue across multiple task PRs.
- Auto-rebase (we stick with merge base→head + squash-merge at PR level).
- Reviewing / commenting on *other* tasks' PRs.
- Policy hooks ("don't auto-merge Fri 5pm-Mon 9am").
- dev→main auto-merge (explicitly human-only per user directive).
