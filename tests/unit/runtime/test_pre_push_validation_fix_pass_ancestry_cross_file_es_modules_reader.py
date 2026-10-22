"""Pure readers behind JS/TS cross-file callee evidence (issue #1047).

Unit tests for the dependency-free ES-module reader: the tokenizer, the import
and export tables it feeds, the conservative shadow detector, and relative
specifier probing. The end-to-end gate is covered in
``test_pre_push_validation_fix_pass_ancestry_callees_cross_file_es_modules.py``.
"""

from __future__ import annotations

import pytest

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file_es_modules as es,
)


def _tokens(text: str, path: str = "src/app.ts") -> tuple[es._EsToken, ...]:
    tokens = es._es_module_tokens(text, path=path)
    assert tokens is not None
    return tokens


def _values(text: str, path: str = "src/app.ts") -> list[str]:
    return [token.value for token in _tokens(text, path)]


def _imports(text: str, path: str = "src/app.ts") -> dict[str, es._EsBinding] | None:
    return es._es_import_bindings(_tokens(text, path))


def _exports(text: str) -> es._EsExports | None:
    return es._es_module_exports(_tokens(text))


def _rebound(text: str, names: frozenset[str] = frozenset({"record"})) -> frozenset[str]:
    return es._es_rebound_names(_tokens(text), names)


# --- paths -----------------------------------------------------------------


@pytest.mark.unit
def test_es_module_sources_are_the_eight_js_ts_suffixes() -> None:
    for suffix in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"):
        assert es._path_is_es_module_source(f"src/app{suffix}")
        assert es._path_is_es_module_source(f"SRC/APP{suffix.upper()}")
    for path in ("src/app.py", "src/app.go", "src/app", "src/app.json"):
        assert not es._path_is_es_module_source(path)


# --- tokenizer ---------------------------------------------------------------


@pytest.mark.unit
def test_strings_keep_their_raw_value_and_escapes() -> None:
    tokens = _tokens("a = 'x\\'y' + \"z\" + 'p\\\nq' + 'r\\\r\ns';")

    assert [(t.kind, t.value) for t in tokens if t.kind == "string"] == [
        ("string", "x\\'y"),
        ("string", "z"),
        ("string", "p\\\nq"),
        ("string", "r\\\r\ns"),
    ]


@pytest.mark.unit
def test_templates_track_nested_substitutions_as_depth() -> None:
    tokens = _tokens("const s = `a${ `b${inner}` }d\\`e`;\nouter;")

    by_value = {token.value: token for token in tokens}
    assert by_value["inner"].depth == 2
    assert by_value["outer"].depth == 0
    assert by_value["outer"].newline_before
    assert [token.kind for token in tokens].count("template") == 2


@pytest.mark.unit
def test_comments_shebangs_and_byte_order_marks_are_skipped() -> None:
    text = "﻿#!/usr/bin/env node\n// import x from 'y'\n/* import\n */ a /* b */ c"

    tokens = _tokens(text)

    assert [token.value for token in tokens] == ["a", "c"]
    assert tokens[0].newline_before
    assert not tokens[1].newline_before


@pytest.mark.unit
def test_regex_literals_are_told_apart_from_division() -> None:
    assert _values("const r = /[/]'\\//g;") == ["const", "r", "=", "", ";"]
    assert _values("return /'/.test(s)")[:2] == ["return", ""]
    assert _values("x = a / b / c") == ["x", "=", "a", "/", "b", "/", "c"]
    assert _values("x = f(a) / 2") == ["x", "=", "f", "(", "a", ")", "/", "2"]
    assert _values("x /= 2") == ["x", "/=", "2"]
    assert _values("x = a++ / 2")[3:5] == ["++", "/"]
    assert _values("x = 'a' / 2")[3] == "/"
    assert _values("/'/.test(s)")[:2] == ["", "."]
    # A regex statement after a block or a condition is a regex, not division.
    assert _values("if (ok) {}\n/`/.test(s);")[6:8] == ["", "."]
    assert _values("if (ok) /`/.test(s);")[4:6] == ["", "."]
    assert _values("while (a) /x/g.exec(s)")[4] == ""
    assert _values("x = `a` / 2")[2:4] == ["", "/"]
    assert _values("x = `a${b}` / 2")[-2:] == ["/", "2"]


@pytest.mark.unit
def test_punctuators_numbers_and_private_names() -> None:
    assert _values("a?.b ?? c => d ... e") == ["a", "?.", "b", "??", "c", "=>", "d", "...", "e"]
    assert _values("x = a?.5:1") == ["x", "=", "a", "?", ".5", ":", "1"]
    assert _values("x = 1.5e3 >>>= 0x1F") == ["x", "=", "1.5e3", ">>>=", "0x1F"]
    assert _values("this.#count = 1") == ["this", ".", "#count", "=", "1"]
    assert _values("@decorator class A {}")[:2] == ["@", "decorator"]


@pytest.mark.unit
def test_jsx_elements_are_skipped_but_their_expressions_are_read() -> None:
    text = (
        "const view = (\n"
        "  <Card.Body title=\"it's\" data-x='y' {...rest} icon=<Icon /> on={() => go(1)}>\n"
        "    Don't stop {record(value)}\n"
        "    <></>\n"
        "    <br/>{/* note */}\n"
        "  </Card.Body>\n"
        ");\n"
        "const after = <b>{x}</b> < 2;\n"
    )

    values = _values(text, "src/view.tsx")

    assert "record" in values
    assert "go" in values
    assert "Don" not in values
    assert values[-3:] == ["<", "2", ";"]
    assert _values("const ok = a < b && c > d;", "src/app.jsx")[4] == "<"


@pytest.mark.unit
def test_angle_brackets_stay_punctuators_in_plain_typescript() -> None:
    assert _values("const v = <T>(x);", "src/app.ts")[3] == "<"
    assert _values("const v = <T,>(x: T) => x;", "src/app.mts")[3] == "<"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "path"),
    [
        ("'abc", "src/app.ts"),
        ("'a\nb'", "src/app.ts"),
        ("'a\\", "src/app.ts"),
        ("`abc", "src/app.ts"),
        ("`a${b`", "src/app.ts"),
        ("/* never closed", "src/app.ts"),
        ("x = /abc", "src/app.ts"),
        ("x = /abc\n/", "src/app.ts"),
        ("x = /[abc/", "src/app.ts"),
        ("(]", "src/app.ts"),
        ("(", "src/app.ts"),
        (")", "src/app.ts"),
        ("}", "src/app.ts"),
        ("x = \\u0061", "src/app.ts"),
        ("# x", "src/app.ts"),
        ("x = <div>", "src/app.tsx"),
        ("x = <a></b>", "src/app.tsx"),
        ("x = <a></a", "src/app.tsx"),
        ("x = <T,>() => 1", "src/app.tsx"),
        ("x = <a b=c/>", "src/app.tsx"),
        ('x = <a b="c/>', "src/app.tsx"),
        ("x = <a b", "src/app.tsx"),
        ("x = <", "src/app.tsx"),
        ("x = <a {...b", "src/app.tsx"),
        ("x = <>{a</>", "src/app.tsx"),
    ],
)
def test_unparseable_text_yields_no_tokens(text: str, path: str) -> None:
    """Every lexing anomaly is reported, so the call site fails closed."""
    assert es._es_module_tokens(text, path=path) is None


@pytest.mark.unit
def test_pathologically_deep_nesting_is_unparseable_rather_than_a_crash() -> None:
    text = "x = " + "`${" * 2000 + "1" + "}`" * 2000

    assert es._es_module_tokens(text, path="src/app.ts") is None


# --- imports -----------------------------------------------------------------


@pytest.mark.unit
def test_import_forms_bind_their_local_names() -> None:
    text = (
        "import record from './a';\n"
        "import { one, two as second, 'str-name' as third, default as fourth, } from './b';\n"
        "import * as ns from './c';\n"
        "import dflt, { five } from './d';\n"
        "import other, * as ns2 from './e' with { type: 'json' };\n"
        "import { type as typed } from './f';\n"
        "import {} from './g';\n"
    )

    assert _imports(text) == {
        "record": es._EsBinding("./a", "default"),
        "one": es._EsBinding("./b", "one"),
        "second": es._EsBinding("./b", "two"),
        "third": es._EsBinding("./b", "str-name"),
        "fourth": es._EsBinding("./b", "default"),
        "ns": es._EsBinding("./c", "*"),
        "dflt": es._EsBinding("./d", "default"),
        "five": es._EsBinding("./d", "five"),
        "other": es._EsBinding("./e", "default"),
        "ns2": es._EsBinding("./e", "*"),
        "typed": es._EsBinding("./f", "type"),
    }


@pytest.mark.unit
def test_type_only_imports_bind_no_runtime_name() -> None:
    text = (
        "import type { record } from './a';\n"
        "import type tel from './b';\n"
        "import type * as ns from './c';\n"
        "import { type helper, type x as y, type as, type as as z, kept } from './d';\n"
        "import type, { also } from './e';\n"
        "import type from './f';\n"
    )

    bindings = _imports(text)

    assert bindings is not None
    assert set(bindings) == {"kept", "type", "also"}
    assert bindings["type"] == es._EsBinding(None, "default")
    assert bindings["also"] == es._EsBinding("./e", "also")


@pytest.mark.unit
def test_expressions_and_side_effect_imports_bind_nothing() -> None:
    text = (
        "import './polyfill';\n"
        "const lazy = await import('./a');\n"
        "const url = import.meta.url;\n"
        "client.import(record);\n"
        "function f() { import('./b'); }\n"
    )

    assert _imports(text) == {}


@pytest.mark.unit
def test_unfollowable_and_repeated_imports_are_held_unresolvable() -> None:
    text = (
        "import record = require('./a');\n"
        "import alias = Namespace.inner;\n"
        "import { twice } from './b';\n"
        "import twice from './c';\n"
    )

    bindings = _imports(text)

    assert bindings == {
        "record": es._EsBinding(None, "default"),
        "alias": es._EsBinding(None, "default"),
        "twice": es._EsBinding(None, "default"),
    }


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "import",
        "import record",
        "import record from",
        "import record from x",
        "import record, from './a'",
        "import record ; from './a'",
        "import * from './a'",
        "import * as from './a'",
        "import { a b c } from './a'",
        "import { 1 } from './a'",
        "import { a as 'b' } from './a'",
        "import { 1 as b } from './a'",
        "import { a } './a'",
        "import ; './a'",
    ],
)
def test_malformed_imports_make_the_table_unreadable(text: str) -> None:
    assert es._es_import_bindings(es._es_module_tokens(text, path="src/a.ts") or ()) is None


# --- exports -----------------------------------------------------------------


@pytest.mark.unit
def test_local_export_forms() -> None:
    text = (
        "export function record() {}\n"
        "export async function asyncOne() {}\n"
        "export function* gen() {}\n"
        "export class Collector {}\n"
        "export abstract class Base {}\n"
        "export const value = 1;\n"
        "export let other = f(a, b)\n"
        "export var third = [1, 2];\n"
        "function inner() {}\n"
        "export { inner, inner as renamed, inner as 'quoted', type Hidden, };\n"
        "export type Shape = { a: 1 };\n"
        "export interface Thing {}\n"
        "export type { Other } from './types';\n"
    )

    exports = _exports(text)

    assert exports is not None
    assert exports.local == {
        "record": "record",
        "asyncOne": "asyncOne",
        "gen": "gen",
        "Collector": "Collector",
        "Base": "Base",
        "value": "value",
        "other": "other",
        "third": "third",
        "inner": "inner",
        "renamed": "inner",
        "quoted": "inner",
    }
    assert not exports.opaque
    assert exports.reexport == {}
    assert exports.star == ()


def _local_exports(text: str) -> dict[str, str]:
    exports = _exports(text)
    assert exports is not None
    return exports.local


@pytest.mark.unit
def test_default_export_forms() -> None:
    assert _local_exports("export default function record() {}") == {"default": "record"}
    assert _local_exports("export default async function record() {}") == {"default": "record"}
    assert _local_exports("export default class Collector {}") == {"default": "Collector"}
    assert _local_exports("function r() {}\nexport default r;") == {"default": "r"}
    assert _local_exports("function r() {}\nexport default r") == {"default": "r"}
    assert _local_exports("export default r\nexport const a = 1;") == {"default": "r", "a": "a"}
    assert _local_exports("export { r as default };") == {"default": "r"}
    for anonymous in (
        "export default function () {}",
        "export default class extends Base {}",
        "export default () => 1;",
        "export default r(1);",
        "export default r\n(1);",
        "export default r\nin x;",
    ):
        assert "default" not in _local_exports(anonymous)


@pytest.mark.unit
def test_reexport_forms() -> None:
    text = (
        "export { record, impl as alias, default as dflt } from './impl';\n"
        "export * from './star';\n"
        "export * as ns from './space';\n"
        "export * from './other' with { type: 'json' };\n"
    )

    exports = _exports(text)

    assert exports is not None
    assert exports.reexport == {
        "record": es._EsBinding("./impl", "record"),
        "alias": es._EsBinding("./impl", "impl"),
        "dflt": es._EsBinding("./impl", "default"),
    }
    assert exports.star == ("./star", "./other")
    assert exports.namespace == frozenset({"ns"})
    assert exports.local == {}


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "export const { a, b } = obj;",
        "export const [a] = list;",
        "export const a = 1, b = 2;",
        "export const a = 1\n  , b = 2",
        "export const enum Kind { A }",
        "export enum Kind { A }",
        "export declare function record(): void;",
        "export namespace Space {}",
        "export = record;",
        "export import alias = Space.inner;",
    ],
)
def test_export_forms_this_reader_cannot_list_mark_the_module_opaque(text: str) -> None:
    exports = _exports(text)

    assert exports is not None
    assert exports.opaque


@pytest.mark.unit
def test_statements_after_a_variable_export_do_not_mark_it_opaque() -> None:
    text = (
        "export const a = f(x,\n  y)\n"
        "export const b = 2\n"
        "const c = 3\n"
        "export const d = [1, 2]\n"
        "foo(e, f)\n"
        "export as namespace Lib;\n"
    )

    exports = _exports(text)

    assert exports is not None
    assert not exports.opaque
    assert set(exports.local) == {"a", "b", "d"}
    solo = _exports("export const solo = 1")
    assert solo is not None
    assert not solo.opaque


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "export",
        "export { a b }",
        "export { a as }",
        "export { 'a' }",
        "export { a } from x",
        "export *",
        "export * as",
        "export * as ns from x",
        "export * from x",
    ],
)
def test_malformed_exports_make_the_table_unreadable(text: str) -> None:
    assert es._es_module_exports(es._es_module_tokens(text, path="src/a.ts") or ()) is None


# --- shadowing -----------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "function f() { const record = 1; }",
        "function f() { let { a: record } = obj; }",
        "function f() { var [first, record] = list; }",
        "let a = 1, record = 2;",
        "const a = 1 +\n  2, record = 3;",
        "const a = function () {}, record = 1;",
        "function record() {}",
        "function* record() {}",
        "class record {}",
        "enum record { A }",
        "namespace record {}",
        "function f(a, record) {}",
        "function f({ record }) {}",
        "const f = (record) => record();",
        "const f = async (record): Promise<void> => {};",
        "const f = record => record();",
        "items.map(record => record())",
        "try { go(); } catch (record) {}",
        "for (const record of items) {}",
        "class A { method(record) {} }",
        "using record = open();",
        "namespace Legacy { import record = Other.record; }",
        "namespace Legacy { export import record = Other.record; }",
        "let first, record",
    ],
)
def test_binding_positions_rebind_the_name(text: str) -> None:
    assert _rebound(text) == frozenset({"record"})


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "import { record } from './a';\nconst result = record();",
        "export { record };",
        "client.record = 1;",
        "client?.record(1);",
        "const a = { record: 1 };",
        "if (record) { go(); }",
        "while (record(1)) {}",
        "record = other;",
        "const a = 1;\nrecord();",
        "const total = sum(a, record);",
        "type record = string;",
        "const a = 1\nreturn record, 2",
        "let a\nif (x) record(1)",
        "function f() { let a }\nrecord(1)",
    ],
)
def test_references_are_not_bindings(text: str) -> None:
    assert _rebound(text) == frozenset()


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "record = other;",
        "(record) = other;",
        "((record)) ||= other;",
        "(record)++;",
        "--(record);",
        "for ((record) of [other]) {}",
        "record += 1;",
        "record ??= other;",
        "record++;",
        "--record;",
        "[record] = [other];",
        "[first, ...record] = list;",
        "({ record } = overrides);",
        "({ a: { record } } = overrides);",
        "for (record of [other]) {}",
        "for (record in table) {}",
        "export function setup() { record = other; }",
    ],
)
def test_assignment_targets_reassign_the_name(text: str) -> None:
    assert es._es_assigned_names(_tokens(text), frozenset({"record"})) == frozenset({"record"})


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "export function record() {\n  return record.count === 1;\n}",
        "const total = record(1) + record.size;",
        "if (record == null || record != other) {}",
        "const f = () => record;",
        "export { record };\nexport default record;",
        "client.record = other;",
        "const a = [record];\nconst b = { record };",
        "function f() { const { record } = handlers; }",
        "for (const [record] of pairs) {}",
    ],
)
def test_references_are_not_assignments(text: str) -> None:
    assert es._es_assigned_names(_tokens(text), frozenset({"record"})) == frozenset()
    assert es._es_assigned_names(_tokens(text), frozenset()) == frozenset()


@pytest.mark.unit
def test_only_the_asked_names_are_reported() -> None:
    text = "function f(record, other) {}"

    assert _rebound(text, frozenset({"other", "missing"})) == frozenset({"other"})
    assert _rebound(text, frozenset()) == frozenset()


# --- specifiers ------------------------------------------------------------------


def _with_index(stem: str, suffixes: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(f"{stem}{suffix}" for suffix in suffixes) + tuple(
        f"{stem}/index{suffix}" for suffix in suffixes
    )


_EXTENSIONLESS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")


@pytest.mark.unit
def test_relative_specifiers_probe_like_typescript_and_node() -> None:
    importer = "src/app/main.ts"

    assert es._relative_specifier_probe_paths(importer, "./telemetry") == _with_index(
        "src/app/telemetry", _EXTENSIONLESS
    )
    assert es._relative_specifier_probe_paths(importer, "../lib/telemetry.config") == (
        _with_index("src/lib/telemetry.config", _EXTENSIONLESS)
    )
    assert es._relative_specifier_probe_paths(importer, "./telemetry.js") == (
        "src/app/telemetry.ts",
        "src/app/telemetry.tsx",
        "src/app/telemetry.d.ts",
        "src/app/telemetry.js",
        "src/app/telemetry.jsx",
    )
    assert es._relative_specifier_probe_paths(importer, "./view.jsx") == (
        "src/app/view.tsx",
        "src/app/view.d.ts",
        "src/app/view.jsx",
    )
    assert es._relative_specifier_probe_paths(importer, "./esm.mjs") == (
        "src/app/esm.mts",
        "src/app/esm.d.mts",
        "src/app/esm.mjs",
    )
    assert es._relative_specifier_probe_paths(importer, "./cjs.cjs") == (
        "src/app/cjs.cts",
        "src/app/cjs.d.cts",
        "src/app/cjs.cjs",
    )
    for explicit in ("./x.ts", "./x.tsx", "./x.mts", "./x.cts"):
        assert es._relative_specifier_probe_paths(importer, explicit) == (
            f"src/app/{explicit[2:]}",
        )
    index_only = tuple(f"src/app/index{suffix}" for suffix in _EXTENSIONLESS)
    assert es._relative_specifier_probe_paths(importer, ".") == index_only
    assert es._relative_specifier_probe_paths(importer, "./") == index_only
    assert es._relative_specifier_probe_paths(importer, "./sub/./../") == index_only
    assert es._relative_specifier_probe_paths("main.ts", "./x.ts") == ("x.ts",)
    assert es._relative_specifier_probe_paths("main.ts", ".") == tuple(
        f"index{suffix}" for suffix in _EXTENSIONLESS
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "specifier",
    [
        "telemetry",
        "@/lib/telemetry",
        "~/lib/telemetry",
        "@scope/pkg",
        "/abs/telemetry",
        "node:fs",
        "https://example.com/x.js",
        "../../../escape",
        "",
    ],
)
def test_non_relative_or_escaping_specifiers_are_unresolvable(specifier: str) -> None:
    assert es._relative_specifier_probe_paths("src/app/main.ts", specifier) is None


@pytest.mark.unit
def test_a_module_resolves_only_to_exactly_one_runtime_file() -> None:
    probes = ("a.ts", "a.d.ts", "a.js", "a/index.ts")

    assert es._resolved_module_path(probes, frozenset({"a.ts"})) == "a.ts"
    assert es._resolved_module_path(probes, frozenset({"a.ts", "a.d.ts"})) == "a.ts"
    assert es._resolved_module_path(probes, frozenset({"a.d.ts"})) is None
    assert es._resolved_module_path(probes, frozenset()) is None
    assert es._resolved_module_path(probes, frozenset({"a.ts", "a.js"})) is None
    assert es._resolved_module_path(probes, frozenset({"a.ts", "a/index.ts"})) is None
    assert es._resolved_module_path(("a.d.mts", "a.d.cts"), frozenset({"a.d.mts"})) is None


# --- call-site refs --------------------------------------------------------------


@pytest.mark.unit
def test_refs_resolve_to_a_specifier_and_export_name_or_fail_closed() -> None:
    imports = {
        "record": es._EsBinding("./a", "record"),
        "alias": es._EsBinding("./a", "impl"),
        "dflt": es._EsBinding("./b", "default"),
        "ns": es._EsBinding("./c", "*"),
        "required": es._EsBinding(None, "default"),
    }
    rebound = frozenset({"shadowed"})
    chained = frozenset({("ns", "deep")})
    imports["shadowed"] = es._EsBinding("./a", "shadowed")

    assert es._es_ref_target(imports, rebound, chained, None, "record") == ("./a", "record")
    assert es._es_ref_target(imports, rebound, chained, None, "alias") == ("./a", "impl")
    assert es._es_ref_target(imports, rebound, chained, None, "dflt") == ("./b", "default")
    assert es._es_ref_target(imports, rebound, chained, "ns", "record") == ("./c", "record")
    # Fail closed: a namespace called bare, a named import used as a receiver,
    # an unfollowable binding, a rebound name and an unbound one.
    assert es._es_ref_target(imports, rebound, chained, None, "ns") is None
    assert es._es_ref_target(imports, rebound, chained, "record", "inner") is None
    assert es._es_ref_target(imports, rebound, chained, None, "required") is None
    assert es._es_ref_target(imports, rebound, chained, None, "shadowed") is None
    assert es._es_ref_target(imports, rebound, chained, None, "missing") is None
    assert es._es_ref_target(imports, rebound, chained, "this", "record") is None
    assert es._es_ref_target(imports, rebound, chained, "ns", "deep") is None


@pytest.mark.unit
def test_receivers_reached_as_properties_are_reported() -> None:
    text = "a = other.\n  tel.record(1) + tel.flush() + x?.ns?.go()"

    assert es._es_chained_member_refs(_tokens(text)) == frozenset({("tel", "record"), ("ns", "go")})
    assert es._es_chained_member_refs(_tokens("a.b")) == frozenset()
