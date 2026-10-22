"""JS/TS cross-file callee evidence through ES-module bindings (issue #1047).

The JS/TS counterpart of ``_commit_range_changes_callee_definition``'s Python
body. A JS/TS callee binds through the call-site file's own ``import``
statements, so a changed file is evidence only when that binding resolves to
it: ``import { record } from './telemetry'; record()`` is fixed by an edit to
``record`` in ``./telemetry``'s file, never by an edit to a same-named
``record`` elsewhere. Resolution follows relative specifiers the way TS/Node
probe them and at most one re-export hop (``export { x } from``, ``export *
from`` or a barrel re-exporting its own import). Anything it cannot resolve
statically fails closed — there is no name-only fallback for JS/TS.

The pure readers live in ``..._ancestry_cross_file_es_modules``; this module
does the Git reads. Helpers from the parent modules are imported inside the
functions, the cycle-avoidance pattern ``..._ancestry_cross_file`` uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_cross_file_es_modules as _es,
)
from awf.runtime.pr_monitor_runner.git_utils import git_worktree_command

# What one export lookup proves: the ``(path, local_name)`` that defines the
# name, False when the module provably does not export it, or None when this
# reader cannot tell (unresolvable, unreadable, or deeper than one hop).
_Lookup = tuple[str, str] | bool | None

# A module's export table, import table and tokens.
_ParsedModule = tuple[_es._EsExports, dict[str, _es._EsBinding], tuple[_es._EsToken, ...]]


async def _paths_existing_at_ref(
    self: Any, *, worktree_path: Path, ref: str, paths: tuple[str, ...]
) -> frozenset[str] | None:
    """Which of ``paths`` exist as files at ``ref``, or None when unreadable.

    One ``git ls-tree`` answers every probe of a specifier. Pathspecs are
    literal, so a ``[`` or ``*`` in a path matches only itself.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _git_env_for_merge_safety_object_lookup,
    )

    result = await self._deps.runner.run(
        git_worktree_command(
            worktree_path, "ls-tree", "-r", "-z", "--name-only", ref, "--", *paths
        ),
        env={**_git_env_for_merge_safety_object_lookup(), "GIT_LITERAL_PATHSPECS": "1"},
    )
    if not result.ok:
        return None
    listed = frozenset(str(result.stdout or "").split("\0"))
    return frozenset(path for path in paths if path in listed)


class _EsModuleReader:
    """Resolves ES-module bindings at one ref, caching every Git read."""

    def __init__(self, owner: Any, *, worktree_path: Path, ref: str) -> None:
        self.owner = owner
        self.worktree_path = worktree_path
        self.ref = ref
        self._texts: dict[str, str | None] = {}
        self._resolved: dict[tuple[str, str], str | None] = {}
        self._parsed: dict[str, _ParsedModule | None] = {}

    async def text(self, path: str) -> str | None:
        from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
            _path_text_at_ref,
        )

        if path not in self._texts:
            self._texts[path] = await _path_text_at_ref(
                self.owner, worktree_path=self.worktree_path, ref=self.ref, path=path
            )
        return self._texts[path]

    async def resolve(self, importer: str, specifier: str) -> str | None:
        """The one file ``specifier`` imported from ``importer`` resolves to."""
        key = (importer, specifier)
        if key not in self._resolved:
            probes = _es._relative_specifier_probe_paths(importer, specifier)
            existing = (
                None
                if probes is None
                else await _paths_existing_at_ref(
                    self.owner,
                    worktree_path=self.worktree_path,
                    ref=self.ref,
                    paths=probes + _es._directory_manifest_paths(probes),
                )
            )
            self._resolved[key] = (
                None
                if probes is None or existing is None
                else _es._resolved_module_path(probes, existing)
            )
        return self._resolved[key]

    async def parsed(self, path: str) -> _ParsedModule | None:
        """``path``'s export and import tables and tokens, or None when unreadable."""
        if path not in self._parsed:
            text = await self.text(path)
            tokens = None if text is None else _es._es_module_tokens(text, path=path)
            exports = None if tokens is None else _es._es_module_exports(tokens)
            imports = None if tokens is None else _es._es_import_bindings(tokens)
            self._parsed[path] = (
                None
                if tokens is None or exports is None or imports is None
                else (exports, imports, tokens)
            )
        return self._parsed[path]

    async def definition_heads(self, path: str, name: str) -> list[_es._EsToken]:
        """The tokens heading a definition of ``name`` in ``path``; none when unreadable."""
        parsed = await self.parsed(path)
        return [] if parsed is None else _es._es_definition_heads(parsed[2], name)

    async def lookup(self, module: str, name: str, *, hops: int) -> _Lookup:
        """Where ``module``'s export ``name`` is defined, following ``hops`` re-exports.

        A local export whose local name the module itself imports is a barrel
        re-export and costs a hop, as does ``export { } from``. ``export *``
        sources cost a hop too: each must be readable, and exactly one may
        define the name locally — an unreadable source, a source that is
        itself a barrel, or two defining sources fail closed. A star never
        carries ``default``. A namespace re-export is not callable. A local
        name the module also assigns to fails closed: an export is a live
        binding, so importers reach whatever was assigned last.
        """
        parsed = await self.parsed(module)
        if parsed is None:
            return None
        exports, imports, tokens = parsed
        forwarded: _es._EsBinding | None
        if name in exports.local:
            local_name = exports.local[name]
            forwarded = imports.get(local_name)
            if forwarded is None:
                if _es._es_assigned_names(tokens, frozenset({local_name})):
                    return None
                return module, local_name
        elif name in exports.reexport:
            forwarded = exports.reexport[name]
        elif name in exports.namespace or exports.opaque:
            return None
        elif name == "default" or not exports.star:
            return False
        else:
            return await self._star_lookup(module, exports.star, name, hops=hops)
        if hops < 1 or forwarded.specifier is None or forwarded.imported == "*":
            return None
        source = await self.resolve(module, forwarded.specifier)
        if source is None:
            return None
        found = await self.lookup(source, forwarded.imported, hops=hops - 1)
        return found if isinstance(found, tuple) else None

    async def _star_lookup(
        self, module: str, specifiers: tuple[str, ...], name: str, *, hops: int
    ) -> _Lookup:
        if hops < 1:
            return None
        found: set[tuple[str, str]] = set()
        for specifier in specifiers:
            source = await self.resolve(module, specifier)
            if source is None:
                return None
            result = await self.lookup(source, name, hops=hops - 1)
            if result is None:
                return None
            if isinstance(result, tuple):
                found.add(result)
        if len(found) > 1:
            return None
        return next(iter(found)) if found else False

    async def target(self, importer: str, specifier: str, name: str) -> tuple[str, str] | None:
        """``(definition_path, local_name)`` of ``importer``'s ``specifier`` export ``name``."""
        module = await self.resolve(importer, specifier)
        if module is None:
            return None
        found = await self.lookup(module, name, hops=1)
        return found if isinstance(found, tuple) else None


def _es_call_site_bindings(
    text: str, *, path: str
) -> tuple[dict[str, _es._EsBinding], frozenset[str], tuple[_es._EsToken, ...]] | None:
    """``(imports, rebound, tokens)`` of one call-site text, or None when unreadable."""
    tokens = _es._es_module_tokens(text, path=path)
    imports = None if tokens is None else _es._es_import_bindings(tokens)
    if tokens is None or imports is None:
        return None
    return imports, _es._es_rebound_names(tokens, frozenset(imports)), tokens


async def _es_module_commit_range_changes_callee_definition(
    self: Any,
    *,
    worktree_path: Path,
    left: str,
    right: str,
    item_path: str,
    item_text: str,
    item_line: int,
) -> bool:
    """True when ``left``..``right`` changes the definition a JS/TS call site binds to.

    Each callee ref at ``item_line`` is resolved at ``left`` through the call
    site's ES-module bindings to one ``(path, local_name)``. That path must be
    one of the range's changed files (other than ``item_path``), the range's
    diff must overlap the definition's span there, and the *corrected* caller
    must still reach the same definition at ``right`` — at the moved path when
    the range renamed it — where the definition must still exist. That last
    read replaces the Python path's rename and survival rules: it fails closed
    on a correction that re-points the import, shadows the name, deletes the
    definition or moves it without updating the caller. Every unresolvable
    step fails closed, keeping the item on the reason-coded
    ``needs_human`` path.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry import (
        _changed_paths_in_commit_range,
        _diff_hunk_overlaps_line_span,
        _normalize_evidence_item_path,
        _rename_map_in_commit_range,
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_callees import (
        _callee_refs_from_file_line,
        _receiver_chain_segments_from_file_line,
    )
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file import (
        _MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS,
        _callee_definition_survives_at_right,
        _diff_adds_decorators_above_span,
        _importable_definition_spans_for_names,
        _path_diff_text_in_commit_range,
    )

    call_site = _es_call_site_bindings(item_text, path=item_path)
    if call_site is None:
        return False
    imports, rebound, tokens = call_site
    # A chained receiver (``tel.sub.record()``) is an attribute no import binds,
    # and every other unresolvable ref drops out here, so it fails closed. So
    # does a bare ref the anchored line also defines or reaches as a member
    # (``record(v) {``, or ``getClient().`` ending the line above).
    chains = _receiver_chain_segments_from_file_line(item_text, item_line, path=item_path)
    non_calls = _es._es_line_non_call_names(tokens, item_line)
    chained = _es._es_chained_member_refs(tokens, item_line)
    refs = [
        (qualifier, name, reached)
        for qualifier, name in sorted(
            _callee_refs_from_file_line(item_text, item_line, path=item_path),
            key=lambda ref: (ref[0] or "", ref[1]),
        )
        if qualifier not in chains
        and (qualifier is not None or name not in non_calls)
        and (reached := _es._es_ref_target(imports, rebound, chained, qualifier, name)) is not None
    ]
    if not refs:
        return False
    candidates = [
        normalized
        for normalized in (
            _normalize_evidence_item_path(changed)
            for changed in await _changed_paths_in_commit_range(
                self, worktree_path=worktree_path, left=left, right=right
            )
        )
        if normalized and normalized != item_path
    ][:_MAX_CALLEE_EVIDENCE_CANDIDATE_PATHS]
    if not candidates:
        return False
    rename_map, _name_status_z = await _rename_map_in_commit_range(
        self, worktree_path=worktree_path, left=left, right=right
    )
    at_left = _EsModuleReader(self, worktree_path=worktree_path, ref=left)
    at_right = _EsModuleReader(self, worktree_path=worktree_path, ref=right)
    caller_after = rename_map.get(item_path) or item_path
    for qualifier, name, reached in refs:
        target = await at_left.target(item_path, *reached)
        if target is None or target[0] not in candidates:
            continue
        path, local_name = target
        text = await at_left.text(path)
        bare_names = frozenset({local_name})
        spans = _importable_definition_spans_for_names(
            text or "", frozenset(), path=path, bare_names=bare_names
        )
        # The line-based span scan cannot see brackets, so the file must hold
        # exactly one head for the name at depth 0, and it must be the only
        # head inside the one span found. A nested head outside that span (an
        # unrelated ``Box.record``) cannot be what the overlapping edit changed.
        heads = await at_left.definition_heads(path, local_name)
        module_heads = [head for head in heads if head.depth == 0]
        if not (
            len(spans) == 1
            and len(module_heads) == 1
            and [head for head in heads if spans[0][0] <= head.line <= spans[0][1]] == module_heads
        ):
            continue
        diff_text = await _path_diff_text_in_commit_range(
            self,
            worktree_path=worktree_path,
            left=left,
            right=right,
            path=path,
            rename_path=rename_map.get(path),
        )
        if diff_text is None or not any(
            _diff_hunk_overlaps_line_span(diff_text, start, end, file_text=text)
            or _diff_adds_decorators_above_span(diff_text, start)
            for start, end in spans
        ):
            continue
        moved = rename_map.get(path) or path
        if await _es_caller_still_reaches(
            at_right, caller=caller_after, qualifier=qualifier, name=name
        ) != (moved, local_name):
            continue
        if await _callee_definition_survives_at_right(
            self,
            worktree_path=worktree_path,
            right=right,
            path=moved,
            names=frozenset(),
            bare_names=bare_names,
            enclosed_by={},
        ):
            return True
    return False


async def _es_caller_still_reaches(
    reader: _EsModuleReader, *, caller: str, qualifier: str | None, name: str
) -> tuple[str, str] | None:
    """The definition the corrected caller's same ref reaches at ``reader``'s ref.

    The anchored occurrence was already held to a non-chained receiver at
    ``left``; here only the corrected file's bindings matter, since its
    anchor line is unknown and a chain elsewhere says nothing about it.
    """
    text = await reader.text(caller)
    call_site = None if text is None else _es_call_site_bindings(text, path=caller)
    if call_site is None:
        return None
    imports, rebound, _tokens = call_site
    reached = _es._es_ref_target(imports, rebound, frozenset(), qualifier, name)
    return None if reached is None else await reader.target(caller, *reached)
