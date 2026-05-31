"""Unit tests for ``_classify_post_agent_commit_failure``.

The classifier reads a ``git commit`` ``CommandResult`` and returns a
structured classification that lets the executor distinguish:

  * generic ``git commit`` failures (missing identity, detached HEAD, ...)
  * pre-commit hook failures (``awf-ruff-check``, ``awf-mypy``, ...)
  * format-only pre-commit failures (``awf-ruff-format-check`` reporting
    ``Would reformat: ...``) — the deterministic-repair entrypoint.

These tests run against the helper directly so the contract is locked
in without touching the executor pipeline.
"""

from __future__ import annotations

import pytest

from awf.common.commands import CommandResult
from awf.control.executor.constants import (
    POST_AGENT_COMMIT_FAILED_REASON_CODE,
    POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE,
    POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE,
)
from awf.control.executor.quality_gates import (
    _build_post_agent_precommit_repair_prompt,
    _classify_post_agent_commit_failure,
)


def _commit_result(*, stdout: str = "", stderr: str = "", returncode: int = 1) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.unit
def test_precommit_ruff_check_failure_uses_precommit_reason_code() -> None:
    stdout = (
        "trailing-whitespace.....................................................Passed\n"
        "ruff check..............................................................Failed\n"
        "- hook id: awf-ruff-check\n"
        "- exit code: 1\n"
        "\n"
        "error: Failed to parse src/awf/foo.py:1:1: SyntaxError\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert classification.failed_hooks == ("awf-ruff-check",)
    assert classification.format_repair_files == ()
    assert "awf-ruff-check" in classification.summary


@pytest.mark.unit
def test_precommit_format_only_failure_uses_format_reason_code_and_parses_paths() -> None:
    stdout = (
        "ruff check..............................................................Passed\n"
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n"
        "Would reformat: src/awf/control/executor.py\n"
        "Would reformat: src/awf/control/quality_gates.py\n"
        "2 files would be reformatted\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE
    assert classification.failed_hooks == ("awf-ruff-format-check",)
    assert classification.format_repair_files == (
        "src/awf/control/executor.py",
        "src/awf/control/quality_gates.py",
    )
    assert classification.repair_strategy == "deterministic"
    assert classification.deterministic_hooks == ("awf-ruff-format-check",)
    assert classification.semantic_hooks == ()


@pytest.mark.unit
def test_precommit_eof_only_failure_is_deterministic_repairable() -> None:
    stdout = (
        "fix end of files.......................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n"
        "\n"
        "Fixing docs/awf-plans/ws_06.conformance.json\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert classification.failed_hooks == ("end-of-file-fixer",)
    assert classification.deterministic_hooks == ("end-of-file-fixer",)
    assert classification.semantic_hooks == ()
    assert classification.repair_strategy == "deterministic"
    assert classification.normalizer_repair_files == ("docs/awf-plans/ws_06.conformance.json",)


@pytest.mark.unit
def test_precommit_whitespace_eof_and_ruff_format_are_deterministic_repairable() -> None:
    stdout = (
        "trim trailing whitespace.................................................Failed\n"
        "- hook id: trailing-whitespace\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n"
        "\n"
        "Fixing docs/awf-plans/ws_761.md\n"
        "fix end of files.......................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n"
        "\n"
        "Fixing docs/awf-plans/ws_761.conformance.json\n"
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n"
        "Would reformat: tests/unit/mcp/test_mcp_server.py\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert classification.failed_hooks == (
        "trailing-whitespace",
        "end-of-file-fixer",
        "awf-ruff-format-check",
    )
    assert classification.deterministic_hooks == (
        "trailing-whitespace",
        "end-of-file-fixer",
        "awf-ruff-format-check",
    )
    assert classification.semantic_hooks == ()
    assert classification.repair_strategy == "deterministic"
    assert classification.format_repair_files == ("tests/unit/mcp/test_mcp_server.py",)
    assert classification.normalizer_repair_files == (
        "docs/awf-plans/ws_761.md",
        "docs/awf-plans/ws_761.conformance.json",
    )


@pytest.mark.unit
def test_generic_git_failure_uses_generic_reason_code() -> None:
    stderr = "fatal: empty ident name (for <>) not allowed\n"
    classification = _classify_post_agent_commit_failure(_commit_result(stderr=stderr))

    assert classification.reason_code == POST_AGENT_COMMIT_FAILED_REASON_CODE
    assert classification.failed_hooks == ()
    assert classification.format_repair_files == ()
    assert "fatal" in classification.summary.lower()


@pytest.mark.unit
def test_empty_output_uses_generic_reason_code() -> None:
    classification = _classify_post_agent_commit_failure(_commit_result())

    assert classification.reason_code == POST_AGENT_COMMIT_FAILED_REASON_CODE
    assert classification.failed_hooks == ()
    assert classification.format_repair_files == ()


@pytest.mark.unit
def test_format_plus_other_hook_failure_falls_through_to_precommit_reason() -> None:
    stdout = (
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n"
        "Would reformat: src/awf/control/executor.py\n"
        "1 file would be reformatted\n"
        "mypy....................................................................Failed\n"
        "- hook id: awf-mypy\n"
        "- exit code: 1\n"
        "\n"
        "src/awf/foo.py:42: error: Incompatible types\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert "awf-mypy" in classification.failed_hooks
    assert "awf-ruff-format-check" in classification.failed_hooks
    assert classification.repair_strategy == "agent"
    assert classification.deterministic_hooks == ("awf-ruff-format-check",)
    assert classification.semantic_hooks == ("awf-mypy",)
    # Format repair files are still parsed; the executor will decide not to
    # repair because the failure set is wider than format only.
    assert classification.format_repair_files == ("src/awf/control/executor.py",)


@pytest.mark.unit
def test_ruff_check_plus_ruff_format_uses_agent_repair_not_blind_auto_format() -> None:
    stdout = (
        "ruff check..............................................................Failed\n"
        "- hook id: awf-ruff-check\n"
        "- exit code: 1\n"
        "\n"
        "F401 fix_test.py imported but unused\n"
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n"
        "Would reformat: run_debug.py\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert classification.repair_strategy == "agent"
    assert classification.deterministic_hooks == ("awf-ruff-format-check",)
    assert classification.semantic_hooks == ("awf-ruff-check",)
    assert classification.format_repair_files == ("run_debug.py",)
    assert classification.autofix_repair_files == ()


@pytest.mark.unit
def test_ruff_check_fixable_diagnostics_expose_bounded_autofix_paths() -> None:
    stdout = (
        "ruff check..............................................................Failed\n"
        "- hook id: awf-ruff-check\n"
        "- exit code: 1\n"
        "\n"
        "I001 [*] Import block is un-sorted or un-formatted\n"
        "   --> src/awf/mcp/server.py:13:1\n"
        "UP035 [*] Import from `collections.abc` instead: `Callable`\n"
        "   --> src/awf/mcp/server.py:21:1\n"
        "Found 2 errors.\n"
        "[*] 2 fixable with the `--fix` option.\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert classification.repair_strategy == "agent"
    assert classification.deterministic_hooks == ()
    assert classification.semantic_hooks == ("awf-ruff-check",)
    assert classification.autofix_repair_files == ("src/awf/mcp/server.py",)


@pytest.mark.unit
def test_unknown_hook_uses_agent_repair_not_deterministic_repair() -> None:
    stdout = (
        "custom security scan....................................................Failed\n"
        "- hook id: custom-security-scan\n"
        "- exit code: 1\n"
        "\n"
        "blocked\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_PRECOMMIT_FAILED_REASON_CODE
    assert classification.repair_strategy == "agent"
    assert classification.deterministic_hooks == ()
    assert classification.semantic_hooks == ("custom-security-scan",)


@pytest.mark.unit
def test_format_paths_preserved_verbatim_for_executor_intersection() -> None:
    stdout = (
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n"
        "Would reformat: legacy/untouched.py\n"
        "Would reformat: src/awf/control/executor.py\n"
        "2 files would be reformatted\n"
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))

    assert classification.reason_code == POST_AGENT_COMMIT_FORMAT_REWRITE_NEEDED_REASON_CODE
    assert classification.format_repair_files == (
        "legacy/untouched.py",
        "src/awf/control/executor.py",
    )


@pytest.mark.unit
def test_precommit_repair_prompt_summarizes_large_path_sets() -> None:
    stdout = (
        "fix end of files.......................................................Failed\n"
        "- hook id: end-of-file-fixer\n"
        "- exit code: 1\n"
        "- files were modified by this hook\n"
        "\n"
        + "".join(f"Fixing docs/generated/file_{i}.md\n" for i in range(45))
        + "ruff check..............................................................Failed\n"
        "- hook id: awf-ruff-check\n"
        "- exit code: 1\n"
        "\n"
        "F401 generated.py imported but unused\n"
        "ruff format --check.....................................................Failed\n"
        "- hook id: awf-ruff-format-check\n"
        "- exit code: 1\n"
        "\n" + "".join(f"Would reformat: src/generated/file_{i}.py\n" for i in range(45))
    )
    classification = _classify_post_agent_commit_failure(_commit_result(stdout=stdout))
    staged_paths = [f"src/generated/file_{i}.py" for i in range(85)]

    prompt = _build_post_agent_precommit_repair_prompt(
        classification=classification,
        staged_paths=staged_paths,
    )

    assert "Do not bypass pre-commit" in prompt
    assert "Failed hooks: end-of-file-fixer, awf-ruff-check, awf-ruff-format-check" in prompt
    assert "Normalizer-rewritten paths, if any:" in prompt
    assert "docs/generated/file_0.md" in prompt
    assert "- docs/generated/file_40.md" not in prompt
    assert prompt.count("- ... and 5 more") == 3


class TestNothingToCommitDetection:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout,stderr",
        [
            ("nothing to commit, working tree clean\n", ""),
            ("", "nothing to commit, working tree clean\n"),
            ("On branch awf/x\nnothing to commit, working tree clean\n", ""),
            ("", "On branch awf/x\nnothing to commit, working tree clean\n"),
            ('nothing to commit (create/copy files and use "git add" to track)\n', ""),
            ("working tree clean\n", ""),
            ("", "working tree clean\n"),
        ],
        ids=[
            "stdout-clean",
            "stderr-clean",
            "stdout-with-branch-prefix",
            "stderr-with-branch-prefix",
            "stdout-untracked-hint",
            "stdout-working-tree-clean",
            "stderr-working-tree-clean",
        ],
    )
    def test_is_nothing_to_commit_detects_benign_clean_tree(self, stdout: str, stderr: str) -> None:
        from awf.control.executor.quality_gates import _is_nothing_to_commit

        result = CommandResult(returncode=1, stdout=stdout, stderr=stderr)
        assert _is_nothing_to_commit(result) is True

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout,stderr",
        [
            ("fatal: empty ident name (for <>) not allowed\n", ""),
            ("", "fatal: not a git repository\n"),
            ("some pre-commit output\n", ""),
            ("", ""),
            ("no changes added to commit but untracked files present\n", ""),
        ],
        ids=[
            "empty-ident",
            "not-a-repo",
            "unrelated-output",
            "empty",
            "dirty-tree-no-changes-added",
        ],
    )
    def test_is_nothing_to_commit_rejects_real_errors(self, stdout: str, stderr: str) -> None:
        from awf.control.executor.quality_gates import _is_nothing_to_commit

        result = CommandResult(returncode=1, stdout=stdout, stderr=stderr)
        assert _is_nothing_to_commit(result) is False

    @pytest.mark.unit
    def test_is_nothing_to_commit_returns_false_on_ok(self) -> None:
        from awf.control.executor.quality_gates import _is_nothing_to_commit

        result = CommandResult(
            returncode=0, stdout="", stderr="nothing to commit, working tree clean\n"
        )
        assert _is_nothing_to_commit(result) is False

    @pytest.mark.unit
    def test_is_nothing_to_commit_returns_false_with_pre_commit_hook_failure(self) -> None:
        from awf.control.executor.quality_gates import _is_nothing_to_commit

        result = CommandResult(
            returncode=1,
            stdout="",
            stderr="- hook id: ruff\nnothing to commit, working tree clean\n",
        )
        assert _is_nothing_to_commit(result) is False

    @pytest.mark.unit
    def test_nothing_to_commit_classification_reason_code(self) -> None:
        classification = _classify_post_agent_commit_failure(
            _commit_result(stderr="nothing to commit, working tree clean\n")
        )
        assert classification.reason_code == POST_AGENT_COMMIT_FAILED_REASON_CODE
        assert classification.repair_strategy == "none"
