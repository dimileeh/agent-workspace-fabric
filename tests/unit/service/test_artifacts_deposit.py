"""Deposit-of-planning-artifacts service tests.

Split from ``test_artifacts.py`` to keep first-party files under the
maintainability line limit (see ``test_core_decomposition_maintainability``).
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

import awf.service.artifacts as artifacts_module
from awf.service.artifacts import (
    DEPOSITED_CONFORMANCE_NAME,
    DEPOSITED_PLAN_NAME,
    MAX_ARTIFACT_CONTENT_BYTES,
    _open_planning_source_under_root,
    _workspace_artifact_dir,
    deposit_workspace_planning_artifacts,
    workspace_artifact_dir,
)


class TestDepositWorkspacePlanningArtifacts:
    """Deposit of the worktree plan + conformance report into the served dir."""

    @staticmethod
    def _seed_worktree(
        tmp_path: Path,
        *,
        plan_text: str | None = None,
        report_text: str | None = None,
    ) -> tuple[Path, Path, Path]:
        worktree = tmp_path / "work" / "worktrees" / "ws_dep"
        plan_path = Path("docs/awf-plans/ws_dep.md")
        report_path = Path("docs/awf-plans/ws_dep.conformance.json")
        if plan_text is not None or report_text is not None:
            (worktree / "docs" / "awf-plans").mkdir(parents=True, exist_ok=True)
        if plan_text is not None:
            (worktree / plan_path).write_text(plan_text, encoding="utf-8")
        if report_text is not None:
            (worktree / report_path).write_text(report_text, encoding="utf-8")
        return worktree, plan_path, report_path

    @pytest.mark.unit
    def test_deposits_both_plan_and_conformance(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "work"
        plan_text = "# Plan\n\n- step one\n"
        report_text = '{"satisfied": true, "summary": "done"}'
        worktree, plan_path, report_path = self._seed_worktree(
            tmp_path, plan_text=plan_text, report_text=report_text
        )

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert (artifact_dir / DEPOSITED_PLAN_NAME).read_text(encoding="utf-8") == plan_text
        assert (artifact_dir / DEPOSITED_CONFORMANCE_NAME).read_text(
            encoding="utf-8"
        ) == report_text

    @pytest.mark.unit
    def test_idempotent_overwrite_on_rerun(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(
            tmp_path, plan_text="first", report_text="{}"
        )

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )
        (worktree / plan_path).write_text("second", encoding="utf-8")
        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert (artifact_dir / DEPOSITED_PLAN_NAME).read_text(encoding="utf-8") == "second"

    @pytest.mark.unit
    def test_plan_only_present_deposits_only_plan(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(tmp_path, plan_text="# Plan")

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert (artifact_dir / DEPOSITED_PLAN_NAME).is_file()
        assert not (artifact_dir / DEPOSITED_CONFORMANCE_NAME).exists()

    @pytest.mark.unit
    def test_report_only_present_deposits_only_conformance(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(tmp_path, report_text="{}")

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert (artifact_dir / DEPOSITED_CONFORMANCE_NAME).is_file()
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()

    @pytest.mark.unit
    def test_no_deposit_and_no_dir_when_sources_absent(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(tmp_path)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        assert not workspace_artifact_dir(work_dir, "ws_dep").exists()

    @pytest.mark.unit
    def test_unsatisfied_conformance_still_deposits_report(self, tmp_path: Path) -> None:
        work_dir = tmp_path / "work"
        report_text = '{"satisfied": false, "gaps": ["missing tests"]}'
        worktree, plan_path, report_path = self._seed_worktree(tmp_path, report_text=report_text)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert (artifact_dir / DEPOSITED_CONFORMANCE_NAME).read_text(
            encoding="utf-8"
        ) == report_text

    @pytest.mark.unit
    def test_deposited_artifacts_survive_worktree_teardown(self, tmp_path: Path) -> None:
        import shutil as shutil_module

        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(
            tmp_path, plan_text="# Plan", report_text="{}"
        )

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )
        # Successful-workspace teardown removes the worktree; the served
        # artifact dir is a sibling, so the deposited copies must survive.
        shutil_module.rmtree(worktree)

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert (artifact_dir / DEPOSITED_PLAN_NAME).is_file()
        assert (artifact_dir / DEPOSITED_CONFORMANCE_NAME).is_file()

    @pytest.mark.unit
    def test_copy_failure_is_non_fatal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(
            tmp_path, plan_text="# Plan", report_text="{}"
        )

        def boom(src: Any, dst: Any, *args: Any, **kwargs: Any) -> bool:
            raise OSError("disk full")

        monkeypatch.setattr(artifacts_module, "_copy_capped", boom)

        # Must not raise despite every copy failing.
        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()
        assert not (artifact_dir / DEPOSITED_CONFORMANCE_NAME).exists()

    @pytest.mark.unit
    def test_symlinked_source_outside_worktree_is_rejected(self, tmp_path: Path) -> None:
        # An agent-controlled worktree could leave the plan as a symlink to an
        # arbitrary host-readable file; the deposit step must refuse to copy it.
        work_dir = tmp_path / "work"
        secret = tmp_path / "host-secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        worktree, plan_path, report_path = self._seed_worktree(tmp_path)
        (worktree / "docs" / "awf-plans").mkdir(parents=True, exist_ok=True)
        (worktree / plan_path).symlink_to(secret)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()

    @pytest.mark.unit
    def test_hard_linked_source_is_rejected(self, tmp_path: Path) -> None:
        # A hard link shares its inode with an arbitrary host file, so it slips
        # past the symlink and escape guards while still copying that file's
        # contents into the served artifact dir. The deposit step must refuse a
        # multi-linked source, mirroring the content reader's st_nlink guard.
        work_dir = tmp_path / "work"
        secret = tmp_path / "host-secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        worktree, plan_path, report_path = self._seed_worktree(tmp_path)
        (worktree / "docs" / "awf-plans").mkdir(parents=True, exist_ok=True)
        os.link(secret, worktree / plan_path)
        assert (worktree / plan_path).stat().st_nlink > 1

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()

    @pytest.mark.unit
    def test_oversized_source_is_rejected(self, tmp_path: Path) -> None:
        # A buggy or malicious agent could emit an arbitrarily large plan and
        # have ``copyfile`` fill the served artifact dir while synchronously
        # blocking the executor. The deposit step must refuse a source larger
        # than ``MAX_ARTIFACT_CONTENT_BYTES`` (the cap the reader enforces).
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(tmp_path)
        (worktree / "docs" / "awf-plans").mkdir(parents=True, exist_ok=True)
        (worktree / plan_path).write_bytes(b"x" * (MAX_ARTIFACT_CONTENT_BYTES + 1))
        (worktree / report_path).write_text("{}", encoding="utf-8")

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        # Oversized plan refused, but the small conformance report still deposits.
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()
        assert (artifact_dir / DEPOSITED_CONFORMANCE_NAME).exists()

    @pytest.mark.unit
    def test_source_at_size_cap_is_deposited(self, tmp_path: Path) -> None:
        # A source exactly at the cap is still readable by the content reader,
        # so it must be deposited rather than rejected.
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(tmp_path)
        (worktree / "docs" / "awf-plans").mkdir(parents=True, exist_ok=True)
        (worktree / plan_path).write_bytes(b"x" * MAX_ARTIFACT_CONTENT_BYTES)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert (artifact_dir / DEPOSITED_PLAN_NAME).exists()

    @pytest.mark.unit
    def test_source_growing_after_size_check_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TOCTOU: ``fstat`` only samples the size *before* the copy starts. A
        # process still appending to the open descriptor can stream the file past
        # ``MAX_ARTIFACT_CONTENT_BYTES`` during an unbounded copy, leaving an
        # oversized artifact the reader can never serve. Simulate a stale small
        # ``fstat`` over a source whose real bytes exceed the cap: the bounded
        # copy must refuse it rather than deposit the oversized contents.
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(tmp_path)
        (worktree / "docs" / "awf-plans").mkdir(parents=True, exist_ok=True)
        (worktree / plan_path).write_bytes(b"x" * (MAX_ARTIFACT_CONTENT_BYTES + 1))
        (worktree / report_path).write_text("{}", encoding="utf-8")

        real_fstat = artifacts_module.os.fstat

        class _UndersizedStat:
            # Report a size under the cap so the pre-copy guard waves the source
            # through, while delegating every other field to the real ``fstat``.
            def __init__(self, real: os.stat_result) -> None:
                self._real = real
                self.st_size = 1

            def __getattr__(self, name: str) -> Any:
                return getattr(self._real, name)

        def small_fstat(fd: int) -> Any:
            return _UndersizedStat(real_fstat(fd))

        monkeypatch.setattr(artifacts_module.os, "fstat", small_fstat)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        # Oversized plan refused by the bounded copy; the small report still lands.
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()
        assert not (artifact_dir / f".{DEPOSITED_PLAN_NAME}.tmp").exists()
        assert (artifact_dir / DEPOSITED_CONFORMANCE_NAME).exists()

    @pytest.mark.unit
    def test_source_escaping_worktree_via_dir_symlink_is_rejected(self, tmp_path: Path) -> None:
        # A regular plan file reached through an intermediate directory symlink
        # that points outside the worktree must also be refused.
        work_dir = tmp_path / "work"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "ws_dep.md").write_text("escaped plan", encoding="utf-8")
        worktree, _, report_path = self._seed_worktree(tmp_path)
        worktree.mkdir(parents=True, exist_ok=True)
        (worktree / "docs").mkdir(parents=True, exist_ok=True)
        (worktree / "docs" / "awf-plans").symlink_to(outside, target_is_directory=True)
        plan_path = Path("docs/awf-plans/ws_dep.md")

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()

    @pytest.mark.unit
    def test_parent_dir_swapped_to_symlink_after_checks_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TOCTOU: a benign plan under a real intermediate directory passes the
        # resolve()/escape checks, then a racing agent swaps the *parent*
        # directory for a symlink pointing outside the worktree before the bytes
        # are opened. ``O_NOFOLLOW`` guards only the final component, so opening
        # the resolved pathname would follow the swapped parent and exfiltrate a
        # host file; the dir_fd component walk must refuse the swapped parent.
        import shutil as shutil_module

        work_dir = tmp_path / "work"
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "ws_dep.md").write_text("escaped plan", encoding="utf-8")
        worktree, plan_path, report_path = self._seed_worktree(tmp_path, plan_text="benign")
        parent_dir = (worktree / plan_path).parent
        worktree_root = worktree.resolve()

        real_os_open = artifacts_module.os.open
        _swapped: set[bool] = set()

        def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            # On the first descriptor the copy walk opens (the worktree root,
            # after resolve() canonicalised the real parent), replace the real
            # intermediate directory with an outside symlink of the same name.
            if not _swapped and Path(path) == worktree_root and parent_dir.is_dir():
                _swapped.add(True)
                shutil_module.rmtree(parent_dir)
                parent_dir.symlink_to(outside, target_is_directory=True)
            return real_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(artifacts_module.os, "open", swapping_open)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        assert _swapped, "swap hook never fired"
        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()

    @pytest.mark.unit
    def test_source_swapped_to_hard_link_after_checks_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TOCTOU: a benign plan passes the path-based symlink/escape checks, then
        # a racing agent process replaces it with a hard link to a host secret
        # just before the bytes are copied. Validating + copying from the opened
        # descriptor must catch the swap (st_nlink > 1) rather than depositing
        # the secret a stale path-based stat would have waved through.
        work_dir = tmp_path / "work"
        secret = tmp_path / "host-secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")
        worktree, plan_path, report_path = self._seed_worktree(tmp_path, plan_text="benign")
        target = worktree / plan_path

        real_os_open = artifacts_module.os.open
        _swapped: set[bool] = set()

        def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            # The copy opens the leaf via a ``dir_fd`` component walk, so key the
            # swap on that final (non-directory) ``openat`` rather than a full path.
            if (
                kwargs.get("dir_fd") is not None
                and not flags & os.O_DIRECTORY
                and target.exists()
                and not _swapped
            ):
                _swapped.add(True)
                target.unlink()
                os.link(secret, target)
            return real_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(artifacts_module.os, "open", swapping_open)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()

    @pytest.mark.unit
    def test_source_swapped_to_non_regular_file_after_checks_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The opened descriptor must be a regular file: a swap to a directory (or
        # other non-regular inode) after the path checks must be refused rather
        # than handed to the copy.
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(tmp_path, plan_text="benign")
        target = worktree / plan_path

        real_os_open = artifacts_module.os.open

        def swapping_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            # Swap the leaf for a directory on the final (non-directory) ``openat``
            # of the ``dir_fd`` component walk, after the path checks resolved it.
            if kwargs.get("dir_fd") is not None and not flags & os.O_DIRECTORY and target.is_file():
                target.unlink()
                target.mkdir()
            return real_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(artifacts_module.os, "open", swapping_open)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()

    @pytest.mark.unit
    def test_open_under_root_rejects_resolved_equal_to_root(self, tmp_path: Path) -> None:
        # A resolved path that collapsed onto ``worktree_root`` itself leaves no
        # component to open. The walk must raise ``OSError`` (caught fail-closed
        # by the deposit) rather than a bare ``IndexError`` from ``rel_parts[-1]``.
        worktree_root = (tmp_path / "work" / "worktrees" / "ws_dep").resolve()
        worktree_root.mkdir(parents=True, exist_ok=True)

        with pytest.raises(OSError) as excinfo:
            _open_planning_source_under_root(worktree_root=worktree_root, resolved=worktree_root)

        assert not isinstance(excinfo.value, IndexError)
        assert excinfo.value.errno == errno.EINVAL

    @pytest.mark.unit
    def test_source_resolving_onto_worktree_root_after_checks_is_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # TOCTOU: a benign regular plan passes the symlink/is_file checks, then a
        # racing swap of the final component to a symlink pointing at the worktree
        # root collapses ``resolve()`` onto ``worktree_root`` (empty rel_parts).
        # The deposit must log-and-skip fail-closed — never raise out and fail the
        # workspace — while an unraced sibling (the report) still lands.
        work_dir = tmp_path / "work"
        worktree, plan_path, report_path = self._seed_worktree(
            tmp_path, plan_text="benign", report_text="{}"
        )
        worktree_root = worktree.resolve()
        plan_source = worktree / plan_path
        real_resolve = Path.resolve

        def collapsing_resolve(self: Path, *args: Any, **kwargs: Any) -> Path:
            # Only the raced plan source collapses onto the root; everything else
            # (including ``worktree_root`` itself) resolves normally.
            if self == plan_source:
                return worktree_root
            return real_resolve(self, *args, **kwargs)

        monkeypatch.setattr(artifacts_module.Path, "resolve", collapsing_resolve)

        deposit_workspace_planning_artifacts(
            work_dir=work_dir,
            workspace_id="ws_dep",
            worktree_path=worktree,
            plan_path=plan_path,
            report_path=report_path,
        )

        artifact_dir = workspace_artifact_dir(work_dir, "ws_dep")
        assert not (artifact_dir / DEPOSITED_PLAN_NAME).exists()
        assert (artifact_dir / DEPOSITED_CONFORMANCE_NAME).exists()

    @pytest.mark.unit
    def test_served_dir_matches_api_resolution(self, tmp_path: Path) -> None:
        # The executor passes ``compose_projects_root.parent`` as work_dir; the
        # API resolves the served dir from the same work_dir. Guard the
        # ``.parent`` derivation against drift.
        work_dir = tmp_path / "work"
        compose_projects_root = work_dir / "compose"
        assert workspace_artifact_dir(compose_projects_root.parent, "ws_dep") == (
            _workspace_artifact_dir("ws_dep", work_dir=work_dir)
        )
