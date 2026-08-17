"""Coverage edges for call-scan / f-string / control-flow presence helpers."""

from __future__ import annotations

import pytest

_MEMBER_GUARD_SALVAGE = "guard = Guard()\nguard.enable()\n"


@pytest.mark.unit
def test_js_template_interpolation_masks_comments_regex_and_keeps_real_calls() -> None:
    """``//`` / ``/* */`` / regex inside ``${...}`` must not invent tip-extra calls.

    Real interpolations still supersede; regex-only bodies stay non-executable
    (PRRT_kwDOSJAM6s6ZtJG8 call-scan edges).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
        _call_site_names_for_line,
        _find_js_template_interpolation_end,
    )

    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "const x = `${guard.disable() // other.call()}`;\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "const x = `${/* other.call() */ guard.disable()}`;\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "const x = `${/guard.disable()/}`;\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "const x = `${1 / guard.disable() / 2}`;\n",
    )
    assert _call_site_names_for_line("const x = `${guard.disable() // other.call()}`") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("const x = `${/guard.disable()/}`") == ()
    assert _call_site_names_for_line("const x = `${1 / guard.disable() / 2}`") == (
        "guard",
        "guard.disable",
    )
    # Unclosed block comment blanks through end of the interpolation body.
    assert _find_js_template_interpolation_end("/* unclosed", 0) == len("/* unclosed")
    assert _find_js_template_interpolation_end("x // c}", 0) == 7
    assert _find_js_template_interpolation_end("1 / 2}", 0) == 5
    assert _find_js_template_interpolation_end("a /= b}", 0) == 6


@pytest.mark.unit
def test_python_fstring_field_edges_mask_static_and_keep_nested_calls() -> None:
    """Nested strings, ``#`` comments, and ``{{`` / ``}}`` stay non-call static text.

    Replacement fields that still contain a real call supersede salvage; identifier
    prefixes such as ``xf\"...\"`` are not f-strings (PRRT_kwDOSJAM6s6Zt7Go).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
        _call_site_names_for_line,
        _find_py_fstring_expr_end,
        _py_fstring_prefix_len,
        _skip_py_fstring,
        _skip_py_triple_quoted_string,
    )

    assert _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + 'marker = f"{{guard.disable()}}"\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + 'marker = f"guard.disable()}}"\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + 'marker = f"{"""guard.disable()"""}"\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "marker = f\"{'guard.disable()'}\"\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + 'marker = f"{guard.disable() # other.call()}"\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + 'marker = f"{("""x"""); guard.disable()}"\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "marker = f\"{s = 'hi\\''; guard.disable()}\"\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + 'marker = f"{s = \\"hi\\"; guard.disable()}"\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "marker = f\"{f'{guard.disable()}'}\"\n",
    )
    assert _call_site_names_for_line('marker = f"{"""guard.disable()"""}"') == ()
    assert _call_site_names_for_line('marker = f"{guard.disable() # other.call()}"') == (
        "guard",
        "guard.disable",
    )
    # ``xf`` continues an identifier — not an f-string prefix.
    assert _py_fstring_prefix_len('xf"', 2) == 0
    assert _py_fstring_prefix_len('af"', 2) == 0
    assert _py_fstring_prefix_len('Fr"', 2) == 2
    # Nested skip helpers: escapes, unclosed triples, and prefix without a quote.
    assert _skip_py_triple_quoted_string('"""ab\\yc"""', 0) == 11
    assert _skip_py_triple_quoted_string('"""abc', 0) == 6
    assert _skip_py_fstring("f", 0) == 1
    assert _skip_py_fstring(r'f"a\nb"', 0) == 7
    assert _skip_py_fstring(r"f'a\'b'", 0) == 7
    assert _skip_py_fstring('f"a{{b}}c"', 0) == 10
    assert _skip_py_fstring('f"a}}b"', 0) == 7
    assert _skip_py_fstring('f"a}b"', 0) == 6
    assert _skip_py_fstring("f'{x}'", 0) == 6
    # Unclosed quote / field exit the skip helpers without an early return.
    assert _skip_py_fstring('f"abc', 0) == 5
    assert _skip_py_fstring('f"{x', 0) == 4
    assert _find_py_fstring_expr_end("x # c}", 0) == 6
    assert _find_py_fstring_expr_end(r'"a\"b" + y}', 0) == 10
    assert _find_py_fstring_expr_end(r"'a\'b' + y}", 0) == 10
    assert _find_py_fstring_expr_end('"""inner""" + y}', 0) == 15
    assert _find_py_fstring_expr_end("'''inner''' + y}", 0) == 15
    assert _find_py_fstring_expr_end('f"{inner}" + y}', 0) == 14
    assert _find_py_fstring_expr_end("f'{inner}' + y}", 0) == 14
    # Nested braces inside a field deepen then close.
    assert _find_py_fstring_expr_end("a[{b}] + y}", 0) == 10
    # Lone closing brace / escape blanking in static f-string text.
    assert _call_site_names_for_line(r'f"a\nb{guard.disable()}"') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('f"guard.disable()}"') == ()


@pytest.mark.unit
def test_member_call_continuation_skips_blanks_comments_and_stops_at_semicolon() -> None:
    """Blank / ``//`` / ``#`` lines join; a prior ``;`` statement does not."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
        _is_member_call_continuation,
        _join_member_call_continuation_line,
    )

    assert _is_member_call_continuation('["disable"]();')
    assert _is_member_call_continuation('["disable"]?.();')
    assert not _is_member_call_continuation("[incomplete")
    assert not _is_member_call_continuation("[logging]")

    assert (
        _join_member_call_continuation_line(["guard", "", "  .disable();"], 2) == "guard.disable();"
    )
    assert (
        _join_member_call_continuation_line(["guard", "  // note", "  .disable();"], 2)
        == "guard.disable();"
    )
    assert (
        _join_member_call_continuation_line(["guard", "  # note", "  .disable();"], 2)
        == "guard.disable();"
    )
    assert _join_member_call_continuation_line(["other();", "  .disable();"], 1) == ".disable();"
    assert _join_member_call_continuation_line(["  .disable();"], 0) == ".disable();"
    # Non-continuation lines return unchanged; nested continuations join through.
    assert _join_member_call_continuation_line(["guard.disable();"], 0) == "guard.disable();"
    assert (
        _join_member_call_continuation_line(["guard", "  .foo", "  .disable();"], 2)
        == "guard.foo.disable();"
    )

    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "guard\n\n  .disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "guard\n  // note\n  .disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_MEMBER_GUARD_SALVAGE,
        head_blob=_MEMBER_GUARD_SALVAGE + "guard\n  # note\n  .disable()\n",
    )


@pytest.mark.unit
def test_decorator_prefix_rejects_added_salvage_suffix() -> None:
    """Prepended ``@decorator`` must not retain a salvaged def as an exact suffix.

    A descendant can keep the salvage blob byte-identical while wrapping the
    declaration so the fix never runs (PRRT_kwDOSJAM6s6ZwrnM).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
        _prefix_opens_control_flow_over_suffix,
    )

    salvage = "def enable_guard():\n    FEATURE_ENABLED = True\n"
    assert _prefix_opens_control_flow_over_suffix("@no_op\n") is True
    assert _prefix_opens_control_flow_over_suffix("@a\n@b\n") is True
    assert _prefix_opens_control_flow_over_suffix("@no_op\n\n") is True
    assert _prefix_opens_control_flow_over_suffix("@no_op\n# note\n") is True
    assert _prefix_opens_control_flow_over_suffix("@functools.lru_cache(maxsize=None)\n") is True
    # Call-form argument continuations must keep the decorator open until the
    # decorated def/class (PRRT_kwDOSJAM6s6ZxeRW).
    assert _prefix_opens_control_flow_over_suffix("@dec(\n  x=1\n)\n") is True
    assert (
        _prefix_opens_control_flow_over_suffix("@functools.lru_cache(\n  maxsize=None\n)\n") is True
    )
    assert _prefix_opens_control_flow_over_suffix("@a(\n  1\n)\n@b\n") is True
    # Decorator already applied to a declaration in the prefix is not open.
    assert _prefix_opens_control_flow_over_suffix("@no_op\ndef helper():\n    return 1\n") is False
    assert (
        _prefix_opens_control_flow_over_suffix("@dec(\n  x=1\n)\ndef helper():\n    return 1\n")
        is False
    )

    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="@no_op\n" + salvage,
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="@dec(\n  x=1\n)\n" + salvage,
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="@a\n@b\n" + salvage,
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="@no_op\n\n" + salvage,
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="@no_op\n# note\n" + salvage,
    )
    # Consumed decorator before an unrelated body does not block suffix retention.
    call_salvage = "enable_guard()\n"
    assert _added_salvage_blob_retained(
        commit_blob=call_salvage,
        head_blob="@no_op\ndef helper():\n    return 1\n" + call_salvage,
    )


@pytest.mark.unit
def test_control_flow_header_effect_paren_depth_and_brace_tails() -> None:
    """Paren headers report open depth, awaiting-body, or brace-closed effects."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
        _control_flow_header_effect,
        _prefix_opens_control_flow_over_suffix,
    )

    assert _control_flow_header_effect("if (a") == (False, 1)
    assert _control_flow_header_effect("while (x") == (False, 1)
    assert _control_flow_header_effect("if (false)") == (True, None)
    assert _control_flow_header_effect("for (;;)") == (True, None)
    assert _control_flow_header_effect("else if (x)") == (True, None)
    assert _control_flow_header_effect("catch (e)") == (True, None)
    assert _control_flow_header_effect("if (a) {") == (False, None)
    assert _control_flow_header_effect("switch (x) {") == (False, None)
    assert _control_flow_header_effect("else") == (True, None)
    # Neither bare nor paren keyword — fall through to the neutral effect.
    assert _control_flow_header_effect("foobar (x)") == (False, None)
    assert _control_flow_header_effect("return x") == (False, None)
    # Multi-line open paren then closer with brace keeps the prefix "open".
    assert _prefix_opens_control_flow_over_suffix("if (\nfalse\n) {\n") is True
    assert _prefix_opens_control_flow_over_suffix("if (\nfalse\n)\n") is True


@pytest.mark.unit
def test_binding_span_keeps_interior_blanks_and_drops_trailing_blanks() -> None:
    """Binding spans continue through blank body lines then trim trailing blanks."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _binding_span_at,
        _binding_span_end_exclusive,
    )

    lines = [
        "def enable_guard():",
        "    setup()",
        "",
        "    finish()",
        "",
        "",
        "def other():",
        "    pass",
    ]
    assert _binding_span_end_exclusive(lines, 0) == 4
    assert _binding_span_at(lines, 0) == (
        "def enable_guard():",
        "    setup()",
        "",
        "    finish()",
    )


@pytest.mark.unit
def test_comment_only_lines_yield_no_unset_update_or_setattr_bindings() -> None:
    """``#`` / ``//`` lines must not bind unset / ``++`` / setattr mutations."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
        _binding_name_for_line,
        _binding_names_for_line,
        _del_binding_names,
        _object_assign_mutation_binding_names,
        _object_define_property_mutation_binding_names,
        _setattr_mutation_binding_names,
        _setitem_mutation_binding_names,
        _unset_binding_names,
        _update_expr_binding_names,
        _update_mutation_binding_names,
    )

    assert _unset_binding_names("# unset FEATURE_ENABLED") == ()
    assert _unset_binding_names("// unset FEATURE_ENABLED") == ()
    assert _update_expr_binding_names("# retryBudget++") == ()
    assert _update_expr_binding_names("// retryBudget--") == ()
    assert _setattr_mutation_binding_names('# setattr(guard, "enabled", False)') == ()
    assert _setattr_mutation_binding_names('// setattr(guard, "enabled", False)') == ()
    assert _setitem_mutation_binding_names('# FLAGS.__setitem__("enabled", False)') == ()
    assert _setitem_mutation_binding_names('// FLAGS.__setitem__("enabled", False)') == ()
    assert _update_mutation_binding_names("# FLAGS.update(enabled=False)") == ()
    assert _update_mutation_binding_names("// FLAGS.update(enabled=False)") == ()
    assert _object_assign_mutation_binding_names("// Object.assign(guard, {enabled: false})") == ()
    assert _object_assign_mutation_binding_names("# Object.assign(guard, {enabled: false})") == ()
    assert (
        _object_define_property_mutation_binding_names(
            '// Object.defineProperty(guard, "enabled", {value: false})'
        )
        == ()
    )
    assert (
        _object_define_property_mutation_binding_names(
            '# Object.defineProperty(guard, "enabled", {value: false})'
        )
        == ()
    )
    assert _del_binding_names("# del guard.enabled") == ()
    assert _del_binding_names("// delete guard.enabled") == ()
    # ``#undef`` is a preprocessor binding (like ``#define``), not a comment
    # (PRRT_kwDOSJAM6s6ZyImI). Other ``#`` directives stay unbound.
    assert _binding_name_for_line("#undef FEATURE_ENABLED") == "FEATURE_ENABLED"
    assert _binding_name_for_line("# undef FEATURE_ENABLED") == "FEATURE_ENABLED"
    assert _binding_names_for_line("#undef FEATURE_ENABLED") == ("FEATURE_ENABLED",)
    assert _binding_name_for_line("#ifdef FEATURE_ENABLED") is None
    assert _binding_name_for_line("# not-a-define") is None


@pytest.mark.unit
def test_object_assign_mutation_binding_names() -> None:
    """``Object.assign`` synthesizes ``target.key`` bindings (PRRT_kwDOSJAM6s6Zxwhs)."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
        _join_incomplete_object_assign_line,
        _object_assign_call_unclosed,
        _object_assign_mutation_args_fully_synthesizable,
        _object_assign_mutation_binding_names,
    )

    assert _object_assign_mutation_binding_names("Object.assign(guard, {enabled: false})") == (
        "guard.enabled",
    )
    assert _object_assign_mutation_binding_names('Object.assign(guard, {"enabled": false})') == (
        "guard.enabled",
    )
    assert _object_assign_mutation_binding_names("Object.assign(guard, {'enabled': false})") == (
        "guard.enabled",
    )
    assert _object_assign_mutation_binding_names("Object.assign(guard, {enabled})") == (
        "guard.enabled",
    )
    assert _object_assign_mutation_binding_names(
        "Object.assign(guard, {enabled: false, other: true})"
    ) == ("guard.enabled", "guard.other")
    assert _object_assign_mutation_binding_names(
        "globalThis.Object.assign(guard, {enabled: false})"
    ) == ("guard.enabled",)
    assert _object_assign_mutation_binding_names("Object.assign(guard, other)") == ()
    assert _object_assign_mutation_binding_names("Object.assign(guard, {enabled: false") == ()
    assert (
        _object_assign_mutation_binding_names('msg = "Object.assign(guard, {enabled: false})"')
        == ()
    )
    assert _object_assign_mutation_binding_names(
        "Object.assign(guard, {other: false}, {enabled: true})"
    ) == ("guard.other", "guard.enabled")
    assert _object_assign_mutation_args_fully_synthesizable(
        "Object.assign(guard, {enabled: false})"
    )
    assert not _object_assign_mutation_args_fully_synthesizable("Object.assign(guard, other)")
    assert not _object_assign_mutation_args_fully_synthesizable(
        "Object.assign(guard, {enabled: false}, extra)"
    )
    assert not _object_assign_mutation_args_fully_synthesizable("Object.assign(guard, {...other})")
    assert not _object_assign_mutation_args_fully_synthesizable(
        "Object.assign(guard, {[k]: false})"
    )
    # Multiline argument lists join before mutation scanning (PRRT_kwDOSJAM6s6Zyo4_).
    multi = [
        "Object.assign(",
        "  guard,",
        "  {enabled: false}",
        ");",
    ]
    assert _object_assign_call_unclosed(multi[0])
    joined = _join_incomplete_object_assign_line(multi, 0)
    assert not _object_assign_call_unclosed(joined)
    assert _object_assign_mutation_binding_names(joined) == ("guard.enabled",)
    # Tip-extra argument lines must look back to a shared opener (PRRT_kwDOSJAM6s6Zy5DN).
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
        _join_incomplete_object_mutation_line_covering,
    )

    covered = _join_incomplete_object_mutation_line_covering(multi, 2)
    assert _object_assign_mutation_binding_names(covered) == ("guard.enabled",)
    assert _join_incomplete_object_mutation_line_covering(["foo = 1"], 0) == "foo = 1"


@pytest.mark.unit
def test_opaque_object_assign_shares_salvaged_receiver() -> None:
    """Opaque ``Object.assign`` fail-closed on salvaged attribute receivers."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
        _tip_extra_opaque_object_assign_shares_receiver,
    )

    salvage = "guard.enabled = true\n"
    assert _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "Object.assign(guard, other)\n",
        candidate_keys={"guard.enabled"},
    )
    assert _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "Object.assign(guard, {other: false}, extra)\n",
        candidate_keys={"guard.enabled"},
    )
    assert not _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "Object.assign(guard, {enabled: false})\n",
        candidate_keys={"guard.enabled"},
    )
    assert not _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "Object.assign(other, extra)\n",
        candidate_keys={"guard.enabled"},
    )
    assert not _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "// Object.assign(guard, other)\n",
        candidate_keys={"guard.enabled"},
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "Object.assign(guard, {other: false})\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "Object.assign(guard, other)\n",
    )
    # Dotted assign targets are full ``_SETATTR_OBJ`` paths; root-only alias
    # refs would miss ``Object.assign(config.guard, other)`` vs salvaged
    # ``config.guard.enabled`` and retain stale FIXED evidence
    # (PRRT_kwDOSJAM6s6ZyGqh).
    nested = "config.guard.enabled = true\n"
    assert _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=nested,
        head_blob=nested + "Object.assign(config.guard, other)\n",
        candidate_keys={"config.guard.enabled"},
    )
    assert not _added_salvage_blob_retained(
        commit_blob=nested,
        head_blob=nested + "Object.assign(config.guard, other)\n",
    )
    # Multiline ``Object.assign(\n  guard,\n  …)`` must join before scanning;
    # per-line targets miss the opener and leave no ``guard.enabled`` mutation
    # on continuations (PRRT_kwDOSJAM6s6Zyo4_).
    assert _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "Object.assign(\n  guard,\n  other\n);\n",
        candidate_keys={"guard.enabled"},
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "Object.assign(\n  guard,\n  other\n);\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "Object.assign(\n  guard,\n  {enabled: false}\n);\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "Object.assign(\n  other,\n  extra\n);\n",
    )
    # Shared opener: tip-extra is only the opaque source line (PRRT_kwDOSJAM6s6Zy5DN).
    shared_opener_baseline = "Object.assign(\n  guard,\n  {enabled: true}\n);\n"
    assert _tip_extra_opaque_object_assign_shares_receiver(
        baseline_blob=shared_opener_baseline,
        head_blob="Object.assign(\n  guard,\n  other\n);\n",
        candidate_keys={"guard.enabled"},
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _tip_extra_keys_supersede_baseline,
    )

    assert _tip_extra_keys_supersede_baseline(
        baseline_blob=shared_opener_baseline,
        head_blob="Object.assign(\n  guard,\n  {enabled: false}\n);\n",
        candidate_keys={"guard.enabled"},
    )


@pytest.mark.unit
def test_object_define_property_mutation_binding_names() -> None:
    """``Object.defineProperty`` synthesizes ``target.key`` (PRRT_kwDOSJAM6s6Zy4pR)."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
        _join_incomplete_object_define_property_line,
        _object_define_property_call_unclosed,
        _object_define_property_mutation_args_fully_synthesizable,
        _object_define_property_mutation_binding_names,
    )

    assert _object_define_property_mutation_binding_names(
        'Object.defineProperty(guard, "enabled", {value: false})'
    ) == ("guard.enabled",)
    assert _object_define_property_mutation_binding_names(
        "Object.defineProperty(guard, 'enabled', {value: false})"
    ) == ("guard.enabled",)
    assert _object_define_property_mutation_binding_names(
        'globalThis.Object.defineProperty(guard, "enabled", {value: false})'
    ) == ("guard.enabled",)
    assert (
        _object_define_property_mutation_binding_names(
            "Object.defineProperty(guard, key, {value: false})"
        )
        == ()
    )
    assert (
        _object_define_property_mutation_binding_names(
            'Object.defineProperty(guard, "enabled", {value: false'
        )
        == ()
    )
    assert (
        _object_define_property_mutation_binding_names(
            'msg = "Object.defineProperty(guard, \\"enabled\\", {value: false})"'
        )
        == ()
    )
    assert _object_define_property_mutation_args_fully_synthesizable(
        'Object.defineProperty(guard, "enabled", {value: false})'
    )
    assert not _object_define_property_mutation_args_fully_synthesizable(
        "Object.defineProperty(guard, key, {value: false})"
    )
    multi = [
        "Object.defineProperty(",
        "  guard,",
        '  "enabled",',
        "  {value: false}",
        ");",
    ]
    assert _object_define_property_call_unclosed(multi[0])
    joined = _join_incomplete_object_define_property_line(multi, 0)
    assert not _object_define_property_call_unclosed(joined)
    assert _object_define_property_mutation_binding_names(joined) == ("guard.enabled",)


@pytest.mark.unit
def test_opaque_object_define_property_shares_salvaged_receiver() -> None:
    """Opaque ``Object.defineProperty`` fail-closed on salvaged receivers."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
        _tip_extra_opaque_object_define_property_shares_receiver,
    )

    salvage = "guard.enabled = true\n"
    assert _tip_extra_opaque_object_define_property_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "Object.defineProperty(guard, key, {value: false})\n",
        candidate_keys={"guard.enabled"},
    )
    assert not _tip_extra_opaque_object_define_property_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + 'Object.defineProperty(guard, "enabled", {value: false})\n',
        candidate_keys={"guard.enabled"},
    )
    assert not _tip_extra_opaque_object_define_property_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "Object.defineProperty(other, key, {value: false})\n",
        candidate_keys={"guard.enabled"},
    )
    assert not _tip_extra_opaque_object_define_property_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "// Object.defineProperty(guard, key, {value: false})\n",
        candidate_keys={"guard.enabled"},
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "Object.defineProperty(guard, key, {value: false})\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + 'Object.defineProperty(guard, "enabled", {value: false})\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + 'Object.defineProperty(guard, "other", {value: false})\n',
    )
    nested = "config.guard.enabled = true\n"
    assert _tip_extra_opaque_object_define_property_shares_receiver(
        baseline_blob=nested,
        head_blob=nested + "Object.defineProperty(config.guard, key, {value: false})\n",
        candidate_keys={"config.guard.enabled"},
    )
    assert not _added_salvage_blob_retained(
        commit_blob=nested,
        head_blob=nested + "Object.defineProperty(config.guard, key, {value: false})\n",
    )
    assert _tip_extra_opaque_object_define_property_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "Object.defineProperty(\n  guard,\n  key,\n  {value: false}\n);\n",
        candidate_keys={"guard.enabled"},
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "Object.defineProperty(\n  guard,\n  key,\n  {value: false}\n);\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=(
            salvage + 'Object.defineProperty(\n  guard,\n  "enabled",\n  {value: false}\n);\n'
        ),
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "Object.defineProperty(\n  other,\n  key,\n  {value: false}\n);\n",
    )


@pytest.mark.unit
def test_setitem_and_update_mutation_binding_names() -> None:
    """Collection helpers synthesize ``obj["key"]`` bindings (PRRT_kwDOSJAM6s6ZwrnH)."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
        _helper_keyword_executable,
        _setitem_mutation_binding_names,
        _split_top_level_call_args,
        _update_arg_fully_synthesizable,
        _update_call_argument_span,
        _update_mutation_args_fully_synthesizable,
        _update_mutation_binding_names,
    )

    assert _setitem_mutation_binding_names('FLAGS.__setitem__("enabled", False)') == (
        'FLAGS["enabled"]',
    )
    assert _setitem_mutation_binding_names("FLAGS.__delitem__('enabled')") == ('FLAGS["enabled"]',)
    assert _setitem_mutation_binding_names('dict.__setitem__(FLAGS, "enabled", False)') == (
        'FLAGS["enabled"]',
    )
    assert _setitem_mutation_binding_names(
        'FLAGS.__setitem__("enabled", False); FLAGS.__setitem__("enabled", True)'
    ) == ('FLAGS["enabled"]',)
    assert _setitem_mutation_binding_names('msg = "FLAGS.__setitem__(\\"enabled\\", False)"') == ()
    assert _update_mutation_binding_names("FLAGS.update(enabled=False, other=True)") == (
        'FLAGS["enabled"]',
        'FLAGS["other"]',
    )
    assert _update_mutation_binding_names('FLAGS.update({"enabled": False})') == (
        'FLAGS["enabled"]',
    )
    assert _update_mutation_binding_names("FLAGS.update({'enabled': False})") == (
        'FLAGS["enabled"]',
    )
    assert _update_mutation_binding_names('FLAGS.update({"enabled": False, "enabled": True})') == (
        'FLAGS["enabled"]',
    )
    assert _update_mutation_binding_names("FLAGS.update(other_flags)") == ()
    assert _update_mutation_binding_names("FLAGS.update(enabled=False") == ()
    assert _update_mutation_binding_names('msg = "FLAGS.update(enabled=False)"') == ()
    # Mixed opaque+kwargs still synthesize the kwarg key; opacity is checked
    # separately so salvage cannot fail open (PRRT_kwDOSJAM6s6Zxt0p).
    assert _update_mutation_binding_names("FLAGS.update(other_flags, other=False)") == (
        'FLAGS["other"]',
    )
    assert _update_mutation_args_fully_synthesizable("FLAGS.update(other=False)")
    assert _update_mutation_args_fully_synthesizable('FLAGS.update({"other": False})')
    assert _update_mutation_args_fully_synthesizable('FLAGS.update({"other": False}, x=1)')
    assert _update_mutation_args_fully_synthesizable("FLAGS.update({})")
    assert _update_mutation_args_fully_synthesizable("FLAGS.update(other=False,)")
    assert _update_mutation_args_fully_synthesizable('FLAGS.update({"a": "x,y", "b": 1})')
    assert _update_mutation_args_fully_synthesizable("FLAGS.update(other=f(1, 2))")
    assert _update_mutation_args_fully_synthesizable("FLAGS.update(other=xs[0, 1])")
    assert _update_mutation_args_fully_synthesizable('FLAGS.update(other="a\\"b,c")')
    assert _update_mutation_args_fully_synthesizable("FLAGS.update(other='a\\'b,c')")
    assert _split_top_level_call_args("a=1,,b=2") == ["a=1", "b=2"]
    assert _split_top_level_call_args("") == []
    assert _update_arg_fully_synthesizable("")
    assert _update_arg_fully_synthesizable("   ")
    assert not _update_mutation_args_fully_synthesizable("FLAGS.update(other_flags)")
    assert not _update_mutation_args_fully_synthesizable("FLAGS.update(other_flags, other=False)")
    assert not _update_mutation_args_fully_synthesizable(
        'FLAGS.update(other_flags | {"other": False})'
    )
    assert not _update_mutation_args_fully_synthesizable(
        'FLAGS.update({**other_flags, "other": False})'
    )
    assert not _update_mutation_args_fully_synthesizable("FLAGS.update(**other_flags)")
    assert not _update_mutation_args_fully_synthesizable("FLAGS.update({enabled: False})")
    assert not _update_mutation_args_fully_synthesizable("# FLAGS.update(other=False)")
    assert not _update_mutation_args_fully_synthesizable("// FLAGS.update(other=False)")
    assert not _update_mutation_args_fully_synthesizable("FLAGS.update(other=False")
    assert not _update_mutation_args_fully_synthesizable('msg = "FLAGS.update(other=False)"')
    assert _update_call_argument_span("FLAGS.update(enabled=False)", 12) == "enabled=False"
    assert _update_call_argument_span("FLAGS.update(enabled=False", 12) is None
    assert (
        _helper_keyword_executable(
            raw_line="FLAGS.__setitem__",
            scan="FLAGS.__setitem__",
            match_start=0,
            tokens=("__setitem__",),
        )
        is False
    )


@pytest.mark.unit
def test_opaque_collection_mutator_shares_salvaged_subscript_receiver() -> None:
    """Opaque ``update`` / ``clear`` fail closed on salvaged receivers."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
        _tip_extra_opaque_collection_mutator_shares_receiver,
    )

    salvage = 'FLAGS["enabled"] = True\n'
    assert _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "FLAGS.clear()\n",
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "FLAGS.update(other_flags)\n",
        candidate_keys={'FLAGS["enabled"]'},
    )
    # Kwargs / dict-literal ``update`` synthesize keys; do not treat as opaque
    # so unrelated keys keep salvage like ``__setitem__("other", …)``
    # (PRRT_kwDOSJAM6s6ZxeRb).
    assert not _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "FLAGS.update(other=False)\n",
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert not _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + 'FLAGS.update({"other": False})\n',
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert not _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + 'FLAGS.update({"other": False}, x=1)\n',
        candidate_keys={'FLAGS["enabled"]'},
    )
    # Partially opaque ``update`` must still fail closed even when a kwarg or
    # nested dict key synthesizes an unrelated binding (PRRT_kwDOSJAM6s6Zxt0p).
    assert _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "FLAGS.update(other_flags, other=False)\n",
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + 'FLAGS.update(other_flags | {"other": False})\n',
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + 'FLAGS.update({**other_flags, "other": False})\n',
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "FLAGS.update(other=False)\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + 'FLAGS.update({"other": False})\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "FLAGS.update(enabled=False)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + "FLAGS.update(other_flags, other=False)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob=salvage + 'FLAGS.update({**other_flags, "other": False})\n',
    )
    assert not _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "FLAGS.clear()\n",
        candidate_keys=set(),
    )
    assert not _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage,
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert not _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "FLAGS.copy()\n",
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert not _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "OTHER.clear()\n",
        candidate_keys={'FLAGS["enabled"]'},
    )
    assert not _tip_extra_opaque_collection_mutator_shares_receiver(
        baseline_blob=salvage,
        head_blob=salvage + "\n",
        candidate_keys={'FLAGS["enabled"]'},
    )


@pytest.mark.unit
def test_tip_extra_alias_of_salvaged_guard_supersedes() -> None:
    """Tip-extra ``alias = guard`` then ``alias.enabled = …`` must drop salvage.

    Exact-key intersection sees only ``alias`` / ``alias.enabled``, which do not
    overlap salvaged ``guard`` / ``guard.enabled``, and call checks find nothing —
    without alias tracking a no-change FIXED could reuse stale salvage after the
    effective guard was disabled (PRRT_kwDOSJAM6s6ZxHGP).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
        _suffix_can_supersede_added_salvage,
        _tip_extra_aliases_salvaged_candidate,
        _tip_extra_can_supersede_modified_salvage,
    )

    salvage = "const guard = {};\nguard.enabled = true;\n"
    head = salvage + "const alias = guard;\nalias.enabled = false;\n"
    assert _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=head,
        candidate_keys={"guard", "guard.enabled"},
    )
    assert _suffix_can_supersede_added_salvage(salvage=salvage, head_blob=head)
    assert not _added_salvage_blob_retained(commit_blob=salvage, head_blob=head)

    parent = "const guard = {};\nguard.enabled = false;\n"
    commit = "const guard = {};\nguard.enabled = true;\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=commit + "const alias = guard;\nalias.enabled = false;\n",
    )

    salvage_py = "guard = {}\nguard.enabled = True\n"
    head_py = salvage_py + "alias = guard\nalias.enabled = False\n"
    assert _suffix_can_supersede_added_salvage(salvage=salvage_py, head_blob=head_py)
    assert not _added_salvage_blob_retained(commit_blob=salvage_py, head_blob=head_py)

    # Unrelated tip-extra alias must not drop salvage.
    assert not _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=salvage + "const alias = other;\nalias.enabled = false;\n",
        candidate_keys={"guard", "guard.enabled"},
    )
    assert not _suffix_can_supersede_added_salvage(
        salvage=salvage,
        head_blob=salvage + "const alias = other;\nalias.enabled = false;\n",
    )
    # Comment / string aliasing must not supersede.
    assert not _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=salvage + "// const alias = guard;\n",
        candidate_keys={"guard", "guard.enabled"},
    )
    assert not _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=salvage + 'msg = "const alias = guard;"\n',
        candidate_keys={"guard", "guard.enabled"},
    )


@pytest.mark.unit
def test_tip_extra_multiline_alias_of_salvaged_guard_supersedes() -> None:
    """Tip-extra ``const alias =\\n  guard`` must drop salvage like same-line alias.

    Formatters split the alias assign across lines; same-line-only matching would
    miss the salvaged RHS and retain stale FIXED evidence after
    ``alias.enabled = false`` (PRRT_kwDOSJAM6s6ZxhFW).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
        _alias_assign_gap_line_is_skippable,
        _join_incomplete_alias_assign_line,
        _suffix_can_supersede_added_salvage,
        _tip_extra_aliases_salvaged_candidate,
        _tip_extra_can_supersede_modified_salvage,
    )

    salvage = "const guard = {};\nguard.enabled = true;\n"
    head = salvage + "const alias =\n  guard;\nalias.enabled = false;\n"
    assert _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=head,
        candidate_keys={"guard", "guard.enabled"},
    )
    assert _suffix_can_supersede_added_salvage(salvage=salvage, head_blob=head)
    assert not _added_salvage_blob_retained(commit_blob=salvage, head_blob=head)

    # Blank / line-comment between ``=`` and RHS still aliases.
    head_gap = salvage + "const alias =\n  // note\n  guard;\nalias.enabled = false;\n"
    assert _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=head_gap,
        candidate_keys={"guard", "guard.enabled"},
    )
    assert _suffix_can_supersede_added_salvage(salvage=salvage, head_blob=head_gap)

    # Whole-line ``/* … */`` between ``=`` and RHS must also be skipped; treating
    # the block-comment line as the RHS leaves ``const alias = /* note */``,
    # which never matches ``_TIP_EXTRA_ALIAS_ASSIGN_RE`` after comment blanking
    # and would keep stale FIXED evidence (PRRT_kwDOSJAM6s6Zxt0u).
    assert _alias_assign_gap_line_is_skippable("/* note */")
    assert not _alias_assign_gap_line_is_skippable("/* note */ guard")
    assert (
        _join_incomplete_alias_assign_line(["const alias =", "  /* note */", "  guard;"], 0)
        == "const alias = guard;"
    )
    assert (
        _join_incomplete_alias_assign_line(["const alias =", "  /* note */ guard;"], 0)
        == "const alias = /* note */ guard;"
    )
    assert (
        _join_incomplete_alias_assign_line(
            ["const alias =", "  /* note", "   * more */", "  guard;"], 0
        )
        == "const alias = guard;"
    )
    assert (
        _join_incomplete_alias_assign_line(["const alias =", "  /* note", "   */ guard;"], 0)
        == "const alias = guard;"
    )
    # Unclosed multi-line ``/*`` leaves the assign incomplete (fail-closed later).
    assert _join_incomplete_alias_assign_line(["const alias =", "  /* note"], 0) == (
        "const alias ="
    )
    head_block = salvage + "const alias =\n  /* note */\n  guard;\nalias.enabled = false;\n"
    assert _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=head_block,
        candidate_keys={"guard", "guard.enabled"},
    )
    assert _suffix_can_supersede_added_salvage(salvage=salvage, head_blob=head_block)
    head_block_multi = (
        salvage + "const alias =\n  /* note\n   * more */\n  guard;\nalias.enabled = false;\n"
    )
    assert _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=head_block_multi,
        candidate_keys={"guard", "guard.enabled"},
    )

    parent = "const guard = {};\nguard.enabled = false;\n"
    commit = "const guard = {};\nguard.enabled = true;\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=commit + "const alias =\n  guard;\nalias.enabled = false;\n",
    )

    salvage_py = "guard = {}\nguard.enabled = True\n"
    head_py = salvage_py + "alias =\n  guard\nalias.enabled = False\n"
    assert _suffix_can_supersede_added_salvage(salvage=salvage_py, head_blob=head_py)
    assert not _added_salvage_blob_retained(commit_blob=salvage_py, head_blob=head_py)

    # Unrelated multiline alias must not drop salvage.
    assert not _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=salvage + "const alias =\n  other;\nalias.enabled = false;\n",
        candidate_keys={"guard", "guard.enabled"},
    )
    assert not _suffix_can_supersede_added_salvage(
        salvage=salvage,
        head_blob=salvage + "const alias =\n  other;\nalias.enabled = false;\n",
    )
    # Incomplete RHS after look-ahead fails closed (cannot prove non-alias).
    assert _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=salvage + "const alias =\n",
        candidate_keys={"guard", "guard.enabled"},
    )
    # String / same-line complete assign must not look incomplete.
    assert not _tip_extra_aliases_salvaged_candidate(
        baseline_blob=salvage,
        head_blob=salvage + 'msg = "const alias = guard;"\n',
        candidate_keys={"guard", "guard.enabled"},
    )
