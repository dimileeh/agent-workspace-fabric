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
def test_diff_hunk_near_anchor_related_rejects_neighboring_unicode_def() -> None:
    """Unicode-named neighboring defs must still be distinct near-anchor scopes."""
    text = "def 甲():\n    x = 1\n    y = 2\n\ndef 乙():\n    a = 1\n    b = 2\n    do_work()\n"
    assert ancestry._diff_hunk_near_anchor_related("@@ -2,0 +3,1 @@\n", 8, file_text=text) is False
    assert callees._enclosing_definition_identity(text, 8) == ("乙", 5)
    assert callees._enclosing_definition_identity(text, 2) == ("甲", 1)


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
def test_callee_refs_treat_dollar_as_js_identifier_char() -> None:
    """``$helper()`` must not strip the leading ``$`` and bind bare ``helper``.

    Python ``\\b`` treats ``$`` as a non-word character, so a naive matcher starts
    after ``$`` and reports ``helper``. Editing an unrelated module-level
    ``helper`` would then satisfy call-site→definition FIXED evidence.
    """
    assert callees._callee_refs_from_anchor_line(
        "    return $helper()", path="src/mod.ts"
    ) == frozenset({(None, "$helper")})
    assert callees._callee_refs_from_anchor_line(
        "    return obj.$fn()", path="src/mod.js"
    ) == frozenset({("obj", "$fn")})
    assert callees._callee_names_from_anchor_line("    return foo$bar()", path="src/mod.ts") == (
        frozenset({"foo$bar"})
    )

    js = (
        "function helper() {\n"
        "  return 99;\n"
        "}\n"
        "\n"
        "const $helper = () => {\n"
        "  return 1;\n"
        "};\n"
        "\n"
        "function reviewed() {\n"
        "  return $helper();\n"
        "}\n"
    )
    refs = callees._callee_refs_from_file_line(js, 10, path="src/mod.ts")
    assert refs == frozenset({(None, "$helper")})
    qualifier, name = next(iter(refs))
    assert callees._resolve_callee_definition_span(
        js, call_line=10, qualifier=qualifier, name=name, path="src/mod.ts"
    ) == (5, 8)
    # Stripping ``$`` would bind the decoy module ``helper`` at lines 1–4.
    assert callees._resolve_callee_definition_span(
        js, call_line=10, qualifier=None, name="helper", path="src/mod.ts"
    ) == (1, 4)


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
def test_callee_refs_ignore_calls_inside_js_regex_literals() -> None:
    """JS/TS regex bodies must not become FIXED callee evidence."""
    assert (
        callees._callee_refs_from_anchor_line("    const pattern = /helper()/;", path="src/mod.ts")
        == frozenset()
    )
    assert (
        callees._callee_refs_from_anchor_line("    const pattern = /helper()/g;", path="src/mod.js")
        == frozenset()
    )
    # Escaped slash and character-class slash stay inside the literal.
    assert (
        callees._callee_refs_from_anchor_line(
            r"    const pattern = /help\/er()/;", path="src/mod.tsx"
        )
        == frozenset()
    )
    assert (
        callees._callee_refs_from_anchor_line(
            "    const pattern = /pre[x/y]helper()/;", path="src/mod.ts"
        )
        == frozenset()
    )
    # Real call kept; regex decoy ignored on the same line.
    assert callees._callee_refs_from_anchor_line(
        "    return real(/helper()/);", path="src/mod.ts"
    ) == frozenset({(None, "real")})
    # Division must not be treated as a regex opener.
    assert callees._callee_refs_from_anchor_line(
        "    return a / helper() / 2;", path="src/mod.ts"
    ) == frozenset({(None, "helper")})
    # Keyword-prefixed regex literals are still inert.
    assert (
        callees._callee_refs_from_anchor_line("    return /helper()/;", path="src/mod.ts")
        == frozenset()
    )
    # Template interpolation may contain a regex decoy or a real call.
    assert (
        callees._callee_refs_from_anchor_line(
            "    message = `x ${/helper()/} y`", path="src/mod.ts"
        )
        == frozenset()
    )
    assert callees._callee_refs_from_anchor_line(
        "    message = `x ${real(/helper()/)} y`", path="src/mod.ts"
    ) == frozenset({(None, "real")})
    # Non-JS paths do not invent regex masking for `/.../`.
    assert callees._callee_refs_from_anchor_line(
        "    const pattern = /helper()/;", path="src/mod.py"
    ) == frozenset({(None, "helper")})


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
    # Inert nested literals inside interpolations must not yield callees.
    assert callees._callee_refs_from_anchor_line("    message = f'{\"helper()\"}'") == frozenset()
    assert callees._callee_refs_from_anchor_line('    message = `${"helper()}"`') == frozenset()
    assert callees._callee_refs_from_anchor_line(
        "    message = f'{real(\"helper()\")}'"
    ) == frozenset({(None, "real")})
    # Nested f-string / template interpolations remain executable.
    assert callees._callee_refs_from_anchor_line("    message = f\"{f'{helper()}'}\"") == frozenset(
        {(None, "helper")}
    )
    assert callees._callee_refs_from_anchor_line(
        "    message = `${`x ${helper()} y`}`"
    ) == frozenset({(None, "helper")})
    # Comments inside retained brace expressions must not yield callees.
    assert callees._callee_refs_from_anchor_line(
        '    message = f"{real(1)  # helper()}"'
    ) == frozenset({(None, "real")})


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
def test_callee_refs_include_default_expression_calls_before_definition_colon() -> None:
    """Default-arg calls before ``:`` must count as callees (not only post-colon bodies)."""
    assert callees._callee_refs_from_anchor_line("def reviewed(value=helper()):") == frozenset(
        {(None, "helper")}
    )
    assert callees._callee_names_from_anchor_line(
        "def reviewed(value=helper()): return other()"
    ) == frozenset({"helper", "other"})
    # Declared name is still not a callee; empty signature stays empty.
    assert callees._callee_names_from_anchor_line("def reviewed():") == frozenset()
    assert callees._callee_names_from_anchor_line("class Foo:") == frozenset()


@pytest.mark.unit
def test_callee_refs_preserve_blocklisted_qualifier() -> None:
    """Keyword-like receivers stay qualified so FIXED evidence fails closed.

    Erasing ``match`` / ``from`` to a bare name would let an unrelated
    module-level ``helper`` / ``send`` satisfy call-site→definition linking.
    """
    assert callees._callee_refs_from_anchor_line("    return from.send()") == frozenset(
        {("from", "send")}
    )
    assert callees._callee_refs_from_anchor_line("    return match.helper()") == frozenset(
        {("match", "helper")}
    )

    py = "def helper():\n    return 99\n\ndef reviewed(match):\n    return match.helper()\n"
    refs = callees._callee_refs_from_file_line(py, 5, path="pkg/mod.py")
    assert refs == frozenset({("match", "helper")})
    qualifier, name = next(iter(refs))
    # Non-self/cls receivers fail closed; bare ``helper`` would bind lines 1–3.
    assert (
        callees._resolve_callee_definition_span(
            py, call_line=5, qualifier=qualifier, name=name, path="pkg/mod.py"
        )
        is None
    )
    assert callees._resolve_callee_definition_span(
        py, call_line=5, qualifier=None, name="helper", path="pkg/mod.py"
    ) == (1, 3)


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
    exported = "@@ -1,1 +1,1 @@\n-export function helper() {\n+export function helper() {\n"
    assert callees._diff_text_changes_definition_names(exported, frozenset({"helper"})) is True
    export_async = (
        "@@ -1,1 +1,1 @@\n-export async function helper() {\n+export async function helper() {\n"
    )
    assert callees._diff_text_changes_definition_names(export_async, frozenset({"helper"})) is True


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
def test_resolve_callee_definition_span_includes_typed_typescript_arrow_body() -> None:
    """TS ``const helper = (value: number): number =>`` must record a definition span.

    Return-type annotations sit between the parameter list and ``=>``. Body-only
    repairs to that helper at an anchored ``helper()`` call must stay
    FIXED-with-evidence rather than FIXED-without-evidence.
    """
    text = (
        "const helper = (value: number): number => {\n"
        "  return value + 1;\n"
        "};\n"
        "\n"
        "function reviewed() {\n"
        "  return helper(1);\n"
        "}\n"
    )
    assert callees._enclosing_definition_name(text, 2) == "helper"
    assert callees._resolve_callee_definition_span(
        text, call_line=6, qualifier=None, name="helper"
    ) == (1, 4)
    async_text = (
        "const helper = async (value: number): Promise<number> => {\n"
        "  return value + 1;\n"
        "};\n"
        "\n"
        "function reviewed() {\n"
        "  return helper(1);\n"
        "}\n"
    )
    assert callees._resolve_callee_definition_span(
        async_text, call_line=6, qualifier=None, name="helper"
    ) == (1, 4)
    typed_diff = (
        "@@ -1,1 +1,1 @@\n"
        "-const helper = (value: number): number => {\n"
        "+const helper = (value: number): number => {\n"
    )
    assert callees._diff_text_changes_definition_names(typed_diff, frozenset({"helper"})) is True


@pytest.mark.unit
def test_resolve_callee_definition_span_includes_export_function_body() -> None:
    """``export function helper`` must resolve so body-only repairs count as evidence."""
    text = (
        "export function helper() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function reviewed() {\n"
        "  return helper();\n"
        "}\n"
    )
    assert callees._enclosing_definition_name(text, 2) == "helper"
    assert callees._resolve_callee_definition_span(
        text, call_line=6, qualifier=None, name="helper"
    ) == (1, 4)
    async_text = (
        "export async function helper() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function reviewed() {\n"
        "  return helper();\n"
        "}\n"
    )
    assert callees._resolve_callee_definition_span(
        async_text, call_line=6, qualifier=None, name="helper"
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
def test_resolve_callee_definition_span_js_this_class_field_arrow() -> None:
    """JS/TS ``this.helper()`` must resolve a same-class field arrow (FIXED evidence).

    Extraction preserves ``this``, but resolution must treat it as a class receiver
    on JS/TS paths — otherwise body-only repairs of ``helper`` look FIXED-without-
    evidence and enter correction/rollback.
    """
    js = (
        "class Foo {\n"
        "  helper = () => {\n"
        "    return 1;\n"
        "  }\n"
        "  reviewed = () => {\n"
        "    return this.helper();\n"
        "  }\n"
        "}\n"
    )
    assert callees._callee_refs_from_anchor_line(
        "    return this.helper()", path="src/mod.ts"
    ) == frozenset({("this", "helper")})
    assert callees._resolve_callee_definition_span(
        js, call_line=6, qualifier="this", name="helper", path="src/mod.ts"
    ) == (2, 4)
    # Non-JS/TS paths must still fail closed for ``this``.
    assert (
        callees._resolve_callee_definition_span(
            js, call_line=6, qualifier="this", name="helper", path="src/mod.py"
        )
        is None
    )
    assert (
        callees._resolve_callee_definition_span(
            js, call_line=6, qualifier="this", name="helper", path=None
        )
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_js_this_outside_class_fails_closed() -> None:
    """``this.helper()`` with no enclosing class must not bind a module helper."""
    js = (
        "function helper() {\n  return 1;\n}\n\nfunction reviewed() {\n  return this.helper();\n}\n"
    )
    assert (
        callees._resolve_callee_definition_span(
            js, call_line=6, qualifier="this", name="helper", path="src/mod.ts"
        )
        is None
    )


@pytest.mark.unit
def test_definition_head_has_js_dynamic_this_guards_and_forms() -> None:
    """Dynamic-``this`` detection covers decls/exprs and rejects empty/OOB heads."""
    assert callees._definition_head_has_js_dynamic_this("", 1) is False
    assert callees._definition_head_has_js_dynamic_this("function f() {}\n", 0) is False
    assert callees._definition_head_has_js_dynamic_this("function f() {}\n", 99) is False
    assert callees._definition_head_has_js_dynamic_this("  function inner() {\n", 1) is True
    assert (
        callees._definition_head_has_js_dynamic_this("  const inner = async function () {\n", 1)
        is True
    )
    assert callees._definition_head_has_js_dynamic_this("  const inner = () => {\n", 1) is False


@pytest.mark.unit
def test_js_nested_dynamic_this_between_skips_nested_class() -> None:
    """Nested class spans between method and line are not dynamic-``this`` heads."""
    js = (
        "class Foo {\n"
        "  reviewed = () => {\n"
        "    class Inner {\n"
        "      x = 1\n"
        "    }\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
    )
    assert (
        callees._js_nested_dynamic_this_between(js, method_start=2, line=4, path="src/mod.ts")
        is False
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_js_this_nested_function_fails_closed() -> None:
    """Nested ``function`` has dynamic ``this`` — must not inherit the outer class.

    ``inner.call(other)`` invokes ``other.helper``, so linking the class field
    ``helper`` would allow FIXED evidence without repairing the called target.
    """
    nested_decl = (
        "class Foo {\n"
        "  helper = () => {\n"
        "    return 1;\n"
        "  }\n"
        "  reviewed = () => {\n"
        "    function inner() {\n"
        "      return this.helper();\n"
        "    }\n"
        "    return inner();\n"
        "  }\n"
        "}\n"
    )
    assert (
        callees._resolve_callee_definition_span(
            nested_decl, call_line=7, qualifier="this", name="helper", path="src/mod.ts"
        )
        is None
    )
    nested_expr = (
        "class Foo {\n"
        "  helper = () => {\n"
        "    return 1;\n"
        "  }\n"
        "  reviewed = () => {\n"
        "    const inner = function () {\n"
        "      return this.helper();\n"
        "    }\n"
        "    return inner();\n"
        "  }\n"
        "}\n"
    )
    assert (
        callees._resolve_callee_definition_span(
            nested_expr, call_line=7, qualifier="this", name="helper", path="src/mod.ts"
        )
        is None
    )
    # Arrow wrapping a nested function still has dynamic ``this`` at the call.
    wrapped = (
        "class Foo {\n"
        "  helper = () => {\n"
        "    return 1;\n"
        "  }\n"
        "  reviewed = () => {\n"
        "    const mid = () => {\n"
        "      function inner() {\n"
        "        return this.helper();\n"
        "      }\n"
        "      return inner();\n"
        "    }\n"
        "    return mid();\n"
        "  }\n"
        "}\n"
    )
    assert (
        callees._resolve_callee_definition_span(
            wrapped, call_line=8, qualifier="this", name="helper", path="src/mod.ts"
        )
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_js_this_nested_arrow_inherits() -> None:
    """Nested arrows lexically inherit ``this`` from the enclosing class method."""
    js = (
        "class Foo {\n"
        "  helper = () => {\n"
        "    return 1;\n"
        "  }\n"
        "  reviewed = () => {\n"
        "    const inner = () => {\n"
        "      return this.helper();\n"
        "    }\n"
        "    return inner();\n"
        "  }\n"
        "}\n"
    )
    assert callees._resolve_callee_definition_span(
        js, call_line=7, qualifier="this", name="helper", path="src/mod.ts"
    ) == (2, 4)


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
def test_resolve_callee_definition_span_unicode_outer_keeps_nested_helper_local() -> None:
    """ASCII helpers nested under Unicode-named defs must not look module-scoped.

    Dropping Unicode ``def`` heads from scope matching would treat the nested
    ``helper`` as a module candidate, so an outside call that cannot see it
    would still link that body as FIXED evidence.
    """
    text = (
        "def 函数():\n"
        "    def helper():\n"
        "        return 1\n"
        "    return 0\n"
        "\n"
        "def reviewed():\n"
        "    return helper()\n"
    )
    spans = callees._iter_definition_spans(text)
    assert ("函数", 1, 5, 0) in spans
    assert ("helper", 2, 3, 4) in spans
    assert callees._definition_is_nested_in_other(spans, start=2, indent=4) is True
    assert (
        callees._resolve_callee_definition_span(text, call_line=7, qualifier=None, name="helper")
        is None
    )
    # Inside the Unicode outer, the nested helper remains the in-scope target.
    inside = (
        "def 函数():\n"
        "    def helper():\n"
        "        return 1\n"
        "    return helper()\n"
        "\n"
        "def helper():\n"
        "    return 99\n"
    )
    assert callees._resolve_callee_definition_span(
        inside, call_line=4, qualifier=None, name="helper"
    ) == (2, 3)
    assert callees._enclosing_definition_identity(inside, 1) == ("函数", 1)


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
def test_resolve_callee_definition_span_rejects_block_scoped_js_assignment() -> None:
    """Indented ``const``/``let`` under ``if`` must not satisfy a later module call.

    Block-scoped declarations are invisible outside the block; linking their body
    would let an inaccessible edit count as FIXED callee evidence.
    """
    text = (
        "if (flag) {\n"
        "  const helper = () => {\n"
        "    return 1;\n"
        "  };\n"
        "}\n"
        "\n"
        "function reviewed() {\n"
        "  return helper();\n"
        "}\n"
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=8, qualifier=None, name="helper")
        is None
    )
    # True module-level const remains callable from a later function.
    module_const = (
        "const helper = () => {\n  return 1;\n};\n\nfunction reviewed() {\n  return helper();\n}\n"
    )
    assert callees._resolve_callee_definition_span(
        module_const, call_line=6, qualifier=None, name="helper"
    ) == (1, 4)
    let_under_if = "if (flag) {\n  let helper = () => {\n    return 1;\n  };\n}\nhelper();\n"
    assert (
        callees._resolve_callee_definition_span(
            let_under_if, call_line=6, qualifier=None, name="helper"
        )
        is None
    )


@pytest.mark.unit
def test_definition_head_is_assignment() -> None:
    text = "const helper = () => {\n  return 1;\n};\n\ndef reviewed():\n    pass\n"
    assert callees._definition_head_is_assignment(text, 1) is True
    assert callees._definition_head_is_assignment(text, 5) is False
    assert callees._definition_head_is_assignment(text, 0) is False
    assert callees._definition_head_is_assignment(text, 99) is False


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
def test_definition_discovery_ignores_defs_inside_multiline_string_literals() -> None:
    """String-embedded ``def helper():`` must not become a nearer executable span.

    Otherwise a later ``helper()`` call binds the decoy, and editing inert prose
    in its apparent body can satisfy FIXED evidence while the real callee is
    untouched.
    """
    text = (
        "def helper():\n"
        "    return 1\n"
        "\n"
        'DOC = """\n'
        "def helper():\n"
        "    prose only\n"
        '"""\n'
        "\n"
        "def reviewed():\n"
        "    return helper()\n"
    )
    spans = [
        (name, start, end)
        for name, start, end, _indent in callees._iter_definition_spans(text, path="src/x.py")
        if name == "helper"
    ]
    assert spans == [("helper", 1, 3)]
    assert callees._resolve_callee_definition_span(
        text, call_line=10, qualifier=None, name="helper", path="src/x.py"
    ) == (1, 3)
    # Nearest head above decoy prose must be the real helper, not the string line.
    assert callees._enclosing_definition_identity(text, 6, path="src/x.py") == ("helper", 1)
    # JS block-comment decoy function heads are likewise non-definitions.
    js = (
        "function helper() {\n"
        "  return 1;\n"
        "}\n"
        "/*\n"
        "function helper() {\n"
        "  decoy\n"
        "}\n"
        "*/\n"
        "function reviewed() {\n"
        "  return helper();\n"
        "}\n"
    )
    assert callees._resolve_callee_definition_span(
        js, call_line=10, qualifier=None, name="helper", path="src/mod.ts"
    ) == (1, 3)


@pytest.mark.unit
def test_resolve_callee_definition_span_rejects_unsupported_qualifier() -> None:
    """Non-self/cls/this receivers must fail closed, not bind an unrelated bare def."""
    text = "def send():\n    return 99\n\ndef reviewed():\n    return client.send()\n"
    assert callees._callee_refs_from_anchor_line("    return client.send()") == frozenset(
        {("client", "send")}
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=5, qualifier="client", name="send")
        is None
    )


@pytest.mark.unit
def test_callee_refs_reject_bare_callee_from_call_result_method() -> None:
    """Method calls on non-ident receivers must not emit unqualified callees.

    ``factory().helper()``, ``items[0].helper()``, and ``super().helper()`` only
    capture a simple-ident qualifier today, so ``helper`` was emitted bare and
    could link an unrelated module ``def helper`` as FIXED evidence.
    """
    assert callees._callee_refs_from_anchor_line("    return factory().helper()") == frozenset(
        {(None, "factory")}
    )
    assert callees._callee_refs_from_anchor_line("    return items[0].helper()") == frozenset()
    assert callees._callee_refs_from_anchor_line("    return super().helper()") == frozenset(
        {(None, "super")}
    )
    # True bare / simple-ident receivers keep current behavior.
    assert callees._callee_refs_from_anchor_line("    return helper()") == frozenset(
        {(None, "helper")}
    )
    assert callees._callee_refs_from_anchor_line("    return self.helper()") == frozenset(
        {("self", "helper")}
    )
    assert callees._callee_refs_from_anchor_line("    return client.helper()") == frozenset(
        {("client", "helper")}
    )
    # Optional-chain on a call result likewise fails closed for the method name.
    assert callees._callee_refs_from_anchor_line(
        "    return factory()?.helper()", path="src/mod.ts"
    ) == frozenset({(None, "factory")})
    assert callees._callee_refs_from_anchor_line("    return factory().helper?.()") == frozenset(
        {(None, "factory")}
    )


@pytest.mark.unit
async def test_diff_changes_referenced_definition_rejects_call_result_method_decoy(
    tmp_path: Path,
) -> None:
    """Editing an unrelated module ``helper`` must not satisfy ``factory().helper()``."""
    file_text = "def helper():\n    return 1\n\ndef reviewed():\n    return factory().helper()\n"
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
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
        is False
    )
    # Indexed / super receivers likewise must not bind the decoy helper.
    for anchor in ("    return items[0].helper()\n", "    return super().helper()\n"):
        decoy = "def helper():\n    return 1\n\ndef reviewed():\n" + anchor
        assert (
            await ancestry._diff_changes_referenced_definition(
                runner,
                worktree_path=tmp_path,
                left="HEAD",
                path="src/x.py",
                line=5,
                diff_text="@@ -2,1 +2,1 @@\n-    return 1\n+    return 2\n",
                file_text=decoy,
            )
            is False
        )
