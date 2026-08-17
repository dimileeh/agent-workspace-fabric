"""Coverage edges for salvage presence / call-scan helpers (part 012)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_call_site_names_mask_triple_quotes_and_line_comments() -> None:
    """Closed ``\"\"\"`` / ``'''`` and ``//`` tails must not yield phantom calls."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
        _call_site_names_for_line,
    )

    assert _call_site_names_for_line('x = """hello""" + guard.disable()') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("x = '''hello''' + guard.disable()") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('x = """guard.disable()"""') == ()
    assert _call_site_names_for_line("guard.disable(); // other.call()") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("// guard.disable()") == ()


@pytest.mark.unit
def test_join_member_call_continuation_preserves_receiver_across_lines() -> None:
    """Multiline ``guard`` + ``.disable()`` joins before call scanning.

    Per-line scanning would emit only a bare ``disable`` leaf
    (PRRT_kwDOSJAM6s6ZuG-J). TOML ``[table]`` headers are not continuations.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
        _call_site_names_for_line,
        _is_member_call_continuation,
        _join_member_call_continuation_line,
    )

    assert _is_member_call_continuation(".disable();")
    assert _is_member_call_continuation('["disable"]();')
    assert not _is_member_call_continuation("[logging]")
    assert not _is_member_call_continuation("guard.disable();")

    lines = ["guard", "  .disable();"]
    joined = _join_member_call_continuation_line(lines, 1)
    assert joined == "guard.disable();"
    assert _call_site_names_for_line(joined) == ("guard", "guard.disable")

    nested = ["guard", "  .foo", "  .disable();"]
    assert _join_member_call_continuation_line(nested, 2) == "guard.foo.disable();"
    assert _join_member_call_continuation_line(["  .disable();"], 0) == ".disable();"


@pytest.mark.unit
def test_call_site_names_skip_definition_prefixed_paren_and_computed_forms() -> None:
    """``def`` / ``function`` / ``class`` prefixes skip paren and computed call sites.

    Without the skip, definition forms that look like ``(recv).meth()`` /
    ``recv[\"k\"]()`` would be treated as tip-extra call overrides.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
        _call_site_names_for_line,
    )

    # Paren-member / paren-computed under a definition prefix are skipped; a
    # trailing bare ``def(`` / ``function(`` leaf may still appear from the
    # plain call scanner and must not invent a receiver chain for the body.
    assert "guard" not in _call_site_names_for_line("def (guard).disable(): pass")
    assert "guard.disable" not in _call_site_names_for_line("function (guard).disable() {}")
    assert "guard" not in _call_site_names_for_line('def (guard)["disable"](): pass')
    assert _call_site_names_for_line('def guard["disable"](): pass') == ()
    assert _call_site_names_for_line('class guard["disable"](): pass') == ()


@pytest.mark.unit
def test_inline_assign_excludes_js_arrow_parameter_equals() -> None:
    """``=>`` must not bind the arrow parameter as an equals-style rebind.

    Plain ``=(?!=)`` matched the ``=`` in ``=>``, so tip
    ``const fn = FEATURE_ENABLED => false;`` recorded ``FEATURE_ENABLED`` as a
    later assignment and discarded retained FIXED salvage
    (PRRT_kwDOSJAM6s6ZtZ_2).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
        _binding_names_for_line,
        _inline_assign_binding_names,
    )

    assert _inline_assign_binding_names("const fn = FEATURE_ENABLED => false;") == ("fn",)
    assert "FEATURE_ENABLED" not in _binding_names_for_line("const fn = FEATURE_ENABLED => false;")
    assert _inline_assign_binding_names("FEATURE_ENABLED => false;") == ()
    assert _binding_names_for_line("FEATURE_ENABLED => false;") == ()
    # Real assigns and compounds still bind; ``==`` still does not.
    assert _inline_assign_binding_names("FEATURE_ENABLED = false;") == ("FEATURE_ENABLED",)
    assert _inline_assign_binding_names("FEATURE_ENABLED &= false;") == ("FEATURE_ENABLED",)
    # JS logical assigns must bind like ``&=`` (PRRT_kwDOSJAM6s6ZyImG).
    assert _inline_assign_binding_names("FEATURE_ENABLED &&= false;") == ("FEATURE_ENABLED",)
    assert _inline_assign_binding_names("FEATURE_ENABLED ||= false;") == ("FEATURE_ENABLED",)
    assert _inline_assign_binding_names("FEATURE_ENABLED ??= false;") == ("FEATURE_ENABLED",)
    assert _inline_assign_binding_names("guard.enabled &&= false;") == ("guard.enabled",)
    assert _inline_assign_binding_names("guard.enabled ||= false;") == ("guard.enabled",)
    assert _inline_assign_binding_names("guard.enabled ??= false;") == ("guard.enabled",)
    assert _binding_names_for_line("guard.enabled &&= false;") == ("guard.enabled",)
    assert _inline_assign_binding_names("FEATURE_ENABLED == false;") == ()


@pytest.mark.unit
def test_subscript_and_assign_key_normalize_edge_spellings() -> None:
    """Subscript / dotted-key normalizers keep identity across edge spellings."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_assigns import (
        _executable_call_scan_text,
        _format_normalized_assign_key_segment,
        _matching_bracket_closer_index,
        _normalize_assign_binding_name,
        _normalize_subscript_binding_name,
        _paren_depth,
        _paren_list_unpack_binding_names,
        _unpacking_lhs_names_before,
    )

    assert _normalize_subscript_binding_name("FLAGS[enabled]") == "FLAGS[enabled]"
    assert _normalize_subscript_binding_name("FLAGS[0]") == "FLAGS[0]"
    assert _normalize_subscript_binding_name("FLAGS['enabled']") == 'FLAGS["enabled"]'
    assert _format_normalized_assign_key_segment('a"b') == "'a\"b'"
    assert _normalize_assign_binding_name("a.b#c") == "a.b#c"
    assert _normalize_assign_binding_name('a."b"c') == 'a."b"c'
    assert _normalize_assign_binding_name("a.") == "a."
    assert _normalize_assign_binding_name("a..b") == "a..b"
    assert _normalize_assign_binding_name("plain") == "plain"
    assert _paren_depth(")(") == 1
    assert _unpacking_lhs_names_before("nope", raw_before="nope") == ()
    assert _matching_bracket_closer_index("x", 0) is None
    assert _matching_bracket_closer_index("(a]", 0) is None
    assert _matching_bracket_closer_index("(a, b", 0) is None
    raw = "(a, b ="
    assert _paren_list_unpack_binding_names(raw, scan=_executable_call_scan_text(raw)) == ()
    # JS object alias properties bind the value-side target only; keys must not
    # enter the binding set (PRRT_kwDOSJAM6s6Zv4pl).
    alias = "({FEATURE_ENABLED: local} = source)"
    assert _paren_list_unpack_binding_names(alias, scan=_executable_call_scan_text(alias)) == (
        "local",
    )
    shorthand = "({FEATURE_ENABLED} = source)"
    assert _paren_list_unpack_binding_names(
        shorthand, scan=_executable_call_scan_text(shorthand)
    ) == ("FEATURE_ENABLED",)
    value_target = "({a: FEATURE_ENABLED} = source)"
    assert _paren_list_unpack_binding_names(
        value_target, scan=_executable_call_scan_text(value_target)
    ) == ("FEATURE_ENABLED",)


@pytest.mark.unit
def test_control_flow_prefix_edges_retain_or_reject_added_salvage() -> None:
    """Control-flow prefix scanner covers body-clear, brace, and comment edges."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
        _delta_brackets_outside_strings,
        _line_code_without_line_comment,
        _prefix_opens_control_flow_over_suffix,
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_context import (
        _line_is_control_flow_change,
    )

    # Body line after a paren header clears awaiting_body so a later suffix retains.
    assert _prefix_opens_control_flow_over_suffix("if (false)\nsetup();\n") is False
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="if (false)\nsetup();\nenable_guard();\n",
    )
    # Header that opens a brace stays "open" until closed.
    assert _prefix_opens_control_flow_over_suffix("if (false) {\n") is True
    assert _prefix_opens_control_flow_over_suffix("else {\n") is True
    assert _prefix_opens_control_flow_over_suffix("do\n") is True
    # Multi-line paren header that closes onto ``) {`` keeps brace depth open
    # without setting awaiting_body on the closer line alone.
    assert _prefix_opens_control_flow_over_suffix("if (\nfalse\n) {\n") is True
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="else {\nsetup();\n}\nenable_guard();\n",
    )
    # Escapes / comments inside strings must not confuse bracket or comment scans.
    assert _delta_brackets_outside_strings(r'"\{" {', opens="{", closes="}") == 1
    assert _line_code_without_line_comment(r'x = "a#b" # c') == 'x = "a#b" '
    assert _line_code_without_line_comment(r"x = 'a\'b' # c") == r"x = 'a\'b' "
    # Tip-extra control-flow classifier (PRRT_kwDOSJAM6s6ZvVZK): transfers and
    # ordinary headers match; nested defs / blank comment-only lines do not.
    assert _line_is_control_flow_change("    return\n") is True
    assert _line_is_control_flow_change("    break\n") is True
    assert _line_is_control_flow_change("    continue\n") is True
    assert _line_is_control_flow_change("else {\n") is True
    assert _line_is_control_flow_change("} else\n") is True
    assert _line_is_control_flow_change("    # return\n") is False
    assert _line_is_control_flow_change("    \n") is False
    assert _line_is_control_flow_change("def helper():\n") is False
    assert _line_is_control_flow_change("    log('x')\n") is False


@pytest.mark.unit
def test_brace_continuation_control_flow_prefix_rejects_disabled_salvage() -> None:
    """``} else`` / ``} catch`` / same-line ``if {} else`` must open control-flow.

    Column-zero-only header matching missed idiomatic brace continuations, so a
    prepend could park added salvage under a disabled branch while retention
    still succeeded (PRRT_kwDOSJAM6s6ZtYk1). Inter-token ``/* … */`` between
    ``}`` and the keyword must not reopen that gap (PRRT_kwDOSJAM6s6Zt56f).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
        _prefix_opens_control_flow_over_suffix,
    )

    assert _prefix_opens_control_flow_over_suffix("if (false) {\n} else\n") is True
    assert _prefix_opens_control_flow_over_suffix("if (false) {} else\n") is True
    assert _prefix_opens_control_flow_over_suffix("if (false) {\n  x();\n} else\n") is True
    assert _prefix_opens_control_flow_over_suffix("try {\n} catch (e)\n") is True
    assert _prefix_opens_control_flow_over_suffix("try {\n} finally\n") is True
    assert _prefix_opens_control_flow_over_suffix("if (a) {\n} else if (false)\n") is True
    assert _prefix_opens_control_flow_over_suffix("if (\nfalse\n) {} else\n") is True
    # Inter-token block comments are still brace continuations (PRRT_kwDOSJAM6s6Zt56f).
    assert _prefix_opens_control_flow_over_suffix("if (false) {\n} /* */ else\n") is True
    assert _prefix_opens_control_flow_over_suffix("if (false) {}/*x*/else\n") is True
    assert _prefix_opens_control_flow_over_suffix("try {\n} /* a */ /* b */ catch (e)\n") is True
    assert _prefix_opens_control_flow_over_suffix("try {\n} /* */ finally\n") is True
    assert _prefix_opens_control_flow_over_suffix("if (a) {\n} /* */ else if (false)\n") is True
    # do-while terminator must not treat the following line as its body.
    assert _prefix_opens_control_flow_over_suffix("do {\n  x();\n} while (false)\n") is False
    # Open brace on the continuation still counts via brace depth.
    assert _prefix_opens_control_flow_over_suffix("if (false) {\n} else {\n") is True

    salvage = "enable_guard();\n"
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="if (false) {\n} else\n" + salvage,
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="if (false) {} else\n" + salvage,
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="if (false) {\n} /* */ else\n" + salvage,
    )
    assert not _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="try {\nx();\n} catch (e)\n" + salvage,
    )
    assert _added_salvage_blob_retained(
        commit_blob=salvage,
        head_blob="do {\nx();\n} while (false)\n" + salvage,
    )


@pytest.mark.unit
def test_string_comment_state_and_ls_tree_meta_edges() -> None:
    """Triple-quote / escape state and ls-tree meta parsing stay fail-closed."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _added_salvage_blob_retained,
        _advance_string_or_block_comment_state,
        _normalize_yaml_sequence_item_scalar,
        _parse_ls_tree_meta,
        _prefix_leaves_open_disabling_context,
        _yaml_scalar_sequence_item_scope,
    )

    assert _advance_string_or_block_comment_state(
        'x"""', in_block_comment=False, in_triple_double=True, in_triple_single=False
    ) == (False, False, False)
    assert _advance_string_or_block_comment_state(
        "x'''", in_block_comment=False, in_triple_double=False, in_triple_single=True
    ) == (False, False, False)
    assert _advance_string_or_block_comment_state(
        r'"a\"b"', in_block_comment=False, in_triple_double=False, in_triple_single=False
    ) == (False, False, False)
    assert _advance_string_or_block_comment_state(
        r"'a\'b'", in_block_comment=False, in_triple_double=False, in_triple_single=False
    ) == (False, False, False)

    assert _prefix_leaves_open_disabling_context('x = "a\\"\nb"\n') is False
    assert _prefix_leaves_open_disabling_context("x = 'a\\'\nb'\n") is False
    assert _prefix_leaves_open_disabling_context('x = "line1\nline2"\n') is False

    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob='x = """\nhi\n"""\nenable_guard()\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="x = '''\nhi\n'''\nenable_guard()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob='x = """\nenable_guard()\n',
    )
    # Whitespace between ``#`` and a directive keyword must still track depth.
    assert _prefix_leaves_open_disabling_context("#  if 0\n") is True
    assert _prefix_leaves_open_disabling_context("#  if 0\n#  endif\n") is False

    assert _parse_ls_tree_meta("nospace") is None
    assert _parse_ls_tree_meta("100644 ") is None
    assert _parse_ls_tree_meta("100644 blob") is None
    assert _parse_ls_tree_meta("100644 blob abc def") is None
    assert _parse_ls_tree_meta("100644 blob abcdef") == ("100644", "blob", "abcdef")

    assert _normalize_yaml_sequence_item_scalar('"x"') == "x"
    assert _normalize_yaml_sequence_item_scalar("'x'") == "x"
    assert _normalize_yaml_sequence_item_scalar("x") == "x"
    assert _yaml_scalar_sequence_item_scope("- name: a") == ("name.a", "name")


@pytest.mark.unit
def test_git_mode_and_merge_byte_safety_helpers() -> None:
    """Tree-mode kinds and merge-unsafe byte checks stay fail-closed."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence import (
        _bytes_unsafe_for_text_merge,
        _git_mode_file_kind,
        _merge_file_result_matches_head,
        _opens_nested_binding_scope,
        _raw_blob_from_cat_file_result,
        _toml_table_header_path,
    )

    assert _git_mode_file_kind("100644") == "file"
    assert _git_mode_file_kind("100755") == "file"
    assert _git_mode_file_kind("120000") == "symlink"
    assert _git_mode_file_kind("160000") == "gitlink"
    assert _git_mode_file_kind("040000") == "040000"

    assert _bytes_unsafe_for_text_merge(b"ok") is False
    assert _bytes_unsafe_for_text_merge(b"a\0b") is True
    assert _bytes_unsafe_for_text_merge(b"\xff") is True

    assert _raw_blob_from_cat_file_result(ok=False, stdout="", stdout_bytes=None) is None
    assert _raw_blob_from_cat_file_result(ok=True, stdout="", stdout_bytes=b"raw") == b"raw"
    assert _raw_blob_from_cat_file_result(ok=True, stdout="a\0b", stdout_bytes=None) is None
    assert _raw_blob_from_cat_file_result(ok=True, stdout="ok", stdout_bytes=None) == b"ok"

    assert _merge_file_result_matches_head(head_raw=b"x", stdout="x", stdout_bytes=None)
    assert _merge_file_result_matches_head(head_raw=b"x", stdout="", stdout_bytes=b"x")

    assert _opens_nested_binding_scope("// comment") is False
    assert _opens_nested_binding_scope("# comment") is False
    assert _opens_nested_binding_scope("def f():") is True
    assert _opens_nested_binding_scope("- name: a") is True
    assert _toml_table_header_path("[feature]") == "feature"
    assert _toml_table_header_path("[a]]") is None
