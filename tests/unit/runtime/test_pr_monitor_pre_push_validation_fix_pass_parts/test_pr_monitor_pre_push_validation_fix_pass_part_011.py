"""Related near-anchor / call-site helper FIXED evidence (part 11)."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from awf.adapters.base import AgentRunResult
from awf.common.commands import AsyncioSubprocessRunner
from awf.db.session import make_session_factory
from awf.runtime.pr_monitor_runner import comment_verdict
from awf.runtime.pr_monitor_runner.comment_verdict import (
    AGENT_FIXED_WITHOUT_EVIDENCE,
    AgentVerdictProtocolError,
)
from tests.postgres import postgres_test_engine
from tests.unit.runtime._monitor_runner_fixtures import (
    FakeAdapter,
    RecordedSleep,
    make_runner,
)


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


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "awf@example.com")
    _git(repo, "config", "user.name", "AWF Test")


@pytest.mark.unit
async def test_commit_range_touches_path_accepts_guard_before_review_anchor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Guard inserted several lines above the review line counts as FIXED evidence."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    # Review anchor is do_work() at line 8. Exact matcher only accepts overlap at
    # line 8 or a pure insert at line 8 / line 7. A guard several lines earlier
    # must still count as related near-anchor evidence.
    target.write_text(
        "\n".join(
            [
                "def reviewed():",
                "    a = 1",
                "    b = 2",
                "    c = 3",
                "    d = 4",
                "    e = 5",
                "    f = 6",
                "    do_work()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    lines = target.read_text(encoding="utf-8").splitlines()
    # Insert a two-line guard after line 3 (old_start=3,0) — not line/line-1.
    lines[3:3] = ["    if not ready:", "        return"]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "guard before anchor")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=tip,
        path="src/target.py",
        line=8,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_rejects_unrelated_nearby_insert(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Pure insert in a neighboring function within the proximity window is not evidence."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    target.write_text(
        "\n".join(
            [
                "def other():",
                "    x = 1",
                "    y = 2",
                "    z = 3",
                "",
                "def reviewed():",
                "    a = 1",
                "    do_work()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    review_line = target.read_text(encoding="utf-8").splitlines().index("    do_work()") + 1

    lines = target.read_text(encoding="utf-8").splitlines()
    # Insert inside other() after line 2 — still within 12 lines of do_work().
    lines[2:2] = ["    unrelated = True"]
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "unrelated nearby insert")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=tip,
        path="src/target.py",
        line=review_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_accepts_helper_def_at_call_site_anchor(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Changing a callee body counts when the review line is its call site."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    # Helper body is far above the call site so near-anchor proximity alone
    # cannot accept the edit — call-site→definition linking must.
    padding = [f"    # pad {i}" for i in range(20)]
    target.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                *padding,
                "    return helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    call_site_line = (
        target.read_text(encoding="utf-8").splitlines().index("    return helper()") + 1
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return 2"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "fix helper body")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=tip,
        path="src/target.py",
        line=call_site_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_rejects_unrelated_same_file_region(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Edits in a different, non-referenced region must not count as FIXED evidence."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    padding = [f"# spacer {i}" for i in range(20)]
    target.write_text(
        "\n".join(
            [
                "def unrelated_helper():",
                "    return 1",
                "",
                *padding,
                "def reviewed():",
                "    return helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    review_line = target.read_text(encoding="utf-8").splitlines().index("    return helper()") + 1

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return 99"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "unrelated helper body")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=tip,
        path="src/target.py",
        line=review_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_rejects_same_named_unrelated_definition(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Changing an unrelated same-named def must not satisfy self.helper() call-site evidence."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    padding = [f"        # pad {i}" for i in range(20)]
    target.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 99",
                "",
                "class Foo:",
                "    def helper(self):",
                "        return 1",
                "",
                "    def reviewed(self):",
                *padding,
                "        return self.helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    call_site_line = (
        target.read_text(encoding="utf-8").splitlines().index("        return self.helper()") + 1
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return 0"  # module-level helper, not Foo.helper
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "unrelated same-named helper")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert not await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=tip,
        path="src/target.py",
        line=call_site_line,
    )


@pytest.mark.unit
async def test_commit_range_touches_path_accepts_attribute_helper_method(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Changing the in-class method referenced by self.helper() still counts."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    padding = [f"        # pad {i}" for i in range(20)]
    target.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 99",
                "",
                "class Foo:",
                "    def helper(self):",
                "        return 1",
                "",
                "    def reviewed(self):",
                *padding,
                "        return self.helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    call_site_line = (
        target.read_text(encoding="utf-8").splitlines().index("        return self.helper()") + 1
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    method_body = lines.index("    def helper(self):") + 1
    lines[method_body] = "        return 2"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "fix Foo.helper")
    tip = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=start,
        right=tip,
        path="src/target.py",
        line=call_site_line,
    )


@pytest.mark.unit
async def test_first_attempt_fixed_accepts_related_helper_repair_without_retry(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid self-commit fixing a helper for a call-site review needs no correction."""
    worktree = tmp_path / "worktrees" / "ws_protocol"
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "src").mkdir()
    target = worktree / "src" / "mod.py"
    padding = [f"    # pad {i}" for i in range(20)]
    target.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                *padding,
                "    return helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "item start")
    item_start = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    call_site_line = (
        target.read_text(encoding="utf-8").splitlines().index("    return helper()") + 1
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return 2"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "related helper repair")
    fixed_tip = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    agent_calls = 0

    async def _ok(**_kwargs: object) -> bool:
        return True

    async def _fixed_agent(**_kwargs: object) -> AgentRunResult:
        nonlocal agent_calls
        agent_calls += 1
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FIXED: repaired helper used at the review line",
            stderr="",
        )

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)
    runner._run_monitor_agent_with_service_recovery = _fixed_agent  # type: ignore[method-assign]

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="fix the helper used at the review line",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=item_start,
        evidence_item_path="src/mod.py",
        evidence_item_line=call_site_line,
        evidence_anchor_head=item_start,
        commit_dirty_changes=False,
    )

    assert result.verdict == "fix_committed"
    assert agent_calls == 1
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == fixed_tip


@pytest.mark.unit
async def test_later_item_cannot_inherit_earlier_related_helper_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    """Item 2 FIXED with no new related delta must not inherit item 1's commit."""
    import awf.runtime.pr_monitor_runner.pre_push_validation as pre_push_validation

    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "src").mkdir()
    target = repo / "src" / "target.py"
    padding = [f"    # pad {i}" for i in range(20)]
    target.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                *padding,
                "    return helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "base")
    item1_start = _git(repo, "rev-parse", "HEAD").stdout.strip()
    call_site_line = (
        target.read_text(encoding="utf-8").splitlines().index("    return helper()") + 1
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return 2"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(repo, "add", "src/target.py")
    _git(repo, "commit", "-qm", "item1 related repair")
    item2_start = _git(repo, "rev-parse", "HEAD").stdout.strip()

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    # Item 1 range includes the related repair.
    assert await pre_push_validation._commit_range_touches_path(
        runner,
        worktree_path=repo,
        left=item1_start,
        right=item2_start,
        path="src/target.py",
        line=call_site_line,
    )
    # Item 2 start == current tip: no new delta for item 2.
    assert not await comment_verdict._item_fix_evidence(
        runner,
        worktree_path=repo,
        item_start_head=item2_start,
        item_path="src/target.py",
        item_line=call_site_line,
        state=None,
        dirty_changes_committed=False,
    )


@pytest.mark.unit
async def test_first_attempt_false_positive_still_rolls_back_related_commit(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-FIXED first-attempt verdicts still discard commits (rollback unchanged)."""
    worktree = tmp_path / "worktrees" / "ws_protocol"
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "src").mkdir()
    target = worktree / "src" / "mod.py"
    padding = [f"    # pad {i}" for i in range(20)]
    target.write_text(
        "\n".join(
            [
                "def helper():",
                "    return 1",
                "",
                "def reviewed():",
                *padding,
                "    return helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "item start")
    item_start = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    call_site_line = (
        target.read_text(encoding="utf-8").splitlines().index("    return helper()") + 1
    )

    lines = target.read_text(encoding="utf-8").splitlines()
    lines[1] = "    return 2"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "related helper repair")

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )

    async def _ok(**_kwargs: object) -> bool:
        return True

    async def _fp_agent(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(
            returncode=0,
            stdout="AWF-VERDICT: FALSE POSITIVE: existing behavior is correct",
            stderr="",
        )

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)
    runner._run_monitor_agent_with_service_recovery = _fp_agent  # type: ignore[method-assign]

    result = await comment_verdict._invoke_cli_for_verdict_result(
        runner,
        workspace_id="ws_protocol",
        prompt="review the helper call",
        commit_message="fix: review item",
        compose_project="awf_ws_protocol",
        compose_file=Path("compose.yml"),
        operation_start_head=item_start,
        evidence_item_path="src/mod.py",
        evidence_item_line=call_site_line,
        evidence_anchor_head=item_start,
        commit_dirty_changes=False,
    )

    assert result.verdict == "false_positive"
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == item_start


@pytest.mark.unit
async def test_malformed_first_attempt_with_related_commit_still_retries_then_rolls_back(
    factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed then FIXED-without-new-delta still ends without inheriting evidence."""
    worktree = tmp_path / "worktrees" / "ws_protocol"
    worktree.mkdir(parents=True)
    _git(worktree, "init", "-q")
    _git(worktree, "config", "user.email", "awf@example.com")
    _git(worktree, "config", "user.name", "AWF Test")
    (worktree / "src").mkdir()
    target = worktree / "src" / "mod.py"
    target.write_text("def reviewed():\n    return None\n", encoding="utf-8")
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "item start")
    item_start = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    # Unrelated distant edit that never satisfies line-related evidence.
    target.write_text(
        "def other():\n    return 1\n\ndef reviewed():\n    return None\n",
        encoding="utf-8",
    )
    _git(worktree, "add", "src/mod.py")
    _git(worktree, "commit", "-qm", "unrelated top edit")

    runner = make_runner(
        factory=factory,
        cmd=AsyncioSubprocessRunner(),
        adapter=FakeAdapter(),
        sleep_fn=RecordedSleep(),
        worktrees_root=tmp_path / "worktrees",
    )
    outputs = iter(
        [
            "garbled output without verdict",
            "AWF-VERDICT: FIXED: claimed after malformed attempt",
        ]
    )

    async def _ok(**_kwargs: object) -> bool:
        return True

    async def _agent(**_kwargs: object) -> AgentRunResult:
        return AgentRunResult(returncode=0, stdout=next(outputs), stderr="")

    monkeypatch.setattr(comment_verdict, "repair_agent_runtime_ownership", _ok)
    monkeypatch.setattr(comment_verdict, "mirror_path_for_worktree", lambda _path: None)
    runner._run_monitor_agent_with_service_recovery = _agent  # type: ignore[method-assign]

    with pytest.raises(AgentVerdictProtocolError) as caught:
        await comment_verdict._invoke_cli_for_verdict_result(
            runner,
            workspace_id="ws_protocol",
            prompt="fix reviewed",
            commit_message="fix: review item",
            compose_project="awf_ws_protocol",
            compose_file=Path("compose.yml"),
            operation_start_head=item_start,
            evidence_item_path="src/mod.py",
            evidence_item_line=2,
            evidence_anchor_head=item_start,
            commit_dirty_changes=False,
        )

    assert caught.value.reason_code == AGENT_FIXED_WITHOUT_EVIDENCE
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() == item_start
