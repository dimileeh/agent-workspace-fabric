"""AWF prompt preamble definition."""

from __future__ import annotations

# Prepended to every agent prompt. Encodes AWF workspace contract invariants.
_AWF_PROMPT_PREAMBLE = """\
## AWF workspace contract (DO NOT VIOLATE)

You are inside an AWF-managed Docker workspace at /workspace, on a git
branch that AWF has already created for you. Your contract:

1. **DO NOT switch git branches.** Do not run `git checkout -b <name>`,
   `git switch -c <name>`, `git branch <name>`, `git checkout <name>`,
   or any equivalent. Commit ALL work on the current branch. AWF owns
   branch management. Drifting to a "properly named" feature branch
   strands your commits and the PR ends up empty.
2. **DO NOT push, rebase onto origin, or force-push.** AWF handles
   push + PR creation after you exit.
3. Commit your work locally as you go (`git add` + `git commit` is
   fine). AWF's post-agent step will also capture any uncommitted
   changes, but commits with good messages are preferred.
4. **DO NOT run AWF/GitHub-owned broad validation inside the agent
   phase.** Do not run the full `.awf/workspace.yml` validation suite,
   whole-repository test suites, full coverage gates such as
   `pytest --cov` / `--cov-fail-under`, full frontend builds, or CI-
   equivalent commands unless the operator explicitly asks for that
   exact diagnostic action in this task. AWF and GitHub CI own broad
   validation, provenance, logs, timeouts, and merge gating after you
   finish the code.
5. Focus your local checks. Run targeted tests, focused lint/type checks,
   or small repro commands only for the files and behavior you changed.
   When a plan or validation document needs evidence, record those
   focused checks and state that full AWF/GitHub validation is managed by
   AWF after agent completion; do not execute the broad suite yourself.
6. **Keep changes minimal and scoped.** Fix only what THIS task asks.
   Do not refactor, rename, or restructure unrelated code, split files,
   or introduce new abstractions unless the fix strictly requires it. A
   reviewer should see a small, obviously-correct diff; sprawling diffs
   that touch dozens of unrelated files get rejected and waste the run.
7. **Cover what you change; never pad coverage.** AWF enforces a hard
   coverage gate after you finish. As you implement, add or adjust focused
   tests for each behavior you add or change (test-first when practical),
   and reason about which new lines and branches need a test so total
   coverage does not drop — you do not run the full gate yourself. For
   genuinely non-behavioral, unreachable, or type-only code (e.g. a
   Protocol stub or an unexecutable defensive branch), add a justified
   coverage exclusion instead of a hollow test. A test that only executes
   a line to move the number is coverage-theater and gets rejected.

---

"""
