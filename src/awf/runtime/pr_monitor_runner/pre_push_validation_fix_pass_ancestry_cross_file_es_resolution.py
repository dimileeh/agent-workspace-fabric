"""Relative-specifier module-path resolution for ES-module imports (issue #1047).

Split from ``..._ancestry_cross_file_es_modules``, which re-exports these
names. Only ``./`` / ``../`` specifiers resolve, probed the way TS/Node probe
them; a specifier that does not resolve to exactly one file fails closed.
"""

from __future__ import annotations

import posixpath

# Probe suffixes per specifier extension, in TS/Node resolution order. An
# extensionless specifier also probes ``index`` files under it.
_EXTENSIONLESS_PROBES = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mts", ".cts", ".mjs", ".cjs")
_EXTENSION_PROBES = {
    ".js": (".ts", ".tsx", ".d.ts", ".js", ".jsx"),
    ".jsx": (".tsx", ".d.ts", ".jsx"),
    ".mjs": (".mts", ".d.mts", ".mjs"),
    ".cjs": (".cts", ".d.cts", ".cjs"),
    ".ts": (".ts",),
    ".tsx": (".tsx",),
    ".mts": (".mts",),
    ".cts": (".cts",),
}
_DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")


def _relative_specifier_probe_paths(importer: str, specifier: str) -> tuple[str, ...] | None:
    """Repo paths a relative ``specifier`` imported from ``importer`` may resolve to.

    Only ``./`` / ``../`` specifiers (and ``.`` / ``..``) are resolvable here.
    Bare packages, path aliases (``@/x``, ``~/x``), absolute paths, ``node:``
    and URLs depend on configuration this reader does not see, so they return
    None, as does a path climbing above the repository root. A ``.js``
    specifier maps to its ``.ts`` source the way TS resolves it.
    """
    if not (specifier in (".", "..") or specifier.startswith(("./", "../"))):
        return None
    directory_only = specifier in (".", "..") or specifier.endswith("/")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    if target == ".." or target.startswith("../"):
        return None
    target = "" if target == "." else target
    if directory_only:
        return tuple(posixpath.join(target, f"index{suffix}") for suffix in _EXTENSIONLESS_PROBES)
    stem, suffix = posixpath.splitext(target)
    if suffix.lower() in _EXTENSION_PROBES:
        return tuple(f"{stem}{probe}" for probe in _EXTENSION_PROBES[suffix.lower()])
    return tuple(f"{target}{probe}" for probe in _EXTENSIONLESS_PROBES) + tuple(
        f"{target}/index{probe}" for probe in _EXTENSIONLESS_PROBES
    )


def _directory_manifest_paths(probes: tuple[str, ...]) -> tuple[str, ...]:
    """The ``package.json`` beside each ``index`` file among ``probes``, deduplicated."""
    return tuple(
        dict.fromkeys(
            posixpath.join(posixpath.dirname(path), "package.json")
            for path in probes
            if posixpath.basename(path).startswith("index.")
        )
    )


def _resolved_module_path(probes: tuple[str, ...], existing: frozenset[str]) -> str | None:
    """The one runtime file ``probes`` resolve to among ``existing``, or None.

    Declaration files carry no runtime definition and are ignored. Zero or
    several runtime candidates (``x.ts`` beside ``x.js`` or ``x/index.ts``)
    fail closed, which is deterministic whatever a toolchain would prefer. An
    ``index`` file with a ``package.json`` beside it fails closed too: the
    manifest's ``main`` / ``exports`` / ``types`` may point the directory
    import at another file.
    """
    found = [
        path for path in probes if path in existing and not path.endswith(_DECLARATION_SUFFIXES)
    ]
    if len(found) != 1:
        return None
    manifests = _directory_manifest_paths((found[0],))
    return None if any(manifest in existing for manifest in manifests) else found[0]
