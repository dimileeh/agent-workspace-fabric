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
from awf.runtime.validation import (
    _leading_executable,
    _leading_executables,
    validate_command_probe_targets,
)


def _profile_with_validate(commands: list[str]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "validate-profile", "phases": {"validate": commands}}
    )


def _profile_with_validate_objects(commands: list[dict[str, object]]) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {"name": "validate-profile", "phases": {"validate": commands}}
    )


def _profile_with_refresh_and_validate(
    *,
    pre_validation_refresh: list[object],
    validate: list[object],
) -> WorkspaceProfile:
    return WorkspaceProfile.model_validate(
        {
            "name": "validate-profile",
            "phases": {"validate": validate},
            "database": {"pre_validation_refresh": pre_validation_refresh},
        }
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
            # A YAML block command opening with a shell comment line runs the
            # real command under ``sh -lc`` (which ignores ``#`` comments), so
            # the probe must look past the comment to the actual executable.
            ("# run lint\nruff check .", "ruff"),
            ("  # leading comment\nmypy src", "mypy"),
            ("ruff check . # trailing comment", "ruff"),
            # A required validate block guarded by a leading shell-option command
            # (``set -e``, ``set -euo pipefail``, ``shopt -s globstar``) runs the
            # real tool after the guard under ``sh -lc``; the probe must look past
            # the guard statement to the tool that follows, otherwise a missing
            # toolchain slips past the handoff and only fails later in
            # ``monitoring_pr`` instead of as PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
            ("set -e; ruff check .", "ruff"),
            ("set -euo pipefail && ruff check .", "ruff"),
            ("set -euo pipefail\nruff check .", "ruff"),
            ("shopt -s globstar; ruff check .", "ruff"),
            ("umask 022; mypy src", "mypy"),
            # Multiple stacked guards are all skipped to reach the real tool.
            ("set -e; set -x; ruff check .", "ruff"),
            # A guard followed by an env-assignment prefix still probes the tool.
            ("set -e; FOO=bar ruff check .", "ruff"),
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
            "[[ -f pyproject.toml ]] && ruff check .",
            "select f in *.py; do ruff $f; done",
            "time ruff check .",
            "exit 0",
            "pwd",
            "printf '%s\\n' done",
            "read -r answer",
            "function lint { ruff; }",
            "command ruff check .",
            # A leading ``PATH=...`` env assignment is what makes the executable
            # resolvable, but the shared-PATH ``command -v`` probe cannot replay a
            # per-command PATH prefix, so the command fails open rather than being
            # falsely reported PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
            "PATH=/workspace/node_modules/.bin:$PATH eslint .",
            "FOO=bar PATH=/opt/bin:$PATH mypy src",
            # A leading subshell opener is shell grouping the runner executes
            # under ``sh -lc``; ``shlex`` glues the ``(`` to the first word
            # (``(cd``) or keeps it standalone (``(``), so probing it would
            # falsely report PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. Fail open.
            "(cd frontend && npm test)",
            "( cd frontend && npm test )",
            # A leading token that names the executable via shell expansion —
            # tilde or parameter/command substitution — is expanded by the
            # ``sh -lc`` the real runner uses, but ``shlex`` keeps the literal
            # token and the probe passes it quoted to ``command -v "$t"`` where
            # it is not re-expanded. Probing it would falsely report
            # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED, so fail open.
            "~/bin/ruff check .",
            "$HOME/.local/bin/ruff check .",
            "${HOME}/bin/ruff check .",
            "FOO=bar $HOME/.local/bin/mypy src",
            "`which ruff` check .",
            # A command that is nothing but a shell comment reduces to no tokens
            # under ``sh -lc``; there is no executable to probe, so fail open.
            "# just a note, no command here",
            # A leading shell *guard* (``set -e``) is skipped, but only guard
            # statements are — a leading ``cd`` changes the working directory the
            # tool resolves against, so a ``cd``-prefixed sequence keeps the
            # existing fail-open behavior rather than probing the later tool under
            # the wrong directory.
            "cd build; ruff check .",
            # A guard with no following command names no tool, so fail open.
            "set -euo pipefail",
            "shopt -s globstar",
        ],
    )
    def test_unprobeable_leading_token_returns_none(self, command: str) -> None:
        assert _leading_executable(command) is None

    def test_non_path_assignments_still_probe_the_executable(self) -> None:
        # Only a PATH-binding assignment forces fail-open; other env assignments
        # leave the executable probeable under the shared PATH.
        assert _leading_executable("PYTHONPATH=/x FOO=bar ruff check .") == "ruff"


@pytest.mark.unit
class TestLeadingExecutables:
    def test_single_command_yields_one_tool(self) -> None:
        assert _leading_executables("ruff check .") == ["ruff"]

    def test_compound_command_yields_every_chained_tool(self) -> None:
        # A single validate command that chains tools with ``&&`` must be probed
        # for *all* of them — otherwise a later chained tool that is off PATH
        # slips past the handoff and fails later in ``monitoring_pr`` instead of
        # early with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        assert _leading_executables("ruff check . && mypy src") == ["ruff", "mypy"]

    def test_chains_with_semicolons_and_pipes_collect_each_statement_head(self) -> None:
        # ``;`` and ``||`` start new statements like ``&&`` does; a pipeline's
        # leading token is still the tool to probe for that statement.
        assert _leading_executables("ruff check .; black --check . || mypy src") == [
            "ruff",
            "black",
            "mypy",
        ]

    def test_leading_guard_is_skipped_before_chained_tools(self) -> None:
        assert _leading_executables("set -euo pipefail && ruff check . && mypy src") == [
            "ruff",
            "mypy",
        ]

    def test_unprobeable_statement_stops_collection_keeping_earlier_tools(self) -> None:
        # A directory-changing ``cd`` ends collection because tools after it
        # resolve against a different directory, but the tool collected before it
        # is still probed rather than discarded.
        assert _leading_executables("ruff check . && cd build && mypy") == ["ruff"]

    def test_leading_unprobeable_token_yields_no_tools(self) -> None:
        assert _leading_executables("cd build && ruff check .") == []

    def test_comment_only_command_yields_no_tools(self) -> None:
        assert _leading_executables("# just a note, no command here") == []


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

    def test_compound_command_probes_every_chained_tool(self) -> None:
        # A single required validate command chaining tools with ``&&`` yields a
        # probe target for each tool, all keeping the full command as the
        # representative for the operator message, so a later chained tool that
        # is off PATH fails the handoff early with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED instead of slipping through.
        targets = validate_command_probe_targets(
            _profile_with_validate(["ruff check . && mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "ruff check . && mypy src"),
            ("mypy", "ruff check . && mypy src"),
        ]

    def test_dedupes_chained_tool_shared_across_commands(self) -> None:
        # A tool that appears both inside a compound command and as a standalone
        # command collapses to a single probe target, keeping the first command
        # that introduced it as the representative.
        targets = validate_command_probe_targets(
            _profile_with_validate(["ruff check . && mypy src", "mypy --strict src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "ruff check . && mypy src"),
            ("mypy", "ruff check . && mypy src"),
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

    def test_skips_path_modifying_commands(self) -> None:
        # A command whose env prefix binds PATH is what makes its executable
        # resolvable; the shared-PATH probe cannot replay that, so it is skipped
        # (fail-open) rather than failing the handoff with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. The plain command still probes.
        targets = validate_command_probe_targets(
            _profile_with_validate(
                ["PATH=/workspace/node_modules/.bin:$PATH eslint .", "ruff check ."]
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_subshell_wrapped_commands(self) -> None:
        # A subshell-wrapped command (``(cd frontend && npm test)``) is shell
        # grouping the runner executes under ``sh -lc``; its leading token is
        # the glued ``(cd``, not a probeable tool, so it fails open rather than
        # failing the handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. The
        # plain command still probes.
        targets = validate_command_probe_targets(
            _profile_with_validate(["(cd frontend && npm test)", "ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_shell_expanded_leading_token_commands(self) -> None:
        # A command whose leading executable is named via shell expansion
        # (``$HOME/.local/bin/ruff``) resolves under the runner's ``sh -lc`` but
        # not under the quoted ``command -v "$t"`` probe, so it fails open rather
        # than failing the handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        # The plain command still probes.
        targets = validate_command_probe_targets(
            _profile_with_validate(["$HOME/.local/bin/ruff check .", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [("mypy", "mypy src")]

    def test_probes_tool_after_leading_comment_line(self) -> None:
        # A YAML block command opening with a shell comment line (``# run lint``)
        # runs the real command under ``sh -lc``, which ignores the comment, so
        # the probe must target the actual tool rather than the literal ``#`` —
        # otherwise a valid command falsely fails the handoff with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.
        targets = validate_command_probe_targets(
            _profile_with_validate(["# run lint\nruff check .", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "# run lint\nruff check ."),
            ("mypy", "mypy src"),
        ]

    def test_probes_tool_after_leading_shell_guard(self) -> None:
        # A required validate command guarded by a leading ``set -e`` (or
        # ``set -euo pipefail``) runs the real tool after the guard under
        # ``sh -lc``; the probe must look past the guard so a missing toolchain
        # is caught at handoff as PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED rather
        # than slipping through to fail later in ``monitoring_pr``. The full
        # command is kept as the representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_validate(["set -euo pipefail; ruff check .", "mypy src"])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "set -euo pipefail; ruff check ."),
            ("mypy", "mypy src"),
        ]

    def test_skips_guard_only_command_with_no_following_tool(self) -> None:
        # A validate command that is only a shell guard names no tool, so it
        # yields no probe target (fail-open) rather than reporting the guard
        # itself as a missing toolchain.
        targets = validate_command_probe_targets(
            _profile_with_validate(["set -euo pipefail", "ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_advisory_required_false_commands(self) -> None:
        # An advisory (``required: false``) validate command is not probed: its
        # missing tool is recorded non-blocking by the runner, so it must not fail
        # the handoff with PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. Only the
        # required command yields a probe target.
        targets = validate_command_probe_targets(
            _profile_with_validate_objects(
                [
                    {"command": "advisory-lint .", "required": False},
                    {"command": "ruff check ."},
                ]
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_probes_pre_validation_refresh_tools_before_validate(self) -> None:
        # ``profile_phase_command_plan`` prepends ``database.pre_validation_refresh``
        # commands as required DB-refresh gates whenever the validate phase runs, so
        # a refresh hook like ``alembic upgrade head`` whose tool setup did not
        # install must be probed too — otherwise the missing tool slips past the
        # handoff and dies 127 later during pre-push validation. Refresh targets
        # come first, matching runtime execution order.
        targets = validate_command_probe_targets(
            _profile_with_refresh_and_validate(
                pre_validation_refresh=["alembic upgrade head"],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("alembic", "alembic upgrade head"),
            ("ruff", "ruff check ."),
        ]

    def test_dedupes_refresh_tool_shared_with_validate(self) -> None:
        # A tool that appears in both a refresh hook and a validate command
        # collapses to a single probe target, keeping the first (refresh) command
        # as the representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_refresh_and_validate(
                pre_validation_refresh=["python -m alembic upgrade head"],
                validate=["python -m pytest -q"],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("python", "python -m alembic upgrade head"),
        ]

    def test_skips_advisory_required_false_refresh_commands(self) -> None:
        # An advisory (``required: false``) refresh hook is non-blocking in the
        # runner, so it must not fail the handoff with
        # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. Only the required validate
        # command yields a probe target.
        targets = validate_command_probe_targets(
            _profile_with_refresh_and_validate(
                pre_validation_refresh=[{"command": "alembic upgrade head", "required": False}],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_skips_unprobeable_refresh_commands(self) -> None:
        # A refresh hook whose leading token is un-probeable (a ``psql`` heredoc
        # guarded by ``cd``) fails open like any other validate command rather than
        # reporting a false missing toolchain.
        targets = validate_command_probe_targets(
            _profile_with_refresh_and_validate(
                pre_validation_refresh=["cd db"],
                validate=["ruff check ."],
            )
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]
