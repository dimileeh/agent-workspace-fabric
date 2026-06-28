"""Plan/execute/compare lifecycle helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.adapters.base import AgentRunError
from awf.common.commands import CommandResult
from awf.db.enums import AgentRuntime
from awf.runtime import planning as planning_mod
from awf.runtime.planning import (
    AGENT_STALLED_IN_CONFORMANCE,
    MAX_CONFORMANCE_TEXT_CHARS,
    ConformanceIterationRecord,
    ConformanceStallEvidence,
    ConformanceStallKind,
    ConformanceStallPolicy,
    GapKind,
    _evidence_strings,
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
        '{"status":"satisfied|needs_iteration","summary":"...",'
        '"gaps":[{"kind":"awf_validation_evidence","detail":"..."}],'
        '"reason_code":"optional reason code"}'
    ) in prompt
    assert "Each `gaps` item must be an object with a `kind`" in prompt
    for kind in GapKind:
        assert kind.value in prompt
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


@pytest.mark.unit
def test_classify_conformance_stall_breaks_no_output_streak_on_report_file_progress() -> None:
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=400.0,
            report_digest="digest-from-report-file",
            worktree_changed=True,
            stdout="",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=400.0,
            report_digest=None,
            worktree_changed=False,
            stdout="",
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(no_output_seconds=600),
        plan_path=Path("docs/awf-plans/ws_report_progress.md"),
        report_path=Path("docs/awf-plans/ws_report_progress.conformance.json"),
        latest_error=None,
    )

    assert evidence is None


@pytest.mark.unit
def test_classify_conformance_stall_returns_no_output_for_idle_timeout() -> None:
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=120.0,
            report_digest="abc",
            worktree_changed=True,
            stdout="some output",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=620.0,
            report_digest=None,
            worktree_changed=False,
            stdout="" * 0,
            stderr="",
            error_reason_code="AGENT_IDLE_TIMEOUT",
        ),
    ]
    error = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="idle timeout exceeded"),
        reason_code="AGENT_IDLE_TIMEOUT",
    )

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(),
        plan_path=Path("docs/awf-plans/ws_no_output.md"),
        report_path=Path("docs/awf-plans/ws_no_output.conformance.json"),
        latest_error=error,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.no_output
    assert evidence.iteration_index == 1
    assert evidence.elapsed_seconds == pytest.approx(620.0 + 120.0)
    assert evidence.no_output_seconds == pytest.approx(620.0)
    assert evidence.repeated_output_count == 0
    assert evidence.last_report_digest is None
    assert evidence.plan_path == "docs/awf-plans/ws_no_output.md"
    assert evidence.report_path == "docs/awf-plans/ws_no_output.conformance.json"
    assert "idle timeout" in evidence.last_output_excerpt


@pytest.mark.unit
def test_classify_conformance_stall_returns_none_without_history() -> None:
    assert (
        classify_conformance_stall(
            history=[],
            policy=_stall_policy(),
            plan_path=Path("docs/awf-plans/ws_empty.md"),
            report_path=Path("docs/awf-plans/ws_empty.conformance.json"),
            latest_error=None,
        )
        is None
    )


@pytest.mark.unit
def test_classify_conformance_stall_uses_error_stdout_when_stderr_is_empty() -> None:
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=600.0,
            report_digest=None,
            worktree_changed=False,
            stdout="",
            error_reason_code="AGENT_TIMEOUT",
        )
    ]
    error = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="stdout-only failure", stderr=""),
        reason_code="AGENT_TIMEOUT",
    )

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(over_duration_seconds=300),
        plan_path=Path("docs/awf-plans/ws_stdout.md"),
        report_path=Path("docs/awf-plans/ws_stdout.conformance.json"),
        latest_error=error,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.over_duration
    assert "stdout-only failure" in evidence.last_output_excerpt


@pytest.mark.unit
def test_classify_conformance_stall_redacts_secrets_in_last_output_excerpt() -> None:
    # Stall evidence is persisted as durable failure details and surfaced into
    # recovery prompts; raw agent stderr/stdout can include provider tokens or
    # URL credentials. Verify the excerpt is scrubbed before persistence.
    leaked_token = "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    leaked_stderr = f"fatal: could not read Username for 'https://github.com/': {leaked_token}"
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=30.0,
            report_digest=None,
            worktree_changed=False,
            stderr=leaked_stderr,
            error_reason_code="AGENT_IDLE_TIMEOUT",
        ),
    ]
    error = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr=leaked_stderr),
        reason_code="AGENT_IDLE_TIMEOUT",
    )

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(),
        plan_path=Path("docs/awf-plans/ws_redact.md"),
        report_path=Path("docs/awf-plans/ws_redact.conformance.json"),
        latest_error=error,
    )

    assert evidence is not None
    assert leaked_token not in evidence.last_output_excerpt
    assert "<redacted>" in evidence.last_output_excerpt

    payload = build_conformance_stall_failure_evidence(
        stall=evidence,
        head_sha=None,
        base_sha=None,
        commit_count=0,
    )
    assert leaked_token not in payload["last_output_excerpt"]
    assert "<redacted>" in payload["last_output_excerpt"]


@pytest.mark.unit
def test_stall_output_excerpt_falls_back_to_redacted_exception_text() -> None:
    record = ConformanceIterationRecord(
        iteration=1,
        elapsed_seconds=1,
        report_digest=None,
        worktree_changed=False,
        stdout="",
        stderr="",
    )

    excerpt = planning_mod._stall_output_excerpt(  # noqa: SLF001
        RuntimeError("provider failed with sk-test-secret"),
        record,
    )

    assert "<redacted>" in excerpt
    assert "sk-test-secret" not in excerpt


@pytest.mark.unit
def test_evidence_strings_ignores_non_list_payloads() -> None:
    assert _evidence_strings({"gaps": ["not", "a", "list"]}) == ()


@pytest.mark.unit
def test_classify_conformance_stall_returns_over_duration_for_wall_timeout_with_active_output() -> (
    None
):
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=120.0,
            report_digest="abc",
            worktree_changed=True,
            stdout="some output",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=900.0,
            report_digest="def",
            worktree_changed=True,
            stdout="streaming progress chunk",
            stderr="more progress",
            error_reason_code="AGENT_TIMEOUT",
        ),
    ]
    error = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(
            returncode=124, stdout="streaming progress chunk", stderr="more progress"
        ),
        reason_code="AGENT_TIMEOUT",
    )

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(),
        plan_path=Path("docs/awf-plans/ws_wall_timeout.md"),
        report_path=Path("docs/awf-plans/ws_wall_timeout.conformance.json"),
        latest_error=error,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.over_duration
    assert evidence.iteration_index == 1
    assert evidence.elapsed_seconds == pytest.approx(900.0 + 120.0)
    assert evidence.no_output_seconds == 0.0
    assert evidence.repeated_output_count == 0
    assert evidence.last_report_digest == "def"
    assert evidence.plan_path == "docs/awf-plans/ws_wall_timeout.md"
    assert evidence.report_path == "docs/awf-plans/ws_wall_timeout.conformance.json"


@pytest.mark.unit
def test_classify_conformance_stall_returns_no_output_for_idle_timeout_below_policy_threshold() -> (
    None
):
    # AGENT_IDLE_TIMEOUT is an explicit adapter signal (e.g. when an operator
    # sets AWF_AGENT_IDLE_TIMEOUT_SECONDS lower than the loop's
    # policy.no_output_seconds). The classifier must honour it regardless of the
    # loop policy threshold, otherwise the executor re-raises the idle timeout
    # as a generic agent failure instead of AGENT_STALLED_IN_CONFORMANCE.
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=30.0,
            report_digest=None,
            worktree_changed=False,
            stderr="idle timeout exceeded",
            error_reason_code="AGENT_IDLE_TIMEOUT",
        ),
    ]
    error = AgentRunError(
        agent=AgentRuntime.codex,
        result=CommandResult(returncode=124, stdout="", stderr="idle timeout exceeded"),
        reason_code="AGENT_IDLE_TIMEOUT",
    )

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(no_output_seconds=600),
        plan_path=Path("docs/awf-plans/ws_no_output_below.md"),
        report_path=Path("docs/awf-plans/ws_no_output_below.conformance.json"),
        latest_error=error,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.no_output
    assert evidence.iteration_index == 0
    assert evidence.no_output_seconds == pytest.approx(30.0)
    assert "idle timeout" in evidence.last_output_excerpt


@pytest.mark.unit
def test_classify_conformance_stall_returns_no_output_when_stdout_empty_across_consecutive_iterations() -> (
    None
):
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=120.0,
            report_digest="abc",
            worktree_changed=True,
            stdout="some output",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=300.0,
            report_digest=None,
            worktree_changed=False,
            stdout="",
        ),
        _iter_record(
            iteration=2,
            elapsed_seconds=350.0,
            report_digest=None,
            worktree_changed=False,
            stdout="   \n",
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(no_output_seconds=600),
        plan_path=Path("docs/awf-plans/ws_no_output_streak.md"),
        report_path=Path("docs/awf-plans/ws_no_output_streak.conformance.json"),
        latest_error=None,
    )

    assert evidence is not None
    assert evidence.kind == ConformanceStallKind.no_output
    assert evidence.iteration_index == 2
    assert evidence.no_output_seconds == pytest.approx(650.0)
    assert evidence.elapsed_seconds == pytest.approx(770.0)
    assert evidence.repeated_output_count == 0


@pytest.mark.unit
def test_classify_conformance_stall_returns_none_when_stdout_empty_streak_below_no_output_seconds() -> (
    None
):
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=200.0,
            report_digest=None,
            worktree_changed=False,
            stdout="",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=200.0,
            report_digest=None,
            worktree_changed=False,
            stdout="",
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(no_output_seconds=600),
        plan_path=Path("docs/awf-plans/ws_no_output_short.md"),
        report_path=Path("docs/awf-plans/ws_no_output_short.conformance.json"),
        latest_error=None,
    )

    assert evidence is None


@pytest.mark.unit
def test_classify_conformance_stall_breaks_no_output_streak_on_stderr_progress() -> None:
    history = [
        _iter_record(
            iteration=0,
            elapsed_seconds=400.0,
            report_digest=None,
            worktree_changed=False,
            stdout="",
            stderr="progress: still working...",
        ),
        _iter_record(
            iteration=1,
            elapsed_seconds=400.0,
            report_digest=None,
            worktree_changed=False,
            stdout="",
            stderr="",
        ),
    ]

    evidence = classify_conformance_stall(
        history=history,
        policy=_stall_policy(no_output_seconds=600),
        plan_path=Path("docs/awf-plans/ws_stderr_progress.md"),
        report_path=Path("docs/awf-plans/ws_stderr_progress.conformance.json"),
        latest_error=None,
    )

    assert evidence is None
