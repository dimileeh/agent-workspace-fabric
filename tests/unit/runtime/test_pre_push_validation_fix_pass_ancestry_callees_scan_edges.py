"""Direct unit tests for pre-push FIXED callee scan/mask edge helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry as ancestry
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry_callees as callees


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


@pytest.mark.unit
def test_js_slash_can_start_regex_rejects_assign_and_accepts_bol_arrow() -> None:
    """``/=`` is not a regex; BOL and ``=>`` may open one."""
    assert callees._js_slash_can_start_regex("x /= helper()", 2) is False
    assert callees._js_slash_can_start_regex("/* not regex", 0) is False
    assert callees._js_slash_can_start_regex("/helper()/", 0) is True
    assert callees._js_slash_can_start_regex("  /helper()/", 2) is True
    assert callees._js_slash_can_start_regex("() => /helper()/", 6) is True
    # Division-assign must keep the call; not swallow it as a regex body.
    assert callees._callee_refs_from_anchor_line(
        "    x /= helper();", path="src/mod.ts"
    ) == frozenset({(None, "helper")})
    assert (
        callees._callee_refs_from_anchor_line("() => /helper()/;", path="src/mod.ts") == frozenset()
    )
    assert callees._callee_refs_from_anchor_line("/helper()/;", path="src/mod.ts") == frozenset()
    # Unterminated regex at EOF blanks to end without swallowing a later line.
    assert (
        callees._callee_refs_from_anchor_line("const p = /helper(", path="src/mod.ts")
        == frozenset()
    )


@pytest.mark.unit
def test_decorator_basenames_above_walks_to_file_start_without_prior_def() -> None:
    """Decorator stacks at file top must still collect names when no prior def exists."""
    text = "@decoy(\n    x=1\n)\ndef reviewed():\n    return 1\n"
    assert callees._decorator_basenames_above(text, 4) == frozenset({"decoy"})


@pytest.mark.unit
def test_js_regex_mask_stops_at_newline_so_later_call_remains() -> None:
    """An unterminated regex must not blank past the newline into later code."""
    text = "const pattern = /helper(\n)/;\nreal()\n"
    assert callees._callee_refs_from_file_line(text, 3, path="src/mod.ts") == frozenset(
        {(None, "real")}
    )


@pytest.mark.unit
def test_js_regex_mask_preserves_form_feed_alignment() -> None:
    """Form feed inside a regex is not a JS line terminator; keep line indices.

    ``str.splitlines()`` splits on ``\\f``, but JS regex literals continue across
    it. Masking must blank the whole literal (no false defs from the body) and
    preserve the separator so a later ``function helper`` keeps its raw line.
    """
    text = (
        "const pattern = /x\x0cfunction decoy() { return 1; }/;\n"
        "function helper() {\n"
        "  return 1;\n"
        "}\n"
    )
    assert len(text.splitlines()) == 5
    spans_by_name = {
        name: (start, end)
        for name, start, end, _indent in callees._iter_definition_spans(text, path="src/mod.ts")
    }
    assert "decoy" not in spans_by_name
    assert spans_by_name["helper"][0] == 3
    assert callees._enclosing_definition_identity(text, 4, path="src/mod.ts") == ("helper", 3)
    # Escaped form feed inside a regex must also stay inert and aligned.
    escaped = "const pattern = /x\\\x0cy/;\nfunction helper() {\n  return 1;\n}\n"
    assert callees._enclosing_definition_identity(escaped, 4, path="src/mod.ts") == ("helper", 3)


@pytest.mark.unit
def test_jsx_text_mask_preserves_form_feed_alignment() -> None:
    """JSX text blanking must retain ``splitlines`` separators for line indices.

    Replacing form feed with a space collapses raw lines and pads empty scan
    slots, so later fragments no longer share indices with ``splitlines()``.
    Definitions above the JSX stay numbered; inert text yields no callees.
    """
    text = (
        "function helper() {\n"
        "  return 1;\n"
        "}\n"
        "function reviewed() {\n"
        "  return <div>decoy()\x0cmore</div>;\n"
        "}\n"
    )
    raw_count = len(text.splitlines())
    assert raw_count == 7
    scan = callees._definition_head_scan_lines(text, path="src/mod.tsx")
    assert len(scan) == raw_count
    # Form feed must remain a real split so ``</div>`` stays on raw line 6.
    assert "<div>" in scan[4]
    assert "</div>" in scan[5]
    assert callees._callee_refs_from_file_line(text, 5, path="src/mod.tsx") == frozenset()
    assert callees._callee_refs_from_file_line(text, 6, path="src/mod.tsx") == frozenset()
    spans_by_name = {
        name: (start, end)
        for name, start, end, _indent in callees._iter_definition_spans(text, path="src/mod.tsx")
    }
    assert spans_by_name["helper"][0] == 1
    assert spans_by_name["reviewed"][0] == 4
    assert callees._enclosing_definition_identity(text, 2, path="src/mod.tsx") == ("helper", 1)


@pytest.mark.unit
def test_enclosing_class_method_skips_nested_class_and_class_body() -> None:
    """Nested class heads are not methods; bare class-body lines have no method."""
    text = (
        "class Outer:\n"
        "    class Inner:\n"
        "        def helper(self):\n"
        "            return 1\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
    )
    # Line inside Inner.helper: nested class span is skipped, then helper wins.
    assert callees._enclosing_class_method_def_start(text, 3) == 3
    # Class-body assignment is not under a method.
    body_only = "class Foo:\n    x = 1\n    def helper(self):\n        return 1\n"
    assert callees._enclosing_class_method_def_start(body_only, 2) is None
    # Locals nested under a method are not themselves class methods.
    nested_local = (
        "class Foo:\n"
        "    def reviewed(self):\n"
        "        def local():\n"
        "            return 1\n"
        "        return self.helper()\n"
        "    def helper(self):\n"
        "        return 2\n"
    )
    assert callees._enclosing_class_method_def_start(nested_local, 3) == 2
    assert callees._enclosing_class_method_def_start(nested_local, 4) == 2


@pytest.mark.unit
def test_callee_refs_retain_private_field_and_strip_line_comment_in_template() -> None:
    """Template ``${...}`` keeps ``#ident`` calls and blanks ``//`` decoys."""
    assert callees._callee_refs_from_anchor_line(
        "    message = `x ${obj.#helper()} y`", path="src/mod.ts"
    ) == frozenset({(None, "helper")})
    assert callees._callee_refs_from_anchor_line(
        "    message = `x ${real() // helper()} y`", path="src/mod.ts"
    ) == frozenset({(None, "real")})


@pytest.mark.unit
def test_jsx_mask_handles_unclosed_tag_and_nested_expression_braces() -> None:
    """Unclosed tags fail closed without crashing; nested ``{`` stays scannable."""
    assert callees._callee_refs_from_anchor_line(
        "    return <div helper()", path="src/mod.tsx"
    ) == frozenset({(None, "helper")})
    assert callees._callee_refs_from_anchor_line(
        "    return <div>{outer({inner: helper()})}</div>;", path="src/mod.tsx"
    ) == frozenset({(None, "outer"), (None, "helper")})


@pytest.mark.unit
def test_bare_callee_at_column_zero_is_not_attribute_dot() -> None:
    """A callee at match start 0 is a real bare call, not ``.helper`` fallout."""
    assert callees._callee_refs_from_anchor_line("helper()", path="src/mod.ts") == frozenset(
        {(None, "helper")}
    )
    assert callees._bare_callee_follows_attribute_dot("helper()", 0) is False


@pytest.mark.unit
def test_split_receiver_with_trailing_dot_ignores_non_call_continuation() -> None:
    """``self.`` above a non-call line must not invent a qualified callee."""
    text = (
        "class Foo:\n"
        "    def reviewed(self):\n"
        "        return (\n"
        "            self.\n"
        "            not_a_call\n"
        "        )\n"
    )
    assert callees._callee_refs_from_file_line(text, 5) == frozenset()
