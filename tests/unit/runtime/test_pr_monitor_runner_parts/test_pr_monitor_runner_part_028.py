"""Unit tests for verdict parsing helpers (part 028).

Reference-definition title/closer coverage and trailing emphasis balance split
from part 026 to stay under the first-party file line limit.
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
    _markdown_line_is_leaf_block_boundary,
    _markdown_normalize_link_reference_label,
    _markdown_reference_definition_awaits_destination,
    _markdown_reference_definition_awaits_title,
    _markdown_reference_definition_spans,
    _match_markdown_reference_definition_line,
    _normalize_markdown_emphasized_verdict_line,
    _verdict_reason_trailing_emphasis_is_balanced,
)


class TestParseVerdict:
    @pytest.mark.unit
    def test_parse_verdict_rejects_nested_paren_in_reference_definition_title(
        self,
    ) -> None:
        # CommonMark §6.3: a parenthesized reference-definition title must not
        # contain an unescaped ``(``. Accepting ``(bad(**x)`` would register
        # ``issue**ref`` and make ``[details][issue**ref]`` opaque, wrongly
        # normalizing the emphasized wrapper to false_positive
        # (PRRT_kwDOSJAM6s6bVIzS).
        stdout = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            "[issue**ref]: /url (bad(**x)\n"
        )
        assert _markdown_reference_definition_spans(stdout) == []
        assert _match_markdown_reference_definition_line("[issue**ref]: /url (bad(**x)") is None
        result = _parse_verdict_result(stdout)
        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"
        # Valid parenthesized titles and escaped nested ``(`` remain definitions.
        assert _match_markdown_reference_definition_line("[issue**ref]: /url (title)") == (
            "issue**ref"
        )
        assert (
            _match_markdown_reference_definition_line(r"[issue**ref]: /url (bad\(**x)")
            == "issue**ref"
        )
        # Quoted titles may still contain ``(``.
        assert (
            _match_markdown_reference_definition_line('[issue**ref]: /url "bad(**x)"')
            == "issue**ref"
        )
        valid = (
            "**AWF-VERDICT: FALSE POSITIVE: see [details][issue**ref]**\n\n"
            "[issue**ref]: /url (title)\n"
        )
        valid_result = _parse_verdict_result(valid)
        assert valid_result.verdict == "false_positive"
        assert valid_result.reason == "see [details][issue**ref]"

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
            # Valid CommonMark HTML comments are opaque; stars inside do not
            # claim the outer closer. Empty forms ``<!-->`` / ``<!--->`` match
            # too (PRRT_kwDOSJAM6s6bWHdN).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see <!-- **ok** -->**",
                "AWF-VERDICT: FALSE POSITIVE: see <!-- **ok** -->",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see <!-- *ok* -->*",
                "AWF-VERDICT: FALSE POSITIVE: see <!-- *ok* -->",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see <!-- __ok__ -->__",
                "AWF-VERDICT: FALSE POSITIVE: see <!-- __ok__ -->",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see <!-->**",
                "AWF-VERDICT: FALSE POSITIVE: see <!-->",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see <!--->**",
                "AWF-VERDICT: FALSE POSITIVE: see <!--->",
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
            # CommonMark declarations are ``<![A-Z]+[^>]*>`` — no required
            # whitespace after the name — so interior stars stay literal
            # (PRRT_kwDOSJAM6s6bV5qC).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see <!A**>**",
                "AWF-VERDICT: FALSE POSITIVE: see <!A**>",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see <!A*>*",
                "AWF-VERDICT: FALSE POSITIVE: see <!A*>",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see <!DOCTYPE>__",
                "AWF-VERDICT: FALSE POSITIVE: see <!DOCTYPE>",
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
            # Opposite-marker emphasis isolates interior delimiters: ``_*foo_``
            # literalizes the unmatched ``*`` so the trailing wrapper closer is
            # not stolen (PRRT_kwDOSJAM6s6bV80s).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see _*foo_**",
                "AWF-VERDICT: FALSE POSITIVE: see _*foo_",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see *_bar_*__",
                "AWF-VERDICT: FALSE POSITIVE: see *_bar_*",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see _**bold_**",
                "AWF-VERDICT: FALSE POSITIVE: see _**bold_",
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
            # Escaped closer tick still closes the span (escapes inactive in
            # code spans); mid-reason stealers after it claim the trailing
            # closer (PRRT_kwDOSJAM6s6bWSff).
            (r"see `foo\` and **unclosed`x**", "**", True),
            (r"see `foo\` and *unclosed`x*", "*", True),
            (r"see `foo\` and __unclosed`x__", "__", True),
            # Inline HTML attribute stars are opaque; trailing closer is not
            # claimed (PRRT_kwDOSJAM6s6bTBv6).
            ('see <span title="**">ok</span>**', "**", False),
            ("see <em class='*'>ok</em>*", "*", False),
            ('see <span title="__">ok</span>__', "__", False),
            # Valid HTML comments are opaque; malformed comments (``--`` in
            # content) are not, so interior stars claim the closer
            # (PRRT_kwDOSJAM6s6bWHdN).
            ("see <!-- **ok** -->**", "**", False),
            ("see <!--**--foo-->**", "**", True),
            ("see <!--*--foo-->*", "*", True),
            ("see <!--__--foo-->__", "__", True),
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
        # Failed inline ``](`` with a defined label *does* fall through to
        # shortcut (CommonMark ex. 568; PRRT_kwDOSJAM6s6bWBAo).
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                "see [issue**ref](bad dest)**",
                "**",
                extra_reference_definitions=frozenset({"issue**ref"}),
            )
            is False
        )
        assert (
            _normalize_markdown_emphasized_verdict_line(
                "**AWF-VERDICT: FALSE POSITIVE: see [issue**ref](bad dest)**",
                extra_reference_definitions=frozenset({"issue**ref"}),
            )
            == "AWF-VERDICT: FALSE POSITIVE: see [issue**ref](bad dest)"
        )
        # Without a definition, failed inline leaves label stars as emphasis.
        assert (
            _normalize_markdown_emphasized_verdict_line(
                "**AWF-VERDICT: FALSE POSITIVE: see [issue**ref](bad dest)**",
            )
            is None
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
    def test_overlong_shortcut_label_does_not_resolve_via_normalization(
        self,
    ) -> None:
        # Overlong initial labels must not shortcut/collapse-resolve even when
        # normalization matches a short definition (PRRT_kwDOSJAM6s6bWwOQ).
        overlong_label = (" " * 1000) + "issue**ref"
        extra = frozenset({"issue**ref"})
        shortcut_reason = f"see [{overlong_label}]**"
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                shortcut_reason,
                "**",
                extra_reference_definitions=extra,
            )
            is True
        )
        assert (
            _normalize_markdown_emphasized_verdict_line(
                f"**AWF-VERDICT: FALSE POSITIVE: {shortcut_reason}**",
                extra_reference_definitions=extra,
            )
            is None
        )
        failed_inline = f"see [{overlong_label}](bad dest)**"
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                failed_inline,
                "**",
                extra_reference_definitions=extra,
            )
            is True
        )
        assert (
            _normalize_markdown_emphasized_verdict_line(
                f"**AWF-VERDICT: FALSE POSITIVE: {failed_inline}**",
                extra_reference_definitions=extra,
            )
            is None
        )
        collapsed = f"see [{overlong_label}][]**"
        assert (
            _verdict_reason_trailing_emphasis_is_balanced(
                collapsed,
                "**",
                extra_reference_definitions=extra,
            )
            is True
        )
        assert (
            _normalize_markdown_emphasized_verdict_line(
                f"**AWF-VERDICT: FALSE POSITIVE: {collapsed}**",
                extra_reference_definitions=extra,
            )
            is None
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
            # Nested opposite-marker spans isolate interior openers so the
            # seeded wrapper closer is not stolen (PRRT_kwDOSJAM6s6bV80s).
            ("see _*foo_**", "**", True),
            ("see *_bar_*__", "__", True),
            ("see _**bold_**", "**", True),
            # Unclosed opposite span leaves the interior opener active.
            ("see _*foo**", "**", False),
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
        # Escaped pairs count *both* source characters toward the 999 limit
        # (PRRT_kwDOSJAM6s6bVMBE); 500 ``\!`` pairs are 1000 source chars.
        assert _advance_past_markdown_link_reference_label("[" + (r"\!" * 500) + "]", 0) == 0
        assert _advance_past_markdown_link_reference_label("[" + (r"\!" * 499) + "x]", 0) == 1001
        assert _advance_past_markdown_link_reference_label("[" + (r"\!" * 499) + "**x]", 0) == 0
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
        assert _match_markdown_reference_definition_line(r"[foo]: <foo\>") is None
        assert _match_markdown_reference_definition_line(r"[foo]: <foo\<bar>") == "foo"
        assert _match_markdown_reference_definition_line('[foo]: /url "unterminated') is None
        # Colon-only lines await a destination continuation (PRRT_kwDOSJAM6s6bVQlQ).
        assert _markdown_reference_definition_awaits_destination("[foo]:") is True
        assert _markdown_reference_definition_awaits_destination("[foo]:   ") is True
        assert _markdown_reference_definition_awaits_destination("   [foo]:") is True
        assert _markdown_reference_definition_awaits_destination("[foo]: /url") is False
        assert _markdown_reference_definition_awaits_destination("[]:") is False
        assert _markdown_reference_definition_awaits_destination("[foo]") is False
        assert _markdown_reference_definition_awaits_destination("see [foo]:") is False
        assert _markdown_reference_definition_awaits_destination("    [foo]:") is False
        assert _markdown_reference_definition_awaits_destination("[") is False
        assert _markdown_reference_definition_awaits_destination("[foo") is False
        # Opened titles await continuation across line endings (PRRT_kwDOSJAM6s6bVrCq).
        assert _markdown_reference_definition_awaits_title('[foo]: /url "open') is True
        assert _markdown_reference_definition_awaits_title("[foo]: /url 'open") is True
        assert _markdown_reference_definition_awaits_title("[foo]: /url (open") is True
        assert _markdown_reference_definition_awaits_title('[foo]: /url "closed"') is False
        assert _markdown_reference_definition_awaits_title("[foo]: /url") is False
        assert _markdown_reference_definition_awaits_title("[foo]:") is False
        assert _markdown_reference_definition_awaits_title('[foo]: /url"glued') is False
        assert _markdown_reference_definition_awaits_title("[foo]: /url (bad(open") is False
        # ATX headings and thematic breaks are leaf-block boundaries
        # (PRRT_kwDOSJAM6s6bVZvh); paragraphs and near-misses are not.
        assert _markdown_line_is_leaf_block_boundary("# context") is True
        assert _markdown_line_is_leaf_block_boundary("###") is True
        assert _markdown_line_is_leaf_block_boundary("  ## title") is True
        assert _markdown_line_is_leaf_block_boundary("---") is True
        assert _markdown_line_is_leaf_block_boundary("* * *") is True
        assert _markdown_line_is_leaf_block_boundary("___") is True
        assert _markdown_line_is_leaf_block_boundary("") is False
        assert _markdown_line_is_leaf_block_boundary("   ") is False
        assert _markdown_line_is_leaf_block_boundary("#foo") is False
        assert _markdown_line_is_leaf_block_boundary("####### too many") is False
        assert _markdown_line_is_leaf_block_boundary("--") is False
        assert _markdown_line_is_leaf_block_boundary("-*-") is False
        assert _markdown_line_is_leaf_block_boundary("paragraph") is False
        # Container prefixes must be peeled before leaf checks
        # (PRRT_kwDOSJAM6s6bWLeD).
        assert _markdown_line_is_leaf_block_boundary("> # context") is True
        assert _markdown_line_is_leaf_block_boundary("> ---") is True
        assert _markdown_line_is_leaf_block_boundary("> * * *") is True
        assert _markdown_line_is_leaf_block_boundary("- # context") is True
        assert _markdown_line_is_leaf_block_boundary("> - # context") is True
        assert _markdown_line_is_leaf_block_boundary("> paragraph") is False
        # Setext underlines are leaf boundaries only after paragraph content
        # (PRRT_kwDOSJAM6s6bVkD0); bare ``===`` is not a thematic break.
        assert _markdown_line_is_leaf_block_boundary("===") is False
        assert _markdown_line_is_leaf_block_boundary("=", after_paragraph=True) is True
        assert _markdown_line_is_leaf_block_boundary("===", after_paragraph=True) is True
        assert _markdown_line_is_leaf_block_boundary("  ==", after_paragraph=True) is True
        assert _markdown_line_is_leaf_block_boundary("--", after_paragraph=True) is True
        assert _markdown_line_is_leaf_block_boundary("-", after_paragraph=True) is True
        assert _markdown_line_is_leaf_block_boundary("===", after_paragraph=False) is False
        assert _markdown_line_is_leaf_block_boundary("= = =", after_paragraph=True) is False
        assert _markdown_line_is_leaf_block_boundary("=-", after_paragraph=True) is False
        assert _markdown_line_is_leaf_block_boundary("> ===", after_paragraph=True) is True
        assert _markdown_line_is_leaf_block_boundary("> ===", after_paragraph=False) is False
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
        # Destination on the line after colon (PRRT_kwDOSJAM6s6bVQlQ).
        cont = "[Foo]:\n  /a\n\n[bar]:\n/c\n"
        cont_spans = _markdown_reference_definition_spans(cont)
        assert [label for _, _, label in cont_spans] == ["foo", "bar"]
        assert cont[cont_spans[0][0] : cont_spans[0][1]] == "[Foo]:\n  /a\n"
        assert cont[cont_spans[1][0] : cont_spans[1][1]] == "[bar]:\n/c\n"
        # Multiline title continuation (PRRT_kwDOSJAM6s6bVrCq).
        title_cont = '[Foo]: /url "multi\nline"\n'
        title_spans = _markdown_reference_definition_spans(title_cont)
        assert [label for _, _, label in title_spans] == ["foo"]
        assert title_cont[title_spans[0][0] : title_spans[0][1]] == '[Foo]: /url "multi\nline"\n'
        # CRLF: strip ``\r`` on both the colon line and the destination continuation.
        crlf = "[Foo]:\r\n  /a\r\n"
        crlf_spans = _markdown_reference_definition_spans(crlf)
        assert [label for _, _, label in crlf_spans] == ["foo"]
        assert crlf[crlf_spans[0][0] : crlf_spans[0][1]] == "[Foo]:\r\n  /a\r\n"
        # Lone ``\r`` line boundaries (progress redraws) must match verdict-line iteration
        # (PRRT_kwDOSJAM6s6bWzca).
        lone_cr = "para\r\r[foo]: /url\r"
        lone_cr_spans = _markdown_reference_definition_spans(lone_cr)
        assert [label for _, _, label in lone_cr_spans] == ["foo"]
        assert lone_cr[lone_cr_spans[0][0] : lone_cr_spans[0][1]] == "[foo]: /url\r"
        # Invalid continuation destination does not register a definition.
        assert _markdown_reference_definition_spans("[foo]:\n  foo(bar\n") == []
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
        # Leaf blocks (ATX / thematic) establish a boundary without a blank
        # (PRRT_kwDOSJAM6s6bVZvh); ordinary paragraphs do not.
        leaf = "para\n# heading\n[foo]: /url\n"
        assert [label for _, _, label in _markdown_reference_definition_spans(leaf)] == ["foo"]
        thematic = "para\n---\n[foo]: /url\n"
        assert [label for _, _, label in _markdown_reference_definition_spans(thematic)] == ["foo"]
        assert _markdown_reference_definition_spans("para\n[foo]: /url\n") == []
        # Nested leaf boundaries after peeling containers (PRRT_kwDOSJAM6s6bWLeD).
        nested_atx = "> # heading\n> [foo]: /url\n"
        assert [label for _, _, label in _markdown_reference_definition_spans(nested_atx)] == [
            "foo"
        ]
        nested_thematic = "> ---\n> [foo]: /url\n"
        assert [label for _, _, label in _markdown_reference_definition_spans(nested_thematic)] == [
            "foo"
        ]
        # Setext underlines complete a heading leaf block (PRRT_kwDOSJAM6s6bVkD0).
        setext = "Heading\n===\n[foo]: /url\n"
        assert [label for _, _, label in _markdown_reference_definition_spans(setext)] == ["foo"]
        setext_dash = "Heading\n--\n[foo]: /url\n"
        assert [label for _, _, label in _markdown_reference_definition_spans(setext_dash)] == [
            "foo"
        ]
        nested_setext = "> Heading\n> ===\n> [foo]: /url\n"
        assert [label for _, _, label in _markdown_reference_definition_spans(nested_setext)] == [
            "foo"
        ]
        # Bare ``===`` at BOS or after a blank is a paragraph, not a Setext
        # underline — the following line continues that paragraph.
        assert _markdown_reference_definition_spans("===\n[foo]: /url\n") == []
        assert _markdown_reference_definition_spans("para\n\n===\n[foo]: /url\n") == []
        assert _markdown_reference_definition_spans("> ===\n> [foo]: /url\n") == []
        # Entering a blockquote/list after outer paragraph content must not
        # reuse that paragraph for peeled Setext detection — ``> ===`` /
        # ``- ===`` starts a new paragraph inside the container
        # (PRRT_kwDOSJAM6s6bWOTK).
        assert _markdown_reference_definition_spans("para\n> ===\n> [foo]: /url\n") == []
        assert _markdown_reference_definition_spans("para\n- ===\n  [foo]: /url\n") == []
        assert _markdown_reference_definition_spans("para\n> > ===\n> > [foo]: /url\n") == []
        assert _markdown_reference_definition_spans("> para\n> > ===\n> > [foo]: /url\n") == []
        assert _markdown_reference_definition_spans("- Heading\n- ===\n  [foo]: /url\n") == []
        # Same-container list-item indent continuation still completes Setext.
        assert [
            label
            for _, _, label in _markdown_reference_definition_spans(
                "- Heading\n  ===\n  [foo]: /url\n"
            )
        ] == ["foo"]

    @pytest.mark.unit
    @pytest.mark.parametrize(
        ("line", "expected_reason"),
        [
            (
                "**AWF-VERDICT: FALSE POSITIVE:**(expected)**",
                "(expected)",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE:**(expected)",
                "(expected)",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE:*(expected)*",
                "(expected)",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE:*(expected)",
                "(expected)",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE:__(expected)__",
                "(expected)",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE:__(expected)",
                "(expected)",
            ),
        ],
    )
    def test_private_markdown_emphasis_normalizer_accepts_punctuation_after_prefix_closer(
        self,
        line: str,
        expected_reason: str,
    ) -> None:
        # CommonMark punctuation-flanked closers need no whitespace before the
        # reason; prefix-only forms need no trailing whole-line wrap
        # (PRRT_kwDOSJAM6s6bW-zR, PRRT_kwDOSJAM6s6bXIVh).
        assert (
            _normalize_markdown_emphasized_verdict_line(line)
            == f"AWF-VERDICT: FALSE POSITIVE:{expected_reason}"
        )
        result = _parse_verdict_result(line)
        assert result.verdict == "false_positive"
        assert result.reason == expected_reason

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
