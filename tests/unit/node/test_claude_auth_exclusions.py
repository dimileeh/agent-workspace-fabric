"""Top-level-anchored ``~/.claude`` copy/signature exclusions (#874).

The pre-#874 constant matched excluded names by *basename at every depth*, so
widening it to cover volatile host state would also have stripped nested
``plugins/cache`` (an installed-plugin store) and stray ``node_modules`` from
every agent's ``~/.claude``. These tests pin the anchoring, the closed-allowlist
governing rule, and the copy-set ⊆ signature-set invariant.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.node.auth_mounts_claude_exclusions import (
    CLAUDE_COPY_EXCLUDED_TOP_LEVEL,
    CLAUDE_SIGNATURE_EXCLUDED_PATTERNS,
    CLAUDE_SIGNATURE_EXCLUDED_TOP_LEVEL,
    claude_copy_excludes_rel,
    claude_copy_ignore,
    claude_signature_excludes_rel,
)

# Names that carry auth/config/content the agent must see. None of them may ever
# be excluded from the *signature*: excluding one means a host credential change
# would not mint a new base signature and the workspace would mount a stale base.
_MUST_BE_SIGNED = frozenset(
    {
        ".credentials.json",
        "settings.json",
        "settings.local.json",
        "skills",
        "plugins",
        "agents",
        "CLAUDE.md",
    }
)


@pytest.mark.unit
def test_copy_exclusions_membership_is_unchanged_from_pre_874() -> None:
    # Keeping the copy set byte-identical is what makes #874 zero-risk for the
    # credential path: only the *matching semantics* narrow to the top level.
    assert set(CLAUDE_COPY_EXCLUDED_TOP_LEVEL) == {
        "projects",
        "todos",
        "shell-snapshots",
        "statsig",
    }


@pytest.mark.unit
def test_signature_exclusions_are_a_strict_superset_of_copy_exclusions() -> None:
    assert CLAUDE_COPY_EXCLUDED_TOP_LEVEL < CLAUDE_SIGNATURE_EXCLUDED_TOP_LEVEL


@pytest.mark.unit
def test_signature_exclusions_never_cover_auth_or_content_names() -> None:
    assert _MUST_BE_SIGNED.isdisjoint(CLAUDE_SIGNATURE_EXCLUDED_TOP_LEVEL)
    for name in _MUST_BE_SIGNED:
        assert claude_signature_excludes_rel(name) is False


@pytest.mark.unit
def test_signature_exclusion_set_is_a_closed_allowlist() -> None:
    # The governing rule: anything unrecognised is SIGNED. A future Claude Code
    # state dir should cause churn (annoying but correct), never a stale
    # credential (silent and wrong).
    assert claude_signature_excludes_rel("future-state-dir") is False
    assert claude_signature_excludes_rel("future-state.json") is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
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
        "projects",
        "todos",
        "shell-snapshots",
        "statsig",
    ],
)
def test_known_volatile_top_level_names_are_excluded_from_the_signature(name: str) -> None:
    assert claude_signature_excludes_rel(name) is True


@pytest.mark.unit
@pytest.mark.parametrize("name", ["daemon.sock", "daemon-1.log", "plugin-cache.json"])
def test_signature_patterns_match_volatile_top_level_names(name: str) -> None:
    assert claude_signature_excludes_rel(name) is True


@pytest.mark.unit
def test_signature_patterns_are_declared_for_introspection() -> None:
    assert "daemon*" in CLAUDE_SIGNATURE_EXCLUDED_PATTERNS
    assert "*-cache.json" in CLAUDE_SIGNATURE_EXCLUDED_PATTERNS


@pytest.mark.unit
@pytest.mark.parametrize(
    "rel",
    [
        "plugins/cache",
        "skills/cache",
        "plugins/repos/x/node_modules",
        "projects/nested/history.jsonl",
        "plugins/daemon.sock",
    ],
)
def test_signature_exclusions_are_anchored_to_the_top_level(rel: str) -> None:
    # Nested paths are never excluded — matching by basename at every depth is
    # exactly the trap #874 forbids.
    assert claude_signature_excludes_rel(rel) is False


@pytest.mark.unit
def test_copy_exclusion_predicate_is_anchored_to_the_top_level() -> None:
    assert claude_copy_excludes_rel("projects") is True
    assert claude_copy_excludes_rel("plugins/projects") is False
    assert claude_copy_excludes_rel("history.jsonl") is False


@pytest.mark.unit
def test_copy_ignore_strips_top_level_usage_history_only(tmp_path: Path) -> None:
    root = tmp_path / ".claude"
    ignore = claude_copy_ignore(root)

    at_root = ignore(str(root), ["projects", "todos", "skills", "plugins", "settings.json"])

    assert at_root == {"projects", "todos"}


@pytest.mark.unit
def test_copy_ignore_keeps_nested_plugin_cache_and_node_modules(tmp_path: Path) -> None:
    # The trap-(a) regression: pre-#874 ``ignore_patterns`` matched at every
    # depth, so a widened set would have stripped ``plugins/cache`` (~66 MB of
    # installed plugins) and every nested ``node_modules`` from the copy.
    root = tmp_path / ".claude"
    ignore = claude_copy_ignore(root)

    nested = ignore(str(root / "plugins"), ["cache", "node_modules", "projects", "repos"])

    assert nested == set()


@pytest.mark.unit
def test_copy_ignore_normalizes_the_visited_directory_path(tmp_path: Path) -> None:
    # ``shutil.copytree`` passes ``os.fspath(src)``; a non-normalized root must
    # still compare equal so the top-level exclusion is not silently skipped.
    root = tmp_path / ".claude"

    ignore = claude_copy_ignore(Path(f"{root}/./"))

    assert ignore(str(root), ["projects", "skills"]) == {"projects"}
