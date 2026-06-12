"""Fix-cycle helpers for the validation stage.

When validation fails in ``WorkspaceExecutor``, the old behaviour was
to mark the workspace ``failed`` and stop. That left many perfectly
recoverable workspaces stuck on minor test bugs the coding CLI could
have fixed on a second pass — the same capability the PR monitor
already has via ``max_fix_cycle_passes`` + ``ReportCiFailure``.

This module owns the *pure* pieces of the validation retry loop:

  - ``ValidationFixContext`` — an immutable dataclass describing one
    failed attempt. Carries the failing command, its exit code,
    stdout/stderr tails, the attempt counter, and the full list of
    validation commands the next pass will run against.
  - ``build_fix_prompt`` — turns that context into a prompt the
    coding CLI can act on. Pure string construction, trivially
    testable.
  - ``read_output_tail`` — truncates a validation-artifact file to
    its last ``max_chars`` characters so tails don't blow the
    context window.

The *loop* lives in ``WorkspaceExecutor.execute`` — it's the part that
touches the DB + adapter + validation runner, and belongs with the
rest of the executor's wiring. Keeping pure helpers here makes each
independently unit-testable without a FakeCommandRunner.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The CLI needs enough context to diagnose but not so much that the
# important signal scrolls off the top. 4 KB per stream is a practical
# middle ground: long enough for a full pytest failure dump, short
# enough that two streams (stdout + stderr) plus the rest of the
# prompt fits comfortably under a 200 KB context budget.
DEFAULT_TAIL_CHARS = 4000


@dataclass(frozen=True)
class ValidationFixContext:
    """One validation failure, packaged for the retry prompt.

    ``test_commands`` is stored as a tuple so the dataclass stays
    hashable — structured-logging layers sometimes dedupe retry events
    by context.
    """

    failed_command: str
    returncode: int
    stdout_tail: str
    stderr_tail: str
    pass_number: int
    """1-indexed — "this is attempt 1 of N". Never 0."""

    total_passes: int
    """The retry budget for this failure category. The coding CLI sees
    both counters so it can pace itself (conservative on attempt 1,
    more aggressive near the cap)."""

    test_commands: tuple[str, ...]
    """All validation commands that will run on the NEXT pass —
    not only the one that just failed. Gives the CLI the full gate
    list so a fix to one command doesn't accidentally break another."""

    reason_code: str | None = None
    """Structured validation reason code when available."""

    coverage_percent: float | None = None
    """Coverage observed on the failed validation pass, when available."""

    coverage_minimum_percent: float | None = None
    """Configured coverage threshold, when coverage enforcement failed."""

    baseline_coverage_percent: float | None = None
    """Coverage observed before the agent changed the branch, when measured."""

    failing_test_node_ids: tuple[str, ...] = ()
    """Pytest node IDs parsed from coverage-wrapped pytest output."""

    failing_test_evidence: tuple[str, ...] = ()
    """Fallback pytest failure evidence when node IDs are unavailable."""

    task_tag: str | None = None
    """Validated Jira issue key for the workspace, when one is attached.

    When set, the fix prompt instructs the agent to prefix any commit it
    authors itself with this tag. A fix-pass agent that self-commits leaves a
    clean worktree, so AWF's tagging fallback commit is skipped and the pushed
    commit would otherwise lose its Jira link."""


def read_output_tail(path: Path, *, max_chars: int = DEFAULT_TAIL_CHARS) -> str:
    """Return up to ``max_chars`` trailing characters from a file.

    Used to extract the last N chars of a validation command's
    stdout / stderr artifact for embedding in the fix prompt. Failure
    signals live at the tail of test logs, hence "tail".

    Implementation note: opens the file in binary mode and ``seek()``s
    to ``-max_chars`` from the end rather than reading the whole file
    into memory. A noisy failing test run can produce multi-MB
    artifacts; every fix-cycle pass reads two of them (stdout +
    stderr), so the naive ``read_bytes`` grows O(artifact_size) per
    retry. Tail-seeking keeps it O(max_chars) regardless of file size.

    Defensive: missing files return ``""`` (a failed validation run
    should always have written its artifact, but don't crash the
    whole retry loop if there's a race); non-UTF-8 bytes fall back to
    best-effort decode with lossy replacement.
    """
    if not path.exists():
        return ""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    if size == 0:
        return ""
    # Read only the trailing slice — bounded memory regardless of file size.
    to_read = min(size, max_chars)
    with path.open("rb") as f:
        if size > to_read:
            f.seek(-to_read, 2)  # 2 = SEEK_END
        data = f.read(to_read)
    return data.decode("utf-8", errors="replace")


def build_fix_prompt(context: ValidationFixContext) -> str:
    """Compose the retry prompt the coding CLI sees on a fix pass.

    Key contracts (asserted by tests):
      - Mentions the failing command + returncode.
      - Embeds stdout + stderr tails verbatim.
      - Tells the CLI to FIX, not re-implement.
      - Lists every validation command so fixes can be holistic.
      - Shows "attempt N of M" so the CLI can pace itself.
    """
    stdout_block = context.stdout_tail.rstrip() or "(empty)"
    stderr_block = context.stderr_tail.rstrip() or "(empty)"
    test_cmds_block = "\n".join(f"  - {cmd}" for cmd in context.test_commands)
    provider_fail_under = context.reason_code == "COVERAGE_FAIL_UNDER_NOT_REACHED"
    numeric_coverage_below_threshold = (
        context.coverage_percent is not None
        and context.coverage_minimum_percent is not None
        and context.coverage_percent < context.coverage_minimum_percent
    )
    coverage_below_threshold = provider_fail_under or numeric_coverage_below_threshold
    coverage_meets_threshold = (
        not provider_fail_under
        and context.coverage_percent is not None
        and context.coverage_minimum_percent is not None
        and context.coverage_percent >= context.coverage_minimum_percent
    )
    policy_lines = [
        "Quality-gate policy:",
        "  - Do not lower, disable, or bypass validation or coverage thresholds.",
        "  - Do not edit quality-gate configuration files such as "
        "`.awf/workspace.yml`, `pyproject.toml`, `.coveragerc`, "
        "`pytest.ini`, or CI workflow files unless the task explicitly "
        "asked you to change those policies.",
    ]
    if provider_fail_under:
        policy_lines.append(
            "  - Coverage provider reported fail-under was not reached; add "
            "meaningful tests for the relevant code paths after fixing any named "
            "failing tests. If the threshold is already unreachable on the "
            "unchanged base branch, preserve the threshold and make the smallest "
            "honest coverage improvement you can."
        )
    elif numeric_coverage_below_threshold:
        policy_lines.append(
            "  - Coverage is below the configured threshold; add meaningful "
            "tests for the relevant code paths after fixing any named failing "
            "tests. If the threshold is already unreachable on the unchanged "
            "base branch, preserve the threshold and make the smallest honest "
            "coverage improvement you can."
        )
    elif coverage_meets_threshold:
        policy_lines.append(
            "  - Coverage already meets the configured threshold; focus this "
            "retry on the named pytest failures."
        )
    policy_block = "\n".join(policy_lines) + "\n"
    failing_values = context.failing_test_node_ids or context.failing_test_evidence
    failing_tests_block = ""
    if failing_values:
        failing_tests_block = (
            "Failing pytest tests:\n"
            + "\n".join(f"  - {value}" for value in failing_values[:10])
            + "\nPrimary retry action: fix the failing pytest tests first.\n"
        )
    failing_tests_section = f"{failing_tests_block}\n" if failing_tests_block else ""
    coverage_lines: list[str] = []
    if context.reason_code:
        coverage_lines.append(f"Reason code: {context.reason_code}")
    if context.coverage_percent is not None:
        coverage_lines.append(f"Observed coverage: {context.coverage_percent:.2f}%")
    if context.coverage_minimum_percent is not None:
        coverage_lines.append(f"Required coverage: {context.coverage_minimum_percent:.2f}%")
    if context.baseline_coverage_percent is not None:
        coverage_lines.append(
            f"Pre-agent base-branch coverage: {context.baseline_coverage_percent:.2f}%"
        )
    if provider_fail_under:
        coverage_lines.append("Coverage provider reported fail-under was not reached")
    elif coverage_meets_threshold:
        coverage_lines.append("Coverage already meets the configured threshold")
    if coverage_below_threshold and failing_values:
        coverage_lines.append("Fix the failing pytest tests first, then revisit coverage")
    coverage_block = ""
    if coverage_lines:
        coverage_block = "Coverage context:\n" + "\n".join(f"  - {line}" for line in coverage_lines)

    task_tag_block = ""
    if context.task_tag:
        task_tag_block = (
            f"\n\nIf you commit your fix yourself, prefix every commit subject "
            f"you create with the task tag `{context.task_tag}` followed by a "
            f"space (for example `{context.task_tag} fix: …`) so the commit "
            f"links to its tracking issue. If a subject already starts with that "
            f"tag, do not add it again."
        )

    return (
        f"Validation failed after your previous pass. This is attempt "
        f"{context.pass_number} of {context.total_passes}.\n\n"
        f"Failing command: {context.failed_command}\n"
        f"Exit code: {context.returncode}\n\n"
        f"── stdout (tail) ──\n"
        f"{stdout_block}\n"
        f"── stderr (tail) ──\n"
        f"{stderr_block}\n\n"
        f"All validation commands that will run on the next pass:\n"
        f"{test_cmds_block}\n\n"
        f"{failing_tests_section}"
        f"{policy_block}\n"
        f"{coverage_block}\n\n"
        f"Your job: FIX the failure above. Inspect the tail output for "
        f"concrete clues (assertion mismatches, missing fixtures, wrong "
        f"test IDs, stale imports) and make the minimum change that will "
        f"let validation pass. Do not re-implement the feature from "
        f"scratch — the existing work is almost certainly fine except "
        f"for the specific failure reported. Keep changes tight.\n\n"
        f"After you exit, validation will run every command above "
        f"again, in order. If any of them fail you'll get one more "
        f"fix prompt until attempts are exhausted."
        f"{task_tag_block}"
    )
