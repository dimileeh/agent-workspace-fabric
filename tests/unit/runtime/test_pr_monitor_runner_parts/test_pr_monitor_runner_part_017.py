"""Unit tests for verdict parsing helpers."""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _html_code_close_appears_later,
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
        # Complete ``<code>`` is type 7 and cannot interrupt a paragraph — blank
        # before the opener is required for shielding (PRRT_kwDOSJAM6s6ZqS4U).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "\n"
            "<code>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "</code>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_after_html_code_until_blank(
        self,
    ) -> None:
        # Complete ``<code>`` openers keep a blank-terminated tail after
        # ``</code>``. Ending the shield at ``</code>`` alone lets a
        # FALSE POSITIVE before the blank override NEEDS_HUMAN
        # (PRRT_kwDOSJAM6s6ZpLqP).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "\n"
            "<code>\n"
            "example inside\n"
            "</code>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_ignores_markers_after_blank_inside_html_code(
        self,
    ) -> None:
        # Pure type-7 blank termination for ``<code>`` ends shielding on an
        # interior blank, so a later resolvable marker before ``</code>``
        # overrides NEEDS_HUMAN (PRRT_kwDOSJAM6s6ZpPQA). Keep close-tag
        # shielding through the wrapper, then the blank-terminated tail.
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "\n"
            "<code>\n"
            "AWF-VERDICT: FALSE POSITIVE: before blank\n"
            "\n"
            "AWF-VERDICT: FIXED: after blank inside code\n"
            "</code>\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "clarify intent"

    @pytest.mark.unit
    def test_private_awf_verdict_self_closing_html_code_blank_terminates(
        self,
    ) -> None:
        # Hybrid close-tag mode for complete ``<code>`` never sees ``</code>``
        # on ``<code/>``, so blank-tail never starts and a real trailing FIXED
        # stays suppressed through EOF (PRRT_kwDOSJAM6s6ZpTPI). Self-closing
        # must stay blank-terminated like other type-7 tags.
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "\n"
            "<code/>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "\n"
            "AWF-VERDICT: FIXED: real trailing fix\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real trailing fix"

    @pytest.mark.unit
    def test_private_awf_verdict_never_closed_html_code_blank_terminates(
        self,
    ) -> None:
        # Never-closed complete ``<code>`` also never reaches blank-tail under
        # hybrid mode, so blanks no longer end shielding and FIXED after the
        # blank is suppressed through EOF (PRRT_kwDOSJAM6s6ZpTPI). Without a
        # later ``</code>``, use type-7 blank termination.
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "\n"
            "<code>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "\n"
            "AWF-VERDICT: FIXED: real trailing fix\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real trailing fix"

    @pytest.mark.unit
    def test_private_awf_verdict_many_unclosed_html_code_openers_stay_linear(
        self,
    ) -> None:
        # Repeated blank-separated unclosed ``<code>`` openers must not
        # rescan the remaining suffix on every opener (PRRT_kwDOSJAM6s6ZpaIp).
        # Behavior stays blank-terminated so a trailing FIXED after the last
        # blank remains authoritative.
        repeated = "".join("<code>\n\n" for _ in range(200))
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "\n"
            f"{repeated}"
            "AWF-VERDICT: FIXED: real trailing fix\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real trailing fix"

    @pytest.mark.unit
    def test_private_awf_verdict_many_html_code_openers_with_late_closer(
        self,
    ) -> None:
        # Precomputed look-ahead must still enable hybrid close-tag + blank-tail
        # when a later ``</code>`` exists after many prior openers
        # (PRRT_kwDOSJAM6s6ZpaIp / PRRT_kwDOSJAM6s6ZpLqP).
        repeated = "".join("<code>\n\n" for _ in range(50))
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: clarify intent\n"
            "\n"
            f"{repeated}"
            "<code>\n"
            "example inside\n"
            "</code>\n"
            "AWF-VERDICT: FALSE POSITIVE: example\n"
            "\n"
            "AWF-VERDICT: FIXED: real trailing fix\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "fix_committed"
        assert result.reason == "real trailing fix"

    @pytest.mark.unit
    def test_html_code_close_appears_later_single_query_helper(self) -> None:
        # Iterator uses the precomputed table; keep the single-query helper
        # correct for out-of-range and suffix look-ahead (PRRT_kwDOSJAM6s6ZpaIp).
        lines = ["<code>", "body", "</code>", "after"]

        assert _html_code_close_appears_later(lines, 0) is True
        assert _html_code_close_appears_later(lines, 2) is True
        assert _html_code_close_appears_later(lines, 3) is False
        assert _html_code_close_appears_later(lines, 4) is False
        assert _html_code_close_appears_later(lines, -1) is False

    @pytest.mark.unit
    def test_html_code_close_appears_later_ignores_fenced_closer(self) -> None:
        # A ``</code>`` only inside a Markdown fence must not count as a later
        # hybrid closer (PRRT_kwDOSJAM6s6ZpoBt).
        lines = ["<code>", "body", "```", "</code>", "```", "after"]

        assert _html_code_close_appears_later(lines, 0) is False
        assert _html_code_close_appears_later(lines, 1) is False

    @pytest.mark.unit
    def test_html_code_close_appears_later_ignores_comment_closer(self) -> None:
        # Same for HTML comments: example ``</code>`` text is not a hybrid
        # closer (PRRT_kwDOSJAM6s6ZpoBt).
        lines = ["<code>", "body", "<!--", "</code>", "-->", "after"]

        assert _html_code_close_appears_later(lines, 0) is False
        assert _html_code_close_appears_later(lines, 1) is False

    @pytest.mark.unit
    def test_private_awf_verdict_fenced_code_closer_does_not_hybrid_suppress_needs_human(
        self,
    ) -> None:
        # Context-insensitive look-ahead treated fenced ``</code>`` as a real
        # closer, entered hybrid shielding past the blank, and suppressed the
        # explicit NEEDS_HUMAN so an earlier FIXED stayed authoritative
        # (PRRT_kwDOSJAM6s6ZpoBt).
        stdout = (
            "AWF-VERDICT: FIXED: earlier evidence-backed fix\n"
            "<code>\n"
            "AWF-VERDICT: FALSE POSITIVE: example inside\n"
            "\n"
            "AWF-VERDICT: NEEDS_HUMAN: agent blocked this\n"
            "```\n"
            "</code>\n"
            "```\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "agent blocked this"

    @pytest.mark.unit
    def test_private_awf_verdict_comment_code_closer_does_not_hybrid_suppress_needs_human(
        self,
    ) -> None:
        # Same defect when the only later ``</code>`` sits inside an HTML
        # comment (PRRT_kwDOSJAM6s6ZpoBt).
        stdout = (
            "AWF-VERDICT: FIXED: earlier evidence-backed fix\n"
            "<code>\n"
            "AWF-VERDICT: FALSE POSITIVE: example inside\n"
            "\n"
            "AWF-VERDICT: NEEDS_HUMAN: agent blocked this\n"
            "<!--\n"
            "</code>\n"
            "-->\n"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "agent blocked this"

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
    @pytest.mark.parametrize(
        "stdout",
        [
            (
                "<!--\nAWF-VERDICT: FALSE POSITIVE: example\n--!>\n"
                "AWF-VERDICT: FIXED: real fix landed\n"
            ),
            (
                "<!-- AWF-VERDICT: FALSE POSITIVE: example --!>\n"
                "AWF-VERDICT: FIXED: real fix landed\n"
            ),
        ],
        ids=["multi_line_bang_close", "same_line_bang_close"],
    )
    def test_private_awf_verdict_unfenced_after_bang_closed_html_comment_still_wins(
        self,
        stdout: str,
    ) -> None:
        # ``--!>`` is a comment end tag (HTML "comment end bang state"), so the
        # trailing marker is the authoritative verdict a reader sees rendered.
        # Treating only ``-->`` as the closer kept the shield open forever and
        # silently dropped that real marker (CodeQL py/bad-tag-filter).
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
