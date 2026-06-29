"""Coverage final-gate probe target tests."""

from __future__ import annotations

import pytest

from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation import validate_command_probe_targets


def _profile_with_coverage_gate(
    *,
    validate: list[object],
    final_gate: str = "coverage",
    coverage_command: object | None = "coverage run -m pytest",
) -> WorkspaceProfile:
    coverage: dict[str, object] = {}
    if coverage_command is not None:
        coverage["command"] = coverage_command
    return WorkspaceProfile.model_validate(
        {
            "name": "validate-profile",
            "phases": {"validate": validate},
            "validation": {
                "strategy": {"final_gate": final_gate},
                "coverage": coverage,
            },
        }
    )


@pytest.mark.unit
class TestValidateCoverageGateProbeTargets:
    def test_probes_coverage_gate_command_after_validate(self) -> None:
        # When the profile uses the local coverage final gate
        # (``validation.strategy.final_gate: coverage`` with a coverage command),
        # PR-monitor pre-push validation runs that command after the validate
        # phase, so its toolchain must be probed too — otherwise an adopted PR
        # whose setup forgot to install ``coverage`` slips past the handoff and
        # dies 127 later in ``monitoring_pr``. The coverage target comes last,
        # matching runtime execution order.
        targets = validate_command_probe_targets(
            _profile_with_coverage_gate(validate=["ruff check ."])
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "ruff check ."),
            ("coverage", "coverage run -m pytest"),
        ]

    def test_dedupes_coverage_gate_tool_shared_with_validate(self) -> None:
        # A tool shared between a validate command and the coverage gate command
        # collapses to a single probe target, keeping the first (validate) command
        # as the representative for the operator message.
        targets = validate_command_probe_targets(
            _profile_with_coverage_gate(
                validate=["coverage run -m pytest"],
                coverage_command="coverage report",
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("coverage", "coverage run -m pytest"),
        ]

    def test_skips_coverage_command_when_final_gate_not_coverage(self) -> None:
        # A coverage command is configured but the final gate is ``none``, so the
        # gate never runs; probing the coverage tool would falsely fail the handoff
        # for a profile that never executes it.
        targets = validate_command_probe_targets(
            _profile_with_coverage_gate(validate=["ruff check ."], final_gate="none")
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_no_coverage_command_yields_only_validate_targets(self) -> None:
        # ``final_gate: coverage`` with no coverage command cannot run the gate
        # (``_should_run_local_coverage`` is False), so only the validate tool is
        # probed.
        targets = validate_command_probe_targets(
            _profile_with_coverage_gate(validate=["ruff check ."], coverage_command=None)
        )
        assert [(t.tool, t.command) for t in targets] == [("ruff", "ruff check .")]

    def test_probes_coverage_command_regardless_of_required_flag(self) -> None:
        # The coverage final gate runs whenever ``final_gate: coverage`` and a
        # coverage command are set — the ``required`` flag does not gate it — so a
        # ``required: false`` coverage command is still probed (its 127 still
        # blocks pre-push validation).
        targets = validate_command_probe_targets(
            _profile_with_coverage_gate(
                validate=["ruff check ."],
                coverage_command={"command": "coverage run -m pytest", "required": False},
            )
        )
        assert [(t.tool, t.command) for t in targets] == [
            ("ruff", "ruff check ."),
            ("coverage", "coverage run -m pytest"),
        ]
