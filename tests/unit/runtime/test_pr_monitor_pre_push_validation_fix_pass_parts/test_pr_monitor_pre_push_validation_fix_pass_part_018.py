"""Pre-push validation fix-pass object-mutation edge cases (part 018).

Covers parse / join helpers in ``presence_object_mut`` that the happy-path
Object.assign / defineProperty(ies) / Reflect.set salvage tests leave cold —
especially blank / line-comment / block-comment gaps on multiline calls and
opaque-literal early exits that fail-closed tip-extra salvage.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_object_literal_entry_key_rejects_opaque_and_accepts_empty_sources() -> None:
    """Opaque object-literal keys must not synthesize bindings; empty ``{}`` may.

    Tip-extra fail-closed depends on distinguishing synthesizable sources from
    spreads / computed keys / non-key tokens (PRRT_kwDOSJAM6s6Zxwhs).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
        _object_assign_mutation_args_fully_synthesizable,
        _object_assign_mutation_binding_names,
        _object_assign_source_fully_synthesizable,
        _object_literal_entry_key,
    )

    assert _object_literal_entry_key("enabled: false") == "enabled"
    assert _object_literal_entry_key('"enabled": false') == "enabled"
    assert _object_literal_entry_key("'enabled': false") == "enabled"
    assert _object_literal_entry_key("enabled") == "enabled"
    assert _object_literal_entry_key("") is None
    assert _object_literal_entry_key("...") is None
    assert _object_literal_entry_key("...other") is None
    assert _object_literal_entry_key("[k]: false") is None
    # Digit-leading tokens are not identifier keys.
    assert _object_literal_entry_key("1enabled: false") is None

    assert _object_assign_source_fully_synthesizable("{}")
    assert _object_assign_source_fully_synthesizable("{  }")
    assert not _object_assign_source_fully_synthesizable("{...other}")
    assert not _object_assign_source_fully_synthesizable("{[k]: false}")
    assert not _object_assign_source_fully_synthesizable("other")

    assert _object_assign_mutation_args_fully_synthesizable("Object.assign(guard, {})")
    assert _object_assign_mutation_binding_names("Object.assign(guard, {})") == ()
    assert not _object_assign_mutation_args_fully_synthesizable("no Object.assign here")
    assert _object_assign_mutation_binding_names("// Object.assign(guard, {enabled: false})") == ()
    assert _object_assign_mutation_binding_names("# Object.assign(guard, {enabled: false})") == ()


@pytest.mark.unit
def test_object_assign_target_parse_and_unclosed_edges() -> None:
    """Unclosed / mid-line / non-executable Object.assign must fail closed."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
        _object_assign_call_targets,
        _object_assign_call_unclosed,
        _object_assign_mutation_binding_names,
        _object_assign_target_and_args,
    )

    # Unclosed argument list reports synthesizable=False for tip-extra drop.
    assert _object_assign_call_targets("Object.assign(guard, {enabled: false") == (
        ("guard", False),
    )
    assert _object_assign_call_unclosed("Object.assign(guard, {enabled: false")
    assert not _object_assign_call_unclosed("Object.assign(guard, {enabled: false})")
    assert not _object_assign_call_unclosed("// Object.assign(guard, {enabled: false")
    assert not _object_assign_call_unclosed("# Object.assign(guard, {enabled: false")
    assert _object_assign_call_targets("// Object.assign(guard, {enabled: false})") == ()
    assert _object_assign_call_targets("# Object.assign(guard, {enabled: false})") == ()
    # String-embedded call names are not executable mutations.
    assert _object_assign_call_targets('msg = "Object.assign(guard, {enabled: false})"') == ()
    assert not _object_assign_call_unclosed('msg = "Object.assign(guard,"')

    # Target regex miss when match_start does not align with a call site.
    assert _object_assign_target_and_args("noop(guard)", match_start=0) is None
    assert _object_assign_target_and_args("Object.assign(guard, {})", match_start=1) is None
    # Executable call whose first arg is not a synthesizable receiver — parse
    # returns None and the call contributes no mutation targets.
    assert _object_assign_call_targets("Object.assign(1, {enabled: false})") == ()
    assert _object_assign_mutation_binding_names("Object.assign(1, {enabled: false})") == ()
    # Spread / computed keys appear as object literals but synthesize no names.
    assert _object_assign_mutation_binding_names("Object.assign(guard, {...other})") == ()
    assert _object_assign_mutation_binding_names("Object.assign(guard, {[k]: false})") == ()
    assert _object_assign_mutation_binding_names("Object.assign(guard, {1x: false})") == ()
    assert _object_assign_mutation_binding_names(
        "Object.assign(guard, {enabled: false, enabled: true})"
    ) == ("guard.enabled",)
    assert _object_assign_call_targets('msg = "Object.assign(guard, {enabled: false})"') == ()
    assert not _object_assign_call_unclosed('msg = "Object.assign(guard,"')


@pytest.mark.unit
def test_object_assign_multiline_joins_skip_blank_and_block_comment_gaps() -> None:
    """Multiline Object.assign must skip blank / // / /* */ gaps before closing.

    Formatters insert comments between opener and args; treating those lines as
    code would leave the call unjoined and retain stale salvage
    (PRRT_kwDOSJAM6s6Zyo4_).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
        _join_incomplete_object_assign_line,
        _join_incomplete_object_mutation_line,
        _object_assign_call_unclosed,
        _object_assign_join_gap_skippable,
        _object_assign_mutation_binding_names,
        _object_mutation_join_last_index,
    )

    assert _object_assign_join_gap_skippable("")
    assert _object_assign_join_gap_skippable("// note")
    assert _object_assign_join_gap_skippable("# note")
    assert _object_assign_join_gap_skippable("/* note */")
    assert not _object_assign_join_gap_skippable("/* note */ guard")
    assert not _object_assign_join_gap_skippable("guard,")

    # Already-closed opener is returned unchanged.
    closed = ["Object.assign(guard, {enabled: false});"]
    assert _join_incomplete_object_assign_line(closed, 0) == closed[0]

    multi = [
        "Object.assign(",
        "",
        "  // note",
        "  /* whole line */",
        "  guard,",
        "  {enabled: false}",
        ");",
    ]
    assert _object_assign_call_unclosed(multi[0])
    joined = _join_incomplete_object_assign_line(multi, 0)
    assert not _object_assign_call_unclosed(joined)
    assert _object_assign_mutation_binding_names(joined) == ("guard.enabled",)
    assert _join_incomplete_object_mutation_line(multi, 0) == joined
    assert _object_mutation_join_last_index(multi, 0) == 6

    # Multi-line block comment with interior lines that lack ``*/``.
    multi_block = [
        "Object.assign(",
        "  /* note",
        "   * middle",
        "   * more */",
        "  guard,",
        "  {enabled: false}",
        ");",
    ]
    joined_block = _join_incomplete_object_assign_line(multi_block, 0)
    assert _object_assign_mutation_binding_names(joined_block) == ("guard.enabled",)
    assert _object_mutation_join_last_index(multi_block, 0) == 6

    # ``*/`` line that also carries the next argument must append the trailer.
    multi_trailer = [
        "Object.assign(",
        "  /* note",
        "   */ guard,",
        "  {enabled: false}",
        ");",
    ]
    joined_trailer = _join_incomplete_object_assign_line(multi_trailer, 0)
    assert "guard" in joined_trailer
    assert _object_assign_mutation_binding_names(joined_trailer) == ("guard.enabled",)
    assert _object_mutation_join_last_index(multi_trailer, 0) == 4

    # Trailer after ``*/`` can itself close the call.
    multi_close = [
        "Object.assign(guard, {enabled: false",
        "  /* x",
        "   */ });",
    ]
    joined_close = _join_incomplete_object_assign_line(multi_close, 0)
    assert not _object_assign_call_unclosed(joined_close)
    assert _object_assign_mutation_binding_names(joined_close) == ("guard.enabled",)
    assert _object_mutation_join_last_index(multi_close, 0) == 2

    # Unclosed opener at EOF leaves the partial call for tip-extra fail-closed.
    eof_unclosed = ["Object.assign(guard, {enabled: false"]
    assert _object_assign_call_unclosed(eof_unclosed[0])
    assert _join_incomplete_object_assign_line(eof_unclosed, 0) == eof_unclosed[0]
    assert _object_mutation_join_last_index(eof_unclosed, 0) == 0

    # Unclosed block comment leaves the opener incomplete (fail closed later).
    unclosed_block = [
        "Object.assign(",
        "  /* note",
    ]
    assert _join_incomplete_object_assign_line(unclosed_block, 0) == "Object.assign("
    assert _object_mutation_join_last_index(unclosed_block, 0) == 0
    assert _object_mutation_join_last_index(["foo = 1"], 0) == 0


@pytest.mark.unit
def test_object_define_property_parse_join_and_synthesizable_edges() -> None:
    """defineProperty opaque / unclosed / comment-gap joins mirror Object.assign."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
        _join_incomplete_object_define_property_line,
        _join_incomplete_object_mutation_line,
        _object_define_property_call_targets,
        _object_define_property_call_unclosed,
        _object_define_property_literal_key,
        _object_define_property_mutation_args_fully_synthesizable,
        _object_define_property_mutation_binding_names,
        _object_define_property_target_and_args,
        _object_mutation_join_last_index,
    )

    assert _object_define_property_literal_key('"enabled"') == "enabled"
    assert _object_define_property_literal_key("'enabled'") == "enabled"
    assert _object_define_property_literal_key("enabled") is None
    assert _object_define_property_literal_key("key") is None

    assert _object_define_property_call_targets(
        'Object.defineProperty(guard, "enabled", {value: false}'
    ) == (("guard", False),)
    assert _object_define_property_call_targets(
        "Object.defineProperty(guard, key, {value: false})"
    ) == (("guard", False),)
    assert (
        _object_define_property_call_targets(
            '// Object.defineProperty(guard, "enabled", {value: false})'
        )
        == ()
    )
    assert (
        _object_define_property_call_targets(
            '# Object.defineProperty(guard, "enabled", {value: false})'
        )
        == ()
    )
    assert not _object_define_property_call_unclosed(
        '// Object.defineProperty(guard, "enabled", {value: false'
    )
    assert _object_define_property_target_and_args("noop(guard)", match_start=0) is None
    assert (
        _object_define_property_target_and_args(
            'Object.defineProperty(guard, "enabled", {})', match_start=1
        )
        is None
    )
    assert (
        _object_define_property_call_targets('Object.defineProperty(1, "enabled", {value: false})')
        == ()
    )
    assert (
        _object_define_property_mutation_binding_names(
            'Object.defineProperty(1, "enabled", {value: false})'
        )
        == ()
    )

    assert not _object_define_property_mutation_args_fully_synthesizable("no defineProperty")
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
    assert (
        _object_define_property_mutation_binding_names(
            "Object.defineProperty(guard, key, {value: false})"
        )
        == ()
    )

    closed = ['Object.defineProperty(guard, "enabled", {value: false});']
    assert _join_incomplete_object_define_property_line(closed, 0) == closed[0]

    multi = [
        "Object.defineProperty(",
        "  // note",
        "  /* whole */",
        "  /* open",
        "   * mid",
        "   * more */",
        "  guard,",
        '  "enabled",',
        "  {value: false}",
        ");",
    ]
    joined = _join_incomplete_object_define_property_line(multi, 0)
    assert not _object_define_property_call_unclosed(joined)
    assert _object_define_property_mutation_binding_names(joined) == ("guard.enabled",)
    assert _join_incomplete_object_mutation_line(multi, 0) == joined
    assert _object_mutation_join_last_index(multi, 0) == 9

    multi_trailer = [
        "Object.defineProperty(",
        "  /* open",
        "   */ guard,",
        '  "enabled",',
        "  {value: false}",
        ");",
    ]
    joined_trailer = _join_incomplete_object_define_property_line(multi_trailer, 0)
    assert _object_define_property_mutation_binding_names(joined_trailer) == ("guard.enabled",)

    multi_close = [
        'Object.defineProperty(guard, "enabled", {value: false',
        "  /* x",
        "   */ });",
    ]
    joined_close = _join_incomplete_object_define_property_line(multi_close, 0)
    assert not _object_define_property_call_unclosed(joined_close)
    assert _object_define_property_mutation_binding_names(joined_close) == ("guard.enabled",)

    # String-embedded / non-receiver calls contribute no executable mutations.
    assert (
        _object_define_property_call_targets(
            'msg = "Object.defineProperty(guard, \\"enabled\\", {value: false})"'
        )
        == ()
    )
    assert not _object_define_property_call_unclosed('msg = "Object.defineProperty(guard,"')
    assert _object_define_property_mutation_binding_names(
        'Object.defineProperty(guard, "enabled", {value: false}); '
        'Object.defineProperty(guard, "enabled", {value: true})'
    ) == ("guard.enabled",)

    unclosed_block = ["Object.defineProperty(", "  /* note"]
    assert _join_incomplete_object_define_property_line(unclosed_block, 0) == (
        "Object.defineProperty("
    )
    eof_unclosed = ['Object.defineProperty(guard, "enabled", {value: false']
    assert _object_define_property_call_unclosed(eof_unclosed[0])
    assert _join_incomplete_object_define_property_line(eof_unclosed, 0) == eof_unclosed[0]
    assert _object_mutation_join_last_index(eof_unclosed, 0) == 0


@pytest.mark.unit
def test_object_define_properties_parse_join_and_synthesizable_edges() -> None:
    """defineProperties opaque / empty / comment-gap joins must fail closed."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
        _join_incomplete_object_define_properties_line,
        _join_incomplete_object_mutation_line,
        _object_define_properties_call_targets,
        _object_define_properties_call_unclosed,
        _object_define_properties_mutation_args_fully_synthesizable,
        _object_define_properties_mutation_binding_names,
        _object_define_properties_target_and_args,
        _object_mutation_join_last_index,
    )

    assert _object_define_properties_call_targets(
        "Object.defineProperties(guard, {enabled: {value: false}"
    ) == (("guard", False),)
    assert _object_define_properties_call_targets("Object.defineProperties(guard, other)") == (
        ("guard", False),
    )
    assert (
        _object_define_properties_call_targets(
            "// Object.defineProperties(guard, {enabled: {value: false}})"
        )
        == ()
    )
    assert (
        _object_define_properties_call_targets(
            "# Object.defineProperties(guard, {enabled: {value: false}})"
        )
        == ()
    )
    assert not _object_define_properties_call_unclosed(
        "// Object.defineProperties(guard, {enabled: {value: false}"
    )
    assert _object_define_properties_target_and_args("noop(guard)", match_start=0) is None
    assert (
        _object_define_properties_target_and_args(
            "Object.defineProperties(guard, {})", match_start=1
        )
        is None
    )
    assert (
        _object_define_properties_call_targets(
            "Object.defineProperties(1, {enabled: {value: false}})"
        )
        == ()
    )
    assert (
        _object_define_properties_mutation_binding_names(
            "Object.defineProperties(1, {enabled: {value: false}})"
        )
        == ()
    )

    assert not _object_define_properties_mutation_args_fully_synthesizable("no defineProperties")
    assert _object_define_properties_mutation_args_fully_synthesizable(
        "Object.defineProperties(guard, {})"
    )
    assert (
        _object_define_properties_mutation_binding_names("Object.defineProperties(guard, {})") == ()
    )
    assert (
        _object_define_properties_mutation_binding_names(
            "// Object.defineProperties(guard, {enabled: {value: false}})"
        )
        == ()
    )
    assert (
        _object_define_properties_mutation_binding_names(
            "# Object.defineProperties(guard, {enabled: {value: false}})"
        )
        == ()
    )
    assert (
        _object_define_properties_mutation_binding_names(
            "Object.defineProperties(guard, {[k]: {value: false}})"
        )
        == ()
    )
    assert (
        _object_define_properties_mutation_binding_names(
            "Object.defineProperties(guard, {...other})"
        )
        == ()
    )
    assert _object_define_properties_mutation_binding_names(
        "Object.defineProperties(guard, {enabled: {value: false}, enabled: {value: true}})"
    ) == ("guard.enabled",)
    assert (
        _object_define_properties_call_targets(
            'msg = "Object.defineProperties(guard, {enabled: {value: false}})"'
        )
        == ()
    )
    assert not _object_define_properties_call_unclosed('msg = "Object.defineProperties(guard,"')

    closed = ["Object.defineProperties(guard, {enabled: {value: false}});"]
    assert _join_incomplete_object_define_properties_line(closed, 0) == closed[0]

    multi = [
        "Object.defineProperties(",
        "",
        "  /* open",
        "   * mid",
        "   * more */",
        "  guard,",
        "  {enabled: {value: false}}",
        ");",
    ]
    joined = _join_incomplete_object_define_properties_line(multi, 0)
    assert not _object_define_properties_call_unclosed(joined)
    assert _object_define_properties_mutation_binding_names(joined) == ("guard.enabled",)
    assert _join_incomplete_object_mutation_line(multi, 0) == joined
    assert _object_mutation_join_last_index(multi, 0) == 7

    multi_trailer = [
        "Object.defineProperties(",
        "  /* open",
        "   */ guard,",
        "  {enabled: {value: false}}",
        ");",
    ]
    joined_trailer = _join_incomplete_object_define_properties_line(multi_trailer, 0)
    assert _object_define_properties_mutation_binding_names(joined_trailer) == ("guard.enabled",)

    multi_close = [
        "Object.defineProperties(guard, {enabled: {value: false}",
        "  /* x",
        "   */ });",
    ]
    joined_close = _join_incomplete_object_define_properties_line(multi_close, 0)
    assert not _object_define_properties_call_unclosed(joined_close)
    assert _object_define_properties_mutation_binding_names(joined_close) == ("guard.enabled",)

    unclosed_block = ["Object.defineProperties(", "  /* note"]
    assert _join_incomplete_object_define_properties_line(unclosed_block, 0) == (
        "Object.defineProperties("
    )
    eof_unclosed = ["Object.defineProperties(guard, {enabled: {value: false}"]
    assert _object_define_properties_call_unclosed(eof_unclosed[0])
    assert _join_incomplete_object_define_properties_line(eof_unclosed, 0) == (eof_unclosed[0])
    assert _object_mutation_join_last_index(eof_unclosed, 0) == 0


@pytest.mark.unit
def test_reflect_set_parse_join_and_synthesizable_edges() -> None:
    """Reflect.set opaque / unclosed / comment-gap joins must fail closed."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
        _join_incomplete_object_mutation_line,
        _join_incomplete_reflect_set_line,
        _object_mutation_join_last_index,
        _reflect_set_call_targets,
        _reflect_set_call_unclosed,
        _reflect_set_mutation_args_fully_synthesizable,
        _reflect_set_mutation_binding_names,
        _reflect_set_target_and_args,
    )

    assert _reflect_set_call_targets('Reflect.set(guard, "enabled", false') == (("guard", False),)
    assert _reflect_set_call_targets("Reflect.set(guard, key, false)") == (("guard", False),)
    assert _reflect_set_call_targets('// Reflect.set(guard, "enabled", false)') == ()
    assert _reflect_set_call_targets('# Reflect.set(guard, "enabled", false)') == ()
    assert not _reflect_set_call_unclosed('// Reflect.set(guard, "enabled", false')
    assert _reflect_set_target_and_args("noop(guard)", match_start=0) is None
    assert _reflect_set_target_and_args('Reflect.set(guard, "enabled", false)', match_start=1) is (
        None
    )
    assert _reflect_set_call_targets('Reflect.set(1, "enabled", false)') == ()
    assert _reflect_set_mutation_binding_names('Reflect.set(1, "enabled", false)') == ()
    assert _reflect_set_call_targets('msg = "Reflect.set(guard, \\"enabled\\", false)"') == ()
    assert not _reflect_set_call_unclosed('msg = "Reflect.set(guard,"')
    assert _reflect_set_mutation_binding_names(
        'Reflect.set(guard, "enabled", false); Reflect.set(guard, "enabled", true)'
    ) == ("guard.enabled",)

    assert not _reflect_set_mutation_args_fully_synthesizable("no Reflect.set")
    assert _reflect_set_mutation_binding_names('// Reflect.set(guard, "enabled", false)') == ()
    assert _reflect_set_mutation_binding_names('# Reflect.set(guard, "enabled", false)') == ()
    assert _reflect_set_mutation_binding_names("Reflect.set(guard, key, false)") == ()

    closed = ['Reflect.set(guard, "enabled", false);']
    assert _join_incomplete_reflect_set_line(closed, 0) == closed[0]

    multi = [
        "Reflect.set(",
        "  // note",
        "  /* whole */",
        "  /* open",
        "   * mid",
        "   * more */",
        "  guard,",
        '  "enabled",',
        "  false",
        ");",
    ]
    joined = _join_incomplete_reflect_set_line(multi, 0)
    assert not _reflect_set_call_unclosed(joined)
    assert _reflect_set_mutation_binding_names(joined) == ("guard.enabled",)
    assert _join_incomplete_object_mutation_line(multi, 0) == joined
    assert _object_mutation_join_last_index(multi, 0) == 9

    multi_trailer = [
        "Reflect.set(",
        "  /* open",
        "   */ guard,",
        '  "enabled",',
        "  false",
        ");",
    ]
    joined_trailer = _join_incomplete_reflect_set_line(multi_trailer, 0)
    assert _reflect_set_mutation_binding_names(joined_trailer) == ("guard.enabled",)

    multi_block = [
        "Reflect.set(",
        "  /* open",
        "   * more */",
        "  guard,",
        '  "enabled",',
        "  false",
        ");",
    ]
    joined_block = _join_incomplete_reflect_set_line(multi_block, 0)
    assert _reflect_set_mutation_binding_names(joined_block) == ("guard.enabled",)

    multi_close = [
        'Reflect.set(guard, "enabled", false',
        "  /* x",
        "   */ );",
    ]
    joined_close = _join_incomplete_reflect_set_line(multi_close, 0)
    assert not _reflect_set_call_unclosed(joined_close)
    assert _reflect_set_mutation_binding_names(joined_close) == ("guard.enabled",)

    unclosed_block = ["Reflect.set(", "  /* note"]
    assert _join_incomplete_reflect_set_line(unclosed_block, 0) == "Reflect.set("
    eof_unclosed = ['Reflect.set(guard, "enabled", false']
    assert _reflect_set_call_unclosed(eof_unclosed[0])
    assert _join_incomplete_reflect_set_line(eof_unclosed, 0) == eof_unclosed[0]
    assert _object_mutation_join_last_index(eof_unclosed, 0) == 0


@pytest.mark.unit
def test_object_mutation_lookback_skips_blank_lines_before_shared_opener() -> None:
    """Look-back covering join must skip blank lines above a shared opener."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
        _join_incomplete_object_mutation_line_covering,
        _object_assign_mutation_binding_names,
        _object_define_properties_mutation_binding_names,
        _object_define_property_mutation_binding_names,
        _reflect_set_mutation_binding_names,
    )

    assign_lines = [
        "Object.assign(",
        "",
        "  guard,",
        "  {enabled: false}",
        ");",
    ]
    covered = _join_incomplete_object_mutation_line_covering(assign_lines, 3)
    assert _object_assign_mutation_binding_names(covered) == ("guard.enabled",)

    define_lines = [
        "Object.defineProperty(",
        "",
        "  guard,",
        '  "enabled",',
        "  {value: false}",
        ");",
    ]
    define_covered = _join_incomplete_object_mutation_line_covering(define_lines, 3)
    assert _object_define_property_mutation_binding_names(define_covered) == ("guard.enabled",)

    props_lines = [
        "Object.defineProperties(",
        "",
        "  guard,",
        "  {enabled: {value: false}}",
        ");",
    ]
    props_covered = _join_incomplete_object_mutation_line_covering(props_lines, 3)
    assert _object_define_properties_mutation_binding_names(props_covered) == ("guard.enabled",)

    reflect_lines = [
        "Reflect.set(",
        "",
        "  guard,",
        '  "enabled",',
        "  false",
        ");",
    ]
    reflect_covered = _join_incomplete_object_mutation_line_covering(reflect_lines, 3)
    assert _reflect_set_mutation_binding_names(reflect_covered) == ("guard.enabled",)


@pytest.mark.unit
def test_object_mutation_binding_scan_and_lookback_edge_closes() -> None:
    """Binding scanners and look-back must skip non-executable / nested misses.

    Tip-extra salvage depends on these fail-closed paths: string-embedded calls
    are not executable mutations, unclosed / opaque sources synthesize nothing,
    multi-key literals stay synthesizable, and nested incomplete openers that
    close before the tip must not steal the outer covering join.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_object_mut import (
        _join_incomplete_object_mutation_line_covering,
        _object_assign_mutation_binding_names,
        _object_assign_source_fully_synthesizable,
        _object_define_properties_mutation_binding_names,
        _object_define_property_mutation_args_fully_synthesizable,
        _object_define_property_mutation_binding_names,
        _reflect_set_mutation_args_fully_synthesizable,
        _reflect_set_mutation_binding_names,
    )

    # Multi-key object literals remain fully synthesizable (loop back-edge).
    assert _object_assign_source_fully_synthesizable("{a: 1, b: 2}")

    # String-embedded / unclosed / opaque sources never synthesize bindings.
    assert (
        _object_assign_mutation_binding_names('msg = "Object.assign(guard, {enabled: false})"')
        == ()
    )
    assert _object_assign_mutation_binding_names("Object.assign(guard, {enabled: false") == ()
    assert _object_assign_mutation_binding_names("Object.assign(guard, other)") == ()
    assert (
        _object_define_properties_mutation_binding_names(
            'msg = "Object.defineProperties(guard, {enabled: {value: false}})"'
        )
        == ()
    )
    assert (
        _object_define_properties_mutation_binding_names(
            "Object.defineProperties(guard, {enabled: {value: false}"
        )
        == ()
    )
    assert (
        _object_define_properties_mutation_binding_names("Object.defineProperties(guard, other)")
        == ()
    )
    assert (
        _object_define_property_mutation_binding_names(
            'msg = "Object.defineProperty(guard, \\"enabled\\", {value: false})"'
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
        _reflect_set_mutation_binding_names('msg = "Reflect.set(guard, \\"enabled\\", false)"')
        == ()
    )
    assert _reflect_set_mutation_binding_names('Reflect.set(guard, "enabled", false') == ()

    # Fully-synthesizable checks must evaluate existing calls, not only empties.
    assert _object_define_property_mutation_args_fully_synthesizable(
        'Object.defineProperty(guard, "enabled", {value: false})'
    )
    assert not _object_define_property_mutation_args_fully_synthesizable(
        "Object.defineProperty(guard, key, {value: false})"
    )
    assert _reflect_set_mutation_args_fully_synthesizable('Reflect.set(guard, "enabled", false)')
    assert not _reflect_set_mutation_args_fully_synthesizable("Reflect.set(guard, key, false)")

    # Covering join on an already-complete tip returns that tip unchanged.
    complete = ["Object.assign(guard, {enabled: false});"]
    assert _join_incomplete_object_mutation_line_covering(complete, 0) == complete[0]

    # Nested incomplete opener that closes before the tip must be skipped so the
    # outer Object.assign still covers the tip attribute line.
    nested = [
        "Object.assign(outer, {",
        "  a: Object.assign(inner,",
        "  {b: 1}),",
        "  enabled: false",
        "});",
    ]
    nested_covered = _join_incomplete_object_mutation_line_covering(nested, 3)
    assert "outer" in nested_covered
    assert _object_assign_mutation_binding_names(nested_covered) == (
        "outer.a",
        "outer.enabled",
        "inner.b",
    )

    # When the only incomplete opener closes before the tip and no outer cover
    # remains, look-back must fall through to the forward (uncovered) tip line.
    nested_only = [
        "const x = 1;",
        "  a: Object.assign(inner,",
        "  {b: 1}),",
        "  enabled: false",
    ]
    assert _join_incomplete_object_mutation_line_covering(nested_only, 3) == (nested_only[3])
