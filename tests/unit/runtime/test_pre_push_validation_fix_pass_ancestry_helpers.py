"""Direct unit tests for pre-push validation ancestry helpers."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.common.commands import CommandResult, FakeCommandRunner
from awf.runtime.pr_monitor_runner import pre_push_validation_fix_pass_ancestry as ancestry


@pytest.mark.unit
@pytest.mark.parametrize(
    ("line", "start", "count", "expected"),
    [
        (5, 5, 0, False),
        (4, 5, 3, False),
        (5, 5, 3, True),
        (7, 5, 3, True),
        (8, 5, 3, False),
    ],
)
def test_line_in_unified_diff_hunk_range(line: int, start: int, count: int, expected: bool) -> None:
    assert ancestry._line_in_unified_diff_hunk_range(line, start, count) is expected


@pytest.mark.unit
def test_map_review_line_through_diff_rejects_non_positive_lines() -> None:
    assert ancestry._map_review_line_through_diff(0, "@@ -1,1 +1,1 @@\n") == 0


@pytest.mark.unit
def test_map_review_line_through_diff_clamps_when_new_side_is_shorter() -> None:
    diff = "@@ -10,5 +10,2 @@\n"
    assert ancestry._map_review_line_through_diff(14, diff) == 11


@pytest.mark.unit
@pytest.mark.parametrize(
    ("diff_stdout", "expected"),
    [
        ("", {}),
        ("M\0src/a.py\0", {}),
        ("R100\0src/old.py\0src/new.py", {}),
        ("C100\0src/old.py\0src/new.py\0", {}),
        (
            "R100\0src/old.py\0src/new.py\0",
            {"src/old.py": "src/new.py"},
        ),
    ],
)
def test_rename_map_from_name_status_z_parses_edges(
    diff_stdout: str, expected: dict[str, str]
) -> None:
    assert ancestry._rename_map_from_name_status_z(diff_stdout) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("", True),
        ("  ", True),
        ("ab", True),
        ("# comment", True),
        ("import os", True),
        ("pass", True),
        ("return None", True),
        ("meaningful content", False),
    ],
)
def test_is_trivial_content_overlap_line(line: str, expected: bool) -> None:
    assert ancestry._is_trivial_content_overlap_line(line) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        (set(), {"keep"}, False),
        ({"keep"}, set(), False),
        ({"import os"}, {"import os"}, False),
        ({"alpha", "beta"}, {"alpha", "beta"}, True),
        ({"alpha"}, {"alpha", "beta", "gamma"}, True),
        ({"alpha"}, {"beta"}, False),
    ],
)
def test_paths_have_meaningful_line_level_content_overlap(
    left: set[str],
    right: set[str],
    expected: bool,
) -> None:
    assert ancestry._paths_have_meaningful_line_level_content_overlap(left, right) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name_status_z", "expected"),
    [
        ("", ()),
        ("M\0src/a.py\0", ()),
        ("A\0src/new.py\0", ("src/new.py",)),
    ],
)
def test_added_paths_from_name_status_z(name_status_z: str, expected: tuple[str, ...]) -> None:
    assert ancestry._added_paths_from_name_status_z(name_status_z) == expected


@pytest.mark.unit
def test_plausible_rename_partners_for_deletion_filters_unrelated_adds() -> None:
    name_status = "D\0src/old.py\0A\0tests/test_other.py\0"
    assert ancestry._plausible_rename_partners_for_deletion(name_status, "src/old.py") == ()


@pytest.mark.unit
@pytest.mark.parametrize(
    ("deleted", "added", "expected"),
    [
        ("", "src/new.py", False),
        ("src/old.py", "", False),
        ("old.py", "new.py", True),
        ("src/foo.py", "tests/test_foo.py", True),
        ("src/foo.py", "tests/test_bar.py", False),
    ],
)
def test_plausible_rename_replacement(deleted: str, added: str, expected: bool) -> None:
    assert ancestry._plausible_rename_replacement(deleted, added) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name_status_z", "path", "expected"),
    [
        ("", "src/old.py", False),
        ("D\0src/old.py\0A\0src/new.py\0", "src/missing.py", False),
        ("D\0src/old.py\0A\0src/new.py\0", "src/old.py", True),
        ("R100\0src/old.py\0src/new.py\0", "src/old.py", False),
    ],
)
def test_path_deletion_addition_without_rename(
    name_status_z: str,
    path: str,
    expected: bool,
) -> None:
    assert ancestry._path_deletion_addition_without_rename(name_status_z, path) is expected


@pytest.mark.unit
def test_follow_rename_map_detects_cycles() -> None:
    rename_map = {"a.py": "b.py", "b.py": "a.py"}
    assert ancestry._follow_rename_map("a.py", rename_map) == "a.py"


@pytest.mark.unit
def test_merge_rename_edge_ignores_empty_paths() -> None:
    rename_map: dict[str, str] = {}
    ancestry._merge_rename_edge(rename_map, "", "src/new.py")
    ancestry._merge_rename_edge(rename_map, "src/old.py", "")
    assert rename_map == {}


@pytest.mark.unit
def test_diff_hunk_touches_line_rejects_non_positive_lines() -> None:
    assert ancestry._diff_hunk_touches_line("@@ -1,1 +1,1 @@\n", 0) is False


@pytest.mark.unit
def test_diff_hunk_touches_line_matches_insert_before_anchor() -> None:
    diff = "@@ -174,0 +175,5 @@\n"
    assert ancestry._diff_hunk_touches_line(diff, 174) is True
    assert ancestry._diff_hunk_touches_line(diff, 175) is True


@pytest.mark.unit
def test_changed_path_in_item_scope_rejects_empty_paths() -> None:
    assert ancestry._changed_path_in_item_scope(item_path="", changed_path="src/a.py") is False
    assert ancestry._changed_path_in_item_scope(item_path="src/a.py", changed_path="") is False


@pytest.mark.unit
def test_changed_path_in_item_scope_accepts_descendant_paths() -> None:
    assert ancestry._changed_path_in_item_scope(
        item_path="src/pkg",
        changed_path="src/pkg/module.py",
    )


@pytest.mark.unit
async def test_path_line_at_ref_rejects_non_positive_lines() -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._path_line_at_ref(
            runner,
            worktree_path=Path("/tmp/repo"),
            ref="HEAD",
            path="src/a.py",
            line=0,
        )
        is None
    )


@pytest.mark.unit
async def test_path_line_at_ref_returns_none_when_show_fails() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="missing")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._path_line_at_ref(
            runner,
            worktree_path=Path("/tmp/repo"),
            ref="HEAD",
            path="src/a.py",
            line=1,
        )
        is None
    )


@pytest.mark.unit
async def test_path_line_at_ref_returns_none_when_line_out_of_range() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="only\n")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._path_line_at_ref(
            runner,
            worktree_path=Path("/tmp/repo"),
            ref="HEAD",
            path="src/a.py",
            line=5,
        )
        is None
    )


@pytest.mark.unit
async def test_paths_share_review_anchor_line_returns_false_when_anchor_missing() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="missing")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._paths_share_review_anchor_line(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            left_path="src/a.py",
            right_path="src/b.py",
            line=1,
        )
        is False
    )


@pytest.mark.unit
async def test_paths_share_line_level_content_returns_false_when_show_fails() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="missing")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._paths_share_line_level_content(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            left_path="src/a.py",
            right_path="src/b.py",
        )
        is False
    )


@pytest.mark.unit
async def test_name_status_z_between_returns_empty_on_diff_failure() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="diff failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._name_status_z_between(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
        )
        == ""
    )


@pytest.mark.unit
async def test_name_status_z_between_prefers_stdout_bytes() -> None:
    class _BytesStdoutRunner:
        async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            del args
            return CommandResult(
                returncode=0,
                stdout="",
                stderr="",
                stdout_bytes=b"M\0src/a.py\0",
            )

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_BytesStdoutRunner()))
    assert (
        await ancestry._name_status_z_between(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
        )
        == "M\0src/a.py\0"
    )


@pytest.mark.unit
async def test_per_commit_rename_map_in_range_returns_empty_for_same_head() -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._per_commit_rename_map_in_range(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="abc",
            right="ABC",
        )
        == {}
    )


@pytest.mark.unit
async def test_map_review_path_through_commits_returns_none_for_empty_path() -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._map_review_path_through_commits(
            runner,
            worktree_path=Path("/tmp/repo"),
            anchor_head="aaa",
            target_head="bbb",
            path="   ",
        )
        is None
    )


@pytest.mark.unit
async def test_map_review_line_through_commits_returns_line_for_same_head() -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._map_review_line_through_commits(
            runner,
            worktree_path=Path("/tmp/repo"),
            anchor_head="abc",
            target_head="ABC",
            path="src/a.py",
            line=4,
        )
        == 4
    )


@pytest.mark.unit
async def test_map_review_line_through_commits_returns_none_when_diff_fails() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="diff failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._map_review_line_through_commits(
            runner,
            worktree_path=Path("/tmp/repo"),
            anchor_head="aaa",
            target_head="bbb",
            path="src/a.py",
            line=2,
        )
        is None
    )


@pytest.mark.unit
async def test_commit_range_in_item_scope_returns_false_when_no_changed_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_paths(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ()

    monkeypatch.setattr(ancestry, "_changed_paths_in_commit_range", _no_paths)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._commit_range_in_item_scope(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
            item_path="src/a.py",
        )
        is False
    )


@pytest.mark.unit
def test_rename_diff_preserves_line_numbers_when_no_hunks() -> None:
    assert ancestry._rename_diff_preserves_line_numbers("diff --git a/x b/y\n") is True


@pytest.mark.unit
def test_follow_rename_map_chains_edges() -> None:
    rename_map = {"src/old.py": "src/mid.py", "src/mid.py": "src/new.py"}
    assert ancestry._follow_rename_map("src/old.py", rename_map) == "src/new.py"


@pytest.mark.unit
async def test_same_dir_unrelated_conftest_addition_returns_true_for_unrelated_partner() -> None:
    class _ShowRunner:
        async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            if "show" in args:
                ref_path = args[args.index("show") + 1]
                if ref_path.endswith("old.py"):
                    return CommandResult(returncode=0, stdout="keep\nREVIEWED\nremove\n", stderr="")
                if ref_path.endswith("conftest.py"):
                    return CommandResult(returncode=0, stdout="import pytest\n", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="missing")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_ShowRunner()))
    name_status = "D\0src/old.py\0A\0src/conftest.py\0"
    assert (
        await ancestry._same_dir_unrelated_conftest_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/old.py",
            name_status_z=name_status,
            line=2,
        )
        is True
    )


@pytest.mark.unit
async def test_unrelated_test_prefix_rename_addition_returns_true_for_unrelated_test_add() -> None:
    class _ShowRunner:
        async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            if "show" in args:
                ref_path = args[args.index("show") + 1]
                if ref_path.endswith("src/foo.py"):
                    return CommandResult(returncode=0, stdout="reviewed helper\n", stderr="")
                if ref_path.endswith("tests/test_foo.py"):
                    return CommandResult(returncode=0, stdout="import pytest\n", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="missing")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_ShowRunner()))
    name_status = "D\0src/foo.py\0A\0tests/test_foo.py\0"
    assert (
        await ancestry._unrelated_test_prefix_rename_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/foo.py",
            name_status_z=name_status,
            line=1,
        )
        is True
    )


@pytest.mark.unit
async def test_paths_share_review_anchor_line_matches_shared_content() -> None:
    class _ShowRunner:
        async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            if "show" in args:
                return CommandResult(returncode=0, stdout="reviewed anchor\n", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="missing")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_ShowRunner()))
    assert (
        await ancestry._paths_share_review_anchor_line(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            left_path="src/a.py",
            right_path="src/b.py",
            line=1,
        )
        is True
    )


@pytest.mark.unit
async def test_paths_share_line_level_content_detects_overlap() -> None:
    class _ShowRunner:
        async def run(self, args: list[str], **_kwargs: object) -> CommandResult:
            if "show" in args:
                return CommandResult(returncode=0, stdout="alpha\nbeta\n", stderr="")
            return CommandResult(returncode=1, stdout="", stderr="missing")

    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_ShowRunner()))
    assert (
        await ancestry._paths_share_line_level_content(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            left_path="src/a.py",
            right_path="src/b.py",
        )
        is True
    )
