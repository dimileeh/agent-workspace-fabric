"""Tests for shared command evidence helpers."""

from awf.common.command_evidence import append_command_evidence


def test_append_command_evidence_keeps_non_empty_streams() -> None:
    evidence: list[str] = []

    append_command_evidence(evidence, stdout="stdout", stderr="")
    append_command_evidence(evidence, stdout="", stderr="stderr")

    assert evidence == ["stdout", "stderr"]


def test_append_command_evidence_tolerates_missing_evidence_sink() -> None:
    append_command_evidence(None, stdout="ignored", stderr="ignored")
