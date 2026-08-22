"""Unit tests for verdict parsing helpers (part 026).

Emphasis-balance, closer-validity, and link-reference coverage split from
part 021 to stay under the first-party file line limit.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _parse_verdict_result,
)
from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _advance_past_markdown_link_reference_label,
    _emphasis_run_pair_blocked_by_multiple_of_three,
    _markdown_emphasis_run_can_close,
    _markdown_emphasis_run_can_open,
    _markdown_normalize_link_reference_label,
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
    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (
                "**AWF-VERDICT: FALSE POSITIVE:** rationale",
                "AWF-VERDICT: FALSE POSITIVE: rationale",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: rationale**",
                "AWF-VERDICT: FALSE POSITIVE: rationale",
            ),
            # Literal backslash then a real closer remains valid.
            (
                r"**AWF-VERDICT: FALSE POSITIVE: rationale\\**",
                r"AWF-VERDICT: FALSE POSITIVE: rationale\\",
            ),
            # Escaped same-marker before the closer is content, not a longer run.
            (
                r"**AWF-VERDICT: FALSE POSITIVE: rationale\***",
                r"AWF-VERDICT: FALSE POSITIVE: rationale\*",
            ),
            # Prefix-only wrap plus a separately balanced reason span ending at
            # EOF is valid Markdown, not a dual whole-line closer
            # (PRRT_kwDOSJAM6s6bRROQ).
            (
                "**AWF-VERDICT: FALSE POSITIVE:** This is **expected**",
                "AWF-VERDICT: FALSE POSITIVE: This is **expected**",
            ),
            (
                "*AWF-VERDICT: FIXED:* committed with *emphasis*",
                "AWF-VERDICT: FIXED: committed with *emphasis*",
            ),
            (
                "__AWF-VERDICT: DEFER:__ track __later__",
                "AWF-VERDICT: DEFER: track __later__",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE:** rationale with **bold** and **more**",
                "AWF-VERDICT: FALSE POSITIVE: rationale with **bold** and **more**",
            ),
            (
                r"**AWF-VERDICT: FALSE POSITIVE:** see \**literal and **ok**",
                r"AWF-VERDICT: FALSE POSITIVE: see \**literal and **ok**",
            ),
            # Word-internal `_` in NEEDS_HUMAN / snake_case must not steal the
            # trailing whole-line closer (PRRT_kwDOSJAM6s6bRy5w).
            (
                "_AWF-VERDICT: NEEDS_HUMAN: please clarify_",
                "AWF-VERDICT: NEEDS_HUMAN: please clarify",
            ),
            (
                "_AWF-VERDICT: FALSE POSITIVE: already_correct_",
                "AWF-VERDICT: FALSE POSITIVE: already_correct",
            ),
            (
                "_AWF-VERDICT: FIXED: see_this_",
                "AWF-VERDICT: FIXED: see_this",
            ),
            (
                "_AWF-VERDICT: DEFER: track_follow_up later_",
                "AWF-VERDICT: DEFER: track_follow_up later",
            ),
            # Stars / underscores inside inline code spans are literal content,
            # not reason emphasis that claims the outer closer
            # (PRRT_kwDOSJAM6s6bShql).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see `**`**",
                "AWF-VERDICT: FALSE POSITIVE: see `**`",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see `*`*",
                "AWF-VERDICT: FALSE POSITIVE: see `*`",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see `__`__",
                "AWF-VERDICT: FALSE POSITIVE: see `__`",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see ``**``**",
                "AWF-VERDICT: FALSE POSITIVE: see ``**``",
            ),
            # Inline HTML tokens are opaque; attribute stars do not claim the
            # outer closer (PRRT_kwDOSJAM6s6bTBv6).
            (
                '**AWF-VERDICT: FALSE POSITIVE: see <span title="**">ok</span>**',
                'AWF-VERDICT: FALSE POSITIVE: see <span title="**">ok</span>',
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see <em class='*'>ok</em>*",
                "AWF-VERDICT: FALSE POSITIVE: see <em class='*'>ok</em>",
            ),
            (
                '__AWF-VERDICT: FALSE POSITIVE: see <span title="__">ok</span>__',
                'AWF-VERDICT: FALSE POSITIVE: see <span title="__">ok</span>',
            ),
            # Link destinations are opaque; destination stars do not claim the
            # outer closer (PRRT_kwDOSJAM6s6bTLZq).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link](foo**bar)**",
                "AWF-VERDICT: FALSE POSITIVE: see [link](foo**bar)",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see [link](foo*bar)*",
                "AWF-VERDICT: FALSE POSITIVE: see [link](foo*bar)",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see [link](foo__bar)__",
                "AWF-VERDICT: FALSE POSITIVE: see [link](foo__bar)",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link](a_(b)**c)**",
                "AWF-VERDICT: FALSE POSITIVE: see [link](a_(b)**c)",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see ![img](foo**bar)**",
                "AWF-VERDICT: FALSE POSITIVE: see ![img](foo**bar)",
            ),
            # Destination adjacent to ``(`` may itself begin with balanced
            # parentheses; interior stars stay literal (PRRT_kwDOSJAM6s6bUx1F).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [x]((a(**b)))**",
                "AWF-VERDICT: FALSE POSITIVE: see [x]((a(**b)))",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see [x]((a(*b)))*",
                "AWF-VERDICT: FALSE POSITIVE: see [x]((a(*b)))",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see [x]((a(__b)))__",
                "AWF-VERDICT: FALSE POSITIVE: see [x]((a(__b)))",
            ),
            # Angle-bracket destinations and quoted titles may contain spaces
            # (PRRT_kwDOSJAM6s6bTgB6).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link](<foo **bar>)**",
                "AWF-VERDICT: FALSE POSITIVE: see [link](<foo **bar>)",
            ),
            (
                '**AWF-VERDICT: FALSE POSITIVE: see [link](url "a ** b")**',
                'AWF-VERDICT: FALSE POSITIVE: see [link](url "a ** b")',
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link]( foo**bar )**",
                "AWF-VERDICT: FALSE POSITIVE: see [link]( foo**bar )",
            ),
            # URI/email autolinks are opaque (PRRT_kwDOSJAM6s6bTgB-).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a**b>**",
                "AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a**b>",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a*b>*",
                "AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a*b>",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a__b>__",
                "AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a__b>",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see <user**name@example.com>**",
                "AWF-VERDICT: FALSE POSITIVE: see <user**name@example.com>",
            ),
            # Both-flanking mid ``*`` plus closing-only trailing ``**``: rule 9
            # blocks pairing when the opener can close, so the outer wrapper
            # stays valid (PRRT_kwDOSJAM6s6bTW7t).
            (
                "**AWF-VERDICT: FALSE POSITIVE: a*x**",
                "AWF-VERDICT: FALSE POSITIVE: a*x",
            ),
            # Label-internal closers are isolated when a link forms, so a
            # whole-line wrap with no mid-reason opener stays valid
            # (PRRT_kwDOSJAM6s6bUs3M).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [x**](url) rest**",
                "AWF-VERDICT: FALSE POSITIVE: see [x**](url) rest",
            ),
            # Non-link brackets are literals; label closers may still pair
            # across ``[`` and leave the trailing wrapper free.
            (
                "**AWF-VERDICT: FALSE POSITIVE: reason **see [x**] rest**",
                "AWF-VERDICT: FALSE POSITIVE: reason **see [x**] rest",
            ),
        ],
    )
    def test_private_markdown_emphasis_normalizer_keeps_valid_closers(
        self,
        line: str,
        expected: str,
    ) -> None:
        assert _normalize_markdown_emphasized_verdict_line(line) == expected

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("reason", "opener", "expected"),
        [
            ("This is **expected**", "**", True),
            ("rationale**", "**", False),
            ("rationale", "**", False),
            ("**a** junk**", "**", False),
            # Two closing-only exact runs: even parity, but neither opens
            # (PRRT_kwDOSJAM6s6bRfTo).
            ("rationale** more**", "**", False),
            ("rationale* more*", "*", False),
            ("rationale__ more__", "__", False),
            # Alphanumeric-to-punctuation mid run is closing-only under
            # CommonMark left-flanking; must not pair with the trailing closer
            # (PRRT_kwDOSJAM6s6bSOmb).
            ("rationale a**. more**", "**", False),
            ("rationale a*. more*", "*", False),
            ("rationale a__. more__", "__", False),
            # Longer mid-run may literalize leftovers; trailing exact pair of
            # ``**done**`` still balances (open units need not return to zero).
            ("***lead* and **done**", "**", True),
            # Partial longer-run match consumes the trailing wrapper-length
            # closer inside the reason (PRRT_kwDOSJAM6s6bR2FM).
            ("***lead* rest**", "**", True),
            # Truly both-flanking mid opener + complementary closer: rule 9
            # blocks when the opener can close (PRRT_kwDOSJAM6s6bTW7t).
            ("x**lead* rest*", "*", False),
            ("___lead_ rest__", "__", True),
            # Escaped markers are not delimiter runs.
            (r"see \**literal and **ok**", "**", True),
            # Trailing whitespace makes the closer invalid (not right-flanking).
            ("bold** ", "**", False),
            # Mid-reason opener pairs the trailing closer (PRRT_kwDOSJAM6s6bRrWv).
            ("rationale **unclosed**", "**", True),
            ("rationale *unclosed*", "*", True),
            ("rationale __unclosed__", "__", True),
            ("rationale _unclosed_", "_", True),
            # Word-internal `_` is not an emphasis opener (PRRT_kwDOSJAM6s6bRy5w).
            ("already_correct_", "_", False),
            ("see_this_", "_", False),
            ("please clarify_", "_", False),
            # Space-preceded length-1 mid opener cannot close, so rule 9 does
            # not block; it steals one star from the trailing ``**``
            # (PRRT_kwDOSJAM6s6bTBv4). BOS-leading complementary openers are
            # likewise opening-only and steal (PRRT_kwDOSJAM6s6bTi4S).
            ("*foo**", "**", True),
            ("**lead* rest*", "*", True),
            ("_foo__", "__", True),
            ("__lead_ rest_", "_", True),
            ("reason *x**", "**", True),
            # Both-flanking mid ``*`` can close: rule 9 must also consult the
            # opener and block pairing with trailing ``**`` (PRRT_kwDOSJAM6s6bTW7t).
            ("a*x**", "**", False),
            # Rule 9 skips the nearest length-1 opener; search continues to the
            # earlier length-2 opener, which claims the trailing closer
            # (PRRT_kwDOSJAM6s6bTtr5).
            ("reason **lower a*b**", "**", True),
            ("reason __lower ._.x__", "__", True),
            # Code-span markers are opaque; trailing closer is not claimed
            # (PRRT_kwDOSJAM6s6bShql).
            ("see `**`**", "**", False),
            ("see `*`*", "*", False),
            ("see `__`__", "__", False),
            ("see ``**``**", "**", False),
            # Real mid-reason emphasis after a code span still claims the closer.
            ("see `**` and **unclosed**", "**", True),
            # Unclosed code span: opening backticks are literal, so mid stars open.
            ("see `**x**", "**", True),
            # Mismatched tick lengths do not close; later emphasis still pairs
            # (PRRT_kwDOSJAM6s6bShql).
            ("see ```**x`y**", "**", True),
            # Escaped opener tick is literal; a later real tick must not open a
            # false code span that swallows mid-reason stealers
            # (PRRT_kwDOSJAM6s6bSsnj).
            (r"\` **unclosed`x**", "**", True),
            (r"\` *unclosed`x*", "*", True),
            (r"\` __unclosed`x__", "__", True),
            # Inline HTML attribute stars are opaque; trailing closer is not
            # claimed (PRRT_kwDOSJAM6s6bTBv6).
            ('see <span title="**">ok</span>**', "**", False),
            ("see <em class='*'>ok</em>*", "*", False),
            ('see <span title="__">ok</span>__', "__", False),
            # Real mid-reason emphasis after an HTML tag still claims the closer.
            ('see <span title="**">ok</span> and **unclosed**', "**", True),
            # Incomplete HTML (no ``>``) is not a tag; attribute stars remain
            # emphasis and claim the trailing closer (PRRT_kwDOSJAM6s6bTBv6).
            ('see <span title="**"text**', "**", True),
            # Escaped ``\<`` is literal; attribute stars claim the trailing
            # closer (PRRT_kwDOSJAM6s6bTLZk).
            (r'see \<span title="**">x**', "**", True),
            (r"see \<em class='*'>x*", "*", True),
            (r'see \<span title="__">x__', "__", True),
            # Even backslash run leaves ``<`` unescaped; HTML stays opaque.
            (r'see \\<span title="**">ok</span>**', "**", False),
            # URI/email autolink markers are opaque; trailing closer is not
            # claimed (PRRT_kwDOSJAM6s6bTgB-).
            ("see <https://example.test/a**b>**", "**", False),
            ("see <https://example.test/a*b>*", "*", False),
            ("see <https://example.test/a__b>__", "__", False),
            ("see <user**name@example.com>**", "**", False),
            ("see <user*name@example.com>*", "*", False),
            # Real mid-reason emphasis after an autolink still claims.
            ("see <https://example.test/a**b> and **unclosed**", "**", True),
            # Incomplete autolink (no ``>``) is not opaque; interior stars claim.
            ("see <https://example.test/a**b**", "**", True),
            ("see <user**name@example.com**", "**", True),
            # Link destinations are opaque; trailing closer is not claimed
            # (PRRT_kwDOSJAM6s6bTLZq).
            ("see [link](foo**bar)**", "**", False),
            ("see [link](foo*bar)*", "*", False),
            ("see [link](foo__bar)__", "__", False),
            # Space between ``]`` and ``(`` is not a CommonMark link; stars
            # claim the trailing closer (PRRT_kwDOSJAM6s6bTtr6).
            ("see [link] (foo**bar)**", "**", True),
            ("see [link]\t(foo**bar)**", "**", True),
            ("see [link] (__bar)__", "__", True),
            ("see [link](a_(b)**c)**", "**", False),
            ("see ![img](foo**bar)**", "**", False),
            ("see [link](<foo **bar>)**", "**", False),
            ('see [link](url "a ** b")**', "**", False),
            ('see [link](<url> "a ** b")**', "**", False),
            ("see [link](url 'a * b')*", "*", False),
            ("see [link](url (a ** b))**", "**", False),
            ("see [link](<url> (a ** b))**", "**", False),
            (r"see [link](url (a\(** b)))**", "**", False),
            ("see [link]( foo**bar )**", "**", False),
            # Leading balanced ``(`` destination (no title-separating whitespace)
            # stays opaque (PRRT_kwDOSJAM6s6bUx1F).
            ("see [x]((a(**b)))**", "**", False),
            ("see [x]((a(*b)))*", "*", False),
            ("see [x]((a(__b)))__", "__", False),
            # Unescaped ``(`` in a parenthesized title is invalid; markers claim
            # the closer (PRRT_kwDOSJAM6s6bUOZ9).
            ("see [link](url (a(**bar)))**", "**", True),
            ("see [link](url (a(*bar)))*", "*", True),
            ("see [link](url (a(__bar)))__", "__", True),
            # Whitespace before ``(`` is a parenthesized title, not a leading
            # destination; nested ``(`` still invalid (PRRT_kwDOSJAM6s6bUx1F).
            ("see [x]( (a(**b)))**", "**", True),
            ("see [x]( (a(*b)))*", "*", True),
            ("see [x]( (a(__b)))__", "__", True),
            # Real mid-reason emphasis after a link destination still claims.
            ("see [link](foo**bar) and **unclosed**", "**", True),
            ("see [link](foo __bar) and __unclosed__", "__", True),
            # Invalid destination whitespace: markers claim the closer
            # (PRRT_kwDOSJAM6s6bTgB6).
            ("see [link](foo **bar)**", "**", True),
            ("see [link](foo *bar)*", "*", True),
            ("see [link](foo __bar)__", "__", True),
            # Non-escapable ``\ `` leaves the space; destination invalid
            # (PRRT_kwDOSJAM6s6bT50A).
            (r"see [link](foo\ **bar)**", "**", True),
            (r"see [link](foo\ *bar)*", "*", True),
            (r"see [link](foo\ __bar)__", "__", True),
            # Angle-bracket destination glued to title (no whitespace) is not a
            # link; title markers claim the closer (PRRT_kwDOSJAM6s6bTvK5).
            ('see [link](<url>"**steal") rest**', "**", True),
            ("see [link](<url>'*steal') rest*", "*", True),
            ("see [link](<url>(**steal)) rest**", "**", True),
            ('see [link](<url>"__steal") rest__', "__", True),
            # Title without destination stays opaque; unclosed titles are not.
            ('see [link]( "a ** b")**', "**", False),
            ('see [link](url "a**b**', "**", True),
            ("see [link](url 'a*b*", "*", True),
            ("see [link](url (a**b**", "**", True),
            # Incomplete destination is not opaque; destination stars claim.
            ("see [link](foo**bar**", "**", True),
            ("see [link](foo __bar__", "__", True),
            # Escaped ``]`` is not a label closer; following dest stars claim.
            (r"see [link\](foo**bar)**", "**", True),
            # Unmatched ``]`` has no active label opener; parenthesized stars
            # claim the trailing closer (PRRT_kwDOSJAM6s6bTW7q).
            ("see ](foo**bar)**", "**", True),
            ("see ](foo*bar)*", "*", True),
            ("see ](foo __bar)__", "__", True),
            # Prior closed link does not leave a spare opener for a later bare ``]``.
            ("see [a](x) and ](foo**bar)**", "**", True),
            # Nested links deactivate enclosing link openers; outer ``](…)`` is
            # not a link, so destination stars claim the closer
            # (PRRT_kwDOSJAM6s6bUCMq).
            ("see [outer [inner](url)](foo**bar)**", "**", True),
            ("see [outer [inner](url)](foo*bar)*", "*", True),
            ("see [outer [inner](url)](__bar)__", "__", True),
            # Images may contain links; the image opener stays active so the
            # outer destination remains opaque.
            ("see ![outer [inner](url)](foo**bar)**", "**", False),
            # Links may contain images; forming the image does not deactivate
            # the outer link opener, so its destination stays opaque.
            ("see [outer ![img](url)](foo**bar)**", "**", False),
            # Undefined full reference labels are not links; stars in the ref id
            # remain emphasis and claim the closer (PRRT_kwDOSJAM6s6bUCMm).
            ("see [details][issue**ref]**", "**", True),
            ("see [details][issue*ref]*", "*", True),
            ("see ![img][issue**ref]**", "**", True),
            # Collapsed ``[]`` with no definition: no interior markers to steal.
            ("see [details][]**", "**", False),
            # Defined full reference labels are opaque; stars in the ref id must
            # not claim the closer (PRRT_kwDOSJAM6s6bT50C / PRRT_kwDOSJAM6s6bUCMm).
            (
                "see [details][issue**ref]\n\n[issue**ref]: /url\n**",
                "**",
                False,
            ),
            (
                "see [details][issue*ref]\n\n[issue*ref]: /url\n*",
                "*",
                False,
            ),
            (
                "see ![img][issue**ref]\n\n[issue**ref]: /url\n**",
                "**",
                False,
            ),
            # Collapsed form resolves via link text when that label is defined.
            (
                "see [issue**ref][]\n\n[issue**ref]: /url\n**",
                "**",
                False,
            ),
            # Undefined shortcut leaves label stars as emphasis
            # (PRRT_kwDOSJAM6s6bVBWW).
            ("see [issue**ref]**", "**", True),
            ("see ![issue**ref]**", "**", True),
            # Space between ``]`` and ``[`` is not a full reference link; stars
            # in the second bracket span claim the closer.
            ("see [details] [issue**ref]**", "**", True),
            # Incomplete / invalid reference labels leave markers as emphasis.
            ("see [details][issue**ref**", "**", True),
            ("see [details][iss[ue**ref]**", "**", True),
            # Link text remains inlines; stars there still claim the closer.
            ("see [de**tails][ref]**", "**", True),
            # Mid-paragraph ``[label]: dest`` is not a block definition, so the
            # full-ref label stays undefined and its stars claim the closer
            # (PRRT_kwDOSJAM6s6bUCMm).
            ("see [details][issue**ref] [other]: /url**", "**", True),
            # Reason BOS is not a document block boundary (reason sits after
            # ``AWF-VERDICT: LABEL: `` in the same paragraph). A reason-leading
            # ``[label]: dest`` must not skip label emphasis or let ``\S+``
            # absorb the trailing wrapper closer (PRRT_kwDOSJAM6s6bUPZ6).
            ("[foo**bar]: /url**", "**", True),
            ("[a*b]: /u*", "*", True),
            ("[x.__y]: /z__", "__", True),
            ("[issue**ref]: /x**", "**", True),
        ],
    )
    def test_private_verdict_reason_trailing_emphasis_balance(
        self,
        reason: str,
        opener: str,
        expected: bool,
    ) -> None:
        assert _verdict_reason_trailing_emphasis_is_balanced(reason, opener) is expected

    @pytest.mark.unit
    def test_shortcut_reference_extra_definitions_isolate_label_emphasis(self) -> None:
        # Defined shortcut (via extra defs) must isolate label stars so the
        # trailing closer is unclaimed; undefined keeps them (PRRT_kwDOSJAM6s6bVBWW).
        reason = "see [issue**ref]**"
        assert _verdict_reason_trailing_emphasis_is_balanced(reason, "**") is True
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                reason,
                "**",
                extra_reference_definitions=frozenset({"issue**ref"}),
            )
            is False
        )
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                "see ![issue**ref]**",
                "**",
                extra_reference_definitions=frozenset({"issue**ref"}),
            )
            is False
        )
        # Failed full-reference attempt must not fall through to shortcut even
        # when the link text itself is a defined label.
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                "see [issue**ref][missing]**",
                "**",
                extra_reference_definitions=frozenset({"issue**ref"}),
            )
            is True
        )
        # ``] [`` is not a full reference; the first label may still form a
        # shortcut, isolating its stars while a later undefined label claims.
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                "see [issue**ref] [other**x]**",
                "**",
                extra_reference_definitions=frozenset({"issue**ref"}),
            )
            is True
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("reason", "opener", "expected"),
        [
            # Seeded outer opener: trailing must close the seed; an earlier
            # closing-only run consumes it first (PRRT_kwDOSJAM6s6bUx1A).
            ("rationale** more**", "**", False),
            ("rationale* more*", "*", False),
            ("rationale__ more__", "__", False),
            ("rationale more**", "**", True),
            ("rationale more*", "*", True),
            ("a*x**", "**", True),
            ("rationale **unclosed**", "**", False),
        ],
    )
    def test_private_verdict_reason_trailing_emphasis_balance_seeded_outer(
        self,
        reason: str,
        opener: str,
        expected: bool,
    ) -> None:
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(reason, opener, seed_outer_opener=True)
            is expected
        )

    @pytest.mark.unit
    def test_private_markdown_emphasis_run_flanking_helpers(self) -> None:
        # Defensive bounds and content mismatches for the maximal-run helpers.
        assert _markdown_emphasis_run_can_close("", 0, 1, "*") is False
        assert _markdown_emphasis_run_can_close("**", -1, 2, "*") is False
        assert _markdown_emphasis_run_can_close("ab", 0, 2, "*") is False
        assert _markdown_emphasis_run_can_open("**", 0, 0, "*") is False
        # CommonMark: end of line counts as whitespace → not left-flanking
        # (PRRT_kwDOSJAM6s6bTBv4). Beginning of line likewise → not right-flanking
        # (PRRT_kwDOSJAM6s6bTi4S).
        assert _markdown_emphasis_run_can_open("x*", 1, 1, "*") is False
        assert _markdown_emphasis_run_can_open("**", 0, 2, "*") is False
        assert _markdown_emphasis_run_can_open("* ", 0, 1, "*") is False  # followed by space
        assert _markdown_emphasis_run_can_close(" *", 1, 1, "*") is False  # preceded by space
        assert _markdown_emphasis_run_can_close("*foo", 0, 1, "*") is False  # BOS
        assert _markdown_emphasis_run_can_close("**lead", 0, 2, "*") is False  # BOS
        assert _markdown_emphasis_run_can_close("_foo", 0, 1, "_") is False  # BOS
        assert _markdown_emphasis_run_can_close("a_b", 1, 1, "_") is False  # intra-word closer
        assert _markdown_emphasis_run_can_open("a_b", 1, 1, "_") is False  # intra-word opener
        # Non-whitespace, non-punctuation Unicode symbols also block underscore
        # open/close when the run is both-flanking (PRRT_kwDOSJAM6s6bSs2f).
        assert _markdown_emphasis_run_can_close("n_🦄", 1, 1, "_") is False
        assert _markdown_emphasis_run_can_open("🦄_n", 1, 1, "_") is False
        assert _markdown_emphasis_run_can_close("n__🦄", 1, 2, "_") is False
        assert _markdown_emphasis_run_can_open("🦄__n", 1, 2, "_") is False
        # Followed/preceded by punctuation still allows the matching side.
        assert _markdown_emphasis_run_can_close("n_.", 1, 1, "_") is True
        assert _markdown_emphasis_run_can_open("._n", 1, 1, "_") is True
        # Alphanumeric then punctuation: not left-flanking (PRRT_kwDOSJAM6s6bSOmb).
        assert _markdown_emphasis_run_can_open("a*.", 1, 1, "*") is False
        assert _markdown_emphasis_run_can_open("a**.", 1, 2, "*") is False
        # ASCII punctuation with non-P Unicode categories (PRRT_kwDOSJAM6s6bSZP4).
        assert _markdown_emphasis_run_can_open("a*$", 1, 1, "*") is False
        assert _markdown_emphasis_run_can_open("a**$", 1, 2, "*") is False
        assert _markdown_emphasis_run_can_open("a**+", 1, 2, "*") is False
        assert _markdown_emphasis_run_can_open("a**^", 1, 2, "*") is False
        # Punctuation/whitespace/BOS before + punctuation after remains left-flanking.
        assert _markdown_emphasis_run_can_open(".*.", 1, 1, "*") is True
        assert _markdown_emphasis_run_can_open("**.", 0, 2, "*") is True
        assert _markdown_emphasis_run_can_open(" **.", 1, 2, "*") is True
        assert _markdown_emphasis_run_can_open("**$", 0, 2, "*") is True
        assert _markdown_emphasis_run_can_open(" **$", 1, 2, "*") is True
        # Punctuation then alphanumeric: not right-flanking (PRRT_kwDOSJAM6s6bShqh).
        assert _markdown_emphasis_run_can_close(".*x", 1, 1, "*") is False
        assert _markdown_emphasis_run_can_close(".**x", 1, 2, "*") is False
        # ASCII punctuation with non-P Unicode categories as the precede char.
        assert _markdown_emphasis_run_can_close("$*x", 1, 1, "*") is False
        assert _markdown_emphasis_run_can_close("$**x", 1, 2, "*") is False
        assert _markdown_emphasis_run_can_close("+**x", 1, 2, "*") is False
        assert _markdown_emphasis_run_can_close("^**x", 1, 2, "*") is False
        # Punctuation before + punctuation/whitespace/EOS after remains right-flanking.
        assert _markdown_emphasis_run_can_close(".*.", 1, 1, "*") is True
        assert _markdown_emphasis_run_can_close(".**", 1, 2, "*") is True
        assert _markdown_emphasis_run_can_close(".** ", 1, 2, "*") is True
        assert _markdown_emphasis_run_can_close(".**$", 1, 2, "*") is True
        assert _emphasis_run_pair_blocked_by_multiple_of_three(1, 2, True, False) is True
        assert _emphasis_run_pair_blocked_by_multiple_of_three(1, 2, False, False) is False
        assert _emphasis_run_pair_blocked_by_multiple_of_three(1, 2, False, True) is True
        assert _emphasis_run_pair_blocked_by_multiple_of_three(3, 3, True, True) is False
        # Escaped marker characters are never flanking runs.
        assert _markdown_emphasis_run_can_close(r"a\*", 2, 1, "*") is False
        assert _markdown_emphasis_run_can_open(r"\*", 1, 1, "*") is False

    @pytest.mark.unit
    def test_private_advance_past_markdown_link_reference_label(self) -> None:
        # Defensive / CommonMark label edges for opaque full-ref skipping
        # (PRRT_kwDOSJAM6s6bT50C).
        assert _advance_past_markdown_link_reference_label("x", 0) == 0
        assert _advance_past_markdown_link_reference_label("", 0) == 0
        assert _advance_past_markdown_link_reference_label("[", 0) == 0
        assert _advance_past_markdown_link_reference_label("[]", 0) == 2
        assert _advance_past_markdown_link_reference_label("[ ]", 0) == 0
        assert _advance_past_markdown_link_reference_label("[a]", 0) == 3
        assert _advance_past_markdown_link_reference_label("[a\nb]", 0) == 0
        assert _advance_past_markdown_link_reference_label(r"[\]]", 0) == 4
        assert _advance_past_markdown_link_reference_label("[iss[ue]", 0) == 0
        assert _advance_past_markdown_link_reference_label("[" + ("a" * 999) + "]", 0) == 1001
        assert _advance_past_markdown_link_reference_label("[" + ("a" * 1000) + "]", 0) == 0
        # Escaped pairs count toward the 999-character label limit.
        assert _advance_past_markdown_link_reference_label("[" + (r"\*" * 1000) + "]", 0) == 0

    @pytest.mark.unit
    def test_private_markdown_reference_definition_helpers(self) -> None:
        # Label normalization: unescape, collapse ws, casefold
        # (PRRT_kwDOSJAM6s6bUCMm).
        assert _markdown_normalize_link_reference_label(r"  Foo\*\*Bar  ") == "foo**bar"
        assert _markdown_normalize_link_reference_label("ISSUE**REF") == "issue**ref"
        # Single-line definitions: indent, angle dest, title, rejection edges.
        assert _match_markdown_reference_definition_line("[foo]: /url") == "foo"
        assert _match_markdown_reference_definition_line("   [foo]: /url") == "foo"
        assert _match_markdown_reference_definition_line("    [foo]: /url") is None
        assert _match_markdown_reference_definition_line("[foo]: <https://ex.test>") == "foo"
        assert _match_markdown_reference_definition_line('[foo]: /url "title"') == "foo"
        assert _match_markdown_reference_definition_line("[foo]: /url 'title'") == "foo"
        assert _match_markdown_reference_definition_line("[foo]: /url (title)") == "foo"
        assert _match_markdown_reference_definition_line('[foo]: /url "title" extra') is None
        assert _match_markdown_reference_definition_line("[foo]:") is None
        assert _match_markdown_reference_definition_line("[]: /url") is None
        assert _match_markdown_reference_definition_line("see [foo]: /url") is None
        assert _match_markdown_reference_definition_line("[foo]: /url junk") is None
        assert _match_markdown_reference_definition_line("[foo]: <a<b>") is None
        assert _match_markdown_reference_definition_line("[foo]: <no-close") is None
        assert _match_markdown_reference_definition_line('[foo]: /url "unterminated') is None
        # Unbalanced non-angle destinations are not CommonMark definitions
        # (PRRT_kwDOSJAM6s6bVBWV); balanced/escaped parens remain valid.
        assert _match_markdown_reference_definition_line("[foo]: foo(bar") is None
        assert _match_markdown_reference_definition_line("[foo]: foo)") is None
        assert _match_markdown_reference_definition_line("[foo]: foo(bar)") == "foo"
        assert _match_markdown_reference_definition_line(r"[foo]: foo\(bar") == "foo"
        assert _match_markdown_reference_definition_line(r"[foo]: foo\)bar") == "foo"
        assert _match_markdown_reference_definition_line("[foo]: foo(bar) 'title'") == "foo"
        # Block-boundary spans: BOS / blank line; mid-paragraph ignored; first wins.
        text = "[Foo]: /a\n\npara [bar]: /b\n\n[bar]: /c\n[FOO]: /d\n"
        spans = _markdown_reference_definition_spans(text)
        assert [label for _, _, label in spans] == ["foo", "bar"]
        assert text[spans[0][0] : spans[0][1]] == "[Foo]: /a\n"
        assert text[spans[1][0] : spans[1][1]] == "[bar]: /c\n"
        # Duplicate normalized label at a later block boundary is ignored.
        assert "[FOO]: /d\n" not in {text[s:e] for s, e, _ in spans}
        # Opt-out: reason-fragment scans must not treat BOS as a boundary
        # (PRRT_kwDOSJAM6s6bUPZ6).
        assert (
            _markdown_reference_definition_spans(
                "[Foo]: /a\n",
                bos_is_block_boundary=False,
            )
            == []
        )
        # Blank-line boundaries still count when BOS is disabled.
        text_blank = "para\n\n[bar]: /c\n"
        spans_blank = _markdown_reference_definition_spans(
            text_blank,
            bos_is_block_boundary=False,
        )
        assert [label for _, _, label in spans_blank] == ["bar"]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "line",
        [
            # Reason-leading reference-definition lookalike with emphasis in the
            # label must not strip as whole-line wrap (PRRT_kwDOSJAM6s6bUPZ6).
            "**AWF-VERDICT: FIXED: [foo**bar]: /url**",
            "*AWF-VERDICT: FIXED: [a*b]: /u*",
            "__AWF-VERDICT: DEFER: [x.__y]: /z__",
            "**AWF-VERDICT: FALSE POSITIVE: [issue**ref]: /x**",
            # Reason-leading same run with no space after the markers is content,
            # not a label-prefix closer (PRRT_kwDOSJAM6s6bQqbC).
            "**AWF-VERDICT: FIXED:**committed",
            "*AWF-VERDICT: FIXED:*committed",
            "__AWF-VERDICT: DEFER:__track later",
            "***AWF-VERDICT: FIXED:***committed",
            # Whole-line wrap whose reason opens with the same run: trailing
            # closer belongs to the reason span; do not strip it and resolve.
            "**AWF-VERDICT: FIXED: **committed****",
            "**AWF-VERDICT: FIXED: **committed**",
            "*AWF-VERDICT: FALSE POSITIVE: *star lead* rest*",
            "__AWF-VERDICT: DEFER: __track__ later__",
            "**AWF-VERDICT: FIXED:**committed**",
            "*AWF-VERDICT: FIXED:*committed*",
        ],
    )
    def test_private_markdown_emphasis_normalizer_rejects_reason_leading_same_run(
        self,
        line: str,
    ) -> None:
        assert _normalize_markdown_emphasized_verdict_line(line) is None
        result = _parse_verdict_result(line)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("stdout", "expected_reason"),
        [
            # Whole-line emphasis must not orphan a leading run on a placeholder
            # and let FIXED / FALSE POSITIVE resolve (PRRT_kwDOSJAM6s6bQqbC).
            ("**AWF-VERDICT: FIXED: **<one-sentence summary>**", "garbled_verdict_marker"),
            ("*AWF-VERDICT: FIXED: *<one-sentence summary>*", "garbled_verdict_marker"),
            ("__AWF-VERDICT: FIXED: __<one-sentence summary>__", "garbled_verdict_marker"),
            ("**AWF-VERDICT: FALSE POSITIVE: **<reason>**", "garbled_verdict_marker"),
            ("**AWF-VERDICT: FIXED:**<one-sentence summary>**", "garbled_verdict_marker"),
            # No trailing whole-line closer: prefix path must not strip markers
            # off a placeholder either.
            ("**AWF-VERDICT: FIXED:**<one-sentence summary>", "garbled_verdict_marker"),
        ],
    )
    def test_private_awf_emphasized_reason_leading_placeholders_fail_closed(
        self,
        stdout: str,
        expected_reason: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason == expected_reason

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
