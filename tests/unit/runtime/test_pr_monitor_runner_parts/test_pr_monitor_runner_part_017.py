"""Unit tests for verdict parsing helpers."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
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
    def test_private_awf_verdict_ignores_blockquote_indented_code_example(self) -> None:
        # Four-column indented code inside a blockquote starts with ``>``, so a
        # raw-line indent check misses it; attempt detection then peels the
        # quote and treats the example as a garbled final (PRRT_kwDOSJAM6s6Zoxg4).
        stdout = (
            "AWF-VERDICT: FIXED: committed the quote-indent skip\n"
            ">     AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "committed the quote-indent skip"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "quoted_indent",
        [
            ">     ",
            ">>     ",
            "> >     ",
            "> \t",
            "   >     ",
        ],
        ids=[
            "single_bq_four_spaces",
            "nested_bq_four_spaces",
            "spaced_nested_bq_four_spaces",
            "bq_space_tab",
            "indented_bq_four_spaces",
        ],
    )
    def test_private_awf_verdict_ignores_nested_blockquote_indented_code(
        self, quoted_indent: str
    ) -> None:
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            f"{quoted_indent}AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "list_indent",
        [
            "-     ",
            "*     ",
            "1.     ",
            "- \t",
            "   -     ",
            "> -     ",
            "- >     ",
        ],
        ids=[
            "bullet_four_spaces",
            "star_four_spaces",
            "ordered_four_spaces",
            "bullet_space_tab",
            "indented_bullet_four_spaces",
            "bq_then_list_four_spaces",
            "list_then_bq_four_spaces",
        ],
    )
    def test_private_awf_verdict_ignores_list_nested_indented_code(self, list_indent: str) -> None:
        # Four-column indented code nested in a list starts with ``-`` / ``1.``,
        # so raw-line and blockquote-only peels miss it; attempt detection then
        # treats the example as a garbled final over an earlier FIXED
        # (PRRT_kwDOSJAM6s6Zo4bL).
        stdout = (
            "AWF-VERDICT: FIXED: committed the list-indent skip\n"
            f"{list_indent}AWF-VERDICT: FALSE POSITIVE: example\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "committed the list-indent skip"

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
    @pytest.mark.parametrize(
        ("stdout", "reason"),
        [
            (
                (
                    "- ~~~text\n"
                    "  AWF-VERDICT: FALSE POSITIVE: example\n"
                    "\t~~~\n"
                    "AWF-VERDICT: FIXED: closed tab-indented list fence\n"
                ),
                "closed tab-indented list fence",
            ),
            (
                (
                    "10. ~~~text\n"
                    "    AWF-VERDICT: FALSE POSITIVE: example\n"
                    "\t~~~\n"
                    "AWF-VERDICT: FIXED: closed tab-indented ordered fence\n"
                ),
                "closed tab-indented ordered fence",
            ),
        ],
        ids=["unordered_list", "ordered_list"],
    )
    def test_private_awf_verdict_list_fence_tab_indented_closer(
        self,
        stdout: str,
        reason: str,
    ) -> None:
        # CommonMark expands a leading tab to four indent columns. List-nested
        # openers (``- ~~~text`` / ``10. ~~~text``) may therefore close with
        # ``\t~~~``. A spaces-only indent regex misses that closer and shields
        # every later line, including a real top-level FIXED
        # (PRRT_kwDOSJAM6s6Zolex).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == reason

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
    @pytest.mark.parametrize(
        ("stdout", "reason"),
        [
            (
                (
                    "-\t-\t~~~text\n"
                    "        AWF-VERDICT: FALSE POSITIVE: example\n"
                    "        ~~~\n"
                    "AWF-VERDICT: FIXED: closed nested tab-padded list fence\n"
                ),
                "closed nested tab-padded list fence",
            ),
            (
                (
                    "-\t-\t~~~text\n"
                    "        AWF-VERDICT: FALSE POSITIVE: example\n"
                    "\t\t~~~\n"
                    "AWF-VERDICT: FIXED: closed nested tab-padded list fence via tabs\n"
                ),
                "closed nested tab-padded list fence via tabs",
            ),
            (
                (
                    "-\t~~~text\n"
                    "    AWF-VERDICT: FALSE POSITIVE: example\n"
                    "    ~~~\n"
                    "AWF-VERDICT: FIXED: closed tab-padded list fence\n"
                ),
                "closed tab-padded list fence",
            ),
        ],
        ids=["nested_spaces_closer", "nested_tabs_closer", "single_tab_marker"],
    )
    def test_private_awf_verdict_list_fence_tab_padded_marker_container_indent(
        self,
        stdout: str,
        reason: str,
    ) -> None:
        # Tab-padded list markers occupy four CommonMark columns each, but
        # character-based ``lst.end()`` counted ``-\t`` as width 2. Nested
        # ``-\t-\t~~~text`` therefore recorded container_indent 4 (max closer
        # indent 7) instead of 8, rejecting the valid eight-column closer and
        # shielding a later top-level FIXED (PRRT_kwDOSJAM6s6ZopxJ).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == reason

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
