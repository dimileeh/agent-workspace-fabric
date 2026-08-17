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
    # Salvage that defines enable/disable helpers and ends with enable_guard()
    # must fail closed when a tip appends disable_guard(): call sites produce no
    # binding key, so assignment-only matching would retain stale FIXED evidence
    # (PRRT_kwDOSJAM6s6ZrJ3a).
    _guard_salvage = (
        "def enable_guard():\n    pass\ndef disable_guard():\n    pass\nenable_guard()\n"
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "disable_guard()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "disable_guard();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "await disable_guard()\n",
    )
    # Commented / unrelated call appends stay retained (behavior-neutral).
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "# disable_guard()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "// disable_guard()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "extra()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "note = 1\n",
    )
    # Tip-extra call inside a closed block comment is not executable → retain.
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "/*\ndisable_guard()\n*/\n",
    )
    # Same-line ``/* … */`` must also be non-executable (PRRT_kwDOSJAM6s6Zrhbs).
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "/* disable_guard() */\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "code; /* disable_guard() */\n",
    )
    # Scoped salvage defs: tip-extra bare call to the leaf still supersedes.
    _scoped_guard_salvage = (
        "class Guards:\n"
        "    def enable_guard(self):\n"
        "        pass\n"
        "    def disable_guard(self):\n"
        "        pass\n"
        "Guards().enable_guard()\n"
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_scoped_guard_salvage,
        head_blob=_scoped_guard_salvage + "disable_guard()\n",
    )
    # Tip member call on a different receiver sharing only the method leaf must
    # not match scoped ``Guards.disable_guard`` via unpaired leaf collision
    # (PRRT_kwDOSJAM6s6ZrWwo).
    assert _added_salvage_blob_retained(
        commit_blob=_scoped_guard_salvage,
        head_blob=_scoped_guard_salvage + "other.disable_guard()\n",
    )
    # Member-call overrides: tip ``guard.disable()`` must supersede salvage that
    # bound ``guard`` and called ``guard.enable()``. Bare identifier-then-``(``
    # matching misses dotted receivers/callees and would retain stale FIXED
    # evidence (PRRT_kwDOSJAM6s6ZrSYE).
    _member_guard_salvage = "guard = Guard()\nguard.enable()\n"
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "await guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard.disable();\n",
    )
    # Nested / mid-expression calls must supersede too: statement-leading-only
    # matching misses ``if ready: guard.disable()`` and ``result = guard.disable()``
    # and would retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZrYJk).
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "if ready: guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "result = guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "x = await guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "print(guard.disable())\n",
    )
    # Commented / string-only / unrelated member calls stay retained.
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "# guard.disable()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "code  # guard.disable()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'x = "guard.disable()"\n',
    )
    # Same-line block comments must not count as tip-extra calls
    # (PRRT_kwDOSJAM6s6Zrhbs).
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "/* guard.disable() */\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "code; /* guard.disable() */\n",
    )
    # Real call after a closed same-line block comment still supersedes.
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "/* note */ guard.disable()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "other.disable()\n",
    )
    # Appended rebinding of a salvage assignment must fail closed: the original
    # addition remains a line-aligned prefix, but the later assignment supersedes
    # it (PRRT_kwDOSJAM6s6Zp8jM).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED = False\n",
    )
    # Duplicate earlier ``False`` in the salvage blob must not hide an appended
    # override via set-membership tip-extra accounting (PRRT_kwDOSJAM6s6ZrFdv).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = False\nFEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = False\nFEATURE_ENABLED = True\nFEATURE_ENABLED = False\n"),
    )
    # Surplus identical assignment copy keeps last binding equal → retain.
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED = True\n",
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
    # Nested YAML leaves under different parents must not collide as bare
    # ``enabled`` on the added-file append path. Flat ``_binding_names`` would
    # intersect salvage ``feature.enabled`` with an unrelated ``logging.enabled``
    # append and discard still-valid FIXED evidence (PRRT_kwDOSJAM6s6Zq76q;
    # baseline-backed path already scopes via PRRT_kwDOSJAM6s6ZqZo2).
    assert _added_salvage_blob_retained(
        commit_blob="feature:\n  enabled: true\n",
        head_blob=("feature:\n  enabled: true\nlogging:\n  enabled: false\n"),
    )
    # Same-parent nested rebind under the salvage prefix still supersedes.
    assert not _added_salvage_blob_retained(
        commit_blob="feature:\n  enabled: true\n",
        head_blob=("feature:\n  enabled: true\n  enabled: false\n"),
    )
    # TOML table siblings: ``[feature] enabled`` vs ``[logging] enabled``.
    assert _added_salvage_blob_retained(
        commit_blob="[feature]\nenabled = true\n",
        head_blob=("[feature]\nenabled = true\n[logging]\nenabled = false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="[feature]\nenabled = true\n",
        head_blob=("[feature]\nenabled = true\nenabled = false\n"),
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
    # YAML/JSON ``:`` treats ``"a.b"`` and ``a.b`` as one key. Re-quoting
    # non-bare segments (correct for TOML ``=``) made quote-only tip rebinds
    # miss salvage names and retain superseded FIXED evidence
    # (PRRT_kwDOSJAM6s6ZqtHj).
    assert not _added_salvage_blob_retained(
        commit_blob='"a.b": true\n',
        head_blob='"a.b": true\na.b: false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="a.b: true\n",
        head_blob='a.b: true\n"a.b": false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="'a.b': true\n",
        head_blob="'a.b': true\na.b: false\n",
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
    # TOML dotted keys (`feature.enabled = true`) must bind the full path.
    # Identifier-only matching required `=` immediately after the first segment,
    # so neither salvage nor an appended `feature.enabled = false` bound and the
    # tip kept a line-aligned prefix / reused stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zql88). Quoted dotted segments normalize to the same key.
    assert not _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob="feature.enabled = true\nfeature.enabled = false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob=("feature.enabled = true\nother = 1\nfeature.enabled = false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob='feature.enabled = true\nfeature."enabled" = false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='"feature".enabled = true\n',
        head_blob='"feature".enabled = true\nfeature.enabled = false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='site."google.com" = true\n',
        head_blob='site."google.com" = true\nsite."google.com" = false\n',
    )
    # Quoted segments that contain dots must stay distinct from bare dotted
    # paths: site."google.com" ≠ site.google.com, and "a.b" ≠ a.b. Collapsing
    # them let tip extras look like rebinds and drop FIXED evidence
    # (PRRT_kwDOSJAM6s6ZqoYV).
    assert _added_salvage_blob_retained(
        commit_blob='site."google.com" = true\n',
        head_blob='site."google.com" = true\nsite.google.com = false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="site.google.com = true\n",
        head_blob='site.google.com = true\nsite."google.com" = false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='"a.b" = true\n',
        head_blob='"a.b" = true\na.b = false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="a.b = true\n",
        head_blob='a.b = true\n"a.b" = false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="'a.b' = true\n",
        head_blob="'a.b' = true\na.b = false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="a.b.c = 1\n",
        head_blob="a.b.c = 1\na.b.c = 2\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob="feature.enabled = true\n# feature.enabled = false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob="feature.enabled = true\nother.key = 1\n",
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
    # Shell ``export NAME=value`` must bind like bare assignments; otherwise an
    # appended ``export FEATURE_ENABLED=false`` keeps a line-aligned prefix and
    # reuses stale FIXED evidence (PRRT_kwDOSJAM6s6ZqseO).
    assert not _added_salvage_blob_retained(
        commit_blob="export FEATURE_ENABLED=true\n",
        head_blob="export FEATURE_ENABLED=true\nexport FEATURE_ENABLED=false\n",
    )
    # ``declare -x`` / ``typeset`` assignment forms must bind like ``export``;
    # otherwise a descendant rebind keeps a line-aligned prefix and reuses
    # stale FIXED evidence (PRRT_kwDOSJAM6s6ZqxX4).
    assert not _added_salvage_blob_retained(
        commit_blob="declare -x FEATURE_ENABLED=true\n",
        head_blob=("declare -x FEATURE_ENABLED=true\ndeclare -x FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="typeset -x FEATURE_ENABLED=true\n",
        head_blob=("typeset -x FEATURE_ENABLED=true\ntypeset -x FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="declare -rx FEATURE_ENABLED=true\n",
        head_blob=("declare -rx FEATURE_ENABLED=true\ndeclare -rx FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="declare FEATURE_ENABLED=true\n",
        head_blob="declare FEATURE_ENABLED=true\ndeclare FEATURE_ENABLED=false\n",
    )
    # ``readonly NAME=value`` (and flagged forms) must bind like declare/typeset;
    # otherwise an appended readonly rebind keeps a line-aligned prefix and
    # reuses stale FIXED evidence (PRRT_kwDOSJAM6s6ZrBJF).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nreadonly FEATURE_ENABLED=false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="readonly FEATURE_ENABLED=true\n",
        head_blob=("readonly FEATURE_ENABLED=true\nreadonly FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="readonly -x FEATURE_ENABLED=true\n",
        head_blob=("readonly -x FEATURE_ENABLED=true\nreadonly -x FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export FEATURE_ENABLED=true\n",
        head_blob=("export FEATURE_ENABLED=true\nreadonly FEATURE_ENABLED=false\n"),
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
    # Quoted / line-comment ``/*`` in a prepended prefix must not look like an
    # unterminated block comment or a still-valid salvage suffix is rejected and
    # a later no-change FIXED becomes fixed_without_head_advance
    # (PRRT_kwDOSJAM6s6Zq2m_).
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='const marker = "/*";\ncheck();\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="const marker = '/*';\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='const marker = "\\"/*";\ncheck();\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="// /*\ncheck();\n",
    )
    # After a closed ordinary string, a real open ``/*`` still disables.
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='const marker = "*/"; /*\ncheck();\n',
    )
    # Possessives / contractions / inch marks must not open ordinary-string
    # opacity, or a later real ``/*`` / ``#if`` in the prepended prefix is
    # ignored and suffix salvage is retained under a disabling region
    # (PRRT_kwDOSJAM6s6Zq7kr).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="user's note\n/*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="don't touch\n/*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='the 5" panel\n/*\ncheck();\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="it's fine\n#if 0\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="users' notes\n/*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="grab 'em\n/*\ncheck();\n",
    )
    # Same opener filter for binding-state scanning: a prose apostrophe must
    # not swallow a same-line ``/*`` that should hide a later rebind.
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nuser's note /*\nFEATURE_ENABLED = False\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob='FEATURE_ENABLED = True\npanel 5" /*\nFEATURE_ENABLED = False\n',
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
def test_tip_extra_can_supersede_modified_salvage_call_site_override() -> None:
    """Tip-extra calls must supersede call-only modified salvage flips.

    Salvage that only rewrites ``disable_guard()`` → ``enable_guard()`` changes
    no bindings, so a binding-only check retains stale FIXED evidence when a
    descendant appends ``disable_guard()`` and ``git merge-file`` still equals
    HEAD (PRRT_kwDOSJAM6s6ZrN5J). Mirrors added-file call-site fail-closed
    behavior (PRRT_kwDOSJAM6s6ZrJ3a).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _salvage_changed_binding_names,
        _tip_extra_can_supersede_modified_salvage,
    )

    parent = "x = 1\ndisable_guard()\ny = 2\n"
    commit = "x = 1\nenable_guard()\ny = 2\n"
    assert _salvage_changed_binding_names(parent_blob=parent, commit_blob=commit) == set()
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nenable_guard()\ny = 2\ndisable_guard()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nenable_guard()\ny = 2\nawait disable_guard()\n",
    )
    # Commented / unrelated tip-extra calls stay retained.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nenable_guard()\ny = 2\n# disable_guard()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nenable_guard()\ny = 2\nextra()\n",
    )
    # Same flip with helper defs present: bindings unchanged, tip restore
    # of disable_guard() must still supersede.
    parent_defs = "def enable_guard():\n    pass\ndef disable_guard():\n    pass\ndisable_guard()\n"
    commit_defs = "def enable_guard():\n    pass\ndef disable_guard():\n    pass\nenable_guard()\n"
    assert _salvage_changed_binding_names(parent_blob=parent_defs, commit_blob=commit_defs) == set()
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_defs,
        commit_blob=commit_defs,
        head_blob=commit_defs + "disable_guard()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_defs,
        commit_blob=commit_defs,
        head_blob=commit_defs + "# disable_guard()\n",
    )
    # Tip-extra call to an *unchanged* salvage helper must not supersede when
    # only a binding (or call) fix changed — unioning every commit binding as
    # call candidates would drop valid FIXED evidence (PRRT_kwDOSJAM6s6ZrR2e).
    parent_helper = "def helper():\n    pass\nFEATURE_ENABLED = False\n"
    commit_helper = "def helper():\n    pass\nFEATURE_ENABLED = True\n"
    assert _salvage_changed_binding_names(parent_blob=parent_helper, commit_blob=commit_helper) == {
        "FEATURE_ENABLED"
    }
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_helper,
        commit_blob=commit_helper,
        head_blob=commit_helper + "helper()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_helper,
        commit_blob=commit_helper,
        head_blob=commit_helper + "FEATURE_ENABLED = False\n",
    )
    # Member-call-only flips (``guard.disable()`` → ``guard.enable()``) must
    # supersede when a tip restores ``guard.disable()`` (PRRT_kwDOSJAM6s6ZrSYE).
    parent_member = "x = 1\nguard.disable()\ny = 2\n"
    commit_member = "x = 1\nguard.enable()\ny = 2\n"
    assert (
        _salvage_changed_binding_names(parent_blob=parent_member, commit_blob=commit_member)
        == set()
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "await guard.disable()\n",
    )
    # Nested / mid-expression restores must supersede (PRRT_kwDOSJAM6s6ZrYJk).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "if ready: guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "result = guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "print(guard.disable())\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "# guard.disable()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "code  # guard.disable()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'x = "guard.disable()"\n',
    )
    # Same-line ``/* … */`` tip extras are non-executable (PRRT_kwDOSJAM6s6Zrhbs).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "/* guard.disable() */\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "code; /* guard.disable() */\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "/* note */ guard.disable()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "other.noop()\n",
    )
    # Unrelated tip member call sharing only the method leaf must not supersede
    # (mirrors added-salvage retain for ``other.disable()``; PRRT_kwDOSJAM6s6ZrWwo).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "other.disable()\n",
    )


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
    # Shell ``export NAME=value`` rebinds must supersede like bare assignments
    # (PRRT_kwDOSJAM6s6ZqseO).
    parent_export = "x=1\nexport FEATURE_ENABLED=false\ny=2\n"
    commit_export = "x=1\nexport FEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(parent_blob=parent_export, commit_blob=commit_export) == {
        "FEATURE_ENABLED"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_export,
        commit_blob=commit_export,
        head_blob=("x=1\nexport FEATURE_ENABLED=true\ny=2\nexport FEATURE_ENABLED=false\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_export,
        commit_blob=commit_export,
        head_blob="x=1\nexport FEATURE_ENABLED=true\ny=2\nother=1\n",
    )
    # ``declare -x`` / ``typeset`` rebinds must supersede like ``export``
    # (PRRT_kwDOSJAM6s6ZqxX4).
    parent_declare = "x=1\ndeclare -x FEATURE_ENABLED=false\ny=2\n"
    commit_declare = "x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_declare, commit_blob=commit_declare
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_declare,
        commit_blob=commit_declare,
        head_blob=("x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\ndeclare -x FEATURE_ENABLED=false\n"),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_declare,
        commit_blob=commit_declare,
        head_blob="x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\nother=1\n",
    )
    parent_typeset = "x=1\ntypeset -x FEATURE_ENABLED=false\ny=2\n"
    commit_typeset = "x=1\ntypeset -x FEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_typeset, commit_blob=commit_typeset
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_typeset,
        commit_blob=commit_typeset,
        head_blob=("x=1\ntypeset -x FEATURE_ENABLED=true\ny=2\ntypeset -x FEATURE_ENABLED=false\n"),
    )
    # Mixed declare/export spellings of the same name still intersect.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_declare,
        commit_blob=commit_declare,
        head_blob=("x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\nexport FEATURE_ENABLED=false\n"),
    )
    # ``readonly`` rebinds must supersede like declare/export
    # (PRRT_kwDOSJAM6s6ZrBJF).
    parent_readonly = "x=1\nFEATURE_ENABLED=false\ny=2\n"
    commit_readonly = "x=1\nFEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_readonly, commit_blob=commit_readonly
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_readonly,
        commit_blob=commit_readonly,
        head_blob=("x=1\nFEATURE_ENABLED=true\ny=2\nreadonly FEATURE_ENABLED=false\n"),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_declare,
        commit_blob=commit_declare,
        head_blob=("x=1\ndeclare -x FEATURE_ENABLED=true\ny=2\nreadonly FEATURE_ENABLED=false\n"),
    )
    parent_readonly_decl = "x=1\nreadonly FEATURE_ENABLED=false\ny=2\n"
    commit_readonly_decl = "x=1\nreadonly FEATURE_ENABLED=true\ny=2\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_readonly_decl, commit_blob=commit_readonly_decl
    ) == {"FEATURE_ENABLED"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_readonly_decl,
        commit_blob=commit_readonly_decl,
        head_blob=("x=1\nreadonly FEATURE_ENABLED=true\ny=2\nreadonly FEATURE_ENABLED=false\n"),
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
    # retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZqeWt). Scalar identity
    # qualifies the inline leaf as ``feature.enabled.<value>`` so same-item
    # rebinds still intersect while sibling items stay distinct
    # (PRRT_kwDOSJAM6s6ZqxYE).
    parent_seq_yaml = "feature:\n  - enabled: false\n"
    commit_seq_yaml = "feature:\n  - enabled: true\n"
    seq_yaml_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_yaml, commit_blob=commit_seq_yaml
    )
    assert "feature.enabled.true" in seq_yaml_changed
    assert "feature.enabled.false" in seq_yaml_changed
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
    assert "feature.enabled.true" in seq_quoted_changed
    assert "feature.enabled.false" in seq_quoted_changed
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
    # Scalar sequence items (``- name: a``) must open an identity scope so a tip
    # sibling ``- name: b`` with its own ``enabled`` does not collide as
    # ``features.enabled`` and clear retained salvage (PRRT_kwDOSJAM6s6ZqxYE).
    parent_seq_sibling = "features:\n  - name: a\n    enabled: false\n"
    commit_seq_sibling = "features:\n  - name: a\n    enabled: true\n"
    sibling_changed = _salvage_changed_binding_names(
        parent_blob=parent_seq_sibling, commit_blob=commit_seq_sibling
    )
    assert "features.name.a.enabled" in sibling_changed
    assert "features.name.a" in sibling_changed
    assert "features.enabled" not in sibling_changed
    assert "features.name" not in sibling_changed
    assert "enabled" not in sibling_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_sibling,
        commit_blob=commit_seq_sibling,
        head_blob=("features:\n  - name: a\n    enabled: true\n  - name: b\n    enabled: false\n"),
    )
    # Same-identity tip rebind under the salvaged item still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_seq_sibling,
        commit_blob=commit_seq_sibling,
        head_blob=("features:\n  - name: a\n    enabled: true\n    enabled: false\n"),
    )
    # Bare hyphenated sequence-item keys (``- feature-name: a``) must open the
    # same identity scope as underscore keys; omitting ``-`` from the bare
    # class left siblings collapsing to ``features.enabled``
    # (PRRT_kwDOSJAM6s6Zq13_).
    parent_hyphen_seq = "features:\n  - feature-name: a\n    enabled: false\n"
    commit_hyphen_seq = "features:\n  - feature-name: a\n    enabled: true\n"
    hyphen_seq_changed = _salvage_changed_binding_names(
        parent_blob=parent_hyphen_seq, commit_blob=commit_hyphen_seq
    )
    assert "features.feature-name.a.enabled" in hyphen_seq_changed
    assert "features.feature-name.a" in hyphen_seq_changed
    assert "features.enabled" not in hyphen_seq_changed
    assert "enabled" not in hyphen_seq_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hyphen_seq,
        commit_blob=commit_hyphen_seq,
        head_blob=(
            "features:\n  - feature-name: a\n    enabled: true\n"
            "  - feature-name: b\n    enabled: false\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hyphen_seq,
        commit_blob=commit_hyphen_seq,
        head_blob=("features:\n  - feature-name: a\n    enabled: true\n    enabled: false\n"),
    )
    # Quoted scalars may contain ``#``; truncating at ``#`` would collapse
    # ``"a#1"`` / ``"a#2"`` to the same identity and falsely supersede salvage
    # when a sibling tip rebinds (PRRT_kwDOSJAM6s6Zq135).
    parent_hash_quoted = 'features:\n  - name: "a#1"\n    enabled: false\n'
    commit_hash_quoted = 'features:\n  - name: "a#1"\n    enabled: true\n'
    hash_quoted_changed = _salvage_changed_binding_names(
        parent_blob=parent_hash_quoted, commit_blob=commit_hash_quoted
    )
    assert "features.name.a#1.enabled" in hash_quoted_changed
    assert "features.name.a#1" in hash_quoted_changed
    assert 'features.name."a' not in hash_quoted_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hash_quoted,
        commit_blob=commit_hash_quoted,
        head_blob=(
            'features:\n  - name: "a#1"\n    enabled: true\n  - name: "a#2"\n    enabled: false\n'
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hash_quoted,
        commit_blob=commit_hash_quoted,
        head_blob=('features:\n  - name: "a#1"\n    enabled: true\n    enabled: false\n'),
    )
    parent_hash_single = "features:\n  - name: 'a#1'\n    enabled: false\n"
    commit_hash_single = "features:\n  - name: 'a#1'\n    enabled: true\n"
    assert "features.name.a#1.enabled" in _salvage_changed_binding_names(
        parent_blob=parent_hash_single, commit_blob=commit_hash_single
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_hash_single,
        commit_blob=commit_hash_single,
        head_blob=(
            "features:\n  - name: 'a#1'\n    enabled: true\n  - name: 'a#2'\n    enabled: false\n"
        ),
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
    # TOML table / array-table headers must qualify leaf keys so ``[feature]``
    # ``enabled`` and ``[logging]`` ``enabled`` do not collide as bare
    # ``enabled``. Salvage of ``feature.enabled`` plus a tip that adds
    # ``logging.enabled`` still merge-file-matches HEAD; unqualified keys would
    # discard salvage and leave a later FIXED retry as fixed_without_head_advance
    # (PRRT_kwDOSJAM6s6ZqpBC).
    parent_toml_table = '[feature]\nenabled = false\n[logging]\nlevel = "info"\n'
    commit_toml_table = '[feature]\nenabled = true\n[logging]\nlevel = "info"\n'
    toml_table_changed = _salvage_changed_binding_names(
        parent_blob=parent_toml_table, commit_blob=commit_toml_table
    )
    assert "feature.enabled" in toml_table_changed
    assert "enabled" not in toml_table_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_table,
        commit_blob=commit_toml_table,
        head_blob=('[feature]\nenabled = true\n[logging]\nlevel = "info"\nenabled = false\n'),
    )
    # Same-table tip rebind of the salvaged leaf still supersedes.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_table,
        commit_blob=commit_toml_table,
        head_blob=('[feature]\nenabled = true\nenabled = false\n[logging]\nlevel = "info"\n'),
    )
    # Array tables and dotted / quoted table paths qualify the same way.
    parent_arr = '[[feature]]\nenabled = false\n[[logging]]\nlevel = "info"\n'
    commit_arr = '[[feature]]\nenabled = true\n[[logging]]\nlevel = "info"\n'
    arr_changed = _salvage_changed_binding_names(parent_blob=parent_arr, commit_blob=commit_arr)
    assert "feature.enabled" in arr_changed
    assert "enabled" not in arr_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_arr,
        commit_blob=commit_arr,
        head_blob=('[[feature]]\nenabled = true\n[[logging]]\nlevel = "info"\nenabled = false\n'),
    )
    parent_dotted_table = '[feature.sub]\nenabled = false\n[logging]\nlevel = "info"\n'
    commit_dotted_table = '[feature.sub]\nenabled = true\n[logging]\nlevel = "info"\n'
    dotted_table_changed = _salvage_changed_binding_names(
        parent_blob=parent_dotted_table, commit_blob=commit_dotted_table
    )
    assert "feature.sub.enabled" in dotted_table_changed
    assert "enabled" not in dotted_table_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dotted_table,
        commit_blob=commit_dotted_table,
        head_blob=('[feature.sub]\nenabled = true\n[logging]\nlevel = "info"\nenabled = false\n'),
    )
    parent_quoted_table = '["feature"]\nenabled = false\n["logging"]\nlevel = "info"\n'
    commit_quoted_table = '["feature"]\nenabled = true\n["logging"]\nlevel = "info"\n'
    quoted_table_changed = _salvage_changed_binding_names(
        parent_blob=parent_quoted_table, commit_blob=commit_quoted_table
    )
    assert "feature.enabled" in quoted_table_changed
    assert "enabled" not in quoted_table_changed
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_quoted_table,
        commit_blob=commit_quoted_table,
        head_blob=('["feature"]\nenabled = true\n["logging"]\nlevel = "info"\nenabled = false\n'),
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
    # TOML dotted keys (incl. quoted segments) must supersede on the modified
    # salvage path the same way as added salvage (PRRT_kwDOSJAM6s6Zql88).
    parent_toml_dot = "feature.enabled = false\n"
    commit_toml_dot = "feature.enabled = true\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_toml_dot, commit_blob=commit_toml_dot
    ) == {"feature.enabled"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob="feature.enabled = true\nfeature.enabled = false\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob='feature.enabled = true\nfeature."enabled" = false\n',
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_toml_dot,
        commit_blob=commit_toml_dot,
        head_blob="feature.enabled = true\nother.key = 1\n",
    )
    # Distinct dotted / quoted-dot keys must not cross-supersede
    # (PRRT_kwDOSJAM6s6ZqoYV).
    parent_host = 'site."google.com" = false\n'
    commit_host = 'site."google.com" = true\n'
    assert _salvage_changed_binding_names(parent_blob=parent_host, commit_blob=commit_host) == {
        'site."google.com"'
    }
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_host,
        commit_blob=commit_host,
        head_blob='site."google.com" = true\nsite.google.com = false\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_host,
        commit_blob=commit_host,
        head_blob='site."google.com" = true\nsite."google.com" = false\n',
    )
    parent_ab = '"a.b" = false\n'
    commit_ab = '"a.b" = true\n'
    assert _salvage_changed_binding_names(parent_blob=parent_ab, commit_blob=commit_ab) == {'"a.b"'}
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_ab,
        commit_blob=commit_ab,
        head_blob='"a.b" = true\na.b = false\n',
    )
    # YAML ``:`` quote-only rebinds of dotted keys must supersede (unlike TOML
    # ``=`` where ``"a.b"`` ≠ ``a.b``; PRRT_kwDOSJAM6s6ZqtHj).
    parent_yaml_ab = '"a.b": false\n'
    commit_yaml_ab = '"a.b": true\n'
    assert _salvage_changed_binding_names(
        parent_blob=parent_yaml_ab, commit_blob=commit_yaml_ab
    ) == {"a.b"}
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_yaml_ab,
        commit_blob=commit_yaml_ab,
        head_blob='"a.b": true\na.b: false\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob="a.b: false\n",
        commit_blob="a.b: true\n",
        head_blob='a.b: true\n"a.b": false\n',
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
    # not look like supersession. Full-line multiset marks the duplicate
    # ``FEATURE_ENABLED = True`` as tip-only, but last-binding equality keeps
    # FIXED evidence (PRRT_kwDOSJAM6s6ZqGeU).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nFEATURE_ENABLED = True\ny = 2\nFEATURE_ENABLED = True\n",
    )
    # When salvage flips only the last of two identical ``False`` assignments
    # to ``True``, an appended third ``False`` must still supersede: set
    # membership would hide it behind the unchanged earlier copy
    # (PRRT_kwDOSJAM6s6ZrFdv).
    parent_dup = "FEATURE_ENABLED = False\nFEATURE_ENABLED = False\n"
    commit_dup = "FEATURE_ENABLED = False\nFEATURE_ENABLED = True\n"
    assert _salvage_changed_binding_names(parent_blob=parent_dup, commit_blob=commit_dup) == {
        "FEATURE_ENABLED"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dup,
        commit_blob=commit_dup,
        head_blob=("FEATURE_ENABLED = False\nFEATURE_ENABLED = True\nFEATURE_ENABLED = False\n"),
    )
    parent_indented = "class C:\n    FEATURE_ENABLED = False\n"
    commit_indented = "class C:\n    FEATURE_ENABLED = True\n"
    assert _salvage_changed_binding_names(
        parent_blob=parent_indented, commit_blob=commit_indented
    ) == {"C", "C.FEATURE_ENABLED"}
    # Same indented assignment text reused in a later local hunk — identical line
    # text makes a tip-extra multiset hit, but scoped tip keys bind ``helper`` /
    # last ``C.FEATURE_ENABLED`` stays equal so FIXED evidence retains
    # (PRRT_kwDOSJAM6s6ZqGeU).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_indented,
        commit_blob=commit_indented,
        head_blob=(
            "class C:\n    FEATURE_ENABLED = True\ndef helper():\n    FEATURE_ENABLED = True\n"
        ),
    )
    # Same-signature redefinition reuses the salvage opener line text. Tip-extra
    # multiset counting keeps the duplicate opener tip-only; last-binding span
    # then differs so the append supersedes (PRRT_kwDOSJAM6s6ZqDij).
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
    # Comment / non-directive hash lines are ordinary tip-extra text; they must
    # not look like binding supersession (PRRT_kwDOSJAM6s6ZqGeU).
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
