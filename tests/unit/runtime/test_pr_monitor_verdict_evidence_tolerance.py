"""Evidence-gated tolerance of output around a verdict record (#1074).

``test_pr_monitor_verdict_protocol.py`` pins the strict contract: the record must
be the final non-empty line and the only record. That contract is unchanged and
stays the first check. This file pins the separate seam layered on top of it.
When the strict parse fails, the last exact, line-anchored record may still be
accepted, but only if it is ``FIXED`` *and* AWF verified item-scoped fix evidence
for it. A correction attempt that prints no record at all inherits attempt 0's
evidence-refused ``FIXED`` so the caller's existing evidence gates can judge it.
Everything else raises the strict protocol error exactly as before.
"""

from __future__ import annotations

import pytest
import structlog

from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AGENT_VERDICT_PROTOCOL_VIOLATION,
    AgentVerdictProtocolError,
    VerdictResult,
)
from awf.runtime.pr_monitor_runner.comment_verdict_evidence_tolerance import (
    parse_verdict_with_fix_evidence,
)
from awf.runtime.pr_monitor_runner.helpers_verdict import _last_valid_verdict_record
from tests.unit.runtime._verdict_trailing_output_fixtures import (
    F1,
    F1_REASON,
    F2,
    F2_REASON,
    F3,
    F4,
    F4_REASON,
)

_INHERITED = VerdictResult(verdict="fix_committed", reason="attempt 0 claimed the fix")

# Record lines that the strict contract never counts as records. The tolerant
# locator uses the very same record syntax, so none of them may count there either
# — even when they are the only thing that looks like a verdict (scenario 12).
_NOT_RECORDS = [
    "awf-verdict: FIXED: wrong prefix case",
    "AWF-VERDICT: fixed: wrong label case",
    "AWF-VERDICT: NEEDS HUMAN: wrong separator",
    "AWF-VERDICT: NEEDS_HUMAN : wrong spacing",
    "AWF-VERDICT:FIXED: missing separator space",
    "AWF-VERDICT: FIXED:missing reason separator space",
    "AWF-VERDICT: UNKNOWN: no such disposition",
    "AWF-VERDICT: FIXED:",
    "AWF-VERDICT: FIXED:   ",
    " AWF-VERDICT: FIXED: leading indentation",
    "\tAWF-VERDICT: FIXED: leading indentation",
    "**AWF-VERDICT: FIXED:** provider decoration",
    "**AWF-VERDICT: FIXED: provider decoration**",
    "- AWF-VERDICT: FIXED: list decoration",
    "> AWF-VERDICT: FIXED: quote decoration",
    "`AWF-VERDICT: FIXED: inline code`",
    "<code>AWF-VERDICT: FIXED: html wrapper</code>",
    "Done. AWF-VERDICT: FIXED: mentioned mid-line",
]


def _trailing(record_line: str) -> str:
    return f"analysis first\n{record_line}\nstill waiting on the test shards\n"


# --- _last_valid_verdict_record: the pure locator ------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (F1, VerdictResult(verdict="fix_committed", reason=F1_REASON)),
        (F2, VerdictResult(verdict="fix_committed", reason=F2_REASON)),
        (F4, VerdictResult(verdict="false_positive", reason=F4_REASON)),
        (
            "AWF-VERDICT: DEFER: first record\nAWF-VERDICT: FALSE POSITIVE: second record",
            VerdictResult(verdict="false_positive", reason="second record"),
        ),
        (
            "AWF-VERDICT: FIXED: the real fix\nAWF-VERDICT: FIXED: <one-sentence summary>\n",
            VerdictResult(verdict="fix_committed", reason="the real fix"),
        ),
        (
            "AWF-VERDICT: DEFER: track it\nAWF-VERDICT: FIXED: <one-sentence summary>",
            VerdictResult(verdict="defer", reason="track it"),
        ),
        (
            "prose\r\nAWF-VERDICT: FIXED: crlf fix\r\ntrailing chatter\r\n\r\n",
            VerdictResult(verdict="fix_committed", reason="crlf fix"),
        ),
        (
            "AWF-VERDICT: NEEDS_HUMAN: choose the policy",
            VerdictResult(verdict="needs_human", reason="choose the policy"),
        ),
    ],
)
def test_locator_returns_the_last_valid_record(stdout: str, expected: VerdictResult) -> None:
    assert _last_valid_verdict_record(stdout) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        "",
        "\n\n",
        F3,
        "AWF-VERDICT: FIXED: <one-sentence summary>\nchatter",
        "AWF-VERDICT: FIXED: ...\nchatter",
        "AWF-VERDICT: NEEDS_HUMAN: ghp_exampletokenABCDEF0123456789\nchatter",
    ],
)
def test_locator_finds_nothing_without_a_valid_record(stdout: str) -> None:
    assert _last_valid_verdict_record(stdout) is None


@pytest.mark.unit
@pytest.mark.parametrize("record_line", _NOT_RECORDS)
def test_locator_never_counts_decorated_or_malformed_lines(record_line: str) -> None:
    assert _last_valid_verdict_record(record_line) is None
    assert _last_valid_verdict_record(_trailing(record_line)) is None


@pytest.mark.unit
def test_locator_redacts_and_bounds_the_reason() -> None:
    secret = "ghp_exampletokenABCDEF0123456789"

    result = _last_valid_verdict_record(
        f"AWF-VERDICT: FIXED: {secret} {'x' * 600}\nthe shards are still running"
    )

    assert result is not None
    assert result.verdict == "fix_committed"
    assert result.reason is not None
    assert secret not in result.reason
    assert len(result.reason) <= 500


@pytest.mark.unit
def test_locator_does_not_split_or_copy_all_stdout() -> None:
    class NoSplitString(str):
        def splitlines(self, *args: object, **kwargs: object) -> list[str]:
            raise AssertionError("the record scan must not split all stdout")

    stdout = NoSplitString("x" * 100_000 + "\nAWF-VERDICT: DEFER: later\nchatter\n")

    assert _last_valid_verdict_record(stdout) == VerdictResult(verdict="defer", reason="later")


# --- parse_verdict_with_fix_evidence: strict first, then the evidence gate ------


def _parse(
    stdout: str,
    *,
    fix_evidence: bool,
    inherited: VerdictResult | None = None,
) -> VerdictResult:
    return parse_verdict_with_fix_evidence(
        stdout,
        workspace_id="ws_tolerance",
        fix_evidence=fix_evidence,
        inherited=inherited,
    )


@pytest.mark.unit
def test_the_seam_is_reexported_from_comment_verdict() -> None:
    assert comment_verdict.parse_verdict_with_fix_evidence is parse_verdict_with_fix_evidence


@pytest.mark.unit
@pytest.mark.parametrize("fix_evidence", [True, False])
@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            "analysis first\nAWF-VERDICT: FIXED: committed the guard",
            VerdictResult(verdict="fix_committed", reason="committed the guard"),
        ),
        (F2, VerdictResult(verdict="fix_committed", reason=F2_REASON)),
        (
            "AWF-VERDICT: FALSE POSITIVE: duplicate of another thread",
            VerdictResult(verdict="false_positive", reason="duplicate of another thread"),
        ),
    ],
)
def test_strict_records_pass_through_whatever_the_evidence(
    stdout: str,
    expected: VerdictResult,
    fix_evidence: bool,
) -> None:
    """The strict parse stays the first check; the caller's gates judge its FIXED."""
    with structlog.testing.capture_logs() as captured:
        assert _parse(stdout, fix_evidence=fix_evidence, inherited=_INHERITED) == expected

    assert captured == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("stdout", "expected_reason"),
    [
        (F1, F1_REASON),
        (f"{F2}\nStill waiting on the test shards.\n", F2_REASON),
        (
            "AWF-VERDICT: DEFER: track separately\nAWF-VERDICT: FIXED: fixed after all\nchatter",
            "fixed after all",
        ),
        (
            "AWF-VERDICT: FIXED: the real fix\nAWF-VERDICT: FIXED: <one-sentence summary>",
            "the real fix",
        ),
    ],
)
def test_fixed_with_extra_output_is_accepted_only_with_fix_evidence(
    stdout: str,
    expected_reason: str,
) -> None:
    with structlog.testing.capture_logs() as captured:
        result = _parse(stdout, fix_evidence=True)

    assert result == VerdictResult(verdict="fix_committed", reason=expected_reason)
    accepted = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_trailing_output_accepted"
    ]
    assert len(accepted) == 1
    assert accepted[0]["workspace_id"] == "ws_tolerance"
    assert accepted[0]["reason_code"] == AGENT_VERDICT_PROTOCOL_VIOLATION
    assert accepted[0]["verdict"] == "fix_committed"

    with pytest.raises(AgentVerdictProtocolError) as caught:
        _parse(stdout, fix_evidence=False)
    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        # A valid non-FIXED record with chatter: AWF has no evidence to check (F4).
        F4,
        # The last valid record decides; non-FIXED last means the strict error.
        "AWF-VERDICT: DEFER: first record\nAWF-VERDICT: FALSE POSITIVE: second record",
        "AWF-VERDICT: FIXED: fixed it\nAWF-VERDICT: DEFER: on second thought",
        # A placeholder is never selected, and nothing valid sits above it.
        "AWF-VERDICT: FIXED: <one-sentence summary>\nchatter",
        "AWF-VERDICT: DEFER: track it\nAWF-VERDICT: FIXED: <one-sentence summary>",
        # No record at all, and nothing to inherit.
        F3,
        "",
        *(_trailing(line) for line in _NOT_RECORDS),
    ],
)
def test_everything_else_keeps_the_strict_protocol_error(stdout: str) -> None:
    with pytest.raises(AgentVerdictProtocolError) as caught:
        _parse(stdout, fix_evidence=True)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION


@pytest.mark.unit
@pytest.mark.parametrize("stdout", [F3, "", "**AWF-VERDICT: FIXED: decorated**\nmore chatter"])
@pytest.mark.parametrize("fix_evidence", [True, False])
def test_a_record_less_correction_inherits_the_refused_fixed(
    stdout: str,
    fix_evidence: bool,
) -> None:
    """Scope B: the evidence is judged by the caller's gates, not by this seam."""
    with structlog.testing.capture_logs() as captured:
        result = _parse(stdout, fix_evidence=fix_evidence, inherited=_INHERITED)

    assert result is _INHERITED
    inherited = [
        entry
        for entry in captured
        if entry.get("event") == "monitor.agent_verdict_correction_inherited_fixed"
    ]
    assert len(inherited) == 1
    assert inherited[0]["workspace_id"] == "ws_tolerance"
    assert inherited[0]["reason_code"] == AGENT_FIXED_WITHOUT_EVIDENCE


@pytest.mark.unit
@pytest.mark.parametrize(
    "stdout",
    [
        F4,
        # A placeholder still has the exact record syntax: it blocks inheritance.
        "AWF-VERDICT: FIXED: <one-sentence summary>\nchatter",
        "AWF-VERDICT: DEFER: first record\nAWF-VERDICT: FALSE POSITIVE: second record",
        # An exact FIXED with chatter but no evidence is the correction's own claim.
        F1,
    ],
)
def test_any_exact_record_on_the_correction_blocks_inheritance(stdout: str) -> None:
    with pytest.raises(AgentVerdictProtocolError) as caught:
        _parse(stdout, fix_evidence=False, inherited=_INHERITED)

    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION


# Decorated, indented or wrong-case lines that name a non-FIXED verdict. They are
# never records, but they are still the agent's own word against FIXED.
_LOOSE_NON_FIXED = [
    "**AWF-VERDICT: NEEDS_HUMAN: only partially addressed; a maintainer must decide**",
    "`AWF-VERDICT: DEFER: track the remaining migration separately`",
    "Done. AWF-VERDICT: FALSE POSITIVE: the reviewer misread the diff",
    "  AWF-VERDICT: NEEDS HUMAN: wrong separator, same request",
    "awf-verdict: false positive: wrong case, same answer",
]


@pytest.mark.unit
@pytest.mark.parametrize("contradiction", _LOOSE_NON_FIXED)
def test_a_later_non_fixed_mention_contradicts_a_tolerated_fixed(contradiction: str) -> None:
    """The agent's last word was not FIXED, so evidence cannot make it FIXED."""
    stdout = f"AWF-VERDICT: FIXED: fixed the guard\n{contradiction}\n"

    assert _last_valid_verdict_record(stdout) is None
    with pytest.raises(AgentVerdictProtocolError) as caught:
        _parse(stdout, fix_evidence=True)
    assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION


@pytest.mark.unit
@pytest.mark.parametrize("earlier", _LOOSE_NON_FIXED)
def test_an_earlier_non_fixed_mention_does_not_contradict_a_later_fixed(earlier: str) -> None:
    """Like exact records, only what comes after the selected FIXED can overrule it."""
    stdout = f"{earlier}\nAWF-VERDICT: FIXED: fixed it after all\nchatter"

    assert _parse(stdout, fix_evidence=True) == VerdictResult(
        verdict="fix_committed",
        reason="fixed it after all",
    )


@pytest.mark.unit
@pytest.mark.parametrize("correction", _LOOSE_NON_FIXED)
def test_a_non_fixed_mention_on_the_correction_blocks_inheritance(correction: str) -> None:
    """A decorated NEEDS_HUMAN / DEFER / FALSE POSITIVE is no record, but not silence either."""
    for fix_evidence in (True, False):
        with pytest.raises(AgentVerdictProtocolError) as caught:
            _parse(f"{F3}{correction}\n", fix_evidence=fix_evidence, inherited=_INHERITED)
        assert caught.value.reason_code == AGENT_VERDICT_PROTOCOL_VIOLATION


@pytest.mark.unit
def test_a_tolerated_fixed_with_evidence_beats_inheritance() -> None:
    result = _parse(F1, fix_evidence=True, inherited=_INHERITED)

    assert result == VerdictResult(verdict="fix_committed", reason=F1_REASON)
