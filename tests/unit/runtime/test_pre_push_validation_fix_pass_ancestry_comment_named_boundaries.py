"""Comment-named identifier statement boundaries (issue #1232).

Split from ``test_pre_push_validation_fix_pass_ancestry_comment_named.py``: these
cases pin where a named identifier's binding statement ends — column-0 literals,
continuations, chained and semicolon-separated assignments, JS/TS declarators,
labels, type arguments, JSX initializers and module-scope indentation — so an
edit just past the binding is not mistaken for a change to its definition.
"""

from __future__ import annotations

import pytest

from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import _LEFT, _RIGHT, _Probe
from tests.unit.runtime._pre_push_comment_named_helpers import (
    _INBOUND,
    _WEB,
    _changes_named_target,
    _diff,
    _probe,
)


@pytest.mark.parametrize(
    ("path", "text", "diff"),
    [
        pytest.param(
            _INBOUND,
            '_TASK_INBOUND_TYPES = ("a",)\n"""Trailing note."""\nVALUE = 1\n',
            _diff(_INBOUND, "@@ -2 +2 @@", '-"""Trailing note."""', '+"""Reworded note."""'),
            id="column-0-string-literal",
        ),
        pytest.param(
            _INBOUND,
            '_TASK_INBOUND_TYPES = """\na\n"""\n"""Trailing\nnote."""\n',
            _diff(_INBOUND, "@@ -5 +5 @@", '-note."""', '+notes."""'),
            id="string-literal-after-multi-line-string",
        ),
        pytest.param(
            _WEB,
            'const _TASK_INBOUND_TYPES = ["a"];\n/* Trailing\n   note. */\nconst other = 1;\n',
            _diff(_WEB, "@@ -3 +3 @@", "-   note. */", "+   notes. */"),
            id="column-0-block-comment",
        ),
    ],
)
@pytest.mark.unit
async def test_column_0_literals_after_a_binding_are_not_evidence(
    path: str, text: str, diff: str
) -> None:
    """Masked blanks still open a new statement at column 0 (PRRT_kwDOSJAM6s63MDQr)."""
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert not await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses the task type")


@pytest.mark.parametrize(
    ("text", "diff", "expected"),
    [
        pytest.param(
            '_TASK_INBOUND_TYPES = "a\\\nb"\n',
            _diff(_INBOUND, "@@ -2 +2 @@", '-b"', '+c"'),
            True,
            id="last-statement-continued-line",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = ("a\\\nb",\n    1)\nother = 1\n',
            _diff(_INBOUND, "@@ -4 +4 @@", "-other = 1", "+other = 2"),
            False,
            id="statement-after-bracketed-continuation",
        ),
    ],
)
@pytest.mark.unit
async def test_backslash_continued_string_keeps_binding_lines_aligned(
    text: str, diff: str, *, expected: bool
) -> None:
    """An escaped newline in a string keeps its raw line (PRRT_kwDOSJAM6s63MUc8)."""
    runner = _probe(texts={_INBOUND: text}, changed_paths=(_INBOUND,), diffs={_INBOUND: diff})

    assert await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` is wrong") is expected


@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        pytest.param(
            _diff(_INBOUND, "@@ -2 +2 @@", "-OTHER = 1", "+OTHER = 2"), False, id="next-binding"
        ),
        pytest.param(
            _diff(_INBOUND, "@@ -1 +1 @@", "-LIMIT = (10 // 2)", "+LIMIT = (10 // 1)"),
            True,
            id="own-binding",
        ),
    ],
)
@pytest.mark.unit
async def test_python_floor_division_does_not_extend_a_binding(
    diff: str, *, expected: bool
) -> None:
    """``//`` is an operator in Python, not a comment (PRRT_kwDOSJAM6s63OADy)."""
    runner = _probe(
        texts={_INBOUND: "LIMIT = (10 // 2)\nOTHER = 1\n"},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: diff},
    )

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("diff", "expected"),
    [
        pytest.param(
            _diff(
                _INBOUND,
                "@@ -1 +1 @@",
                "-DEFAULT_LIMIT = LIMIT = 10",
                "+DEFAULT_LIMIT = LIMIT = 20",
            ),
            True,
            id="chained-binding",
        ),
        pytest.param(
            _diff(_INBOUND, "@@ -2 +2 @@", "-OTHER = LIMIT == 1", "+OTHER = LIMIT == 2"),
            False,
            id="comparison-is-not-a-target",
        ),
    ],
)
@pytest.mark.unit
async def test_chained_assignment_binds_every_target(diff: str, *, expected: bool) -> None:
    """A secondary target of ``A = B = v`` is bound too (PRRT_kwDOSJAM6s63PKQ9)."""
    runner = _probe(
        texts={_INBOUND: "DEFAULT_LIMIT = LIMIT = 10\nOTHER = LIMIT == 1\n"},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: diff},
    )

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("line", "new", "expected"),
    [
        pytest.param(3, "  2", False, id="arrow-parameter-is-not-a-target"),
        pytest.param(1, "export const LIMIT = 20;", True, id="own-binding"),
    ],
)
@pytest.mark.unit
async def test_chained_assignment_target_excludes_an_arrow(
    line: int, new: str, *, expected: bool
) -> None:
    """The ``=`` of ``=>`` binds no arrow parameter (PRRT_kwDOSJAM6s63VYOS)."""
    text = "export const LIMIT = 10;\nconst unrelated = LIMIT => (\n  1\n);\n"
    old = text.splitlines()[line - 1]
    diff = _diff(_WEB, f"@@ -{line} +{line} @@", f"-{old}", f"+{new}")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("line", "new", "expected"),
    [
        pytest.param(3, "  2", False, id="arrow-parameter-is-not-a-target"),
        pytest.param(1, "export const LIMIT = 20;", True, id="own-binding"),
    ],
)
@pytest.mark.unit
async def test_module_level_assignment_excludes_an_arrow(
    line: int, new: str, *, expected: bool
) -> None:
    """A standalone arrow's ``=>`` binds no parameter (PRRT_kwDOSJAM6s63V0Yl)."""
    text = "export const LIMIT = 10;\nLIMIT => (\n  1\n);\n"
    old = text.splitlines()[line - 1]
    diff = _diff(_WEB, f"@@ -{line} +{line} @@", f"-{old}", f"+{new}")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "export const LIMIT: (request: Request) => Response = makeHandler(\n  10\n);\n",
            True,
            id="function-type-annotation",
        ),
        pytest.param(
            "const OTHER = 0, LIMIT: () => void = makeHandler(\n  10\n);\n",
            True,
            id="secondary-declarator-function-type-annotation",
        ),
        pytest.param(
            "const OTHER = makeHandler(\n  10\n), LIMIT: () => void;\n",
            False,
            id="uninitialized-function-type-declarator",
        ),
    ],
)
@pytest.mark.unit
async def test_function_type_annotation_precedes_the_binding_assignment(
    text: str, *, expected: bool
) -> None:
    """An annotation's ``=>`` is not the binding's ``=`` (PRRT_kwDOSJAM6s63WYf0)."""
    diff = _diff(_WEB, "@@ -2 +2 @@", "-  10", "+  20")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("text", "diff", "expected"),
    [
        pytest.param(
            "let LIMIT: () => void, OTHER = 10;\n",
            _diff(
                _WEB,
                "@@ -1 +1 @@",
                "-let LIMIT: () => void, OTHER = 10;",
                "+let LIMIT: () => void, OTHER = 20;",
            ),
            False,
            id="later-declarator-initializer",
        ),
        pytest.param(
            "let LIMIT: <T = number>(\n  x: T\n) => T;\n",
            _diff(_WEB, "@@ -2 +2 @@", "-  x: T", "+  x: T[]"),
            False,
            id="generic-default-in-uninitialized-annotation",
        ),
        pytest.param(
            "export const LIMIT: <T = number>(x: T) => T = (\n  x\n) => x;\n",
            _diff(_WEB, "@@ -2 +2 @@", "-  x", "+  x ?? 10"),
            True,
            id="generic-default-before-binding-assignment",
        ),
    ],
)
@pytest.mark.unit
async def test_annotation_binds_only_its_own_top_level_assignment(
    text: str, diff: str, *, expected: bool
) -> None:
    """A later declarator's or generic default's ``=`` initializes no annotated name (PRRT_kwDOSJAM6s63WhGd)."""
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("text", "line", "new", "name", "expected"),
    [
        pytest.param(
            "const OTHER = 0, LIMIT = [\n  10,\n];\n",
            2,
            "  20,",
            "LIMIT",
            True,
            id="secondary-declarator",
        ),
        pytest.param(
            "const OTHER = 0, LIMIT = [\n  10,\n];\n",
            2,
            "  20,",
            "OTHER",
            False,
            id="first-declarator-ends-at-its-comma",
        ),
        pytest.param(
            "const OTHER = 0,\n  LIMIT = [\n    10,\n  ];\n",
            3,
            "    20,",
            "LIMIT",
            True,
            id="declarator-on-its-own-line",
        ),
        pytest.param(
            "const OTHER = 0,\n  LIMIT = [\n    10,\n  ];\n",
            1,
            "const OTHER = 1,",
            "LIMIT",
            False,
            id="previous-declarator-is-not-the-binding",
        ),
        pytest.param(
            "let first,\n  // the cap\n\n  LIMIT = 10;\n",
            4,
            "  LIMIT = 20;",
            "LIMIT",
            True,
            id="after-uninitialized-declarator-and-gap",
        ),
        pytest.param(
            "const m: Map<string, number> = new Map(), LIMIT = 10;\n",
            1,
            "const m: Map<string, number> = new Map(), LIMIT = 20;",
            "LIMIT",
            True,
            id="after-generic-annotation",
        ),
        pytest.param(
            "const OTHER = f(a, LIMIT = 10);\n",
            1,
            "const OTHER = f(a, LIMIT = 20);",
            "LIMIT",
            False,
            id="bracketed-comma-is-not-a-declarator",
        ),
        pytest.param(
            "export const LIMIT = 10;\nconst identity = <T, LIMIT = number>(x: T) => x;\n",
            2,
            "const identity = <T, LIMIT = string>(x: T) => x;",
            "LIMIT",
            False,
            id="generic-arrow-type-parameter-is-not-a-declarator",
        ),
        pytest.param(
            "export const LIMIT = 10;\nconst f = <T, LIMIT = (a: T) => void>(x: T) => x;\n",
            2,
            "const f = <T, LIMIT = (a: T) => string>(x: T) => x;",
            "LIMIT",
            False,
            id="generic-default-with-arrow-type",
        ),
        pytest.param(
            "export const LIMIT = 10;\nconst f = function <T, LIMIT = number>(x: T) { return x; };\n",
            2,
            "const f = function <T, LIMIT = string>(x: T) { return x; };",
            "LIMIT",
            False,
            id="generic-function-expression-type-parameter",
        ),
        pytest.param(
            "export const LIMIT = 10;\nlet make: new <T, LIMIT = number>(x: T) => T;\n",
            2,
            "let make: new <T, LIMIT = string>(x: T) => T;",
            "LIMIT",
            False,
            id="construct-signature-type-parameter",
        ),
        pytest.param(
            "const OTHER = renew < 2, LIMIT = 3 > (4);\n",
            1,
            "const OTHER = renew < 2, LIMIT = 5 > (4);",
            "LIMIT",
            True,
            id="new-suffixed-operand-is-not-a-construct-signature",
        ),
        pytest.param(
            "const g = <T,>(x: T) => x, LIMIT = 10;\n",
            1,
            "const g = <T,>(x: T) => x, LIMIT = 20;",
            "LIMIT",
            True,
            id="declarator-after-generic-arrow",
        ),
        pytest.param(
            "const ok = a < b, LIMIT = 10\n",
            1,
            "const ok = a < b, LIMIT = 20",
            "LIMIT",
            True,
            id="less-than-comparison-is-not-a-generic",
        ),
        pytest.param(
            "export const LIMIT = 10;\nconst f = <T, LIMIT = Map<string, number>>(x: T) => x;\n",
            2,
            "const f = <T, LIMIT = Map<string, string>>(x: T) => x;",
            "LIMIT",
            False,
            id="nested-generic-type-parameter-default",
        ),
        pytest.param(
            "const ok = a < b, LIMIT = 10; f > (x);\n",
            1,
            "const ok = a < b, LIMIT = 20; f > (x);",
            "LIMIT",
            True,
            id="comparison-ends-at-semicolon",
        ),
        pytest.param(
            "const OTHER = 1 < 2, LIMIT = 3 > (4);\n",
            1,
            "const OTHER = 1 < 2, LIMIT = 5 > (4);",
            "LIMIT",
            True,
            id="comparisons-around-declarator-are-not-a-generic",
        ),
        pytest.param(
            "const OTHER = 1 < 2, LIMIT = 3 > (\n  4\n);\n",
            2,
            "  5",
            "LIMIT",
            True,
            id="comparisons-around-declarator-with-multiline-operand",
        ),
        pytest.param(
            "const OTHER = className < 2, LIMIT = 3 > (4);\n",
            1,
            "const OTHER = className < 2, LIMIT = 5 > (4);",
            "LIMIT",
            True,
            id="class-prefixed-operand-is-not-a-class-head",
        ),
        pytest.param(
            "const OTHER = functional < 2, LIMIT = 3 > (4);\n",
            1,
            "const OTHER = functional < 2, LIMIT = 5 > (4);",
            "LIMIT",
            True,
            id="function-prefixed-operand-is-not-a-function-head",
        ),
        pytest.param(
            "const OTHER = base\n  .method(), LIMIT = [\n  10,\n];\n",
            3,
            "  20,",
            "LIMIT",
            True,
            id="declarator-after-leading-continuation",
        ),
        pytest.param(
            "const OTHER = base\n  /*\n   * note\n   */\n  .method(), LIMIT = [\n  10,\n];\n",
            6,
            "  20,",
            "LIMIT",
            True,
            id="declarator-after-comment-and-leading-continuation",
        ),
        pytest.param(
            "const OTHER = base\n/* explanation\n note */ .method(), LIMIT = [\n  10,\n];\n",
            4,
            "  20,",
            "LIMIT",
            True,
            id="declarator-after-unstarred-comment-close-continuation",
        ),
        pytest.param(
            "const OTHER = base\n/* explanation\n note */ foo(), LIMIT = [\n  10,\n];\n",
            4,
            "  20,",
            "LIMIT",
            False,
            id="new-statement-after-unstarred-comment-close",
        ),
        pytest.param(
            "const OTHER = base\nfoo(), LIMIT = [\n  10,\n];\n",
            3,
            "  20,",
            "LIMIT",
            False,
            id="new-statement-after-complete-declaration",
        ),
        pytest.param(
            "const OTHER = `hello\nworld`, LIMIT = [\n  10\n];\n",
            3,
            "  20",
            "LIMIT",
            True,
            id="declarator-after-multiline-template",
        ),
        pytest.param(
            "const OTHER = `a ${x}\nb ${y}\nc`, LIMIT = [\n  10\n];\n",
            4,
            "  20",
            "LIMIT",
            True,
            id="declarator-after-template-interpolation-line",
        ),
        pytest.param(
            "const OTHER = 1 /* note\n  more */\nfoo(), LIMIT = [\n  10,\n];\n",
            4,
            "  20,",
            "LIMIT",
            False,
            id="new-statement-after-declaration-block-comment",
        ),
        pytest.param(
            "const OTHER =\n`a\nb`\nconst LIMIT = 1;\n",
            4,
            "const LIMIT = 2;",
            "LIMIT",
            True,
            id="declaration-after-semicolon-free-multiline-template",
        ),
        pytest.param(
            "const OTHER =\n`a\nb`\nconst LIMIT = 1;\n",
            4,
            "const LIMIT = 2;",
            "OTHER",
            False,
            id="semicolon-free-multiline-template-ends-declaration",
        ),
        pytest.param(
            'const OTHER =\n  "hello"\nconst LIMIT = [\n  10\n];\n',
            4,
            "  20",
            "LIMIT",
            True,
            id="declaration-after-standalone-string-operand",
        ),
        pytest.param(
            'const OTHER =\n  "hello"\nconst LIMIT = [\n  10\n];\n',
            4,
            "  20",
            "OTHER",
            False,
            id="standalone-string-operand-ends-declaration",
        ),
        pytest.param(
            "const LIMIT = `a\n// cleanup`\nconst OTHER = 1;\n",
            2,
            "// teardown`",
            "LIMIT",
            True,
            id="semicolon-free-multiline-template-closing-comment-marker",
        ),
        pytest.param(
            "const LIMIT =\n`a\n// cleanup`\nconst OTHER = 1;\n",
            3,
            "// teardown`",
            "LIMIT",
            True,
            id="wrapped-semicolon-free-multiline-template-closing-comment-marker",
        ),
        pytest.param(
            "const OTHER = `hello\nworld`, LIMIT = 10\nconst NEXT = 3;\n",
            3,
            "const NEXT = 4;",
            "LIMIT",
            False,
            id="declarator-on-multiline-template-closing-line",
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_declaration_binds_every_declarator(
    text: str, line: int, new: str, name: str, *, expected: bool
) -> None:
    """Each declarator of ``const A = 0, B = v`` is bound to its own initializer span.

    (PRRT_kwDOSJAM6s63QfAU)
    """
    old = text.splitlines()[line - 1]
    diff = _diff(_WEB, f"@@ -{line} +{line} @@", f"-{old}", f"+{new}")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, f"`{name}` is too low") is expected


@pytest.mark.parametrize(
    ("path", "text", "line", "expected"),
    [
        pytest.param(_INBOUND, "LIMIT = 10; OTHER = (\n    1\n)\n", 2, False, id="python-next"),
        pytest.param(_INBOUND, "LIMIT = 10; OTHER = (\n    1\n)\n", 1, True, id="python-own"),
        pytest.param(
            _INBOUND, "LIMIT = (\n    1\n); OTHER = (\n    1\n)\n", 4, False, id="python-wrapped"
        ),
        pytest.param(
            _WEB, "export const LIMIT = 10; const OTHER = [\n  1,\n];\n", 2, False, id="js-ts-next"
        ),
    ],
)
@pytest.mark.unit
async def test_binding_ends_at_its_semicolon_boundary(
    path: str, text: str, line: int, *, expected: bool
) -> None:
    """A ``;``-separated next statement is not the binding's (PRRT_kwDOSJAM6s63Pd7z)."""
    old = text.splitlines()[line - 1]
    diff = _diff(path, f"@@ -{line} +{line} @@", f"-{old}", f"+{old.replace('1', '2')}")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("path", "text", "line", "expected"),
    [
        pytest.param(_INBOUND, "OTHER = 0; LIMIT = (\n    10\n)\n", 2, True, id="python-later"),
        pytest.param(
            _INBOUND, "OTHER = 0; LIMIT = 0; X = (\n    10\n)\n", 2, False, id="python-middle"
        ),
        pytest.param(
            _INBOUND, "OTHER = (\n    0\n); LIMIT = [\n    10\n]\n", 4, True, id="python-wrapped"
        ),
        pytest.param(
            _INBOUND, "LIMIT = 0; LIMIT = (\n    10\n)\n", 2, True, id="python-last-rebinding"
        ),
        pytest.param(
            _INBOUND, "if ready: OTHER = 0; LIMIT = (\n    10\n)\n", 2, False, id="python-body"
        ),
        pytest.param(
            _INBOUND, "class C: OTHER = 0; LIMIT = (\n    10\n)\n", 2, False, id="class-body"
        ),
        pytest.param(
            _INBOUND, "OTHER = ';'; LIMIT = (\n    10\n)\n", 2, True, id="python-string-semicolon"
        ),
        pytest.param(
            _INBOUND, "OTHER = 'é'; LIMIT = (\n    10\n)\n", 2, True, id="python-non-ascii-column"
        ),
        pytest.param(
            _INBOUND,
            "OTHER = 0; LIMIT = (\n    10\n)\nfor LIMIT in ():\n    pass\n",
            2,
            False,
            id="python-rebound-later",
        ),
        pytest.param(
            _WEB, "const OTHER = 0; const LIMIT = [\n  10,\n];\n", 2, True, id="js-ts-later"
        ),
    ],
)
@pytest.mark.unit
async def test_semicolon_separated_statements_each_bind(
    path: str, text: str, line: int, *, expected: bool
) -> None:
    """A binding after a ``;`` on the same line is found, over its own lines.

    (PRRT_kwDOSJAM6s63Sh3G)
    """
    old = text.splitlines()[line - 1]
    diff = _diff(path, f"@@ -{line} +{line} @@", f"-{old}", f"+{old.replace('1', '2')}")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("path", "text", "line", "expected"),
    [
        pytest.param(
            _WEB,
            "export const LIMIT = 10;\nlet unrelated;\nLIMIT: unrelated = (\n  1\n);\n",
            4,
            False,
            id="js-ts-label",
        ),
        pytest.param(_WEB, "export const LIMIT: number = (\n  1\n);\n", 2, True, id="js-ts-typed"),
        pytest.param(_INBOUND, "LIMIT: int = (\n    1\n)\n", 2, True, id="python-annotated"),
    ],
)
@pytest.mark.unit
async def test_js_ts_label_binds_nothing(
    path: str, text: str, line: int, *, expected: bool
) -> None:
    """A JS/TS ``LIMIT: target = ...`` labels a statement and binds no ``LIMIT``.

    (PRRT_kwDOSJAM6s63SzHq)
    """
    old = text.splitlines()[line - 1]
    diff = _diff(path, f"@@ -{line} +{line} @@", f"-{old}", f"+{old.replace('1', '2')}")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("text", "line", "expected"),
    [
        pytest.param(
            "export const LIMIT = 10;\n  export function limit() {\n    return 1;\n  }\n",
            3,
            False,
            id="indented-statement-after-semicolon",
        ),
        pytest.param(
            'export const LIMIT = "10"\n  export function limit() {\n    return 1;\n  }\n',
            3,
            False,
            id="indented-statement-after-asi",
        ),
        pytest.param(
            "export const LIMIT = 10; // max\n  limit();\n", 2, False, id="trailing-comment"
        ),
        pytest.param("export const LIMIT =\n  10;\n", 2, True, id="operator-continuation"),
        pytest.param(
            "export const LIMIT = base\n  .scaled(10);\n", 2, True, id="leading-continuation"
        ),
        pytest.param(
            "export const LIMIT =\n10;\n", 2, True, id="column-zero-operator-continuation"
        ),
        pytest.param(
            "export const LIMIT = base\n.scaled(10);\n",
            2,
            True,
            id="column-zero-leading-continuation",
        ),
        pytest.param(
            "export const LIMIT = 10\nexport const OTHER = 1;\n",
            2,
            False,
            id="column-zero-statement-after-asi",
        ),
        pytest.param(
            "export const LIMIT = 10\n++counter1;\n", 2, False, id="prefix-increment-after-asi"
        ),
        pytest.param(
            "export const LIMIT = 10\n  --counter1;\n", 2, False, id="prefix-decrement-after-asi"
        ),
        pytest.param(
            "export const LIMIT = base\n+ step1;\n", 2, True, id="leading-binary-plus-continuation"
        ),
        pytest.param(
            "export const LIMIT = value\n  instanceof Expected1;\n",
            2,
            True,
            id="leading-instanceof-continuation",
        ),
        pytest.param(
            "export const LIMIT = key\n  in table1;\n", 2, True, id="leading-in-continuation"
        ),
        pytest.param(
            "export const LIMIT = 10\ninner1();\n", 2, False, id="in-prefixed-identifier-after-asi"
        ),
        pytest.param(
            "export const LIMIT = await\n  loadLimit(10);\n",
            2,
            True,
            id="trailing-await-continuation",
        ),
        pytest.param(
            "export const LIMIT = typeof\n  limit1;\n", 2, True, id="trailing-typeof-continuation"
        ),
        pytest.param(
            "export const LIMIT = new\n  Limit(10);\n", 2, True, id="trailing-new-continuation"
        ),
        pytest.param(
            "export const LIMIT = cache.delete\nrun1();\n",
            2,
            False,
            id="keyword-named-property-after-asi",
        ),
        pytest.param(
            "export const LIMIT = cache. delete\nexport const OTHER = 1;\n",
            2,
            False,
            id="spaced-keyword-named-property-after-asi",
        ),
        pytest.param(
            "export const LIMIT = cache. /* key */ delete\nexport const OTHER = 1;\n",
            2,
            False,
            id="commented-keyword-named-property-after-asi",
        ),
        pytest.param(
            "export const LIMIT = cache.\n  delete\nexport const OTHER = 1;\n",
            3,
            False,
            id="wrapped-keyword-named-property-after-asi",
        ),
        pytest.param(
            "export const LIMIT = cache.\n  // key\n  delete\nexport const OTHER = 1;\n",
            4,
            False,
            id="comment-wrapped-keyword-named-property-after-asi",
        ),
        pytest.param(
            "export const LIMIT = cache.\n  delete\n  (1);\n", 3, True, id="wrapped-property-call"
        ),
        pytest.param(
            "export const LIMIT = renew\nrun1();\n", 2, False, id="keyword-suffixed-identifier"
        ),
        pytest.param(
            "export const LIMIT = value satisfies\n  Expected1;\n",
            2,
            True,
            id="trailing-satisfies-continuation",
        ),
        pytest.param(
            "export const LIMIT = value as\n  Expected1;\n", 2, True, id="trailing-as-continuation"
        ),
        pytest.param(
            'export const LIMIT = "a" as\n  Expected1;\n', 2, True, id="trailing-as-after-string"
        ),
        pytest.param(
            "export const LIMIT = value /* why */ as\n  Expected1;\n",
            2,
            True,
            id="trailing-as-after-commented-operand",
        ),
        pytest.param(
            "export const LIMIT = value! as\n  Expected1;\n",
            2,
            True,
            id="trailing-as-after-non-null-assertion",
        ),
        pytest.param(
            "export const LIMIT = items[0] /* set */ !! satisfies\n  Expected1;\n",
            2,
            True,
            id="trailing-satisfies-after-commented-non-null-assertion",
        ),
        pytest.param(
            "export const LIMIT = a < b > as\n  Expected1;\n",
            2,
            False,
            id="trailing-as-after-spaced-comparison",
        ),
        pytest.param(
            "export const LIMIT = value as keyof\n  Expected1;\n",
            2,
            True,
            id="trailing-keyof-type-operand-continuation",
        ),
        pytest.param(
            "export const LIMIT = value satisfies /* why */ readonly\n  Expected1[];\n",
            2,
            True,
            id="trailing-readonly-type-operand-continuation",
        ),
        pytest.param(
            "export const LIMIT = value as\n  keyof\n  Expected1;\n",
            3,
            True,
            id="wrapped-keyof-type-operand-continuation",
        ),
        pytest.param(
            "export const LIMIT = fn<\n  number> as\n  keyof\n  Expected1;\n",
            4,
            True,
            id="wrapped-keyof-after-wrapped-instantiation-expression",
        ),
        pytest.param(
            "const as = 0;\nexport const LIMIT = as\n  keyof\nexport const OTHER = 1;\n",
            4,
            False,
            id="wrapped-keyof-after-as-named-identifier",
        ),
        pytest.param(
            "export const LIMIT = value as T extends\n  U ? Expected1 : Other;\n",
            2,
            True,
            id="trailing-extends-conditional-type-continuation",
        ),
        pytest.param(
            "export const LIMIT = value as T extends /* why */\n  U ? Expected1 : Other;\n",
            2,
            True,
            id="commented-trailing-extends-conditional-type-continuation",
        ),
        pytest.param(
            "export const LIMIT = config.extends\nexport const OTHER = 1;\n",
            2,
            False,
            id="extends-named-property-after-asi",
        ),
        pytest.param(
            "export const LIMIT = config.\n  extends\nexport const OTHER = 1;\n",
            3,
            False,
            id="wrapped-extends-named-property-after-asi",
        ),
        pytest.param(
            "const keyof = 0;\nexport const LIMIT = keyof\nexport const OTHER = 1;\n",
            3,
            False,
            id="keyof-named-identifier-after-asi",
        ),
        pytest.param(
            "export const LIMIT = config.as readonly\nexport const OTHER = 1;\n",
            2,
            False,
            id="readonly-after-as-named-property-after-asi",
        ),
        pytest.param(
            "const as = 0;\nexport const LIMIT = !as\nexport const OTHER = 1;\n",
            3,
            False,
            id="negated-as-named-identifier-after-asi",
        ),
        pytest.param(
            "const satisfies = 0;\nexport const LIMIT = satisfies\nexport const OTHER = 1;\n",
            3,
            False,
            id="satisfies-named-identifier-after-asi",
        ),
        pytest.param(
            "const as = 0;\nexport const LIMIT = as\nexport const OTHER = 1;\n",
            3,
            False,
            id="as-named-identifier-after-asi",
        ),
        pytest.param(
            "export const LIMIT = typeof satisfies\nexport const OTHER = 1;\n",
            2,
            False,
            id="satisfies-named-operand-after-asi",
        ),
        pytest.param(
            "export const LIMIT =\n  satisfies\nexport const OTHER = 1;\n",
            3,
            False,
            id="wrapped-satisfies-named-identifier-after-asi",
        ),
        pytest.param(
            "export const LIMIT = config.as\nrun1();\n", 2, False, id="as-named-property-after-asi"
        ),
        pytest.param(
            "export const LIMIT = alias\nrun1();\n", 2, False, id="as-suffixed-identifier"
        ),
        pytest.param(
            "export const LIMIT = value as Array<number>\nexport const OTHER = 1;\n",
            2,
            False,
            id="closing-type-arguments-after-asi",
        ),
        pytest.param(
            "export const LIMIT = value as Array <number>\nexport const OTHER = 1;\n",
            2,
            False,
            id="spaced-closing-type-arguments-after-asi",
        ),
        pytest.param(
            "export const LIMIT = value satisfies ns.List /* n */ <number>\nrun1();\n",
            2,
            False,
            id="commented-closing-type-arguments-after-asi",
        ),
        pytest.param(
            "const as = 0;\nexport const LIMIT = as < b >\n  c1;\n",
            3,
            True,
            id="as-named-identifier-comparison-continuation",
        ),
        pytest.param(
            "export const LIMIT = value as Map<string, Array<(a: number) => void>>\nrun1();\n",
            2,
            False,
            id="nested-closing-type-arguments-after-asi",
        ),
        pytest.param(
            "export const LIMIT = value as Array<\n  number>\nexport const OTHER = 1;\n",
            3,
            False,
            id="wrapped-closing-type-arguments-after-asi",
        ),
        pytest.param(
            "export const LIMIT = value as Array<\n  number>\nrun1();\n",
            3,
            False,
            id="wrapped-closing-type-arguments-before-statement",
        ),
        pytest.param(
            "export const LIMIT = a <\n  b >\n  c1;\n",
            3,
            True,
            id="wrapped-trailing-comparison-continuation",
        ),
        pytest.param(
            "export const LIMIT = a < b >\n  c1;\n", 2, True, id="trailing-comparison-continuation"
        ),
        pytest.param(
            "export const LIMIT = a >>\n  b1;\n", 2, True, id="trailing-shift-continuation"
        ),
        pytest.param(
            "export const LIMIT = (a) =>\n  a1;\n", 2, True, id="trailing-arrow-continuation"
        ),
        pytest.param(
            "export const LIMIT = !\n    Boolean(10);\n",
            2,
            True,
            id="trailing-unary-negation-continuation",
        ),
        pytest.param(
            "export const LIMIT = !!\n    Boolean(10);\n",
            2,
            True,
            id="trailing-double-negation-continuation",
        ),
        pytest.param(
            "export const LIMIT = value!\nrun1();\n", 2, False, id="non-null-assertion-after-asi"
        ),
        pytest.param(
            "export const LIMIT = items[0] !\nrun1();\n",
            2,
            False,
            id="spaced-non-null-assertion-after-asi",
        ),
        pytest.param(
            'export const LIMIT = "a"!\nrun1();\n', 2, False, id="string-non-null-assertion"
        ),
        pytest.param(
            "export const LIMIT = `a${b}` !\nrun1();\n",
            2,
            False,
            id="template-non-null-assertion",
        ),
        pytest.param(
            "export const LIMIT = /a/g!\nrun1();\n", 2, False, id="regex-non-null-assertion"
        ),
        pytest.param(
            'export const LIMIT = "a" /* x */ /* y */!\nrun1();\n',
            2,
            False,
            id="commented-string-non-null-assertion",
        ),
        pytest.param(
            "export const LIMIT = /* why */ !\n    Boolean(10);\n",
            2,
            True,
            id="comment-then-unary-negation-continuation",
        ),
        pytest.param("export const LIMIT = `a\n  b`;\n", 2, True, id="template-literal"),
        pytest.param(
            "export const LIMIT = 1\n  /* unrelated\n     note 1 */\n  export const OTHER = 2;\n",
            3,
            False,
            id="standalone-block-comment",
        ),
        pytest.param(
            "export const LIMIT = 2\n/* unrelated, and\n */ export const OTHER = 1;\n",
            3,
            False,
            id="statement-after-block-comment-close",
        ),
        pytest.param(
            "export const LIMIT = base\n/* scale\n   note */ .scaled(10);\n",
            3,
            True,
            id="continuation-after-block-comment-close",
        ),
        pytest.param(
            "export const LIMIT = 2 /* unrelated\n*/ export const OTHER = 1;\n",
            2,
            False,
            id="statement-after-trailing-block-comment-close",
        ),
        pytest.param(
            "export const LIMIT = 2 /* unrelated\n   note */\nexport const OTHER = 1;\n",
            3,
            False,
            id="statement-after-trailing-multi-line-block-comment",
        ),
        pytest.param(
            "export const LIMIT = base /* scale\n */ .scaled(10);\n",
            2,
            True,
            id="continuation-after-trailing-block-comment-close",
        ),
        pytest.param(
            "export const LIMIT = base\n  /* why */ .scaled(10);\n",
            2,
            True,
            id="block-comment-then-continuation",
        ),
        pytest.param(
            "export const LIMIT = base\n/* scale for production */\n.scaled(10);\n",
            3,
            True,
            id="standalone-block-comment-then-continuation",
        ),
        pytest.param(
            "export const LIMIT = base\n  /* scale\n     note */\n  .scaled(10);\n",
            4,
            True,
            id="multi-line-block-comment-then-continuation",
        ),
        pytest.param(
            "export const LIMIT = base +\n/* why */\n10;\n",
            3,
            True,
            id="operator-then-block-comment",
        ),
        pytest.param(
            "export const LIMIT = base\n/* note 1 */\nlimit();\n",
            2,
            False,
            id="block-comment-before-next-statement",
        ),
        pytest.param(
            "export const LIMIT = base\n/* note 1 */\n", 2, False, id="trailing-block-comment"
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_binding_ends_at_its_statement_boundary(
    text: str, line: int, *, expected: bool
) -> None:
    """Indentation is not scope in JS/TS (PRRT_kwDOSJAM6s63PU_-)."""
    old = text.splitlines()[line - 1]
    diff = _diff(_WEB, f"@@ -{line} +{line} @@", f"-{old}", f"+{old.replace('1', '2')}")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param("src/app/web/team.js", True, id="javascript-comparison"),
        pytest.param(_WEB, False, id="typescript-instantiation-expression"),
    ],
)
@pytest.mark.unit
async def test_js_ts_compact_trailing_angle_is_type_arguments_only_in_typescript(
    path: str, *, expected: bool
) -> None:
    """JavaScript has no type arguments: ``a<b >`` is a comparison (PRRT_kwDOSJAM6s63TdLy)."""
    text = "export const LIMIT = a<b >\n  c1;\n"
    diff = _diff(path, "@@ -2 +2 @@", "-  c1;", "+  c2;")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("path", "text", "line", "expected"),
    [
        pytest.param(
            "src/app/web/team.js",
            "export const LIMIT = fn<number> as\n  Handler1;\n",
            2,
            False,
            id="javascript-comparison",
        ),
        pytest.param(
            _WEB,
            "export const LIMIT = fn<number> as\n  Handler1;\n",
            2,
            True,
            id="typescript-instantiation-expression",
        ),
        pytest.param(
            _WEB,
            "export const LIMIT = fn<\n  number\n> as\n  Handler1;\n",
            4,
            True,
            id="typescript-wrapped-instantiation-expression",
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_type_operator_after_instantiation_expression_continues_only_in_typescript(
    path: str, text: str, line: int, *, expected: bool
) -> None:
    """``fn<number> as`` asserts a completed instantiation (PRRT_kwDOSJAM6s63YH3s)."""
    diff = _diff(path, f"@@ -{line} +{line} @@", "-  Handler1;", "+  Handler2;")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("path", "element"),
    [
        pytest.param("src/app/web/team.tsx", "<Widget />", id="tsx-self-closing"),
        pytest.param("src/app/web/team.jsx", "<></>", id="jsx-fragment"),
        pytest.param("src/app/web/team.tsx", "<div>hi</div>", id="tsx-closing-tag"),
        pytest.param("src/app/web/team.jsx", "<ui.Panel open />", id="jsx-member-tag"),
        # PRRT_kwDOSJAM6s63Y_3L: a concise arrow body roots its JSX element.
        pytest.param("src/app/web/team.tsx", "() => <Widget />;", id="tsx-arrow-body"),
        pytest.param("src/app/web/team.jsx", "() => <div>hi</div>;", id="jsx-arrow-body"),
        # PRRT_kwDOSJAM6s63ZCcb: a block body's ``}`` is code, not a JSX container end.
        pytest.param(
            "src/app/web/team.tsx", "() => { return <Widget />; };", id="tsx-arrow-block-body"
        ),
        pytest.param(
            "src/app/web/team.jsx", "() => { return <div>hi</div>; };", id="jsx-arrow-block-body"
        ),
        # PRRT_kwDOSJAM6s63ZMkA: an ASI-terminated block body's ``}`` is code too.
        pytest.param(
            "src/app/web/team.tsx", "() => { return <Widget /> };", id="tsx-arrow-asi-block-body"
        ),
        pytest.param(
            "src/app/web/team.jsx",
            "function () { return <div>hi</div> };",
            id="jsx-function-asi-block-body",
        ),
        pytest.param(
            "src/app/web/team.tsx",
            "function (p: P): JSX.Element { return <Widget /> };",
            id="tsx-function-return-type-asi-block-body",
        ),
        pytest.param(
            "src/app/web/team.tsx",
            "{ render() { return <Widget /> } };",
            id="tsx-method-shorthand-asi-block-body",
        ),
        # PRRT_kwDOSJAM6s63ZGB0: an object literal's ``}`` is code, not a JSX container end.
        pytest.param(
            "src/app/web/team.tsx", "{ node: <Widget /> };", id="tsx-object-property-value"
        ),
        pytest.param(
            "src/app/web/team.jsx", "{ a: 1, 'b': <div>hi</div> };", id="jsx-object-property-value"
        ),
        pytest.param(
            "src/app/web/team.tsx", "{ a: { b: () => <Widget /> } };", id="tsx-nested-object-value"
        ),
        pytest.param(
            "src/app/web/team.tsx", "{ node: c ? <A /> : (<B />) };", id="tsx-object-ternary-value"
        ),
    ],
)
@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        pytest.param("`VIEW` is wrong", False, id="unchanged-jsx-binding"),
        pytest.param("`OTHER` is wrong", True, id="changed-next-binding"),
    ],
)
@pytest.mark.unit
async def test_js_ts_completed_jsx_initializer_ends_its_statement(
    path: str, element: str, comment: str, *, expected: bool
) -> None:
    """A completed JSX element's ``>`` is no comparison (PRRT_kwDOSJAM6s63VJ_g)."""
    text = f"export const VIEW = {element}\nexport const OTHER = 1;\n"
    diff = _diff(path, "@@ -2 +2 @@", "-export const OTHER = 1;", "+export const OTHER = 2;")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, comment) is expected


@pytest.mark.parametrize(
    "operator",
    [
        pytest.param("as\n  Expected", id="as-continued-on-next-line"),
        pytest.param("as Expected", id="as-same-line"),
        pytest.param("satisfies\n  Expected", id="satisfies-continued-on-next-line"),
    ],
)
@pytest.mark.parametrize(
    ("comment", "expected"),
    [
        pytest.param("`VIEW` is wrong", False, id="unchanged-jsx-binding"),
        pytest.param("`OTHER` is wrong", True, id="changed-next-binding"),
    ],
)
@pytest.mark.unit
async def test_tsx_type_operator_after_completed_jsx_element_stays_code(
    operator: str, comment: str, *, expected: bool
) -> None:
    """``<Widget /> as T`` keeps ``as`` code, not JSX text (PRRT_kwDOSJAM6s63XLQg)."""
    path = "src/app/web/team.tsx"
    text = f"export const VIEW = <Widget /> {operator};\nexport const OTHER = 1;\n"
    line = text.count("\n")
    diff = _diff(
        path, f"@@ -{line} +{line} @@", "-export const OTHER = 1;", "+export const OTHER = 2;"
    )
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, comment) is expected


@pytest.mark.parametrize(
    "element",
    [
        pytest.param("<Widget />", id="self-closing"),
        pytest.param("<div>hi</div>", id="closing-tag"),
        pytest.param("<></>", id="fragment"),
    ],
)
@pytest.mark.parametrize("operator", ["as", "satisfies"])
@pytest.mark.unit
async def test_tsx_wrapped_type_operator_after_completed_jsx_element_continues(
    element: str, operator: str
) -> None:
    """``<Widget /> as`` wraps to its type line (PRRT_kwDOSJAM6s63YMpb)."""
    path = "src/app/web/team.tsx"
    text = f"export const VIEW = {element} {operator}\n  Expected1;\n"
    diff = _diff(path, "@@ -2 +2 @@", "-  Expected1;", "+  Expected2;")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`VIEW` is wrong") is True


@pytest.mark.parametrize(
    "element",
    [
        pytest.param("<Widget value={a > b} />", id="comparison"),
        pytest.param("<Widget render={() => <Icon />} />", id="nested-element"),
        pytest.param("<Widget style={{ gap: a > b ? 1 : 2 }} />", id="nested-braces"),
        # PRRT_kwDOSJAM6s63Yzs5: a JSX type argument's ``>`` does not end the tag.
        pytest.param("<Widget<Props> value={10} />", id="type-argument"),
        pytest.param("<Widget<Map<K, V>> value={10} />", id="nested-type-arguments"),
        # PRRT_kwDOSJAM6s63Y7R6: the ``>`` of an ``=>`` function type is not a closing angle.
        pytest.param("<Widget<(x: number) => void> value={10} />", id="arrow-type-argument"),
    ],
)
@pytest.mark.unit
async def test_tsx_attribute_expression_gt_does_not_end_the_opening_tag(element: str) -> None:
    """A ``>`` inside ``{...}`` keeps the tag open (PRRT_kwDOSJAM6s63YQ8-)."""
    path = "src/app/web/team.tsx"
    text = f"export const VIEW = {element}\nexport const OTHER = 1;\n"
    diff = _diff(path, "@@ -2 +2 @@", "-export const OTHER = 1;", "+export const OTHER = 2;")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`VIEW` is wrong") is False
    assert await _changes_named_target(runner, "`OTHER` is wrong") is True


@pytest.mark.parametrize(
    ("path", "text", "line", "expected"),
    [
        pytest.param(
            "src/app/web/team.tsx",
            "export const VIEW = <Widget\n  value={10}\n/>;\n",
            2,
            True,
            id="tsx-multi-line-opening-tag",
        ),
        pytest.param(
            "src/app/web/team.jsx",
            "export const VIEW = <Widget\n  title='x'\n  value={10}\n/>\nconst OTHER = 1;\n",
            3,
            True,
            id="jsx-attribute-after-string-attribute",
        ),
        pytest.param(
            "src/app/web/team.tsx",
            "export const VIEW = <div>\n  <Widget\n    value={10}\n  />\n</div>;\n",
            3,
            True,
            id="tsx-nested-multi-line-opening-tag",
        ),
        pytest.param(
            "src/app/web/team.tsx",
            "export const VIEW = <Widget\n  value={1}\n/>\nconst OTHER = 10;\n",
            4,
            False,
            id="tsx-next-binding-after-line-leading-self-close",
        ),
        pytest.param(
            "src/app/web/team.tsx",
            "export const VIEW = a <b\n  value={10}\n",
            2,
            False,
            id="tsx-comparison-after-operand",
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_multi_line_jsx_opening_tag_keeps_its_statement_open(
    path: str, text: str, line: int, *, expected: bool
) -> None:
    """A JSX opening tag spanning lines keeps its initializer open (PRRT_kwDOSJAM6s63Wpyz)."""
    old = text.splitlines()[line - 1]
    diff = _diff(path, f"@@ -{line} +{line} @@", f"-{old}", f"+{old.replace('10', '11')}")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`VIEW` is wrong") is expected


@pytest.mark.parametrize(
    ("path", "text", "line", "expected"),
    [
        pytest.param(_WEB, "  export const LIMIT = 1;\n", 1, True, id="js-ts-indented-module"),
        pytest.param(
            _WEB, "const a = 0;\n    LIMIT = 1;\n", 2, True, id="js-ts-indented-reassignment"
        ),
        pytest.param(
            _WEB,
            "export function f() {\n  const LIMIT = 1;\n}\n",
            2,
            False,
            id="js-ts-function-body",
        ),
        pytest.param(_WEB, "if (on) {\n  LIMIT = 1;\n}\n", 2, False, id="js-ts-block-body"),
        pytest.param(_INBOUND, "if on:\n    LIMIT = 1\n", 2, False, id="python-indented"),
        pytest.param(_WEB, "if (on)\n  LIMIT = 1;\n", 2, False, id="js-ts-braceless-if"),
        pytest.param(_WEB, "if (on)\nLIMIT = 1;\n", 2, False, id="js-ts-braceless-col0"),
        pytest.param(
            _WEB,
            "for (let i = 0; i < n; i++)\n  LIMIT = 1;\n",
            2,
            False,
            id="js-ts-braceless-for",
        ),
        pytest.param(_WEB, "if (on) {\n  a();\n} else\n  LIMIT = 1;\n", 4, False, id="js-ts-else"),
        pytest.param(_WEB, "const f = () =>\n  LIMIT = 1;\n", 2, False, id="js-ts-arrow-body"),
        pytest.param(
            _WEB,
            "if (on)\n  a = 0;\n  LIMIT = 1;\n",
            3,
            True,
            id="js-ts-after-braceless-body",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 1;\nif (on)\n  LIMIT = 3;\n",
            3,
            False,
            id="js-ts-braceless-write-after-binding",
        ),
        pytest.param(
            _WEB,
            "if (enabled) initialize()\nexport const LIMIT = 1;\n",
            2,
            True,
            id="js-ts-binding-after-completed-conditional-body",
        ),
        pytest.param(
            _WEB,
            "if (enabled) initialize()\nexport const LIMIT = [\n  1,\n];\n",
            3,
            True,
            id="js-ts-multiline-binding-after-completed-conditional-body",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nif (check(on))\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-nested-call-head",
        ),
        pytest.param(
            _WEB,
            "init())\nexport const LIMIT = 1;\n",
            2,
            True,
            id="js-ts-binding-after-unbalanced-close",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nif (on) while (check())\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-nested-loop-head",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nif (on) for (const x of xs)\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-nested-for-head",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nif (on) do\n  LIMIT = 1;\nwhile (check());\n",
            3,
            False,
            id="js-ts-braceless-body-after-nested-do-head",
        ),
        pytest.param(
            _WEB,
            "if (on) while (check()) step()\nexport const LIMIT = 1;\n",
            2,
            True,
            id="js-ts-binding-after-completed-nested-loop-body",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nouter: while (check())\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-labelled-loop-head",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nif (on) outer: for (const x of xs)\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-nested-labelled-loop-head",
        ),
        pytest.param(
            _WEB,
            "do { work() } while (enabled)\nexport const LIMIT = [\n  1\n];\n",
            3,
            True,
            id="js-ts-binding-after-do-while-terminator",
        ),
        pytest.param(
            _WEB,
            "do {\n  work()\n} while (enabled)\nexport const LIMIT = 1;\n",
            4,
            True,
            id="js-ts-binding-after-multiline-do-while-terminator",
        ),
        pytest.param(
            _WEB,
            "do { work() }\nwhile (enabled)\nexport const LIMIT = [\n  1\n];\n",
            4,
            True,
            id="js-ts-binding-after-do-while-terminator-on-next-line",
        ),
        pytest.param(
            _WEB,
            "do\n{ work() }\nwhile (enabled)\nexport const LIMIT = 1;\n",
            4,
            True,
            id="js-ts-binding-after-do-while-with-wrapped-block-body",
        ),
        pytest.param(
            _WEB,
            "do work(); while (enabled)\nexport const LIMIT = [\n  1\n];\n",
            3,
            True,
            id="js-ts-binding-after-do-while-with-braceless-body",
        ),
        pytest.param(
            _WEB,
            "do\n  work()\nwhile (enabled)\nexport const LIMIT = 1;\n",
            4,
            True,
            id="js-ts-binding-after-do-while-with-wrapped-braceless-body",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\ndo work(); other(); while (check())\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-loop-head-following-a-do-body-statement",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nundone(); while (check())\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-loop-head-following-a-do-prefixed-call",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nqueue.do(); while (check())\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-loop-head-following-a-do-method-call",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\nwork()\nwhile (check())\n  LIMIT = 1;\n",
            4,
            False,
            id="js-ts-braceless-body-after-loop-head-on-line-after-statement",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\n{ work() } while (check())\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-loop-head-following-a-block",
        ),
        pytest.param(
            _WEB,
            "let LIMIT = 0;\n} while (check())\n  LIMIT = 1;\n",
            3,
            False,
            id="js-ts-braceless-body-after-loop-head-following-a-stray-close",
        ),
    ],
)
@pytest.mark.unit
async def test_indented_binding_counts_only_at_js_ts_module_scope(
    path: str, text: str, line: int, *, expected: bool
) -> None:
    """Leading whitespace is valid at JS/TS module scope (PRRT_kwDOSJAM6s63P5Zi).

    A brace-less control or concise-arrow body is not a statement of its own, so
    a write there is no binding (PRRT_kwDOSJAM6s63QK8_). A head whose body
    already follows its condition leaves the next statement at module scope
    (PRRT_kwDOSJAM6s63VYOX); one nested in another brace-less head still
    leaves it as a body (PRRT_kwDOSJAM6s63V2yC).
    """
    old = text.splitlines()[line - 1]
    diff = _diff(path, f"@@ -{line} +{line} @@", f"-{old}", f"+{old.replace('1', '2')}")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("left_text", "right_text", "diff", "expected"),
    [
        pytest.param(
            "export const LIMIT = 1;\n",
            "export const LIMIT = 1;\n  export const OTHER = 2;\n",
            _diff(_WEB, "@@ -1,0 +2 @@", "+  export const OTHER = 2;"),
            False,
            id="js-ts-indented-statement-after-binding",
        ),
        pytest.param(
            "export const LIMIT = base\n",
            "export const LIMIT = base\n  .scaled(10)\n",
            _diff(_WEB, "@@ -1,0 +2 @@", "+  .scaled(10)"),
            True,
            id="js-ts-insert-continuing-binding",
        ),
    ],
)
@pytest.mark.unit
async def test_insert_after_a_complete_binding_is_not_evidence(
    left_text: str, right_text: str, diff: str, *, expected: bool
) -> None:
    """Deeper indent does not extend an assignment (PRRT_kwDOSJAM6s63P5ZX)."""
    runner = _Probe(
        texts={(_LEFT, _WEB): left_text, (_RIGHT, _WEB): right_text},
        changed_paths=(_WEB,),
        diffs={_WEB: diff},
    )

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("path", "text", "expected"),
    [
        pytest.param(
            _WEB,
            "export let LIMIT = 10;\n({ LIMIT } = {LIMIT: 20});\n",
            False,
            id="js-ts-object-destructuring-reassignment",
        ),
        pytest.param(
            _WEB,
            "export let LIMIT = 10;\n[LIMIT] = [20];\n",
            False,
            id="js-ts-array-destructuring-reassignment",
        ),
        pytest.param(
            _WEB,
            "export let LIMIT = 10;\nwork(); ({ size: LIMIT } = options);\n",
            False,
            id="js-ts-renamed-destructuring-after-semicolon",
        ),
        pytest.param(
            _WEB,
            "export var LIMIT = 10;\nvar { LIMIT } = options;\n",
            False,
            id="js-ts-destructuring-redeclaration",
        ),
        pytest.param(
            _WEB,
            "export let LIMIT = 10;\nif (on)\n  [LIMIT] = [20];\n",
            False,
            id="js-ts-braceless-destructuring-body",
        ),
        pytest.param(
            _WEB,
            "export let LIMIT = 10;\n({ OTHER } = options);\n[LIMIT].map(f);\n",
            True,
            id="js-ts-destructuring-of-another-name",
        ),
        pytest.param(
            _WEB,
            "export let LIMIT = 10;\n[LIMIT\n",
            True,
            id="js-ts-unclosed-pattern-at-end-of-file",
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_destructuring_reassignment_withholds_the_binding(
    path: str, text: str, *, expected: bool
) -> None:
    """A module-scope destructuring target rebinds the name (PRRT_kwDOSJAM6s63W936)."""
    old = text.splitlines()[0]
    diff = _diff(path, "@@ -1 +1 @@", f"-{old}", f"+{old.replace('10', '11')}")
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "export let LIMIT = 10;\nif (true) LIMIT = 20;\n",
            False,
            id="inline-if-body",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (on) work(); else LIMIT = 20;\n",
            False,
            id="inline-else-body",
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor (const x of xs) LIMIT = x;\n",
            False,
            id="inline-for-body",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (on) {\n  LIMIT = 20;\n}\n",
            False,
            id="braced-block-body",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (on) LIMIT += 20;\n",
            False,
            id="inline-compound-assignment",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (on) LIMIT++;\n",
            False,
            id="inline-update-expression",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (on) [LIMIT] = [20];\n",
            False,
            id="inline-destructuring-body",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (a)\n  if (b) LIMIT = 20;\n",
            False,
            id="nested-inline-body",
        ),
        pytest.param(
            "export let LIMIT = 10;\n(LIMIT) = 20;\n", False, id="parenthesized-assignment"
        ),
        pytest.param(
            "export let LIMIT = 10;\n(( LIMIT )) += 20;\n",
            False,
            id="nested-parenthesized-compound-assignment",
        ),
        pytest.param("export let LIMIT = 10;\n(LIMIT)++;\n", False, id="parenthesized-postfix"),
        pytest.param("export let LIMIT = 10;\n++(LIMIT);\n", False, id="parenthesized-prefix"),
        pytest.param("export let LIMIT = 10;\nLIMIT! = 20;\n", False, id="non-null-assignment"),
        pytest.param(
            "export let LIMIT = 10;\n(LIMIT !)! += 20;\n",
            False,
            id="parenthesized-non-null-compound-assignment",
        ),
        pytest.param("export let LIMIT = 10;\nLIMIT!++;\n", False, id="non-null-postfix"),
        pytest.param(
            "export let LIMIT = 10;\n(LIMIT as number) = 20;\n", False, id="as-asserted-assignment"
        ),
        pytest.param(
            "export let LIMIT = 10;\n((LIMIT satisfies number)) += 20;\n",
            False,
            id="satisfies-asserted-compound-assignment",
        ),
        pytest.param(
            "export let LIMIT = 10;\n(LIMIT as Array<number>[0])++;\n",
            False,
            id="as-asserted-postfix",
        ),
        pytest.param(
            "export let LIMIT = 10;\n(LIMIT as (number)) = 20;\n",
            False,
            id="as-asserted-parenthesized-type-assignment",
        ),
        pytest.param(
            "export let LIMIT = 10;\n((LIMIT satisfies ((() => number) | (string)))) += 20;\n",
            False,
            id="satisfies-asserted-nested-function-type-compound-assignment",
        ),
        pytest.param(
            "export const LIMIT = {value: 10};\n++(LIMIT as (Box)).value;\n"
            "--(LIMIT satisfies (() => Box))[0];\n",
            True,
            id="prefix-member-updates-through-parenthesized-type-only",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (LIMIT as boolean) x = 1;\nuse(LIMIT as number);\n",
            True,
            id="as-asserted-reads-only",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (LIMIT!= 5 || LIMIT !== 6 || LIMIT!!= 7) work();\n",
            True,
            id="non-null-inequality-reads-only",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (on) other = LIMIT;\n"
            "if (LIMIT >= 5) obj.LIMIT = cache[LIMIT] = 1;\n",
            True,
            id="reads-and-member-assignments-only",
        ),
        pytest.param(
            "export let LIMIT = 10;\nconst obj = {};\nobj. LIMIT = 20;\nobj?.\tLIMIT++;\n"
            "obj./* c */LIMIT += 1;\nobj\n  .LIMIT = 30;\n",
            True,
            id="spaced-member-assignments-only",
        ),
        pytest.param(
            "export const LIMIT = {value: 10};\n++LIMIT.value;\n--LIMIT[0];\n"
            "++(LIMIT).value;\n--LIMIT!.value;\n++(LIMIT as Box)\n  [0];\n",
            True,
            id="prefix-member-updates-only",
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_inline_reassignment_withholds_the_binding(
    text: str, *, expected: bool
) -> None:
    """An assignment that does not open its statement rebinds the name (PRRT_kwDOSJAM6s63XLQk)."""
    old = text.splitlines()[0]
    diff = _diff(_WEB, "@@ -1 +1 @@", f"-{old}", f"+{old.replace('10', '11')}")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("export let LIMIT = 10;\nfor (LIMIT of [20]) {}\n", False, id="for-of"),
        pytest.param("export let LIMIT = 10;\nfor (LIMIT in { a: 1 }) {}\n", False, id="for-in"),
        pytest.param(
            "export let LIMIT = 10;\nasync function f() {}\nfor await (LIMIT of gen()) {}\n",
            False,
            id="for-await-of",
        ),
        pytest.param("export var LIMIT = 10;\nfor (var LIMIT of [20]) {}\n", False, id="for-var"),
        pytest.param(
            "export let LIMIT = 10;\nfor ([LIMIT] of [[20]]) {}\n", False, id="for-of-array-pattern"
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor ({ a: [LIMIT] } of [{ a: [20] }]) {}\n",
            False,
            id="for-of-nested-pattern",
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor ((LIMIT) of [20]) {}\n", False, id="for-of-parenthesized"
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor ( ( LIMIT )of [20]) {}\n",
            False,
            id="for-of-parenthesized-abutting",
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor ([LIMIT]of [[20]]) {}\n",
            False,
            id="for-of-pattern-abutting",
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor (LIMIT! of [20]) {}\n", False, id="for-of-non-null"
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor ((LIMIT!)of [20]) {}\n",
            False,
            id="for-of-parenthesized-non-null",
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor ((LIMIT as number) of [20]) {}\n",
            False,
            id="for-of-as-asserted",
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor ((LIMIT satisfies number)in { a: 1 }) {}\n",
            False,
            id="for-in-satisfies-asserted",
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor ((LIMIT as (number)) of [20]) {}\n",
            False,
            id="for-of-as-asserted-parenthesized-type",
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (on) for (LIMIT of [20]) work();\n",
            False,
            id="inline-for-of-body",
        ),
        pytest.param(
            "export let LIMIT = 10;\nfor (const LIMIT of [20]) {}\n"
            "for (const x of [LIMIT]) {}\nfor (const key in LIMIT) {}\n"
            "for ([x] = [LIMIT]; x < 1; x++) {}\n",
            True,
            id="block-scoped-and-read-only",
        ),
        pytest.param("export let LIMIT = 10;\nfor ([LIMIT\n", True, id="unclosed-pattern"),
    ],
)
@pytest.mark.unit
async def test_js_ts_loop_target_withholds_the_binding(text: str, *, expected: bool) -> None:
    """A ``for...of``/``for...in`` target rebinds the name (PRRT_kwDOSJAM6s63XrHa).

    So does a TS non-null or ``as``/``satisfies`` asserted one (PRRT_kwDOSJAM6s63YTT3).
    """
    old = text.splitlines()[0]
    diff = _diff(_WEB, "@@ -1 +1 @@", f"-{old}", f"+{old.replace('10', '11')}")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "export function handler() {\n  return 10;\n}\nif (true) handler = () => 20;\n",
            False,
            id="inline-assignment",
        ),
        pytest.param(
            "export function handler() {\n  return 10;\n}\n({ handler } = other);\n",
            False,
            id="destructuring-assignment",
        ),
        pytest.param(
            "export function handler() {\n  return 10;\n}\nfor (handler of [() => 20]) {}\n",
            False,
            id="for-of-target",
        ),
        pytest.param(
            "export function handler() {\n  return 10;\n}\nif (on) other = handler;\n",
            True,
            id="reads-only",
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_reassignment_withholds_the_definition_head(
    text: str, *, expected: bool
) -> None:
    """A JS/TS rebinding withholds a ``function`` head too (PRRT_kwDOSJAM6s63YX72)."""
    diff = _diff(_WEB, "@@ -2 +2 @@", "-  return 10;", "+  return 11;")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`handler` is too low") is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("export let LIMIT = 10;\nLIMIT\n = 20;\n", False, id="target-then-operator"),
        pytest.param(
            "export let LIMIT = 10;\nLIMIT // comment\n\n  += 20;\n", False, id="compound-past-gap"
        ),
        pytest.param(
            "export let LIMIT = 10;\nif (on)\n  LIMIT\n  = 20;\n", False, id="brace-less-body"
        ),
        pytest.param("export let LIMIT = 10;\nother\n = LIMIT;\n", True, id="reads-only"),
        pytest.param(
            "export let LIMIT = 10;\nwork();\n[x] = [LIMIT];\n", True, id="ended-statement"
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_continued_reassignment_withholds_the_binding(
    text: str, *, expected: bool
) -> None:
    """A reassignment whose operator leads the next line rebinds (PRRT_kwDOSJAM6s63Ycby)."""
    diff = _diff(_WEB, "@@ -1 +1 @@", "-export let LIMIT = 10;", "+export let LIMIT = 11;")
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`LIMIT` is too low") is expected


@pytest.mark.parametrize(
    ("text", "changed", "expected"),
    [
        pytest.param(
            "export const handler = function ()\n{\n  return 10;\n};\n",
            ("@@ -3 +3 @@", "-  return 10;", "+  return 11;"),
            True,
            id="function-body",
        ),
        pytest.param(
            "export const handler = async function* gen(): Gen\n{\n  yield 10;\n}\nwork();\n",
            ("@@ -3 +3 @@", "-  yield 10;", "+  yield 11;"),
            True,
            id="annotated-generator-body",
        ),
        pytest.param(
            "export const handler = class extends Base\n{\n  run() { return 10; }\n};\n",
            ("@@ -3 +3 @@", "-  run() { return 10; }", "+  run() { return 11; }"),
            True,
            id="class-body",
        ),
        pytest.param(
            "export const handler = function () {\n  return 10;\n};\n{\n  work();\n}\n",
            ("@@ -5 +5 @@", "-  work();", "+  rework();"),
            False,
            id="later-block",
        ),
        pytest.param(
            "export const handler = function (): Promise<number> {\n  return 10;\n};\n"
            "{\n  work();\n}\n",
            ("@@ -5 +5 @@", "-  work();", "+  rework();"),
            False,
            id="generic-return-type-later-block",
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_split_function_initializer_body_stays_in_the_binding(
    text: str, changed: tuple[str, str, str], *, expected: bool
) -> None:
    """A function or class initializer's next-line body is its binding's (PRRT_kwDOSJAM6s63YhJR)."""
    diff = _diff(_WEB, *changed)
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`handler` is too low") is expected


@pytest.mark.parametrize(
    ("text", "changed"),
    [
        pytest.param(
            "export const handler = function ({ value })\n{\n  return 10;\n};\n",
            ("@@ -3 +3 @@", "-  return 10;", "+  return 11;"),
            id="destructured-parameter",
        ),
        pytest.param(
            "export const handler = function (options = { value: 1 })\n{\n  return 10;\n};\n",
            ("@@ -3 +3 @@", "-  return 10;", "+  return 11;"),
            id="object-default-parameter",
        ),
        pytest.param(
            "export const handler = class extends mixin({ base: Base })\n"
            "{\n  run() { return 10; }\n};\n",
            ("@@ -3 +3 @@", "-  run() { return 10; }", "+  run() { return 11; }"),
            id="object-argument-heritage",
        ),
        pytest.param(
            "export const handler = function (): { value: number }\n"
            "{\n  return { value: 10 };\n};\n",
            ("@@ -3 +3 @@", "-  return { value: 10 };", "+  return { value: 11 };"),
            id="object-return-type",
        ),
        pytest.param(
            "export const handler = function (): Promise<{ value: number }> | { value: 1 }\n"
            "{\n  return { value: 1 };\n};\n",
            ("@@ -3 +3 @@", "-  return { value: 1 };", "+  return { value: 1 as const };"),
            id="union-generic-return-type",
        ),
        pytest.param(
            "export const handler = function (): () => { value: number }\n"
            "{\n  return () => ({ value: 10 });\n};\n",
            ("@@ -3 +3 @@", "-  return () => ({ value: 10 });", "+  return () => ({ value: 11 });"),
            id="function-type-return-type",
        ),
        pytest.param(
            "export const handler = function (): true extends true ? { value: number } : never\n"
            "{\n  return { value: 10 };\n};\n",
            ("@@ -3 +3 @@", "-  return { value: 10 };", "+  return { value: 11 };"),
            id="conditional-return-type",
        ),
        pytest.param(
            "export const handler = function (): { value: number; label: string }\n"
            "{\n  return { value: 10, label: 'a' };\n};\n",
            (
                "@@ -3 +3 @@",
                "-  return { value: 10, label: 'a' };",
                "+  return { value: 11, label: 'a' };",
            ),
            id="semicolon-delimited-type-literal-return-type",
        ),
        pytest.param(
            "export const handler = function (): keyof { value: number }\n"
            "{\n  return 'value';\n};\n",
            ("@@ -3 +3 @@", "-  return 'value';", "+  return 'value' as const;"),
            id="keyof-type-literal-return-type",
        ),
        pytest.param(
            "export const handler = function <T>(): T extends { value: number } ? T : never\n"
            "{\n  return 10 as never;\n};\n",
            ("@@ -3 +3 @@", "-  return 10 as never;", "+  return 11 as never;"),
            id="extends-type-literal-return-type",
        ),
        pytest.param(
            "export const handler = function (input: unknown): input is { value: number }\n"
            "{\n  return input !== null;\n};\n",
            ("@@ -3 +3 @@", "-  return input !== null;", "+  return input != null;"),
            id="type-predicate-type-literal-return-type",
        ),
    ],
)
@pytest.mark.unit
async def test_js_ts_split_function_initializer_body_survives_nested_braces(
    text: str, changed: tuple[str, str, str]
) -> None:
    """Braces nested in a head's parentheses do not open its body (PRRT_kwDOSJAM6s63Yl_Z)."""
    diff = _diff(_WEB, *changed)
    runner = _probe(texts={_WEB: text}, changed_paths=(_WEB,), diffs={_WEB: diff})

    assert await _changes_named_target(runner, "`handler` is too low") is True
