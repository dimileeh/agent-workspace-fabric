"""Unit tests for verdict parsing helpers."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _monitor_state_verdict,
    _parse_verdict,
    _parse_verdict_result,
)


class TestParseVerdict:
    @pytest.mark.unit
    def test_empty_stdout_needs_human(self) -> None:
        # #305: empty agent output is a failure to produce, not a considered
        # defer. Block the merge (needs_human) rather than auto-capturing a
        # follow-up tracking issue on a thread the agent never addressed.
        assert _parse_verdict("") == "needs_human"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            "FIXED: done",
            "FALSE POSITIVE: reviewer misread the diff",
            "DEFER: needs human judgement",
            "FALSE POSITIVE:",
            "false positive: minor",
        ],
    )
    def test_bare_verdict_without_awf_marker_fail_closed(self, stdout: str) -> None:
        # Canonical contract is AWF-VERDICT:; bare FIXED / FALSE POSITIVE / DEFER
        # must never resolve (PR #822 review 4945481906).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_private_awf_verdict_false_positive_marker(self) -> None:
        assert (
            _parse_verdict("AWF-VERDICT: FALSE POSITIVE: stale review boilerplate")
            == "false_positive"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_marker_preserves_reason(self) -> None:
        # #305: NEEDS_HUMAN maps to its own needs_human verdict (blocks merge,
        # never auto-resolved), distinct from a follow-up defer.
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    def test_private_awf_verdict_bounds_large_reason_before_state_persistence(self) -> None:
        result = _parse_verdict_result(f"AWF-VERDICT: NEEDS_HUMAN: {'x' * 10_000}")

        assert result.verdict == "needs_human"
        assert result.reason == f"{'x' * 499}…"
        assert len(result.reason) == 500

    @pytest.mark.unit
    def test_private_awf_verdict_uses_final_line_not_prompt_echo(self) -> None:
        stdout = (
            'Re-reading: "print AWF-VERDICT: NEEDS_HUMAN: <what you need> and exit."\n'
            "Some deliberation about the tradeoff.\n"
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose the checkout policy"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must choose the checkout policy"

    @pytest.mark.unit
    def test_private_awf_verdict_placeholder_after_real_verdict_preserves_previous(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: maintainer review required\n"
            "AWF-VERDICT: FIXED: <one-sentence summary>"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer review required"

    @pytest.mark.unit
    def test_private_awf_no_reason_fixed_not_trumped_by_earlier_false_positive(self) -> None:
        # A genuine final ``FIXED`` with no prose reason must remain the agent's
        # last word: the placeholder-echo guard only blocks regressing past a
        # hard block (needs_human/defer), so an earlier non-blocking
        # ``false_positive`` must not override it (#676).
        stdout = "AWF-VERDICT: FALSE POSITIVE: stale review boilerplate\nAWF-VERDICT: FIXED:"

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_no_reason_fixed_preserves_earlier_defer(self) -> None:
        # The hard-block half of the same guard: an earlier ``defer`` is blocking
        # follow-up, so a no-reason final ``FIXED`` placeholder echo cannot clear
        # it (#676).
        stdout = "AWF-VERDICT: DEFER: out-of-scope follow-up\nAWF-VERDICT: FIXED:"

        result = _parse_verdict_result(stdout)

        assert result.verdict == "defer"
        assert result.reason == "out-of-scope follow-up"

    @pytest.mark.unit
    def test_private_awf_mixed_verdict_prefers_awf_over_bare_fallback(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: maintainer decision required\nFALSE POSITIVE: maintainer later added a comment"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision required"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("bare_line", "expected_verdict", "expected_reason"),
        [
            (
                "NEEDS_HUMAN: maintainer decision required",
                "needs_human",
                "maintainer decision required",
            ),
            ("DEFER: track a follow-up", "defer", "track a follow-up"),
        ],
    )
    def test_empty_non_blocking_awf_verdict_preserves_later_bare_blocker(
        self,
        bare_line: str,
        expected_verdict: str,
        expected_reason: str,
    ) -> None:
        result = _parse_verdict_result(f"AWF-VERDICT: FIXED:\n{bare_line}")

        assert result.verdict == expected_verdict
        assert result.reason == expected_reason

    @pytest.mark.unit
    def test_private_awf_later_verdict_wins_over_prior_verdict(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: stale review boilerplate\n"
            "AWF-VERDICT: NEEDS_HUMAN: maintainer follow-up required"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer follow-up required"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_reason",
        [
            "",
            "<what you need>",
        ],
    )
    def test_private_awf_empty_final_needs_human_wins_over_prior_fixed(
        self,
        final_reason: str,
    ) -> None:
        result = _parse_verdict_result(
            f"AWF-VERDICT: FIXED: committed a fix\nAWF-VERDICT: NEEDS_HUMAN: {final_reason}"
        )

        assert result.verdict == "needs_human"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_multiple_needs_human_uses_latest_reason(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: first pass needs human review\nAWF-VERDICT: NEEDS_HUMAN:"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "first pass needs human review"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_inline_prompt_template(self) -> None:
        stdout = (
            'Re-reading: "If you need a human decision, print '
            'AWF-VERDICT: NEEDS_HUMAN: <what you need> and exit."'
        )

        result = _parse_verdict_result(stdout)

        # Inline prompt echoes are not a fullmatch verdict line; fail closed.
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_placeholder_only_needs_human_has_no_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN: <what you need>")

        assert result.verdict == "needs_human"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_placeholder_with_trailing_exit_boilerplate_has_no_reason(
        self,
    ) -> None:
        # Prompt echoes often keep trailing ``and exit."`` after the placeholder.
        # That must still sanitize away without treating mid-reason ``<summary>``
        # as a template echo.
        result = _parse_verdict_result('AWF-VERDICT: NEEDS_HUMAN: <what you need> and exit."')

        assert result.verdict == "needs_human"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_without_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: NEEDS_HUMAN:")

        assert result.verdict == "needs_human"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_defer_without_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: DEFER:")

        assert result.verdict == "defer"
        assert result.reason is None

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_space_variant_preserves_reason(self) -> None:
        # The primary _AWF_VERDICT regex tolerates "NEEDS HUMAN" (space) like
        # "FALSE POSITIVE", so the reason is extracted cleanly instead of being
        # garbled by the bare fallback (which splits on the AWF-VERDICT colon).
        result = _parse_verdict_result("AWF-VERDICT: NEEDS HUMAN: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "label",
        [
            "NEEDS_HUMAN",
            "NEEDS HUMAN",
            "NEEDS_ HUMAN",
            "NEEDS _HUMAN",
            "NEEDS__HUMAN",
            "needs_human",
        ],
    )
    def test_private_awf_verdict_needs_human_separator_variants(self, label: str) -> None:
        # Any separator the NEEDS[\s_]+HUMAN regex accepts must normalize to
        # needs_human — never silently fall through to fix_committed (#305).
        result = _parse_verdict_result(f"AWF-VERDICT: {label}: maintainer decision")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    def test_private_awf_verdict_accepts_inline_code_span(self) -> None:
        result = _parse_verdict_result("`AWF-VERDICT: NEEDS_HUMAN: maintainer decision`")

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer decision"

    @pytest.mark.unit
    def test_private_awf_verdict_accepts_one_line_code_fence(self) -> None:
        result = _parse_verdict_result("```AWF-VERDICT: DEFER: track follow-up separately```")

        assert result.verdict == "defer"
        assert result.reason == "track follow-up separately"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_multiline_fence(self) -> None:
        # Agents may emit a blocking verdict then quote another verdict grammar
        # inside a fenced example. Fence contents must not become the final
        # authoritative marker (PRRT_kwDOSJAM6s6ZlqAE).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "```\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_html_pre_block(self) -> None:
        # Raw Markdown HTML <pre> examples are not fenced/indented code; without
        # tracking them the example overrides an earlier NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZnEAt).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<pre>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "</pre>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_html_code_block(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<code>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "</code>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "tag",
        ["script", "style", "textarea"],
        ids=["html_script", "html_style", "html_textarea"],
    )
    def test_private_awf_verdict_ignores_markers_inside_html_type1_raw_blocks(
        self,
        tag: str,
    ) -> None:
        # CommonMark type-1 HTML blocks also include script/style/textarea.
        # Without tracking them, an example inside overrides NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZnQhP).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            f"<{tag}>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            f"</{tag}>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_same_line_html_script_wrapper(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<script>AWF-VERDICT: FALSE POSITIVE: example</script>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_html_comment(self) -> None:
        # HTML comments are not <pre>/<code>; without shielding, a clean
        # marker line inside <!-- … --> overrides an earlier NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZnN2F).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<!--\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "-->\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_same_line_html_comment_wrapper(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<!-- AWF-VERDICT: FALSE POSITIVE: example -->\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "- <!--\n"
                "  AWF-VERDICT: FALSE POSITIVE: example\n"
                "  -->\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> <!--\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                "> -->\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> - <!--\n"
                ">   AWF-VERDICT: FALSE POSITIVE: example\n"
                ">   -->\n"
            ),
        ],
        ids=[
            "list_html_comment",
            "blockquote_html_comment",
            "blockquote_list_html_comment",
        ],
    )
    def test_private_awf_verdict_ignores_list_blockquote_nested_html_comment(
        self,
        stdout: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unclosed_html_comment_shields_trailing_markers(
        self,
    ) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n<!--\nAWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unfenced_after_closed_html_comment_still_wins(
        self,
    ) -> None:
        stdout = (
            "<!--\nAWF-VERDICT: FALSE POSITIVE: example\n-->\nAWF-VERDICT: FIXED: real fix landed\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real fix landed"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_html_processing_instruction(
        self,
    ) -> None:
        # CommonMark type-3 processing instructions are not comments or type-1
        # tags; without shielding, a clean marker inside <?…?> overrides an
        # earlier NEEDS_HUMAN (PRRT_kwDOSJAM6s6ZnSrG).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<?xml\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "?>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_same_line_html_processing_instruction(
        self,
    ) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<?xml AWF-VERDICT: FALSE POSITIVE: example ?>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "- <?xml\n"
                "  AWF-VERDICT: FALSE POSITIVE: example\n"
                "  ?>\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> <?xml\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                "> ?>\n"
            ),
        ],
        ids=["list_html_pi", "blockquote_html_pi"],
    )
    def test_private_awf_verdict_ignores_list_blockquote_nested_html_pi(
        self,
        stdout: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unclosed_html_pi_shields_trailing_markers(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<?xml\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unfenced_after_closed_html_pi_still_wins(self) -> None:
        stdout = (
            "<?xml\nAWF-VERDICT: FALSE POSITIVE: example\n?>\nAWF-VERDICT: FIXED: real fix landed\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real fix landed"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_html_declaration(self) -> None:
        # CommonMark type-4 declarations (<!Letter…>) end on the first line
        # containing `>`; example markers inside must not override NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZnSrG).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<!DOCTYPE\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            ">\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "<!DOCTYPE html AWF-VERDICT: FALSE POSITIVE: example>\n"
            ),
            (
                # Blockquote prefix on a same-line declaration must still detect
                # the real ``>`` closer after peeling container depth
                # (PRRT_kwDOSJAM6s6ZnUwf).
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> <!DOCTYPE html AWF-VERDICT: FALSE POSITIVE: example>\n"
            ),
        ],
        ids=["same_line_html_declaration", "blockquote_same_line_html_declaration"],
    )
    def test_private_awf_verdict_ignores_same_line_html_declaration(
        self,
        stdout: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "- <!DOCTYPE\n"
                "  AWF-VERDICT: FALSE POSITIVE: example\n"
                "  >\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> <!DOCTYPE\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                "> >\n"
            ),
            (
                # Opener-line blockquote `>` must not count as the type-4 closer
                # (same-line skip). A bare ``>`` continuation is empty quote
                # content, not a declaration end — leave the region open so a
                # later FIXED cannot override NEEDS_HUMAN (PRRT_kwDOSJAM6s6ZnUwf).
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> <!DOCTYPE\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                ">\n"
                "AWF-VERDICT: FIXED: real fix landed\n"
            ),
        ],
        ids=[
            "list_html_declaration",
            "blockquote_html_declaration",
            "blockquote_html_declaration_trailing_fixed",
        ],
    )
    def test_private_awf_verdict_ignores_list_blockquote_nested_html_declaration(
        self,
        stdout: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_html_cdata(self) -> None:
        # CommonMark type-5 CDATA blocks are neither comments nor type-1 tags;
        # without shielding they override an earlier NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZnSrG).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<![CDATA[\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "]]>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_same_line_html_cdata(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<![CDATA[AWF-VERDICT: FALSE POSITIVE: example]]>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "- <![CDATA[\n"
                "  AWF-VERDICT: FALSE POSITIVE: example\n"
                "  ]]>\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> <![CDATA[\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                "> ]]>\n"
            ),
        ],
        ids=["list_html_cdata", "blockquote_html_cdata"],
    )
    def test_private_awf_verdict_ignores_list_blockquote_nested_html_cdata(
        self,
        stdout: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unclosed_html_cdata_shields_trailing_markers(
        self,
    ) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<![CDATA[\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unfenced_after_closed_html_cdata_still_wins(self) -> None:
        stdout = (
            "<![CDATA[\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "]]>\n"
            "AWF-VERDICT: FIXED: real fix landed\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real fix landed"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_html_type6_div_block(
        self,
    ) -> None:
        # CommonMark type-6 blocks (``div``, etc.) end on a blank line, not a
        # close tag. Without shielding, an example inside overrides NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZnUxZ).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<div>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "</div>\n"
            "\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_inside_html_type7_custom_tag(
        self,
    ) -> None:
        # Type-7 complete custom tags also continue until a blank line
        # (PRRT_kwDOSJAM6s6ZnUxZ).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<custom-example>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "</custom-example>\n"
            "\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "closer",
        ["</pre>", "</code>", "</script>", "</style>", "</textarea>"],
        ids=["close_pre", "close_code", "close_script", "close_style", "close_textarea"],
    )
    def test_private_awf_verdict_ignores_markers_after_standalone_type1_closer(
        self,
        closer: str,
    ) -> None:
        # CommonMark type 7 allows any complete closing tag (including type-1
        # names). Openers stay on the type-1 path; a naked ``</pre>`` /
        # ``</script>`` must enter blank-terminated type-7 shielding or an
        # example FALSE POSITIVE overrides NEEDS_HUMAN (PRRT_kwDOSJAM6s6Zn6x4).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            f"{closer}\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "opener",
        [
            '<custom-example data-value=">">',
            "<custom-example data-value='>'>",
            "<custom-example title=\"a > b\" class='x>y'>",
        ],
        ids=["double_quoted_gt", "single_quoted_gt", "mixed_quoted_gt"],
    )
    def test_private_awf_verdict_ignores_markers_inside_html_type7_quoted_attr(
        self,
        opener: str,
    ) -> None:
        # Quoted ``>`` inside type-7 attribute values must not abort the open
        # tag match (PRRT_kwDOSJAM6s6ZnYwM); otherwise an example FALSE
        # POSITIVE inside the blank-line-terminated block overrides NEEDS_HUMAN.
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            f"{opener}\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "</custom-example>\n"
            "\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "- <div>\n"
                "  AWF-VERDICT: FALSE POSITIVE: example\n"
                "\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> <div>\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                "\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> - <custom-example>\n"
                ">   AWF-VERDICT: FALSE POSITIVE: example\n"
                "\n"
            ),
        ],
        ids=["list_html_div", "blockquote_html_div", "blockquote_list_custom_tag"],
    )
    def test_private_awf_verdict_ignores_list_blockquote_nested_html_type6_type7(
        self,
        stdout: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unclosed_html_type6_shields_trailing_markers(
        self,
    ) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<div>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_html_type6_blank_line_ends_shield(self) -> None:
        stdout = (
            "<div>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "</div>\n"
            "\n"
            "AWF-VERDICT: FIXED: real fix landed\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real fix landed"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "> <div>\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                ">\n"
                "AWF-VERDICT: FIXED: real fix landed\n"
            ),
            (
                "> <div>\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                ">   \n"
                "AWF-VERDICT: FIXED: real fix landed\n"
            ),
            (
                "> - <custom-example>\n"
                ">   AWF-VERDICT: FALSE POSITIVE: example\n"
                ">\n"
                "AWF-VERDICT: FIXED: real fix landed\n"
            ),
        ],
        ids=[
            "blockquote_gt_blank",
            "blockquote_gt_spaces_blank",
            "blockquote_list_gt_blank",
        ],
    )
    def test_private_awf_verdict_blockquote_html_type6_gt_blank_ends_shield(
        self,
        stdout: str,
    ) -> None:
        # Blockquote-nested type-6/7 blocks terminate on ``>`` / ``>   ``
        # content blank lines; without stripping the container the parser
        # stays in html_blank_terminated mode and suppresses FIXED
        # (PRRT_kwDOSJAM6s6ZnYwP).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real fix landed"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "nested_blank",
        ["> >", ">>", "> > ", ">>   "],
        ids=["gt_space_gt", "gt_gt", "gt_space_gt_spaces", "gt_gt_spaces"],
    )
    def test_private_awf_verdict_nested_blockquote_blank_does_not_end_single_level_html_type6(
        self,
        nested_blank: str,
    ) -> None:
        # Full blockquote-prefix stripping would treat nested ``> >`` / ``>>`` as
        # a blank terminator for a single-level ``> <div>`` shield and let a
        # later FALSE POSITIVE override NEEDS_HUMAN (PRRT_kwDOSJAM6s6ZnblF).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "> <div>\n"
            "> AWF-VERDICT: FALSE POSITIVE: example\n"
            f"{nested_blank}\n"
            "AWF-VERDICT: FALSE POSITIVE: would override\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_double_blockquote_html_type6_gt_blank_ends_shield(
        self,
    ) -> None:
        # Depth-2 openers still terminate on a matching ``>>`` content blank.
        stdout = (
            ">> <div>\n"
            ">> AWF-VERDICT: FALSE POSITIVE: example\n"
            ">>\n"
            "AWF-VERDICT: FIXED: real fix landed\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real fix landed"

    @pytest.mark.unit
    def test_private_awf_verdict_toplevel_html_type6_gt_line_does_not_end_shield(
        self,
    ) -> None:
        # A bare ``>`` is not a blank terminator for top-level type-6/7 blocks.
        stdout = (
            "<div>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            ">\n"
            "AWF-VERDICT: FIXED: must stay shielded\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_same_line_html_type6_wrapper(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<div>AWF-VERDICT: FALSE POSITIVE: example</div>\n"
            "\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "- <pre>\n"
                "  AWF-VERDICT: FALSE POSITIVE: example\n"
                "  </pre>\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> <pre>\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                "> </pre>\n"
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
                "> - <code>\n"
                ">   AWF-VERDICT: FALSE POSITIVE: example\n"
                ">   </code>\n"
            ),
        ],
        ids=["list_html_pre", "blockquote_html_pre", "blockquote_list_html_code"],
    )
    def test_private_awf_verdict_ignores_list_blockquote_nested_html_code(
        self,
        stdout: str,
    ) -> None:
        # HTML <pre>/<code> openers must peel list/blockquote containers the
        # same way fence openers do; otherwise ``- <pre>`` never enters HTML
        # mode and a lightly indented example overrides NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZnHBW).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_same_line_html_pre_wrapper(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<pre>AWF-VERDICT: FALSE POSITIVE: example</pre>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_html_pre_only_quote_is_markerless(self) -> None:
        stdout = "<pre>\nAWF-VERDICT: FALSE POSITIVE: example\n</pre>\n"

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_private_awf_verdict_unclosed_html_pre_shields_trailing_markers(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "<pre>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unfenced_after_closed_html_pre_still_wins(self) -> None:
        stdout = (
            "<pre>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "</pre>\n"
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_html_pre_placeholder_does_not_poison_final(self) -> None:
        stdout = (
            "AWF-VERDICT: FIXED: committed the html skip\n"
            "<pre>\n"
            "AWF-VERDICT: FIXED: <one-sentence summary>\n"
            "</pre>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "committed the html skip"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_in_indented_code_block(self) -> None:
        # Markdown also represents code without fences (four-space indent). An
        # unconditional strip would promote the example to the final verdict and
        # resolve a blocked thread (PRRT_kwDOSJAM6s6ZlsjH).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n    AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_tab_indented_code_example(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n\tAWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize("indent", [" \t", "  \t", "   \t"])
    def test_private_awf_verdict_ignores_spaces_plus_tab_indented_code(self, indent: str) -> None:
        # CommonMark expands 1–3 spaces + tab to a four-column indent, so those
        # lines are indented code — not prose that strip() may promote
        # (PRRT_kwDOSJAM6s6ZluBy).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            f"{indent}AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_indented_only_quote_is_markerless(self) -> None:
        stdout = "    AWF-VERDICT: FALSE POSITIVE: example\n"

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_private_awf_verdict_indented_placeholder_does_not_poison_final(self) -> None:
        stdout = (
            "AWF-VERDICT: FIXED: committed the indent skip\n"
            "    AWF-VERDICT: FIXED: <one-sentence summary>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "committed the indent skip"

    @pytest.mark.unit
    def test_private_awf_verdict_three_space_indent_still_counts(self) -> None:
        # CommonMark indented code requires four spaces; lighter indent is prose.
        result = _parse_verdict_result("   AWF-VERDICT: DEFER: track follow-up separately")

        assert result.verdict == "defer"
        assert result.reason == "track follow-up separately"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_fenced_example_with_info_string(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "```text\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_list_nested_fenced_example(self) -> None:
        # List-contained fences (``- ```text``) are not top-level openers, and
        # two-space list continuation does not meet indented-code indent — the
        # example must still be skipped (PRRT_kwDOSJAM6s6ZmirV).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "- ```text\n"
            "  AWF-VERDICT: FALSE POSITIVE: example\n"
            "  ```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: FIXED: done\n"
                "> ```text\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                "> ```\n"
            ),
            (
                "AWF-VERDICT: FIXED: done\n"
                "> - ```text\n"
                ">   AWF-VERDICT: FALSE POSITIVE: example\n"
                ">   ```\n"
            ),
        ],
        ids=["blockquote_fence", "blockquote_list_fence"],
    )
    def test_private_awf_verdict_ignores_blockquote_nested_fenced_example(
        self,
        stdout: str,
    ) -> None:
        # Blockquote-contained fences (``> ```text`` / ``> - ```text``) are not
        # top-level openers. Without peeling ``>`` for open/close matching, the
        # example is yielded as a later attempt and fails closed over an earlier
        # FIXED (PRRT_kwDOSJAM6s6ZnAyG).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "done"

    @pytest.mark.unit
    def test_private_awf_verdict_deeper_nested_blockquote_fence_does_not_close(
        self,
    ) -> None:
        # Fence closers must peel exactly the opener's blockquote depth. A
        # depth-1 opener (``> ```text``) must not treat nested ``>> ``` `` as a
        # closer — unrestricted ``>+`` peel ends the shield early and exposes a
        # following ``> AWF-VERDICT:`` example as a later attempt over FIXED
        # (PRRT_kwDOSJAM6s6Zn213).
        stdout = (
            "AWF-VERDICT: FIXED: done\n"
            "> ```text\n"
            "> earlier FIXED example stays shielded\n"
            ">> ```\n"
            "> AWF-VERDICT: NEEDS_HUMAN: nested quote example\n"
            "> ```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "done"

    @pytest.mark.unit
    def test_private_awf_verdict_depth_two_blockquote_fence_closes_exact(
        self,
    ) -> None:
        # Depth-2 openers (``>> ```text``) must still close on a matching
        # ``>> ``` `` after exact-depth peel (PRRT_kwDOSJAM6s6Zn213).
        stdout = (
            "AWF-VERDICT: FIXED: earlier\n"
            ">> ```text\n"
            ">> AWF-VERDICT: FALSE POSITIVE: example\n"
            ">> ```\n"
            "AWF-VERDICT: NEEDS_HUMAN: later authoritative\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "later authoritative"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "AWF-VERDICT: FIXED: earlier\n"
                "- > ```text\n"
                "> AWF-VERDICT: FALSE POSITIVE: example\n"
                "> ```\n"
                "AWF-VERDICT: NEEDS_HUMAN: later authoritative\n"
            ),
            (
                "AWF-VERDICT: FIXED: earlier\n"
                "> - ```text\n"
                ">   AWF-VERDICT: FALSE POSITIVE: example\n"
                "> ```\n"
                "AWF-VERDICT: NEEDS_HUMAN: later authoritative\n"
            ),
        ],
        ids=["list_then_blockquote", "blockquote_then_list"],
    )
    def test_private_awf_verdict_list_blockquote_fence_zero_indent_closer(
        self,
        stdout: str,
    ) -> None:
        # List+blockquote openers record a non-zero list container_indent.
        # A normal closer ``> ``` `` strips ``>`` and leaves zero indent; that
        # must still close so a later unfenced NEEDS_HUMAN is not shielded
        # (PRRT_kwDOSJAM6s6ZnDm7).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "later authoritative"

    @pytest.mark.unit
    def test_private_awf_verdict_blockquote_prefix_inside_fence_does_not_close(
        self,
    ) -> None:
        # Closer matching must not peel blockquote markers for top-level fences:
        # ``> ``` `` inside an open fence is content, not a closer.
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "```\n"
            "example closer:\n"
            "> ```\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ordered_list_fence_closer_uses_container_indent(
        self,
    ) -> None:
        # Ordered-list openers (``10. ```text``) put continuation at column 4.
        # Absolute 0–3 closer matching never closes that fence, so a following
        # top-level FIXED is shielded as fenced content (PRRT_kwDOSJAM6s6ZmsZS).
        stdout = (
            "10. ```text\n"
            "    AWF-VERDICT: FALSE POSITIVE: example\n"
            "    ```\n"
            "AWF-VERDICT: FIXED: closed ordered-list fence\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "closed ordered-list fence"

    @pytest.mark.unit
    def test_private_awf_verdict_nested_ordered_list_fence_sums_container_indent(
        self,
    ) -> None:
        # Nested ordered-list openers (``10. 10. ```text``) need eight columns of
        # continuation indent. Overwriting list_width with only the inner marker
        # leaves container_indent at 4 (max closer indent 7), so the fence never
        # closes and a later top-level FIXED stays hidden (PRRT_kwDOSJAM6s6Zn6x6).
        stdout = (
            "10. 10. ```text\n"
            "        AWF-VERDICT: FALSE POSITIVE: example\n"
            "        ```\n"
            "AWF-VERDICT: FIXED: closed nested ordered-list fence\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "closed nested ordered-list fence"

    @pytest.mark.unit
    def test_private_awf_verdict_ordered_list_blockquote_fence_indented_closer(
        self,
    ) -> None:
        # ``10. > ```text`` records list container_indent 4 and blockquote mode.
        # Continuation closers are shaped ``    > ``` ``; a prefix matcher that
        # only allows 0–3 spaces before ``>`` never peels, so the fence stays
        # open and a later top-level FIXED remains hidden (PRRT_kwDOSJAM6s6ZnHH2).
        stdout = (
            "10. > ```text\n"
            "    > AWF-VERDICT: FALSE POSITIVE: example\n"
            "    > ```\n"
            "AWF-VERDICT: FIXED: closed ordered-list blockquote fence\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "closed ordered-list blockquote fence"

    @pytest.mark.unit
    def test_private_awf_verdict_list_prefix_inside_fence_does_not_close(self) -> None:
        # Closer matching must not peel list markers: ``- ``` `` inside a
        # top-level fence is content, not a closer. Treating it as one ends the
        # shield early and lets a later example override NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZmnUU).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "```\n"
            "example closer:\n"
            "- ```\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_four_space_indent_inside_fence_does_not_close(
        self,
    ) -> None:
        # CommonMark permits at most three leading spaces on a closing fence.
        # Unrestricted lstrip would treat four-space-indented content as a
        # closer and let a later example override NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZmqRo).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "```\n"
            "example closer:\n"
            "    ```\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_tilde_fenced_example(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "~~~\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "~~~\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_tilde_fence_with_tilde_in_info_string(
        self,
    ) -> None:
        # CommonMark allows ``~`` in tilde-fence info strings (unlike backticks).
        # Rejecting ``~~~ lang~option`` leaves the body unfenced so an example
        # FALSE POSITIVE can override an earlier NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6Znz-z).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "~~~ lang~option\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "~~~\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_unfenced_after_closed_fence_still_wins(self) -> None:
        stdout = (
            "```\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "```\n"
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_fenced_only_quote_is_markerless(self) -> None:
        # A stdout that only quotes the grammar inside a fence never addressed
        # the thread — fail closed as markerless, not as a selected resolvable.
        stdout = "```\nAWF-VERDICT: FALSE POSITIVE: example\n```\n"

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_private_awf_verdict_unclosed_fence_shields_trailing_markers(self) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n```\nAWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_fenced_placeholder_does_not_poison_final(self) -> None:
        # A fenced template echo must not make an earlier reasoned FIXED look like
        # a placeholder-only final when scanning last-reason provenance.
        stdout = (
            "AWF-VERDICT: FIXED: committed the fence skip\n"
            "```\n"
            "AWF-VERDICT: FIXED: <one-sentence summary>\n"
            "```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "committed the fence skip"

    @pytest.mark.unit
    def test_private_awf_verdict_defer_placeholder_only_fail_closed(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: DEFER: <defer follow-up needed>")

        assert result.verdict == "needs_human"
        assert result.reason == "verdict_placeholder_echo"

    @pytest.mark.unit
    def test_private_awf_verdict_false_positive_placeholder_only_fail_closed(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: FALSE POSITIVE: <one-sentence justification>")

        assert result.verdict == "needs_human"
        assert result.reason == "verdict_placeholder_echo"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            "AWF-VERDICT: FALSE POSITIVE: `<one-sentence justification>`",
            "AWF-VERDICT: FALSE POSITIVE: ``<one-sentence justification>``",
            'AWF-VERDICT: FALSE POSITIVE: "<one-sentence justification>"',
            "AWF-VERDICT: FALSE POSITIVE: '<one-sentence justification>'",
            'AWF-VERDICT: FALSE POSITIVE: "`<one-sentence justification>`"',
            "AWF-VERDICT: FALSE POSITIVE: **<one-sentence justification>**",
            "AWF-VERDICT: FALSE POSITIVE: __<one-sentence justification>__",
            "AWF-VERDICT: FALSE POSITIVE: **`<one-sentence justification>`**",
            "AWF-VERDICT: FALSE POSITIVE: *<one-sentence justification>*",
            "AWF-VERDICT: FALSE POSITIVE: _<one-sentence justification>_",
            "AWF-VERDICT: FALSE POSITIVE: *`<one-sentence justification>`*",
            "AWF-VERDICT: FIXED: `<one-sentence summary>`",
            "AWF-VERDICT: FIXED: **<one-sentence summary>**",
            "AWF-VERDICT: FIXED: *<one-sentence summary>*",
        ],
    )
    def test_private_awf_formatted_placeholder_reason_fail_closed(self, stdout: str) -> None:
        # Balanced quote/backtick/Markdown-strong wrappers around a template
        # placeholder must not leave the echo as a usable reason
        # (PRRT_kwDOSJAM6s6Zn-VK, PRRT_kwDOSJAM6s6ZoAz9). Single emphasis is
        # peeled only when the enclosed value is placeholder-shaped
        # (PRRT_kwDOSJAM6s6ZoDQU).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason in {"verdict_placeholder_echo", "fixed_placeholder_echo"}

    @pytest.mark.unit
    def test_private_awf_quote_only_reason_sanitizes_to_none(self) -> None:
        # Empty quote wrappers are not usable reasons; after unwrap they behave
        # like a bare empty FALSE POSITIVE (reasonless, still false_positive).
        result = _parse_verdict_result('AWF-VERDICT: FALSE POSITIVE: ""')

        assert result.verdict == "false_positive"
        assert result.reason is None

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            "AWF-VERDICT: FALSE POSITIVE: `stale review boilerplate`",
            "AWF-VERDICT: FALSE POSITIVE: **stale review boilerplate**",
            "AWF-VERDICT: FALSE POSITIVE: __stale review boilerplate__",
        ],
    )
    def test_private_awf_formatted_real_reason_still_usable(self, stdout: str) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "false_positive"
        assert result.reason == "stale review boilerplate"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stdout", "expected_reason"),
        [
            # Single emphasis around real prose is not peeled (only placeholder-
            # shaped inners are); the wrapped text remains a usable reason.
            (
                "AWF-VERDICT: FALSE POSITIVE: *stale review boilerplate*",
                "*stale review boilerplate*",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: _stale review boilerplate_",
                "_stale review boilerplate_",
            ),
            # Underscored identifiers must not be mistaken for emphasis wrappers
            # around a template placeholder (PRRT_kwDOSJAM6s6ZoDQU).
            ("AWF-VERDICT: FALSE POSITIVE: _already_fixed_", "_already_fixed_"),
            ("AWF-VERDICT: FALSE POSITIVE: _snake_case_reason_", "_snake_case_reason_"),
        ],
    )
    def test_private_awf_single_emphasis_non_placeholder_reason_still_usable(
        self,
        stdout: str,
        expected_reason: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "false_positive"
        assert result.reason == expected_reason

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stdout", "expected_reason"),
        [
            ("AWF-VERDICT: FALSE POSITIVE: __init__", "__init__"),
            ("AWF-VERDICT: FALSE POSITIVE: __name__", "__name__"),
            ("AWF-VERDICT: FALSE POSITIVE: `__init__`", "__init__"),
            ("AWF-VERDICT: FIXED: __all__", "__all__"),
        ],
    )
    def test_private_awf_python_dunder_reason_not_peeled(
        self,
        stdout: str,
        expected_reason: str,
    ) -> None:
        # Whole-reason Python dunders must not be treated as Markdown ``__…__``
        # strong wrappers (PRRT_kwDOSJAM6s6ZoC7B).
        result = _parse_verdict_result(stdout)

        assert result.verdict in {"false_positive", "fix_committed"}
        assert result.reason == expected_reason

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stdout", "expected_reason"),
        [
            (
                "AWF-VERDICT: FALSE POSITIVE: <one-sentence justification> "
                "AWF-VERDICT: FIXED: cited",
                "verdict_placeholder_echo",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: <one-sentence justification> and exit. "
                "AWF-VERDICT: DEFER: track later",
                "verdict_placeholder_echo",
            ),
            (
                "AWF-VERDICT: FIXED: <one-sentence summary> AWF-VERDICT: FALSE POSITIVE: cite",
                "fixed_placeholder_echo",
            ),
            (
                "AWF-VERDICT: DEFER: <what to track> AWF-VERDICT: FALSE POSITIVE: cite",
                "verdict_placeholder_echo",
            ),
        ],
    )
    def test_private_awf_placeholder_with_absorbed_same_line_citation_fail_closed(
        self,
        stdout: str,
        expected_reason: str,
    ) -> None:
        # Same-line absorption must not let a template-placeholder prefix evade
        # the whole-reason placeholder check by folding later FIXED/DEFER/
        # FALSE POSITIVE citations into the reason (#822 PRRT_kwDOSJAM6s6Znin1).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == expected_reason

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stdout", "expected_reason"),
        [
            (
                "AWF-VERDICT: FALSE POSITIVE: prior rationale\n"
                "AWF-VERDICT: FALSE POSITIVE: <one-sentence justification>",
                "verdict_placeholder_echo",
            ),
            (
                "AWF-VERDICT: DEFER: track follow-up separately\n"
                "AWF-VERDICT: DEFER: <what to track>",
                "verdict_placeholder_echo",
            ),
            (
                "AWF-VERDICT: FIXED: committed a regression test\n"
                "AWF-VERDICT: FIXED: <one-sentence summary>",
                "fixed_placeholder_echo",
            ),
        ],
    )
    def test_private_awf_same_label_earlier_reason_does_not_rescue_final_placeholder(
        self,
        stdout: str,
        expected_reason: str,
    ) -> None:
        # A reasoned same-label line must not be reused when the final AWF line is
        # only a template-placeholder echo — that would still resolve/defer contrary
        # to fail-closed grammar (#822 PRRT_kwDOSJAM6s6ZlCOG).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == expected_reason

    @pytest.mark.unit
    def test_private_awf_same_label_empty_final_still_reuses_earlier_reason(self) -> None:
        # Genuine empty finals (not placeholders) still reuse an earlier same-label
        # reason; only template echoes skip that fallback.
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: prior rationale\nAWF-VERDICT: FALSE POSITIVE:"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "prior rationale"

    @pytest.mark.unit
    def test_private_awf_fixed_placeholder_after_same_label_still_preserves_hard_block(
        self,
    ) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: maintainer review required\n"
            "AWF-VERDICT: FIXED: committed a regression test\n"
            "AWF-VERDICT: FIXED: <one-sentence summary>"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer review required"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_placeholder",
        [
            "AWF-VERDICT: FALSE POSITIVE: <one-sentence justification>",
            "AWF-VERDICT: DEFER: <what to track>",
        ],
    )
    def test_private_awf_resolvable_placeholder_preserves_earlier_hard_block(
        self,
        final_placeholder: str,
    ) -> None:
        # FALSE POSITIVE / DEFER placeholders must keep an earlier reasoned hard
        # block the same way FIXED placeholders do (#822 PRRT_kwDOSJAM6s6ZlxgI).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: maintainer review required\n" + final_placeholder
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer review required"

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_placeholder_only_fail_closed(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: FIXED: <one-sentence summary>")

        assert result.verdict == "needs_human"
        assert result.reason == "fixed_placeholder_echo"

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_ellipsis_placeholder_fail_closed(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: FIXED: …")

        assert result.verdict == "needs_human"
        assert result.reason == "fixed_placeholder_echo"

    @pytest.mark.unit
    def test_private_awf_verdict_garbled_final_marker_fail_closed_after_earlier_verdict(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: rationale\nAWF-VERDICT: SHIPPED: done"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "✅ AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "❌ AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "⚠️ AWF-VERDICT: SHIPPED: done",
            "✓ AWF-VERDICT: FIXED: committed the fix",
            "✅AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            # Unknown word wrappers must also fail closed — closed allowlists
            # leave search() finding the marker while is_attempt stays false.
            "Status: AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "Note: AWF-VERDICT: SHIPPED: done",
        ],
    )
    def test_private_awf_verdict_emoji_or_unknown_prefix_final_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # Decorative / unknown prefixes leave the marker mid-segment so a closed
        # allowlist attempt check ignores them and an earlier resolvable verdict
        # stays selected (#822 PRRT_kwDOSJAM6s6ZmTD6).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "- AWF-VERDICT: SHIPPED: done",
            "* AWF-VERDICT: SHIPPED: done",
            "+ AWF-VERDICT: SHIPPED: done",
            "1. AWF-VERDICT: SHIPPED: done",
            "2) AWF-VERDICT: SHIPPED: done",
        ],
    )
    def test_private_awf_verdict_list_prefixed_garbled_final_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # Markdown list markers before a garbled final marker must not prevent
        # attempt classification — otherwise an earlier resolvable verdict wins
        # (#822 PRRT_kwDOSJAM6s6ZlgUj).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_list_prefixed_valid_final_fail_closed(self) -> None:
        # List-stripped candidates must not make bulleted valid markers
        # authoritative — that newly bypasses fail-closed option-list handling
        # (#822 PRRT_kwDOSJAM6s6ZljVL). Agents must emit a non-bulleted final.
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: rationale\n- AWF-VERDICT: FIXED: committed the fix"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "- [ ] AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "- [x] AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "- [X] AWF-VERDICT: SHIPPED: done",
            "* [ ] AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "1. [ ] AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "> - [ ] AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "- [ ] > AWF-VERDICT: NEEDS_HUMAN: actually unsure",
        ],
    )
    def test_private_awf_verdict_task_list_prefixed_final_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # GFM task-list checkboxes remain after plain list-marker strip
        # (``- `` → ``[ ] AWF-…``). Without stripping ``[ ]``/``[x]``/``[X]``
        # for attempt classification, a trailing task-list marker is ignored
        # and an earlier resolvable verdict stays selected
        # (#822 PRRT_kwDOSJAM6s6ZlxPo).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "> AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            ">AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            ">> AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "> AWF-VERDICT: SHIPPED: done",
            "> - AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            # Combined list+blockquote (either order / nested) must strip
            # repeatedly — one-pass blockquote-then-list leaves ``> AWF-…``
            # after ``- >`` and ignores the blocker (#822 PRRT_kwDOSJAM6s6Zlnby).
            "- > AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "* > AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "1. > AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "- >> AWF-VERDICT: SHIPPED: done",
            "> - > AWF-VERDICT: NEEDS_HUMAN: actually unsure",
        ],
    )
    def test_private_awf_verdict_blockquoted_final_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # Blockquote prefixes must be stripped for attempt classification the
        # same way list markers are — otherwise a trailing ``> AWF-VERDICT:``
        # is ignored and an earlier resolvable verdict stays selected
        # (#822 PRRT_kwDOSJAM6s6ZllZ3).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "Final answer: AWF-VERDICT: NEEDS_HUMAN: unsure",
            "Final answer: AWF-VERDICT: SHIPPED: done",
            "My final answer: AWF-VERDICT: NEEDS_HUMAN: unsure",
            "The final answer is: AWF-VERDICT: SHIPPED: done",
            # Nested Markdown + final-answer must strip repeatedly.
            "> Final answer: AWF-VERDICT: NEEDS_HUMAN: unsure",
            "- Final answer: AWF-VERDICT: SHIPPED: done",
        ],
    )
    def test_private_awf_verdict_prose_prefixed_final_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # Common "Final answer:" wrappers leave the marker mid-segment, so a
        # start-only attempt check ignores them and an earlier resolvable
        # verdict stays selected (#822 PRRT_kwDOSJAM6s6ZmJni).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "Verdict: AWF-VERDICT: NEEDS_HUMAN: unsure",
            "Verdict: AWF-VERDICT: SHIPPED: done",
            "Result: AWF-VERDICT: NEEDS_HUMAN: unsure",
            "Result: AWF-VERDICT: SHIPPED: done",
            "VERDICT: AWF-VERDICT: NEEDS_HUMAN: unsure",
            "result - AWF-VERDICT: SHIPPED: done",
            # Nested Markdown + Verdict:/Result: must strip repeatedly.
            "> Verdict: AWF-VERDICT: NEEDS_HUMAN: unsure",
            "- Result: AWF-VERDICT: SHIPPED: done",
            "**Verdict: AWF-VERDICT: NEEDS_HUMAN: unsure**",
            "### Result: AWF-VERDICT: SHIPPED: done",
        ],
    )
    def test_private_awf_verdict_verdict_result_label_prefixed_final_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # Obvious Verdict:/Result: wrappers leave the marker mid-segment, so a
        # start-only attempt check ignores them and an earlier resolvable
        # verdict stays selected (#822 PRRT_kwDOSJAM6s6ZmPRr).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "**AWF-VERDICT: NEEDS_HUMAN: unsure**",
            "__AWF-VERDICT: NEEDS_HUMAN: unsure__",
            "*AWF-VERDICT: NEEDS_HUMAN: unsure*",
            "_AWF-VERDICT: NEEDS_HUMAN: unsure_",
            "***AWF-VERDICT: SHIPPED: done***",
            # Nested Markdown + emphasis must strip repeatedly.
            "> **AWF-VERDICT: NEEDS_HUMAN: unsure**",
            "- **AWF-VERDICT: SHIPPED: done**",
            "**Final answer: AWF-VERDICT: NEEDS_HUMAN: unsure**",
        ],
    )
    def test_private_awf_verdict_emphasis_wrapped_final_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # Markdown emphasis wrappers leave a leading ``**`` / ``*`` so a
        # start-only attempt check ignores the marker and an earlier
        # resolvable verdict stays selected (#822 PRRT_kwDOSJAM6s6ZmLYh).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "# AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "## AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "### AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "###### AWF-VERDICT: SHIPPED: done",
            # Nested Markdown + heading must strip repeatedly.
            "> ### AWF-VERDICT: NEEDS_HUMAN: actually unsure",
            "- ### AWF-VERDICT: SHIPPED: done",
            "### Final answer: AWF-VERDICT: NEEDS_HUMAN: actually unsure",
        ],
    )
    def test_private_awf_verdict_heading_prefixed_final_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # ATX Markdown headings leave a leading ``###`` so a start-only attempt
        # check ignores the marker while ``search()`` still sees it — an earlier
        # resolvable verdict stays selected (#822 PRRT_kwDOSJAM6s6ZmNXi).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_multiline_list_option_items_fail_closed(self) -> None:
        # Same-line mid-prose option lists already keep NEEDS_HUMAN; multiline
        # ``- AWF-VERDICT:`` option items must not select the last list entry
        # and resolve (#822 PRRT_kwDOSJAM6s6ZljVL).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose checkout policy\n"
            "- AWF-VERDICT: FALSE POSITIVE: stale nit\n"
            "- AWF-VERDICT: DEFER: track later"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_trailing_prose_marker_quote_keeps_earlier_verdict(
        self,
    ) -> None:
        # Mid-prose quotes of the marker grammar after a valid canonical line must
        # not clear last_awf_mention_recognized / drop the earlier verdict (#822).
        stdout = (
            "AWF-VERDICT: FIXED: committed a regression test\n"
            'Re-reading: "print AWF-VERDICT: FIXED: <one-sentence summary> and exit."'
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "committed a regression test"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            'See "AWF-VERDICT: FIXED: example" then Status: AWF-VERDICT: SHIPPED: done',
            'Cite "AWF-VERDICT: DEFER: track" Status: AWF-VERDICT: SHIPPED: done',
            "`AWF-VERDICT: FIXED: x` Status: AWF-VERDICT: SHIPPED: done",
            "Note 'AWF-VERDICT: FIXED: x' then Status: AWF-VERDICT: NEEDS_HUMAN: unsure",
        ],
    )
    def test_private_awf_verdict_quoted_then_unquoted_mid_prose_fail_closed(
        self,
        final_line: str,
    ) -> None:
        # Mid-prose lines kept whole (leading text before the first marker) must
        # still fail closed when a quoted citation precedes a later unquoted
        # marker — inspecting only the first match would leave is_attempt false
        # and an earlier resolvable verdict would win (#822 PRRT_kwDOSJAM6s6ZmWN6).
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_mid_prose_multi_marker_option_list_fail_closed(
        self,
    ) -> None:
        # Unquoted mid-prose option markers after a real verdict must fail closed
        # rather than resolve to a later FALSE POSITIVE / DEFER (#822
        # PRRT_kwDOSJAM6s6ZlPBt / PRRT_kwDOSJAM6s6ZmTD6). Quoted citations still
        # keep the earlier verdict; unquoted ones are garbled finals.
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose checkout policy\n"
            "Decide among: (1) AWF-VERDICT: FALSE POSITIVE: stale nit "
            "(2) AWF-VERDICT: DEFER: track later"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_quoted_marker_in_reason_keeps_needs_human(
        self,
    ) -> None:
        # A blocking verdict that quotes another complete marker in its same-line
        # reason must not split that quote into a second authoritative verdict
        # (#822 PRRT_kwDOSJAM6s6ZlQ-D).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: choose whether to emit "
            '"AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken"'
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            'choose whether to emit "AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken"'
        )

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_unquoted_marker_in_blocking_reason_keeps_needs_human(
        self,
    ) -> None:
        # Unquoted prose citations of the marker grammar inside a hard-block reason
        # must not split into a later authoritative verdict (#822 PRRT_kwDOSJAM6s6Zl4Ra).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: choose whether to emit "
            "AWF-VERDICT: FALSE POSITIVE: reviewer is wrong."
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            "choose whether to emit AWF-VERDICT: FALSE POSITIVE: reviewer is wrong."
        )

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_unquoted_marker_in_fixed_reason_keeps_fixed(
        self,
    ) -> None:
        # Unquoted marker-grammar citations inside a FIXED reason must stay rationale.
        # Splitting them lets false_positive win and bypass the HEAD-advance gate
        # (#822 PRRT_kwDOSJAM6s6Zmggp).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: stopped emitting AWF-VERDICT: FALSE POSITIVE: for valid findings"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == ("stopped emitting AWF-VERDICT: FALSE POSITIVE: for valid findings")

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_leading_defer_then_needs_human(
        self,
    ) -> None:
        # DEFER absorbs later nonblocking citations, but a later unquoted
        # NEEDS_HUMAN must still win (fail closed), not be swallowed as DEFER
        # reason prose (#822 PRRT_kwDOSJAM6s6Zl4Ra repair).
        result = _parse_verdict_result(
            "AWF-VERDICT: DEFER: maybe use AWF-VERDICT: NEEDS_HUMAN: actually block"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "actually block"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_unquoted_marker_in_defer_reason_keeps_defer(
        self,
    ) -> None:
        # Unquoted marker-grammar citations inside a DEFER reason must stay
        # rationale. Splitting them lets false_positive win and skips the DEFER
        # tracking artifact (#822 PRRT_kwDOSJAM6s6Zm6F4).
        result = _parse_verdict_result(
            "AWF-VERDICT: DEFER: stop emitting AWF-VERDICT: FALSE POSITIVE: for valid findings"
        )

        assert result.verdict == "defer"
        assert result.reason == ("stop emitting AWF-VERDICT: FALSE POSITIVE: for valid findings")

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_leading_defer_absorbs_false_positive_citation(
        self,
    ) -> None:
        # Mid-reason FALSE POSITIVE grammar after DEFER is a citation, not a
        # separate attempt — keep DEFER so tracking-issue creation still runs.
        result = _parse_verdict_result(
            "AWF-VERDICT: DEFER: track whether to emit "
            "AWF-VERDICT: FALSE POSITIVE: reviewer is wrong."
        )

        assert result.verdict == "defer"
        assert result.reason == (
            "track whether to emit AWF-VERDICT: FALSE POSITIVE: reviewer is wrong."
        )

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_unquoted_marker_in_false_positive_reason_keeps_false_positive(
        self,
    ) -> None:
        # Unquoted FIXED citations inside a FALSE POSITIVE reason must stay
        # rationale. Splitting them lets FIXED win and become
        # fixed_without_head_advance with no HEAD advance
        # (#822 PRRT_kwDOSJAM6s6ZngUH).
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: existing code already handles AWF-VERDICT: FIXED: lines"
        )

        assert result.verdict == "false_positive"
        assert result.reason == ("existing code already handles AWF-VERDICT: FIXED: lines")

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_leading_false_positive_then_needs_human(
        self,
    ) -> None:
        # FALSE POSITIVE absorbs later nonblocking citations, but a later
        # unquoted NEEDS_HUMAN must still win (fail closed).
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: maybe cite AWF-VERDICT: FIXED: x but "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_quote_adjacent_trailing_after_false_positive_still_splits(
        self,
    ) -> None:
        # Unambiguous trailing attempts after a closed quote still split for
        # FALSE POSITIVE leaders (parity with FIXED/DEFER absorption gate).
        result = _parse_verdict_result(
            'AWF-VERDICT: FALSE POSITIVE: cite "something"AWF-VERDICT: FIXED: real trailing'
        )

        assert result.verdict == "fix_committed"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_quote_adjacent_trailing_after_defer_still_splits(
        self,
    ) -> None:
        # Unambiguous trailing attempts after a closed quote still split for
        # DEFER leaders (parity with FIXED absorption gate).
        result = _parse_verdict_result(
            'AWF-VERDICT: DEFER: cite "something"AWF-VERDICT: FALSE POSITIVE: real trailing'
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_explicit_correction_splits_later_attempt(
        self,
    ) -> None:
        # Explicit self-correction separators (e.g. ``; correction:``) mark a
        # later same-line marker as a new attempt even without a closed quote.
        # Absorbing FIXED into the FALSE POSITIVE reason would resolve without
        # the HEAD-advance gate (#822 PRRT_kwDOSJAM6s6Znm-N).
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: initially misread; correction: "
            "AWF-VERDICT: FIXED: changed the implementation"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == "changed the implementation"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_corrected_to_separator_splits_later_attempt(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: first pass wrong; corrected to: "
            "AWF-VERDICT: FIXED: applied the real fix"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == "applied the real fix"

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_explicit_correction_to_false_positive(
        self,
    ) -> None:
        # NEEDS_HUMAN leaders must still honor explicit self-corrections; the
        # hard-block early return must not absorb ``correction:`` before
        # separator logic runs (#822 PRRT_kwDOSJAM6s6ZnrAH).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: initially unsure; correction: "
            "AWF-VERDICT: FALSE POSITIVE: existing behavior handles it"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "existing behavior handles it"

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_corrected_to_fixed(
        self,
    ) -> None:
        # Same separator gate for a corrected FIXED after NEEDS_HUMAN so valid
        # commit evidence is not trapped behind the hard block
        # (#822 PRRT_kwDOSJAM6s6ZnrAH).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: first pass unsure; corrected to: "
            "AWF-VERDICT: FIXED: changed the implementation"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == "changed the implementation"

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_quote_exit_trailing_marker_still_splits(
        self,
    ) -> None:
        # Quote-exit trailing attempts after NEEDS_HUMAN are unambiguous
        # separate verdicts, same as after resolvable leaders
        # (#822 PRRT_kwDOSJAM6s6ZnrAH).
        result = _parse_verdict_result(
            'AWF-VERDICT: NEEDS_HUMAN: cite "something"AWF-VERDICT: FALSE POSITIVE: real trailing'
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_bare_corrected_prose_stays_absorbed(
        self,
    ) -> None:
        # Bare past-participle ``corrected:`` remains ordinary reason prose after
        # NEEDS_HUMAN (parity with FIXED/DEFER, #822 PRRT_kwDOSJAM6s6Znq6K).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: already corrected: "
            "AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("already corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar")

    @pytest.mark.unit
    def test_private_awf_verdict_needs_human_hyphenated_self_correction_stays_absorbed(
        self,
    ) -> None:
        # Compound ``self-correction:`` must not match the explicit separator
        # (hyphen in the boundary class). Treating it as a self-correction would
        # split a later resolvable marker and clear the NEEDS_HUMAN hard block
        # (#822 PRRT_kwDOSJAM6s6ZnuQ0).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: docs mention self-correction: "
            "AWF-VERDICT: FIXED: cited grammar"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("docs mention self-correction: AWF-VERDICT: FIXED: cited grammar")

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_hyphenated_self_correction_stays_absorbed(
        self,
    ) -> None:
        # Same compound-word guard under FIXED absorption so a cited
        # ``self-correction:`` cannot become FALSE POSITIVE without HEAD advance
        # (#822 PRRT_kwDOSJAM6s6ZnuQ0).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: docs mention self-correction: "
            "AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == (
            "docs mention self-correction: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_bare_corrected_prose_stays_absorbed(
        self,
    ) -> None:
        # Bare past-participle ``corrected:`` is ordinary reason prose, not an
        # explicit self-correction separator (``correction:`` / ``corrected to:``).
        # Matching it would split a later same-line citation and let FIXED without
        # HEAD advance clear as false_positive, or DEFER skip tracking
        # (#822 PRRT_kwDOSJAM6s6Znq6K).
        fixed_result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: the bug was corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )
        defer_result = _parse_verdict_result(
            "AWF-VERDICT: DEFER: already corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

        assert fixed_result.verdict == "fix_committed"
        assert fixed_result.reason == (
            "the bug was corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )
        assert defer_result.verdict == "defer"
        assert defer_result.reason == (
            "already corrected: AWF-VERDICT: FALSE POSITIVE: cited grammar"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_quoted_correction_separator_stays_absorbed(
        self,
    ) -> None:
        # A cited correction phrase inside quotes is still reason prose, not a
        # separate attempt — only unquoted correction separators split.
        result = _parse_verdict_result(
            'AWF-VERDICT: FALSE POSITIVE: docs say "correction: '
            'AWF-VERDICT: FIXED: example" already'
        )

        assert result.verdict == "false_positive"
        assert result.reason == ('docs say "correction: AWF-VERDICT: FIXED: example" already')

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_curly_quoted_marker_keeps_needs_human(
        self,
    ) -> None:
        # Typographic quotes with prose before the cited marker must still count as
        # embedded — ASCII-only odd/even would split and let FALSE POSITIVE win
        # (#822 PRRT_kwDOSJAM6s6ZlTEh).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: cite (“print "
            "AWF-VERDICT: FALSE POSITIVE: override”) and block"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("cite (“print AWF-VERDICT: FALSE POSITIVE: override”) and block")

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_backtick_quoted_marker_keeps_needs_human(
        self,
    ) -> None:
        # Markdown code spans are a common way to cite the marker grammar; only
        # tracking ASCII/curly doubles would let the cited FALSE POSITIVE win
        # (#822 PRRT_kwDOSJAM6s6ZlTlv).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: choose whether to emit "
            "`AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken`"
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            "choose whether to emit `AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken`"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_double_backtick_span_keeps_needs_human(
        self,
    ) -> None:
        # CommonMark code spans use a run of N backticks as one delimiter; toggling
        # once per character closes a ``…`` span at the opener and lets an
        # embedded FALSE POSITIVE resolve a blocked thread (#822 PRRT_kwDOSJAM6s6Zlnbx).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: choose whether to emit "
            "``AWF-VERDICT: FALSE POSITIVE: reviewer is wrong``"
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            "choose whether to emit ``AWF-VERDICT: FALSE POSITIVE: reviewer is wrong``"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_double_backtick_span_with_inner_single_keeps_needs_human(
        self,
    ) -> None:
        # Inner single-backtick runs are content inside a double-backtick span and
        # must not close it early (#822 PRRT_kwDOSJAM6s6Zlnbx).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: cite ``print "
            "`AWF-VERDICT: FALSE POSITIVE: override` and block``"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("cite ``print `AWF-VERDICT: FALSE POSITIVE: override` and block``")

    @pytest.mark.unit
    def test_private_awf_verdict_closing_double_backtick_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        # Matching double-backtick close must leave the parser outside so a real
        # trailing marker still wins after a resolvable leading verdict
        # (#822 PRRT_kwDOSJAM6s6Zlnbx).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: cite ``something``AWF-VERDICT: FALSE POSITIVE: real trailing"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_single_quoted_marker_keeps_needs_human(
        self,
    ) -> None:
        # ASCII single quotes delimit reason-prose citations the same way doubles do
        # (#822 PRRT_kwDOSJAM6s6ZlTlv).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: choose whether to emit "
            "'AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken'"
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            "choose whether to emit 'AWF-VERDICT: FALSE POSITIVE: reviewer is mistaken'"
        )

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_curly_single_quoted_marker_keeps_needs_human(
        self,
    ) -> None:
        # Typographic single quotes must track open/close like curly doubles
        # (#822 PRRT_kwDOSJAM6s6ZlTlv).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: cite (‘print "
            "AWF-VERDICT: FALSE POSITIVE: override’) and block"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("cite (‘print AWF-VERDICT: FALSE POSITIVE: override’) and block")

    @pytest.mark.unit
    def test_private_awf_verdict_closing_quote_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        # A closing quote immediately before a real trailing marker is outside the
        # quote — must not be treated as still embedded (#822 PRRT_kwDOSJAM6s6ZlTEh).
        result = _parse_verdict_result(
            'AWF-VERDICT: FIXED: cite "something"AWF-VERDICT: FALSE POSITIVE: real trailing'
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_backtick_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        # Closed Markdown code span immediately before a real trailing marker must
        # still split (#822 PRRT_kwDOSJAM6s6ZlTlv).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: cite `something`AWF-VERDICT: FALSE POSITIVE: real trailing"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_single_quote_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: cite 'something'AWF-VERDICT: FALSE POSITIVE: real trailing"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_single_quote_jammed_before_lowercase_still_splits(
        self,
    ) -> None:
        # A closer jammed against a following lowercase token ('strict'by) must
        # still close — otherwise quote state stays open and a trailing unquoted
        # blocker is absorbed into an earlier resolvable verdict
        # (#822 PRRT_kwDOSJAM6s6ZlYG3).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: use 'strict'by default "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    def test_private_awf_verdict_contraction_inside_single_quotes_keeps_embedded_marker(
        self,
    ) -> None:
        # Contractions inside a real ASCII single-quoted span must not close the
        # quote early, or a mid-prose marker citation would incorrectly split.
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: docs say 'it's AWF-VERDICT: FIXED: done' as an example"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("docs say 'it's AWF-VERDICT: FIXED: done' as an example")

    @pytest.mark.unit
    def test_private_awf_verdict_inch_mark_in_reason_does_not_absorb_trailing_marker(
        self,
    ) -> None:
        # Unmatched ASCII inch/unit marks (``5"``) must not open quote state or a
        # later unquoted same-line blocker is absorbed into an earlier resolvable
        # verdict (#822 PRRT_kwDOSJAM6s6ZlciX).
        result = _parse_verdict_result(
            'AWF-VERDICT: FALSE POSITIVE: the 5" screen is expected '
            "AWF-VERDICT: NEEDS_HUMAN: actually unsure."
        )

        assert result.verdict == "needs_human"
        assert result.reason == "actually unsure."

    @pytest.mark.unit
    def test_private_awf_verdict_escaped_quote_in_reason_does_not_absorb_trailing_marker(
        self,
    ) -> None:
        # Backslash-escaped ASCII quotes (``\"``) are literal reason text, not
        # delimiters. Treating them as openers absorbs a later unquoted same-line
        # blocker into an earlier resolvable verdict (#822 PRRT_kwDOSJAM6s6ZlzwP).
        result = _parse_verdict_result(
            r"AWF-VERDICT: FALSE POSITIVE: expected text starts with \"foo "
            "AWF-VERDICT: NEEDS_HUMAN: actually unsure"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "actually unsure"

    @pytest.mark.unit
    def test_private_awf_verdict_escaped_quotes_inside_real_span_keep_embedded_marker(
        self,
    ) -> None:
        # Escaped quotes inside a real ASCII double-quoted citation must not close
        # the span early or a mid-prose marker citation would incorrectly split
        # (#822 PRRT_kwDOSJAM6s6ZlzwP).
        result = _parse_verdict_result(
            r'AWF-VERDICT: NEEDS_HUMAN: cite "print \"AWF-VERDICT: FALSE POSITIVE: '
            r'override\" here" and block'
        )

        assert result.verdict == "needs_human"
        assert result.reason == (
            r'cite "print \"AWF-VERDICT: FALSE POSITIVE: override\" here" and block'
        )

    @pytest.mark.unit
    def test_private_awf_verdict_escaped_single_quote_in_reason_does_not_absorb_trailing_marker(
        self,
    ) -> None:
        # Same escape rule for ASCII single quotes (#822 PRRT_kwDOSJAM6s6ZlzwP).
        result = _parse_verdict_result(
            r"AWF-VERDICT: FALSE POSITIVE: expected text starts with \'foo "
            "AWF-VERDICT: NEEDS_HUMAN: actually unsure"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "actually unsure"

    @pytest.mark.unit
    def test_private_awf_verdict_double_backslash_before_quote_still_delimits(
        self,
    ) -> None:
        # An even backslash run (``\\"``) leaves a real delimiter; an unclosed
        # quote must still absorb the trailing marker (#822 PRRT_kwDOSJAM6s6ZlzwP).
        result = _parse_verdict_result(
            r"AWF-VERDICT: FALSE POSITIVE: path ends with \\"
            '"foo AWF-VERDICT: NEEDS_HUMAN: actually unsure'
        )

        assert result.verdict == "false_positive"
        assert result.reason == r'path ends with \\"foo AWF-VERDICT: NEEDS_HUMAN: actually unsure'

    @pytest.mark.unit
    def test_private_awf_verdict_apostrophe_in_reason_does_not_absorb_trailing_marker(
        self,
    ) -> None:
        # ASCII apostrophes in contractions/possessives must not toggle quote state
        # or a later unquoted same-line marker is treated as embedded and absorbed
        # (#822 PRRT_kwDOSJAM6s6ZlVIN). Use an odd count so naive toggle cannot
        # accidentally land outside-quote again.
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: don't absorb the trailing blocker "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reason_with_elision",
        [
            "drop 'em safely",
            "wait 'til merge",
            "'cause the guard holds",
        ],
    )
    def test_private_awf_verdict_leading_elision_does_not_absorb_trailing_marker(
        self,
        reason_with_elision: str,
    ) -> None:
        # Whitespace-prefixed elisions ('em, 'til, 'cause) must not open an
        # unclosed ASCII single-quote span, or a later unquoted same-line
        # blocker is absorbed into an earlier resolvable verdict
        # (#822 PRRT_kwDOSJAM6s6ZlgbO).
        result = _parse_verdict_result(
            f"AWF-VERDICT: FIXED: {reason_with_elision} "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    def test_private_awf_verdict_leading_elision_trailing_garbled_still_fail_closed(
        self,
    ) -> None:
        # Same elision trap must not absorb a trailing garbled marker into an
        # earlier resolvable verdict (#822 PRRT_kwDOSJAM6s6ZlgbO).
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: drop 'em already AWF-VERDICT: SHIPPED: done"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_elision_inside_single_quotes_keeps_embedded_marker(
        self,
    ) -> None:
        # Leading elisions inside a real ASCII single-quoted span must not close
        # the quote early, or a mid-prose marker citation would incorrectly split
        # (#822 PRRT_kwDOSJAM6s6ZlgbO).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: docs say 'drop 'em AWF-VERDICT: FIXED: done' as an example"
        )

        assert result.verdict == "needs_human"
        assert result.reason == ("docs say 'drop 'em AWF-VERDICT: FIXED: done' as an example")

    @pytest.mark.unit
    def test_private_awf_verdict_apostrophe_in_reason_trailing_garbled_still_fail_closed(
        self,
    ) -> None:
        # Same apostrophe trap must not absorb a trailing garbled marker into an
        # earlier resolvable verdict (#822 PRRT_kwDOSJAM6s6ZlVIN).
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: it's already correct AWF-VERDICT: SHIPPED: done"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_trailing_garbled_marker_fail_closed(
        self,
    ) -> None:
        # Same-line trailing markers must not be absorbed into the first reason
        # group — the final garbled marker is authoritative and fails closed.
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: rationale AWF-VERDICT: SHIPPED: done"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_fixed_absorbs_unquoted_later_resolvable(
        self,
    ) -> None:
        # FIXED reasons may cite later resolvable marker grammar without quotes;
        # those stay rationale so false_positive cannot bypass HEAD-advance
        # (#822 PRRT_kwDOSJAM6s6Zmggp). Unambiguous trailing attempts after a
        # closed quote still split (see adjacent-quote trailing tests).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: interim note AWF-VERDICT: FALSE POSITIVE: final rationale"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == ("interim note AWF-VERDICT: FALSE POSITIVE: final rationale")

    @pytest.mark.unit
    def test_private_awf_verdict_same_line_fixed_then_unquoted_needs_human_still_wins(
        self,
    ) -> None:
        # FIXED absorbs resolvable citations, but a later unquoted NEEDS_HUMAN
        # must still split and fail closed.
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: maybe cite AWF-VERDICT: FALSE POSITIVE: x but "
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must decide"
        )

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must decide"

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_reason_keeps_inline_angle_bracket_term(self) -> None:
        # Prompt-template detection must not treat ordinary mid-reason tags such as
        # HTML-ish ``<summary>`` as a whole-reason placeholder echo (#822 Greptile).
        result = _parse_verdict_result("AWF-VERDICT: FIXED: added the <summary> section")

        assert result.verdict == "fix_committed"
        assert result.reason == "added the <summary> section"

    @pytest.mark.unit
    def test_private_awf_verdict_false_positive_reason_keeps_inline_angle_bracket_term(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: docs already document the <reason> field"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "docs already document the <reason> field"

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_reason_keeps_leading_angle_bracket_term(
        self,
    ) -> None:
        # Start-anchored placeholder detection must not strip reasons that merely
        # begin with a template-shaped tag and continue with real content
        # (#822 PRRT_kwDOSJAM6s6ZlK2d).
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: <summary> section was rewritten with a null check"
        )

        assert result.verdict == "fix_committed"
        assert result.reason == "<summary> section was rewritten with a null check"

    @pytest.mark.unit
    def test_private_awf_verdict_false_positive_reason_keeps_leading_angle_bracket_term(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FALSE POSITIVE: <reason> field already documents this"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "<reason> field already documents this"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "reason",
        [
            "...fixed the null check in helpers",
            "…because the branch already handles None",
        ],
    )
    def test_private_awf_verdict_fixed_reason_keeps_leading_ellipsis_content(
        self,
        reason: str,
    ) -> None:
        result = _parse_verdict_result(f"AWF-VERDICT: FIXED: {reason}")

        assert result.verdict == "fix_committed"
        assert result.reason == reason

    @pytest.mark.unit
    def test_private_awf_verdict_fixed_marker_preserves_reason(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: FIXED: pushed regression test")

        assert result.verdict == "fix_committed"
        assert result.reason == "pushed regression test"

    @pytest.mark.unit
    def test_awf_false_positive_case_insensitive(self) -> None:
        assert _parse_verdict("AWF-VERDICT: false positive: minor") == "false_positive"

    @pytest.mark.unit
    def test_plain_reply_fail_closed_as_needs_human(self) -> None:
        result = _parse_verdict_result("Committed fix in abc1234: renamed variable.")

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_whitespace_only_stdout_needs_human(self) -> None:
        result = _parse_verdict_result("   \n\t  ")

        assert result.verdict == "needs_human"
        assert result.reason == "empty_verdict_output"

    @pytest.mark.unit
    def test_empty_stdout_includes_fail_closed_reason(self) -> None:
        result = _parse_verdict_result("")

        assert result.verdict == "needs_human"
        assert result.reason == "empty_verdict_output"

    @pytest.mark.unit
    def test_garbled_awf_verdict_marker_fail_closed(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: COMPLETELY_BOGUS: not a real label")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_unrecognized_awf_verdict_label_fail_closed(self) -> None:
        result = _parse_verdict_result("AWF-VERDICT: SHIPPED: done")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_bare_only_mixed_verdicts_fail_closed(self) -> None:
        # Without an AWF marker, precedence among bare lines is irrelevant —
        # all fail closed rather than selecting FALSE POSITIVE / DEFER.
        reply = "FALSE POSITIVE: not a real issue.\nDEFER: follow-up issue"
        result = _parse_verdict_result(reply)

        assert result.verdict == "needs_human"
        assert result.reason == "unrecognized_or_markerless_verdict"

    @pytest.mark.unit
    def test_later_defer_does_not_overwrite_awf_needs_human(self) -> None:
        # AWF ``NEEDS_HUMAN`` must keep merge-blocking priority over later bare defer.
        reply = "AWF-VERDICT: NEEDS_HUMAN: follow-up needed\nDEFER: follow-up issue"
        assert _parse_verdict(reply) == "needs_human"

    @pytest.mark.unit
    def test_later_bare_false_positive_does_not_overwrite_awf_needs_human(self) -> None:
        # Bare false-positive text must not clear an AWF hard block.
        reply = "AWF-VERDICT: NEEDS_HUMAN: follow-up needed\nFALSE POSITIVE: not a real issue"
        assert _parse_verdict(reply) == "needs_human"

    @pytest.mark.unit
    def test_awf_false_positive_takes_precedence_over_awf_defer(self) -> None:
        reply = (
            "AWF-VERDICT: DEFER: fix this later\nAWF-VERDICT: FALSE POSITIVE: not a real problem"
        )
        assert _parse_verdict(reply) == "false_positive"

    @pytest.mark.unit
    def test_monitor_state_verdict_normalizes_persisted_private_verdicts(self) -> None:
        # #305: needs_human is now its own verdict, no longer collapsed to defer.
        assert _monitor_state_verdict("NEEDS_HUMAN") == "needs_human"
        assert _monitor_state_verdict("defer") == "defer"
        assert _monitor_state_verdict("agent_failed") == "agent_failed"
        assert _monitor_state_verdict("fixed") == "fix_committed"
