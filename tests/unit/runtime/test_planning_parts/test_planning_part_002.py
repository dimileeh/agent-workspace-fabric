"""Plan/execute/compare lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.planning import (
    AGENT_STALLED_IN_CONFORMANCE,
    MAX_CONFORMANCE_TEXT_CHARS,
    ConformanceIterationRecord,
    ConformanceStallEvidence,
    ConformanceStallKind,
    ConformanceStallPolicy,
    build_conformance_stall_failure_evidence,
    build_conformance_stall_recovery_prompt,
    classify_conformance_stall,
)


def _stall_policy(
    *,
    no_output_seconds: int = 600,
    over_duration_seconds: int = 1800,
    repeated_output_threshold: int = 3,
) -> ConformanceStallPolicy:
    return ConformanceStallPolicy(
        no_output_seconds=no_output_seconds,
        over_duration_seconds=over_duration_seconds,
        repeated_output_threshold=repeated_output_threshold,
    )


def _iter_record(
    *,
    iteration: int,
    elapsed_seconds: float,
    report_digest: str | None,
    worktree_changed: bool,
    stdout: str = "",
    stderr: str = "",
    error_reason_code: str | None = None,
) -> ConformanceIterationRecord:
    return ConformanceIterationRecord(
        iteration=iteration,
        elapsed_seconds=elapsed_seconds,
        report_digest=report_digest,
        worktree_changed=worktree_changed,
        stdout=stdout,
        stderr=stderr,
        error_reason_code=error_reason_code,
    )


@pytest.mark.unit
def test_classify_conformance_stall_no_output_streak_breaks_on_changed_report_digest() -> None:
    # A non-None digest that differs from the prior iteration's digest is
    # genuine fresh progress, so it must break the no-output streak even
    # when stdout/stderr are empty (the agent wrote the report directly to
    # disk without surfacing it via stdout).
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=400.0,
            report_digest="digest-x",
            worktree_changed=True,
            stdout="",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=400.0,
            report_digest="digest-y",
            worktree_changed=True,
            stdout="",
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(no_output_seconds=600),
        plan_path=Path("docs/awf-plans/ws_fresh_digest.md"),
        report_path=Path("docs/awf-plans/ws_fresh_digest.conformance.json"),
        latest_error=None,
    )

    assert evidence is None


@pytest.mark.unit
def test_classify_conformance_stall_no_output_streak_includes_iter_zero_with_stale_preexisting_digest() -> (
    None
):
    # A preserved worktree (retry/salvage) can leave a report file on disk
    # before iteration 0 ever runs, so iteration 0's report_digest is non-
    # None even when the iteration produced no output and made no
    # worktree changes. The classifier must not treat that pre-existing
    # digest as fresh progress; otherwise the empty streak skips iter 0
    # and the stall is masked.
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=320.0,
            report_digest="stale-preexisting-digest",
            worktree_changed=False,
            stdout="",
            stderr="",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=320.0,
            report_digest="stale-preexisting-digest",
            worktree_changed=False,
            stdout="",
            stderr="",
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(no_output_seconds=600),
        plan_path=Path("docs/awf-plans/ws_preexisting_digest.md"),
        report_path=Path("docs/awf-plans/ws_preexisting_digest.conformance.json"),
        latest_error=None,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.no_output
    assert evidence.iteration_index == 1
    assert evidence.no_output_seconds == pytest.approx(640.0)


@pytest.mark.unit
def test_classify_conformance_stall_returns_repeated_output_when_report_digest_repeats() -> None:
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=60.0,
            report_digest="digest-a",
            worktree_changed=False,
            stdout="needs_iteration first",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=60.0,
            report_digest="digest-a",
            worktree_changed=False,
            stdout="needs_iteration second",
        ),
        _iter_record(
            iteration=2,
            elapsed_seconds=60.0,
            report_digest="digest-a",
            worktree_changed=False,
            stdout="needs_iteration third",
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(),
        plan_path=Path("docs/awf-plans/ws_repeat.md"),
        report_path=Path("docs/awf-plans/ws_repeat.conformance.json"),
        latest_error=None,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.repeated_output
    assert evidence.repeated_output_count == 3
    assert evidence.last_report_digest == "digest-a"
    assert evidence.iteration_index == 2


@pytest.mark.unit
def test_classify_conformance_stall_returns_over_duration_when_cumulative_seconds_exceed_threshold() -> (
    None
):
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=900.0,
            report_digest="d-1",
            worktree_changed=True,
            stdout='{"status":"needs_iteration","gaps":["a"]}',
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=1100.0,
            report_digest="d-2",
            worktree_changed=True,
            stdout='{"status":"needs_iteration","gaps":["b"]}',
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(over_duration_seconds=1800),
        plan_path=Path("docs/awf-plans/ws_over.md"),
        report_path=Path("docs/awf-plans/ws_over.conformance.json"),
        latest_error=None,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.over_duration
    assert evidence.elapsed_seconds == pytest.approx(2000.0)
    assert evidence.iteration_index == 1


@pytest.mark.unit
def test_classify_conformance_stall_returns_none_for_progressing_needs_iteration() -> None:
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=60.0,
            report_digest="d-1",
            worktree_changed=True,
            stdout='{"status":"needs_iteration"}',
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=60.0,
            report_digest="d-2",
            worktree_changed=True,
            stdout='{"status":"needs_iteration"}',
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(),
        plan_path=Path("docs/awf-plans/ws_progress.md"),
        report_path=Path("docs/awf-plans/ws_progress.conformance.json"),
        latest_error=None,
    )

    assert evidence is None


@pytest.mark.unit
def test_build_conformance_stall_failure_evidence_is_structured_and_bounded() -> None:
    long_stdout = "x" * (MAX_CONFORMANCE_TEXT_CHARS * 2)
    stall = ConformanceStallEvidence(
        kind=ConformanceStallKind.no_output,
        iteration_index=2,
        elapsed_seconds=620.5,
        no_output_seconds=620.5,
        repeated_output_count=0,
        last_report_digest=None,
        plan_path="docs/awf-plans/ws_e.md",
        report_path="docs/awf-plans/ws_e.conformance.json",
        last_output_excerpt=long_stdout,
    )

    evidence = build_conformance_stall_failure_evidence(
        stall=stall,
        head_sha="abc123",
        base_sha="base000",
        commit_count=2,
        changed_paths=("src/awf/foo.py", "tests/unit/test_foo.py"),
        recovery_action="proceed_to_validation",
    )

    assert evidence["reason_code"] == AGENT_STALLED_IN_CONFORMANCE
    assert evidence["kind"] == ConformanceStallKind.no_output.value
    assert evidence["iteration_index"] == 2
    assert evidence["elapsed_seconds"] == pytest.approx(620.5)
    assert evidence["no_output_seconds"] == pytest.approx(620.5)
    assert evidence["repeated_output_count"] == 0
    assert evidence["plan_path"] == "docs/awf-plans/ws_e.md"
    assert evidence["report_path"] == "docs/awf-plans/ws_e.conformance.json"
    assert len(evidence["last_output_excerpt"]) <= MAX_CONFORMANCE_TEXT_CHARS
    assert evidence["recovery_action"] == "proceed_to_validation"
    salvage = evidence["salvage_hint"]
    assert salvage["plan_path"] == "docs/awf-plans/ws_e.md"
    assert salvage["report_path"] == "docs/awf-plans/ws_e.conformance.json"
    assert salvage["implementation_commit_count"] == 2
    assert salvage["head_sha"] == "abc123"
    assert salvage["base_sha"] == "base000"
    assert salvage["changed_paths"] == ["src/awf/foo.py", "tests/unit/test_foo.py"]


@pytest.mark.unit
def test_build_conformance_stall_failure_evidence_omits_recovery_action_when_none() -> None:
    stall = ConformanceStallEvidence(
        kind=ConformanceStallKind.no_output,
        iteration_index=0,
        elapsed_seconds=0.0,
        no_output_seconds=0.0,
        repeated_output_count=0,
        last_report_digest=None,
        plan_path="docs/awf-plans/ws_e.md",
        report_path="docs/awf-plans/ws_e.conformance.json",
        last_output_excerpt="",
    )

    evidence = build_conformance_stall_failure_evidence(
        stall=stall,
        head_sha=None,
        base_sha=None,
        commit_count=0,
    )

    assert "recovery_action" not in evidence


@pytest.mark.unit
def test_build_conformance_stall_recovery_prompt_steers_agent_to_only_redo_compare() -> None:
    prompt = build_conformance_stall_recovery_prompt(
        task_prompt="Implement the billing retry flow.",
        stall_evidence={
            "kind": ConformanceStallKind.no_output.value,
            "plan_path": "docs/awf-plans/ws_old.md",
            "report_path": "docs/awf-plans/ws_old.conformance.json",
            "iteration_index": 1,
        },
        prior_gaps=("Add regression test", "Wire retry endpoint"),
    )

    assert "docs/awf-plans/ws_old.md" in prompt
    assert "docs/awf-plans/ws_old.conformance.json" in prompt
    assert "Do not modify implementation files" in prompt
    assert "Do not run validation commands" in prompt
    assert "Use existing validation evidence" in prompt
    for command in ("pytest", "ruff", "mypy", "coverage", "npm", "git commit"):
        assert command in prompt
    assert "- Add regression test" in prompt
    assert "- Wire retry endpoint" in prompt
    assert "also print the same JSON object as your final response" in prompt
    assert (
        "only when every remaining gap is missing, stale, or insufficient AWF-owned validation evidence"
        in prompt
    )
    assert (
        '{"status":"satisfied|needs_iteration","summary":"...","gaps":["..."],'
        '"reason_code":"optional reason code"}'
    ) in prompt
    assert "### Original task" in prompt
    assert "Implement the billing retry flow." in prompt


@pytest.mark.unit
def test_classify_conformance_stall_no_output_streak_ignores_stale_report_digest() -> None:
    # The executor reads the report file from disk each iteration, so an
    # iteration with empty stdout/stderr that fails to write a fresh report
    # still surfaces the prior iteration's digest. The classifier must treat
    # an unchanged digest as no-output so the streak builds toward the
    # policy threshold instead of breaking on a stale read.
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=120.0,
            report_digest="digest-x",
            worktree_changed=True,
            stdout="initial conformance output",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=320.0,
            report_digest="digest-x",
            worktree_changed=False,
            stdout="",
            stderr="",
        ),
        _iter_record(
            iteration=2,
            elapsed_seconds=320.0,
            report_digest="digest-x",
            worktree_changed=False,
            stdout="",
            stderr="",
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(no_output_seconds=600),
        plan_path=Path("docs/awf-plans/ws_stale_digest.md"),
        report_path=Path("docs/awf-plans/ws_stale_digest.conformance.json"),
        latest_error=None,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.no_output
    assert evidence.iteration_index == 2
    assert evidence.no_output_seconds == pytest.approx(640.0)
