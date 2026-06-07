# Plan — PRRT_kwDOSJAM6s6HoYAj: preserve Bitbucket review activity for settle gates

## Reviewer claim (PR #443, `src/awf/common/bitbucket_client.py:425`)
When `non_check_reviewer_logins` is configured, Bitbucket PRs never populate
`latest_external_review_activity_at` / `quiet_period_anchor_at` in `PRStatus`. The
non-check-reviewer settle helper falls back to the head-only done key when the anchor
is absent, so once the initial head settle has elapsed a *later* Bitbucket reviewer
comment can be handled and merged immediately instead of waiting
`non_check_reviewer_settle_seconds` from the new review activity. The GitHub status
path sets these fields precisely to reset the quiet window.

## Verdict: real bug — FIX

- `github_client.fetch_pr_status` (lines ~904-933) computes
  `latest_external_review_activity_at` from thread/issue/review comments and folds it
  into `_quiet_period_anchor(...)`, then sets all four fields on `PRStatus`.
- `bitbucket_client.fetch_pr_status` (lines 405-425) sets **none** of them → they
  default to `None`. `reviewer_settle._non_check_reviewer_settle_decision` then takes
  the head-only fallback (`quiet_period_anchor_at is None`), whose `done_key` is keyed
  on `head_sha` only. A late reviewer comment that does not change the head SHA never
  re-arms the quiet window.
- All building blocks already exist: Bitbucket comments carry `created_on`/`updated_on`
  (`parse_bb_datetime`), viewer filtering via `_is_viewer`/`account_id`, and the shared
  pure helpers `_quiet_period_anchor` / `_newer_activity` in `github_client_parsing`.

## Change (minimal, mirrors GitHub)

1. `src/awf/common/bitbucket_client_parsing.py`: add pure
   `latest_external_review_activity(comments, *, account_id)` returning the newest
   external (non-viewer, non-deleted) comment activity timestamp + source
   (`review_thread_comment` for inline, `issue_comment` for general), mirroring
   GitHub's `_latest_activity_from_thread_comments` semantics (counts activity across
   ALL comments, including resolved threads).
2. `src/awf/common/bitbucket_client.py` `fetch_pr_status`: compute the latest activity
   and the quiet-period anchor (`_quiet_period_anchor` from `github_client_parsing`,
   PR `created_on`/`updated_on` as fallbacks, `head_committed_at=None`), and pass all
   four fields to `PRStatus`.

## Tests (focused, TDD)

- Pure: `latest_external_review_activity` picks the newest external comment, ignores
  viewer-authored/deleted, distinguishes inline vs general source.
- Client: `fetch_pr_status` populates `latest_external_review_activity_at` /
  `quiet_period_anchor_at` from a reviewer comment so the activity-settle path engages.

Full AWF/GitHub validation (full suite, coverage gate) runs after agent completion.
