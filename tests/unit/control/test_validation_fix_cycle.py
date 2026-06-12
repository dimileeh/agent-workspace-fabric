"""Tests for the validation fix-cycle helpers.

Scope: the *pure* parts of the fix-cycle — prompt composition, output
tailing, context dataclass. The executor-level loop that consumes these
is covered in ``test_executor_validation_fix_cycle.py`` with the normal
executor test harness.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.control.validation_fix_cycle import (
    ValidationFixContext,
    build_fix_prompt,
    read_output_tail,
)


class TestReadOutputTail:
    """The fix prompt embeds tails of the failing command's stdout +
    stderr. The tail length matters for context budget — too short and
    the coding CLI lacks signal, too long and we burn tokens."""

    @pytest.mark.unit
    def test_short_file_returned_whole(self, tmp_path: Path) -> None:
        path = tmp_path / "stderr"
        path.write_text("line1\nline2\nline3\n")

        assert read_output_tail(path, max_chars=1000) == "line1\nline2\nline3\n"

    @pytest.mark.unit
    def test_long_file_truncated_to_max_chars_from_end(self, tmp_path: Path) -> None:
        """Tail — we keep the LAST max_chars, not the first. The failure
        signal is almost always at the end of a test log."""
        path = tmp_path / "stderr"
        path.write_text("a" * 5000 + "TRAIL")

        tail = read_output_tail(path, max_chars=100)

        assert len(tail) == 100
        assert tail.endswith("TRAIL")

    @pytest.mark.unit
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """A failed command should always produce its artifact, but
        handle the race gracefully."""
        path = tmp_path / "does-not-exist"

        assert read_output_tail(path, max_chars=1000) == ""

    @pytest.mark.unit
    def test_empty_file_returns_empty_string(self, tmp_path: Path) -> None:
        path = tmp_path / "empty"
        path.write_text("")

        assert read_output_tail(path, max_chars=1000) == ""

    @pytest.mark.unit
    def test_binary_garbage_falls_back_to_best_effort(self, tmp_path: Path) -> None:
        """Test frameworks sometimes dump bytes that aren't valid UTF-8
        (terminal escape codes, binary payloads). We tolerate those and
        return something readable rather than crashing the fix loop."""
        path = tmp_path / "stderr"
        path.write_bytes(b"\xff\xfe\x00valid ascii tail")

        tail = read_output_tail(path, max_chars=1000)

        assert "valid ascii tail" in tail


class TestBuildFixPrompt:
    """The fix prompt is the whole reason the coding CLI gets a second
    chance. Must be specific enough that it reviews the test output
    before blindly editing, but short enough not to crowd the CLI's
    context window."""

    def _ctx(self, **overrides) -> ValidationFixContext:
        defaults = {
            "failed_command": "pytest -q",
            "returncode": 1,
            "stdout_tail": "FAILED tests/unit/foo.py::test_bar",
            "stderr_tail": "AssertionError: assert 1 == 2",
            "pass_number": 1,
            "total_passes": 5,
            "test_commands": ["pip install -e .", "pytest -q", "ruff check ."],
        }
        defaults.update(overrides)
        return ValidationFixContext(**defaults)

    @pytest.mark.unit
    def test_mentions_the_failing_command(self) -> None:
        prompt = build_fix_prompt(self._ctx())
        assert "pytest -q" in prompt

    @pytest.mark.unit
    def test_includes_returncode(self) -> None:
        prompt = build_fix_prompt(self._ctx(returncode=42))
        assert "42" in prompt

    @pytest.mark.unit
    def test_embeds_stdout_and_stderr_tails(self) -> None:
        prompt = build_fix_prompt(
            self._ctx(
                stdout_tail="MYSTDOUT_UNIQUE_MARKER",
                stderr_tail="MYSTDERR_UNIQUE_MARKER",
            )
        )
        assert "MYSTDOUT_UNIQUE_MARKER" in prompt
        assert "MYSTDERR_UNIQUE_MARKER" in prompt

    @pytest.mark.unit
    def test_tells_agent_to_fix_not_re_implement(self) -> None:
        """The single most important contract: the prompt must orient
        the CLI toward fixing the reported failure, not wiping the
        existing work to re-implement from scratch."""
        prompt = build_fix_prompt(self._ctx()).lower()
        assert "fix" in prompt
        # Negation pressure — don't invite a rewrite.
        assert "do not" in prompt or "don't" in prompt

    @pytest.mark.unit
    def test_lists_all_validation_commands(self) -> None:
        """The agent needs to know which commands will run on the next
        validation pass — otherwise it might fix only the one that
        failed this pass and break another."""
        prompt = build_fix_prompt(self._ctx())
        assert "pip install -e ." in prompt
        assert "pytest -q" in prompt
        assert "ruff check ." in prompt

    @pytest.mark.unit
    def test_shows_attempt_counter(self) -> None:
        """Telemetry for the CLI: "this is attempt 3 of 5" helps it
        decide whether to be conservative or experimental."""
        prompt = build_fix_prompt(self._ctx(pass_number=3, total_passes=5))
        assert "3" in prompt
        assert "5" in prompt

    @pytest.mark.unit
    def test_handles_empty_output_tails(self) -> None:
        """A command can fail with no output (OOM kill, SIGTERM). The
        prompt must still be well-formed and not crash."""
        prompt = build_fix_prompt(self._ctx(stdout_tail="", stderr_tail=""))
        assert "pytest -q" in prompt
        # No stray empty-placeholder artifacts.
        assert "None" not in prompt

    @pytest.mark.unit
    def test_omits_extra_blank_line_when_no_failing_test_evidence(self) -> None:
        prompt = build_fix_prompt(self._ctx(test_commands=["pytest -q", "ruff check ."]))

        assert "  - pytest -q\n  - ruff check .\n\nQuality-gate policy:" in prompt
        assert "  - pytest -q\n  - ruff check .\n\n\nQuality-gate policy:" not in prompt

    @pytest.mark.unit
    def test_forbids_lowering_quality_gates_on_coverage_failure(self) -> None:
        prompt = build_fix_prompt(
            self._ctx(
                reason_code="COVERAGE_BELOW_THRESHOLD",
                coverage_percent=88.07,
                coverage_minimum_percent=99.0,
                baseline_coverage_percent=88.07,
            )
        ).lower()

        assert "do not lower" in prompt
        assert "do not edit quality-gate configuration" in prompt
        assert "add meaningful tests" in prompt
        assert "pre-agent base-branch coverage: 88.07%" in prompt
        assert "required coverage: 99.00%" in prompt

    @pytest.mark.unit
    def test_retry_prompt_names_failing_tests_instead_of_coverage_work_when_threshold_met(
        self,
    ) -> None:
        prompt = build_fix_prompt(
            self._ctx(
                failed_command="pytest --cov=awf --cov-report=term",
                reason_code="PYTEST_TEST_FAILURE",
                coverage_percent=99.2,
                coverage_minimum_percent=99.0,
                failing_test_node_ids=("tests/unit/test_widget.py::test_handles_edges",),
                failing_test_evidence=(
                    "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError",
                ),
            )
        )

        assert "tests/unit/test_widget.py::test_handles_edges" in prompt
        assert "fix the failing pytest tests first" in prompt.lower()
        assert "coverage already meets the configured threshold" in prompt.lower()
        assert "raise coverage" not in prompt.lower()
        assert "add meaningful tests for the relevant code paths" not in prompt.lower()

    @pytest.mark.unit
    def test_retry_prompt_prioritizes_failing_tests_before_coverage_work_when_both_fail(
        self,
    ) -> None:
        prompt = build_fix_prompt(
            self._ctx(
                failed_command="pytest --cov=awf --cov-report=term",
                reason_code="COVERAGE_BELOW_THRESHOLD",
                coverage_percent=98.7,
                coverage_minimum_percent=99.0,
                failing_test_node_ids=("tests/unit/test_widget.py::test_handles_edges",),
                failing_test_evidence=(
                    "FAILED tests/unit/test_widget.py::test_handles_edges - AssertionError",
                ),
            )
        ).lower()

        assert "fix the failing pytest tests first, then revisit coverage" in prompt
        assert "tests/unit/test_widget.py::test_handles_edges" in prompt

    @pytest.mark.unit
    def test_instructs_tag_prefix_for_self_committed_fix_when_tag_present(self) -> None:
        """A tagged workspace whose agent self-commits the fix leaves a clean
        worktree, so AWF's tagging fallback commit is skipped. The prompt must
        tell the agent to prefix its own commit subjects with the task tag so
        the pushed fix-pass commit keeps its Jira link (PRRT_kwDOSJAM6s6I-vMm)."""
        prompt = build_fix_prompt(self._ctx(task_tag="PROJ-123"))

        assert "task tag `PROJ-123`" in prompt
        assert "links to its tracking issue" in prompt
        # Idempotent: an already-tagged subject is left alone.
        assert "do not add it again" in prompt

    @pytest.mark.unit
    def test_omits_tag_instruction_when_tag_absent(self) -> None:
        prompt = build_fix_prompt(self._ctx())

        assert "task tag" not in prompt
        assert "links to its tracking issue" not in prompt

    @pytest.mark.unit
    def test_retry_prompt_treats_provider_fail_under_as_coverage_work(self) -> None:
        prompt = build_fix_prompt(
            self._ctx(
                failed_command="pytest --cov=awf --cov-report=term-missing",
                reason_code="COVERAGE_FAIL_UNDER_NOT_REACHED",
                coverage_percent=99.0,
                coverage_minimum_percent=99.0,
            )
        ).lower()

        assert "add meaningful tests" in prompt
        assert "coverage provider reported fail-under was not reached" in prompt
        assert "coverage already meets the configured threshold" not in prompt


class TestValidationFixContext:
    @pytest.mark.unit
    def test_is_hashable_for_logging_structure(self) -> None:
        """Structured-logging callers may compare contexts as dict keys
        (dedupe retry events); frozen dataclass guarantees this."""
        ctx = ValidationFixContext(
            failed_command="pytest",
            returncode=1,
            stdout_tail="",
            stderr_tail="",
            pass_number=1,
            total_passes=5,
            test_commands=("pytest",),  # must be tuple for hashability
        )
        # Assignment keeps the lint rule happy (B018) while still
        # asserting the dataclass can be used as a dict key.
        _ = {ctx: "x"}
