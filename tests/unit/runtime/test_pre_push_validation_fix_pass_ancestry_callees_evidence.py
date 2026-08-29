"""Direct unit tests for pre-push FIXED callee ancestry (receivers/evidence)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import FakeCommandRunner
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry as ancestry
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry_callees as callees


@pytest.mark.unit
def test_callee_refs_mask_jsx_text_nodes() -> None:
    """Literal JSX text must not extract callees; ``{...}`` expressions may.

    ``<div>helper()</div>`` would otherwise link an unrelated module ``helper``
    as FIXED evidence while the rendered text is unchanged.
    """
    assert (
        callees._callee_refs_from_anchor_line("    return <div>helper()</div>;", path="src/mod.tsx")
        == frozenset()
    )
    assert callees._callee_refs_from_anchor_line(
        "    return <div>{helper()}</div>;", path="src/mod.tsx"
    ) == frozenset({(None, "helper")})
    assert (
        callees._callee_refs_from_anchor_line("    return <div>helper()</div>;", path="src/mod.jsx")
        == frozenset()
    )
    # Plain JS/TS without JSX text nodes keeps call extraction.
    assert callees._callee_refs_from_anchor_line(
        "    return helper();", path="src/mod.ts"
    ) == frozenset({(None, "helper")})

    decoy = (
        "function helper() {\n"
        "  return 1;\n"
        "}\n"
        "\n"
        "function reviewed() {\n"
        "  return <div>helper()</div>;\n"
        "}\n"
    )
    assert callees._callee_refs_from_file_line(decoy, 6, path="src/mod.tsx") == frozenset()


@pytest.mark.unit
def test_callee_refs_mask_jsx_fragment_text_nodes() -> None:
    """Fragment openers ``<>`` must blank text nodes like named tags.

    Without recognizing ``<>``, ``<>helper()</>`` still extracts ``helper`` and
    can treat an unrelated same-file helper body edit as FIXED evidence.
    """
    assert (
        callees._callee_refs_from_anchor_line("    return <>helper()</>;", path="src/mod.tsx")
        == frozenset()
    )
    assert callees._callee_refs_from_anchor_line(
        "    return <>{helper()}</>;", path="src/mod.tsx"
    ) == frozenset({(None, "helper")})
    assert (
        callees._callee_refs_from_anchor_line("    return <>helper()</>;", path="src/mod.jsx")
        == frozenset()
    )

    decoy = (
        "function helper() {\n  return 1;\n}\n\nfunction reviewed() {\n  return <>helper()</>;\n}\n"
    )
    assert callees._callee_refs_from_file_line(decoy, 6, path="src/mod.tsx") == frozenset()


@pytest.mark.unit
def test_resolve_callee_definition_span_rejects_block_scoped_js_function() -> None:
    """Indented ``function helper`` under ``if`` must not win over module scope.

    Unlike assignment heads, ordinary function declarations were still treated
    as module candidates, so editing the block-local body could satisfy FIXED
    evidence for a later top-level ``helper()`` call.
    """
    text = (
        "function helper() {\n"
        "  return 0;\n"
        "}\n"
        "\n"
        "if (flag) {\n"
        "  function helper() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
        "\n"
        "function reviewed() {\n"
        "  return helper();\n"
        "}\n"
    )
    # Module call must bind the top-level helper, not the block function.
    assert callees._resolve_callee_definition_span(
        text, call_line=12, qualifier=None, name="helper", path="src/mod.ts"
    ) == (1, 4)
    # Without a top-level helper, the block function must not become a candidate.
    block_only = (
        "if (flag) {\n"
        "  function helper() {\n"
        "    return 1;\n"
        "  }\n"
        "}\n"
        "\n"
        "function reviewed() {\n"
        "  return helper();\n"
        "}\n"
    )
    assert (
        callees._resolve_callee_definition_span(
            block_only, call_line=8, qualifier=None, name="helper", path="src/mod.js"
        )
        is None
    )


@pytest.mark.unit
def test_callee_refs_preserve_optional_chain_qualifier() -> None:
    """JS/TS ``client?.send()`` must keep the receiver, not become a bare ``send``.

    Dropping the optional-chain qualifier would let an unrelated module-level
    ``function send`` satisfy FIXED evidence while the invoked method is untouched.
    """
    assert callees._callee_refs_from_anchor_line(
        "    return client?.send()", path="src/mod.ts"
    ) == frozenset({("client", "send")})
    assert callees._callee_refs_from_anchor_line(
        "    return self?.helper()", path="src/mod.ts"
    ) == frozenset({("self", "helper")})

    js = (
        "function send() {\n"
        "  return 99;\n"
        "}\n"
        "\n"
        "function reviewed(client) {\n"
        "  return client?.send();\n"
        "}\n"
    )
    refs = callees._callee_refs_from_file_line(js, 6, path="src/mod.ts")
    assert refs == frozenset({("client", "send")})
    qualifier, name = next(iter(refs))
    assert (
        callees._resolve_callee_definition_span(
            js, call_line=6, qualifier=qualifier, name=name, path="src/mod.ts"
        )
        is None
    )

    # Split optional-chain receivers must also stay qualified.
    split = (
        "function send() {\n"
        "  return 99;\n"
        "}\n"
        "\n"
        "function reviewed(client) {\n"
        "  return (\n"
        "    client\n"
        "    ?.send()\n"
        "  );\n"
        "}\n"
    )
    assert callees._callee_refs_from_file_line(split, 8, path="src/mod.ts") == frozenset(
        {("client", "send")}
    )
    dotted_prior = (
        "function send() {\n"
        "  return 99;\n"
        "}\n"
        "\n"
        "function reviewed(client) {\n"
        "  return client?.\n"
        "    send();\n"
        "}\n"
    )
    assert callees._callee_refs_from_file_line(dotted_prior, 7, path="src/mod.ts") == frozenset(
        {("client", "send")}
    )

    # Optional-call continuation after a trailing ``?.`` must keep the receiver.
    optional_call_split = (
        "function send() {\n"
        "  return 99;\n"
        "}\n"
        "\n"
        "function reviewed(client) {\n"
        "  return client?.\n"
        "    send?.();\n"
        "}\n"
    )
    assert callees._callee_refs_from_file_line(
        optional_call_split, 7, path="src/mod.ts"
    ) == frozenset({("client", "send")})
    oq, oname = next(
        iter(callees._callee_refs_from_file_line(optional_call_split, 7, path="src/mod.ts"))
    )
    assert (
        callees._resolve_callee_definition_span(
            optional_call_split,
            call_line=7,
            qualifier=oq,
            name=oname,
            path="src/mod.ts",
        )
        is None
    )


@pytest.mark.unit
def test_callee_refs_capture_optional_call() -> None:
    """JS/TS ``helper?.()`` must extract the callee, not require bare ``(``.

    Optional-call places ``?.`` between the name and ``(``. Without that form,
    a body-only repair of ``const helper = () => ...`` has no call-site→definition
    FIXED evidence and is incorrectly routed through correction or rollback.
    """
    assert callees._callee_refs_from_anchor_line(
        "    return helper?.()", path="src/mod.ts"
    ) == frozenset({(None, "helper")})
    assert callees._callee_refs_from_anchor_line(
        "    return helper ?. ()", path="src/mod.ts"
    ) == frozenset({(None, "helper")})
    assert callees._callee_refs_from_anchor_line(
        "    return client?.send?.()", path="src/mod.ts"
    ) == frozenset({("client", "send")})

    js = (
        "const helper = () => {\n"
        "  return 1;\n"
        "};\n"
        "\n"
        "function reviewed() {\n"
        "  return helper?.();\n"
        "}\n"
    )
    refs = callees._callee_refs_from_file_line(js, 6, path="src/mod.ts")
    assert refs == frozenset({(None, "helper")})
    qualifier, name = next(iter(refs))
    assert callees._resolve_callee_definition_span(
        js, call_line=6, qualifier=qualifier, name=name, path="src/mod.ts"
    ) == (1, 4)


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
    """``cls.helper()`` resolves only when ``@classmethod`` establishes class binding."""
    text = (
        "class Foo:\n"
        "    def helper(cls):\n"
        "        return 1\n"
        "\n"
        "    @classmethod\n"
        "    def reviewed(cls):\n"
        "        return cls.helper()\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=7, qualifier="cls", name="helper"
    ) == (2, 4)


@pytest.mark.unit
def test_resolve_callee_definition_span_staticmethod_self_fails_closed() -> None:
    """``self`` on a ``@staticmethod`` is an ordinary arg, not an instance receiver."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    @staticmethod\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=7, qualifier="self", name="helper")
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_staticmethod_cls_fails_closed() -> None:
    """``cls`` on a ``@staticmethod`` must not link the enclosing class method."""
    text = (
        "class Foo:\n"
        "    def helper(cls):\n"
        "        return 1\n"
        "\n"
        "    @staticmethod\n"
        "    def reviewed(cls):\n"
        "        return cls.helper()\n"
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=7, qualifier="cls", name="helper")
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_cls_without_classmethod_fails_closed() -> None:
    """Undecorated methods establish instance binding; ``cls.`` must fail closed."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(cls):\n"
        "        return cls.helper()\n"
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=6, qualifier="cls", name="helper")
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_self_on_classmethod_fails_closed() -> None:
    """``self.`` inside ``@classmethod`` is not an instance-receiver binding."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    @classmethod\n"
        "    def reviewed(cls):\n"
        "        return self.helper()\n"
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=7, qualifier="self", name="helper")
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_self_nested_in_instance_method() -> None:
    """Nested defs inside an instance method still see closure ``self`` as receiver."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(self):\n"
        "        def inner():\n"
        "            return self.helper()\n"
        "        return inner()\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=7, qualifier="self", name="helper"
    ) == (2, 4)


@pytest.mark.unit
def test_resolve_callee_definition_span_self_nested_in_staticmethod_fails_closed() -> None:
    """Nested defs inside ``@staticmethod`` must not treat ``self`` as a receiver."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    @staticmethod\n"
        "    def reviewed(self):\n"
        "        def inner():\n"
        "            return self.helper()\n"
        "        return inner()\n"
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=8, qualifier="self", name="helper")
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_dotted_staticmethod_fails_closed() -> None:
    """``@foo.staticmethod`` (dotted) still clears receiver binding."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    @foo.staticmethod\n"
        "    # decoy comment between decorator and def\n"
        "\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=9, qualifier="self", name="helper")
        is None
    )


@pytest.mark.unit
def test_decorator_basenames_above_empty_and_stops_at_definition_head() -> None:
    """Decorator walk guards empty inputs and stops at the prior definition head."""
    assert callees._decorator_basenames_above("def x():\n    return 1\n", 1) == frozenset()
    assert callees._decorator_basenames_above("def x():\n    return 1\n", 99) == frozenset()
    text = "class Foo:\n    @classmethod\n    @other\n    def reviewed(cls):\n        return 1\n"
    assert callees._decorator_basenames_above(text, 4) == frozenset({"classmethod", "other"})


@pytest.mark.unit
def test_decorator_basenames_above_skips_multiline_call_tail() -> None:
    """Multiline decorator call tails must not drop stacked ``@classmethod``."""
    text = (
        "class Foo:\n"
        "    @classmethod\n"
        "    @decoy(\n"
        "        arg=1,\n"
        "    )\n"
        "    def reviewed(cls):\n"
        "        return 1\n"
    )
    assert callees._decorator_basenames_above(text, 6) == frozenset({"classmethod", "decoy"})


@pytest.mark.unit
def test_decorator_basenames_above_multiline_tail_does_not_steal_sibling() -> None:
    """Call-tail skip must still stop at the prior method head."""
    text = (
        "class Foo:\n"
        "    @staticmethod\n"
        "    def previous(self):\n"
        "        return 1\n"
        "\n"
        "    @decoy(\n"
        "        arg=1,\n"
        "    )\n"
        "    def reviewed(self):\n"
        "        return 1\n"
    )
    assert callees._decorator_basenames_above(text, 9) == frozenset({"decoy"})


@pytest.mark.unit
def test_resolve_callee_definition_span_classmethod_above_multiline_decorator_tail() -> None:
    """``cls.helper()`` stays FIXED evidence when ``@classmethod`` is above a call tail."""
    text = (
        "class Foo:\n"
        "    def helper(cls):\n"
        "        return 1\n"
        "\n"
        "    @classmethod\n"
        "    @decoy(\n"
        "        arg=1,\n"
        "    )\n"
        "    def reviewed(cls):\n"
        "        return cls.helper()\n"
    )
    assert callees._resolve_callee_definition_span(
        text, call_line=10, qualifier="cls", name="helper"
    ) == (2, 4)


@pytest.mark.unit
def test_resolve_callee_definition_span_staticmethod_above_multiline_decorator_tail_fails_closed() -> (
    None
):
    """``self.helper()`` must fail closed when ``@staticmethod`` is above a call tail."""
    text = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    @staticmethod\n"
        "    @decoy(\n"
        "        arg=1,\n"
        "    )\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
    )
    assert (
        callees._resolve_callee_definition_span(text, call_line=10, qualifier="self", name="helper")
        is None
    )


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
def test_callee_refs_from_file_line_uses_multiline_string_lexical_context() -> None:
    """Open multiline strings/docstrings blank decoy calls on interior review lines."""
    docstring = 'def reviewed():\n    """\n    Call helper() when ready.\n    """\n    return 1\n'
    assert callees._callee_refs_from_file_line(docstring, 3, path="src/x.py") == frozenset()
    # Isolated-line parse would falsely see helper(); file context must win.
    assert callees._callee_refs_from_anchor_line("    Call helper() when ready.") == frozenset(
        {(None, "helper")}
    )

    multiline = 'x = """\nhelper()\n"""\nreturn real()\n'
    assert callees._callee_refs_from_file_line(multiline, 2, path="src/x.py") == frozenset()
    assert callees._callee_refs_from_file_line(multiline, 4, path="src/x.py") == frozenset(
        {(None, "real")}
    )

    # Real call after the closing quotes on the same line stays a callee.
    close_then_call = 'x = """\ntext\n""" + helper()\n'
    assert callees._callee_refs_from_file_line(close_then_call, 3, path="src/x.py") == frozenset(
        {(None, "helper")}
    )

    # Multiline f-string interpolation remains executable.
    fstring = 'msg = f"""\n{helper()}\n"""\n'
    assert callees._callee_refs_from_file_line(fstring, 2, path="src/x.py") == frozenset(
        {(None, "helper")}
    )

    assert callees._callee_refs_from_file_line("", 1) == frozenset()
    assert callees._callee_refs_from_file_line("return helper()\n", 0) == frozenset()
    assert callees._callee_refs_from_file_line("return helper()\n", 9) == frozenset()


@pytest.mark.unit
def test_callee_refs_from_file_line_preserves_multiline_receiver() -> None:
    """Receiver on the prior line must stay attached to a leading-dot call.

    Anchoring only ``.helper()`` must not drop ``self``/``cls`` (or other
    identifiers) so an unrelated module ``helper`` cannot satisfy FIXED evidence.
    """
    split_self = (
        "def helper():\n"
        "    return 99\n"
        "\n"
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(self):\n"
        "        return (\n"
        "            self\n"
        "            .helper()\n"
        "        )\n"
    )
    # Anchored on the ``.helper()`` line — qualifier must survive the split.
    assert callees._callee_refs_from_file_line(split_self, 11, path="src/x.py") == frozenset(
        {("self", "helper")}
    )
    # Isolated ``.helper()`` (no receiver) must fail closed, not emit bare ``helper``.
    assert callees._callee_refs_from_anchor_line("            .helper()") == frozenset()
    refs = callees._callee_refs_from_file_line(split_self, 11, path="src/x.py")
    qualifier, name = next(iter(refs))
    assert callees._resolve_callee_definition_span(
        split_self, call_line=11, qualifier=qualifier, name=name, path="src/x.py"
    ) == (5, 7)

    # Prior line may already end with the attribute dot.
    dotted_prior = "class Foo:\n    def helper(self):\n        return 1\n    def reviewed(self):\n        return self.\n            helper()\n"
    assert callees._callee_refs_from_file_line(dotted_prior, 6, path="src/x.py") == frozenset(
        {("self", "helper")}
    )

    # Blank / comment-only gaps between receiver and call still join.
    with_gap = "class Foo:\n    def helper(self):\n        return 1\n    def reviewed(self):\n        return (\n            self\n\n            # note\n            .helper()\n        )\n"
    assert callees._callee_refs_from_file_line(with_gap, 9, path="src/x.py") == frozenset(
        {("self", "helper")}
    )

    # Non-self receivers stay qualified so resolution fails closed (not module bare).
    other = "def send():\n    return 1\n\ndef reviewed(client):\n    return (\n        client\n        .send()\n    )\n"
    assert callees._callee_refs_from_file_line(other, 7, path="src/x.py") == frozenset(
        {("client", "send")}
    )
    assert (
        callees._resolve_callee_definition_span(
            other, call_line=7, qualifier="client", name="send", path="src/x.py"
        )
        is None
    )


@pytest.mark.unit
def test_callee_refs_mask_js_block_comments_and_prefix_poison() -> None:
    """``/* */`` must blank decoys and must not leave quotes that poison later lines."""
    # Same-line decoy inside a block comment is not a callee.
    assert (
        callees._callee_refs_from_anchor_line("    /* TODO: helper() */", path="src/mod.ts")
        == frozenset()
    )
    assert callees._callee_refs_from_anchor_line(
        "    return real(/* helper() */);", path="src/mod.js"
    ) == frozenset({(None, "real")})
    # Quote inside an earlier block comment must not blank a later real call.
    poisoned = '/* " */\nreturn helper();\n'
    assert callees._callee_refs_from_file_line(poisoned, 2, path="src/mod.ts") == frozenset(
        {(None, "helper")}
    )
    # Multiline block comment decoy stays inert; following call remains evidence.
    multiline = "/*\nhelper()\n*/\nreturn real();\n"
    assert callees._callee_refs_from_file_line(multiline, 2, path="src/mod.tsx") == frozenset()
    assert callees._callee_refs_from_file_line(multiline, 4, path="src/mod.tsx") == frozenset(
        {(None, "real")}
    )
    # Unclosed block comment blanks through EOF (fail closed on decoys).
    unclosed = "/* helper()\nreturn decoy();\n"
    assert callees._callee_refs_from_file_line(unclosed, 2, path="src/mod.js") == frozenset()
    # Nested block-comment decoy inside a retained template interpolation.
    assert (
        callees._callee_refs_from_anchor_line(
            "    message = `x ${/* helper() */} y`", path="src/mod.ts"
        )
        == frozenset()
    )


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
def test_diff_hunk_overlaps_rejects_module_insert_after_definition_end() -> None:
    """Pure insert at/after the last body line is outside unless indent continues.

    Git emits ``@@ -N,0 +…`` for inserts after old line N. When N is the last
    body line or a trailing span gap, module-level additions must not satisfy
    call-site→definition FIXED evidence (PRRT_kwDOSJAM6s6dUMC7).
    """
    text = "def helper():\n    return 1\n\ndef reviewed():\n    return helper()\n"
    helper_span = callees._resolve_callee_definition_span(
        text, call_line=5, qualifier=None, name="helper"
    )
    assert helper_span == (1, 3)
    start, end = helper_span
    # Insert after trailing blank (old_start == end).
    module_after_end = "@@ -3,0 +4,1 @@\n+UNRELATED = 1\n"
    assert ancestry._diff_hunk_overlaps_line_span(module_after_end, start, end) is False
    assert (
        ancestry._diff_hunk_overlaps_line_span(module_after_end, start, end, file_text=text)
        is False
    )
    # Insert after last body line (before trailing span blank) is also outside.
    module_after_body = "@@ -2,0 +3,1 @@\n+UNRELATED = 1\n"
    assert (
        ancestry._diff_hunk_overlaps_line_span(module_after_body, start, end, file_text=text)
        is False
    )
    # Blank-only boundary insert also fails closed.
    assert (
        ancestry._diff_hunk_overlaps_line_span("@@ -3,0 +4,1 @@\n+\n", start, end, file_text=text)
        is False
    )
    # Indented body continuation after the last body line does overlap.
    body_cont = "@@ -2,0 +3,1 @@\n+    more = 1\n"
    assert ancestry._diff_hunk_overlaps_line_span(body_cont, start, end, file_text=text) is True
    assert (
        ancestry._diff_hunk_overlaps_line_span(
            "@@ -3,0 +4,1 @@\n+    more = 1\n", start, end, file_text=text
        )
        is True
    )


@pytest.mark.unit
def test_diff_hunk_overlaps_rejects_insert_after_brace_closer() -> None:
    """Insert after a same-indent brace closer is outside the definition body."""
    text = "function helper() {\n  return 1;\n}\n\nfunction reviewed() {\n  return helper();\n}\n"
    helper_span = callees._resolve_callee_definition_span(
        text, call_line=6, qualifier=None, name="helper", path="src/mod.js"
    )
    assert helper_span == (1, 4)
    start, end = helper_span
    after_close = "@@ -3,0 +4,1 @@\n+const decoy = 1;\n"
    assert ancestry._diff_hunk_overlaps_line_span(after_close, start, end, file_text=text) is False
    # Trailing-gap insert (old_start == end) after the closer is also outside.
    assert (
        ancestry._diff_hunk_overlaps_line_span(
            "@@ -4,0 +5,1 @@\n+const decoy = 1;\n", start, end, file_text=text
        )
        is False
    )
    # Even indented junk after ``}`` is outside the closed block.
    assert (
        ancestry._diff_hunk_overlaps_line_span(
            "@@ -3,0 +4,1 @@\n+  still_outside = 1;\n", start, end, file_text=text
        )
        is False
    )
    # Insert before the closer (after a body line) still overlaps.
    assert (
        ancestry._diff_hunk_overlaps_line_span(
            "@@ -2,0 +3,1 @@\n+  extra = 1;\n", start, end, file_text=text
        )
        is True
    )


@pytest.mark.unit
def test_pure_insert_overlaps_definition_span_edge_bounds() -> None:
    """Invalid spans, gap-only bodies, mid-body inserts, and out-of-range fail closed."""
    text = "def helper():\n    a = 1\n    return 2\n\ndef reviewed():\n    return helper()\n"
    # Without file_text: only strict interior inserts count.
    assert (
        ancestry._pure_insert_overlaps_definition_span(
            None, start=1, end=3, old_start=2, added_lines=["    x = 1"]
        )
        is True
    )
    assert (
        ancestry._pure_insert_overlaps_definition_span(
            None, start=1, end=3, old_start=3, added_lines=["x = 1"]
        )
        is False
    )
    # Out-of-bounds start/end with file_text.
    assert (
        ancestry._pure_insert_overlaps_definition_span(
            text, start=0, end=3, old_start=1, added_lines=["    x = 1"]
        )
        is False
    )
    assert (
        ancestry._pure_insert_overlaps_definition_span(
            text, start=1, end=99, old_start=1, added_lines=["    x = 1"]
        )
        is False
    )
    # Span whose lines are only ignorable gaps has no last_content.
    gap_only = "def helper():\n\n\n"
    assert (
        ancestry._pure_insert_overlaps_definition_span(
            gap_only, start=2, end=3, old_start=2, added_lines=["    x = 1"]
        )
        is False
    )
    # Insert before the last non-gap body line overlaps without needing deeper indent.
    assert (
        ancestry._pure_insert_overlaps_definition_span(
            text, start=1, end=4, old_start=1, added_lines=["UNRELATED = 1"]
        )
        is True
    )
    # Insert before the definition start is outside.
    assert (
        ancestry._pure_insert_overlaps_definition_span(
            text, start=1, end=4, old_start=0, added_lines=["    x = 1"]
        )
        is False
    )


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
async def test_diff_changes_referenced_definition_ignores_multiline_string_decoy(
    tmp_path: Path,
) -> None:
    """Docstring decoy helper() must not link an unrelated helper() body edit."""
    file_text = (
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def reviewed():\n"
        '    """\n'
        "    Call helper() when ready.\n"
        '    """\n'
        "    return 1\n"
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=6,
            diff_text="@@ -2,1 +2,1 @@\n-    return 1\n+    return 2\n",
            file_text=file_text,
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
    # file_text provided; no git show required for the happy path.
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
    # Module-level insert immediately after helper's last span line is not FIXED.
    assert (
        await ancestry._diff_changes_referenced_definition(
            runner,
            worktree_path=tmp_path,
            left="HEAD",
            path="src/x.py",
            line=5,
            diff_text="@@ -3,0 +4,1 @@\n+UNRELATED = 1\n",
            file_text=file_text,
        )
        is False
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
