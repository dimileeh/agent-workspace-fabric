"""Top-level-anchored ``~/.claude`` copy and signature exclusion sets (#874).

Dependency-free leaf shared by the shared-base signature walk
(:mod:`awf.node.auth_mounts_claude_base`), the legacy per-workspace copy
(:mod:`awf.node.auth_mounts_claude`) and the fallback-edit reconcile walks
(:mod:`awf.node.auth_mounts_claude_reconcile`). Those modules re-export the
public names so ``awf.node.auth_mounts.<name>`` stays the stable import surface.

Two distinct sets, deliberately not one:

**Copy exclusions** — what is left *out of* the per-workspace/base copy. Excluding
a name here means the agent never sees it, so the membership is exactly the
pre-#874 usage-history set (``projects``/``todos``/``shell-snapshots``/``statsig``,
kept out so ``ccusage`` cannot attribute the host's prior usage to the workspace
run). Keeping it byte-identical means #874 carries zero risk of dropping an auth
file; only the *matching semantics* narrow (see anchoring below).

**Signature exclusions** — what is left out of the host-content hash that names
the shared base (:func:`awf.node.auth_mounts_claude_base._host_claude_signature`).
A strict superset of the copy set: it adds host state that changes on *every*
Claude Code interaction (``history.jsonl``, ``file-history``, ``cache``, …).
Those entries are still **copied** — the agent gets a frozen snapshot of them —
they simply no longer mint a new signature, so a ~1.9 GB base rebuild is not
triggered by the operator merely using Claude Code on the host (#874 observed 5
base hashes built and reaped in a single worker lifetime).

**Governing rule (load-bearing, enforced by test):** the signature-exclusion set
is a **closed allowlist** of known-volatile names. Anything unrecognised — a
top-level entry a future Claude Code release introduces — is *signed*. The
failure modes are asymmetric: signing something volatile causes needless base
churn (annoying, but the workspace still gets correct credentials), whereas
*not* signing something meaningful reuses a stale base and hands the workspace a
superseded credential — silent and wrong. So the set only ever grows by an
explicit, reviewed addition.

**Anchoring (trap (a) of #874):** the pre-#874 constant was consumed via
``shutil.ignore_patterns`` and a bare ``name in excluded`` check inside
``os.walk``, both of which match by *basename at every depth*. Widening that set
would therefore have stripped ``plugins/cache`` (an installed-plugin store,
~66 MB) and assorted nested ``node_modules`` dirs from every agent's
``~/.claude``. Every predicate here is anchored to the **top level** of the
``.claude`` tree: a depth-0 entry only.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from fnmatch import fnmatch
from pathlib import Path

# Historical usage/transcript dirs kept out of the per-workspace and shared-base
# copies. Excluding them keeps the workspace from seeing unrelated host
# transcripts (``ccusage`` reads these local files) and avoids copying large
# history trees. Membership is unchanged from pre-#874 — only the matching
# semantics narrowed to the top level.
CLAUDE_COPY_EXCLUDED_TOP_LEVEL: frozenset[str] = frozenset(
    {"projects", "todos", "shell-snapshots", "statsig"}
)

# Known-volatile top-level entries that are copied but not signed. Every name
# here changes on ordinary Claude Code use on the host, so signing it churns the
# shared-base signature on essentially every provision. See the closed-allowlist
# rule in the module docstring before adding to this set.
_CLAUDE_VOLATILE_TOP_LEVEL: frozenset[str] = frozenset(
    {
        "history.jsonl",
        "file-history",
        "cache",
        "paste-cache",
        "session-env",
        "sessions",
        "backups",
        "debug",
        "jobs",
        "tasks",
    }
)

CLAUDE_SIGNATURE_EXCLUDED_TOP_LEVEL: frozenset[str] = (
    CLAUDE_COPY_EXCLUDED_TOP_LEVEL | _CLAUDE_VOLATILE_TOP_LEVEL
)

# Volatile top-level entries whose exact name varies per process/run, so they
# cannot be listed literally. Matched with ``fnmatch`` against a depth-0 entry
# only. Deliberately narrow: ``*-cache.json`` does not match ``settings.json``
# and ``daemon*`` does not match any auth/config name.
CLAUDE_SIGNATURE_EXCLUDED_PATTERNS: tuple[str, ...] = ("daemon*", "*-cache.json")


def _is_top_level(rel: str) -> bool:
    """Return whether ``rel`` (a POSIX-relative entry) sits at the tree root."""

    return bool(rel) and "/" not in rel


def claude_copy_excludes_rel(rel: str) -> bool:
    """Return whether the copy skips ``rel``, a POSIX path relative to ``.claude``.

    Anchored: ``projects`` is excluded, ``plugins/projects`` is not.
    """

    return _is_top_level(rel) and rel in CLAUDE_COPY_EXCLUDED_TOP_LEVEL


def claude_signature_excludes_rel(rel: str) -> bool:
    """Return whether the host-content signature skips ``rel``.

    Anchored to the top level exactly as :func:`claude_copy_excludes_rel` is, so
    a nested ``plugins/cache`` or ``skills/cache`` is still signed (and therefore
    still forces a fresh base when an operator installs or updates a plugin).
    """

    if not _is_top_level(rel):
        return False
    if rel in CLAUDE_SIGNATURE_EXCLUDED_TOP_LEVEL:
        return True
    return any(fnmatch(rel, pattern) for pattern in CLAUDE_SIGNATURE_EXCLUDED_PATTERNS)


def claude_copy_ignore(root: Path) -> Callable[[str, Iterable[str]], set[str]]:
    """Return a ``shutil.copytree(ignore=...)`` callable anchored at ``root``.

    Replaces ``shutil.ignore_patterns(*names)``, which matched at every depth.
    The returned callable only reports exclusions when ``copytree`` is visiting
    ``root`` itself, so nested ``plugins/cache``, ``node_modules`` and
    ``*/projects`` trees are copied normally.
    """

    anchor = os.path.normpath(os.fspath(root))

    def _ignore(src: str, names: Iterable[str]) -> set[str]:
        if os.path.normpath(src) != anchor:
            return set()
        return {name for name in names if name in CLAUDE_COPY_EXCLUDED_TOP_LEVEL}

    return _ignore
