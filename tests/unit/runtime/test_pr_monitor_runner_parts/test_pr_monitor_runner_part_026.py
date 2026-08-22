"""Unit tests for verdict parsing helpers (part 026).

Emphasis-balance, closer-validity, and link-reference coverage split from
part 021; reference-definition title tests continue in part 028.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _parse_verdict_result,
)
from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _markdown_line_is_leaf_block_boundary,
    _markdown_reference_definition_awaits_destination,
    _markdown_reference_definition_awaits_title,
    _markdown_reference_definition_spans,
    _match_markdown_reference_definition_line,
    _normalize_markdown_emphasized_verdict_line,
    _verdict_reason_trailing_emphasis_is_balanced,
)


class TestParseVerdict:
    @pytest.mark.unit
    def test_private_markdown_emphasis_normalizer_rejects_multiline_candidate(
        self,
    ) -> None:
        assert (
            _normalize_markdown_emphasized_verdict_line(
                "**AWF-VERDICT: FALSE POSITIVE:** rationale\ntrailing prose"
            )
            is None
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stdout", "expected_verdict", "expected_reason"),
        [
            (
                "AWF-VERDICT: FALSE POSITIVE: earlier\n"
                "**AWF-VERDICT: NEEDS_HUMAN: maintainer decision**",
                "needs_human",
                "maintainer decision",
            ),
            (
                "AWF-VERDICT: NEEDS_HUMAN: earlier blocker\n"
                "**AWF-VERDICT: FALSE POSITIVE:** review wrapper only",
                "false_positive",
                "review wrapper only",
            ),
        ],
    )
    def test_private_awf_verdict_emphasized_final_preserves_final_marker_precedence(
        self,
        stdout: str,
        expected_verdict: str,
        expected_reason: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == expected_verdict
        assert result.reason == expected_reason

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stdout", "expected_reason"),
        [
            ("**AWF-VERDICT: FIXED: <one-sentence summary>**", "fixed_placeholder_echo"),
            ("**AWF-VERDICT: FALSE POSITIVE:** <reason>", "verdict_placeholder_echo"),
            ("__AWF-VERDICT: DEFER:__ <what to track>", "verdict_placeholder_echo"),
        ],
    )
    def test_private_awf_emphasized_resolvable_placeholders_still_fail_closed(
        self,
        stdout: str,
        expected_reason: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == expected_reason

    @pytest.mark.unit
    def test_private_markdown_emphasis_normalizer_distinguishes_placeholder_prefix_spacing(
        self,
    ) -> None:
        # Space-separated prefix wraps must normalize so parsing classifies
        # ``verdict_placeholder_echo`` instead of ``garbled_verdict_marker``
        # (review comment 4999396335).
        assert (
            _normalize_markdown_emphasized_verdict_line("**AWF-VERDICT: FALSE POSITIVE:** <reason>")
            == "AWF-VERDICT: FALSE POSITIVE: <reason>"
        )
        # Adjacent placeholder echoes must not strip prefix markers.
        assert (
            _normalize_markdown_emphasized_verdict_line("**AWF-VERDICT: FALSE POSITIVE:**<reason>")
            is None
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "final_line",
        [
            "***AWF-VERDICT: SHIPPED: done***",
            "> **AWF-VERDICT: NEEDS_HUMAN: unsure**",
            "- **AWF-VERDICT: SHIPPED: done**",
            "**Final answer: AWF-VERDICT: NEEDS_HUMAN: unsure**",
            "**AWF-VERDICT: NEEDS_HUMAN: unbalanced",
            "**AWF-VERDICT: NEEDS_HUMAN: mismatched__",
            "**AWF-VERDICT: NEEDS_HUMAN: extra closer***",
            "*AWF-VERDICT: NEEDS_HUMAN:** extra closer",
            # Whitespace before closer is not right-flanking (PRRT_kwDOSJAM6s6bQoY6).
            "**AWF-VERDICT: FALSE POSITIVE: ** rationale",
            "**AWF-VERDICT: FALSE POSITIVE: rationale **",
            "*AWF-VERDICT: FALSE POSITIVE: * rationale",
            "__AWF-VERDICT: FALSE POSITIVE: __ rationale",
            # Backslash-escaped closer stars/underscores cannot close the opener.
            r"**AWF-VERDICT: FALSE POSITIVE: rationale \**",
            r"**AWF-VERDICT: FALSE POSITIVE: rationale\**",
            r"*AWF-VERDICT: FALSE POSITIVE: rationale \*",
            r"__AWF-VERDICT: FALSE POSITIVE: rationale \__",
            # Prefix + trailing same-delimiter closer is malformed, not addressed
            # (PRRT_kwDOSJAM6s6bQo0J).
            "**AWF-VERDICT: FALSE POSITIVE:** rationale**",
            "*AWF-VERDICT: FALSE POSITIVE:* rationale*",
            "__AWF-VERDICT: FALSE POSITIVE:__ rationale__",
            # Two closing-only runs are even-parity but not an opener/closer pair
            # (PRRT_kwDOSJAM6s6bRfTo).
            "**AWF-VERDICT: FALSE POSITIVE:** rationale** more**",
            "*AWF-VERDICT: FALSE POSITIVE:* rationale* more*",
            "__AWF-VERDICT: FALSE POSITIVE:__ rationale__ more__",
            # Alphanumeric-to-punctuation mid run is closing-only; pairing it as
            # an opener would wrongly accept a false_positive (PRRT_kwDOSJAM6s6bSOmb).
            "**AWF-VERDICT: FALSE POSITIVE:** rationale a**. more**",
            "*AWF-VERDICT: FALSE POSITIVE:* rationale a*. more*",
            "__AWF-VERDICT: FALSE POSITIVE:__ rationale a__. more__",
            # ASCII punctuation whose Unicode category is not P* (Sc/Sm/Sk) still
            # counts as CommonMark punctuation; otherwise the mid run opens and
            # the line wrongly resolves (PRRT_kwDOSJAM6s6bSZP4).
            "**AWF-VERDICT: FALSE POSITIVE:** rationale a**$ more**",
            "*AWF-VERDICT: FALSE POSITIVE:* rationale a*$ more*",
            "__AWF-VERDICT: FALSE POSITIVE:__ rationale a__$ more__",
            "**AWF-VERDICT: FALSE POSITIVE:** rationale a**+ more**",
            "**AWF-VERDICT: FALSE POSITIVE:** rationale a**^ more**",
            # Closing-only mid run would close the line-leading wrapper; an empty
            # balance stack must not ignore it and accept the trailing delimiter
            # as a whole-line closer (PRRT_kwDOSJAM6s6bUx1A).
            "**AWF-VERDICT: FALSE POSITIVE: rationale** more**",
            "*AWF-VERDICT: FALSE POSITIVE: rationale* more*",
            "__AWF-VERDICT: FALSE POSITIVE: rationale__ more__",
            # Mid-reason opener + trailing closer is not a whole-line wrap
            # (PRRT_kwDOSJAM6s6bRrWv).
            "**AWF-VERDICT: FALSE POSITIVE: rationale **unclosed**",
            "*AWF-VERDICT: FALSE POSITIVE: rationale *unclosed*",
            "__AWF-VERDICT: FALSE POSITIVE: rationale __unclosed__",
            "_AWF-VERDICT: FALSE POSITIVE: rationale _unclosed_",
            # Rule 9 blocks the nearest mid ``*`` against trailing ``**``, but
            # CommonMark continues to the earlier ``**`` opener — trailing is
            # stolen and the outer wrap must not resolve (PRRT_kwDOSJAM6s6bTtr5).
            # Underscore uses both-flanking ``._.`` (intra-word ``a_b`` is inert).
            "**AWF-VERDICT: FALSE POSITIVE: reason **lower a*b**",
            "__AWF-VERDICT: FALSE POSITIVE: reason __lower ._.x__",
            # Partial longer-run match steals the trailing closer
            # (PRRT_kwDOSJAM6s6bR2FM). Both-flanking openers blocked by rule 9
            # against a complementary-length closer (``**lead*``) do not steal
            # and remain valid whole-line wraps (PRRT_kwDOSJAM6s6bTW7t).
            "**AWF-VERDICT: FALSE POSITIVE: ***lead* rest**",
            # Reason-leading complementary openers at BOS are opening-only and
            # steal the wrapper closer — fail closed (PRRT_kwDOSJAM6s6bTi4S).
            "**AWF-VERDICT: FALSE POSITIVE: *foo**",
            "*AWF-VERDICT: FALSE POSITIVE: **lead* rest*",
            "__AWF-VERDICT: FALSE POSITIVE: _foo__",
            "_AWF-VERDICT: FALSE POSITIVE: __lead_ rest_",
            # Punctuation-to-alphanumeric mid run is opening-only; consuming it as
            # a closer would wrongly resolve false_positive (PRRT_kwDOSJAM6s6bShqh).
            "**AWF-VERDICT: FALSE POSITIVE: lead **open.**x rest**",
            "*AWF-VERDICT: FALSE POSITIVE: lead *open.*x rest*",
            "__AWF-VERDICT: FALSE POSITIVE: lead __open.__x rest__",
            # Underscore both-flanking before a non-alnum Unicode symbol cannot
            # close under CommonMark; consuming it leaves the outer wrap
            # unbalanced and must not resolve (PRRT_kwDOSJAM6s6bSs2f).
            "_AWF-VERDICT: FALSE POSITIVE: reason _open_🦄 rest_",
            "__AWF-VERDICT: FALSE POSITIVE: reason __open__🦄 rest__",
            # Escaped tick + later real tick must not swallow mid-reason steal
            # (PRRT_kwDOSJAM6s6bSsnj).
            r"**AWF-VERDICT: FALSE POSITIVE: \` **unclosed`x**",
            r"*AWF-VERDICT: FALSE POSITIVE: \` *unclosed`x*",
            r"__AWF-VERDICT: FALSE POSITIVE: \` __unclosed`x__",
            # Escaped ``\<`` is not an HTML token; attribute stars steal the
            # outer closer (PRRT_kwDOSJAM6s6bTLZk).
            r'**AWF-VERDICT: FALSE POSITIVE: see \<span title="**">x**',
            r"*AWF-VERDICT: FALSE POSITIVE: see \<em class='*'>x*",
            r'__AWF-VERDICT: FALSE POSITIVE: see \<span title="__">x__',
            # Incomplete URI/email autolinks are not opaque; interior markers
            # steal the outer closer (PRRT_kwDOSJAM6s6bTgB-). Underscore needs
            # flanking space so the mid run can open (intra-word ``a__b`` cannot).
            "**AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a**b**",
            "*AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a*b*",
            "__AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a __b__",
            "**AWF-VERDICT: FALSE POSITIVE: see <user**name@example.com**",
            # Incomplete link destination is not opaque; destination stars steal
            # the outer closer (PRRT_kwDOSJAM6s6bTLZq). Underscore incomplete
            # cases need flanking space so the mid run can open (intra-word
            # ``foo__bar`` cannot).
            "**AWF-VERDICT: FALSE POSITIVE: see [link](foo**bar**",
            "*AWF-VERDICT: FALSE POSITIVE: see [link](foo*bar*",
            "__AWF-VERDICT: FALSE POSITIVE: see [link](foo __bar__",
            # Whitespace makes a non-angle-bracket destination invalid; markers
            # stay emphasis and steal the outer closer (PRRT_kwDOSJAM6s6bTgB6).
            "**AWF-VERDICT: FALSE POSITIVE: see [link](foo **bar)**",
            "*AWF-VERDICT: FALSE POSITIVE: see [link](foo *bar)*",
            "__AWF-VERDICT: FALSE POSITIVE: see [link](foo __bar)__",
            # Backslash before ASCII space is not a CommonMark escape, so the
            # space still invalidates the destination and markers steal the
            # closer (PRRT_kwDOSJAM6s6bT50A).
            r"**AWF-VERDICT: FALSE POSITIVE: see [link](foo\ **bar)**",
            r"*AWF-VERDICT: FALSE POSITIVE: see [link](foo\ *bar)*",
            r"__AWF-VERDICT: FALSE POSITIVE: see [link](foo\ __bar)__",
            # Angle-bracket destination glued to a title (no required whitespace)
            # is not a CommonMark link; title markers steal the outer closer
            # (PRRT_kwDOSJAM6s6bTvK5).
            '**AWF-VERDICT: FALSE POSITIVE: see [link](<url>"**steal") rest**',
            "*AWF-VERDICT: FALSE POSITIVE: see [link](<url>'*steal') rest*",
            "**AWF-VERDICT: FALSE POSITIVE: see [link](<url>(**steal)) rest**",
            '__AWF-VERDICT: FALSE POSITIVE: see [link](<url>"__steal") rest__',
            # Unescaped ``(`` inside a parenthesized title is not CommonMark;
            # interior markers steal the outer closer (PRRT_kwDOSJAM6s6bUOZ9).
            "**AWF-VERDICT: FALSE POSITIVE: see [link](url (a(**bar)))**",
            "*AWF-VERDICT: FALSE POSITIVE: see [link](url (a(*bar)))*",
            "__AWF-VERDICT: FALSE POSITIVE: see [link](url (a(__bar)))__",
            # Whitespace before ``(`` selects the title parser; nested ``(`` is
            # still invalid (PRRT_kwDOSJAM6s6bUx1F).
            "**AWF-VERDICT: FALSE POSITIVE: see [x]( (a(**b)))**",
            "*AWF-VERDICT: FALSE POSITIVE: see [x]( (a(*b)))*",
            "__AWF-VERDICT: FALSE POSITIVE: see [x]( (a(__b)))__",
            # Unmatched label closer is not a link; destination stars steal the
            # outer closer (PRRT_kwDOSJAM6s6bTW7q).
            "**AWF-VERDICT: FALSE POSITIVE: see ](foo**bar)**",
            "*AWF-VERDICT: FALSE POSITIVE: see ](foo*bar)*",
            "__AWF-VERDICT: FALSE POSITIVE: see ](foo __bar)__",
            # Whitespace between ``]`` and ``(`` is not a CommonMark inline link;
            # parenthesized stars steal the outer closer (PRRT_kwDOSJAM6s6bTtr6).
            # Underscore needs a non-word-internal mid run (``foo__bar`` cannot open).
            "**AWF-VERDICT: FALSE POSITIVE: see [link] (foo**bar)**",
            "*AWF-VERDICT: FALSE POSITIVE: see [link] (foo*bar)*",
            "__AWF-VERDICT: FALSE POSITIVE: see [link] (__bar)__",
            # Nested links deactivate the outer label opener; destination stars
            # remain emphasis and steal the wrapper closer (PRRT_kwDOSJAM6s6bUCMq).
            # Underscore uses a non-space destination so opacity (not whitespace)
            # is what would incorrectly protect the closer without deactivation.
            "**AWF-VERDICT: FALSE POSITIVE: see [outer [inner](url)](foo**bar)**",
            "*AWF-VERDICT: FALSE POSITIVE: see [outer [inner](url)](foo*bar)*",
            "__AWF-VERDICT: FALSE POSITIVE: see [outer [inner](url)](__bar)__",
            # Link-label closers must not pair across the ``[`` boundary; the
            # mid-reason opener steals the trailing wrapper closer
            # (PRRT_kwDOSJAM6s6bUs3M).
            "**AWF-VERDICT: FALSE POSITIVE: reason **see [x**](url) rest**",
            "*AWF-VERDICT: FALSE POSITIVE: reason *see [x*](url) rest*",
            "__AWF-VERDICT: FALSE POSITIVE: reason __see [x__](url) rest__",
            # Malformed HTML comments (content contains ``--``) are not CommonMark
            # opaque tokens; interior stars close the line wrapper and leave the
            # trailing closer unmatched (PRRT_kwDOSJAM6s6bWHdN).
            "**AWF-VERDICT: FALSE POSITIVE: see <!--**--foo-->**",
            "*AWF-VERDICT: FALSE POSITIVE: see <!--*--foo-->*",
            "__AWF-VERDICT: FALSE POSITIVE: see <!--__--foo-->__",
        ],
    )
    def test_private_awf_verdict_invalid_emphasis_forms_still_fail_closed(
        self,
        final_line: str,
    ) -> None:
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{final_line}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_opposite_marker_openers_then_closers_stay_linear(self) -> None:
        # Each star closer walking the entire underscore stack is quadratic when
        # many unmatched ``_a`` precede many closing-only ``b**`` runs
        # (PRRT_kwDOSJAM6s6bW3pj).
        unders = " ".join(["_a"] * 8000)
        closers = "b**" * 8000
        reason = f"{unders}{closers}**"
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(reason, "**", seed_outer_opener=True)
            is False
        )
        trailing_only = f"{unders}**"
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                trailing_only, "**", seed_outer_opener=True
            )
            is True
        )

    @pytest.mark.unit
    def test_depleted_opener_tip_pop_skips_opposite_marker(self) -> None:
        # After a depleted same-marker opener is popped, stack_idx must resume
        # from the previous same-marker index, not the adjacent stack slot, or a
        # trailing ``*`` closer can wrongly consume a ``_`` opener
        # (PRRT_kwDOSJAM6s6bW97j).
        assert (
            _verdict_reason_trailing_emphasis_is_balanced("_x *b**", "**", seed_outer_opener=True)
            is False
        )

    @pytest.mark.unit
    def test_many_openers_then_unmatched_brackets_stay_linear(self) -> None:
        # Per-bracket list(open_stack) copies are quadratic when many unmatched
        # openers precede many unmatched ``[`` and can stall the monitor loop
        # before the 500-char reason bound (PRRT_kwDOSJAM6s6bU4CA).
        openers = " ".join(["*a"] * 8000)
        reason = f"{openers}{'[' * 8000}*"
        assert _verdict_reason_trailing_emphasis_is_balanced(reason, "*") is True

    @pytest.mark.unit
    def test_alternating_openers_and_unmatched_brackets_stay_linear(self) -> None:
        # Shared tuple freeze still full-copies when every ``*`` dirties the
        # stack before the next ``[``; alternating unmatched opener + label
        # opener stays quadratic before the 500-char reason bound
        # (PRRT_kwDOSJAM6s6bU8Th).
        reason = f"{'*a[' * 8000}*"
        assert _verdict_reason_trailing_emphasis_is_balanced(reason, "*") is True

    @pytest.mark.unit
    def test_many_openers_then_formed_links_stay_linear(self) -> None:
        # Restoring a formed link by walking/rebuilding the full opener-stack
        # snapshot is quadratic when many opening-only runs precede many valid
        # ``[x](u)`` links (PRRT_kwDOSJAM6s6bVQlP).
        openers = " ".join(["*a"] * 8000)
        links = "[x](u)" * 8000
        reason = f"{openers}{links}*"
        assert _verdict_reason_trailing_emphasis_is_balanced(reason, "*") is True

    @pytest.mark.unit
    def test_many_unmatched_brackets_then_formed_links_stay_linear(self) -> None:
        # Rescanning every outer label on each formed link is quadratic when
        # many unmatched ``[`` precede many valid ``[x](u)`` links
        # (PRRT_kwDOSJAM6s6bVZvi).
        openers = " ".join(["*a"] * 8000)
        reason = f"{openers}{'[' * 8000}{'[x](u)' * 8000}*"
        assert _verdict_reason_trailing_emphasis_is_balanced(reason, "*") is True

    @pytest.mark.unit
    def test_successively_longer_unmatched_backtick_runs_stay_linear(self) -> None:
        # Each unmatched opener rescanning the remaining suffix is quadratic
        # when successively longer backtick runs never close, and can stall
        # PR-monitor parse before the 500-char reason bound
        # (PRRT_kwDOSJAM6s6bWPe1).
        chunks: list[str] = ["*a"]
        for run_len in range(1, 400):
            chunks.append("`" * run_len)
            chunks.append("x" * 400)
        chunks.append("*")
        reason = "".join(chunks)
        assert _verdict_reason_trailing_emphasis_is_balanced(reason, "*") is True

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "line",
        [
            "**AWF-VERDICT: FALSE POSITIVE: ** rationale",
            "**AWF-VERDICT: FALSE POSITIVE: rationale **",
            r"**AWF-VERDICT: FALSE POSITIVE: rationale \**",
            r"**AWF-VERDICT: FALSE POSITIVE: rationale\**",
            "*AWF-VERDICT: FALSE POSITIVE: * rationale",
            r"*AWF-VERDICT: FALSE POSITIVE: rationale \*",
            "__AWF-VERDICT: FALSE POSITIVE: __ rationale",
            r"__AWF-VERDICT: FALSE POSITIVE: rationale \__",
            # Prefix closer plus unmatched same-delimiter closer later must not
            # resolve with leftover markers absorbed into the reason
            # (PRRT_kwDOSJAM6s6bQo0J).
            "**AWF-VERDICT: FALSE POSITIVE:** rationale**",
            "*AWF-VERDICT: FALSE POSITIVE:* rationale*",
            "__AWF-VERDICT: FALSE POSITIVE:__ rationale__",
            "___AWF-VERDICT: FALSE POSITIVE:___ rationale___",
            # Closing-only mid-reason run + trailing closer: even run count is not
            # a balanced span (PRRT_kwDOSJAM6s6bRfTo).
            "**AWF-VERDICT: FALSE POSITIVE:** rationale** more**",
            "*AWF-VERDICT: FALSE POSITIVE:* rationale* more*",
            "__AWF-VERDICT: FALSE POSITIVE:__ rationale__ more__",
            # Alphanumeric-to-punctuation mid run must not open (PRRT_kwDOSJAM6s6bSOmb).
            "**AWF-VERDICT: FALSE POSITIVE:** rationale a**. more**",
            "*AWF-VERDICT: FALSE POSITIVE:* rationale a*. more*",
            "__AWF-VERDICT: FALSE POSITIVE:__ rationale a__. more__",
            # ASCII punctuation outside Unicode P* (PRRT_kwDOSJAM6s6bSZP4).
            "**AWF-VERDICT: FALSE POSITIVE:** rationale a**$ more**",
            "*AWF-VERDICT: FALSE POSITIVE:* rationale a*$ more*",
            "__AWF-VERDICT: FALSE POSITIVE:__ rationale a__$ more__",
            "**AWF-VERDICT: FALSE POSITIVE:** rationale a**+ more**",
            "**AWF-VERDICT: FALSE POSITIVE:** rationale a**^ more**",
            # Closing-only mid run would close the line-leading wrapper; an empty
            # balance stack must not ignore it and accept the trailing delimiter
            # as a whole-line closer (PRRT_kwDOSJAM6s6bUx1A).
            "**AWF-VERDICT: FALSE POSITIVE: rationale** more**",
            "*AWF-VERDICT: FALSE POSITIVE: rationale* more*",
            "__AWF-VERDICT: FALSE POSITIVE: rationale__ more__",
            # Mid-reason same-delimiter opener steals the trailing closer; the
            # line-leading wrapper stays unbalanced (PRRT_kwDOSJAM6s6bRrWv).
            "**AWF-VERDICT: FALSE POSITIVE: rationale **unclosed**",
            "*AWF-VERDICT: FALSE POSITIVE: rationale *unclosed*",
            "__AWF-VERDICT: FALSE POSITIVE: rationale __unclosed__",
            "_AWF-VERDICT: FALSE POSITIVE: rationale _unclosed_",
            # Rule 9 blocks the nearest both-flanking length-1 opener against
            # trailing length-2, but CommonMark continues to the earlier
            # length-2 opener — trailing is stolen (PRRT_kwDOSJAM6s6bTtr5).
            "**AWF-VERDICT: FALSE POSITIVE: reason **lower a*b**",
            "__AWF-VERDICT: FALSE POSITIVE: reason __lower ._.x__",
            # Longer mid-run partially pairs with a short closer then the
            # trailing wrapper-length closer (PRRT_kwDOSJAM6s6bR2FM). A
            # both-flanking ``**`` blocked by rule 9 against ``*`` does not
            # steal (PRRT_kwDOSJAM6s6bTW7t).
            "**AWF-VERDICT: FALSE POSITIVE: ***lead* rest**",
            "__AWF-VERDICT: FALSE POSITIVE: ___lead_ rest__",
            # Shorter mid opener + trailing wrapper at EOS: space-preceded mid
            # run cannot close, so rule 9 does not block and the mid run steals
            # the closer (PRRT_kwDOSJAM6s6bTBv4).
            "**AWF-VERDICT: FALSE POSITIVE: reason *x**",
            "*AWF-VERDICT: FALSE POSITIVE: reason *x*",
            "__AWF-VERDICT: FALSE POSITIVE: reason _x__",
            # Reason-leading complementary opener at BOS is opening-only
            # (BOS counts as whitespace); rule 9 must not falsely block, so the
            # mid run steals the trailing wrapper closer (PRRT_kwDOSJAM6s6bTi4S).
            "**AWF-VERDICT: FALSE POSITIVE: *foo**",
            "*AWF-VERDICT: FALSE POSITIVE: **lead* rest*",
            "__AWF-VERDICT: FALSE POSITIVE: _foo__",
            "_AWF-VERDICT: FALSE POSITIVE: __lead_ rest_",
            # Punctuation-to-alphanumeric mid run is opening-only; treating it as
            # a closer would consume an earlier opener and wrongly accept the
            # whole-line wrap (PRRT_kwDOSJAM6s6bShqh).
            "**AWF-VERDICT: FALSE POSITIVE: lead **open.**x rest**",
            "*AWF-VERDICT: FALSE POSITIVE: lead *open.*x rest*",
            "__AWF-VERDICT: FALSE POSITIVE: lead __open.__x rest__",
            "**AWF-VERDICT: FALSE POSITIVE: lead **open$**x rest**",
            "**AWF-VERDICT: FALSE POSITIVE: lead **open+**x rest**",
            "**AWF-VERDICT: FALSE POSITIVE: lead **open^**x rest**",
            # Underscore both-flanking before a Unicode symbol cannot close
            # (PRRT_kwDOSJAM6s6bSs2f).
            "_AWF-VERDICT: FALSE POSITIVE: reason _open_🦄 rest_",
            "__AWF-VERDICT: FALSE POSITIVE: reason __open__🦄 rest__",
            # Escaped backtick is not a code-span opener; a later real tick must
            # not hide mid-reason stealers of the whole-line closer
            # (PRRT_kwDOSJAM6s6bSsnj).
            r"**AWF-VERDICT: FALSE POSITIVE: \` **unclosed`x**",
            r"*AWF-VERDICT: FALSE POSITIVE: \` *unclosed`x*",
            r"__AWF-VERDICT: FALSE POSITIVE: \` __unclosed`x__",
            # CommonMark: escapes do not work in code spans, so an escaped tick
            # still closes. Precompute must not drop it or a later unescaped
            # run extends the opaque span over mid-reason stealers
            # (PRRT_kwDOSJAM6s6bWSff).
            r"**AWF-VERDICT: FALSE POSITIVE: see `foo\` and **unclosed`x**",
            r"*AWF-VERDICT: FALSE POSITIVE: see `foo\` and *unclosed`x*",
            r"__AWF-VERDICT: FALSE POSITIVE: see `foo\` and __unclosed`x__",
            # Escaped ``\<`` is not HTML; attribute markers steal the closer
            # (PRRT_kwDOSJAM6s6bTLZk).
            r'**AWF-VERDICT: FALSE POSITIVE: see \<span title="**">x**',
            r"*AWF-VERDICT: FALSE POSITIVE: see \<em class='*'>x*",
            r'__AWF-VERDICT: FALSE POSITIVE: see \<span title="__">x__',
            # Incomplete link destination leaves destination markers as emphasis
            # that steal the trailing closer (PRRT_kwDOSJAM6s6bTLZq).
            "**AWF-VERDICT: FALSE POSITIVE: see [link](foo**bar**",
            "*AWF-VERDICT: FALSE POSITIVE: see [link](foo*bar*",
            "__AWF-VERDICT: FALSE POSITIVE: see [link](foo __bar__",
            # Invalid destination whitespace: markers steal the closer
            # (PRRT_kwDOSJAM6s6bTgB6).
            "**AWF-VERDICT: FALSE POSITIVE: see [link](foo **bar)**",
            "*AWF-VERDICT: FALSE POSITIVE: see [link](foo *bar)*",
            "__AWF-VERDICT: FALSE POSITIVE: see [link](foo __bar)__",
            # ``\ `` is not escapable punctuation; space still invalidates the
            # destination (PRRT_kwDOSJAM6s6bT50A).
            r"**AWF-VERDICT: FALSE POSITIVE: see [link](foo\ **bar)**",
            r"*AWF-VERDICT: FALSE POSITIVE: see [link](foo\ *bar)*",
            r"__AWF-VERDICT: FALSE POSITIVE: see [link](foo\ __bar)__",
            # Unmatched ``]`` is not a label closer; parenthesized stars steal
            # the whole-line closer (PRRT_kwDOSJAM6s6bTW7q).
            "**AWF-VERDICT: FALSE POSITIVE: see ](foo**bar)**",
            "*AWF-VERDICT: FALSE POSITIVE: see ](foo*bar)*",
            "__AWF-VERDICT: FALSE POSITIVE: see ](foo __bar)__",
            # Whitespace between ``]`` and ``(`` is not an inline link; stars
            # steal the closer (PRRT_kwDOSJAM6s6bTtr6). Underscore needs a
            # non-word-internal mid run.
            "**AWF-VERDICT: FALSE POSITIVE: see [link] (foo**bar)**",
            "*AWF-VERDICT: FALSE POSITIVE: see [link] (foo*bar)*",
            "__AWF-VERDICT: FALSE POSITIVE: see [link] (__bar)__",
            # Undefined full reference labels are not links; stars in the ref id
            # remain emphasis and steal the whole-line closer
            # (PRRT_kwDOSJAM6s6bUCMm).
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**",
            "*AWF-VERDICT: FIXED: see [details][issue*ref]*",
            # Undefined shortcut reference likewise leaves label stars as
            # emphasis that steal the closer (PRRT_kwDOSJAM6s6bVBWW).
            "**AWF-VERDICT: FALSE POSITIVE: see [issue**ref]**",
            "*AWF-VERDICT: FIXED: see [issue*ref]*",
            # Formed-link labels isolate emphasis: a closer inside the label
            # cannot close an opener before ``[``, so the trailing run pairs
            # with that opener and rejects the whole-line wrap
            # (PRRT_kwDOSJAM6s6bUs3M).
            "**AWF-VERDICT: FALSE POSITIVE: reason **see [x**](url) rest**",
            "*AWF-VERDICT: FALSE POSITIVE: reason *see [x*](url) rest*",
            "__AWF-VERDICT: FALSE POSITIVE: reason __see [x__](url) rest__",
            "**AWF-VERDICT: FALSE POSITIVE: reason **see ![x**](url) rest**",
        ],
    )
    def test_private_markdown_emphasis_normalizer_rejects_invalid_closers(
        self,
        line: str,
    ) -> None:
        # Standalone: invalid closers must not normalize into a resolvable verdict.
        assert _normalize_markdown_emphasized_verdict_line(line) is None
        result = _parse_verdict_result(line)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stdout", "expected_verdict", "expected_reason"),
        [
            # Document-level reference definitions after the verdict line must
            # resolve full-ref labels so whole-line emphasis strips cleanly
            # (PRRT_kwDOSJAM6s6bU8Tf). Line-only normalization cannot see them.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "[issue**ref]: /issue\n",
                "false_positive",
                "see [details][issue**ref]",
            ),
            (
                "*AWF-VERDICT: FIXED: see [details][issue*ref]*\n\n[issue*ref]: /issue\n",
                "fix_committed",
                "see [details][issue*ref]",
            ),
            (
                "`**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**`\n\n"
                "[issue**ref]: /issue\n",
                "false_positive",
                "see [details][issue**ref]",
            ),
            (
                "``*AWF-VERDICT: FIXED: see [details][issue*ref]*``\n\n[issue*ref]: /issue\n",
                "fix_committed",
                "see [details][issue*ref]",
            ),
            # Shortcut reference ``[label]`` (no ``](`` / ``][``) with a matching
            # document definition must isolate label emphasis so the wrapper
            # closer is not stolen (PRRT_kwDOSJAM6s6bVBWW).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [issue**ref]**\n\n[issue**ref]: /issue\n",
                "false_positive",
                "see [issue**ref]",
            ),
            (
                "*AWF-VERDICT: FIXED: see [issue*ref]*\n\n[issue*ref]: /issue\n",
                "fix_committed",
                "see [issue*ref]",
            ),
            (
                "`**AWF-VERDICT: FALSE POSITIVE: see [issue**ref]**`\n\n[issue**ref]: /issue\n",
                "false_positive",
                "see [issue**ref]",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see ![issue**ref]**\n\n[issue**ref]: /issue\n",
                "false_positive",
                "see ![issue**ref]",
            ),
            # Failed adjacent inline ``](bad dest)`` falls back to shortcut when
            # the label resolves (CommonMark ex. 568; PRRT_kwDOSJAM6s6bWBAo).
            # ``(bad dest)`` stays literal; label stars must not steal the closer.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [issue**ref](bad dest)**\n\n"
                "[issue**ref]: /url\n",
                "false_positive",
                "see [issue**ref](bad dest)",
            ),
            (
                "*AWF-VERDICT: FIXED: see [issue*ref](bad dest)*\n\n[issue*ref]: /url\n",
                "fix_committed",
                "see [issue*ref](bad dest)",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see ![issue**ref](bad dest)**\n\n"
                "[issue**ref]: /url\n",
                "false_positive",
                "see ![issue**ref](bad dest)",
            ),
        ],
    )
    def test_parse_verdict_resolves_document_level_reference_definitions(
        self,
        stdout: str,
        expected_verdict: str,
        expected_reason: str,
    ) -> None:
        result = _parse_verdict_result(stdout)
        assert result.verdict == expected_verdict
        assert result.reason == expected_reason

    @pytest.mark.unit
    def test_normalize_emphasized_verdict_uses_extra_reference_definitions(
        self,
    ) -> None:
        line = "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**"
        assert _normalize_markdown_emphasized_verdict_line(line) is None
        assert (
            _normalize_markdown_emphasized_verdict_line(
                line,
                extra_reference_definitions=frozenset({"issue**ref"}),
            )
            == "AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]"
        )
        # Shortcut form likewise needs the extra definition set
        # (PRRT_kwDOSJAM6s6bVBWW).
        shortcut = "**AWF-VERDICT: FALSE POSITIVE: see [issue**ref]**"
        assert _normalize_markdown_emphasized_verdict_line(shortcut) is None
        assert (
            _normalize_markdown_emphasized_verdict_line(
                shortcut,
                extra_reference_definitions=frozenset({"issue**ref"}),
            )
            == "AWF-VERDICT: FALSE POSITIVE: see [issue**ref]"
        )
        # Adjacent (non-blank) follow-on lines are not document definitions, so
        # parse still fails closed without a blank-line block boundary.
        adjacent = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n[issue**ref]: /issue\n"
        )
        adjacent_result = _parse_verdict_result(adjacent)
        assert adjacent_result.verdict == "needs_human"
        assert adjacent_result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_parse_verdict_resolves_reference_definition_after_lone_carriage_return(
        self,
    ) -> None:
        # Progress redraws split stdout on lone ``\r``; reference-definition
        # scanning must use the same line iterator as verdict selection
        # (PRRT_kwDOSJAM6s6bWzca).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\r\r[issue**ref]: /issue\r"
        )
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            # Fenced example after a blank: CommonMark does not activate the
            # interior definition, so the malformed wrapper must stay garbled
            # (PRRT_kwDOSJAM6s6bVBWU).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "```\n\n[issue**ref]: /issue\n```\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "~~~\n\n[issue**ref]: /issue\n~~~\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "<pre>\n\n[issue**ref]: /issue\n</pre>\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "<!--\n\n[issue**ref]: /issue\n-->\n"
            ),
        ],
    )
    def test_parse_verdict_ignores_reference_definitions_in_inactive_regions(
        self,
        stdout: str,
    ) -> None:
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout,expected_verdict,expected_reason",
        [
            # Closed fence after nonblank prose is a block boundary: the
            # definition needs no extra blank line (PRRT_kwDOSJAM6s6bVMBG).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "```\n"
                "code\n"
                "```\n"
                "[issue**ref]: /issue\n",
                "false_positive",
                "see [details][issue**ref]",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "~~~\n"
                "code\n"
                "~~~\n"
                "[issue**ref]: /issue\n",
                "false_positive",
                "see [details][issue**ref]",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "<pre>\n"
                "code\n"
                "</pre>\n"
                "[issue**ref]: /issue\n",
                "false_positive",
                "see [details][issue**ref]",
            ),
            # Unclosed fence still shields the trailing definition.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "```\n"
                "code\n"
                "[issue**ref]: /issue\n",
                "needs_human",
                "garbled_verdict_marker",
            ),
        ],
    )
    def test_parse_verdict_resolves_reference_definition_after_closed_shield(
        self,
        stdout: str,
        expected_verdict: str,
        expected_reason: str,
    ) -> None:
        result = _parse_verdict_result(stdout)
        assert result.verdict == expected_verdict
        assert result.reason == expected_reason
        if expected_verdict == "false_positive":
            spans = _markdown_reference_definition_spans(stdout)
            assert [label for _, _, label in spans] == ["issue**ref"]
        else:
            assert _markdown_reference_definition_spans(stdout) == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            # Mid-paragraph indented continuation is shielded for verdict
            # selection but is not a CommonMark block boundary; a following
            # ``[label]: dest`` must not resolve (PRRT_kwDOSJAM6s6bVP6L).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "    indented continuation\n"
                "[issue**ref]: /issue\n"
            ),
            # Non-interrupting type-7 HTML (complete tag alone on the line) is
            # likewise not a block boundary when it cannot start mid-paragraph.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "<span>\n"
                "[issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "</span>\n"
                "[issue**ref]: /issue\n"
            ),
        ],
    )
    def test_parse_verdict_rejects_reference_definition_after_soft_shield(
        self,
        stdout: str,
    ) -> None:
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_parse_verdict_rejects_reference_definition_after_lowercase_declaration_lookalike(
        self,
    ) -> None:
        # CommonMark type-4 declarations require an uppercase ASCII letter after
        # ``<!``; lowercase lookalikes such as ``<!foo>`` are not HTML blocks and
        # must not establish a hard-shield exit boundary that activates a trailing
        # reference definition (PRRT_kwDOSJAM6s6bW3ph).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
            "<!foo>\n"
            "[issue**ref]: /issue\n"
        )
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            # ATX heading is a leaf block: definition needs no blank line
            # (PRRT_kwDOSJAM6s6bVZvh).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "# context\n"
                "[issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "### details\n"
                "[issue**ref]: /issue\n"
            ),
            # Thematic breaks likewise end at the newline.
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "---\n"
                "[issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "* * *\n"
                "[issue**ref]: /issue\n"
            ),
            # Setext underline completes a leaf heading; definition needs no
            # blank line (PRRT_kwDOSJAM6s6bVkD0).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "Heading\n"
                "===\n"
                "[issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "===\n"
                "[issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "Heading\n"
                "--\n"
                "[issue**ref]: /issue\n"
            ),
            # Nested containers: peel before leaf-boundary detection so a
            # same-container definition after ATX / thematic / Setext still
            # resolves document-wide (PRRT_kwDOSJAM6s6bWLeD).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> # context\n"
                "> [issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> ---\n"
                "> [issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> Heading\n"
                "> ===\n"
                "> [issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "- # context\n"
                "  [issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
                "> - # context\n"
                ">   [issue**ref]: /issue\n"
            ),
        ],
    )
    def test_parse_verdict_resolves_reference_definition_after_leaf_block(
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
            # Outer paragraph must not make peeled ``> ===`` a Setext leaf;
            # otherwise the following definition resolves and hides the
            # emphasis stealer (PRRT_kwDOSJAM6s6bWOTK).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "para\n"
                "> ===\n"
                "> [issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "para\n"
                "- ===\n"
                "  [issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "> para\n"
                "> > ===\n"
                "> > [issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "- Heading\n"
                "- ===\n"
                "  [issue**ref]: /issue\n"
            ),
        ],
    )
    def test_parse_verdict_rejects_setext_after_container_entry_from_paragraph(
        self,
        stdout: str,
    ) -> None:
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_parse_verdict_resolves_reference_definition_after_indented_code_block(
        self,
    ) -> None:
        # Real indented code at a block boundary (blank before) still ends the
        # block so a following definition may resolve without an extra blank.
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            "    code\n"
            "[issue**ref]: /issue\n"
        )
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "stdout",
        [
            # Hard-shield exit into soft-shielded indented code must still
            # record a block boundary; soft exit then preserves it so the
            # following definition resolves (PRRT_kwDOSJAM6s6bVaBX).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "```\n"
                "code\n"
                "```\n"
                "    indented\n"
                "[issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "~~~\n"
                "code\n"
                "~~~\n"
                "    indented\n"
                "[issue**ref]: /issue\n"
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n"
                "<pre>\n"
                "code\n"
                "</pre>\n"
                "    indented\n"
                "[issue**ref]: /issue\n"
            ),
        ],
    )
    def test_parse_verdict_resolves_reference_after_hard_then_soft_shield(
        self,
        stdout: str,
    ) -> None:
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"

    @pytest.mark.unit
    @pytest.mark.unit
    @pytest.mark.parametrize(
        "definition_block",
        [
            "> [issue**ref]: /url\n",
            "- [issue**ref]: /url\n",
            "1. [issue**ref]: /url\n",
            "> - [issue**ref]: /url\n",
            "> [issue**ref]:\n>   /url\n",
            "- [issue**ref]:\n  /url\n",
            # List item wrapping a blockquote LRD: peel the list first, then the
            # continued ``>`` on both lines (PRRT_kwDOSJAM6s6bVqW2).
            "- > [issue**ref]:\n  > /url\n",
            "* > [issue**ref]:\n  > /url\n",
            "1. > [issue**ref]:\n   > /url\n",
            "> - > [issue**ref]:\n>   > /url\n",
        ],
    )
    def test_parse_verdict_resolves_reference_definitions_in_block_containers(
        self,
        definition_block: str,
    ) -> None:
        # CommonMark link reference definitions inside blockquotes / list items
        # remain document-wide. Matching must peel container prefixes before the
        # 0–3 space indent rule or a valid emphasized full-ref verdict is
        # escalated as garbled_verdict_marker (PRRT_kwDOSJAM6s6bVfyC).
        stdout = f"**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n{definition_block}"
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"
        opener = definition_block.splitlines()[0]
        multiline = "\n" in definition_block.rstrip("\n")
        if multiline:
            assert _match_markdown_reference_definition_line(opener) is None
            assert _markdown_reference_definition_awaits_destination(opener) is True
        else:
            assert _match_markdown_reference_definition_line(opener) == "issue**ref"
            assert _markdown_reference_definition_awaits_destination(opener) is False

    @pytest.mark.unit
    def test_parse_verdict_resolves_reference_definitions_with_mixed_space_tab_list_padding(
        self,
    ) -> None:
        # CommonMark expands ``- \\t`` to three columns of list padding; peeling
        # only the first whitespace leaves a tab that indented-code shielding
        # hides the LRD (PRRT_kwDOSJAM6s6bXMLg).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n- \t[issue**ref]: /url\n"
        )
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"

    @pytest.mark.unit
    def test_parse_verdict_rejects_reference_definition_with_indented_list_tab_padding(
        self,
    ) -> None:
        # Two leading spaces shift the tab stop so ``  - \\t`` padding reaches
        # five columns; the line must soft-shield as indented code so link
        # stars stay emphasis instead of fail-opening via an LRD
        # (PRRT_kwDOSJAM6s6bXR5z, PRRT_kwDOSJAM6s6bXSNz).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            "  - \t[issue**ref]: /url\n"
        )
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_parse_verdict_rejects_reference_definition_with_nested_list_tab_padding(
        self,
    ) -> None:
        # Nested list markers must carry cumulative document columns when peeling
        # padding; ``- - \\t`` reaches five columns so the tab-indented opener
        # is indented code, not an LRD (PRRT_kwDOSJAM6s6bXcEC).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            "- - \t[issue**ref]: /url\n"
        )
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_parse_verdict_rejects_reference_definition_with_nested_list_mixed_tab_padding(
        self,
    ) -> None:
        # When the outer list peel includes a tab, the returned column offset must
        # use expanded document columns so the inner peel sees five-column padding
        # (PRRT_kwDOSJAM6s6bXnJR).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            "- \t-   \t[issue**ref]: /url\n"
        )
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_parse_verdict_resolves_reference_definition_destination_on_next_line(
        self,
    ) -> None:
        # CommonMark §4.7: optional spaces/tabs may include one line ending
        # between colon and destination. Line-at-a-time matching must consume
        # that continuation or a valid full-line emphasized verdict is rejected
        # as garbled_verdict_marker (PRRT_kwDOSJAM6s6bVQlQ).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n[issue**ref]:\n  /url\n"
        )
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        assert stdout[spans[0][0] : spans[0][1]] == "[issue**ref]:\n  /url\n"
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"
        # Blank line between colon and destination is not permitted.
        blank_gap = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            "[issue**ref]:\n"
            "\n"
            "  /url\n"
        )
        assert _markdown_reference_definition_spans(blank_gap) == []
        blank_result = _parse_verdict_result(blank_gap)
        assert blank_result.verdict == "needs_human"
        assert blank_result.reason == "garbled_verdict_marker"
        # Colon-only with no continuation remains a non-definition.
        assert _match_markdown_reference_definition_line("[issue**ref]:") is None
        assert _markdown_reference_definition_spans("[issue**ref]:\n") == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "definition_block",
        [
            '[issue**ref]: /url "multi\nline"\n',
            "[issue**ref]: /url 'multi\nline'\n",
            "[issue**ref]: /url (multi\nline)\n",
            # CommonMark §4.7 example: title may span several lines.
            "[issue**ref]: /url '\ntitle\nline1\nline2\n'\n",
            # Destination on the line after colon, then a multiline title.
            '[issue**ref]:\n  /url "multi\nline"\n',
            # Blockquote-nested multiline title (continued ``>``).
            '> [issue**ref]: /url "multi\n> line"\n',
        ],
    )
    def test_parse_verdict_resolves_reference_definition_multiline_title(
        self,
        definition_block: str,
    ) -> None:
        # CommonMark §4.7: an opened title may continue onto following lines
        # (no blank line). Line-at-a-time matching must consume that
        # continuation or ``[details][issue**ref]`` stays unresolved and stars
        # in the label escalate a valid emphasized verdict as
        # garbled_verdict_marker (PRRT_kwDOSJAM6s6bVrCq).
        stdout = f"**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n{definition_block}"
        spans = _markdown_reference_definition_spans(stdout)
        assert [label for _, _, label in spans] == ["issue**ref"]
        result = _parse_verdict_result(stdout)
        assert result.verdict == "false_positive"
        assert result.reason == "see [details][issue**ref]"
        opener = definition_block.splitlines()[0]
        if _match_markdown_reference_definition_line(opener) is None:
            assert _markdown_reference_definition_awaits_title(
                opener
            ) or _markdown_reference_definition_awaits_destination(opener)
        # Blank line inside a title is not permitted.
        blank_in_title = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            '[issue**ref]: /url "multi\n'
            "\n"
            'line"\n'
        )
        assert _markdown_reference_definition_spans(blank_in_title) == []
        blank_result = _parse_verdict_result(blank_in_title)
        assert blank_result.verdict == "needs_human"
        assert blank_result.reason == "garbled_verdict_marker"
        assert _markdown_reference_definition_awaits_title('[foo]: /url "open') is True
        assert _markdown_reference_definition_awaits_title('[foo]: /url "closed"') is False
        assert _markdown_reference_definition_awaits_title("[foo]: /url") is False
        assert _markdown_reference_definition_awaits_title("[foo]:") is False

    @pytest.mark.unit
    def test_parse_verdict_bounds_multiline_reference_title_continuation(self) -> None:
        # Unbounded rebuild+reparse of an opened title is quadratic in
        # continuation size and can stall every PR-monitor verdict parse on
        # crafted agent stdout (PRRT_kwDOSJAM6s6bWCnP). Bound continuation
        # lines / accumulated length; titles that only close past the bound
        # fail closed (no definition).
        from awf.runtime.pr_monitor_runner.helpers_verdict_reference import (
            _MAX_MARKDOWN_REFERENCE_TITLE_ACCUMULATED_CHARS,
            _MAX_MARKDOWN_REFERENCE_TITLE_CONTINUATION_LINES,
        )

        # Closer is one continuation; leave MAX-1 body lines so the total
        # stays within the line bound and still resolves.
        within_body = "\n".join(["x"] * (_MAX_MARKDOWN_REFERENCE_TITLE_CONTINUATION_LINES - 1))
        within = f'[issue**ref]: /url "\n{within_body}\n"\n'
        assert [label for _, _, label in _markdown_reference_definition_spans(within)] == [
            "issue**ref"
        ]

        over_body = "\n".join(["x"] * _MAX_MARKDOWN_REFERENCE_TITLE_CONTINUATION_LINES)
        over_lines = f'[issue**ref]: /url "\n{over_body}\n"\n'
        assert _markdown_reference_definition_spans(over_lines) == []

        long_cont = "x" * (_MAX_MARKDOWN_REFERENCE_TITLE_ACCUMULATED_CHARS + 1)
        over_chars = f'[issue**ref]: /url "\n{long_cont}\n"\n'
        assert _markdown_reference_definition_spans(over_chars) == []

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "definition_block",
        [
            # ATX heading starts a new leaf; do not fold into the open title.
            '[issue**ref]: /url "multi\n# heading"\n',
            '[issue**ref]: /url "multi\n### title"\n',
            # Thematic breaks likewise end the unfinished title.
            '[issue**ref]: /url "multi\n---\n"\n',
            '[issue**ref]: /url "multi\n___\n"\n',
            # Nested blockquote: peel then treat ATX as a leaf interrupt.
            '> [issue**ref]: /url "multi\n> # heading"\n',
            # Destination continuation interrupted by an ATX leaf.
            "[issue**ref]:\n# /url\n",
        ],
    )
    def test_parse_verdict_rejects_leaf_interrupt_on_reference_title_continuation(
        self,
        definition_block: str,
    ) -> None:
        # CommonMark ends an unfinished link title / destination when an
        # ordinary leaf block (ATX heading, thematic break) starts. Folding
        # that leaf into the title can complete a definition and accept an
        # emphasized FALSE POSITIVE that should fail closed
        # (PRRT_kwDOSJAM6s6bVyKH).
        stdout = f"**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n{definition_block}"
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"
        assert _markdown_line_is_leaf_block_boundary("# heading") is True
        assert _markdown_line_is_leaf_block_boundary("---") is True

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "continuation",
        [
            "```\ncode\n```\n",
            "~~~\ncode\n~~~\n",
            "<pre>\ncode\n</pre>\n",
        ],
    )
    def test_parse_verdict_rejects_hard_shielded_reference_destination_continuation(
        self,
        continuation: str,
    ) -> None:
        # CommonMark starts a fenced/HTML hard shield on the next line rather
        # than supplying a missing reference destination. Combining the opener
        # into ``[label]: ``` `` would register a definition and accept a
        # malformed emphasized FALSE POSITIVE (PRRT_kwDOSJAM6s6bVfyB).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            f"[issue**ref]:\n{continuation}"
        )
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "definition_block",
        [
            "[issue**ref]:\n> /url\n",
            "[issue**ref]:\n- /url\n",
            "[issue**ref]:\n* /url\n",
            "[issue**ref]:\n1. /url\n",
            "> [issue**ref]:\n>> /url\n",
            "> [issue**ref]:\n> - /url\n",
            # Blockquote opener requires a continued ``>``; bare destination
            # lines leave the incomplete definition (do not lazy-peel).
            "> [issue**ref]:\n  /url\n",
            # List opener + new blockquote / new list item on the continuation.
            "- [issue**ref]:\n> /url\n",
            "- [issue**ref]:\n- /url\n",
        ],
    )
    def test_parse_verdict_rejects_new_container_on_reference_destination_continuation(
        self,
        definition_block: str,
    ) -> None:
        # A new blockquote/list on the continuation ends an incomplete
        # ``[label]:`` opener. Peeling that marker into the destination would
        # register the definition and accept a malformed emphasized FALSE
        # POSITIVE (PRRT_kwDOSJAM6s6bVjt_).
        stdout = f"**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n{definition_block}"
        assert _markdown_reference_definition_spans(stdout) == []
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_parse_verdict_rejects_unbalanced_reference_definition_destination(
        self,
    ) -> None:
        # CommonMark does not form a reference definition when the non-angle
        # destination has unbalanced unescaped parentheses. Accepting it via
        # ``\S+`` would make ``[details][issue**ref]`` opaque and wrongly
        # normalize the emphasized wrapper to false_positive
        # (PRRT_kwDOSJAM6s6bVBWV).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n[issue**ref]: foo(bar\n"
        )
        assert _markdown_reference_definition_spans(stdout) == []
        assert _match_markdown_reference_definition_line("[issue**ref]: foo(bar") is None
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"
        # Balanced and escaped destinations remain valid definitions.
        assert _match_markdown_reference_definition_line("[issue**ref]: foo(bar)") == "issue**ref"
        assert _match_markdown_reference_definition_line(r"[issue**ref]: foo\(bar") == "issue**ref"
        balanced = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n[issue**ref]: foo(bar)\n"
        )
        balanced_result = _parse_verdict_result(balanced)
        assert balanced_result.verdict == "false_positive"
        assert balanced_result.reason == "see [details][issue**ref]"

    @pytest.mark.unit
    def test_parse_verdict_rejects_escaped_closer_in_angle_reference_destination(
        self,
    ) -> None:
        # CommonMark: a backslash-escaped ``>`` does not close an angle-bracket
        # destination, so ``[issue**ref]: <foo\>`` is not a reference
        # definition. Treating the escaped ``>`` as a closer would register the
        # label and make ``[details][issue**ref]`` opaque, wrongly normalizing
        # the emphasized wrapper to false_positive (PRRT_kwDOSJAM6s6bV80o).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            r"[issue**ref]: <foo\>"
            "\n"
        )
        assert _markdown_reference_definition_spans(stdout) == []
        assert _match_markdown_reference_definition_line(r"[issue**ref]: <foo\>") is None
        assert _markdown_reference_definition_awaits_title(r"[issue**ref]: <foo\>") is False
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"
        # Escaped ``<`` inside the destination is allowed; unescaped ``>`` closes.
        assert (
            _match_markdown_reference_definition_line(r"[issue**ref]: <foo\<bar>") == "issue**ref"
        )
        assert _match_markdown_reference_definition_line(r"[issue**ref]: <foo\<bar\>") is None
        valid = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            r"[issue**ref]: <foo\<bar>"
            "\n"
        )
        valid_result = _parse_verdict_result(valid)
        assert valid_result.verdict == "false_positive"
        assert valid_result.reason == "see [details][issue**ref]"
