"""JS/TS cross-file callee evidence through ES-module bindings (issue #1047).

A JS/TS call site binds its callee through the call-site file's own ``import``
statements, resolved to one file the way TS/Node resolve relative specifiers.
A definition is evidence only in the file that binding resolves to, and a
callee this reader cannot resolve statically fails closed instead of falling
back to the name-only rule. Pure reader units live in
``test_pre_push_validation_fix_pass_ancestry_cross_file_es_modules_reader.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file as cross_file,
)
from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file_es_evidence as es_evidence,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import _LEFT, _RIGHT

_CALLER = "src/app/ingest.ts"
_TELEMETRY = "src/app/telemetry.ts"
_UNRELATED = "src/lib/unrelated/audit.ts"

_RECORD_DEFINITION = "export function record(value: number) {\n  return value + 1;\n}\n"
_UNRELATED_RECORD = "export function record(value: number) {\n  return value * 2;\n}\n"


def _caller(import_line: str, call: str = "record(value)") -> str:
    """A call site whose anchored line 4 makes ``call``."""
    return f"{import_line}\n\nexport function ingest(value: number) {{\n  return {call};\n}}\n"


def _body_edit(path: str, line: int = 2) -> str:
    """A ``-U0`` hunk replacing old line ``line`` of ``path``."""
    return (
        f"--- a/{path}\n+++ b/{path}\n@@ -{line} +{line} @@\n"
        "-  return value + 1;\n+  return value + 2;\n"
    )


class _EsProbe:
    """Runner stub for the JS/TS path: ``show``, ``ls-tree``, name-status and diffs.

    ``files`` are the left-side texts; ``right`` overrides the right side,
    which otherwise falls back to the left (``None`` deletes a path there).
    Diffs are keyed by the first pathspec entry.
    """

    def __init__(
        self,
        *,
        files: dict[str, str],
        diffs: dict[str, str | None],
        right: dict[str, str | None] | None = None,
        renames: dict[str, str] | None = None,
        ls_tree_ok: bool = True,
    ) -> None:
        self.files = files
        self.diffs = diffs
        self.right = right or {}
        self.renames = renames or {}
        self.ls_tree_ok = ls_tree_ok
        self.ls_tree_calls = 0
        self._deps = SimpleNamespace(runner=SimpleNamespace(run=self._run))

    def _text(self, ref: str, path: str) -> str | None:
        if ref == _RIGHT and path in self.right:
            return self.right[path]
        return self.files.get(path)

    async def _run(self, cmd: list[str], **kwargs: object):
        from awf.common.commands import CommandResult

        del kwargs
        if "ls-tree" in cmd:
            self.ls_tree_calls += 1
            if not self.ls_tree_ok:
                return CommandResult(returncode=128, stdout="", stderr="bad object")
            assert "-z" in cmd
            split = cmd.index("--")
            ref = cmd[split - 1]
            found = [p for p in cmd[split + 1 :] if self._text(ref, p) is not None]
            return CommandResult(returncode=0, stdout="".join(f"{p}\0" for p in found), stderr="")
        if "show" in cmd:
            ref, _, path = cmd[-1].partition(":")
            text = self._text(ref, path)
            if text is None:
                return CommandResult(returncode=128, stdout="", stderr="no such path")
            return CommandResult(returncode=0, stdout=text, stderr="")
        if "--name-status" in cmd:
            records = "".join(
                f"R095\0{path}\0{self.renames[path]}\0" if path in self.renames else f"M\0{path}\0"
                for path in self.diffs
            )
            return CommandResult(returncode=0, stdout=records, stderr="")
        if "diff" in cmd:
            diff = self.diffs.get(cmd[cmd.index("--") + 1])
            if diff is None:
                return CommandResult(returncode=1, stdout="", stderr="diff failed")
            return CommandResult(returncode=0, stdout=diff, stderr="")
        return CommandResult(returncode=1, stdout="", stderr="unsupported")


async def _accepted(probe: _EsProbe, *, item_path: str = _CALLER, item_line: int = 4) -> bool:
    return await cross_file._commit_range_changes_callee_definition(
        probe,
        worktree_path=Path("/tmp/ws"),
        left=_LEFT,
        right=_RIGHT,
        item_path=item_path,
        item_line=item_line,
    )


def _telemetry_probe(
    caller: str,
    *,
    files: dict[str, str] | None = None,
    right: dict[str, str | None] | None = None,
    ls_tree_ok: bool = True,
) -> _EsProbe:
    """The call site plus an edited ``record`` in ``src/app/telemetry.ts``."""
    return _EsProbe(
        files={_CALLER: caller, _TELEMETRY: _RECORD_DEFINITION, **(files or {})},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY)},
        right=right,
        ls_tree_ok=ls_tree_ok,
    )


# --- the #1047 reproductions are rejected ------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("import_line", "call"),
    [
        ("import { record } from './telemetry';", "record(value)"),
        ("import * as tel from './telemetry';", "tel.record(value)"),
    ],
)
async def test_an_unrelated_same_named_definition_is_not_evidence(
    import_line: str, call: str
) -> None:
    """The call binds to ``./telemetry``; editing ``audit.ts``'s ``record`` proves nothing."""
    probe = _EsProbe(
        files={
            _CALLER: _caller(import_line, call),
            _TELEMETRY: _RECORD_DEFINITION,
            _UNRELATED: _UNRELATED_RECORD,
        },
        diffs={_UNRELATED: _body_edit(_UNRELATED)},
    )

    assert not await _accepted(probe)


# --- correct cross-file fixes are accepted -------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("import_line", "call"),
    [
        ("import { record } from './telemetry';", "record(value)"),
        ("import { record as track } from './telemetry';", "track(value)"),
        ("import * as tel from './telemetry';", "tel.record(value)"),
        ("import * as tel from './telemetry';", "tel?.record(value)"),
        ("import { record } from './telemetry';", "await record(value)"),
    ],
)
async def test_a_fix_in_the_bound_module_is_evidence(import_line: str, call: str) -> None:
    assert await _accepted(_telemetry_probe(_caller(import_line, call)))


@pytest.mark.unit
@pytest.mark.parametrize(
    "tail",
    [
        "export function flush() {\n  return other.tel.record(1);\n}\n",
        "export function flush() {\n  return other.\n    tel.record(1);\n}\n",
    ],
)
async def test_an_unrelated_chain_elsewhere_does_not_block_the_anchored_call(tail: str) -> None:
    """Only the anchored occurrence decides whether ``tel`` is a property (PRRT_kwDOSJAM6s6vOM0D)."""
    caller = _caller("import * as tel from './telemetry';", "tel.record(value)") + tail

    assert await _accepted(_telemetry_probe(caller))


@pytest.mark.unit
@pytest.mark.parametrize(
    "call",
    [
        "other.tel.record(1) + tel.record(value)",
        "tel.record(value) + other?.tel?.record(1)",
        "other.\n    tel.record(1) + tel.record(value)",
    ],
)
async def test_a_chained_occurrence_does_not_block_a_direct_one_on_the_line(call: str) -> None:
    """Chained status is per call occurrence, not per ``(tel, record)`` pair (PRRT_kwDOSJAM6s6vOa0O)."""
    caller = _caller("import * as tel from './telemetry';", call)
    item_line = 4 + call.count("\n")

    assert await _accepted(_telemetry_probe(caller), item_line=item_line)


@pytest.mark.unit
@pytest.mark.parametrize(
    "call", ["other.tel.record(1) + tel.flush(value)", "other.tel.record(1) + tel.record"]
)
async def test_only_a_direct_call_of_the_same_pair_lifts_a_chained_one(call: str) -> None:
    """A direct ``tel.flush()`` or non-call ``tel.record`` says nothing about ``other.tel.record()``."""
    probe = _telemetry_probe(_caller("import * as tel from './telemetry';", call))

    assert not await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [
        # A directly exported arrow is its own definition head (PRRT_kwDOSJAM6s6vLkoD).
        "export const record = (value: number) => {\n  return value + 1;\n};\n",
        "export const record = async (value: number) => {\n  return value + 1;\n};\n",
        "export let record = function (value: number) {\n  return value + 1;\n};\n",
        # A generic type-parameter list may precede the arrow parameters
        # (PRRT_kwDOSJAM6s6vL9AV).
        "export const record = <T>(value: T) => {\n  return value;\n};\n",
        "export const record = async <T,>(value: T) => {\n  return value;\n};\n",
        "export const record = <K extends keyof Row, V = Row[K]>(value: V): V => {\n"
        "  return value;\n};\n",
        # A variable type annotation may precede the callable initializer
        # (PRRT_kwDOSJAM6s6vMwSF).
        "export const record: Recorder = (value) => {\n  return value + 1;\n};\n",
        "export const record: Recorder<number> = async (value) => {\n  return value + 1;\n};\n",
        "export let record: Recorder = function (value) {\n  return value + 1;\n};\n",
        # A function expression's own name binds only inside it, so it is no
        # second module-level head (PRRT_kwDOSJAM6s6vO2Gy).
        "export const record = function record(value: number) {\n  return value + 1;\n};\n",
        "export const record = async function* record(value: number) {\n  return value + 1;\n};\n",
    ],
)
async def test_a_fix_to_a_directly_exported_arrow_is_evidence(module: str) -> None:
    caller = _caller("import { record } from './telemetry';")
    probe = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: module},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY)},
    )

    assert await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("import_line", "module"),
    [
        # A directly exported class is its own definition head (PRRT_kwDOSJAM6s6vMeFV).
        ("import { Recorder } from './telemetry';", "export class Recorder {\n"),
        ("import Recorder from './telemetry';", "export default class Recorder {\n"),
        ("import { Recorder } from './telemetry';", "export abstract class Recorder {\n"),
        ("import Recorder from './telemetry';", "export default abstract class Recorder {\n"),
    ],
)
async def test_a_fix_to_a_directly_exported_class_is_evidence(
    import_line: str, module: str
) -> None:
    body = f"{module}  constructor(value: number) {{\n    this.value = value;\n  }}\n}}\n"
    probe = _EsProbe(
        files={_CALLER: _caller(import_line, "new Recorder(value)"), _TELEMETRY: body},
        diffs={
            _TELEMETRY: f"--- a/{_TELEMETRY}\n+++ b/{_TELEMETRY}\n@@ -3 +3 @@\n"
            "-    this.value = value;\n+    this.value = value + 1;\n"
        },
    )

    assert await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [
        "export const record = <T,>(value: T) => {\n  return value;\n};\n",
        "export const record = <T extends number>(value: T) => {\n  return value;\n};\n",
    ],
)
async def test_a_fix_to_a_tsx_generic_arrow_is_evidence(module: str) -> None:
    """TSX generic-arrow heads are not misread as JSX (PRRT_kwDOSJAM6s6vMLga)."""
    caller_path, module_path = "src/app/ingest.tsx", "src/app/telemetry.tsx"
    probe = _EsProbe(
        files={caller_path: _caller("import { record } from './telemetry';"), module_path: module},
        diffs={module_path: _body_edit(module_path)},
    )

    assert await _accepted(probe, item_path=caller_path)


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [
        # An exported generator declaration is a head too (PRRT_kwDOSJAM6s6vL9Ae).
        "export function* record(value: number) {\n  return value + 1;\n}\n",
        "export async function* record(value: number) {\n  return value + 1;\n}\n",
        "export function *record(value: number) {\n  return value + 1;\n}\n",
        "export function * record(value: number) {\n  return value + 1;\n}\n",
        # A generic type-parameter list may follow the declared name
        # (PRRT_kwDOSJAM6s6vMwSN).
        "export function record<T>(value: T) {\n  return value;\n}\n",
        "export async function record<T extends number>(value: T) {\n  return value;\n}\n",
        "export function record<K, V = Row<K>>(value: V) {\n  return value;\n}\n",
    ],
)
async def test_a_fix_to_an_exported_generator_is_evidence(module: str) -> None:
    caller = _caller("import { record } from './telemetry';")
    probe = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: module},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY)},
    )

    assert await _accepted(probe)


@pytest.mark.unit
async def test_a_fix_to_a_generic_default_export_is_evidence() -> None:
    """A named generic default export is a head too (PRRT_kwDOSJAM6s6vMwSN)."""
    module = "export default async function record<T>(value: T) {\n  return value;\n}\n"
    probe = _EsProbe(
        files={_CALLER: _caller("import record from './telemetry';"), _TELEMETRY: module},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY)},
    )

    assert await _accepted(probe)


@pytest.mark.unit
async def test_a_fix_to_an_overloaded_implementation_is_evidence() -> None:
    """Body-less overload signatures are not heads of their own (PRRT_kwDOSJAM6s6vL0nj)."""
    module = (
        "export function record(value: string): number;\n"
        "export function record<T extends number>(value: T): Promise<T>;\n"
        "export function record(value: unknown) {\n  return value + 1;\n}\n"
    )
    probe = _EsProbe(
        files={_CALLER: _caller("import { record } from './telemetry';"), _TELEMETRY: module},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY, line=4)},
    )

    assert await _accepted(probe)


@pytest.mark.unit
async def test_each_callee_on_the_line_is_held_to_its_own_definition() -> None:
    """Two callees from one module: the edited one carries the evidence."""
    caller = _caller("import { flush, record } from './telemetry';", "flush(value) + record(value)")
    module = "export function flush(value: number) {\n  return value;\n}\n" + _RECORD_DEFINITION
    probe = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: module},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY, line=5)},
    )

    assert await _accepted(probe)


@pytest.mark.unit
async def test_an_aliased_import_is_looked_up_under_its_imported_name() -> None:
    """``record as track``: only the module's ``record`` is the callee, not a local ``track``."""
    caller = _caller("import { record as track } from './telemetry';", "track(value)")
    module = _RECORD_DEFINITION + "\nexport function track(value: number) {\n  return value;\n}\n"
    probe = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: module},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY, line=6)},
    )

    assert not await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    "module",
    [
        "function record(value: number) {\n  return value + 1;\n}\nexport default record;\n",
        "function record(value: number) {\n  return value + 1;\n}\nexport { record as default };\n",
        # A named default declaration is its own definition head (PRRT_kwDOSJAM6s6vLRfG).
        "export default function record(value: number) {\n  return value + 1;\n}\n",
        "export default async function record(value: number) {\n  return value + 1;\n}\n",
    ],
)
async def test_a_default_import_reaches_the_default_export(module: str) -> None:
    caller = _caller("import track from './telemetry';", "track(value)")
    probe = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: module},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY)},
    )

    assert await _accepted(probe)


_LIB_INDEX = "src/app/lib/index.ts"
_LIB_IMPL = "src/app/lib/impl.ts"
_LIB_OTHER = "src/app/lib/other.ts"


def _barrel_probe(index: str, impl: str = _RECORD_DEFINITION, **files: str) -> _EsProbe:
    caller = _caller("import { record } from './lib';")
    return _EsProbe(
        files={_CALLER: caller, _LIB_INDEX: index, _LIB_IMPL: impl, **files},
        diffs={_LIB_IMPL: _body_edit(_LIB_IMPL)},
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("index", "impl"),
    [
        ("export { record } from './impl';\n", _RECORD_DEFINITION),
        (
            "export { impl as record } from './impl';\n",
            _RECORD_DEFINITION.replace("function record", "function impl"),
        ),
        ("import { record } from './impl';\nexport { record };\n", _RECORD_DEFINITION),
        ("export * from './impl';\nexport * from './other';\n", _RECORD_DEFINITION),
    ],
)
async def test_one_re_export_hop_is_followed(index: str, impl: str) -> None:
    probe = _barrel_probe(index, impl, **{_LIB_OTHER: "export const unrelated = 1;\n"})

    assert await _accepted(probe)


# --- relative specifiers are probed like TS/Node -------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("caller_path", "specifier", "module_path"),
    [
        (_CALLER, "./telemetry", "src/app/telemetry.ts"),
        (_CALLER, "./telemetry.js", "src/app/telemetry.ts"),
        (_CALLER, "./telemetry", "src/app/telemetry/index.ts"),
        (_CALLER, "./telemetry.mjs", "src/app/telemetry.mts"),
        ("src/app/jobs/ingest.tsx", "../lib/telemetry", "src/app/lib/telemetry.tsx"),
        ("src/app/ingest.js", "./telemetry", "src/app/telemetry.js"),
    ],
)
async def test_relative_specifiers_resolve_to_one_file(
    caller_path: str, specifier: str, module_path: str
) -> None:
    caller = _caller(f"import {{ record }} from '{specifier}';")
    probe = _EsProbe(
        files={caller_path: caller, module_path: _RECORD_DEFINITION},
        diffs={module_path: _body_edit(module_path)},
    )

    assert await _accepted(probe, item_path=caller_path)


# --- unresolvable callees fail closed ------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("import_line", "call"),
    [
        ("import { record } from 'telemetry';", "record(value)"),
        ("import { record } from '@/app/telemetry';", "record(value)"),
        ("import { record } from '/src/app/telemetry';", "record(value)"),
        ("const { record } = await import('./telemetry');", "record(value)"),
        ("const { record } = require('./telemetry');", "record(value)"),
        ("import record = require('./telemetry');", "record(value)"),
        ("import { other } from './telemetry';", "record(value)"),
        ("import { record as Collector } from './telemetry';", "Collector.record(value)"),
        ("import * as tel from './telemetry';", "tel.sub.record(value)"),
        ("import * as tel from './telemetry';", "tel(value)"),
        ("import { record } from './telemetry';", "this.record(value)"),
        ("import type { record } from './telemetry';", "record(value)"),
        ("import { type record } from './telemetry';", "record(value)"),
        ("import type * as tel from './telemetry';", "tel.record(value)"),
        ("import { record } from './telemetry'; import record from './telemetry';", "record()"),
    ],
)
async def test_unresolvable_callees_fail_closed(import_line: str, call: str) -> None:
    """Each form edits the module's real ``record`` yet must not be accepted."""
    assert not await _accepted(_telemetry_probe(_caller(import_line, call)))


@pytest.mark.unit
@pytest.mark.parametrize(
    "tail",
    [
        "[record] = [other];\n",
        "(record) = other;\n",
        "record! = other;\n",
        "(record as Recorder) = other;\n",
        "((record satisfies Recorder)!) ??= other;\n",
        "({ record } = overrides);\n",
        "for (record of [other]) {\n}\n",
        "export function setup() {\n  record = other;\n}\n",
    ],
)
async def test_a_module_that_reassigns_the_export_fails_closed(tail: str) -> None:
    """The edited ``function record`` is not what callers reach once the module reassigns it."""
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"))
    probe.files[_TELEMETRY] = _RECORD_DEFINITION + tail

    assert not await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("caller", "anchor"),
    [
        # A receiver chain whose ``.`` ends the previous line.
        (
            "import * as tel from './telemetry';\n"
            "export function ingest(value: number) {\n"
            "  return other.\n"
            "    tel.record(value);\n"
            "}\n",
            4,
        ),
        # A nested TS import alias shadows the module's import.
        (
            "import { record } from './telemetry';\n"
            "namespace Legacy {\n"
            "  import record = Other.record;\n"
            "  export function ingest(value: number) {\n"
            "    return record(value);\n"
            "  }\n"
            "}\n",
            5,
        ),
        # A regex statement after a block must not hide the parameter below.
        (
            "import { record } from './telemetry';\n"
            "if (ok) {}\n"
            "/`/.test(s);\n"
            "export function ingest(record) {\n"
            "  return record(value);\n"
            "}\n"
            "const t = `;\n",
            5,
        ),
        (
            "import { record } from './telemetry';\n"
            "if (ok) /`/.test(s);\n"
            "export function ingest(record) {\n"
            "  return record(value);\n"
            "}\n"
            "const t = `;\n",
            4,
        ),
    ],
)
async def test_call_sites_reaching_something_else_fail_closed(caller: str, anchor: int) -> None:
    assert not await _accepted(_telemetry_probe(caller), item_line=anchor)


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "function ingest(record) {\n  return record(1);\n}\n",
        "const { record } = handlers;\nexport function ingest() {\n  return record(1);\n}\n",
        "try {\n  go();\n} catch (record) {\n  record(1);\n}\n",
    ],
)
async def test_a_rebound_import_name_fails_closed(body: str) -> None:
    caller = "import { record } from './telemetry';\n" + body
    anchor = next(i for i, line in enumerate(caller.splitlines(), 1) if "record(1)" in line)

    assert not await _accepted(_telemetry_probe(caller), item_line=anchor)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("index", "files"),
    [
        # Two hops: index → mid → impl.
        (
            "export { record } from './mid';\n",
            {"src/app/lib/mid.ts": "export { record } from './impl';\n"},
        ),
        # Two star sources both export ``record``.
        (
            "export * from './impl';\nexport * from './other';\n",
            {_LIB_OTHER: _UNRELATED_RECORD},
        ),
        # A star source this reader cannot resolve might export it too.
        ("export * from './impl';\nexport * from 'some-package';\n", {}),
        # A star source that is itself a barrel might export it too.
        (
            "export * from './impl';\nexport * from './other';\n",
            {_LIB_OTHER: "export * from './deeper';\n"},
        ),
        # A star source exporting names this reader cannot list.
        (
            "export * from './impl';\nexport * from './other';\n",
            {_LIB_OTHER: "export const { record } = handlers;\n"},
        ),
        # An unparseable star source.
        ("export * from './impl';\nexport * from './other';\n", {_LIB_OTHER: "const x = `\n"}),
        # The barrel itself is unparseable.
        ("export { record } from './impl';\nconst broken = '\n", {}),
        # A namespace re-export is not a callable.
        ("export * as record from './impl';\n", {}),
        # The barrel lists names it cannot be read for.
        ("export * from './impl';\nexport const { record } = handlers;\n", {}),
        # The re-exported module does not define it.
        ("export { record } from './other';\n", {_LIB_OTHER: "export const x = 1;\n"}),
        # The barrel exports nothing named ``record``.
        ("export const unrelated = 1;\n", {}),
        # A re-export to a module that does not resolve.
        ("export { record } from './missing';\n", {}),
        # A namespace import re-exported by name.
        ("import * as record from './impl';\nexport { record };\n", {}),
    ],
)
async def test_unfollowable_re_exports_fail_closed(index: str, files: dict[str, str]) -> None:
    assert not await _accepted(_barrel_probe(index, **files))


@pytest.mark.unit
async def test_a_star_re_export_never_carries_a_default() -> None:
    impl = "function record(value: number) {\n  return value + 1;\n}\nexport default record;\n"
    probe = _barrel_probe("export * from './impl';\n", impl)
    probe.files[_CALLER] = _caller("import record from './lib';")

    assert not await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    "files",
    [
        {"src/app/telemetry.js": _RECORD_DEFINITION},
        {"src/app/telemetry/index.ts": _RECORD_DEFINITION},
    ],
)
async def test_ambiguous_specifiers_fail_closed(files: dict[str, str]) -> None:
    caller = _caller("import { record } from './telemetry';")

    assert not await _accepted(_telemetry_probe(caller, files=files))


@pytest.mark.unit
async def test_a_declaration_file_alone_carries_no_definition() -> None:
    caller = _caller("import { record } from './telemetry';")
    declaration = "src/app/telemetry.d.ts"
    probe = _EsProbe(
        files={_CALLER: caller, declaration: _RECORD_DEFINITION},
        diffs={declaration: _body_edit(declaration)},
    )

    assert not await _accepted(probe)


@pytest.mark.unit
async def test_an_unparseable_call_site_fails_closed() -> None:
    caller = _caller("import { record } from './telemetry';") + "const tail = `never closed\n"

    assert not await _accepted(_telemetry_probe(caller))


@pytest.mark.unit
async def test_a_failed_specifier_probe_fails_closed() -> None:
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"), ls_tree_ok=False)

    assert not await _accepted(probe)
    assert probe.ls_tree_calls == 1


@pytest.mark.unit
async def test_an_edit_outside_the_definition_or_an_unreadable_diff_is_not_evidence() -> None:
    caller = _caller("import { record } from './telemetry';")
    module = _RECORD_DEFINITION + "\nexport const other = 1;\n"
    outside = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: module},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY, line=5)},
    )
    unreadable = _telemetry_probe(caller)
    unreadable.diffs[_TELEMETRY] = None
    unexported = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: "export { record };\nconst record = make();\n"},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY)},
    )

    assert not await _accepted(outside)
    assert not await _accepted(unreadable)
    assert not await _accepted(unexported)


@pytest.mark.unit
async def test_the_call_site_itself_and_lines_without_callees_are_not_evidence() -> None:
    caller = _caller("import { record } from './telemetry';")
    probe = _telemetry_probe(caller)
    same_file = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: _RECORD_DEFINITION},
        diffs={_CALLER: _body_edit(_CALLER)},
    )

    assert not await _accepted(probe, item_line=2)
    assert not await _accepted(same_file)


# --- the right side has to agree -------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    "right",
    [
        # The correction re-points the caller's import at another module.
        {
            _CALLER: _caller("import { record } from './other';"),
            "src/app/other.ts": _RECORD_DEFINITION,
        },
        # The correction turns the named import into a namespace import.
        {_CALLER: _caller("import * as record from './telemetry';")},
        # The correction deletes the definition, or its whole file.
        {_TELEMETRY: "export function other() {\n  return 1;\n}\n"},
        {_TELEMETRY: None},
        # The correction replaces the definition with a value it cannot read as one.
        {_TELEMETRY: "export const record = make();\n"},
        # The caller is unreadable or unparseable afterwards.
        {_CALLER: None},
        {_CALLER: "const broken = '\n"},
        # The correction shadows the import at the call site.
        {_CALLER: _caller("import { record } from './telemetry';").replace("(value", "(record")},
        # The correction removes or renames the call, leaving the import unused.
        {_CALLER: _caller("import { record } from './telemetry';", "value")},
        {_CALLER: _caller("import { record, other } from './telemetry';", "other(value)")},
        {_CALLER: _caller("import { record } from './telemetry';", "box.record(value)")},
        {_CALLER: _caller("import { record } from './telemetry';", "[record]")},
    ],
)
async def test_the_corrected_caller_must_still_reach_the_definition(
    right: dict[str, str | None],
) -> None:
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"), right=right)

    assert not await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    "call_after",
    ["tel.other(value)", "value", "other.tel.record(value)", "tel.record"],
)
async def test_a_namespace_caller_must_still_call_the_member(call_after: str) -> None:
    import_line = "import * as tel from './telemetry';"
    probe = _telemetry_probe(
        _caller(import_line, "tel.record(value)"),
        right={_CALLER: _caller(import_line, call_after)},
    )

    assert not await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("import_line", "call_after"),
    [
        ("import { record } from './telemetry';", "record?.(value + 1)"),
        ("import * as tel from './telemetry';", "tel?.record(value + 1)"),
        ("import * as tel from './telemetry';", "tel.record?.(value + 1)"),
    ],
)
async def test_a_corrected_caller_that_still_calls_the_callee_is_evidence(
    import_line: str, call_after: str
) -> None:
    call_before = "record(value)" if "{" in import_line else "tel.record(value)"
    probe = _telemetry_probe(
        _caller(import_line, call_before),
        right={_CALLER: _caller(import_line, call_after)},
    )

    assert await _accepted(probe)


_IMPORT_RECORD = "import { record } from './telemetry';"
_AUDIT_HELPER = "export function audit(value: number) {\n  return record(value);\n}\n"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("caller", "caller_after", "caller_diff"),
    [
        # The correction drops the anchored call; an unrelated helper still calls it.
        (
            _caller(_IMPORT_RECORD) + _AUDIT_HELPER,
            _caller(_IMPORT_RECORD, "value") + _AUDIT_HELPER,
            "@@ -4 +4 @@\n-  return record(value);\n+  return value;\n",
        ),
        # The correction deletes the anchored line; the line above it still calls.
        (
            _caller(_IMPORT_RECORD).replace("  return", "  record(value);\n  return", 1),
            _caller(_IMPORT_RECORD, "").replace("  return ;", "  record(value);"),
            "@@ -5 +4,0 @@\n-  return record(value);\n",
        ),
    ],
)
async def test_another_surviving_call_does_not_stand_in_for_the_anchored_one(
    caller: str, caller_after: str, caller_diff: str
) -> None:
    """The anchored call is mapped through the correction (PRRT_kwDOSJAM6s6vRAzJ)."""
    anchor = caller.splitlines().index("  return record(value);") + 1
    probe = _EsProbe(
        files={_CALLER: caller, _TELEMETRY: _RECORD_DEFINITION},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY), _CALLER: caller_diff},
        right={_CALLER: caller_after},
    )

    assert not await _accepted(probe, item_line=anchor)


@pytest.mark.unit
async def test_a_corrected_caller_is_read_at_the_relocated_anchor() -> None:
    """Lines inserted above the anchored call move it (PRRT_kwDOSJAM6s6vRAzJ)."""
    caller_after = _caller(_IMPORT_RECORD).replace("\n\n", "\n// a\n// b\n\n", 1)
    probe = _EsProbe(
        files={_CALLER: _caller(_IMPORT_RECORD), _TELEMETRY: _RECORD_DEFINITION},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY), _CALLER: "@@ -1,0 +2,2 @@\n+// a\n+// b\n"},
        right={_CALLER: caller_after},
    )

    assert caller_after.splitlines()[5] == "  return record(value);"
    assert await _accepted(probe)


@pytest.mark.unit
async def test_an_unreadable_caller_diff_fails_closed() -> None:
    probe = _EsProbe(
        files={_CALLER: _caller(_IMPORT_RECORD), _TELEMETRY: _RECORD_DEFINITION},
        diffs={_TELEMETRY: _body_edit(_TELEMETRY), _CALLER: None},
    )

    assert not await _accepted(probe)


_MOVED = "src/app/metrics.ts"


def _move_probe(*, caller_after: str) -> _EsProbe:
    return _EsProbe(
        files={
            _CALLER: _caller("import { record } from './telemetry';"),
            _TELEMETRY: _RECORD_DEFINITION,
        },
        diffs={_TELEMETRY: _body_edit(_TELEMETRY)},
        renames={_TELEMETRY: _MOVED},
        right={_TELEMETRY: None, _MOVED: _RECORD_DEFINITION, _CALLER: caller_after},
    )


@pytest.mark.unit
async def test_a_move_with_the_caller_updated_is_evidence() -> None:
    probe = _move_probe(caller_after=_caller("import { record } from './metrics';"))

    assert await _accepted(probe)


@pytest.mark.unit
async def test_a_move_without_the_caller_updated_fails_closed() -> None:
    probe = _move_probe(caller_after=_caller("import { record } from './telemetry';"))

    assert not await _accepted(probe)


# --- conservative shadowing stays narrow enough --------------------------------------


@pytest.mark.unit
async def test_an_assignment_from_the_call_is_not_a_rebinding() -> None:
    caller = _caller("import { record } from './telemetry';", "record(value)").replace(
        "  return record(value);", "  const result = record(value); return result;"
    )

    assert await _accepted(_telemetry_probe(caller))


@pytest.mark.unit
async def test_a_call_inside_jsx_with_apostrophes_in_its_text_is_evidence() -> None:
    caller_path = "src/app/View.tsx"
    caller = (
        "import { record } from './telemetry';\n"
        "\n"
        "export function View({ value }: { value: number }) {\n"
        '  return <p title="x">{record(value)}</p>;\n'
        "}\n"
        "\n"
        "export const Note = () => <span>Don't panic</span>;\n"
    )
    probe = _telemetry_probe(caller)
    probe.files[caller_path] = caller

    assert await _accepted(probe, item_path=caller_path)


# --- anchors and targets this reader cannot pin to one call fail closed --------------


def _anchored(body: str) -> tuple[str, int]:
    """``body`` under the ``record`` import, and the line of its ``record(`` marker."""
    caller = "import { record } from './telemetry';\n" + body
    anchor = next(i for i, line in enumerate(caller.splitlines(), 1) if "record(" in line)
    return caller, anchor


@pytest.mark.unit
@pytest.mark.parametrize(
    "body",
    [
        "export class Ingest {\n  record(value: number) {\n    return value;\n  }\n}\n",
        "export class Ingest {\n  async record(value: number) {\n    return value;\n  }\n}\n",
        "export class Ingest {\n  get record() {\n    return 1;\n  }\n}\n",
        "export const handlers = {\n  record(value: number) {\n    return value;\n  },\n};\n",
        "export interface Sink {\n  record(value: number): void;\n}\n",
        "export class Ingest {\n  record(value: number): number {\n    return value;\n  }\n}\n",
        "export const handlers = {\n  record: (value: number) => value,\n  flush: () => 1,\n"
        "  replay() {},\n  record(value) { return value; },\n};\n",
    ],
)
async def test_a_definition_on_the_anchored_line_is_not_a_call(body: str) -> None:
    """``record(v) {`` in a class, object or interface defines ``record``; it calls nothing."""
    caller, anchor = _anchored(body)

    assert not await _accepted(_telemetry_probe(caller), item_line=anchor)


@pytest.mark.unit
@pytest.mark.parametrize(
    "call",
    [
        "getClient().\n    record(value)",
        "clients[0]?.\n    record(value)",
        "clients[0].\n\n    record(value)",
    ],
)
async def test_a_member_call_split_after_its_dot_is_not_a_bare_call(call: str) -> None:
    """``getClient().`` ending the line above makes ``record(value)`` a member call."""
    caller = _caller("import { record } from './telemetry';", call)
    anchor = next(i for i, line in enumerate(caller.splitlines(), 1) if "record(" in line)

    assert not await _accepted(_telemetry_probe(caller), item_line=anchor)


@pytest.mark.unit
async def test_a_non_null_division_cannot_hide_a_shadow_from_the_tokenizer() -> None:
    """``value! / 2`` is division; read as a regex it swallowed the ``record`` binding."""
    caller, anchor = _anchored(
        "export function ingest(value: number) {\n"
        "  const half = value! / 2, record = (v: number) => v * half; const q = value / 3;\n"
        "  return record(value) + q;\n"
        "}\n"
    )

    assert not await _accepted(_telemetry_probe(caller), item_line=anchor)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("definition", "edited_line"),
    [
        # A second, nested ``record`` whose edit the line-based span scan accepted.
        (
            _RECORD_DEFINITION + "function wrap() {\nfunction record(value: number) {\n"
            "  return value;\n}\nreturn record;\n}\n",
            6,
        ),
        # The only head sits inside a block, not at module scope.
        (
            "if (enabled) {\nfunction record(value: number) {\n  return value + 1;\n}\n}\n"
            "export { record };\n",
            3,
        ),
        # A nested head the module-scope span swallows: one line ends the arrow
        # and opens a class, so the indented method reads as the arrow's body.
        (
            "export const record = (value: number) => value; class Box {\n"
            "  record(value: number) {\n  return value + 1;\n  }\n}\n",
            3,
        ),
        # Another statement the span swallows after the definition ends on its
        # head line: the edit changes ``other``, never ``record``.
        (
            "export const record = (value: number) => value; const other = () => {\n"
            "  return value + 1;\n};\n",
            2,
        ),
        (
            "export function record(value: number) { return value; } function other() {\n"
            "  return value + 1;\n}\n",
            2,
        ),
    ],
)
async def test_a_target_without_one_module_scope_head_fails_closed(
    definition: str, edited_line: int
) -> None:
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"))
    probe.files[_TELEMETRY] = definition
    probe.diffs[_TELEMETRY] = _body_edit(_TELEMETRY, line=edited_line)

    assert not await _accepted(probe)


@pytest.mark.unit
async def test_an_unrelated_nested_head_outside_the_span_still_accepts() -> None:
    """A same-named method elsewhere does not hide the sole module-scope head."""
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"))
    probe.files[_TELEMETRY] = (
        _RECORD_DEFINITION + "class Box {\n  record(value: number) {\n    return value;\n  }\n}\n"
    )

    assert await _accepted(probe)


@pytest.mark.unit
@pytest.mark.parametrize(
    "definition",
    [
        "export const record = (value: number) => {\n  return value + 1;\n};\n",
        "export const record = (value: number) =>\n  value + 1;\n",
        "export function record(value: number): { a: number } {\n  return value + 1;\n}\n",
    ],
)
async def test_a_definition_ending_with_its_span_still_accepts(definition: str) -> None:
    """A body edit inside a definition the span ends with is evidence."""
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"))
    probe.files[_TELEMETRY] = definition

    assert await _accepted(probe)


def _hunk(path: str, header: str, *lines: str) -> str:
    """A ``-U0`` diff of ``path`` with one hunk ``header`` and its ``lines``."""
    return f"--- a/{path}\n+++ b/{path}\n@@ {header} @@\n" + "".join(f"{line}\n" for line in lines)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("definition", "diff"),
    [
        # An indented insert after a one-line arrow's ``;`` follows the definition.
        (
            "export const record = (value: number) => value + 1;\nother();\n",
            _hunk(_TELEMETRY, "-1,0 +2", "+  console.log(value);"),
        ),
        # An ASI-ended arrow does not reach the indented statement below it.
        (
            "export const record = (value: number) => value + 1\n  other()\n",
            _hunk(_TELEMETRY, "-2 +2", "-  other()", "+  another()"),
        ),
        # A decorator-shaped insert above a function is no part of it.
        (_RECORD_DEFINITION, _hunk(_TELEMETRY, "-0,0 +1", "+@trace")),
    ],
)
async def test_an_edit_outside_the_bracket_span_is_not_evidence(definition: str, diff: str) -> None:
    """Spans come from bracket depth, never indentation (PRRT_kwDOSJAM6s6vQABw)."""
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"))
    probe.files[_TELEMETRY] = definition
    probe.diffs[_TELEMETRY] = diff

    assert not await _accepted(probe)


@pytest.mark.unit
async def test_a_pure_insert_inside_the_bracket_span_is_evidence() -> None:
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"))
    probe.diffs[_TELEMETRY] = _hunk(_TELEMETRY, "-1,0 +2", "+console.log(value);")

    assert await _accepted(probe)


@pytest.mark.unit
async def test_a_definition_surviving_only_inside_a_literal_fails_closed() -> None:
    """At ``right`` the definition must still be a real head, not text in a template."""
    survivor = "export { record };\nconst doc = `\nfunction record(value: number) {\n}\n`;\n"
    probe = _telemetry_probe(
        _caller("import { record } from './telemetry';"), right={_TELEMETRY: survivor}
    )

    assert not await _accepted(probe)


@pytest.mark.unit
async def test_the_module_reader_reads_each_text_once() -> None:
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"))
    shows: list[list[str]] = []
    run = probe._deps.runner.run

    async def counting_run(cmd: list[str], **kwargs: object):
        shows.extend([cmd] if "show" in cmd else [])
        return await run(cmd, **kwargs)

    probe._deps.runner.run = counting_run
    reader = es_evidence._EsModuleReader(probe, worktree_path=Path("/tmp/ws"), ref=_LEFT)

    assert await reader.text(_TELEMETRY) == await reader.text(_TELEMETRY) == _RECORD_DEFINITION
    assert len(shows) == 1


@pytest.mark.unit
async def test_a_directory_import_with_a_package_manifest_fails_closed() -> None:
    """``package.json`` beside ``index.ts`` may point the directory import elsewhere."""
    index = "src/app/telemetry/index.ts"
    probe = _EsProbe(
        files={
            _CALLER: _caller("import { record } from './telemetry';"),
            index: _RECORD_DEFINITION,
            "src/app/telemetry/package.json": '{"main": "./dist/other.js"}\n',
        },
        diffs={index: _body_edit(index)},
    )

    assert not await _accepted(probe)
