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


@pytest.mark.unit
def test_rename_map_rejects_incomplete_rename_record() -> None:
    assert ancestry._rename_map_from_name_status_z("R100\0src/old.py\0") == {}
    assert ancestry._rename_map_from_name_status_z("R100\0\0src/new.py\0") == {}


@pytest.mark.unit
def test_test_prefixed_stem_accepts_suffix_convention() -> None:
    assert ancestry._test_prefixed_stem_targets_deleted("src/foo.py", "tests/foo_test.py")
    assert not ancestry._test_prefixed_stem_targets_deleted("src/foo.py", "tests/bar_test.py")


@pytest.mark.unit
async def test_paths_share_review_anchor_rejects_trivial_and_missing_right_blob() -> None:
    trivial = FakeCommandRunner()
    trivial.queue_result(returncode=0, stdout="pass\n")
    trivial_runner = SimpleNamespace(_deps=SimpleNamespace(runner=trivial))
    assert (
        await ancestry._paths_share_review_anchor_line(
            trivial_runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            left_path="src/a.py",
            right_path="src/b.py",
            line=1,
        )
        is False
    )

    missing_right = FakeCommandRunner()
    missing_right.queue_result(returncode=0, stdout="meaningful anchor\n")
    missing_right.queue_result(returncode=1, stderr="missing")
    missing_runner = SimpleNamespace(_deps=SimpleNamespace(runner=missing_right))
    assert (
        await ancestry._paths_share_review_anchor_line(
            missing_runner,
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
async def test_paths_share_line_level_content_rejects_missing_right_blob() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="meaningful content\n")
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
@pytest.mark.parametrize(
    ("name_status_z", "expected"),
    [
        ("A\0src/new.py", ()),
        ("A\0", ()),
        ("R100\0src/old.py\0src/new.py\0A\0src/extra.py\0", ("src/extra.py",)),
        ("C100\0src/old.py\0src/copy.py\0A\0\0", ()),
    ],
)
def test_added_paths_handles_malformed_and_multi_path_records(
    name_status_z: str,
    expected: tuple[str, ...],
) -> None:
    assert ancestry._added_paths_from_name_status_z(name_status_z) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name_status_z", "deleted_path", "expected"),
    [
        ("A\0src/new.py", "src/old.py", ()),
        ("A", "src/old.py", ()),
        ("A\0", "src/old.py", ()),
        ("A\0src/new.py\0", " ", ()),
        ("R100\0src/old.py\0src/new.py\0", "src/old.py", ()),
        ("C100\0src/old.py\0src/copy.py\0A\0src/new.py\0", "src/old.py", ("src/new.py",)),
    ],
)
def test_plausible_rename_partners_handles_parser_edges(
    name_status_z: str,
    deleted_path: str,
    expected: tuple[str, ...],
) -> None:
    assert ancestry._plausible_rename_partners_for_deletion(name_status_z, deleted_path) == expected


@pytest.mark.unit
async def test_same_dir_conftest_exemption_rejects_invalid_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._same_dir_unrelated_conftest_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path=" ",
            name_status_z="",
        )
        is False
    )
    assert (
        await ancestry._same_dir_unrelated_conftest_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/old.py",
            name_status_z="D\0src/old.py\0A\0tests/test_other.py\0",
        )
        is False
    )

    async def _no_overlap(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(ancestry, "_paths_share_line_level_content", _no_overlap)
    assert (
        await ancestry._same_dir_unrelated_conftest_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/old.py",
            name_status_z="D\0src/old.py\0A\0lib/new.py\0",
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize("anchor_overlap", [False, True])
async def test_same_dir_conftest_exemption_rejects_content_or_anchor_overlap(
    monkeypatch: pytest.MonkeyPatch,
    anchor_overlap: bool,
) -> None:
    async def _content_overlap(*_args: object, **_kwargs: object) -> bool:
        return not anchor_overlap

    async def _anchor_overlap(*_args: object, **_kwargs: object) -> bool:
        return anchor_overlap

    monkeypatch.setattr(ancestry, "_paths_share_line_level_content", _content_overlap)
    monkeypatch.setattr(ancestry, "_paths_share_review_anchor_line", _anchor_overlap)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._same_dir_unrelated_conftest_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/old.py",
            name_status_z="D\0src/old.py\0A\0src/conftest.py\0",
            line=2,
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize("anchor_overlap", [False, True])
async def test_same_dir_conftest_exemption_checks_other_added_paths(
    monkeypatch: pytest.MonkeyPatch,
    anchor_overlap: bool,
) -> None:
    async def _content_overlap(*_args: object, **kwargs: object) -> bool:
        return not anchor_overlap and str(kwargs["right_path"]).endswith("test_other.py")

    async def _anchor_overlap(*_args: object, **kwargs: object) -> bool:
        return anchor_overlap and str(kwargs["right_path"]).endswith("test_other.py")

    monkeypatch.setattr(ancestry, "_paths_share_line_level_content", _content_overlap)
    monkeypatch.setattr(ancestry, "_paths_share_review_anchor_line", _anchor_overlap)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    name_status = "D\0src/old.py\0A\0src/conftest.py\0A\0tests/test_other.py\0"
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
        is False
    )


@pytest.mark.unit
async def test_test_prefix_exemption_rejects_invalid_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_overlap(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(ancestry, "_paths_share_line_level_content", _no_overlap)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._unrelated_test_prefix_rename_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path=" ",
            name_status_z="",
        )
        is False
    )
    assert (
        await ancestry._unrelated_test_prefix_rename_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/foo.py",
            name_status_z="D\0src/foo.py\0A\0tests/test_other.py\0",
        )
        is False
    )
    assert (
        await ancestry._unrelated_test_prefix_rename_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/foo.py",
            name_status_z="D\0src/foo.py\0A\0src/bar.py\0",
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize("anchor_overlap", [False, True])
async def test_test_prefix_exemption_rejects_content_or_anchor_overlap(
    monkeypatch: pytest.MonkeyPatch,
    anchor_overlap: bool,
) -> None:
    async def _content_overlap(*_args: object, **_kwargs: object) -> bool:
        return not anchor_overlap

    async def _anchor_overlap(*_args: object, **_kwargs: object) -> bool:
        return anchor_overlap

    monkeypatch.setattr(ancestry, "_paths_share_line_level_content", _content_overlap)
    monkeypatch.setattr(ancestry, "_paths_share_review_anchor_line", _anchor_overlap)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._unrelated_test_prefix_rename_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/foo.py",
            name_status_z="D\0src/foo.py\0A\0tests/test_foo.py\0",
            line=2,
        )
        is False
    )


@pytest.mark.unit
@pytest.mark.parametrize("anchor_overlap", [False, True])
async def test_test_prefix_exemption_checks_other_added_paths(
    monkeypatch: pytest.MonkeyPatch,
    anchor_overlap: bool,
) -> None:
    async def _content_overlap(*_args: object, **kwargs: object) -> bool:
        return not anchor_overlap and str(kwargs["right_path"]).endswith("other.py")

    async def _anchor_overlap(*_args: object, **kwargs: object) -> bool:
        return anchor_overlap and str(kwargs["right_path"]).endswith("other.py")

    monkeypatch.setattr(ancestry, "_paths_share_line_level_content", _content_overlap)
    monkeypatch.setattr(ancestry, "_paths_share_review_anchor_line", _anchor_overlap)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    name_status = "D\0src/foo.py\0A\0tests/test_foo.py\0A\0tests/test_other.py\0"
    assert (
        await ancestry._unrelated_test_prefix_rename_addition(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="left",
            right="right",
            deleted_path="src/foo.py",
            name_status_z=name_status,
            line=2,
        )
        is False
    )


@pytest.mark.unit
def test_path_deletion_parser_rejects_incomplete_rename_and_handles_copy() -> None:
    assert (
        ancestry._path_deletion_addition_without_rename("R100\0src/old.py\0", "src/old.py") is False
    )
    name_status = "C100\0src/a.py\0src/copy.py\0D\0src/old.py\0A\0src/new.py\0"
    assert ancestry._path_deletion_addition_without_rename(name_status, "src/old.py") is True
    assert ancestry._path_deletion_addition_without_rename("D\0src/old.py", "src/old.py") is False
    assert ancestry._path_deletion_addition_without_rename("R100\0", "src/old.py") is False
    assert ancestry._path_deletion_addition_without_rename("D\0", "src/old.py") is False
    assert ancestry._path_deletion_addition_without_rename("A\0", "src/old.py") is False
    blank_rename_source = "R100\0\0src/renamed.py\0D\0src/old.py\0A\0src/new.py\0"
    assert (
        ancestry._path_deletion_addition_without_rename(blank_rename_source, "src/old.py") is True
    )

    explicitly_renamed = "R100\0src/old.py\0src/renamed.py\0D\0src/old.py\0A\0src/replacement.py\0"
    assert (
        ancestry._path_deletion_addition_without_rename(explicitly_renamed, "src/old.py") is False
    )


@pytest.mark.unit
def test_merge_and_add_rename_edges_preserve_existing_range_mapping() -> None:
    rename_map = {"src/original.py": "src/middle.py", "src/kept.py": "src/final.py"}
    ancestry._merge_rename_edge(rename_map, "src/middle.py", "src/final.py")
    ancestry._add_missing_per_commit_rename_edges(
        rename_map,
        {"src/kept.py": "src/ignored.py", "src/new.py": "src/newer.py", " ": "x.py"},
    )
    assert rename_map == {
        "src/original.py": "src/final.py",
        "src/kept.py": "src/final.py",
        "src/middle.py": "src/final.py",
        "src/new.py": "src/newer.py",
    }


@pytest.mark.unit
async def test_name_status_z_between_falls_back_to_text_stdout() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="A\0src/new.py\0")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._name_status_z_between(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
        )
        == "A\0src/new.py\0"
    )


@pytest.mark.unit
async def test_per_commit_rename_map_handles_failed_and_malformed_commits() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="\ncommit1\ncommit2\ncommit3\ncommit4\n")
    cmd.queue_result(returncode=1, stderr="parent missing")
    cmd.queue_result(returncode=0, stdout="\n")
    cmd.queue_result(returncode=0, stdout="parent3\n")
    cmd.queue_result(returncode=1, stderr="diff failed")
    cmd.queue_result(returncode=0, stdout="parent4\n")
    cmd.queue_result(returncode=0, stdout="R100\0src/old.py\0")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))

    assert (
        await ancestry._per_commit_rename_map_in_range(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
        )
        == {}
    )


@pytest.mark.unit
async def test_per_commit_rename_map_returns_empty_when_rev_list_fails() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=1, stderr="rev-list failed")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))
    assert (
        await ancestry._per_commit_rename_map_in_range(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
        )
        == {}
    )


@pytest.mark.unit
async def test_per_commit_rename_map_accumulates_valid_edge() -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="commit1\n")
    cmd.queue_result(returncode=0, stdout="parent1\n")
    cmd.queue_result(returncode=0, stdout="R100\0src/old.py\0src/new.py\0")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))

    assert await ancestry._per_commit_rename_map_in_range(
        runner,
        worktree_path=Path("/tmp/repo"),
        left="aaa",
        right="bbb",
    ) == {"src/old.py": "src/new.py"}


@pytest.mark.unit
async def test_rename_map_in_range_rejects_empty_and_malformed_diff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))

    async def _empty(*_args: object, **_kwargs: object) -> str:
        return ""

    monkeypatch.setattr(ancestry, "_name_status_z_between", _empty)
    assert await ancestry._rename_map_in_commit_range(
        runner,
        worktree_path=Path("/tmp/repo"),
        left="aaa",
        right="bbb",
    ) == ({}, "")

    async def _malformed(*_args: object, **_kwargs: object) -> str:
        return "R100\0src/old.py\0"

    monkeypatch.setattr(ancestry, "_name_status_z_between", _malformed)
    assert await ancestry._rename_map_in_commit_range(
        runner,
        worktree_path=Path("/tmp/repo"),
        left="aaa",
        right="bbb",
    ) == ({}, "")


@pytest.mark.unit
async def test_rename_map_in_range_reuses_cached_range_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    name_status_calls = 0
    per_commit_calls = 0

    async def _name_status(*_args: object, **_kwargs: object) -> str:
        nonlocal name_status_calls
        name_status_calls += 1
        return "R100\0src/old.py\0src/new.py\0"

    async def _per_commit(*_args: object, **_kwargs: object) -> dict[str, str]:
        nonlocal per_commit_calls
        per_commit_calls += 1
        return {"src/old.py": "src/new.py"}

    monkeypatch.setattr(ancestry, "_name_status_z_between", _name_status)
    monkeypatch.setattr(ancestry, "_per_commit_rename_map_in_range", _per_commit)

    first = await ancestry._rename_map_in_commit_range(
        runner,
        worktree_path=Path("/tmp/repo"),
        left="aaa",
        right="bbb",
    )
    second = await ancestry._rename_map_in_commit_range(
        runner,
        worktree_path=Path("/tmp/repo"),
        left="aaa",
        right="bbb",
    )

    assert first == second == ({"src/old.py": "src/new.py"}, "R100\0src/old.py\0src/new.py\0")
    assert name_status_calls == 1
    assert per_commit_calls == 1


@pytest.mark.unit
async def test_map_review_path_same_head_returns_normalized_path() -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._map_review_path_through_commits(
            runner,
            worktree_path=Path("/tmp/repo"),
            anchor_head="ABC",
            target_head="abc",
            path="./src/a.py",
        )
        == "src/a.py"
    )


@pytest.mark.unit
async def test_map_review_path_follows_range_rename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _renamed(*_args: object, **_kwargs: object) -> tuple[dict[str, str], str]:
        return {"src/a.py": "src/b.py"}, "R100\0src/a.py\0src/b.py\0"

    monkeypatch.setattr(ancestry, "_rename_map_in_commit_range", _renamed)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._map_review_path_through_commits(
            runner,
            worktree_path=Path("/tmp/repo"),
            anchor_head="aaa",
            target_head="bbb",
            path="src/a.py",
        )
        == "src/b.py"
    )


@pytest.mark.unit
async def test_map_review_line_rejects_empty_path() -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._map_review_line_through_commits(
            runner,
            worktree_path=Path("/tmp/repo"),
            anchor_head="aaa",
            target_head="bbb",
            path=" ",
            line=2,
        )
        is None
    )


@pytest.mark.unit
async def test_map_review_line_uses_text_diff_and_rejects_ambiguous_delete_add(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="@@ -2,1 +2,1 @@\n-old\n+new\n")
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))

    async def _rename_map(*_args: object, **_kwargs: object) -> tuple[dict[str, str], str]:
        return {}, "D\0src/a.py\0A\0lib/a.py\0"

    async def _not_unrelated(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(ancestry, "_rename_map_in_commit_range", _rename_map)
    monkeypatch.setattr(ancestry, "_same_dir_unrelated_conftest_addition", _not_unrelated)
    monkeypatch.setattr(ancestry, "_unrelated_test_prefix_rename_addition", _not_unrelated)
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
@pytest.mark.parametrize("rename_ok", [False, True])
async def test_map_review_line_handles_rename_diff_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    rename_ok: bool,
) -> None:
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="@@ -2,1 +2,1 @@\n")
    cmd.queue_result(
        returncode=0 if rename_ok else 1,
        stdout="diff --git a/src/a.py b/src/b.py\n",
        stderr="rename diff failed" if not rename_ok else "",
    )
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=cmd))

    async def _rename_map(*_args: object, **_kwargs: object) -> tuple[dict[str, str], str]:
        return {"src/a.py": "src/b.py"}, "R100\0src/a.py\0src/b.py\0"

    monkeypatch.setattr(ancestry, "_rename_map_in_commit_range", _rename_map)
    mapped = await ancestry._map_review_line_through_commits(
        runner,
        worktree_path=Path("/tmp/repo"),
        anchor_head="aaa",
        target_head="bbb",
        path="src/a.py",
        line=2,
    )
    assert mapped == 2


@pytest.mark.unit
async def test_map_review_line_decodes_byte_diffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BytesRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            self.calls += 1
            if self.calls == 1:
                return CommandResult(
                    returncode=0,
                    stdout="",
                    stderr="",
                    stdout_bytes=b"@@ -2,1 +2,1 @@\n",
                )
            return CommandResult(
                returncode=0,
                stdout="",
                stderr="",
                stdout_bytes=b"diff --git a/src/a.py b/src/b.py\n",
            )

    async def _renamed(*_args: object, **_kwargs: object) -> tuple[dict[str, str], str]:
        return {"src/a.py": "src/b.py"}, "R100\0src/a.py\0src/b.py\0"

    monkeypatch.setattr(ancestry, "_rename_map_in_commit_range", _renamed)
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=_BytesRunner()))
    assert (
        await ancestry._map_review_line_through_commits(
            runner,
            worktree_path=Path("/tmp/repo"),
            anchor_head="aaa",
            target_head="bbb",
            path="src/a.py",
            line=2,
        )
        == 2
    )


@pytest.mark.unit
async def test_commit_range_touches_path_handles_diff_failure_and_text_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _changed(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("src/a.py",)

    async def _no_renames(*_args: object, **_kwargs: object) -> tuple[dict[str, str], str]:
        return {}, "M\0src/a.py\0"

    monkeypatch.setattr(ancestry, "_changed_paths_in_commit_range", _changed)
    monkeypatch.setattr(ancestry, "_rename_map_in_commit_range", _no_renames)

    failed = FakeCommandRunner()
    failed.queue_result(returncode=1, stderr="diff failed")
    assert (
        await ancestry._commit_range_touches_path(
            SimpleNamespace(_deps=SimpleNamespace(runner=failed)),
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
            path="src/a.py",
            line=2,
        )
        is False
    )

    text = FakeCommandRunner()
    text.queue_result(returncode=0, stdout="@@ -2,1 +2,1 @@\n-old\n+new\n")
    assert (
        await ancestry._commit_range_touches_path(
            SimpleNamespace(_deps=SimpleNamespace(runner=text)),
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
            path="src/a.py",
            line=2,
        )
        is True
    )


@pytest.mark.unit
@pytest.mark.parametrize("rename_ok", [False, True])
async def test_commit_range_touches_path_handles_rename_diff_failures(
    monkeypatch: pytest.MonkeyPatch,
    rename_ok: bool,
) -> None:
    async def _changed(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("src/a.py",)

    async def _renamed(*_args: object, **_kwargs: object) -> tuple[dict[str, str], str]:
        return {"src/a.py": "src/b.py"}, "R100\0src/a.py\0src/b.py\0"

    monkeypatch.setattr(ancestry, "_changed_paths_in_commit_range", _changed)
    monkeypatch.setattr(ancestry, "_rename_map_in_commit_range", _renamed)
    cmd = FakeCommandRunner()
    cmd.queue_result(returncode=0, stdout="@@ -2,1 +2,1 @@\n")
    cmd.queue_result(
        returncode=0 if rename_ok else 1,
        stdout="diff --git a/src/a.py b/src/b.py\n",
        stderr="rename diff failed" if not rename_ok else "",
    )
    result = await ancestry._commit_range_touches_path(
        SimpleNamespace(_deps=SimpleNamespace(runner=cmd)),
        worktree_path=Path("/tmp/repo"),
        left="aaa",
        right="bbb",
        path="src/a.py",
        line=2,
    )
    assert result is False


@pytest.mark.unit
async def test_commit_range_touches_path_decodes_byte_diffs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _changed(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        return ("src/a.py",)

    async def _renamed(*_args: object, **_kwargs: object) -> tuple[dict[str, str], str]:
        return {"src/a.py": "src/b.py"}, "R100\0src/a.py\0src/b.py\0"

    monkeypatch.setattr(ancestry, "_changed_paths_in_commit_range", _changed)
    monkeypatch.setattr(ancestry, "_rename_map_in_commit_range", _renamed)

    class _BytesRunner:
        async def run(self, _args: list[str], **_kwargs: object) -> CommandResult:
            return CommandResult(
                returncode=0,
                stdout="",
                stderr="",
                stdout_bytes=b"@@ -2,1 +2,1 @@\n",
            )

    assert await ancestry._commit_range_touches_path(
        SimpleNamespace(_deps=SimpleNamespace(runner=_BytesRunner())),
        worktree_path=Path("/tmp/repo"),
        left="aaa",
        right="bbb",
        path="src/a.py",
        line=2,
    )


@pytest.mark.unit
def test_diff_hunk_near_anchor_related_accepts_guard_window_before_line() -> None:
    # Pure insert several lines before the review anchor (not line / line-1),
    # but only when the insert shares the review line's enclosing definition.
    same_fn = (
        "def reviewed():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
        "    f = 6\n"
        "    do_work()\n"
    )
    assert (
        ancestry._diff_hunk_near_anchor_related("@@ -3,0 +4,2 @@\n", 8, file_text=same_fn) is True
    )
    # Modifications near the anchor are not proximity evidence (call-site link only).
    assert (
        ancestry._diff_hunk_near_anchor_related("@@ -4,1 +4,1 @@\n", 8, file_text=same_fn) is False
    )


@pytest.mark.unit
def test_diff_hunk_near_anchor_related_rejects_other_enclosing_def() -> None:
    """Unrelated insert in a neighboring function must not count as near-anchor evidence."""
    text = (
        "def other():\n"
        "    x = 1\n"
        "    y = 2\n"
        "\n"
        "def reviewed():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    do_work()\n"
    )
    # Pure insert after line 2 inside other() — within the 12-line window of do_work.
    assert ancestry._diff_hunk_near_anchor_related("@@ -2,0 +3,1 @@\n", 8, file_text=text) is False
    assert ancestry._diff_hunk_near_anchor_related("@@ -3,0 +4,2 @@\n", 8, file_text="") is False


@pytest.mark.unit
def test_diff_hunk_near_anchor_related_rejects_distant_and_after() -> None:
    text = "def reviewed():\n    do_work()\n"
    assert ancestry._diff_hunk_near_anchor_related("@@ -1,0 +2,1 @@\n", 30, file_text=text) is False
    assert (
        ancestry._diff_hunk_near_anchor_related("@@ -20,0 +21,1 @@\n", 8, file_text=text) is False
    )
    assert ancestry._diff_hunk_near_anchor_related("@@ -3,0 +4,2 @@\n", 0, file_text=text) is False


@pytest.mark.unit
def test_callee_names_from_anchor_line_extracts_calls_and_filters_keywords() -> None:
    assert ancestry._callee_names_from_anchor_line("    return helper(x)") == frozenset({"helper"})
    assert ancestry._callee_names_from_anchor_line("if ready(x) and helper():") == frozenset(
        {"ready", "helper"}
    )
    assert ancestry._callee_names_from_anchor_line("    return self.helper()") == frozenset(
        {"helper"}
    )
    assert ancestry._callee_names_from_anchor_line("    if (x):") == frozenset()
    assert ancestry._callee_names_from_anchor_line("    return None") == frozenset()
    assert ancestry._callee_names_from_anchor_line("") == frozenset()
    assert ancestry._callee_names_from_anchor_line("def reviewed():") == frozenset()
    assert ancestry._callee_names_from_anchor_line("def reviewed(): return helper()") == frozenset(
        {"helper"}
    )


@pytest.mark.unit
def test_callee_refs_capture_optional_qualifier() -> None:
    assert ancestry._callee_refs_from_anchor_line("    return self.helper()") == frozenset(
        {("self", "helper")}
    )
    assert ancestry._callee_refs_from_anchor_line("    return helper()") == frozenset(
        {(None, "helper")}
    )


@pytest.mark.unit
def test_callee_refs_ignore_calls_inside_comments_and_string_literals() -> None:
    # Call-shaped text in comments/literals must not become FIXED callee evidence.
    assert ancestry._callee_refs_from_anchor_line("    # TODO: helper()") == frozenset()
    assert ancestry._callee_refs_from_anchor_line("    x = 1  # helper()") == frozenset()
    assert ancestry._callee_refs_from_anchor_line("    // helper()") == frozenset()
    assert ancestry._callee_refs_from_anchor_line('    message = "helper()"') == frozenset()
    assert ancestry._callee_refs_from_anchor_line("    message = 'helper()'") == frozenset()
    assert ancestry._callee_refs_from_anchor_line("    message = `helper()`") == frozenset()
    # Real call kept; literal decoy ignored.
    assert ancestry._callee_refs_from_anchor_line(
        '    return real_call("helper()")  # other()'
    ) == frozenset({(None, "real_call")})
    # JS private-field call is code, not a Python EOL comment.
    assert ancestry._callee_refs_from_anchor_line("    return this.#helper()") == frozenset(
        {(None, "helper")}
    )


@pytest.mark.unit
def test_diff_text_changes_definition_names_detects_def_forms() -> None:
    diff = "@@ -1,1 +1,1 @@\n-def helper():\n+def helper():\n"
    # Signature-adjacent body change still names the def on a +/- line when present.
    body_only = "@@ -2,1 +2,1 @@\n-    return 1\n+    return 2\n"
    assert ancestry._diff_text_changes_definition_names(diff, frozenset({"helper"})) is True
    assert ancestry._diff_text_changes_definition_names(body_only, frozenset({"helper"})) is False
    assert ancestry._diff_text_changes_definition_names(diff, frozenset({"other"})) is False
    assert ancestry._diff_text_changes_definition_names("", frozenset({"helper"})) is False
    arrow = "@@ -1,1 +1,1 @@\n-const helper = () => {\n+const helper = () => {\n"
    assert ancestry._diff_text_changes_definition_names(arrow, frozenset({"helper"})) is True


@pytest.mark.unit
def test_enclosing_definition_name_finds_nearest_def_above() -> None:
    text = "def helper():\n    return 1\n\ndef reviewed():\n    return helper()\n"
    assert ancestry._enclosing_definition_name(text, 2) == "helper"
    assert ancestry._enclosing_definition_name(text, 5) == "reviewed"
    assert ancestry._enclosing_definition_name(text, 0) is None
    assert ancestry._enclosing_definition_name("", 1) is None


@pytest.mark.unit
def test_enclosing_definition_name_recognizes_arrow_assignment() -> None:
    """JS/TS ``const helper = () =>`` bodies must count as enclosing definitions."""
    text = (
        "const helper = () => {\n  return 1;\n};\n\nfunction reviewed() {\n  return helper();\n}\n"
    )
    assert ancestry._enclosing_definition_name(text, 2) == "helper"
    assert ancestry._enclosing_definition_name(text, 6) == "reviewed"
    assert ancestry._enclosing_definition_identity(text, 2) == ("helper", 1)


@pytest.mark.unit
def test_resolve_callee_definition_span_includes_arrow_assignment_body() -> None:
    text = (
        "const helper = () => {\n  return 1;\n};\n\nfunction reviewed() {\n  return helper();\n}\n"
    )
    assert ancestry._resolve_callee_definition_span(
        text, call_line=6, qualifier=None, name="helper"
    ) == (1, 4)


@pytest.mark.unit
def test_resolve_callee_definition_span_prefers_in_scope_target() -> None:
    text = (
        "def helper():\n"
        "    return 99\n"
        "\n"
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
    )
    # Attribute call resolves to the method inside Foo, not the module helper.
    assert ancestry._resolve_callee_definition_span(
        text, call_line=9, qualifier="self", name="helper"
    ) == (5, 7)
    # Bare call below would resolve to nearest preceding module/class-visible def.
    bare = "def helper():\n    return 1\n\ndef reviewed():\n    return helper()\n"
    assert ancestry._resolve_callee_definition_span(
        bare, call_line=5, qualifier=None, name="helper"
    ) == (1, 3)


@pytest.mark.unit
def test_resolve_callee_definition_span_self_method_declared_after_call() -> None:
    """Same-class ``self.helper()`` must resolve even when ``helper`` is declared later."""
    text = (
        "class Foo:\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
        "\n"
        "    def helper(self):\n"
        "        return 1\n"
    )
    assert ancestry._resolve_callee_definition_span(
        text, call_line=3, qualifier="self", name="helper"
    ) == (5, 6)


@pytest.mark.unit
def test_resolve_callee_definition_span_self_skips_nested_class_method() -> None:
    """``self.helper()`` must not bind to a same-named method on a nested class."""
    only_nested = (
        "class Outer:\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
        "\n"
        "    class Inner:\n"
        "        def helper(self):\n"
        "            return 1\n"
    )
    assert (
        ancestry._resolve_callee_definition_span(
            only_nested, call_line=3, qualifier="self", name="helper"
        )
        is None
    )
    with_own = (
        "class Outer:\n"
        "    def reviewed(self):\n"
        "        return self.helper()\n"
        "\n"
        "    def helper(self):\n"
        "        return 2\n"
        "\n"
        "    class Inner:\n"
        "        def helper(self):\n"
        "            return 1\n"
    )
    assert ancestry._resolve_callee_definition_span(
        with_own, call_line=3, qualifier="self", name="helper"
    ) == (5, 7)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_skips_class_method() -> None:
    """Bare ``helper()`` must not bind to a nearer same-named class method."""
    text = (
        "def helper():\n"
        "    return 99\n"
        "\n"
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(self):\n"
        "        return helper()\n"
    )
    assert ancestry._resolve_callee_definition_span(
        text, call_line=9, qualifier=None, name="helper"
    ) == (1, 3)
    only_method = (
        "class Foo:\n"
        "    def helper(self):\n"
        "        return 1\n"
        "\n"
        "    def reviewed(self):\n"
        "        return helper()\n"
    )
    assert (
        ancestry._resolve_callee_definition_span(
            only_method, call_line=6, qualifier=None, name="helper"
        )
        is None
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_later_toplevel_helper() -> None:
    """Module-level helpers declared after the call site remain in scope for bare calls."""
    text = "def reviewed():\n    return helper()\n\ndef helper():\n    return 1\n"
    assert ancestry._resolve_callee_definition_span(
        text, call_line=2, qualifier=None, name="helper"
    ) == (4, 5)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_prefers_nested_helper() -> None:
    """Nested helpers defined before the call beat same-named top-level defs."""
    text = (
        "def helper():\n"
        "    return 99\n"
        "\n"
        "def reviewed():\n"
        "    def helper():\n"
        "        return 1\n"
        "    return helper()\n"
    )
    # Nested helper ends at its lexical body, not the sibling ``return helper()``.
    assert ancestry._resolve_callee_definition_span(
        text, call_line=7, qualifier=None, name="helper"
    ) == (5, 6)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_enclosing_function_helper() -> None:
    """Bare calls must resolve helpers defined in an enclosing function scope."""
    text = (
        "def outer():\n"
        "    def helper():\n"
        "        return 1\n"
        "    def reviewed():\n"
        "        return helper()\n"
    )
    assert ancestry._resolve_callee_definition_span(
        text, call_line=5, qualifier=None, name="helper"
    ) == (2, 3)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_prefers_inner_enclosing_helper() -> None:
    """Innermost enclosing helper wins over an outer same-named helper."""
    text = (
        "def outer():\n"
        "    def helper():\n"
        "        return 99\n"
        "    def mid():\n"
        "        def helper():\n"
        "            return 1\n"
        "        def reviewed():\n"
        "            return helper()\n"
    )
    assert ancestry._resolve_callee_definition_span(
        text, call_line=8, qualifier=None, name="helper"
    ) == (5, 6)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_skips_sibling_nested_helper() -> None:
    """Helpers local to a sibling nested def are not in scope for bare calls."""
    text = (
        "def outer():\n"
        "    def sibling():\n"
        "        def helper():\n"
        "            return 1\n"
        "    def reviewed():\n"
        "        return helper()\n"
        "\n"
        "def helper():\n"
        "    return 99\n"
    )
    assert ancestry._resolve_callee_definition_span(
        text, call_line=6, qualifier=None, name="helper"
    ) == (8, 9)


@pytest.mark.unit
def test_resolve_callee_definition_span_bare_call_indented_module_helper() -> None:
    """Module-scope helpers under ``if`` (indent > 0) remain bare-callable."""
    text = (
        "if True:\n"
        "    def helper():\n"
        "        return 1\n"
        "    def reviewed():\n"
        "        return helper()\n"
    )
    assert ancestry._resolve_callee_definition_span(
        text, call_line=5, qualifier=None, name="helper"
    ) == (2, 3)
    if_body = "if True:\n    def helper():\n        return 1\n    x = helper()\n"
    assert ancestry._resolve_callee_definition_span(
        if_body, call_line=4, qualifier=None, name="helper"
    ) == (2, 3)


@pytest.mark.unit
def test_iter_definition_spans_stops_at_module_level_assignment_dedent() -> None:
    """Ordinary dedents (not only the next def) end a definition span."""
    text = "def helper():\n    return 1\n\nX = 1\n\ndef reviewed():\n    return helper()\n"
    spans = {
        name: (start, end) for name, start, end, _indent in ancestry._iter_definition_spans(text)
    }
    assert spans["helper"] == (1, 3)
    assert spans["reviewed"] == (6, 7)
    helper_span = ancestry._resolve_callee_definition_span(
        text, call_line=7, qualifier=None, name="helper"
    )
    assert helper_span == (1, 3)
    # Unrelated module assignment must not count as FIXED callee-body evidence.
    assert (
        ancestry._diff_hunk_overlaps_line_span("@@ -4,1 +4,1 @@\n-X = 1\n+X = 2\n", *helper_span)
        is False
    )


@pytest.mark.unit
def test_resolve_callee_definition_span_rejects_unsupported_qualifier() -> None:
    """Non-self/cls receivers must fail closed, not bind an unrelated bare def."""
    text = "def send():\n    return 99\n\ndef reviewed():\n    return client.send()\n"
    assert ancestry._callee_refs_from_anchor_line("    return client.send()") == frozenset(
        {("client", "send")}
    )
    assert (
        ancestry._resolve_callee_definition_span(text, call_line=5, qualifier="client", name="send")
        is None
    )


@pytest.mark.unit
def test_diff_hunk_overlaps_definition_span() -> None:
    assert ancestry._diff_hunk_overlaps_line_span("@@ -2,1 +2,1 @@\n", 1, 3) is True
    assert ancestry._diff_hunk_overlaps_line_span("@@ -10,1 +10,1 @@\n", 1, 3) is False
    assert ancestry._diff_hunk_overlaps_line_span("@@ -2,0 +3,1 @@\n", 1, 3) is True


@pytest.mark.unit
def test_diff_hunk_related_line_evidence_combines_exact_and_near() -> None:
    same_fn = (
        "def reviewed():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
        "    f = 6\n"
        "    do_work()\n"
    )
    exact = "@@ -8,1 +8,1 @@\n-    do_work()\n+    do_work(1)\n"
    near = "@@ -3,0 +4,2 @@\n+    if not ready:\n+        return\n"
    distant = "@@ -40,1 +40,1 @@\n-    other()\n+    other(1)\n"
    assert ancestry._diff_hunk_related_line_evidence(exact, 8, file_text=same_fn) is True
    assert ancestry._diff_hunk_related_line_evidence(near, 8, file_text=same_fn) is True
    assert ancestry._diff_hunk_related_line_evidence(distant, 8, file_text=same_fn) is False
    assert ancestry._diff_hunk_related_line_evidence(near, 0, file_text=same_fn) is False


@pytest.mark.unit
async def test_commit_range_in_item_scope_accepts_blank_item_path() -> None:
    runner = SimpleNamespace(_deps=SimpleNamespace(runner=FakeCommandRunner()))
    assert (
        await ancestry._commit_range_in_item_scope(
            runner,
            worktree_path=Path("/tmp/repo"),
            left="aaa",
            right="bbb",
            item_path=" ",
        )
        is True
    )
