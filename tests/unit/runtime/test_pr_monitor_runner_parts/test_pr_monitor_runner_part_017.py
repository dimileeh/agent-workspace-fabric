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
    def test_private_awf_verdict_mid_prose_multi_marker_option_list_keeps_earlier_verdict(
        self,
    ) -> None:
        # Splitting multi-marker lines must not drop leading prose — otherwise
        # later quoted markers become leading attempts and can override an
        # earlier real verdict (#822 PRRT_kwDOSJAM6s6ZlPBt).
        stdout = (
            "AWF-VERDICT: NEEDS_HUMAN: maintainer must choose checkout policy\n"
            "Decide among: (1) AWF-VERDICT: FALSE POSITIVE: stale nit "
            "(2) AWF-VERDICT: DEFER: track later"
        )

        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == "maintainer must choose checkout policy"

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
        # trailing marker still wins (#822 PRRT_kwDOSJAM6s6Zlnbx).
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: cite ``something``AWF-VERDICT: FALSE POSITIVE: real trailing"
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
            'AWF-VERDICT: NEEDS_HUMAN: cite "something"AWF-VERDICT: FALSE POSITIVE: real trailing'
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
            "AWF-VERDICT: NEEDS_HUMAN: cite `something`AWF-VERDICT: FALSE POSITIVE: real trailing"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "real trailing"

    @pytest.mark.unit
    def test_private_awf_verdict_closing_single_quote_adjacent_trailing_marker_still_splits(
        self,
    ) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: NEEDS_HUMAN: cite 'something'AWF-VERDICT: FALSE POSITIVE: real trailing"
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
    def test_private_awf_verdict_same_line_second_valid_marker_wins(self) -> None:
        result = _parse_verdict_result(
            "AWF-VERDICT: FIXED: interim note AWF-VERDICT: FALSE POSITIVE: final rationale"
        )

        assert result.verdict == "false_positive"
        assert result.reason == "final rationale"

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
