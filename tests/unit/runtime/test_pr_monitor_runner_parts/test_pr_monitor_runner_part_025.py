"""Coverage edges for verdict parse / placeholder / peel helpers."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result
from awf.runtime.pr_monitor_runner.helpers_verdict import (
    VerdictResult,
    _awf_verdict_leading_fixed_absorbs_later_marker,
    _awf_verdict_leading_hard_block,
    _fail_closed_resolvable_placeholder_if_needed,
    _last_awf_resolvable_reason_is_placeholder,
    _peel_one_unconditional_verdict_reason_wrapper,
    _select_bare_verdict,
    _stdout_mentions_awf_verdict,
    _text_matches_verdict_reason_template_placeholder,
)


@pytest.mark.unit
def test_resolvable_placeholder_falls_back_to_bare_blocking_verdict() -> None:
    """Bare ``NEEDS_HUMAN`` / ``DEFER`` must win over a final resolvable placeholder.

    Without the bare-blocking branch, ``AWF-VERDICT: FIXED: <reason>`` after a
    bare hard block would fail-closed to a placeholder echo and drop the
    escalation reason.
    """
    result = _parse_verdict_result("NEEDS_HUMAN: blocked\nAWF-VERDICT: FIXED: <reason>")
    assert result.verdict == "needs_human"
    assert result.reason == "blocked"

    defer_first = _parse_verdict_result("DEFER: later\nAWF-VERDICT: FIXED: <reason>")
    assert defer_first.verdict == "defer"
    assert defer_first.reason == "later"

    # DEFER placeholder must not reuse same-label bare DEFER; needs_human bare wins.
    defer_placeholder = _parse_verdict_result("needs human: escalate\nAWF-VERDICT: DEFER: <reason>")
    assert defer_placeholder.verdict == "needs_human"
    assert defer_placeholder.reason == "escalate"


@pytest.mark.unit
def test_stdout_mentions_and_select_bare_verdict_priority_edges() -> None:
    """Marker mention scan and bare-verdict priority selection cover both outcomes."""
    assert _stdout_mentions_awf_verdict("AWF-VERDICT: FIXED: done") is True
    assert _stdout_mentions_awf_verdict("no marker here") is False
    # Marker present only as a non-attempt citation still counts as a mention.
    assert _stdout_mentions_awf_verdict('"AWF-VERDICT: FIXED: example"') is True

    reasoned = _select_bare_verdict(
        [
            VerdictResult(verdict="needs_human", reason=None),
            VerdictResult(verdict="needs_human", reason="why"),
            VerdictResult(verdict="defer", reason=None),
        ],
        priorities=("needs_human", "defer"),
    )
    assert reasoned == VerdictResult(verdict="needs_human", reason="why")

    bare_only = _select_bare_verdict(
        [VerdictResult(verdict="defer", reason=None)],
        priorities=("needs_human", "defer"),
    )
    assert bare_only == VerdictResult(verdict="defer", reason=None)

    assert (
        _select_bare_verdict(
            [VerdictResult(verdict="fix_committed", reason="x")],
            priorities=("needs_human", "defer"),
        )
        is None
    )


@pytest.mark.unit
def test_placeholder_and_peel_helpers_cover_negative_and_wrapper_edges() -> None:
    """Placeholder detection, fail-closed conversion, and one-layer peels."""
    assert (
        _last_awf_resolvable_reason_is_placeholder(
            "AWF-VERDICT: FIXED: <reason>", verdict="agent_failed"
        )
        is False
    )
    assert _last_awf_resolvable_reason_is_placeholder("nope", verdict="fix_committed") is False
    assert (
        _last_awf_resolvable_reason_is_placeholder("AWF-VERDICT: FIXED:", verdict="fix_committed")
        is False
    )
    assert _last_awf_resolvable_reason_is_placeholder(
        "AWF-VERDICT: FIXED: <reason>", verdict="fix_committed"
    )

    assert _fail_closed_resolvable_placeholder_if_needed(
        "AWF-VERDICT: FIXED: <reason>",
        VerdictResult(verdict="fix_committed", reason=None),
    ) == VerdictResult(verdict="needs_human", reason="fixed_placeholder_echo")
    assert _fail_closed_resolvable_placeholder_if_needed(
        "AWF-VERDICT: FALSE POSITIVE: <reason>",
        VerdictResult(verdict="false_positive", reason=None),
    ) == VerdictResult(verdict="needs_human", reason="verdict_placeholder_echo")
    # Reason already present / non-placeholder final — unchanged.
    kept = VerdictResult(verdict="fix_committed", reason="done")
    assert _fail_closed_resolvable_placeholder_if_needed("AWF-VERDICT: FIXED: done", kept) is kept
    reasonless = VerdictResult(verdict="fix_committed", reason=None)
    assert (
        _fail_closed_resolvable_placeholder_if_needed("AWF-VERDICT: FIXED: done", reasonless)
        is reasonless
    )

    assert _peel_one_unconditional_verdict_reason_wrapper("`<reason>`") == "<reason>"
    assert _peel_one_unconditional_verdict_reason_wrapper('"<reason>"') == "<reason>"
    assert _peel_one_unconditional_verdict_reason_wrapper("'<reason>'") == "<reason>"
    assert _peel_one_unconditional_verdict_reason_wrapper("**<reason>**") == "<reason>"
    assert _peel_one_unconditional_verdict_reason_wrapper("~~<reason>~~") == "<reason>"
    assert _peel_one_unconditional_verdict_reason_wrapper("__init__") is None
    assert _peel_one_unconditional_verdict_reason_wrapper("plain") is None
    assert _text_matches_verdict_reason_template_placeholder("&lt;reason&gt;") is True
    # Deep CommonMark backslash nesting hits the pass-cap return (not early stable).
    from awf.runtime.pr_monitor_runner.helpers_verdict import (
        _commonmark_backslash_unescape_to_stable,
    )

    deep = ("\\" * 16) + "<reason>" + ("\\" * 16) + ">"
    assert _commonmark_backslash_unescape_to_stable(deep) == "\\<reason>\\>"


@pytest.mark.unit
def test_leading_hard_block_and_fixed_absorb_reject_non_matches() -> None:
    """Leading hard-block / FIXED-absorb helpers return False when match fails."""
    assert _awf_verdict_leading_hard_block("not a verdict line", 0) is False
    assert _awf_verdict_leading_hard_block("AWF-VERDICT: NEEDS_HUMAN: blocked", 0) is True
    assert _awf_verdict_leading_fixed_absorbs_later_marker("nope", 0, 1) is False
    absorbed = "AWF-VERDICT: FIXED: keep. AWF-VERDICT: FALSE POSITIVE: absorbed"
    assert _awf_verdict_leading_fixed_absorbs_later_marker(absorbed, 0, 26) is True


@pytest.mark.unit
def test_markdown_list_inside_blockquote_html_and_fence_closers() -> None:
    """List markers nested under blockquotes still peel for HTML/fence closes."""
    from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
        _html_blank_terminated_block_closes,
        _html_declaration_closes,
        _iter_non_fenced_verdict_lines,
        _markdown_fence_closes,
    )

    # List marker between blockquote peels (lines 560-561 / 841-842 / 934-937).
    assert _html_declaration_closes("- > residual>", blockquote_depth=1) is True
    assert _html_blank_terminated_block_closes("- >", blockquote_depth=1) is True
    assert _html_blank_terminated_block_closes("- >   ", blockquote_depth=1) is True
    assert (
        _markdown_fence_closes(
            "- > ```",
            fence="```",
            blockquote_depth=1,
            container_indent=0,
        )
        is True
    )
    # Neither blockquote nor list while depth remains — fail closed (562/843/938).
    assert _html_declaration_closes("> notclose", blockquote_depth=2) is False
    assert _html_blank_terminated_block_closes("> notblank", blockquote_depth=2) is False
    assert (
        _markdown_fence_closes(
            "> notfence",
            fence="```",
            blockquote_depth=2,
            container_indent=0,
        )
        is False
    )
    # Complete ``<code>`` same-line closer still skips the opener.
    lines = list(
        _iter_non_fenced_verdict_lines(
            "<code>AWF-VERDICT: FIXED: hidden</code>\nAWF-VERDICT: NEEDS_HUMAN: visible\n"
        )
    )
    assert any("NEEDS_HUMAN" in line for line in lines)
    assert not any("FIXED: hidden" in line for line in lines)
    # Hybrid complete ``<code>`` that closes on a later blank stays shielded.
    blank_tail = list(
        _iter_non_fenced_verdict_lines(
            "<code class='x'>\nAWF-VERDICT: FIXED: hidden\n\n"
            "AWF-VERDICT: NEEDS_HUMAN: after blank\n"
        )
    )
    assert any("after blank" in line for line in blank_tail)
    assert not any("FIXED: hidden" in line for line in blank_tail)
