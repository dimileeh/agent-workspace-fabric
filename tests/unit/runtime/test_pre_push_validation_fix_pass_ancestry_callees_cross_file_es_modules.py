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
    ],
)
async def test_the_corrected_caller_must_still_reach_the_definition(
    right: dict[str, str | None],
) -> None:
    probe = _telemetry_probe(_caller("import { record } from './telemetry';"), right=right)

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
