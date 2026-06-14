"""Pure ``validate``-tool extraction tests (issue #574).

``_leading_executable`` reduces a shell command to its leading PATH-resolvable
executable (or ``None`` when un-probeable), and ``validate_command_probe_targets``
maps a profile's ``validate`` phase to deduped probe targets. Both are pure and
fail-open: an un-probeable leading token is skipped rather than guessed at, so
the downstream probe never false-positives.
"""

from __future__ import annotations

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import _leading_executable, validate_command_probe_targets


def _profile_with_validate(commands: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "validate-profile", "phases": {"validate": commands}}
    )


@pytest.mark.unit
class TestLeadingExecutable:
    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("ruff check .", "ruff"),
            ("python -m ruff check .", "python"),
            ("FOO=bar ruff check .", "ruff"),
            ("FOO=bar BAZ=qux mypy src", "mypy"),
            ("/usr/local/bin/pytest -q", "/usr/local/bin/pytest"),
        ],
    )
    def test_extracts_leading_executable(self, command: str, expected: str) -> None:
        assert _leading_executable(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "cd build",
            ": no-op",
            "echo done",
            "export PATH=/x",
            "FOO=bar",  # only assignments, no executable
            'ruff "check',  # unbalanced quote -> shlex parse failure
            # Shell keywords / builtins leading a compound command: the command
            # cannot be reduced to a single probeable tool, so it fails open
            # rather than treating the keyword (``for``, ``if``) as a fake tool.
            "if [ -f x ]; then ruff; fi",
            "for f in *.py; do ruff $f; done",
            "while read -r line; do echo $line; done",
            "until make; do sleep 1; done",
            "case $x in a) ruff;; esac",
            "exit 0",
            "pwd",
            "printf '%s\\n' done",
            "read -r answer",
            "function lint { ruff; }",
            "command ruff check .",
        ],
    )
    def test_unprobeable_leading_token_returns_none(self, command: str) -> None:
        assert _leading_executable(command) is None


@pytest.mark.unit
class TestValidateCommandProbeTargets:
    def test_empty_validate_phase_has_no_targets(self) -> None:
        assert validate_command_probe_targets(_profile_with_validate([])) == []

    def test_maps_each_validate_command_to_its_tool(self) -> None:
        targets = validate_command_probe_targets(
            _profile_with_validate(["ruff check .", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "ruff check ."),
            ("mypy", "mypy src"),
        ]

    def test_dedupes_by_tool_keeping_first_command(self) -> None:
        # Two ruff commands collapse to a single probe target, keeping the first
        # command as the representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_validate(["ruff check .", "ruff format --check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_unprobeable_commands(self) -> None:
        # A builtin/compound leading token is skipped (fail-open); only the
        # probeable command yields a target.
        targets = validate_command_probe_targets(
            _profile_with_validate(["cd build", "ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]
