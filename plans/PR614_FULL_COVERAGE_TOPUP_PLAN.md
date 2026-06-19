# PR614 Full Coverage Top-Up Plan

## Problem

The current PR #614 CI run `27848776769` passes all coverage shards, lint/type,
console, and release-artifact checks, but the combined `python-full-coverage`
job fails at `98.94%` against the required `99.00%`.

The uploaded `full-coverage-report` artifact shows reachable, PR-touched gaps in
mirror hooks repair handling. The narrowest meaningful uncovered behavior is the
`OSError` path in `src/awf/control/executor/mirror_hooks_repair.py`:

- `repair_mirror_hooks_path_or_mark_failed` must mark the workspace failed with
  `MIRROR_HOOKS_PATH_REPAIR_FAILED` when filesystem repair raises `OSError`.
- `repair_mirror_hooks_path_after_agent_cleanup_failure` must log and swallow an
  `OSError` during post-cleanup best-effort repair.

## Scope

- Add focused unit coverage for the mirror hooks repair `OSError` paths.
- Do not alter thresholds, workflow files, protected config, or broad CI
  behavior.
- Do not run the whole repository test suite or local full coverage gate; AWF and
  GitHub CI own broad validation after the agent exits.

## Steps

1. Add behavior-level tests in the existing executor mirror hooks path test area.
2. Assert failure reason, failure message, recovery completion behavior, and
   warning log fields for the `OSError` branches.
3. Run targeted pytest for the new tests.
4. Run the focused maintainability line-limit guard because this branch already
   had a shard-8 line-limit failure.
5. Write validation notes with the targeted evidence and broad-validation
   handoff.
6. Commit the scoped fix locally with a conventional CI-fix message.
