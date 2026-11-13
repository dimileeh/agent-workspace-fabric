"""Comment-named target FIXED evidence (issue #1232).

Unit tests for the correction attempt's fifth evidence gate: the reviewer's own
comment text names the fix target — a repo-relative path (optionally ``:line``)
or a backticked identifier — and the item's commit range must change that path
(near that line) or that identifier's definition. The probe stub answers the
Git reads the gate issues, so "named path changed", "named path untouched",
"definition changed" and "usage changed" are genuinely different inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from awf.runtime.pr_monitor import ReviewThread
from awf.runtime.pr_monitor_models import ReviewComment, ReviewThreadComment
from awf.runtime.pr_monitor_runner import (
    pre_push_validation_fix_pass_ancestry_comment_named as comment_named,
)
from tests.unit.runtime._pre_push_ancestry_cross_file_helpers import _LEFT, _RIGHT, _Probe

_DOC = "docs/runbooks/vm-core-update.md"
_ROUTES = "src/app/api/routes/team.py"
_INBOUND = "src/app/messaging/inbound_types.py"
_CHANNEL = "src/app/agents_management/channel_service.py"
_WEB = "src/app/web/team.ts"
_OTHER = "src/app/control/queue_sweeper.py"


def _diff(path: str, header: str, *body: str) -> str:
    return f"--- a/{path}\n+++ b/{path}\n{header}\n" + "".join(f"{line}\n" for line in body)


class _RenameProbe(_Probe):
    """A ``_Probe`` whose name-status output is supplied verbatim (rename records)."""

    def __init__(self, *, name_status: str, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.name_status = name_status

    async def _run(self, cmd: list[str], **kwargs: object):
        from awf.common.commands import CommandResult

        if "--name-status" in cmd:
            return CommandResult(returncode=0, stdout=self.name_status, stderr="")
        return await super()._run(cmd, **kwargs)


def _probe(
    *,
    texts: dict[str, str | None] | None = None,
    changed_paths: tuple[str, ...] | None,
    diffs: dict[str, str | None] | None = None,
) -> _Probe:
    return _Probe(
        texts={(_LEFT, path): text for path, text in (texts or {}).items()},
        changed_paths=changed_paths,
        diffs=dict(diffs or {}),
    )


async def _changes_named_target(runner: object, comment_text: str) -> bool:
    return await comment_named._commit_range_changes_comment_named_target(
        runner,
        worktree_path=Path("/tmp/ws"),
        left=_LEFT,
        right=_RIGHT,
        comment_text=comment_text,
    )


# --- parsing -------------------------------------------------------------------


@pytest.mark.unit
def test_comment_named_paths_extracts_plain_lined_backticked_linked_and_dotted_forms() -> None:
    text = (
        "The fix belongs in `docs/runbooks/vm-core-update.md:823` and in\n"
        "src/app/api/routes/team.py. See [the helper](./src/app/util.py), README.md,\n"
        "and src/app/a.py:10-20! Also 'src/app/b.ts', \"src/app/c.tsx\"; then team.py?"
    )

    assert comment_named._comment_named_paths(text) == (
        ("docs/runbooks/vm-core-update.md", 823),
        ("src/app/api/routes/team.py", None),
        ("src/app/util.py", None),
        ("README.md", None),
        ("src/app/a.py", 10),
        ("src/app/b.ts", None),
        ("src/app/c.tsx", None),
        ("team.py", None),
    )


@pytest.mark.unit
def test_comment_named_paths_rejects_outside_glob_url_and_extensionless_tokens() -> None:
    text = (
        "/etc/passwd.txt ../secrets.py src/../escape.py src/./dot.py ~/notes.md "
        "src/*.py src/[ab].py src/{a,b}.py src/a?.py "
        "https://github.com/o/r/blob/main/src/app/permalink.py#L3 "
        "file:///tmp/local.py Makefile src/app/package src/app/zero.py:0"
    )

    assert comment_named._comment_named_paths(text) == ()


@pytest.mark.unit
def test_comment_named_paths_deduplicates_in_order() -> None:
    text = "a.py b.py a.py ./a.py a.py:3 a.py:3"

    assert comment_named._comment_named_paths(text) == (
        ("a.py", None),
        ("b.py", None),
        ("a.py", 3),
    )


@pytest.mark.unit
def test_comment_named_identifiers_accepts_single_backtick_names_and_calls() -> None:
    text = (
        "Add it to `_TASK_INBOUND_TYPES` and honour it in "
        "`_agent_message_visibility_clause()`; JS uses `$helper` and ` padded `."
    )

    assert comment_named._comment_named_identifiers(text) == frozenset(
        {"_TASK_INBOUND_TYPES", "_agent_message_visibility_clause", "$helper", "padded"}
    )


@pytest.mark.unit
def test_comment_named_identifiers_rejects_dotted_argumented_fenced_and_keyword_spans() -> None:
    text = (
        "Not `Foo.bar`, `run(x)`, `two words`, `src/app/a.py`, `9lives`, "
        "``double``, nor `None`, `True`, `self`, `cls`, `this`, `null`, "
        "`undefined`, `return`, `def`, `function`.\n"
        "```python\nfenced_name\n```\n"
        "```\n`fenced_inline`\n```"
    )

    assert comment_named._comment_named_identifiers(text) == frozenset()


@pytest.mark.unit
def test_reviewer_comment_text_excludes_viewer_replies_and_review_context() -> None:
    thread = ReviewThread(
        thread_id="t",
        path=_CHANNEL,
        line=932,
        body_excerpt="excerpt names src/app/excerpt.py",
        comments=(
            ReviewThreadComment(comment_id="1", body="reviewer names `_TASK_INBOUND_TYPES`"),
            ReviewThreadComment(
                comment_id="2",
                body="AWF: already addressed in docs/runbooks/vm-core-update.md",
                viewer_did_author=True,
            ),
            ReviewThreadComment(comment_id="3", body="follow-up names src/app/b.py"),
        ),
        review_context=ReviewComment(comment_id="r", body_excerpt="review body names c.py"),
    )

    assert comment_named.reviewer_comment_text(thread) == (
        "reviewer names `_TASK_INBOUND_TYPES`\nfollow-up names src/app/b.py"
    )


@pytest.mark.unit
def test_reviewer_comment_text_falls_back_to_the_excerpt_only_without_comments() -> None:
    bare = ReviewThread(thread_id="t", path=_CHANNEL, line=1, body_excerpt="names a.py")
    only_awf = ReviewThread(
        thread_id="t",
        path=_CHANNEL,
        line=1,
        body_excerpt="AWF names a.py",
        comments=(ReviewThreadComment(comment_id="1", body="AWF", viewer_did_author=True),),
    )

    assert comment_named.reviewer_comment_text(bare) == "names a.py"
    assert comment_named.reviewer_comment_text(only_awf) == ""


# --- (a) comment-named path ------------------------------------------------------


@pytest.mark.unit
async def test_named_path_without_a_line_accepts_any_change_to_that_file() -> None:
    runner = _probe(
        changed_paths=(_DOC,),
        diffs={_DOC: _diff(_DOC, "@@ -40 +40 @@", "-old", "+new")},
    )

    assert await _changes_named_target(runner, f"Please also update {_DOC}.")


@pytest.mark.unit
async def test_named_path_the_range_does_not_change_is_not_evidence() -> None:
    runner = _probe(
        changed_paths=(_OTHER,),
        diffs={_OTHER: _diff(_OTHER, "@@ -2 +2 @@", "-a", "+b")},
    )

    assert not await _changes_named_target(runner, f"Please also update {_DOC}.")


@pytest.mark.unit
async def test_named_path_with_a_line_needs_a_hunk_at_that_line() -> None:
    at_line = _probe(
        changed_paths=(_DOC,),
        diffs={_DOC: _diff(_DOC, "@@ -823 +823 @@", "-old step", "+new step")},
    )
    far_away = _probe(
        changed_paths=(_DOC,),
        diffs={_DOC: _diff(_DOC, "@@ -10 +10 @@", "-old intro", "+new intro")},
    )

    assert await _changes_named_target(at_line, f"See `{_DOC}:823` for the stale step.")
    assert not await _changes_named_target(far_away, f"See `{_DOC}:823` for the stale step.")


_ANCHOR = "c" * 40


class _AnchoredProbe(_Probe):
    """A ``_Probe`` that also answers the review-anchor..``_LEFT`` relocation reads."""

    def __init__(self, *, anchor_name_status: str, anchor_diffs: dict[str, str], **kwargs: object):
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.anchor_name_status = anchor_name_status
        self.anchor_diffs = anchor_diffs

    async def _run(self, cmd: list[str], **kwargs: object):
        from awf.common.commands import CommandResult

        if _ANCHOR not in cmd:
            return await super()._run(cmd, **kwargs)
        if "--name-status" in cmd:
            return CommandResult(returncode=0, stdout=self.anchor_name_status, stderr="")
        return CommandResult(returncode=0, stdout=self.anchor_diffs.get(cmd[-1], ""), stderr="")


async def _changes_anchored_target(runner: object, comment_text: str) -> bool:
    return await comment_named._commit_range_changes_comment_named_target(
        runner,
        worktree_path=Path("/tmp/ws"),
        left=_LEFT,
        right=_RIGHT,
        comment_text=comment_text,
        anchor_head=_ANCHOR,
    )


@pytest.mark.unit
async def test_named_line_is_relocated_from_the_review_anchor_head() -> None:
    """An earlier item's insert above ``:823`` moves the fix to 923 (PRRT_kwDOSJAM6s63LTKz)."""

    def runner() -> _AnchoredProbe:
        return _AnchoredProbe(
            anchor_name_status=f"M\0{_DOC}\0",
            anchor_diffs={_DOC: _diff(_DOC, "@@ -10,0 +11,100 @@", *(["+intro"] * 100))},
            changed_paths=(_DOC,),
            diffs={_DOC: _diff(_DOC, "@@ -923 +923 @@", "-old step", "+new step")},
            texts={},
        )

    comment = f"See `{_DOC}:823` for the stale step."

    assert await _changes_anchored_target(runner(), comment)
    assert not await _changes_named_target(runner(), comment)


@pytest.mark.unit
async def test_named_path_is_relocated_through_a_rename_since_the_review_anchor() -> None:
    moved = "docs/runbooks/vm-core-upgrade.md"
    runner = _AnchoredProbe(
        anchor_name_status=f"R100\0{_DOC}\0{moved}\0",
        anchor_diffs={},
        changed_paths=(moved,),
        diffs={moved: _diff(moved, "@@ -823 +823 @@", "-old step", "+new step")},
        texts={},
    )

    assert await _changes_anchored_target(runner, f"Update {_DOC} and `{_DOC}:823`.")


@pytest.mark.unit
async def test_named_target_that_no_longer_maps_from_the_review_anchor_is_skipped() -> None:
    gone = "docs/runbooks/legacy.md"
    replacement = "docs/runbooks/replacement.md"
    runner = _AnchoredProbe(
        anchor_name_status=f"D\0{gone}\0A\0{replacement}\0",
        anchor_diffs={},
        changed_paths=(gone,),
        diffs={gone: _diff(gone, "@@ -5 +5 @@", "-a", "+b")},
        texts={},
    )

    assert not await _changes_anchored_target(runner, f"See `{gone}:5`.")
    assert await _changes_named_target(runner, f"See `{gone}:5`.")


# --- (b) comment-named identifier definition ---------------------------------------

_INBOUND_TEXT = '_TASK_INBOUND_TYPES = (\n    "a",\n    "b",\n)\n\n\ndef other():\n    return 0\n'
_INBOUND_INSERT = _diff(_INBOUND, "@@ -3,0 +4 @@", '+    "c",')


@pytest.mark.parametrize(
    ("path", "text", "diff", "comment"),
    [
        pytest.param(
            _ROUTES,
            "def member_workload(team):\n    return team.load\n",
            _diff(_ROUTES, "@@ -2 +2 @@", "-    return team.load", "+    return team.load()"),
            "`member_workload()` ignores the paused members",
            id="python-def",
        ),
        pytest.param(
            _ROUTES,
            "class TeamView:\n    limit = 1\n",
            _diff(_ROUTES, "@@ -2 +2 @@", "-    limit = 1", "+    limit = 2"),
            "`TeamView` hardcodes the limit",
            id="python-class",
        ),
        pytest.param(
            _INBOUND,
            '_TASK_INBOUND_TYPES: tuple[str, ...] = (\n    "a",\n)\n',
            _diff(_INBOUND, "@@ -2,0 +3 @@", '+    "c",'),
            "`_TASK_INBOUND_TYPES` misses the task type",
            id="annotated-assignment",
        ),
        pytest.param(
            _INBOUND,
            _INBOUND_TEXT,
            _INBOUND_INSERT,
            "`_TASK_INBOUND_TYPES` misses the task type",
            id="multi-line-tuple-insert",
        ),
        pytest.param(
            _ROUTES,
            "def member_workload(team):\n    return team.load\n",
            _diff(_ROUTES, "@@ -0,0 +1 @@", "+@functools.cache"),
            "`member_workload` should be cached",
            id="decorator-added",
        ),
        pytest.param(
            _WEB,
            "export function memberWorkload(team) {\n  return team.load;\n}\n",
            _diff(_WEB, "@@ -2 +2 @@", "-  return team.load;", "+  return team.load();"),
            "`memberWorkload()` ignores paused members",
            id="js-export-function",
        ),
        pytest.param(
            _WEB,
            "export const memberWorkload = (team) => {\n  return team.load;\n};\n",
            _diff(_WEB, "@@ -2 +2 @@", "-  return team.load;", "+  return team.load();"),
            "`memberWorkload` ignores paused members",
            id="js-export-const-arrow",
        ),
        pytest.param(
            _INBOUND,
            'def _TASK_INBOUND_TYPES():\n    return ()\n\n\n_TASK_INBOUND_TYPES = ("a",)\n'
            '_TASK_INBOUND_TYPES = ("b",)\n',
            _diff(
                _INBOUND,
                "@@ -6 +6 @@",
                '-_TASK_INBOUND_TYPES = ("b",)',
                '+_TASK_INBOUND_TYPES = ("c",)',
            ),
            "`_TASK_INBOUND_TYPES` misses the task type",
            id="last-module-level-binding",
        ),
        pytest.param(
            _ROUTES,
            'QUERY = """\nSELECT id\nFROM users\n"""\n\n\ndef other():\n    return 0\n',
            _diff(_ROUTES, "@@ -2 +2 @@", "-SELECT id", "+SELECT id, name"),
            "`QUERY` misses the name column",
            id="unindented-multi-line-string",
        ),
        pytest.param(
            _ROUTES,
            "LIMITS = dict(\nteam=1,\n)\nOTHER = 2\n",
            _diff(_ROUTES, "@@ -2 +2 @@", "-team=1,", "+team=2,"),
            "`LIMITS` is too low",
            id="unindented-bracket-continuation",
        ),
        pytest.param(
            _ROUTES,
            "LIMIT = 1 + \\\n2\nOTHER = 2\n",
            _diff(_ROUTES, "@@ -2 +2 @@", "-2", "+3"),
            "`LIMIT` is too low",
            id="backslash-continuation",
        ),
        pytest.param(
            _ROUTES,
            'KINDS: tuple[\n    str, ...\n] = ("a",)\nOTHER = 2\n',
            _diff(_ROUTES, "@@ -3 +3 @@", '-] = ("a",)', '+] = ("a", "b")'),
            "`KINDS` misses a kind",
            id="multi-line-annotation",
        ),
    ],
)
@pytest.mark.unit
async def test_named_identifier_definition_changed_by_the_range_is_evidence(
    path: str, text: str, diff: str, comment: str
) -> None:
    runner = _probe(texts={path: text}, changed_paths=(path,), diffs={path: diff})

    assert await _changes_named_target(runner, comment)


@pytest.mark.parametrize(
    ("text", "diff"),
    [
        pytest.param(
            "def build():\n    def _TASK_INBOUND_TYPES():\n        return 1\n"
            "    return _TASK_INBOUND_TYPES\n",
            _diff(_INBOUND, "@@ -3 +3 @@", "-        return 1", "+        return 2"),
            id="closure",
        ),
        pytest.param(
            'class Config:\n    _TASK_INBOUND_TYPES = (\n        "a",\n    )\n',
            _diff(_INBOUND, "@@ -3 +3 @@", '-        "a",', '+        "b",'),
            id="class-body-assignment",
        ),
        pytest.param(
            'class Config:\n    def _TASK_INBOUND_TYPES(self):\n        return ("a",)\n',
            _diff(_INBOUND, "@@ -3 +3 @@", '-        return ("a",)', '+        return ("b",)'),
            id="class-method",
        ),
        pytest.param(
            '"""Docs.\n\n_TASK_INBOUND_TYPES = (\n    "a",\n)\n"""\nVALUE = 1\n',
            _diff(_INBOUND, "@@ -4 +4 @@", '-    "a",', '+    "b",'),
            id="docstring-decoy",
        ),
        pytest.param(
            "from app.messaging.types import _TASK_INBOUND_TYPES\n\n\n"
            "def visible(kind):\n    return kind in _TASK_INBOUND_TYPES\n",
            _diff(
                _INBOUND,
                "@@ -5 +5 @@",
                "-    return kind in _TASK_INBOUND_TYPES",
                "+    return kind in set(_TASK_INBOUND_TYPES)",
            ),
            id="usage-only",
        ),
        pytest.param(
            _INBOUND_TEXT,
            _diff(_INBOUND, "@@ -8 +8 @@", "-    return 0", "+    return 1"),
            id="same-file-outside-span",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = """\na\n"""\nVALUE = (\n1\n)\n',
            _diff(_INBOUND, "@@ -5 +5 @@", "-1", "+2"),
            id="statement-after-multi-line-string",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = """\na\n"""\nVALUE = (\n1\n)\n',
            _diff(_INBOUND, "@@ -5 +5 @@", "-1", "+2"),
            id="statement-after-multi-line-string",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = ("a",)\n_TASK_INBOUND_TYPES = ("b",)\n',
            _diff(
                _INBOUND,
                "@@ -1 +1 @@",
                '-_TASK_INBOUND_TYPES = ("a",)',
                "+_TASK_INBOUND_TYPES = ()",
            ),
            id="shadowed-by-later-assignment",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = ("a",)\n\n\ndef _TASK_INBOUND_TYPES():\n    return ()\n',
            _diff(
                _INBOUND,
                "@@ -1 +1 @@",
                '-_TASK_INBOUND_TYPES = ("a",)',
                "+_TASK_INBOUND_TYPES = ()",
            ),
            id="shadowed-by-later-def",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = ("a",)\nfrom app.kinds import _TASK_INBOUND_TYPES\n',
            _diff(
                _INBOUND,
                "@@ -1 +1 @@",
                '-_TASK_INBOUND_TYPES = ("a",)',
                "+_TASK_INBOUND_TYPES = ()",
            ),
            id="shadowed-by-import",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = ("a",)\nif FLAG:\n    _TASK_INBOUND_TYPES = ("b",)\n',
            _diff(
                _INBOUND,
                "@@ -1 +1 @@",
                '-_TASK_INBOUND_TYPES = ("a",)',
                "+_TASK_INBOUND_TYPES = ()",
            ),
            id="guarded-rebinding",
        ),
        pytest.param(
            "CONFIG = dict(\n_TASK_INBOUND_TYPES=1,\n)\n",
            _diff(_INBOUND, "@@ -2 +2 @@", "-_TASK_INBOUND_TYPES=1,", "+_TASK_INBOUND_TYPES=2,"),
            id="unindented-keyword-argument",
        ),
        pytest.param(
            "CONFIG = 1 + \\\n_TASK_INBOUND_TYPES=1\n",
            _diff(_INBOUND, "@@ -2 +2 @@", "-_TASK_INBOUND_TYPES=1", "+_TASK_INBOUND_TYPES=2"),
            id="backslash-continuation-line",
        ),
    ],
)
@pytest.mark.unit
async def test_named_identifier_without_a_changed_definition_is_not_evidence(
    text: str, diff: str
) -> None:
    runner = _probe(texts={_INBOUND: text}, changed_paths=(_INBOUND,), diffs={_INBOUND: diff})

    assert not await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses the task type")


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
            "export const LIMIT = renew\nrun1();\n", 2, False, id="keyword-suffixed-identifier"
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
        pytest.param("export const LIMIT = `a\n  b`;\n", 2, True, id="template-literal"),
        pytest.param(
            "export const LIMIT = 1\n  /* unrelated\n     note 1 */\n  export const OTHER = 2;\n",
            3,
            False,
            id="standalone-block-comment",
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
    ],
)
@pytest.mark.unit
async def test_indented_binding_counts_only_at_js_ts_module_scope(
    path: str, text: str, line: int, *, expected: bool
) -> None:
    """Leading whitespace is valid at JS/TS module scope (PRRT_kwDOSJAM6s63P5Zi).

    A brace-less control or concise-arrow body is not a statement of its own, so
    a write there is no binding (PRRT_kwDOSJAM6s63QK8_).
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
    ("left_text", "right_text", "diff"),
    [
        pytest.param(
            "def other():\n    return 0\n",
            'def other():\n    return 0\n\n\n_TASK_INBOUND_TYPES = (\n    "a",\n)\n',
            _diff(
                _INBOUND, "@@ -2,0 +3,5 @@", "+", "+", "+_TASK_INBOUND_TYPES = (", '+    "a",', "+)"
            ),
            id="existing-file",
        ),
        pytest.param(
            None,
            "def _TASK_INBOUND_TYPES():\n    return ()\n",
            _diff(_INBOUND, "@@ -0,0 +1,2 @@", "+def _TASK_INBOUND_TYPES():", "+    return ()"),
            id="new-file",
        ),
    ],
)
@pytest.mark.unit
async def test_named_identifier_definition_added_by_the_range_is_evidence(
    left_text: str | None, right_text: str, diff: str
) -> None:
    """A requested helper the fix adds has no left-side span (PRRT_kwDOSJAM6s63LTK6)."""
    runner = _Probe(
        texts={(_LEFT, _INBOUND): left_text, (_RIGHT, _INBOUND): right_text},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: diff},
    )

    assert await _changes_named_target(runner, "Add a `_TASK_INBOUND_TYPES` constant")


@pytest.mark.unit
async def test_added_lines_outside_the_right_side_definition_are_not_evidence() -> None:
    right_text = (
        '_TASK_INBOUND_TYPES = ("a",)\n\n\ndef visible(kind):\n'
        "    return kind in _TASK_INBOUND_TYPES\n"
    )
    new_usage = _Probe(
        texts={(_LEFT, _INBOUND): None, (_RIGHT, _INBOUND): "from app.kinds import X\n"},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: _diff(_INBOUND, "@@ -0,0 +1 @@", "+from app.kinds import X")},
    )
    usage_edit = _Probe(
        texts={(_LEFT, _INBOUND): right_text, (_RIGHT, _INBOUND): right_text},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: _diff(_INBOUND, "@@ -5 +5 @@", "-    return 0", "+    return 1")},
    )
    removed = _Probe(
        texts={(_LEFT, _INBOUND): right_text, (_RIGHT, _INBOUND): right_text},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: _diff(_INBOUND, "@@ -6,2 +5,0 @@", "-", "-x = 1")},
    )

    for runner in (new_usage, usage_edit, removed):
        assert not await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses a type")


@pytest.mark.parametrize(
    ("left_text", "right_text", "diff"),
    [
        pytest.param(
            "def _TASK_INBOUND_TYPES():\n    return ()\n\n\ndef other():\n    return 0\n",
            "def _TASK_INBOUND_TYPES():\n    return ()\n# note\n\n\ndef other():\n    return 0\n",
            _diff(_INBOUND, "@@ -2,0 +3 @@", "+# note"),
            id="comment-after-def",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = ("a",)\nVALUE = 1\n',
            '_TASK_INBOUND_TYPES = ("a",)\n\n    # note\nVALUE = 1\n',
            _diff(_INBOUND, "@@ -1,0 +2,2 @@", "+", "+    # note"),
            id="gap-after-assignment",
        ),
    ],
)
@pytest.mark.unit
async def test_gap_inserts_trailing_a_right_side_definition_are_not_evidence(
    left_text: str, right_text: str, diff: str
) -> None:
    """Trailing blank/comment gaps are not the definition (PRRT_kwDOSJAM6s63LtTS)."""
    runner = _Probe(
        texts={(_LEFT, _INBOUND): left_text, (_RIGHT, _INBOUND): right_text},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: diff},
    )

    assert not await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses a type")


@pytest.mark.parametrize(
    ("right_text", "diff"),
    [
        pytest.param(
            '_TASK_INBOUND_TYPES = ("a",)\n\n# reworded note\n\nOTHER = 1\n',
            _diff(_INBOUND, "@@ -3 +3 @@", "-# unrelated note", "+# reworded note"),
            id="rewrite-gap-comment",
        ),
        pytest.param(
            '_TASK_INBOUND_TYPES = ("a",)\n\n\nOTHER = 1\n',
            _diff(_INBOUND, "@@ -3 +2,0 @@", "-# unrelated note"),
            id="delete-gap-comment",
        ),
    ],
)
@pytest.mark.unit
async def test_gap_edits_trailing_a_left_side_definition_are_not_evidence(
    right_text: str, diff: str
) -> None:
    """Old-side trailing gaps are not the definition either (PRRT_kwDOSJAM6s63L_5C)."""
    runner = _Probe(
        texts={
            (_LEFT, _INBOUND): '_TASK_INBOUND_TYPES = ("a",)\n\n# unrelated note\n\nOTHER = 1\n',
            (_RIGHT, _INBOUND): right_text,
        },
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: diff},
    )

    assert not await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses a type")


@pytest.mark.parametrize(
    ("path", "left_text", "right_text", "diff"),
    [
        pytest.param(
            _INBOUND,
            '_TASK_INBOUND_TYPES = """echo start\n# cleanup"""\n',
            '_TASK_INBOUND_TYPES = """echo start\n# teardown"""\n',
            _diff(_INBOUND, "@@ -2 +2 @@", '-# cleanup"""', '+# teardown"""'),
            id="python-triple-quoted",
        ),
        pytest.param(
            _WEB,
            "const _TASK_INBOUND_TYPES = `echo start\n// cleanup`;\n",
            "const _TASK_INBOUND_TYPES = `echo start\n// teardown`;\n",
            _diff(_WEB, "@@ -2 +2 @@", "-// cleanup`;", "+// teardown`;"),
            id="js-ts-template-literal",
        ),
    ],
)
@pytest.mark.unit
async def test_literal_lines_spelling_a_comment_marker_stay_in_the_span(
    path: str, left_text: str, right_text: str, diff: str
) -> None:
    """A literal's last line is its contents, not a trailing gap (PRRT_kwDOSJAM6s63SSj1)."""
    runner = _Probe(
        texts={(_LEFT, path): left_text, (_RIGHT, path): right_text},
        changed_paths=(path,),
        diffs={path: diff},
    )

    assert await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses a type")


@pytest.mark.unit
async def test_named_identifier_in_a_pure_rename_is_not_evidence() -> None:
    moved = "src/app/messaging/inbound_kinds.py"
    runner = _RenameProbe(
        name_status=f"R100\0{_INBOUND}\0{moved}\0",
        texts={(_LEFT, _INBOUND): _INBOUND_TEXT, (_RIGHT, moved): _INBOUND_TEXT},
        changed_paths=(),
        # The pathspec carries both sides of the move, so git reports no hunks.
        diffs={moved: f"diff --git a/{_INBOUND} b/{moved}\nrename from {_INBOUND}\n"},
    )

    assert not await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses the task type")
    # Both sides of the move are diffed as the same pair; the source is never read at right.
    assert runner.diff_paths[-2:] == [moved, moved]
    assert runner.shows == [_INBOUND, moved, moved]


@pytest.mark.parametrize(
    "path", [_DOC, "docs/reference/inbound.rst", "notes/inbound.txt", "docs/inbound.py.orig"]
)
@pytest.mark.unit
async def test_named_identifier_snippet_in_a_non_source_file_is_not_evidence(path: str) -> None:
    """A docs snippet spelling the definition is not the definition (PRRT_kwDOSJAM6s63O-t2)."""
    left_text = "# Inbound\n\ndef _TASK_INBOUND_TYPES():\n    return ()\n"
    right_text = "# Inbound\n\ndef _TASK_INBOUND_TYPES():\n    return ('task',)\n"
    runner = _Probe(
        texts={(_LEFT, path): left_text, (_RIGHT, path): right_text},
        changed_paths=(path,),
        diffs={path: _diff(path, "@@ -4 +4 @@", "-    return ()", "+    return ('task',)")},
    )

    assert not await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses the task type")
    assert runner.shows == []


@pytest.mark.unit
async def test_non_source_paths_do_not_spend_the_identifier_candidate_budget() -> None:
    docs = tuple(f"docs/page_{index:02d}.md" for index in range(30))
    runner = _Probe(
        texts={(_LEFT, _INBOUND): _INBOUND_TEXT, (_RIGHT, _INBOUND): _INBOUND_TEXT},
        changed_paths=(*docs, _INBOUND),
        diffs={_INBOUND: _INBOUND_INSERT},
    )

    assert await _changes_named_target(runner, "`_TASK_INBOUND_TYPES` misses the task type")
    assert set(runner.shows) == {_INBOUND}


# --- fail-closed -------------------------------------------------------------------


@pytest.mark.unit
async def test_text_naming_nothing_issues_no_git_reads() -> None:
    runner = _probe(changed_paths=(_DOC,))

    assert not await _changes_named_target(runner, "")
    assert not await _changes_named_target(runner, "please fix the flaky ordering")
    assert runner.shows == []
    assert runner.diff_paths == []


@pytest.mark.unit
async def test_unreadable_git_output_fails_closed() -> None:
    comment = f"`_TASK_INBOUND_TYPES` in {_DOC}:823"
    no_name_status = _probe(
        texts={_INBOUND: _INBOUND_TEXT},
        changed_paths=None,
        diffs={_INBOUND: _INBOUND_INSERT},
    )
    no_show = _probe(
        texts={_INBOUND: None},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: _INBOUND_INSERT},
    )
    no_diff = _probe(
        texts={_INBOUND: _INBOUND_TEXT},
        changed_paths=(_INBOUND,),
        diffs={_INBOUND: None},
    )

    assert not await _changes_named_target(no_name_status, comment)
    assert not await _changes_named_target(no_show, comment)
    assert not await _changes_named_target(no_diff, comment)
    assert no_diff.diff_paths[-1] == _INBOUND


@pytest.mark.unit
async def test_named_line_probes_are_capped() -> None:
    named = [f"src/app/m{index}.py" for index in range(comment_named._MAX_COMMENT_NAMED_PATHS + 2)]
    runner = _probe(
        changed_paths=tuple(named),
        diffs={path: _diff(path, "@@ -900 +900 @@", "-a", "+b") for path in named},
    )
    # Only the last named path carries a hunk at its named line, past the cap.
    runner.diffs[named[-1]] = _diff(named[-1], "@@ -5 +5 @@", "-a", "+b")

    assert not await _changes_named_target(runner, " ".join(f"{path}:5" for path in named))
