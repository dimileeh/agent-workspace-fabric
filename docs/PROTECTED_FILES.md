# Protected Quality-Gate Files

AWF protects files that define test, coverage, build, and CI policy. Agents may
still change these files when the task owns them through `owned_paths`. Without
ownership, AWF only allows a narrow set of deterministic local-diff changes.

## Operator Override

The supported override path is explicit ownership. Add the required protected
path to the workspace or task `owned_paths`, for example:

- `pyproject.toml`
- `.github/workflows/**`
- `.awf/workspace.yml`

Owned protected paths bypass this guardrail. Unowned protected paths are checked
locally and fail closed when AWF cannot read or parse the old and new content.

## `pyproject.toml`

Allowed without ownership:

- Adding new entries to `[project].dependencies`.
- Adding new entries to `[project.optional-dependencies]`, including `dev`.
- Adding new entries to existing top-level `[dependency-groups]`.
- Metadata-only edits under `[project]` and `[project.urls]`.

Blocked without ownership:

- Removing or rewriting dependencies, including `pytest-*` dependencies.
- Removing optional dependency groups.
- Editing `[tool.pytest.*]`, `[tool.coverage.*]`, `[tool.ruff.*]`,
  `[tool.mypy.*]`, `[build-system]`, or `tool.hatch` build configuration.
- Lowering numeric coverage thresholds such as
  `[tool.coverage.report].fail_under`.
- Changing other pyproject sections that AWF cannot prove are metadata or
  dependency additions.

## GitHub Workflows

Allowed without ownership:

- Adding `continue-on-error: true` only to comment, PR-comment, notify, or
  notification steps.
- Bumping a pinned version `uses:` ref when the action owner/repo is unchanged
  and AWF can prove the new version is not a downgrade.
- Replacing a raw SHA `uses:` ref with a full semver tag for the same action.
- Adding jobs that are informational/comment/notify only and do not run tests,
  lint, coverage, build, deploy, publish, or release commands.

Blocked without ownership:

- Switching a pinned version `uses:` ref to a raw SHA, or switching between raw
  SHAs, because AWF cannot prove the new commit is a non-downgrade locally.
- Adding `continue-on-error: true` to validation steps such as pytest, lint,
  coverage, or build steps.
- Removing workflow jobs or steps.
- Editing job or step `if:` gates.
- Narrowing or changing validation commands.
- Adding non-informational jobs or steps.
- Changing workflow fields outside the explicitly allowed cases above.

## Fail-Closed Behavior

For unowned protected files, AWF blocks the change when:

- old or new file content cannot be read from local git/worktree state;
- TOML or YAML cannot be parsed safely;
- a changed protected file is not covered by the narrow classifier; or
- the classifier cannot prove the edit is one of the allowed cases.

Violation messages include the file, section/path, approximate line when known,
and the specific reason so operators can decide whether to reject the edit,
request an in-scope fix, or grant explicit ownership.
