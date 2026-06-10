"""Worker-runtime wiring for the Claude-base reaper (#509).

Split out of ``test_worker.py`` to keep that module under the first-party
file line limit (``test_first_party_code_files_stay_under_line_limit``). Shares
the ``_settings`` / ``_in_process_merge_coordinator`` builders with the parent
module.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from awf.service import worker as worker_mod
from tests.unit.service.test_worker import (
    _in_process_merge_coordinator,
    _settings,
)


@pytest.mark.unit
def test_build_worker_runtime_wires_claude_base_reaper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The worker is wired with a Claude-base reaper closure that actually reaps (#509).

    The closure must drive the real ``reap_superseded_claude_bases`` with
    ``execute=True`` and the runtime's resolved ``host_home``/``work_dir``, so a
    superseded, unmounted, unpinned base seeded under those paths is reclaimed when
    the closure runs (the worker holds CAP_SYS_ADMIN; here a True capability probe
    stands in for it). The kill-switch flag must also flow into ``WorkerConfig``.
    """
    from awf.node.auth_mounts import _host_claude_signature, _shared_claude_base_dir

    created: dict[str, Any] = {}

    class _Engine:
        pass

    class _AnyInit:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

    class _ControlWorker:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created["claude_base_reaper"] = kwargs["claude_base_reaper"]
            created["worker_config"] = kwargs["config"]

    session_factory = object()
    monkeypatch.setattr(worker_mod, "make_engine", lambda _url: _Engine())
    monkeypatch.setattr(worker_mod, "make_session_factory", lambda _engine: session_factory)
    for name in (
        "AsyncioSubprocessRunner",
        "LogStore",
        "ValidationRunner",
        "PullRequestCreator",
        "BranchOpenPullRequestResolver",
        "GitManager",
        "ComposeManager",
        "ServiceAuthMountResolver",
        "LocalSecretLeaseMountResolver",
        "ComposeStackLauncher",
        "Provisioner",
        "WorkspaceExecutor",
        "CcusageCollector",
    ):
        monkeypatch.setattr(worker_mod, name, _AnyInit)
    monkeypatch.setattr(worker_mod, "ControlWorker", _ControlWorker)
    monkeypatch.setattr(
        worker_mod, "_merge_coordinator_for_database_url", _in_process_merge_coordinator
    )
    monkeypatch.setattr(worker_mod, "_companion_image_builder_for", lambda *_a, **_k: None)
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    monkeypatch.setattr(worker_mod, "_apply_service_git_environment", lambda _env: None)
    monkeypatch.setattr(worker_mod, "build_default_compose_teardown", lambda _manager: object())
    monkeypatch.setattr(worker_mod, "build_orphan_compose_teardown", lambda _manager: object())

    settings = _settings(tmp_path)
    work_dir = Path(settings.work_dir).resolve()
    host_home = Path(settings.host_home).resolve()

    # Seed a host ``~/.claude`` (current signature, protected) and a superseded,
    # unmounted, unpinned base under the runtime's resolved work dir.
    claude = host_home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text('{"theme": "dark"}\n')
    sig_current = _host_claude_signature(host_home)
    current_base = _shared_claude_base_dir(work_dir, sig_current)
    current_base.mkdir(parents=True)
    (current_base / "blob").write_text("x" * 16)
    superseded_base = _shared_claude_base_dir(work_dir, "sigdsupersede000")
    superseded_base.mkdir(parents=True)
    (superseded_base / "blob").write_text("y" * 16)

    # The runtime resolves the real ``/proc/mounts``; force a trustworthy capability
    # probe so the no-overlay host environment cannot mark the base unverifiable
    # (mirrors the worker's CAP_SYS_ADMIN vantage).
    monkeypatch.setattr("awf.service.gc_claude_base._has_cap_sys_admin", lambda: True)

    worker_mod.build_worker_runtime(settings)

    assert created["worker_config"].claude_base_gc_enabled is settings.claude_base_gc_enabled
    reaper = created["claude_base_reaper"]
    assert reaper is not None
    assert callable(reaper)

    report = asyncio.run(reaper())

    assert report["reaped"] == ["sigdsupersede000"]
    assert not superseded_base.parent.exists()
    assert current_base.is_dir()
