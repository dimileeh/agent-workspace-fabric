"""Pre-push validation fix-pass salvage retention tests (part 009)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.common.commands import AsyncioSubprocessRunner, FakeCommandRunner
from awf.db.session import make_session_factory
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)
from tests.unit.runtime.test_pr_monitor_pre_push_validation import _mark_git_worktree


@pytest.fixture
async def factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_test_engine() as engine:
        yield make_session_factory(engine)


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo_with_lateral_tip(tmp_path: Path) -> tuple[Path, str, str]:
    """Return ``(repo, ancestor_sha, lateral_sha)`` where lateral is not a descendant."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    _git(repo, "config", "advice.graftFileDeprecated", "false")
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "ancestor")
    ancestor = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "checkout", "--orphan", "lateral", "-q")
    (repo / "c.txt").write_text("c\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "lateral tip")
    lateral = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return repo, ancestor, lateral


@pytest.mark.unit
def test_added_salvage_blob_retained_rejects_mid_line_modified_occurrence() -> None:
    """Commenting out an added call must not count as retained salvage bytes.

    ``enable_guard()\\n`` is a contiguous substring of ``# enable_guard()\\n``, so
    raw containment would reuse stale addition evidence after the functional call
    was disabled (PRRT_kwDOSJAM6s6Zm6F1).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _added_salvage_blob_retained,
    )

    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="enable_guard()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="enable_guard()\nextra\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="prefix\nenable_guard()\n",
    )
    # Appended rebinding of a salvage assignment must fail closed: the original
    # addition remains a line-aligned prefix, but the later assignment supersedes
    # it (PRRT_kwDOSJAM6s6Zp8jM).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED = False\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED: bool = False\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="#define FEATURE_ENABLED 1\n",
        head_blob="#define FEATURE_ENABLED 1\n#define FEATURE_ENABLED 0\n",
    )
    # YAML-style ``key: value`` rebinds must fail closed the same way equals-
    # style assignments do; the matcher previously only handled ``=`` / ``:=``
    # and declarations, so an appended override kept a line-aligned prefix and
    # reused stale FIXED evidence (PRRT_kwDOSJAM6s6ZqNAk).
    assert not _added_salvage_blob_retained(
        commit_blob="feature_enabled: true\n",
        head_blob="feature_enabled: true\nfeature_enabled: false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature_enabled: true\n",
        head_blob="feature_enabled: true\nfeature_enabled: false\nother_key: 1\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature_enabled: true\n",
        head_blob="feature_enabled: true\n# feature_enabled: false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature_enabled: true\n",
        head_blob="feature_enabled: true\nother_key: 1\n",
    )
    # Quoted JSON/YAML mapping keys (incl. hyphenated) must supersede like bare
    # identifiers; identifier-only matching left `"feature-enabled"` unbound so
    # an appended duplicate kept a line-aligned prefix and reused stale FIXED
    # evidence (PRRT_kwDOSJAM6s6ZqQfh).
    assert not _added_salvage_blob_retained(
        commit_blob='"feature-enabled": true\n',
        head_blob='"feature-enabled": true\n"feature-enabled": false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='"feature-enabled": true\n',
        head_blob=('"feature-enabled": true\n"other": 1\n"feature-enabled": false\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="'feature-enabled': true\n",
        head_blob="'feature-enabled': true\n'feature-enabled': false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob='"feature-enabled": true\n',
        head_blob='"feature-enabled": true\n# "feature-enabled": false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='"feature-enabled": true\n',
        head_blob='"feature-enabled": true\n"other-key": 1\n',
    )
    # TOML bare keys may include hyphens (`feature-enabled = true`). Identifier-
    # only matching left both salvage and appended rebind unbound, so the tip
    # kept a line-aligned prefix and reused stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zqip3). Quoted TOML keys use ``=`` (not ``:``).
    assert not _added_salvage_blob_retained(
        commit_blob="feature-enabled = true\n",
        head_blob="feature-enabled = true\nfeature-enabled = false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature-enabled = true\n",
        head_blob=("feature-enabled = true\nother = 1\nfeature-enabled = false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature-enabled: true\n",
        head_blob="feature-enabled: true\nfeature-enabled: false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob='"feature-enabled" = true\n',
        head_blob='"feature-enabled" = true\n"feature-enabled" = false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="'feature-enabled' = true\n",
        head_blob="'feature-enabled' = true\n'feature-enabled' = false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature-enabled = true\n",
        head_blob="feature-enabled = true\n# feature-enabled = false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature-enabled = true\n",
        head_blob="feature-enabled = true\nother-key = 1\n",
    )
    # Docstring / block-comment prose that reuses a salvage assignment name
    # (Google-style ``Args:`` / ``timeout: Seconds…``) must not count as a
    # YAML-style rebind; otherwise benign documentation drops FIXED evidence
    # (PRRT_kwDOSJAM6s6ZqPO9).
    assert _added_salvage_blob_retained(
        commit_blob="timeout = 30\n",
        head_blob=(
            "timeout = 30\n"
            '"""Client options.\n'
            "\n"
            "Args:\n"
            "    timeout: Seconds until the request fails.\n"
            '"""\n'
        ),
    )
    assert _added_salvage_blob_retained(
        commit_blob="timeout = 30\n",
        head_blob=("timeout = 30\n/*\ntimeout: Seconds until the request fails.\n*/\n"),
    )
    assert _added_salvage_blob_retained(
        commit_blob="timeout = 30\n",
        head_blob=(
            "timeout = 30\n"
            "'''Client options.\n"
            "\n"
            "Args:\n"
            "    timeout: Seconds until the request fails.\n"
            "'''\n"
        ),
    )
    # ``/*`` / nested quotes inside ordinary strings or ``#`` / ``//`` line
    # comments must not open block/triple state; otherwise a later real rebind
    # after a URL/glob/comment line is skipped and FIXED evidence is reused
    # (PRRT_kwDOSJAM6s6ZqSbO).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=(
            'FEATURE_ENABLED = True\nurl = "https://example.com/*/path"\nFEATURE_ENABLED = False\n'
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\npattern = 'foo/*bar'\nFEATURE_ENABLED = False\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=(
            "FEATURE_ENABLED = True\nhint = \"use ''' for docs\"\nFEATURE_ENABLED = False\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=(
            "FEATURE_ENABLED = True\n# see https://example.com/*/docs\nFEATURE_ENABLED = False\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\n// pattern: foo/*bar\nFEATURE_ENABLED = False\n"),
    )
    # Spaced ``# define`` is a real preprocessor binding (whitespace between ``#``
    # and the keyword is allowed, same as open-``#if`` scanning). Skipping it as
    # a comment would keep a line-aligned prefix and reuse stale salvage evidence
    # (PRRT_kwDOSJAM6s6Zp_sv).
    assert not _added_salvage_blob_retained(
        commit_blob="# define FEATURE_ENABLED 1\n",
        head_blob="# define FEATURE_ENABLED 1\n# define FEATURE_ENABLED 0\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="#define FEATURE_ENABLED 1\n",
        head_blob="#define FEATURE_ENABLED 1\n# define FEATURE_ENABLED 0\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="def guard():\n    return True\n",
        head_blob="def guard():\n    return True\ndef guard():\n    return False\n",
    )
    # ``export class`` / ``export default function`` must count as bindings the
    # same way bare ``class`` / ``export function`` do; otherwise an appended
    # rebind keeps a line-aligned prefix and reuses stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zp_sx). Avoid ``name =`` bodies so retention is gated on
    # the declaration binding, not an incidental field assignment.
    assert not _added_salvage_blob_retained(
        commit_blob="export class Guard {\n  ok() { return true; }\n}\n",
        head_blob=(
            "export class Guard {\n  ok() { return true; }\n}\n"
            "export class Guard {\n  ok() { return false; }\n}\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export default class Guard {\n  ok() { return true; }\n}\n",
        head_blob=(
            "export default class Guard {\n  ok() { return true; }\n}\n"
            "export default class Guard {\n  ok() { return false; }\n}\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export default function guard() {\n  return true;\n}\n",
        head_blob=(
            "export default function guard() {\n  return true;\n}\n"
            "export default function guard() {\n  return false;\n}\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export default async function guard() {\n  return true;\n}\n",
        head_blob=(
            "export default async function guard() {\n  return true;\n}\n"
            "export default async function guard() {\n  return false;\n}\n"
        ),
    )
    # Comment-only / unrelated appends cannot supersede the salvage binding.
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n# FEATURE_ENABLED = False\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nother = 1\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="# enable_guard()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard()",
        head_blob="x_enable_guard()",
    )
    # Mid-file whole-line occurrence inside disabling wrappers must fail closed
    # even though the salvage bytes remain line-boundary-aligned
    # (PRRT_kwDOSJAM6s6ZpQKt).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#if 0\ncheck();\n#endif\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/*\ncheck();\n*/\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='"""\ncheck();\n"""\n',
    )
    # Prepended *unterminated* wrappers still leave a line-aligned suffix; that
    # must fail closed or a no-change FIXED reuses stale evidence
    # (PRRT_kwDOSJAM6s6ZpaIn).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='"""\ncheck();\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="'''\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#if 0\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#ifdef FEATURE\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#ifndef FEATURE\ncheck();\n",
    )
    # Hash-line bodies must still scan for trailing ``/*``; otherwise
    # ``#endif /*`` / ``#define X /*`` leave block-comment state closed and a
    # later no-change FIXED reuses disabled suffix evidence
    # (PRRT_kwDOSJAM6s6ZpdMC).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#endif /*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#define X /*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#if 0\n#endif /*\ncheck();\n",
    )
    # Closing ``*/`` must not clear line-start across a same-line ``#if`` after
    # a multi-line comment ends (PRRT_kwDOSJAM6s6ZpdMC).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/*\n*/ #if 0\ncheck();\n",
    )
    # Closed wrappers before the salvage suffix are fine (benign prepend region).
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/* note */\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#endif /* note */\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/*\nnote\n*/\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='"""doc"""\ncheck();\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="'''doc'''\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#if 0\n#endif\ncheck();\n",
    )
    # ``#iffy`` is not a preprocessor ``#if``; treat as a benign prefix line.
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#iffy\ncheck();\n",
    )
    # Empty-file addition salvage: only an exact empty tip blob retains it.
    # Vacuous ``"" in head`` / early-True would accept an overwrite and let a
    # later no-change FIXED retry reuse stale evidence (PRRT_kwDOSJAM6s6ZpEZh).
    assert _added_salvage_blob_retained(commit_blob="", head_blob="")
    assert not _added_salvage_blob_retained(commit_blob="", head_blob="anything\n")


@pytest.mark.unit
def test_tip_extra_can_supersede_modified_salvage_rebinding() -> None:
    """Baseline tips that append a rebinding of a salvage-changed name fail closed.

    ``git merge-file`` can cleanly reproduce a descendant that keeps
    ``FEATURE_ENABLED = True`` and appends ``FEATURE_ENABLED = False`` when
    surrounding context exists; equality with HEAD would then falsely retain
    salvage. Only names whose last binding line changed vs parent count, so
    unrelated appends / later hunks stay retained (PRRT_kwDOSJAM6s6Zp_3j).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _salvage_changed_binding_names,
        _tip_extra_can_supersede_modified_salvage,
    )

    parent = "x = 1\nFEATURE_ENABLED = False\ny = 2\n"
    commit = "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
    assert _salvage_changed_binding_names(parent_blob=parent, commit_blob=commit) == {
        "FEATURE_ENABLED"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nFEATURE_ENABLED = False\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 3\nFEATURE_ENABLED = False\n"),
    )
    # YAML-style key rebinds must supersede like equals-style assignments
    # (PRRT_kwDOSJAM6s6ZqNAk).
    parent_yaml = "x: 1\nfeature_enabled: false\ny: 2\n"
    commit_yaml = "x: 1\nfeature_enabled: true\ny: 2\n"
    assert _salvage_changed_binding_names(parent_blob=parent_yaml, commit_blob=commit_yaml) == {
        "feature_enabled"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_yaml,
        commit_blob=commit_yaml,
        head_blob="x: 1\nfeature_enabled: true\ny: 2\nfeature_enabled: false\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_yaml,
        commit_blob=commit_yaml,
        head_blob="x: 1\nfeature_enabled: true\ny: 2\nother_key: 1\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_yaml,
        commit_blob=commit_yaml,
        head_blob="x: 1\nfeature_enabled: true\ny: 2\n# feature_enabled: false\n",
    )
    # Nested YAML leaves under different parents must not collide as bare
    # ``enabled``. Salvage of ``feature.enabled`` plus a tip that adds
    # ``logging.enabled`` still merge-file-matches HEAD; unqualified keys would
    # discard salvage and leave a later FIXED retry as fixed_without_head_advance
    # (PRRT_kwDOSJAM6s6ZqZo2).
    parent_nested_yaml = "feature:\n  enabled: false\nlogging:\n  level: info\n"
    commit_nested_yaml = "feature:\n  enabled: true\nlogging:\n  level: info\n"
    nested_yaml_changed = _salvage_changed_binding_names(
        parent_blob=parent_nested_yaml, commit_blob=commit_nested_yaml
    )
    assert "feature.enabled" in nested_yaml_changed
    assert "enabled" not in nested_yaml_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_nested_yaml,
        commit_blob=commit_nested_yaml,
        head_blob=("feature:\n  enabled: true\nlogging:\n  level: info\n  enabled: false\n"),
    )
    # Same-parent tip rebind of the salvaged nested leaf still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_nested_yaml,
        commit_blob=commit_nested_yaml,
        head_blob=("feature:\n  enabled: true\n  enabled: false\nlogging:\n  level: info\n"),
    )
    # Quoted mapping openers with empty values nest the same way.
    parent_quoted_nested = '"feature":\n  enabled: false\n"logging":\n  level: info\n'
    commit_quoted_nested = '"feature":\n  enabled: true\n"logging":\n  level: info\n'
    quoted_nested_changed = _salvage_changed_binding_names(
        parent_blob=parent_quoted_nested, commit_blob=commit_quoted_nested
    )
    assert "feature.enabled" in quoted_nested_changed
    assert "enabled" not in quoted_nested_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_quoted_nested,
        commit_blob=commit_quoted_nested,
        head_blob=('"feature":\n  enabled: true\n"logging":\n  level: info\n  enabled: false\n'),
    )
    # Block-sequence mapping entries (``- enabled:``) must bind like nested
    # leaves. Without recognizing the sequence-item key, salvage only records
    # the enclosing ``feature`` span while a tip that appends ``enabled: false``
    # on the same item is keyed ``feature.enabled`` — empty intersection would
    # retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZqeWt).
    parent_seq_yaml = "feature:\n  - enabled: false\n"
    commit_seq_yaml = "feature:\n  - enabled: true\n"
    seq_yaml_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_yaml, commit_blob=commit_seq_yaml
    )
    assert "feature.enabled" in seq_yaml_changed
    assert "enabled" not in seq_yaml_changed
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_yaml,
        commit_blob=commit_seq_yaml,
        head_blob="feature:\n  - enabled: true\n    enabled: false\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_yaml,
        commit_blob=commit_seq_yaml,
        head_blob="feature:\n  - enabled: true\n    other: 1\n",
    )
    # Quoted keys after a sequence marker nest/rebind the same way.
    parent_seq_quoted = 'feature:\n  - "enabled": false\n'
    commit_seq_quoted = 'feature:\n  - "enabled": true\n'
    seq_quoted_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_quoted, commit_blob=commit_seq_quoted
    )
    assert "feature.enabled" in seq_quoted_changed
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_quoted,
        commit_blob=commit_seq_quoted,
        head_blob='feature:\n  - "enabled": true\n    enabled: false\n',
    )
    # Sequence-item mapping openers (no same-line scalar) still qualify nested
    # leaves under the item key.
    parent_seq_nested = "feature:\n  - nested:\n      enabled: false\n"
    commit_seq_nested = "feature:\n  - nested:\n      enabled: true\n"
    seq_nested_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_nested, commit_blob=commit_seq_nested
    )
    assert "feature.nested.enabled" in seq_nested_changed
    assert "enabled" not in seq_nested_changed
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_nested,
        commit_blob=commit_seq_nested,
        head_blob=("feature:\n  - nested:\n      enabled: true\n      enabled: false\n"),
    )
    # Bare Python control-flow headers (``else:`` / ``try:`` / ``except:`` /
    # ``finally:``) must not open YAML mapping scopes. Treating them as parents
    # qualifies tip rebinds as ``else.FEATURE_ENABLED`` so they miss the
    # salvage-changed bare key and keep stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zqeen). Quoted ``"else":`` remains a real YAML opener.
    parent_cf = "FEATURE_ENABLED = False\n"
    commit_cf = "FEATURE_ENABLED = True\n"
    for header in ("else:", "try:", "except:", "finally:"):
        assert _tip_extra_can_supersede_modified_salvage(
            parent_blob=parent_cf,
            commit_blob=commit_cf,
            head_blob=(
                f"FEATURE_ENABLED = True\nif cond:\n    pass\n{header}\n"
                "    FEATURE_ENABLED = False\n"
            ),
        )
    parent_quoted_else = '"else":\n  enabled: false\nfeature:\n  level: info\n'
    commit_quoted_else = '"else":\n  enabled: true\nfeature:\n  level: info\n'
    quoted_else_changed = _salvage_changed_binding_names(
        parent_blob=parent_quoted_else, commit_blob=commit_quoted_else
    )
    assert "else.enabled" in quoted_else_changed
    assert "enabled" not in quoted_else_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_quoted_else,
        commit_blob=commit_quoted_else,
        head_blob=('"else":\n  enabled: true\nfeature:\n  level: info\n  enabled: false\n'),
    )
    # Quoted JSON mapping keys must supersede the same way; otherwise a tip that
    # keeps salvage `"feature-enabled": true` and appends a later duplicate
    # false cleanly merge-file-matches HEAD while consumers take the final
    # false (PRRT_kwDOSJAM6s6ZqQfh). Keep the salvage key line byte-identical so
    # only the appended duplicate is tip-extra (trailing commas would retarget
    # the salvage line itself).
    parent_json = '{\n  "feature-enabled": false\n}\n'
    commit_json = '{\n  "feature-enabled": true\n}\n'
    assert _salvage_changed_binding_names(parent_blob=parent_json, commit_blob=commit_json) == {
        "feature-enabled"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json,
        commit_blob=commit_json,
        head_blob=('{\n  "feature-enabled": true\n  "other": 1\n  "feature-enabled": false\n}\n'),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json,
        commit_blob=commit_json,
        head_blob=('{\n  "feature-enabled": true\n}\n"feature-enabled": false\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json,
        commit_blob=commit_json,
        head_blob=('{\n  "feature-enabled": true\n  "other": 1\n}\n'),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json,
        commit_blob=commit_json,
        head_blob=('{\n  "feature-enabled": true\n}\n# "feature-enabled": false\n'),
    )
    # TOML bare / quoted keys with ``=`` and hyphens must supersede like JSON
    # quoted ``:`` keys (PRRT_kwDOSJAM6s6Zqip3).
    parent_toml = "feature-enabled = false\n"
    commit_toml = "feature-enabled = true\n"
    assert _salvage_changed_binding_names(parent_blob=parent_toml, commit_blob=commit_toml) == {
        "feature-enabled"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml,
        commit_blob=commit_toml,
        head_blob="feature-enabled = true\nfeature-enabled = false\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml,
        commit_blob=commit_toml,
        head_blob="feature-enabled = true\nother = 1\nfeature-enabled = false\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml,
        commit_blob=commit_toml,
        head_blob="feature-enabled = true\nother-key = 1\n",
    )
    parent_toml_q = '"feature-enabled" = false\n'
    commit_toml_q = '"feature-enabled" = true\n'
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_q,
        commit_blob=commit_toml_q,
        head_blob='"feature-enabled" = true\n"feature-enabled" = false\n',
    )
    parent_json_sq = "{\n  'feature-enabled': false\n}\n"
    commit_json_sq = "{\n  'feature-enabled': true\n}\n"
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_json_sq,
        commit_blob=commit_json_sq,
        head_blob=("{\n  'feature-enabled': true\n  'other': 1\n  'feature-enabled': false\n}\n"),
    )
    # Tip-extra Google-style docstring prose must not supersede a real salvage
    # assignment rebind (PRRT_kwDOSJAM6s6ZqPO9).
    parent_timeout = "x = 1\ntimeout = 10\ny = 2\n"
    commit_timeout = "x = 1\ntimeout = 30\ny = 2\n"
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_timeout,
        commit_blob=commit_timeout,
        head_blob=(
            "x = 1\ntimeout = 30\ny = 2\n"
            '"""\n'
            "Args:\n"
            "    timeout: Seconds until the request fails.\n"
            '"""\n'
        ),
    )
    # Tip-extra rebind after a URL/glob/`#`/`//` line that embeds ``/*`` must
    # still supersede; false openers used to skip the rebind (PRRT_kwDOSJAM6s6ZqSbO).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
            'url = "https://example.com/*/path"\n'
            "FEATURE_ENABLED = False\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "x = 1\nFEATURE_ENABLED = True\ny = 2\n"
            "# see https://example.com/*/docs\n"
            "FEATURE_ENABLED = False\n"
        ),
    )
    # Unrelated append / later hunk must not look like supersession.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nother = 1\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 3\n",
    )
    # Comment-only append cannot supersede.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\n# FEATURE_ENABLED = False\n",
    )
    # Rebinding an unchanged name (x) must not reject.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nx = 9\n",
    )
    # Surplus copies of salvage assignment text in an unrelated later hunk must
    # not look like supersession. Full-line multiset would mark the duplicate
    # ``FEATURE_ENABLED = True`` as tip-only and drop still-valid FIXED evidence;
    # assignment rebinds already change line text, so set difference is enough
    # (PRRT_kwDOSJAM6s6ZqGeU).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nFEATURE_ENABLED = True\n",
    )
    parent_indented = "class C:\n    FEATURE_ENABLED = False\n"
    commit_indented = "class C:\n    FEATURE_ENABLED = True\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_indented, commit_blob=commit_indented
    ) == {"C", "C.FEATURE_ENABLED"}
    # Same indented assignment text reused in a later local hunk — identical line
    # text, so only full-line multiset would treat the surplus copy as tip-extra.
    # Enclosing class ``C`` is also in ``changed`` because the class span body
    # changed, but tip extras here bind only ``helper`` (PRRT_kwDOSJAM6s6ZqGeU).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_indented,
        commit_blob=commit_indented,
        head_blob=(
            "class C:\n    FEATURE_ENABLED = True\ndef helper():\n    FEATURE_ENABLED = True\n"
        ),
    )
    # Same-signature redefinition reuses the salvage opener line text. A set
    # difference of tip vs salvage lines drops that duplicate opener from
    # tip-only extras, so the append looks non-superseding while merge-file
    # still matches HEAD — unlike the added-blob path, which keeps the literal
    # suffix. Multiset applies only to declaration openers (PRRT_kwDOSJAM6s6ZqDij).
    parent_def = "x = 1\n"
    commit_def = "x = 1\ndef guard():\n    return True\n"
    head_redef = "x = 1\ndef guard():\n    return True\ndef guard():\n    return False\n"
    assert _salvage_changed_binding_names(parent_blob=parent_def, commit_blob=commit_def) == {
        "guard"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_def,
        commit_blob=commit_def,
        head_blob=head_redef,
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\n",
        commit_blob="x = 1\nclass Guard:\n    ok = True\n",
        head_blob=("x = 1\nclass Guard:\n    ok = True\nclass Guard:\n    ok = False\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\n",
        commit_blob="x = 1\nfunction guard() {\n  return true;\n}\n",
        head_blob=(
            "x = 1\nfunction guard() {\n  return true;\n}\nfunction guard() {\n  return false;\n}\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\n",
        commit_blob="x = 1\nconst guard = true;\n",
        head_blob="x = 1\nconst guard = true;\nconst guard = false;\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\n",
        commit_blob="x = 1\n#define GUARD 1\n",
        head_blob="x = 1\n#define GUARD 1\n#define GUARD 0\n",
    )
    # Body-only salvage of an existing declaration keeps the same opener line.
    # Comparing opener text alone would omit the name from ``changed``, so a tip
    # that appends a same-signature redefinition would retain stale FIXED
    # evidence after a clean merge-file match (PRRT_kwDOSJAM6s6ZqHvh).
    parent_body = "x = 1\ndef guard():\n    return False\n"
    commit_body = "x = 1\ndef guard():\n    return True\n"
    head_body_redef = "x = 1\ndef guard():\n    return True\ndef guard():\n    return False\n"
    assert _salvage_changed_binding_names(parent_blob=parent_body, commit_blob=commit_body) == {
        "guard"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_body,
        commit_blob=commit_body,
        head_blob=head_body_redef,
    )
    parent_class = "x = 1\nclass Guard:\n    def ok(self):\n        return False\n"
    commit_class = "x = 1\nclass Guard:\n    def ok(self):\n        return True\n"
    class_changed = _salvage_changed_binding_names(
        parent_blob=parent_class, commit_blob=commit_class
    )
    assert "Guard" in class_changed
    assert "Guard.ok" in class_changed
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_class,
        commit_blob=commit_class,
        head_blob=(
            "x = 1\nclass Guard:\n    def ok(self):\n        return True\n"
            "class Guard:\n    def ok(self):\n        return False\n"
        ),
    )
    # Body-only salvage of ``A.ok`` must not treat an unrelated later ``C.ok``
    # opener as a tip-extra rebind. Flat file-global names + opener multiset
    # wrongly put bare ``ok`` in ``changed`` and counted the surplus method as
    # supersession, dropping still-valid FIXED evidence (PRRT_kwDOSJAM6s6ZqKN3).
    parent_scoped = (
        "class A:\n"
        "    def ok(self):\n"
        "        return False\n"
        "class B:\n"
        "    def other(self):\n"
        "        return 1\n"
    )
    commit_scoped = (
        "class A:\n"
        "    def ok(self):\n"
        "        return True\n"
        "class B:\n"
        "    def other(self):\n"
        "        return 1\n"
    )
    scoped_changed = _salvage_changed_binding_names(
        parent_blob=parent_scoped, commit_blob=commit_scoped
    )
    assert "A.ok" in scoped_changed
    assert "ok" not in scoped_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_scoped,
        commit_blob=commit_scoped,
        head_blob=(commit_scoped + "class C:\n    def ok(self):\n        return False\n"),
    )
    # Same-class tip-extra redefinition of the salvaged method still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_scoped,
        commit_blob=commit_scoped,
        head_blob=(
            "class A:\n"
            "    def ok(self):\n"
            "        return True\n"
            "    def ok(self):\n"
            "        return False\n"
            "class B:\n"
            "    def other(self):\n"
            "        return 1\n"
        ),
    )
    assert _salvage_changed_binding_names(
        parent_blob="x = 1\nfunction guard() {\n  return false;\n}\n",
        commit_blob="x = 1\nfunction guard() {\n  return true;\n}\n",
    ) == {"guard"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="x = 1\nfunction guard() {\n  return false;\n}\n",
        commit_blob="x = 1\nfunction guard() {\n  return true;\n}\n",
        head_blob=(
            "x = 1\nfunction guard() {\n  return true;\n}\nfunction guard() {\n  return false;\n}\n"
        ),
    )
    # Unchanged body must not mark the binding changed.
    assert _salvage_changed_binding_names(parent_blob=commit_body, commit_blob=commit_body) == set()
    # Comment / non-directive hash lines are not declaration openers; they must
    # not flip tip-extra multiset accounting (PRRT_kwDOSJAM6s6ZqGeU).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=("x = 1\nFEATURE_ENABLED = True\ny = 2\n// def guard():\n# not-a-define\n"),
    )
    # No salvage binding change / exact tip match → no supersession.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=commit,
        commit_blob=commit,
        head_blob=commit + "other = 1\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=commit,
    )
    # Salvage that deletes a parent binding must still mark the name changed.
    # Iterating only commit spans would omit it; a tip that reintroduces the
    # binding after unrelated content can then cleanly merge-file-match HEAD
    # while falsely retaining FIXED evidence (PRRT_kwDOSJAM6s6ZqKGY).
    parent_deleted = "x = 1\nFEATURE_ENABLED = False\ny = 2\n"
    commit_deleted = "x = 1\ny = 2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_deleted, commit_blob=commit_deleted
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_deleted,
        commit_blob=commit_deleted,
        head_blob="x = 1\ny = 2\nFEATURE_ENABLED = False\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_deleted,
        commit_blob=commit_deleted,
        head_blob="x = 1\ny = 2\nother = 1\n",
    )


@pytest.mark.unit
def test_bytes_unsafe_for_text_merge_distinguishes_intentional_fffd() -> None:
    """Strict UTF-8 / NUL gate must allow intentional U+FFFD, reject invalid bytes."""
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _bytes_unsafe_for_text_merge,
        _merge_file_result_matches_head,
        _raw_blob_from_cat_file_result,
    )

    intentional = "keep\ufffdsafe\n".encode("utf-8")
    assert not _bytes_unsafe_for_text_merge(intentional)
    assert not _bytes_unsafe_for_text_merge(b"plain ascii\n")
    assert _bytes_unsafe_for_text_merge(b"has\0nul\n")
    assert _bytes_unsafe_for_text_merge(b"bad-\xff\n")

    assert _raw_blob_from_cat_file_result(ok=False, stdout="", stdout_bytes=None) is None
    assert (
        _raw_blob_from_cat_file_result(ok=True, stdout="ignored", stdout_bytes=intentional)
        == intentional
    )
    assert (
        _raw_blob_from_cat_file_result(ok=True, stdout="plain\n", stdout_bytes=None) == b"plain\n"
    )
    assert _raw_blob_from_cat_file_result(ok=True, stdout="has\ufffd", stdout_bytes=None) is None
    assert _raw_blob_from_cat_file_result(ok=True, stdout="has\0", stdout_bytes=None) is None

    assert _merge_file_result_matches_head(
        head_raw=intentional, stdout="ignored", stdout_bytes=intentional
    )
    assert _merge_file_result_matches_head(head_raw=b"plain\n", stdout="plain\n", stdout_bytes=None)
    assert not _merge_file_result_matches_head(
        head_raw=b"plain\n", stdout="other\n", stdout_bytes=None
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_commented_out_addition(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Later tip that comments out an added salvage call must fail closed.

    Salvage adds ``enable_guard()``. A subsequent edit to ``# enable_guard()``
    still contains the salvage bytes mid-line; substring retention would reuse
    stale evidence on a no-change FIXED retry (PRRT_kwDOSJAM6s6Zm6F1).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.py").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.py")
    _git(repo, "commit", "-qm", "base without new file")

    (repo / "guard.py").write_text("enable_guard()\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "salvage adds enable_guard")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "guard.py").write_text("# enable_guard()\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "later tip comments out addition")
    commented = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: append after the added call keeps a line-aligned salvage block.
    _git(repo, "checkout", "-q", "-B", "append-tip", salvage)
    (repo / "guard.py").write_text("enable_guard()\nextra()\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "later tip appends after addition")
    appended = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=appended,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=commented,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_disabled_wrapper_addition(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Later tip that wraps an added salvage call in ``#if 0`` must fail closed.

    Salvage adds ``check();``. A descendant that keeps the whole line but nests
    it under ``#if 0`` / ``#endif`` still satisfies line-boundary substring
    retention; that must not reuse stale evidence on a no-change FIXED retry
    (PRRT_kwDOSJAM6s6ZpQKt).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.py").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.py")
    _git(repo, "commit", "-qm", "base without new file")

    (repo / "guard.py").write_text("check();\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "salvage adds check")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "guard.py").write_text("#if 0\ncheck();\n#endif\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "later tip disables addition under if 0")
    disabled = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: append after the added call keeps a prefix-aligned salvage block.
    _git(repo, "checkout", "-q", "-B", "append-tip", salvage)
    (repo / "guard.py").write_text("check();\nextra();\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "later tip appends after addition")
    appended = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=appended,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=disabled,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_open_disabling_wrapper_prepend(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Prepended unterminated ``/*`` must not retain added salvage as a suffix.

    Salvage adds ``check();``. A descendant that prepends an open block comment
    keeps the salvage bytes as a line-aligned suffix while disabling the call;
    suffix retention must fail closed (PRRT_kwDOSJAM6s6ZpaIn).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.py").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.py")
    _git(repo, "commit", "-qm", "base without new file")

    (repo / "guard.py").write_text("check();\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "salvage adds check")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "guard.py").write_text("/*\ncheck();\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "later tip opens block comment before addition")
    open_wrapped = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: benign prepend keeps salvage as an active suffix.
    _git(repo, "checkout", "-q", "-B", "prepend-tip", salvage)
    (repo / "guard.py").write_text("header\ncheck();\n", encoding="utf-8")
    _git(repo, "add", "guard.py")
    _git(repo, "commit", "-qm", "later tip prepends header")
    prepended = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=prepended,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=open_wrapped,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_intentional_fffd_later_hunk(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Intentional U+FFFD in valid UTF-8 must not block same-file later-hunk retention.

    Gating on the decoded replacement character rejects legitimate ``\\ufffd``
    source bytes as if they were ``decode(errors="replace")`` artifacts, so a
    later tip that edits another hunk while keeping the salvage fix falsely
    fails closed (PRRT_kwDOSJAM6s6ZnK_D).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    fffd = "\ufffd"
    (repo / "a.py").write_text(
        f"line1{fffd}keep\nline2\nline3-middle\nline4\nline5-other\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base with intentional replacement char")
    (repo / "a.py").write_text(
        f"line1{fffd}keep\nline2\nline3-salvaged\nline4\nline5-other\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "salvage middle hunk")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.py").write_text(
        f"line1{fffd}keep\nline2\nline3-salvaged\nline4\nline5-later\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "later tip different hunk")
    later_hunk = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.py").write_text(
        f"line1{fffd}keep\nline2\nline3-third\nline4\nline5-later\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "overwrite salvaged hunk")
    third_content = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=later_hunk,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=third_content,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_invalid_utf8_replace_collapse(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Distinct invalid-UTF-8 blobs must not retain salvage via U+FFFD collapse.

    ``AsyncCommandRunner`` decodes cat-file with ``errors="replace"``. Parent,
    salvage, overwrite, and revert blobs that differ only in invalid bytes all
    become the same replacement-character text, so merge-file would falsely prove
    retention unless invalid UTF-8 raw bytes fail closed (exact OID only).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")

    # Three distinct invalid sequences that all decode to the same U+FFFD text.
    (repo / "blob.bin").write_bytes(b"payload-\xff\n")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "base invalid utf-8")
    (repo / "blob.bin").write_bytes(b"payload-\xfe\n")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "salvage different invalid byte")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "blob.bin").write_bytes(b"payload-\xfd\n")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "overwrite with third invalid byte")
    third_invalid = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-B", "revert-invalid", salvage)
    (repo / "blob.bin").write_bytes(b"payload-\xff\n")
    _git(repo, "add", "blob.bin")
    _git(repo, "commit", "-qm", "revert to parent invalid bytes")
    reverted_invalid = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: exact salvage OID at HEAD still retains (early OID equality).
    _git(repo, "checkout", "-q", "-B", "exact-salvage", salvage)
    (repo / "other.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "unrelated while salvage OID preserved")
    preserved_oid = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved_oid,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=third_invalid,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=reverted_invalid,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_newline_pathname_overwrite(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Newline pathnames must use -z bytes; C-quoted spellings must not retain salvage.

    Without ``diff-tree -z``, Git emits a C-quoted path for names containing a
    newline. ``splitlines()`` feeds that spelling to ``ls-tree``; both lookups
    return empty and compare equal, so a later overwrite/revert falsely looks
    present (PRRT_kwDOSJAM6s6ZmCZz).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-qm", "base")

    weird_name = "weird\nname.txt"
    (repo / weird_name).write_text("salvaged\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "salvage newline pathname")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: later tip keeps the salvage entry while adding an unrelated path.
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "unrelated while salvage preserved")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Overwrite/remove the newline pathname so salvage content is gone.
    _git(repo, "rm", "-f", "--", weird_name)
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "remove weird pathname")
    removed = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=removed,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_retains_invalid_utf8_pathname(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Invalid-UTF-8 pathnames must survive runner decode for salvage retention.

    ``AsyncioSubprocessRunner`` decodes ``diff-tree -z`` with ``errors="replace"``,
    so a legal Git pathname containing ``\\xff`` becomes a different U+FFFD spelling.
    ``ls-tree`` then misses every lookup and valid salvage is discarded
    (PRRT_kwDOSJAM6s6ZmviP). Path records must be taken from raw stdout bytes.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-qm", "base")

    weird_name = b"bad-\xff-name.txt"
    (repo / os.fsdecode(weird_name)).write_text("salvaged\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "salvage invalid utf-8 pathname"],
        check=True,
        capture_output=True,
    )
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "unrelated while invalid pathname salvage preserved")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    subprocess.run(
        ["git", "-C", str(repo), "--literal-pathspecs", "rm", "-f", "--", weird_name],
        check=True,
        capture_output=True,
    )
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "remove invalid utf-8 pathname")
    removed = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=removed,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_pathspec_magic_filename_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Diff-derived paths must use ``--literal-pathspecs`` for ``ls-tree``.

    A legal filename such as ``:(literal)foo`` is pathspec magic without the
    global option: ``ls-tree`` reads ``foo`` instead. After reverting the
    magic-named file while leaving ``foo`` unchanged, baseline/salvage/HEAD
    lookups all return ``foo``'s identical entry, so salvage falsely retains
    (PRRT_kwDOSJAM6s6ZmirW).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-qm", "base")

    magic_name = ":(literal)foo"
    (repo / magic_name).write_text("magic-salvage\n", encoding="utf-8")
    (repo / "foo").write_text("normal\n", encoding="utf-8")
    _git(repo, "--literal-pathspecs", "add", "-A")
    _git(repo, "commit", "-qm", "salvage magic pathname and foo")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: later tip keeps the magic path while adding an unrelated file.
    (repo / "later.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "unrelated while magic salvage preserved")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Revert only the magic-named path; leave ``foo`` byte-identical so a
    # non-literal ls-tree would still see matching foo entries.
    _git(repo, "--literal-pathspecs", "rm", "-f", "--", magic_name)
    (repo / "other.txt").write_text("other\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "revert magic pathname leave foo")
    reverted = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=reverted,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_preserved_deletion(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Deletion salvage must reuse when the tip still lacks the deleted path.

    A crashed fix that removed a file leaves an empty salvage tree entry. Later
    tips that preserve that absence must retain evidence; a tip that recreates
    the file must fail closed (PRRT_kwDOSJAM6s6ZmEAd).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / "gone.txt").write_text("delete-me\n", encoding="utf-8")
    _git(repo, "add", "keep.txt", "gone.txt")
    _git(repo, "commit", "-qm", "base with file to delete")
    _git(repo, "rm", "-q", "gone.txt")
    (repo / "keep.txt").write_text("keep-and-edit\n", encoding="utf-8")
    _git(repo, "add", "keep.txt")
    _git(repo, "commit", "-qm", "salvage deletes gone.txt and edits keep.txt")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "later.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "later tip preserving deletion")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "gone.txt").write_text("recreated\n", encoding="utf-8")
    _git(repo, "add", "gone.txt")
    _git(repo, "commit", "-qm", "undo deletion")
    recreated = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=recreated,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_accepts_preserved_newline_deletion(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """NUL-parsed newline pathnames must retain deletion salvage like plain paths.

    Salvage deletes a legal pathname containing a newline. A later tip that keeps
    the path absent must reuse evidence; recreating the file must fail closed
    (operator hint op_c7b81dcfeeda494596a261f7 / PRRT_kwDOSJAM6s6ZmEAd).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    weird_name = "weird\nname.txt"
    (repo / "keep.txt").write_text("keep\n", encoding="utf-8")
    (repo / weird_name).write_text("delete-me\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base with newline pathname")
    _git(repo, "rm", "-q", "--", weird_name)
    _git(repo, "commit", "-qm", "salvage deletes newline pathname")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "later.txt").write_text("unrelated\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "later tip preserving newline deletion")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / weird_name).write_text("recreated\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "re-add newline pathname")
    readded = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=readded,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_both_missing_tree_entries(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Two empty ls-tree tokens must not count as retained salvage evidence.

    Absence on the salvage tip is only a legitimate deletion when the parent
    still had the path. A bogus/C-quoted spelling that misses parent, salvage,
    and head alike must fail closed.
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    commit = "1" * 40
    head = "2" * 40
    commit_tree = "a" * 40
    head_tree = "b" * 40
    parent = "3" * 40
    parent_tree = "c" * 40
    # C-quoted-style path that will miss in both trees when looked up as-is.
    bogus_path = '"weird\\nname.txt"'

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{commit_tree}\n")  # commit^{tree}
    cmd.queue_result(returncode=0, stdout=f"{head_tree}\n")  # head^{tree}
    cmd.queue_result(returncode=0, stdout=f"{parent}\n")  # commit^
    cmd.queue_result(returncode=0, stdout=f"{parent_tree}\n")  # parent^{tree}
    cmd.queue_result(returncode=0, stdout=f"{bogus_path}\0")  # diff-tree -z
    cmd.queue_result(returncode=0, stdout="")  # ls-tree parent (missing)
    cmd.queue_result(returncode=0, stdout="")  # ls-tree commit (missing)
    cmd.queue_result(returncode=0, stdout="")  # ls-tree head (missing)

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit=commit,
        head=head,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_fail_closed_on_ls_tree_lookup_error(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Deletion salvage must not retain when HEAD ls-tree errors.

    Salvage deleted a path (parent entry present, salvage tip empty). Mapping a
    nonzero HEAD ``ls-tree`` to the same empty token as genuine absence would
    accept retained deletion even if the descendant re-added the file
    (PRRT_kwDOSJAM6s6ZoduB).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    commit = "1" * 40
    head = "2" * 40
    commit_tree = "a" * 40
    head_tree = "b" * 40
    parent = "3" * 40
    parent_tree = "c" * 40
    path = "gone.txt"
    parent_entry = f"100644 blob {'d' * 40}\t{path}"

    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout=f"{commit_tree}\n")  # commit^{tree}
    cmd.queue_result(returncode=0, stdout=f"{head_tree}\n")  # head^{tree}
    cmd.queue_result(returncode=0, stdout=f"{parent}\n")  # commit^
    cmd.queue_result(returncode=0, stdout=f"{parent_tree}\n")  # parent^{tree}
    cmd.queue_result(returncode=0, stdout=f"{path}\0")  # diff-tree -z
    cmd.queue_result(returncode=0, stdout=f"{parent_entry}\0")  # ls-tree parent
    cmd.queue_result(returncode=0, stdout="")  # ls-tree commit (deleted)
    cmd.queue_result(returncode=128, stdout="", stderr="fatal: not a tree object")

    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit=commit,
        head=head,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_symlink_kind_swap(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Content-only salvage must not retain when HEAD swaps file→symlink.

    Salvage writes pathname bytes into a regular file. A later tip replaces that
    file with a symlink to the same path: Git stores both as type blob with the
    same OID. Skipping mode equality for content-only salvage must still reject
    the kind change so a no-change FIXED retry cannot reuse stale evidence
    (PRRT_kwDOSJAM6s6Znm-O).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    path = repo / "linkish"
    path.write_text("other\n", encoding="utf-8")
    _git(repo, "add", "linkish")
    _git(repo, "commit", "-qm", "base regular file")
    path.write_text("target", encoding="utf-8")
    _git(repo, "add", "linkish")
    _git(repo, "commit", "-qm", "salvage content pathname")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "rm", "-q", "linkish")
    path.symlink_to("target")
    _git(repo, "add", "linkish")
    (repo / "unrelated.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "unrelated.txt")
    _git(repo, "commit", "-qm", "replace with symlink and add unrelated")
    kind_swapped = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: keep regular-file kind while adding an unrelated path — still present.
    _git(repo, "checkout", "-q", "-B", "kind-preserved", salvage)
    (repo / "other.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "unrelated while kind preserved")
    kind_preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: content-only salvage still tolerates same-kind chmod on a later tip.
    _git(repo, "checkout", "-q", "-B", "chmod-later", salvage)
    _git(repo, "update-index", "--chmod=+x", "linkish")
    (repo / "chmod-extra.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "linkish", "chmod-extra.txt")
    _git(repo, "commit", "-qm", "chmod +x while content retained")
    chmod_later = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=kind_preserved,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=chmod_later,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=kind_swapped,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_mode_only_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Mode/type are part of salvage evidence; blob OID alone must not retain it.

    Salvage only makes a script executable. A later tip reverts that mode while
    adding an unrelated path: parent/head trees differ (so the full-tree shortcut
    does not reject) and blob OIDs still match. Complete tree-entry comparison
    must fail closed (PRRT_kwDOSJAM6s6Zl_za).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    script = repo / "script.sh"
    script.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    _git(repo, "add", "script.sh")
    _git(repo, "commit", "-qm", "base non-executable")
    _git(repo, "update-index", "--chmod=+x", "script.sh")
    _git(repo, "commit", "-qm", "salvage make executable")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "update-index", "--chmod=-x", "script.sh")
    (repo / "unrelated.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "script.sh", "unrelated.txt")
    _git(repo, "commit", "-qm", "revert mode and add unrelated")
    mode_reverted = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: keep executable mode while adding an unrelated path — still present.
    _git(repo, "checkout", "-q", "-B", "mode-preserved", salvage)
    (repo / "other.txt").write_text("keep\n", encoding="utf-8")
    _git(repo, "add", "other.txt")
    _git(repo, "commit", "-qm", "unrelated while mode preserved")
    mode_preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=mode_preserved,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=mode_reverted,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_fail_closed_on_unresolved(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stdout="", stderr="missing")
    runner = make_runner(
        factory=factory,
        cmd=cmd,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit="1" * 40,
        head="2" * 40,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit="",
        head="2" * 40,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_fail_closed_on_empty_diff_or_missing_parent(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    worktree = tmp_path / "worktrees" / "workspace"
    _mark_git_worktree(worktree)
    commit = "1" * 40
    head = "2" * 40
    commit_tree = "a" * 40
    head_tree = "b" * 40

    # Distinct trees, but first-parent resolution fails → fail closed.
    missing_parent = FakeCommandRunner()
    missing_parent.queue_result(returncode=0, stdout=f"{commit_tree}\n")
    missing_parent.queue_result(returncode=0, stdout=f"{head_tree}\n")
    missing_parent.queue_result(returncode=1, stdout="", stderr="root")
    runner = make_runner(
        factory=factory,
        cmd=missing_parent,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit=commit,
        head=head,
    )

    # Parent resolves, trees differ, but diff-tree returns no paths → fail closed.
    empty_paths = FakeCommandRunner()
    empty_paths.queue_result(returncode=0, stdout=f"{commit_tree}\n")
    empty_paths.queue_result(returncode=0, stdout=f"{head_tree}\n")
    empty_paths.queue_result(returncode=0, stdout=f"{'3' * 40}\n")
    empty_paths.queue_result(returncode=0, stdout=f"{'c' * 40}\n")
    empty_paths.queue_result(returncode=0, stdout="\n")
    runner = make_runner(
        factory=factory,
        cmd=empty_paths,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit=commit,
        head=head,
    )

    # diff-tree itself fails → fail closed.
    diff_tree_fail = FakeCommandRunner()
    diff_tree_fail.queue_result(returncode=0, stdout=f"{commit_tree}\n")
    diff_tree_fail.queue_result(returncode=0, stdout=f"{head_tree}\n")
    diff_tree_fail.queue_result(returncode=0, stdout=f"{'3' * 40}\n")
    diff_tree_fail.queue_result(returncode=0, stdout=f"{'c' * 40}\n")
    diff_tree_fail.queue_result(returncode=1, stdout="", stderr="boom")
    runner = make_runner(
        factory=factory,
        cmd=diff_tree_fail,
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=worktree,
        commit=commit,
        head=head,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_earlier_multi_commit_revert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Multi-commit salvage must verify start..tip, not only tip^..tip.

    A failed run that creates H1 (review fix) then H2 (unrelated) retains H2. The
    first-parent delta is only H1..H2. A later tip that reverts H1 while preserving
    H2 must fail closed when ``baseline`` is the invocation start SHA
    (PRRT_kwDOSJAM6s6ZmG-B).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "a.txt").write_text("base-a\n", encoding="utf-8")
    (repo / "b.txt").write_text("base-b\n", encoding="utf-8")
    _git(repo, "add", "a.txt", "b.txt")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.txt").write_text("fix-a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "H1 review fix")
    h1 = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "b.txt").write_text("unrelated-b\n", encoding="utf-8")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "-qm", "H2 unrelated")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Later tip: revert H1's fix, keep H2's unrelated change.
    (repo / "a.txt").write_text("base-a\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "revert H1 keep H2")
    later = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # Control: later tip that preserves both H1 and H2 deltas.
    _git(repo, "checkout", "-q", "-B", "both-preserved", salvage)
    (repo / "c.txt").write_text("later\n", encoding="utf-8")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-qm", "preserve full start..salvage")
    preserved = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert h1 != salvage
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
        baseline=start,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=preserved,
        baseline=start,
    )
    # First-parent-only check would still see H2's b.txt and wrongly return True.
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=later,
        baseline=start,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_fail_closed_on_salvage_merge_tmpdir(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Merge-file retention must fail closed when temp-dir creation raises OSError.

    ``_salvage_entry_retained`` writes parent/ours/theirs blobs into a temporary
    directory for ``git merge-file``. Creation or write failures must return
    False rather than escaping ``_commit_changes_present_in_head`` and crashing
    FIXED evidence checking (PRRT_kwDOSJAM6s6ZoX2i).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation
    import awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence as fix_pass_presence

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "a.py").write_text(
        "line1\nline2\nline3-middle\nline4\nline5-other\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "base multi-line")
    (repo / "a.py").write_text(
        "line1\nline2\nline3-salvaged\nline4\nline5-other\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "salvage middle hunk")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "a.py").write_text(
        "line1\nline2\nline3-salvaged\nline4\nline5-later\n",
        encoding="utf-8",
    )
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-qm", "later tip different hunk")
    later_hunk = _git(repo, "rev-parse", "HEAD").stdout.strip()

    class _TemporaryDirectoryFailure:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            raise OSError("temporary directory unavailable")

    # tempfile lives on the presence helper module after the line-limit split.
    monkeypatch.setattr(
        fix_pass_presence.tempfile,
        "TemporaryDirectory",
        _TemporaryDirectoryFailure,
    )

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    # Without the OSError guard this raises and crashes FIXED evidence checking.
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=later_hunk,
    )


@pytest.mark.unit
async def test_commit_changes_present_in_head_rejects_baseline_appended_rebind(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Baseline-backed salvage must reject a tip that appends a disabling rebind.

    Salvage flips ``FEATURE_ENABLED`` False→True in a multi-line file. A later tip
    that keeps that line and appends ``FEATURE_ENABLED = False`` merges cleanly
    under ``git merge-file``, so equality-with-HEAD alone would retain stale
    FIXED evidence. Unrelated appends must still retain (PRRT_kwDOSJAM6s6Zp_3j).
    """
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")
    (repo / "flags.py").write_text(
        "x = 1\nFEATURE_ENABLED = False\ny = 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "flags.py")
    _git(repo, "commit", "-qm", "base feature disabled")
    (repo / "flags.py").write_text(
        "x = 1\nFEATURE_ENABLED = True\ny = 2\n",
        encoding="utf-8",
    )
    _git(repo, "add", "flags.py")
    _git(repo, "commit", "-qm", "salvage enables feature")
    salvage = _git(repo, "rev-parse", "HEAD").stdout.strip()

    (repo / "flags.py").write_text(
        "x = 1\nFEATURE_ENABLED = True\ny = 2\nFEATURE_ENABLED = False\n",
        encoding="utf-8",
    )
    _git(repo, "add", "flags.py")
    _git(repo, "commit", "-qm", "tip appends disabling rebind")
    rebound = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-B", "unrelated-append", salvage)
    (repo / "flags.py").write_text(
        "x = 1\nFEATURE_ENABLED = True\ny = 2\nother = 1\n",
        encoding="utf-8",
    )
    _git(repo, "add", "flags.py")
    _git(repo, "commit", "-qm", "tip unrelated append")
    unrelated = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=salvage,
    )
    assert await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=unrelated,
    )
    assert not await pre_push_validation._commit_changes_present_in_head(
        runner,
        worktree_path=repo,
        commit=salvage,
        head=rebound,
    )
