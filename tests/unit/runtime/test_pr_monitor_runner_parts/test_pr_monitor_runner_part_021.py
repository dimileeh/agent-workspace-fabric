"""Unit tests for verdict parsing helpers (part 021)."""

from __future__ import annotations

import html

import pytest

from awf.runtime.pr_monitor_runner.helpers import (
    _parse_verdict_result,
)
from awf.runtime.pr_monitor_runner.helpers_verdict import (
    _aggressively_peel_verdict_reason_wrappers,
    _emphasis_run_pair_blocked_by_multiple_of_three,
    _html_wrapper_close_suffix_start,
    _markdown_emphasis_run_can_close,
    _markdown_emphasis_run_can_open,
    _normalize_markdown_emphasized_verdict_line,
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
    _verdict_reason_trailing_emphasis_is_balanced,
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
            # Mid-reason opener + trailing closer is not a whole-line wrap
            # (PRRT_kwDOSJAM6s6bRrWv).
            "**AWF-VERDICT: FALSE POSITIVE: rationale **unclosed**",
            "*AWF-VERDICT: FALSE POSITIVE: rationale *unclosed*",
            "__AWF-VERDICT: FALSE POSITIVE: rationale __unclosed__",
            "_AWF-VERDICT: FALSE POSITIVE: rationale _unclosed_",
            # Partial longer-run match steals the trailing closer
            # (PRRT_kwDOSJAM6s6bR2FM).
            "**AWF-VERDICT: FALSE POSITIVE: ***lead* rest**",
            "*AWF-VERDICT: FALSE POSITIVE: **lead* rest*",
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
            # Mid-reason same-delimiter opener steals the trailing closer; the
            # line-leading wrapper stays unbalanced (PRRT_kwDOSJAM6s6bRrWv).
            "**AWF-VERDICT: FALSE POSITIVE: rationale **unclosed**",
            "*AWF-VERDICT: FALSE POSITIVE: rationale *unclosed*",
            "__AWF-VERDICT: FALSE POSITIVE: rationale __unclosed__",
            "_AWF-VERDICT: FALSE POSITIVE: rationale _unclosed_",
            # Longer mid-run partially pairs with a short closer then the
            # trailing wrapper-length closer (PRRT_kwDOSJAM6s6bR2FM).
            "**AWF-VERDICT: FALSE POSITIVE: ***lead* rest**",
            "*AWF-VERDICT: FALSE POSITIVE: **lead* rest*",
            "__AWF-VERDICT: FALSE POSITIVE: ___lead_ rest__",
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
            ("**lead* rest*", "*", True),
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
            # Multiple-of-3 rule blocks pairing a length-1 opener run against a
            # length-2 closer that can also open (``*foo**``).
            ("*foo**", "**", False),
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
    def test_private_markdown_emphasis_run_flanking_helpers(self) -> None:
        # Defensive bounds and content mismatches for the maximal-run helpers.
        assert _markdown_emphasis_run_can_close("", 0, 1, "*") is False
        assert _markdown_emphasis_run_can_close("**", -1, 2, "*") is False
        assert _markdown_emphasis_run_can_close("ab", 0, 2, "*") is False
        assert _markdown_emphasis_run_can_open("**", 0, 0, "*") is False
        assert _markdown_emphasis_run_can_open("x*", 1, 1, "*") is True  # EOF is left-flanking
        assert _markdown_emphasis_run_can_open("* ", 0, 1, "*") is False  # followed by space
        assert _markdown_emphasis_run_can_close(" *", 1, 1, "*") is False  # preceded by space
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
        assert _emphasis_run_pair_blocked_by_multiple_of_three(1, 2, True) is True
        assert _emphasis_run_pair_blocked_by_multiple_of_three(1, 2, False) is False
        assert _emphasis_run_pair_blocked_by_multiple_of_three(3, 3, True) is False
        # Escaped marker characters are never flanking runs.
        assert _markdown_emphasis_run_can_close(r"a\*", 2, 1, "*") is False
        assert _markdown_emphasis_run_can_open(r"\*", 1, 1, "*") is False

    @pytest.mark.unit
    @pytest.mark.parametrize(
        "line",
        [
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
