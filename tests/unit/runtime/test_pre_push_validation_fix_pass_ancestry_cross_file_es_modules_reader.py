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
    # ``for await (...)`` heads a statement too, though ``await`` precedes ``(``.
    assert _values("for await (const x of xs) /`/.test(s);")[8:10] == ["", "."]
    assert _values("x = await (a) / 2")[-2:] == ["/", "2"]
    assert _values("x = `a` / 2")[2:4] == ["", "/"]
    assert _values("x = `a${b}` / 2")[-2:] == ["/", "2"]


@pytest.mark.unit
def test_a_non_null_assertion_is_followed_by_division() -> None:
    assert _values("x = a! / b / c") == ["x", "=", "a", "!", "/", "b", "/", "c"]
    assert _values("x = f(a)! / 2")[-3:] == ["!", "/", "2"]
    assert _values("x = a[0]! / 2")[-3:] == ["!", "/", "2"]
    assert _values("x = a!! / 2")[-4:] == ["!", "!", "/", "2"]
    # A prefix ``!`` still starts an expression, so the ``/`` after it opens a regex.
    assert _values("x = !/'/.test(s)")[2:4] == ["!", ""]
    assert _values("x = !!/'/.test(s)")[2:5] == ["!", "!", ""]
    assert _values("!/'/.test(s)")[:2] == ["!", ""]
    assert _values("return !/'/.test(s)")[:3] == ["return", "!", ""]
    assert _values("if (ok) !/'/.test(s)")[4:6] == ["!", ""]
    assert _values("x = a\n!/'/.test(s)")[3:5] == ["!", ""]
    assert _values("x = (a) + !/'/.test(s)")[6:8] == ["!", ""]


@pytest.mark.unit
def test_tokens_carry_the_line_splitlines_numbers_them_on() -> None:
    tokens = _tokens("a\r\nb\rc\u2028d /* x\n */ e\n`t\n` f")

    assert [(token.value, token.line) for token in tokens] == [
        ("a", 1),
        ("b", 2),
        ("c", 3),
        ("d", 4),
        ("e", 5),
        ("", 7),
        ("f", 7),
    ]


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
    "head",
    ["<T,>", "<T, U>", "<T = unknown>", "<T extends Row>", "<const T,>", "< T ,>"],
)
def test_tsx_generic_arrow_heads_are_not_jsx(head: str) -> None:
    """TSX reads these ``<`` prefixes as type parameters (PRRT_kwDOSJAM6s6vMLga)."""
    values = _values(f"export const record = {head}(value: T) => value;", "src/app.tsx")

    assert values[4] == "<"
    assert "value" in values


@pytest.mark.unit
@pytest.mark.parametrize("text", ["x = <T>{a}</T>", "x = <T extends/>", "x = <a extends='b'/>"])
def test_tsx_jsx_that_resembles_a_generic_head_stays_jsx(text: str) -> None:
    assert _values(text, "src/app.tsx")[-1] == "<"


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
        ("x = <a b ,>", "src/app.tsx"),
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
        "record! = other;",
        "(record!) = other;",
        "(record)! ??= other;",
        "record!!++;",
        # TS assertions and their combinations stay the binding
        # (PRRT_kwDOSJAM6s6vQACC), under every assignment operator.
        "(record as any) = other;",
        "(record satisfies Recorder) = other;",
        "record as any = other;",
        "(<any>record) = other;",
        "(<Map<K, V>>record) = other;",
        "((record as unknown as Recorder)!) += other;",
        "((record!) as Recorder)! >>>= 1;",
        "(record as Map<K, V>) ||= other;",
        "record as Map<K, Set<V>>= other;",
        "(record as () => void) = other;",
        "(record as (value: number) => void) = other;",
        "(record as any)++;",
        "--(record satisfies Counter)!;",
        "for ((record as any) of [other]) {}",
        "for (record! in table) {}",
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
        "if (record! != other || record!== other) {}",
        "const total = record!(1);\nrecord\n!other;",
        "const f = () => record;",
        "export { record };\nexport default record;",
        "client.record = other;",
        "const a = [record];\nconst b = { record };",
        "function f() { const { record } = handlers; }",
        "for (const [record] of pairs) {}",
        "const a = record as any;\nconst b = <any>record;",
        "if ((record as any) == other || (record satisfies T) === other) {}",
        "call(record as T, other = 1);",
        "const c = (record as Recorder)(1);",
        "other > record;\nif (a > record) {}\na;\nb > record;",
        "const d = record as Recorder",
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
    # A ``package.json`` beside a resolved ``index`` may point the directory elsewhere.
    assert es._resolved_module_path(probes, frozenset({"a/index.ts", "a/package.json"})) is None
    assert es._resolved_module_path(("index.ts",), frozenset({"index.ts", "package.json"})) is None
    assert es._resolved_module_path(probes, frozenset({"a.ts", "a/package.json"})) == "a.ts"


@pytest.mark.unit
def test_index_probes_also_probe_their_directory_manifest() -> None:
    probes = es._relative_specifier_probe_paths("src/app/main.ts", "./telemetry")
    assert probes is not None

    assert es._directory_manifest_paths(probes) == ("src/app/telemetry/package.json",)
    assert es._directory_manifest_paths(("index.ts", "index.js")) == ("package.json",)
    assert es._directory_manifest_paths(("src/app/telemetry.ts",)) == ()


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
    text = "a = other.\n  tel.record(1) + tel.flush() + x?.ns?.go()\nb = y.ns.stop()"

    assert es._es_chained_member_refs(_tokens(text), 2) == frozenset(
        {("tel", "record"), ("ns", "go")}
    )
    # Only chains on the anchored line count; ``y.ns.stop`` sits on line 3.
    assert es._es_chained_member_refs(_tokens(text), 3) == frozenset({("ns", "stop")})
    assert es._es_chained_member_refs(_tokens(text), 1) == frozenset()
    assert es._es_chained_member_refs(_tokens("a.b"), 1) == frozenset()


@pytest.mark.unit
def test_direct_member_calls_are_reported_per_occurrence() -> None:
    """A bare-receiver call keeps its pair beside a chained one (PRRT_kwDOSJAM6s6vOa0O)."""
    text = "a = other.tel.record(1) + tel.record(2) + ns.go?.(3)\nb = x.ns.stop() + y.flush + z.\n  w.run()"

    assert es._es_direct_member_calls(_tokens(text), 1) == frozenset(
        {("tel", "record"), ("ns", "go")}
    )
    # ``x.ns.stop`` is chained, ``y.flush`` is no call and ``z.w`` is not called.
    assert es._es_direct_member_calls(_tokens(text), 2) == frozenset()
    assert es._es_direct_member_calls(_tokens(text), 3) == frozenset()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "qualifier", "name", "calls"),
    [
        ("record(1)", None, "record", True),
        ("record?.(1)", None, "record", True),
        ("tel.record(1)", "tel", "record", True),
        ("tel?.record?.(1)", "tel", "record", True),
        # A reference, a property call, a method head or a renamed member is no call.
        ("const f = record", None, "record", False),
        ("record?.x", None, "record", False),
        ("box.record(1)", None, "record", False),
        ("class A {\n  record(v) {}\n}", None, "record", False),
        ("tel.other(1) + tel", "tel", "record", False),
        ("tel.record + x.tel.record(1)", "tel", "record", False),
    ],
)
def test_es_calls_requires_a_call_of_the_ref(
    text: str, qualifier: str | None, name: str, calls: bool
) -> None:
    """A surviving import with no call reaches nothing (PRRT_kwDOSJAM6s6vO2G-)."""
    assert es._es_calls(_tokens(text), qualifier, name) is calls


# --- anchor lines and definition heads -------------------------------------------


@pytest.mark.unit
def test_names_an_anchor_line_does_not_call_bare_are_reported() -> None:
    text = (
        "class C {\n"
        "  record(v: number) { return go(v); }\n"
        "  flush(): void;\n"
        "  get size() { return 1; }\n"
        "}\n"
        "const r = getClient().\n"
        "  send(1) + track(2) + cond ? pick(3) : 4;\n"
        "const s = a.\n"
        "  b?.c(1);\n"
        "const handlers = { replay: (v) => v, go(v) {} };\n"
    )
    tokens = _tokens(text)

    assert es._es_line_non_call_names(tokens, 2) == frozenset({"record"})
    assert es._es_line_non_call_names(tokens, 3) == frozenset({"flush"})
    assert es._es_line_non_call_names(tokens, 4) == frozenset({"size"})
    # ``pick(3) :`` reads as a signature head too, which only fails it closed.
    assert es._es_line_non_call_names(tokens, 7) == frozenset({"send", "pick"})
    assert es._es_line_non_call_names(tokens, 9) == frozenset({"b", "c"})
    assert es._es_line_non_call_names(tokens, 10) == frozenset({"go"})
    assert es._es_line_non_call_names(tokens, 1) == frozenset()


@pytest.mark.unit
def test_definition_heads_cover_declarations_and_signatures_at_any_depth() -> None:
    text = (
        "export function record(v) {\n"
        "  return record(v) + other.record(v);\n"
        "}\n"
        "function* gen() {}\n"
        "class Box {\n"
        "  record(v) {}\n"
        "}\n"
        "const x = a * record(1);\n"
        "if (ok) { const record = 1; }\n"
        "export const sink = function* record() {};\n"
    )
    heads = es._es_definition_heads(_tokens(text), "record")

    assert [(token.line, token.depth) for token in heads] == [(1, 0), (6, 1), (9, 1), (10, 0)]
    assert es._es_definition_heads(_tokens(text), "gen")[0].line == 4
    assert es._es_definition_heads(_tokens(text), "missing") == []


@pytest.mark.unit
def test_definition_heads_fold_a_self_named_function_initializer_into_its_declaration() -> None:
    """``const record = function record() {}`` is one head (PRRT_kwDOSJAM6s6vO2Gy)."""
    text = (
        "export const record = function record(v) {};\n"
        "let record: Recorder = async function record(v) {};\n"
        "var record = 1, other = function record() {};\n"
        "let record: Map<K = number> = function record() {};\n"
        "let record;\n"
        "function record() {}\n"
        "class record {}\n"
        "function record() {}\n"
    )
    heads = es._es_definition_heads(_tokens(text), "record")

    # A later declarator (line 3), a ``=`` in the type (line 4), a function
    # declaration after an uninitialized one (line 6) and one after a
    # non-variable head (line 8) each keep their own head.
    assert [token.line for token in heads] == [1, 2, 3, 3, 4, 4, 5, 6, 7, 8]


@pytest.mark.unit
def test_definition_heads_skip_body_less_function_overload_signatures() -> None:
    text = (
        "export function record(v: string): void;\n"
        "declare function record<T extends { a: 1 }>(v: T): void;\n"
        "export function record(v: number): { a: number; b: number } {\n"
        "  return { a: v, b: v };\n"
        "}\n"
        "interface Sink {\n"
        "  record(v: string): void;\n"
        "}\n"
    )
    heads = es._es_definition_heads(_tokens(text), "record")

    # A ``{`` before the ``;`` keeps the head, so the object-typed overload on
    # line 2 stays and the check still fails it closed; method signatures stay too.
    assert [(token.line, token.depth) for token in heads] == [(2, 0), (3, 0), (7, 1)]
    # A signature without its ``;`` — closing its block or ending the file — stays.
    unterminated = "if (ok) { function record(v: string): void }\nfunction record(v: number): void"
    assert [token.line for token in es._es_definition_heads(_tokens(unterminated), "record")] == [
        1,
        2,
    ]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "span"),
    [
        # A ``;`` ends a variable; a statement after it on the line shares it.
        ("export const record = (v) => v; const other = () => {\n  v;\n};\n", None),
        # A variable's own ``}`` does not end it; its ``;`` does.
        ("export const record = () => {\n  v;\n};\nother();\n", (1, 3)),
        # A declaration's body ``}`` ends it.
        ("export function record() { v; } function other() {\n  v;\n}\n", None),
        ("export class record { v = 1 } other(() => {\n  v;\n});\n", None),
        # A ``}`` closing a return type is not the body's.
        ("function record(): { a: 1 } {\n  v;\n}\n", (1, 3)),
        ("function record() {\n  v;\n}\nfunction other() {}\n", (1, 3)),
        # Bracket depth, never indentation: an ASI-ended arrow does not take
        # the indented statement below it, nor a dedented body line leave it.
        ("export const record = (v) => v + 1\n  other()\n", (1, 1)),
        ("export const record = (v) =>\n  v + 1;\n", (1, 2)),
        ("export function record() {\nv;\n}\n  other();\n", (1, 3)),
        ("export function record() {\n  const s = `\n}\n`;\n}\nother();\n", (1, 5)),
        # Type parameters, annotations and return types may hold ``,`` / ``{``.
        ("export const record = <K, V = Row[K]>(v: V): Map<K, V> => {\n  v;\n};\n", (1, 3)),
        ("export const record: Recorder<A, B> = (v) => {\n  v;\n};\n", (1, 3)),
        ("export class record extends Base<{ a: 1 }> implements I {\n  v = 1;\n}\n", (1, 3)),
        ("export function record<K, V = Row<K>>(v: V): Promise<{ a: 1 }> {\n  v;\n}\n", (1, 3)),
        ("export class record<T extends (x: T) => void> {\n}\n", (1, 2)),
        ("export async function* record() {\n  v;\n};\nother();\n", (1, 3)),
        # A statement before the definition on its first line shares it.
        ("other(); export function record() {\n  v;\n}\n", None),
        # Automatic semicolon insertion ends a declarator only where it must.
        ("export const record = async () => await\nother();\n", (1, 2)),
        ("export const record = () => a\n  + b;\n", (1, 2)),
        ("export const record = () => fn\n(x);\n", (1, 2)),
        ("export const record = () => tag\n`x`;\n", (1, 2)),
        ("export const record = () => value\n  as Fn;\n", (1, 2)),
        ("export const record = () => 1\n'use strict';\n", (1, 1)),
        ("export const record = () => 1", (1, 1)),
        ("{\n  const record = () => 1\n}\n", (2, 2)),
        ("export const record = class {\n  v = 1;\n};\n", (1, 3)),
        ("export const record = async function* () {\n  v;\n};\n", (1, 3)),
        ("export const record = v => v;\n", (1, 1)),
        # Unbounded or ambiguous ends fail closed.
        ("export const record = () => 1, other = () => {\n  v;\n};\n", None),
        ("export const record = () => {}\n(other)();\n", None),
        ("export const record = () => {}\n`x`;\n", None),
        ("export const record = () => make\n{\n  v;\n}\n", None),
        ("export const record = () => other\n++v;\n", None),
        ("export const record = () => count++\nother();\n", None),
        ("interface Sink {\n  record(v: string): void;\n}\n", None),
        ("export function record(): { a: 1 }\nother();\n", None),
        ("export function record()\n", None),
        ("export function record {\n}\n", None),
        ("export const record = <T>>(v) => v;\n", None),
        ("export function record<T", None),
        ("export function record<T = A>=B>() {\n}\n", None),
        ("{\n  function record<T\n}\n", None),
        ("export class record extends Base", None),
        ("export class record extends Base = {\n}\n", None),
        ("export enum record {\n  A,\n}\n", None),
        ("export namespace record {\n  v;\n}\n", None),
        # Types that cannot be placed, or a value that is no callable.
        ("export const record: Map<K, V>= () => 1;\n", None),
        ("export const record: A> = () => 1;\n", None),
        ("export let record: A, other = () => 1;\n", None),
        ("{\n  let record: T\n}\n", None),
        ("export let record!: Recorder;\n", None),
        ("export const record = <T;\n", None),
        ("export const record = (make)();\n", None),
        ("export const record = make();\n", None),
    ],
)
def test_definition_span_follows_bracket_depth(text: str, span: tuple[int, int] | None) -> None:
    """A definition's span runs from its head to its own end (PRRT_kwDOSJAM6s6vQABw)."""
    tokens = _tokens(text)

    assert es._es_definition_span(tokens, es._es_definition_heads(tokens, "record")[0]) == span


@pytest.mark.unit
def test_methods_and_object_members_span_their_own_body() -> None:
    text = (
        "class Box {\n"
        "  static async record(v) {\n"
        "    v;\n"
        "  }\n"
        "}\n"
        "const api = {\n"
        "  record(v) { v; }, other: 1,\n"
        "  record(v) {\n"
        "    v;\n"
        "  },\n"
        "};\n"
    )
    tokens = _tokens(text)
    spans = [
        es._es_definition_span(tokens, head) for head in es._es_definition_heads(tokens, "record")
    ]

    assert spans == [(2, 4), None, (8, 10)]


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "span"),
    [
        ("export function record() {\n  v;\n}\nclass Box {\n  record() {}\n}\n", (1, 3)),
        # A nested head inside the module head's own span, or none at module scope.
        ("export function record() {\n  function record() {}\n}\n", None),
        ("if (ok) {\n  function record() {}\n}\n", None),
        ("function record() {}\nfunction record() {}\n", None),
        ("export const record = () => 1, x = 2;\n", None),
        ("const other = 1;\n", None),
    ],
)
def test_module_definition_span_needs_one_module_head(
    text: str, span: tuple[int, int] | None
) -> None:
    assert es._es_module_definition_span(_tokens(text), "record") == span
