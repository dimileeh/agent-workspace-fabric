"""Direct unit tests for pre-push FIXED callee/definition ancestry helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry as ancestry
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry_callees as callees


@pytest.mark.unit
def test_diff_hunk_near_anchor_related_accepts_guard_window_before_line() -> None:
    # Pure insert several lines before the review anchor (not line / line-1),
    # but only when the insert shares the review line's enclosing definition.
    same_fn = (
        "def reviewed():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
        "    f = 6\n"
        "    do_work()\n"
    )
    assert (
        ancestry._diff_hunk_near_anchor_related("@@ -3,0 +4,2 @@\n", 8, file_text=same_fn) is True
    )
    # Modifications near the anchor are not proximity evidence (call-site link only).
    assert (
        ancestry._diff_hunk_near_anchor_related("@@ -4,1 +4,1 @@\n", 8, file_text=same_fn) is False
    )


@pytest.mark.unit
def test_diff_hunk_near_anchor_related_rejects_other_enclosing_def() -> None:
    """Unrelated insert in a neighboring function must not count as near-anchor evidence."""
    text = (
        "def other():\n"
        "    x = 1\n"
        "    y = 2\n"
        "\n"
        "def reviewed():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    do_work()\n"
    )
    # Pure insert after line 2 inside other() — within the 12-line window of do_work.
    assert ancestry._diff_hunk_near_anchor_related("@@ -2,0 +3,1 @@\n", 8, file_text=text) is False
    assert ancestry._diff_hunk_near_anchor_related("@@ -3,0 +4,2 @@\n", 8, file_text="") is False


@pytest.mark.unit
def test_diff_hunk_near_anchor_related_rejects_distant_and_after() -> None:
    text = "def reviewed():\n    do_work()\n"
    assert ancestry._diff_hunk_near_anchor_related("@@ -1,0 +2,1 @@\n", 30, file_text=text) is False
    assert (
        ancestry._diff_hunk_near_anchor_related("@@ -20,0 +21,1 @@\n", 8, file_text=text) is False
    )
    assert ancestry._diff_hunk_near_anchor_related("@@ -3,0 +4,2 @@\n", 0, file_text=text) is False


@pytest.mark.unit
def test_callee_names_from_anchor_line_extracts_calls_and_filters_keywords() -> None:
    assert callees._callee_names_from_anchor_line("    return helper(x)") == frozenset({"helper"})
    assert callees._callee_names_from_anchor_line("if ready(x) and helper():") == frozenset(
        {"ready", "helper"}
    )
    assert callees._callee_names_from_anchor_line("    return self.helper()") == frozenset(
        {"helper"}
    )
    assert callees._callee_names_from_anchor_line("    if (x):") == frozenset()
    assert callees._callee_names_from_anchor_line("    return None") == frozenset()
    assert callees._callee_names_from_anchor_line("") == frozenset()
    assert callees._callee_names_from_anchor_line("def reviewed():") == frozenset()
    assert callees._callee_names_from_anchor_line("def reviewed(): return helper()") == frozenset(
        {"helper"}
    )


@pytest.mark.unit
def test_callee_refs_capture_optional_qualifier() -> None:
    assert callees._callee_refs_from_anchor_line("    return self.helper()") == frozenset(
        {("self", "helper")}
    )
    assert callees._callee_refs_from_anchor_line("    return helper()") == frozenset(
        {(None, "helper")}
    )


@pytest.mark.unit
def test_callee_refs_ignore_calls_inside_comments_and_string_literals() -> None:
    # Call-shaped text in comments/literals must not become FIXED callee evidence.
    assert callees._callee_refs_from_anchor_line("    # TODO: helper()") == frozenset()
    assert callees._callee_refs_from_anchor_line("    x = 1  # helper()") == frozenset()
    assert callees._callee_refs_from_anchor_line("    // helper()") == frozenset()
    assert callees._callee_refs_from_anchor_line('    message = "helper()"') == frozenset()
    assert callees._callee_refs_from_anchor_line("    message = 'helper()'") == frozenset()
    assert callees._callee_refs_from_anchor_line("    message = `helper()`") == frozenset()
    # Real call kept; literal decoy ignored.
    assert callees._callee_refs_from_anchor_line(
        '    return real_call("helper()")  # other()'
    ) == frozenset({(None, "real_call")})
    # No-space Python comments are still comments (not JS private fields).
    assert (
        callees._callee_refs_from_anchor_line("    #TODO helper()", path="src/mod.py")
        == frozenset()
    )
    assert callees._callee_refs_from_anchor_line("    #helper()") == frozenset()
    # Without a JS/TS path, fail closed: do not treat #ident as executable.
    assert callees._callee_refs_from_anchor_line("    return this.#helper()") == frozenset()
    # JS private-field call is code only when the reviewed path is JS/TS.
    assert callees._callee_refs_from_anchor_line(
        "    return this.#helper()", path="src/mod.ts"
    ) == frozenset({(None, "helper")})
    assert callees._callee_refs_from_anchor_line(
        "    return this.#helper()", path="src/mod.d.ts"
    ) == frozenset({(None, "helper")})
    assert callees._path_allows_js_private_fields(None) is False
    assert callees._path_allows_js_private_fields("src\\mod.jsx") is True


@pytest.mark.unit
def test_callee_refs_retain_calls_inside_fstring_and_template_interpolations() -> None:
    """Executable interpolations must remain callee evidence; literal text must not."""
    assert callees._callee_refs_from_anchor_line('    message = f"{helper()}"') == frozenset(
        {(None, "helper")}
    )
    assert callees._callee_refs_from_anchor_line("    message = f'{helper()}'") == frozenset(
        {(None, "helper")}
    )
    assert callees._callee_refs_from_anchor_line('    message = rf"{helper()}"') == frozenset(
        {(None, "helper")}
    )
    assert callees._callee_refs_from_anchor_line('    message = f"""{helper()}"""') == frozenset(
        {(None, "helper")}
    )
    # Literal call-shaped text in an f-string (not inside ``{...}``) is not a callee.
    assert callees._callee_refs_from_anchor_line('    message = f"helper()"') == frozenset()
    # Escaped braces are literal text, not interpolations.
    assert callees._callee_refs_from_anchor_line('    message = f"{{helper()}}"') == frozenset()
    assert callees._callee_refs_from_anchor_line("    message = `x ${helper()} y`") == frozenset(
        {(None, "helper")}
    )
    assert callees._callee_refs_from_anchor_line("    message = `helper()`") == frozenset()
    # Nested braces inside an interpolation still expose the callee.
    assert callees._callee_refs_from_anchor_line(
        '    message = f"{helper({"a": 1})}"'
    ) == frozenset({(None, "helper")})
    assert callees._callee_refs_from_anchor_line(
        "    message = `x ${helper({a: 1})} y`"
    ) == frozenset({(None, "helper")})


@pytest.mark.unit
def test_callee_refs_ignore_triple_quoted_and_escaped_string_calls() -> None:
    """Triple-quoted / escaped literals must not yield callee FIXED refs."""
    assert callees._callee_refs_from_anchor_line('    x = """helper()"""') == frozenset()
    assert callees._callee_refs_from_anchor_line("    x = '''helper()'''") == frozenset()
    # Escaped quote inside a string must not end the literal early.
    assert callees._callee_refs_from_anchor_line(r'    x = "say \"helper()\""') == frozenset()
    assert callees._callee_refs_from_anchor_line(
        r'    return real("say \"helper()\"")'
    ) == frozenset({(None, "real")})
    # Empty line / empty mask input stays empty.
    assert callees._mask_comments_and_string_literals_for_callee_scan("") == ""
    assert callees._callee_refs_from_anchor_line("") == frozenset()


@pytest.mark.unit
def test_callee_refs_fail_closed_on_definition_line_without_colon() -> None:
    """JS/TS function heads without ``:`` must not scan the brace body as callees."""
    assert callees._callee_refs_from_anchor_line("function reviewed() { return helper(); }") == (
        frozenset()
    )
    assert callees._callee_refs_from_anchor_line("def reviewed()") == frozenset()


@pytest.mark.unit
def test_callee_refs_clear_blocklisted_qualifier() -> None:
    """Keyword-looking receivers are dropped so the bare name remains."""
    assert callees._callee_refs_from_anchor_line("    return from.send()") == frozenset(
        {(None, "send")}
    )


@pytest.mark.unit
def test_diff_text_changes_definition_names_rejects_empty_name_set() -> None:
    assert callees._diff_text_changes_definition_names("+def helper():\n", frozenset()) is False


@pytest.mark.unit
def test_diff_text_changes_definition_names_detects_def_forms() -> None:
    diff = "@@ -1,1 +1,1 @@\n-def helper():\n+def helper():\n"
    # Signature-adjacent body change still names the def on a +/- line when present.
    body_only = "@@ -2,1 +2,1 @@\n-    return 1\n+    return 2\n"
    assert callees._diff_text_changes_definition_names(diff, frozenset({"helper"})) is True
    assert callees._diff_text_changes_definition_names(body_only, frozenset({"helper"})) is False
    assert callees._diff_text_changes_definition_names(diff, frozenset({"other"})) is False
    assert callees._diff_text_changes_definition_names("", frozenset({"helper"})) is False
    arrow = "@@ -1,1 +1,1 @@\n-const helper = () => {\n+const helper = () => {\n"
    assert callees._diff_text_changes_definition_names(arrow, frozenset({"helper"})) is True


@pytest.mark.unit
def test_enclosing_definition_name_finds_nearest_def_above() -> None:
    text = "def helper():\n    return 1\n\ndef reviewed():\n    return helper()\n"
    assert callees._enclosing_definition_name(text, 2) == "helper"
    assert callees._enclosing_definition_name(text, 5) == "reviewed"
    assert callees._enclosing_definition_name(text, 0) is None
    assert callees._enclosing_definition_name("", 1) is None


@pytest.mark.unit
def test_enclosing_definition_name_recognizes_arrow_assignment() -> None:
    """JS/TS ``const helper = () =>`` bodies must count as enclosing definitions."""
    text = (
        "const helper = () => {\n  return 1;\n};\n\nfunction reviewed() {\n  return helper();\n}\n"
    )
    assert callees._enclosing_definition_name(text, 2) == "helper"
    assert callees._enclosing_definition_name(text, 6) == "reviewed"
    assert callees._enclosing_definition_identity(text, 2) == ("helper", 1)


@pytest.mark.unit
def test_resolve_callee_definition_span_includes_arrow_assignment_body() -> None:
    text = (
        "const helper = () => {\n  return 1;\n};\n\nfunction reviewed() {\n  return helper();\n}\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=6, qualifier=None, name="helper"
    ) == (1, 4)


@pytest.mark.unit
def test_resolve_callee_definition_span_prefers_in_scope_target() -> None:
    text = (
        "def helper():\n"
        "    return 99\n"
        "\n"
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
    )
    # Attribute call resolves to the method inside Foo, not the module helper.
    assert callees._resolve_callee_definition_span(
        text, call_line=9, qualifier="self", name="helper"
    ) == (5, 7)
    # Bare call below would resolve to nearest preceding module/class-visible def.
    bare = "def helper():\n    return 1\n\ndef reviewed():\n    return helper()\n"
    assert callees._resolve_callee_definition_span(
        bare, call_line=5, qualifier=None, name="helper"
    ) == (1, 3)


@pytest.mark.unit
def test_resolve_callee_definition_span_self_method_declared_after_call() -> None:
    """Same-class ``self.helper()`` must resolve even when ``helper`` is declared later."""
    text = (
        "class Foo:\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
        "\n"
        "    def helper(self):\n"
        "        return 1\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=3, qualifier="self", name="helper"
    ) == (5, 6)


@pytest.mark.unit
def test_resolve_callee_definition_span_self_skips_nested_class_method() -> None:
    """``self.helper()`` must not bind to a same-named method on a nested class."""
    only_nested = (
        "class Outer:\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
        "\n"
        "    class Inner:\n"
        "        def helper(self):\n"
        "            return 1\n"
    )
    assert (
        callees._resolve_callee_definition_span(
            only_nested, call_line=3, qualifier="self", name="helper"
        )
        is None
    )
    with_own = (
        "class Outer:\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
        "\n"
        "    def helper(self):\n"
        "        return 2\n"
        "\n"
        "    class Inner:\n"
        "        def helper(self):\n"
        "            return 1\n"
    )
    assert callees._resolve_callee_definition_span(
        with_own, call_line=3, qualifier="self", name="helper"
    ) == (5, 7)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_skips_class_method() -> None:
    """Bare ``helper()`` must not bind to a nearer same-named class method."""
    text = (
        "def helper():\n"
        "    return 99\n"
        "\n"
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(self):\n"
        "        return helper()\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=9, qualifier=None, name="helper"
    ) == (1, 3)
    only_method = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(self):\n"
        "        return helper()\n"
    )
    assert (
        callees._resolve_callee_definition_span(
            only_method, call_line=6, qualifier=None, name="helper"
        )
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_later_toplevel_helper() -> None:
    """Module-level helpers declared after the call site remain in scope for bare calls."""
    text = "def reviewed():\n    return helper()\n\ndef helper():\n    return 1\n"
    assert callees._resolve_callee_definition_span(
        text, call_line=2, qualifier=None, name="helper"
    ) == (4, 5)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_prefers_nested_helper() -> None:
    """Nested helpers defined before the call beat same-named top-level defs."""
    text = (
        "def helper():\n"
        "    return 99\n"
        "\n"
        "def reviewed():\n"
        "    def helper():\n"
        "        return 1\n"
        "    return helper()\n"
    )
    # Nested helper ends at its lexical body, not the sibling ``return helper()``.
    assert callees._resolve_callee_definition_span(
        text, call_line=7, qualifier=None, name="helper"
    ) == (5, 6)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_enclosing_function_helper() -> None:
    """Bare calls must resolve helpers defined in an enclosing function scope."""
    text = (
        "def outer():\n"
        "    def helper():\n"
        "        return 1\n"
        "    def reviewed():\n"
        "        return helper()\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=5, qualifier=None, name="helper"
    ) == (2, 3)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_prefers_inner_enclosing_helper() -> None:
    """Innermost enclosing helper wins over an outer same-named helper."""
    text = (
        "def outer():\n"
        "    def helper():\n"
        "        return 99\n"
        "    def mid():\n"
        "        def helper():\n"
        "            return 1\n"
        "        def reviewed():\n"
        "            return helper()\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=8, qualifier=None, name="helper"
    ) == (5, 6)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_skips_sibling_nested_helper() -> None:
    """Helpers local to a sibling nested def are not in scope for bare calls."""
    text = (
        "def outer():\n"
        "    def sibling():\n"
        "        def helper():\n"
        "            return 1\n"
        "    def reviewed():\n"
        "        return helper()\n"
        "\n"
        "def helper():\n"
        "    return 99\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=6, qualifier=None, name="helper"
    ) == (8, 9)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_indented_module_helper() -> None:
    """Module-scope helpers under ``if`` (indent > 0) remain bare-callable."""
    text = (
        "if True:\n"
        "    def helper():\n"
        "        return 1\n"
        "    def reviewed():\n"
        "        return helper()\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=5, qualifier=None, name="helper"
    ) == (2, 3)
    if_body = "if True:\n    def helper():\n        return 1\n    x = helper()\n"
    assert callees._resolve_callee_definition_span(
        if_body, call_line=4, qualifier=None, name="helper"
    ) == (2, 3)


@pytest.mark.unit
def test_iter_definition_spans_stops_at_module_level_assignment_dedent() -> None:
    """Ordinary dedents (not only the next def) end a definition span."""
    text = "def helper():\n    return 1\n\nX = 1\n\ndef reviewed():\n    return helper()\n"
    spans = {
        name: (start, end) for name, start, end, _indent in callees._iter_definition_spans(text)
    }
    assert spans["helper"] == (1, 3)
    assert spans["reviewed"] == (6, 7)
    helper_span = callees._resolve_callee_definition_span(
        text, call_line=7, qualifier=None, name="helper"
    )
    assert helper_span == (1, 3)
    # Unrelated module assignment must not count as FIXED callee-body evidence.
    assert (
        ancestry._diff_hunk_overlaps_line_span("@@ -4,1 +4,1 @@\n-X = 1\n+X = 2\n", *helper_span)
        is False
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_rejects_unsupported_qualifier() -> None:
    """Non-self/cls receivers must fail closed, not bind an unrelated bare def."""
    text = "def send():\n    return 99\n\ndef reviewed():\n    return client.send()\n"
    assert callees._callee_refs_from_anchor_line("    return client.send()") == frozenset(
        {("client", "send")}
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=5, qualifier="client", name="send")
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_self_outside_class_fails_closed() -> None:
    """``self.helper()`` with no enclosing class must not bind a module helper."""
    text = "def helper():\n    return 1\n\ndef reviewed():\n    return self.helper()\n"
    assert (
        callees._resolve_callee_definition_span(text, call_line=5, qualifier="self", name="helper")
        is None
    )
    assert (
        callees._resolve_callee_definition_span("", call_line=1, qualifier=None, name="x") is None
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=0, qualifier=None, name="helper")
        is None
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=5, qualifier=None, name="") is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_cls_method_in_class() -> None:
    """``cls.helper()`` resolves to the same-class method like ``self``."""
    text = (
        "class Foo:\n"
        "    def helper(cls):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(cls):\n"
        "        return cls.helper()\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=6, qualifier="cls", name="helper"
    ) == (2, 4)


@pytest.mark.unit
def test_enclosing_class_span_ends_at_sibling_and_misses_without_class() -> None:
    """Class spans stop at the next same-indent definition; no class → None."""
    text = "class Foo:\n    def helper(self):\n        return 1\n\ndef reviewed():\n    return 2\n"
    assert callees._enclosing_class_span(text, 3) == (1, 4)
    # Call site after the class ends must not inherit that preceding class span.
    assert callees._enclosing_class_span(text, 6) is None
    # No class above the call site at all.
    assert callees._enclosing_class_span("def reviewed():\n    return 1\n", 2) is None
    assert callees._enclosing_class_span(text, 0) is None
    assert callees._enclosing_class_span("", 1) is None


@pytest.mark.unit
def test_enclosing_class_span_ends_at_ordinary_dedent_after_local_class() -> None:
    """A local class ends at any same-indent nonblank line, not only the next def/class."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 2\n"
        "\n"
        "    def reviewed(self):\n"
        "        class Local:\n"
        "            def helper(self):\n"
        "                return 1\n"
        "        return self.helper()\n"
    )
    # ``return self.helper()`` is after Local's body; Local must not enclose it.
    assert callees._enclosing_class_span(text, 9) == (1, 9)
    assert callees._resolve_callee_definition_span(
        text, call_line=9, qualifier="self", name="helper"
    ) == (2, 4)
    # No outer class: ordinary dedent after a function-local class must fail closed.
    nested_only = (
        "def reviewed(self):\n"
        "    class Local:\n"
        "        def helper(self):\n"
        "            return 1\n"
        "    return self.helper()\n"
    )
    assert callees._enclosing_class_span(nested_only, 5) is None
    assert (
        callees._resolve_callee_definition_span(
            nested_only, call_line=5, qualifier="self", name="helper"
        )
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_self_after_preceding_class_fails_closed() -> None:
    """Module-level ``self.helper()`` must not bind a method on a preceding class."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "def reviewed(self):\n"
        "    return self.helper()\n"
    )
    assert callees._enclosing_class_span(text, 6) is None
    assert (
        callees._resolve_callee_definition_span(text, call_line=6, qualifier="self", name="helper")
        is None
    )


@pytest.mark.unit
def test_containing_definition_spans_empty_inputs() -> None:
    assert callees._containing_definition_spans("", 1) == []
    assert callees._containing_definition_spans("def x():\n    return 1\n", 0) == []
    # Line outside every definition body yields an empty containing set.
    text = "def helper():\n    return 1\n\nx = 1\n"
    assert callees._containing_definition_spans(text, 4) == []
    # Same-indent block closer lies in the span range but is not a body line.
    arrow = "const helper = () => {\n  return 1;\n};\n"
    assert callees._containing_definition_spans(arrow, 3) == []


@pytest.mark.unit
def test_resolve_callee_definition_span_unknown_name_fails_closed() -> None:
    text = "def helper():\n    return 1\n\ndef reviewed():\n    return helper()\n"
    assert (
        callees._resolve_callee_definition_span(text, call_line=5, qualifier=None, name="missing")
        is None
    )


@pytest.mark.unit
def test_mask_unclosed_triple_and_single_quotes_blanks_remainder() -> None:
    """Unclosed quotes blank through end-of-line so decoy calls stay non-callees."""
    assert "helper" not in callees._mask_comments_and_string_literals_for_callee_scan(
        '    x = """helper()'
    ).replace(" ", "")
    assert callees._callee_refs_from_anchor_line('    x = """helper()') == frozenset()
    assert callees._callee_refs_from_anchor_line("    x = 'helper()") == frozenset()


@pytest.mark.unit
def test_diff_hunk_overlaps_definition_span() -> None:
    assert ancestry._diff_hunk_overlaps_line_span("@@ -2,1 +2,1 @@\n", 1, 3) is True
    assert ancestry._diff_hunk_overlaps_line_span("@@ -10,1 +10,1 @@\n", 1, 3) is False
    assert ancestry._diff_hunk_overlaps_line_span("@@ -2,0 +3,1 @@\n", 1, 3) is True
    # Invalid / inverted spans fail closed.
    assert ancestry._diff_hunk_overlaps_line_span("@@ -2,1 +2,1 @@\n", 0, 3) is False
    assert ancestry._diff_hunk_overlaps_line_span("@@ -2,1 +2,1 @@\n", 3, 1) is False
    # Pure insert outside the span is not overlap.
    assert ancestry._diff_hunk_overlaps_line_span("@@ -10,0 +11,1 @@\n", 1, 3) is False


@pytest.mark.unit
def test_diff_hunk_near_anchor_related_accepts_module_level_insert() -> None:
    """Module-level review lines accept module-level pure inserts in the window."""
    text = "a = 1\nb = 2\nc = 3\ndo_work()\n"
    assert ancestry._diff_hunk_near_anchor_related("@@ -2,0 +3,1 @@\n", 4, file_text=text) is True


@pytest.mark.unit
def test_diff_hunk_related_line_evidence_combines_exact_and_near() -> None:
    same_fn = (
        "def reviewed():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
        "    f = 6\n"
        "    do_work()\n"
    )
    exact = "@@ -8,1 +8,1 @@\n-    do_work()\n+    do_work(1)\n"
    near = "@@ -3,0 +4,2 @@\n+    if not ready:\n+        return\n"
    distant = "@@ -40,1 +40,1 @@\n-    other()\n+    other(1)\n"
    assert ancestry._diff_hunk_related_line_evidence(exact, 8, file_text=same_fn) is True
    assert ancestry._diff_hunk_related_line_evidence(near, 8, file_text=same_fn) is True
    assert ancestry._diff_hunk_related_line_evidence(distant, 8, file_text=same_fn) is False
    assert ancestry._diff_hunk_related_line_evidence(near, 0, file_text=same_fn) is False


@pytest.mark.unit
async def test_path_text_at_ref_returns_none_when_show_fails(tmp_path: Path) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="", stderr="missing")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._path_text_at_ref(
            runner, worktree_path=tmp_path, ref="HEAD", path="src/x.py"
        )
        is None
    )


@pytest.mark.unit
async def test_diff_changes_referenced_definition_fail_closed_paths(tmp_path: Path) -> None:
    """Line/anchor/file/ref failures must not invent call-site→definition evidence."""
    missing_line = FakeCommandRunner()
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=missing_line))
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=0,
            diff_text="@@ -1,1 +1,1 @@\n",
        )
        is False
    )

    no_anchor = FakeCommandRunner()
    no_anchor.queue_result(returncode=1, stdout="", stderr="missing")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=no_anchor))
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=2,
            diff_text="@@ -1,1 +1,1 @@\n",
        )
        is False
    )

    # Anchor has no call refs.
    no_refs = FakeCommandRunner()
    no_refs.queue_result(returncode=0, stdout="return None\n", stderr="")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=no_refs))
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=1,
            diff_text="@@ -1,1 +1,1 @@\n",
        )
        is False
    )

    # Fetch file text when omitted; empty body fails closed.
    empty_file = FakeCommandRunner()
    empty_file.queue_result(returncode=0, stdout="    return helper()\n", stderr="")
    empty_file.queue_result(returncode=0, stdout="", stderr="")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=empty_file))
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=1,
            diff_text="@@ -1,1 +1,1 @@\n",
            file_text=None,
        )
        is False
    )


@pytest.mark.unit
async def test_diff_changes_referenced_definition_accepts_overlapping_callee_span(
    tmp_path: Path,
) -> None:
    """Call-site anchor + overlapping callee-body hunk counts as related evidence."""
    file_text = "def helper():\n    return 1\n\ndef reviewed():\n    return helper()\n"
    cmd = FakeCommandRunner()
    # _path_line_at_ref shows full file and picks line 5.
    cmd.queue_result(returncode=0, stdout=file_text, stderr="")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=5,
            diff_text="@@ -2,1 +2,1 @@\n-    return 1\n+    return 2\n",
            file_text=file_text,
        )
        is True
    )
    # Resolved span with no overlapping hunk fails closed.
    cmd2 = FakeCommandRunner()
    cmd2.queue_result(returncode=0, stdout=file_text, stderr="")
    runner2 = SimpleNamespace(_deps=SimpleNamespace(runner=cmd2))
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner2,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=5,
            diff_text="@@ -40,1 +40,1 @@\n-    other\n+    other2\n",
            file_text=file_text,
        )
        is False
    )
    # Call ref present but no in-scope definition → skip and fail closed.
    unresolved = "def reviewed():\n    return missing()\n"
    cmd3 = FakeCommandRunner()
    cmd3.queue_result(returncode=0, stdout=unresolved, stderr="")
    runner3 = SimpleNamespace(_deps=SimpleNamespace(runner=cmd3))
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner3,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=2,
            diff_text="@@ -1,1 +1,1 @@\n",
            file_text=unresolved,
        )
        is False
    )


@pytest.mark.unit
async def test_diff_provides_related_line_evidence_near_anchor_and_definition(
    tmp_path: Path,
) -> None:
    same_fn = (
        "def reviewed():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
        "    f = 6\n"
        "    do_work()\n"
    )
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=same_fn, stderr="")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._diff_provides_related_line_evidence(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=8,
            diff_text="@@ -3,0 +4,2 @@\n+    if not ready:\n+        return\n",
        )
        is True
    )
    # Exact overlap short-circuits without needing file text.
    assert (
        await ancestry._diff_provides_related_line_evidence(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=8,
            diff_text="@@ -8,1 +8,1 @@\n-    do_work()\n+    do_work(1)\n",
        )
        is True
    )
    # Distant unrelated hunk falls through to call-site→definition (False here).
    cmd2 = FakeCommandRunner()
    cmd2.queue_result(returncode=0, stdout=same_fn, stderr="")
    # Definition lookup also re-reads the anchor line via git show.
    cmd2.queue_result(returncode=0, stdout=same_fn, stderr="")
    runner2 = SimpleNamespace(_deps=SimpleNamespace(runner=cmd2))
    assert (
        await ancestry._diff_provides_related_line_evidence(
            runner2,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=8,
            diff_text="@@ -40,1 +40,1 @@\n-    other()\n+    other(1)\n",
        )
        is False
    )


@pytest.mark.unit
async def test_diff_provides_related_line_evidence_rejects_non_positive_line(
    tmp_path: Path,
) -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._diff_provides_related_line_evidence(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=0,
            diff_text="@@ -1,1 +1,1 @@\n",
        )
        is False
    )
