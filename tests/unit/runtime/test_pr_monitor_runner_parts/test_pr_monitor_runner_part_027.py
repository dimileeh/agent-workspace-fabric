"""Unit tests for block-container LRD boundaries (part 027).

Split from part 026 for the first-party file line limit
(``test_first_party_code_files_stay_under_line_limit``). Covers
PRRT_kwDOSJAM6s6bVrCs: entering a blockquote/list after prose is a definition
boundary; lazy same-depth continuation is not.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import _parse_verdict_result
from awf.runtime.pr_monitor_runner.helpers_verdict import _markdown_reference_definition_spans
from awf.runtime.pr_monitor_runner.helpers_verdict_emphasis import (
    _markdown_block_container_signature,
    _markdown_block_container_transition_is_boundary,
)


class TestBlockContainerReferenceDefinitionBoundaries:
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            # Blockquote / list interrupts a paragraph; LRD at container start is
            # valid without a blank line (PRRT_kwDOSJAM6s6bVrCs).
            ("**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n> [issue**ref]: /url\n"),
            ("**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n- [issue**ref]: /url\n"),
            ("**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n1. [issue**ref]: /url\n"),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "> - [issue**ref]: /url\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "> [issue**ref]:\n"
                ">   /url\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "- [issue**ref]:\n"
                "  /url\n"
            ),
            # Sibling list item after list prose (new item is a boundary).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "- prior item\n"
                "- [issue**ref]: /url\n"
            ),
            # Leave quote into a list (not lazy continuation).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> prior quote\n"
                "- [issue**ref]: /url\n"
            ),
            # Nested quote after outer quote prose.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> prior quote\n"
                "> > [issue**ref]: /url\n"
            ),
        ],
    )
    def test_parse_verdict_resolves_reference_definition_after_paragraph_container(
        self,
        stdout: str,
    ) -> None:
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            # Lazy continuation into a blockquote paragraph — not an LRD.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> prior quote\n"
                "[issue**ref]: /url\n"
            ),
            # Same-depth blockquote continuation after prose — not a boundary.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> prior quote\n"
                "> [issue**ref]: /url\n"
            ),
            # Lazy continuation into a list-item paragraph.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "- prior item\n"
                "[issue**ref]: /url\n"
            ),
        ],
    )
    def test_parse_verdict_rejects_reference_definition_lazy_container_continuation(
        self,
        stdout: str,
    ) -> None:
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            # Non-1 ordered lists cannot interrupt a paragraph (PRRT_kwDOSJAM6s6bVyA3).
            ("**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n2. [issue**ref]: /url\n"),
            ("**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n0. [issue**ref]: /url\n"),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n10. [issue**ref]: /url\n"
            ),
            # Nested non-1 ordered list inside a blockquote paragraph.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> prior quote\n"
                "> 2. [issue**ref]: /url\n"
            ),
            # Leave blockquote onto a non-1 ordered marker — lazy continuation.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> prior quote\n"
                "2. [issue**ref]: /url\n"
            ),
        ],
    )
    def test_parse_verdict_rejects_non_one_ordered_list_after_paragraph(
        self,
        stdout: str,
    ) -> None:
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            # After a blank line, any start number opens a real list.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "2. [issue**ref]: /url\n"
            ),
            # Replacing a list marker at the same depth is not paragraph interrupt.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "- prior item\n"
                "2. [issue**ref]: /url\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "1. prior item\n"
                "2. [issue**ref]: /url\n"
            ),
        ],
    )
    def test_parse_verdict_resolves_non_one_ordered_list_when_not_interrupting(
        self,
        stdout: str,
    ) -> None:
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"

    @pytest.mark.unit
    def test_block_container_signature_and_transition_helpers(self) -> None:
        assert _markdown_block_container_signature("> [foo]: /url") == (">",)
        assert _markdown_block_container_signature("- [foo]: /url") == ("l",)
        assert _markdown_block_container_signature("1. [foo]: /url") == ("l",)
        assert _markdown_block_container_signature("2. [foo]: /url") == ("L",)
        assert _markdown_block_container_signature("0. [foo]: /url") == ("L",)
        assert _markdown_block_container_signature("> - [foo]: /url") == (">", "l")
        assert _markdown_block_container_signature("> 2. [foo]: /url") == (">", "L")
        assert _markdown_block_container_signature("  plain") == ()
        assert _markdown_block_container_transition_is_boundary((), (">",)) is True
        assert _markdown_block_container_transition_is_boundary((">",), (">",)) is False
        assert _markdown_block_container_transition_is_boundary(("l",), ("l",)) is True
        assert _markdown_block_container_transition_is_boundary(("L",), ("L",)) is True
        assert _markdown_block_container_transition_is_boundary((">",), ()) is False
        assert _markdown_block_container_transition_is_boundary((">",), ("l",)) is True
        assert _markdown_block_container_transition_is_boundary((), ("l",)) is True
        assert _markdown_block_container_transition_is_boundary((), ("L",)) is False
        assert _markdown_block_container_transition_is_boundary((">",), (">", "L")) is False
        assert _markdown_block_container_transition_is_boundary((">",), ("L",)) is False
        assert _markdown_block_container_transition_is_boundary(("l",), ("L",)) is True
        assert _markdown_block_container_transition_is_boundary((), (">", "L")) is True
        assert [
            label for _, _, label in _markdown_reference_definition_spans("para\n> [foo]: /url\n")
        ] == ["foo"]
        assert _markdown_reference_definition_spans("para\n2. [foo]: /url\n") == []
        assert _markdown_reference_definition_spans("> quote\n> [foo]: /url\n") == []
        assert _markdown_reference_definition_spans("> quote\n[foo]: /url\n") == []
        # Leading container markers must not invent a BOS entry transition when
        # the caller disabled the beginning-of-string boundary.
        assert (
            _markdown_reference_definition_spans(
                "> [Foo]: /a\n",
                bos_is_block_boundary=False,
            )
            == []
        )
        assert (
            _markdown_reference_definition_spans(
                "- [Foo]: /a\n",
                bos_is_block_boundary=False,
            )
            == []
        )
