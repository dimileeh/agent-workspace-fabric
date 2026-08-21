"""Unit tests for verdict parsing helpers (part 021)."""

from __future__ import annotations

import html

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _parse_verdict_result,
)
from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _aggressively_peel_verdict_reason_wrappers,
    _html_wrapper_close_suffix_start,
    _peel_all_outer_html_verdict_reason_wrappers,
    _peel_all_outer_unconditional_verdict_reason_wrappers,
    _peel_one_unconditional_verdict_reason_wrapper,
    _span_is_python_dunder,
    _strip_final_answer_attempt_prefix,
    _strip_markdown_attempt_prefixes,
    _strip_markdown_blockquote_prefix,
    _strip_markdown_emphasis_prefix,
    _strip_markdown_heading_prefix,
    _strip_markdown_list_prefix,
    _strip_markdown_task_list_checkbox,
    _strip_verdict_result_label_attempt_prefix,
)
from awf.runtime.pr_monitor_runner.helpers_verdict_markdown import (
    _VERDICT_REASON_PYTHON_DUNDER,
    _verdict_reason_inline_link_label,
)


class TestParseVerdict:
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
            "AWF-VERDICT: FALSE POSITIVE: ~~<one-sentence justification>~~",
            "AWF-VERDICT: FALSE POSITIVE: ~~<reason>~~",
            "AWF-VERDICT: FALSE POSITIVE: ~~`<one-sentence justification>`~~",
            "AWF-VERDICT: FALSE POSITIVE: [<one-sentence justification>](https://example.com)",
            "AWF-VERDICT: FALSE POSITIVE: [`<one-sentence justification>`](https://example.com)",
            "AWF-VERDICT: FALSE POSITIVE: [~~<reason>~~](https://example.com)",
            # Markdown image labels (leading ``!``) must peel like links when
            # placeholder-shaped (PRRT_kwDOSJAM6s6Zo-5M).
            "AWF-VERDICT: FALSE POSITIVE: ![<one-sentence justification>](https://example.com)",
            "AWF-VERDICT: FALSE POSITIVE: ![`<one-sentence justification>`](https://example.com)",
            "AWF-VERDICT: FALSE POSITIVE: ![~~<reason>~~](https://example.com)",
            # Destinations with balanced / escaped parentheses must still peel
            # (PRRT_kwDOSJAM6s6ZpLqR); ``[^)]*`` stops at the inner ``)``.
            "AWF-VERDICT: FALSE POSITIVE: [<reason>](https://example.com/a_(b))",
            "AWF-VERDICT: FALSE POSITIVE: [<one-sentence justification>](https://example.com/a_(b)_c)",
            r"AWF-VERDICT: FALSE POSITIVE: [<reason>](https://example.com/a_\(b\))",
            "AWF-VERDICT: FALSE POSITIVE: ![<reason>](https://example.com/a_(b))",
            # Optional whitespace after ``[`` / before ``(`` still peels.
            "AWF-VERDICT: FALSE POSITIVE: [ <reason>](https://example.com/a_(b))",
            "AWF-VERDICT: FALSE POSITIVE: [<reason>] (https://example.com/a_(b))",
            # Reference-style links / images must peel when the label is
            # placeholder-shaped (PRRT_kwDOSJAM6s6ZpXSL).
            "AWF-VERDICT: FALSE POSITIVE: [<reason>][details]",
            "AWF-VERDICT: FALSE POSITIVE: [<reason>][]",
            "AWF-VERDICT: FALSE POSITIVE: [<one-sentence justification>][details]",
            "AWF-VERDICT: FALSE POSITIVE: [`<one-sentence justification>`][]",
            "AWF-VERDICT: FALSE POSITIVE: ![<reason>][details]",
            "AWF-VERDICT: FALSE POSITIVE: ![<reason>][]",
            "AWF-VERDICT: FALSE POSITIVE: [ <reason>][details]",
            "AWF-VERDICT: FALSE POSITIVE: [<reason>] [details]",
            # Shortcut reference ``[label]`` / ``![label]`` must peel when the
            # label is placeholder-shaped (PRRT_kwDOSJAM6s6Zp8jK).
            "AWF-VERDICT: FALSE POSITIVE: [<reason>]",
            "AWF-VERDICT: FALSE POSITIVE: [<one-sentence justification>]",
            "AWF-VERDICT: FALSE POSITIVE: ![<reason>]",
            "AWF-VERDICT: FALSE POSITIVE: [ <reason>]",
            "AWF-VERDICT: FALSE POSITIVE: [`<one-sentence justification>`]",
            "AWF-VERDICT: FIXED: [<one-sentence summary>]",
            "AWF-VERDICT: FIXED: [<one-sentence summary>][]",
            "AWF-VERDICT: FIXED: [<one-sentence summary>](https://example.com/a_(b))",
            "AWF-VERDICT: FIXED: `<one-sentence summary>`",
            "AWF-VERDICT: FIXED: **<one-sentence summary>**",
            "AWF-VERDICT: FIXED: *<one-sentence summary>*",
            "AWF-VERDICT: FIXED: ~~<one-sentence summary>~~",
            "AWF-VERDICT: FIXED: [<one-sentence summary>](https://example.com)",
            "AWF-VERDICT: FIXED: ![<one-sentence summary>](https://example.com)",
            # HTML entity-escaped whole-reason placeholders (PRRT_kwDOSJAM6s6Zoyj2).
            "AWF-VERDICT: FALSE POSITIVE: &lt;reason&gt;",
            "AWF-VERDICT: FALSE POSITIVE: &lt;one-sentence justification&gt;",
            "AWF-VERDICT: FALSE POSITIVE: &#60;reason&#62;",
            "AWF-VERDICT: FALSE POSITIVE: &#x3c;one-sentence summary&#x3e;",
            "AWF-VERDICT: FALSE POSITIVE: `&lt;reason&gt;`",
            "AWF-VERDICT: FIXED: &lt;one-sentence summary&gt;",
            "AWF-VERDICT: DEFER: &lt;what to track&gt;",
            # Nested HTML entity escapes still decode to a placeholder
            # (PRRT_kwDOSJAM6s6Zo4bG).
            "AWF-VERDICT: FALSE POSITIVE: &amp;lt;reason&amp;gt;",
            "AWF-VERDICT: FALSE POSITIVE: &amp;amp;lt;one-sentence justification&amp;amp;gt;",
            "AWF-VERDICT: FALSE POSITIVE: &amp;amp;amp;lt;reason&amp;amp;amp;gt;",
            "AWF-VERDICT: FIXED: &amp;lt;one-sentence summary&amp;gt;",
            "AWF-VERDICT: DEFER: &amp;lt;what to track&amp;gt;",
            # CommonMark backslash-escaped whole-reason placeholders
            # (PRRT_kwDOSJAM6s6ZpA-z).
            r"AWF-VERDICT: FALSE POSITIVE: \<reason\>",
            r"AWF-VERDICT: FALSE POSITIVE: \<one-sentence justification\>",
            r"AWF-VERDICT: FALSE POSITIVE: `\<reason\>`",
            r"AWF-VERDICT: FIXED: \<one-sentence summary\>",
            r"AWF-VERDICT: DEFER: \<what to track\>",
            # Nested backslash escapes still decode to a placeholder.
            r"AWF-VERDICT: FALSE POSITIVE: \\\<reason\\\>",
            # Mixed HTML-entity + CommonMark backslash escapes need successive
            # normalization; either transform alone leaves a layer behind
            # (PRRT_kwDOSJAM6s6ZpHXM).
            r"AWF-VERDICT: FALSE POSITIVE: \&lt;reason\&gt;",
            r"AWF-VERDICT: FALSE POSITIVE: \&lt;one-sentence justification\&gt;",
            r"AWF-VERDICT: FALSE POSITIVE: `\&lt;reason\&gt;`",
            r"AWF-VERDICT: FIXED: \&lt;one-sentence summary\&gt;",
            r"AWF-VERDICT: DEFER: \&lt;what to track\&gt;",
            r"AWF-VERDICT: FALSE POSITIVE: \&amp;lt;reason\&amp;gt;",
            # Safe whole-reason inline HTML wrappers must peel when the
            # enclosed text is placeholder-shaped (PRRT_kwDOSJAM6s6ZpdhJ).
            "AWF-VERDICT: FALSE POSITIVE: <em>&lt;reason&gt;</em>",
            "AWF-VERDICT: FALSE POSITIVE: <em><reason></em>",
            "AWF-VERDICT: FALSE POSITIVE: <em>&lt;one-sentence justification&gt;</em>",
            "AWF-VERDICT: FALSE POSITIVE: <strong>&lt;reason&gt;</strong>",
            "AWF-VERDICT: FALSE POSITIVE: <strong><one-sentence justification></strong>",
            "AWF-VERDICT: FALSE POSITIVE: <span>&lt;one-sentence justification&gt;</span>",
            "AWF-VERDICT: FALSE POSITIVE: <span><reason></span>",
            "AWF-VERDICT: FALSE POSITIVE: <EM>&lt;reason&gt;</EM>",
            "AWF-VERDICT: FALSE POSITIVE: <em class='x'>&lt;reason&gt;</em>",
            "AWF-VERDICT: FALSE POSITIVE: <em>`&lt;reason&gt;`</em>",
            "AWF-VERDICT: FALSE POSITIVE: <strong>**<reason>**</strong>",
            # Quoted ``>`` inside wrapper attributes must not truncate the open
            # tag and leave a non-placeholder remnant (PRRT_kwDOSJAM6s6ZsvLy).
            'AWF-VERDICT: FALSE POSITIVE: <span title="1 > 0">&lt;reason&gt;</span>',
            "AWF-VERDICT: FALSE POSITIVE: <span title='1 > 0'>&lt;reason&gt;</span>",
            'AWF-VERDICT: FALSE POSITIVE: <em title="1 > 0">&lt;reason&gt;</em>',
            'AWF-VERDICT: FALSE POSITIVE: <code data-value=">">&lt;reason&gt;</code>',
            'AWF-VERDICT: FIXED: <span title="1 > 0">&lt;one-sentence summary&gt;</span>',
            # Inline ``<code>`` HTML wrappers (not Markdown ticks) must peel
            # when placeholder-shaped (PRRT_kwDOSJAM6s6Zq76j).
            "AWF-VERDICT: FALSE POSITIVE: <code>&lt;reason&gt;</code>",
            "AWF-VERDICT: FALSE POSITIVE: <code><reason></code>",
            "AWF-VERDICT: FALSE POSITIVE: <CODE>&lt;reason&gt;</CODE>",
            "AWF-VERDICT: FALSE POSITIVE: <code class='x'>&lt;reason&gt;</code>",
            "AWF-VERDICT: FALSE POSITIVE: <code>`&lt;reason&gt;`</code>",
            "AWF-VERDICT: FALSE POSITIVE: <em><code>&lt;reason&gt;</code></em>",
            "AWF-VERDICT: FIXED: <code>&lt;one-sentence summary&gt;</code>",
            "AWF-VERDICT: DEFER: <code>&lt;what to track&gt;</code>",
            "AWF-VERDICT: FIXED: <em>&lt;one-sentence summary&gt;</em>",
            "AWF-VERDICT: FIXED: <span>&lt;one-sentence summary&gt;</span>",
            "AWF-VERDICT: DEFER: <em>&lt;what to track&gt;</em>",
        ],
    )
    def test_private_awf_formatted_placeholder_reason_fail_closed(self, stdout: str) -> None:
        # Balanced quote/backtick/Markdown-strong/strikethrough wrappers around a
        # template placeholder must not leave the echo as a usable reason
        # (PRRT_kwDOSJAM6s6Zn-VK, PRRT_kwDOSJAM6s6ZoAz9, PRRT_kwDOSJAM6s6ZopxG).
        # Single emphasis is peeled only when the enclosed value is placeholder-
        # shaped (PRRT_kwDOSJAM6s6ZoDQU). Markdown link labels are peeled the
        # same way when placeholder-shaped (PRRT_kwDOSJAM6s6Zos6S), including
        # image wrappers with a leading ``!`` (PRRT_kwDOSJAM6s6Zo-5M) and
        # destinations with balanced / escaped parentheses
        # (PRRT_kwDOSJAM6s6ZpLqR). Reference-style ``[label][ref]`` /
        # ``[label][]`` forms likewise (PRRT_kwDOSJAM6s6ZpXSL), including
        # shortcut ``[label]`` / ``![label]`` (PRRT_kwDOSJAM6s6Zp8jK).
        # HTML entity-escaped whole-reason echoes must also fail closed
        # (PRRT_kwDOSJAM6s6Zoyj2), including nested escapes (PRRT_kwDOSJAM6s6Zo4bG).
        # CommonMark backslash escapes (``\<reason\>``) likewise
        # (PRRT_kwDOSJAM6s6ZpA-z). Mixed HTML + backslash layers must decode
        # successively (PRRT_kwDOSJAM6s6ZpHXM). Safe inline HTML wrappers
        # (``<em>`` / ``<strong>`` / ``<span>`` / ``<code>``) peel the same
        # way when placeholder-shaped (PRRT_kwDOSJAM6s6ZpdhJ,
        # PRRT_kwDOSJAM6s6Zq76j), including attributes whose quoted values
        # contain ``>`` (PRRT_kwDOSJAM6s6ZsvLy).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "needs_human"
        assert result.reason in {"verdict_placeholder_echo", "fixed_placeholder_echo"}

    @pytest.mark.unit
    def test_private_awf_escape_normalization_cap_exhaustion_fail_closed(self) -> None:
        # Nested HTML entity layers beyond the mixed-unescape pass budget left a
        # still-encoded remnant (``&lt;reason&gt;``) that was accepted as a
        # substantive FALSE POSITIVE reason. Cap exhaustion must fail closed
        # (PRRT_kwDOSJAM6s6Zqip7).
        nested = "<reason>"
        for _ in range(17):
            nested = html.escape(nested)
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: {nested}")

        assert result.verdict == "needs_human"
        assert result.reason == "verdict_placeholder_echo"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "wrapper",
        [
            ("<span>", "</span>"),
            ("<em>", "</em>"),
            ("<strong>", "</strong>"),
        ],
    )
    def test_private_awf_deeply_nested_html_placeholder_fail_closed(
        self, wrapper: tuple[str, str]
    ) -> None:
        # Recursive normalize of placeholder-gated HTML wrappers hit Python's
        # recursion limit around ~1k nested <em>/<strong>/<span> layers before
        # the 500-char reason bound applies, crashing the monitor instead of
        # fail-closed needs_human (PRRT_kwDOSJAM6s6Zpg0B).
        open_tag, close_tag = wrapper
        nested = "<reason>"
        for _ in range(1000):
            nested = f"{open_tag}{nested}{close_tag}"
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: {nested}")

        assert result.verdict == "needs_human"
        assert result.reason == "verdict_placeholder_echo"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "wrapper",
        [
            ("<span>", "</span>"),
            ("<em>", "</em>"),
            ("<strong>", "</strong>"),
        ],
    )
    def test_private_awf_very_deep_html_placeholder_peel_stays_linear(
        self, wrapper: tuple[str, str]
    ) -> None:
        # Per-layer fullmatch peels are quadratic on deep em/strong/span nests
        # and can stall the monitor event loop before the 500-char reason bound
        # (PRRT_kwDOSJAM6s6Zpjww). Tens of thousands of layers must still
        # fail closed without approaching the default test timeout.
        open_tag, close_tag = wrapper
        nested = "<reason>"
        for _ in range(20_000):
            nested = f"{open_tag}{nested}{close_tag}"
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: {nested}")

        assert result.verdict == "needs_human"
        assert result.reason == "verdict_placeholder_echo"

    @pytest.mark.unit
    def test_private_linear_html_wrapper_peel_edges(self) -> None:
        # Direct contract for the O(n) HTML peel used by speculative normalize
        # (PRRT_kwDOSJAM6s6Zpjww): strip outer nests, refuse mismatched closes,
        # and ignore leading/trailing whitespace without a fullmatch rescan.
        assert _peel_all_outer_html_verdict_reason_wrappers("  <em><span>x</span></em>  ") == "x"
        assert _peel_all_outer_html_verdict_reason_wrappers("<em>x</strong>") == "<em>x</strong>"
        assert _peel_all_outer_html_verdict_reason_wrappers("<em>x</em >") == "x"
        assert _peel_all_outer_html_verdict_reason_wrappers("<code>x</code>") == "x"
        assert _peel_all_outer_html_verdict_reason_wrappers("<code><em>x</em></code>") == "x"
        assert (
            _peel_all_outer_html_verdict_reason_wrappers(
                '<span title="1 > 0">&lt;reason&gt;</span>'
            )
            == "&lt;reason&gt;"
        )
        assert _peel_all_outer_html_verdict_reason_wrappers("plain") == "plain"
        assert _html_wrapper_close_suffix_start("nope", 0, 4, "em") is None
        assert _html_wrapper_close_suffix_start("<em>", 0, 0, "em") is None
        assert _html_wrapper_close_suffix_start("x</em>", 0, 6, "strong") is None
        assert _html_wrapper_close_suffix_start("x<em>", 0, 5, "em") is None
        assert _html_wrapper_close_suffix_start("x/em>", 0, 5, "em") is None
        # Optional spaces before ``>`` / before the close tag still count.
        assert _html_wrapper_close_suffix_start("x</em >", 0, 7, "em") == 1
        assert _html_wrapper_close_suffix_start("x  </em>", 0, 8, "em") == 1
        # Aggressive peel still walks mixed non-HTML + HTML layers after the
        # linear HTML prefix strip (PRRT_kwDOSJAM6s6Zpjww).
        assert _aggressively_peel_verdict_reason_wrappers("**<em><reason></em>**") == "<reason>"
        assert _aggressively_peel_verdict_reason_wrappers("*<reason>*") == "<reason>"
        assert _aggressively_peel_verdict_reason_wrappers("[<reason>](https://example.com)") == (
            "<reason>"
        )

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "wrap",
        [
            lambda inner: f'"{inner}"',
            lambda inner: f"'{inner}'",
            lambda inner: f"`{inner}`",
            lambda inner: f"**{inner}**",
            lambda inner: f"__{inner}__",
            lambda inner: f"~~{inner}~~",
        ],
        ids=["dq", "sq", "tick", "strong_star", "strong_under", "strike"],
    )
    def test_private_awf_very_deep_unconditional_placeholder_peel_stays_linear(
        self, wrap: object
    ) -> None:
        # Per-layer fullmatch peels are quadratic on deep quote/tick/strong/strike
        # nests and can stall the monitor event loop before the 500-char reason
        # bound (PRRT_kwDOSJAM6s6ZqS4V). Underscore-strong nests must also avoid
        # per-layer Python-dunder fullmatch rescans (PRRT_kwDOSJAM6s6ZqZoz).
        # Tens of thousands of layers must still fail closed without approaching
        # the default test timeout.
        nested = "<reason>"
        for _ in range(20_000):
            nested = wrap(nested)  # type: ignore[operator]
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: {nested}")

        assert result.verdict == "needs_human"
        assert result.reason == "verdict_placeholder_echo"

    @pytest.mark.unit
    def test_private_linear_unconditional_wrapper_peel_edges(self) -> None:
        # Direct contract for the O(n) unconditional peel (PRRT_kwDOSJAM6s6ZqS4V).
        peel = _peel_all_outer_unconditional_verdict_reason_wrappers
        assert peel('  "**x**"  ') == "x"
        assert peel("`` `x` ``") == "x"
        assert peel("~~**x**~~") == "x"
        assert peel("__init__") == "__init__"
        assert peel("plain") == "plain"
        assert peel('""') == ""
        assert peel("```") == "`"
        assert peel("“x”") == "x"
        assert peel("‘x’") == "x"
        # Whitespace between __ layers can reveal a dunder after strip; must not
        # over-peel once the span becomes __ident__ (PRRT_kwDOSJAM6s6ZqZoz).
        assert peel("__ __init__ __") == "__init__"
        assert peel("__ __name__ __") == "__name__"
        assert peel("__ __<reason>__ __") == "<reason>"
        assert peel("__123__") == "123"
        # One-layer regex helper stays aligned with a single cursor peel.
        assert _peel_one_unconditional_verdict_reason_wrapper('"<reason>"') == "<reason>"
        assert _peel_one_unconditional_verdict_reason_wrapper("__init__") is None
        assert _peel_one_unconditional_verdict_reason_wrapper("*<reason>*") is None
        assert _aggressively_peel_verdict_reason_wrappers('*"<reason>"*') == "<reason>"

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "text",
        [
            "__init__",
            "__name__",
            "__all__",
            "____init____",
            "__hello__",
            "__<reason>__",
            "____",
            "__",
            "__123__",
            "init",
            "_init_",
            "__init_",
            "_init__",
            "__init __",
        ],
    )
    def test_private_span_is_python_dunder_matches_regex(self, text: str) -> None:
        # Cursor-local helper must stay equivalent to the legacy fullmatch
        # pattern (PRRT_kwDOSJAM6s6ZqZoz).
        assert _span_is_python_dunder(text, 0, len(text)) is (
            _VERDICT_REASON_PYTHON_DUNDER.fullmatch(text) is not None
        )

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
            "AWF-VERDICT: FALSE POSITIVE: ~~stale review boilerplate~~",
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
            (
                "AWF-VERDICT: FALSE POSITIVE: added the &lt;summary&gt; section",
                "added the &lt;summary&gt; section",
            ),
            # Nested escapes still leave mid-reason prose usable after bounded
            # decode for the anchored placeholder check (PRRT_kwDOSJAM6s6Zo4bG).
            (
                "AWF-VERDICT: FALSE POSITIVE: added the &amp;lt;summary&amp;gt; section",
                "added the &amp;lt;summary&amp;gt; section",
            ),
            # CommonMark backslash mid-reason tags stay usable (PRRT_kwDOSJAM6s6ZpA-z).
            (
                r"AWF-VERDICT: FALSE POSITIVE: added the \<summary\> section",
                r"added the \<summary\> section",
            ),
            # Mixed HTML + backslash mid-reason tags stay usable after successive
            # decode (PRRT_kwDOSJAM6s6ZpHXM).
            (
                r"AWF-VERDICT: FALSE POSITIVE: added the \&lt;summary\&gt; section",
                r"added the \&lt;summary\&gt; section",
            ),
        ],
    )
    def test_private_awf_entity_escaped_mid_reason_tag_still_usable(
        self, stdout: str, expected_reason: str
    ) -> None:
        # Anchored placeholder detection must not treat mid-reason entity-escaped
        # tags as whole-reason template echoes (PRRT_kwDOSJAM6s6Zoyj2).
        # Same for CommonMark backslash escapes (PRRT_kwDOSJAM6s6ZpA-z) and
        # mixed HTML + backslash layers (PRRT_kwDOSJAM6s6ZpHXM).
        result = _parse_verdict_result(stdout)

        assert result.verdict == "false_positive"
        assert result.reason == expected_reason

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
            # Markdown links with real labels stay intact (PRRT_kwDOSJAM6s6Zos6S).
            (
                "AWF-VERDICT: FALSE POSITIVE: [stale review boilerplate](https://example.com)",
                "[stale review boilerplate](https://example.com)",
            ),
            # Real image alts likewise stay intact (PRRT_kwDOSJAM6s6Zo-5M).
            (
                "AWF-VERDICT: FALSE POSITIVE: ![stale review boilerplate](https://example.com)",
                "![stale review boilerplate](https://example.com)",
            ),
            # Balanced-paren destinations still leave real labels intact
            # (PRRT_kwDOSJAM6s6ZpLqR).
            (
                "AWF-VERDICT: FALSE POSITIVE: [stale review boilerplate](https://example.com/a_(b))",
                "[stale review boilerplate](https://example.com/a_(b))",
            ),
            # Reference-style links with real labels stay intact
            # (PRRT_kwDOSJAM6s6ZpXSL).
            (
                "AWF-VERDICT: FALSE POSITIVE: [stale review boilerplate][details]",
                "[stale review boilerplate][details]",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: [stale review boilerplate][]",
                "[stale review boilerplate][]",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: ![stale review boilerplate][details]",
                "![stale review boilerplate][details]",
            ),
            # Shortcut references with real labels stay intact
            # (PRRT_kwDOSJAM6s6Zp8jK).
            (
                "AWF-VERDICT: FALSE POSITIVE: [stale review boilerplate]",
                "[stale review boilerplate]",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: ![stale review boilerplate]",
                "![stale review boilerplate]",
            ),
            # Safe inline HTML around real prose is not peeled (PRRT_kwDOSJAM6s6ZpdhJ,
            # PRRT_kwDOSJAM6s6Zq76j).
            (
                "AWF-VERDICT: FALSE POSITIVE: <em>stale review boilerplate</em>",
                "<em>stale review boilerplate</em>",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: <strong>stale review boilerplate</strong>",
                "<strong>stale review boilerplate</strong>",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: <span>stale review boilerplate</span>",
                "<span>stale review boilerplate</span>",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: <code>stale review boilerplate</code>",
                "<code>stale review boilerplate</code>",
            ),
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
        ("reason", "expected_label"),
        [
            ("[<reason>](https://example.com/a_(b))", "<reason>"),
            ("![<reason>](https://example.com/a_(b))", "<reason>"),
            ("[ <reason>](https://example.com)", "<reason>"),
            ("[<reason>] (https://example.com)", "<reason>"),
            (r"[<reason>](https://example.com/a_\(b\))", "<reason>"),
            # Reference-style full / collapsed forms (PRRT_kwDOSJAM6s6ZpXSL).
            ("[<reason>][details]", "<reason>"),
            ("[<reason>][]", "<reason>"),
            ("![<reason>][details]", "<reason>"),
            ("![<reason>][]", "<reason>"),
            ("[ <reason>][details]", "<reason>"),
            ("[<reason>] [details]", "<reason>"),
            (r"[<reason>][det\[ail\]]", "<reason>"),
            # Shortcut reference / image (PRRT_kwDOSJAM6s6Zp8jK).
            ("[<reason>]", "<reason>"),
            ("![<reason>]", "<reason>"),
            ("[ <reason>]", "<reason>"),
            ("[<reason> ]", "<reason>"),
            # Newline / unbalanced / trailing content / non-links do not match.
            ("[<reason>](https://example.com/a\n_(b))", None),
            ("[<reason>](https://example.com/a_(b)", None),
            ("[<reason>](https://example.com/a_(b))x", None),
            ("[<reason>][details\nx]", None),
            ("[<reason>][details", None),
            ("[<reason>][details]x", None),
            ("[<reason>]x", None),
            ("<reason>", None),
            ("", None),
        ],
    )
    def test_private_verdict_reason_inline_link_label_balanced_destinations(
        self,
        reason: str,
        expected_label: str | None,
    ) -> None:
        # Direct parser contract for balanced / escaped destinations and
        # rejection cases (PRRT_kwDOSJAM6s6ZpLqR), plus reference-style
        # ``[label][ref]`` / ``[label][]`` (PRRT_kwDOSJAM6s6ZpXSL) and
        # shortcut ``[label]`` / ``![label]`` (PRRT_kwDOSJAM6s6Zp8jK).
        assert _verdict_reason_inline_link_label(reason) == expected_label

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
            # Single-emphasis peel must use absorbed-placeholder detection, not
            # only the whole-reason regex (#822 PRRT_kwDOSJAM6s6ZoGYD).
            (
                "AWF-VERDICT: FALSE POSITIVE: *<one-sentence justification> "
                "AWF-VERDICT: FIXED: cited*",
                "verdict_placeholder_echo",
            ),
            (
                "AWF-VERDICT: FALSE POSITIVE: _<one-sentence justification> "
                "AWF-VERDICT: DEFER: track later_",
                "verdict_placeholder_echo",
            ),
            (
                "AWF-VERDICT: FIXED: *<one-sentence summary> AWF-VERDICT: FALSE POSITIVE: cite*",
                "fixed_placeholder_echo",
            ),
            (
                "AWF-VERDICT: DEFER: _<what to track> AWF-VERDICT: FALSE POSITIVE: cite_",
                "verdict_placeholder_echo",
            ),
            # Markdown-link peel must use absorbed-placeholder detection
            # (PRRT_kwDOSJAM6s6Zos6S).
            (
                "AWF-VERDICT: FALSE POSITIVE: [<one-sentence justification> "
                "AWF-VERDICT: FIXED: cited](https://example.com)",
                "verdict_placeholder_echo",
            ),
            (
                "AWF-VERDICT: FIXED: [<one-sentence summary> "
                "AWF-VERDICT: FALSE POSITIVE: cite](https://example.com)",
                "fixed_placeholder_echo",
            ),
            # Image wrappers with a leading ``!`` likewise (PRRT_kwDOSJAM6s6Zo-5M).
            (
                "AWF-VERDICT: FALSE POSITIVE: ![<one-sentence justification> "
                "AWF-VERDICT: FIXED: cited](https://example.com)",
                "verdict_placeholder_echo",
            ),
            (
                "AWF-VERDICT: FIXED: ![<one-sentence summary> "
                "AWF-VERDICT: FALSE POSITIVE: cite](https://example.com)",
                "fixed_placeholder_echo",
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
        # Emphasis wrappers around the absorbed form must still peel and fail
        # closed (PRRT_kwDOSJAM6s6ZoGYD). Markdown link / image labels likewise
        # (PRRT_kwDOSJAM6s6Zos6S, PRRT_kwDOSJAM6s6Zo-5M).
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
    def test_private_awf_deeply_nested_list_prefix_attempt_strip_stays_linear(
        self,
    ) -> None:
        # Per-iteration re.sub peels are quadratic on deep nested list prefixes
        # and can stall the monitor event loop before output-size limits apply
        # (PRRT_kwDOSJAM6s6Zq2nB). Tens of thousands of ``- `` layers must still
        # classify as an attempt without approaching the default test timeout.
        nested = ("- " * 20_000) + "AWF-VERDICT: SHIPPED: done"
        result = _parse_verdict_result(f"AWF-VERDICT: FALSE POSITIVE: rationale\n{nested}")

        assert result.verdict == "needs_human"
        assert result.reason == "garbled_verdict_marker"

    @pytest.mark.unit
    def test_private_linear_markdown_attempt_prefix_strip_edges(self) -> None:
        # Direct contract for the O(n) attempt-prefix peel
        # (PRRT_kwDOSJAM6s6Zq2nB): interleaved nests, greedy blockquotes, and
        # prose/emphasis/heading wrappers strip without a per-layer rebuild.
        assert _strip_markdown_attempt_prefixes("- > - AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert _strip_markdown_attempt_prefixes("> - > AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert _strip_markdown_attempt_prefixes("- [ ] AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert (
            _strip_markdown_attempt_prefixes("Final answer: AWF-VERDICT: X: y")
            == "AWF-VERDICT: X: y"
        )
        assert _strip_markdown_attempt_prefixes("Verdict: AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert _strip_markdown_attempt_prefixes("**AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert _strip_markdown_attempt_prefixes("### AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert _strip_markdown_attempt_prefixes("plain") == "plain"
        assert _strip_markdown_attempt_prefixes(("> " * 500) + "AWF-VERDICT: X: y") == (
            "AWF-VERDICT: X: y"
        )
        # Single-prefix helpers remain for one-shot callers / parity with the
        # cursor peel bodies.
        assert _strip_markdown_list_prefix("- AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert _strip_markdown_blockquote_prefix("> AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert _strip_markdown_task_list_checkbox("[ ] AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert (
            _strip_final_answer_attempt_prefix("Final answer: AWF-VERDICT: X: y")
            == "AWF-VERDICT: X: y"
        )
        assert (
            _strip_verdict_result_label_attempt_prefix("Result: AWF-VERDICT: X: y")
            == "AWF-VERDICT: X: y"
        )
        assert _strip_markdown_emphasis_prefix("**AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"
        assert _strip_markdown_heading_prefix("### AWF-VERDICT: X: y") == "AWF-VERDICT: X: y"

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
        ("stdout", "expected_verdict", "expected_reason"),
        [
            (
                "**AWF-VERDICT: FALSE POSITIVE:** Review 4990956104 is Codex boilerplate",
                "false_positive",
                "Review 4990956104 is Codex boilerplate",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: stale wrapper__",
                "false_positive",
                "stale wrapper",
            ),
            ("*AWF-VERDICT: NEEDS_HUMAN: unsure*", "needs_human", "unsure"),
            ("_AWF-VERDICT: DEFER:_ track separately", "defer", "track separately"),
            ("***AWF-VERDICT: FIXED: committed repair***", "fix_committed", "committed repair"),
            (
                "___AWF-VERDICT: NEEDS_HUMAN: maintainer decision___",
                "needs_human",
                "maintainer decision",
            ),
            (
                "`**AWF-VERDICT: FALSE POSITIVE:** code-formatted wrapper`",
                "false_positive",
                "code-formatted wrapper",
            ),
            # Prefix wrap + balanced trailing reason emphasis must resolve
            # (PRRT_kwDOSJAM6s6bRROQ).
            (
                "**AWF-VERDICT: FALSE POSITIVE:** This is **expected**",
                "false_positive",
                "This is **expected**",
            ),
            (
                "*AWF-VERDICT: FIXED:* done with *emphasis*",
                "fix_committed",
                "done with *emphasis*",
            ),
            # Inline code-span stars must not steal the whole-line closer
            # (PRRT_kwDOSJAM6s6bShql).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see `**`**",
                "false_positive",
                "see `**`",
            ),
            # Stars inside inline HTML attribute values are literal tag content
            # and must not steal the whole-line closer (PRRT_kwDOSJAM6s6bTBv6).
            (
                '**AWF-VERDICT: FALSE POSITIVE: see <span title="**">ok</span>**',
                "false_positive",
                'see <span title="**">ok</span>',
            ),
            # Stars inside Markdown link destinations are literal URL content
            # and must not steal the whole-line closer (PRRT_kwDOSJAM6s6bTLZq).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link](foo**bar)**",
                "false_positive",
                "see [link](foo**bar)",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see [link](foo*bar)*",
                "false_positive",
                "see [link](foo*bar)",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see [link](foo__bar)__",
                "false_positive",
                "see [link](foo__bar)",
            ),
            # Angle-bracket destinations may contain spaces; markers stay literal
            # (PRRT_kwDOSJAM6s6bTgB6).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link](<foo **bar>)**",
                "false_positive",
                "see [link](<foo **bar>)",
            ),
            (
                '**AWF-VERDICT: FALSE POSITIVE: see [link](url "a ** b")**',
                "false_positive",
                'see [link](url "a ** b")',
            ),
            (
                '**AWF-VERDICT: FALSE POSITIVE: see [link](<url> "a ** b")**',
                "false_positive",
                'see [link](<url> "a ** b")',
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link](url 'a * b')**",
                "false_positive",
                "see [link](url 'a * b')",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link](url (a ** b))**",
                "false_positive",
                "see [link](url (a ** b))",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see [link](<url> (a ** b))**",
                "false_positive",
                "see [link](<url> (a ** b))",
            ),
            # Escaped nested ``\(`` in a parenthesized title is literal; markers
            # stay opaque (PRRT_kwDOSJAM6s6bUOZ9).
            (
                r"**AWF-VERDICT: FALSE POSITIVE: see [link](url (a\(** b)))**",
                "false_positive",
                r"see [link](url (a\(** b)))",
            ),
            # URI/email autolink interiors are opaque (PRRT_kwDOSJAM6s6bTgB-).
            (
                "**AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a**b>**",
                "false_positive",
                "see <https://example.test/a**b>",
            ),
            (
                "*AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a*b>*",
                "false_positive",
                "see <https://example.test/a*b>",
            ),
            (
                "__AWF-VERDICT: FALSE POSITIVE: see <https://example.test/a__b>__",
                "false_positive",
                "see <https://example.test/a__b>",
            ),
            (
                "**AWF-VERDICT: FALSE POSITIVE: see <user**name@example.com>**",
                "false_positive",
                "see <user**name@example.com>",
            ),
        ],
    )
    def test_private_awf_verdict_accepts_balanced_top_level_emphasis(
        self,
        stdout: str,
        expected_verdict: str,
        expected_reason: str,
    ) -> None:
        result = _parse_verdict_result(stdout)

        assert result.verdict == expected_verdict
        assert result.reason == expected_reason
