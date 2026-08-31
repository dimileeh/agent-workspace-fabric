"""Mount-propagation posture surfacing in collect_service_status (#400)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from awf.service import status as status_mod
from awf.service.config import ServiceSettings
from awf.service.orphans import WorkspaceIdView


@dataclass(frozen=True)
class _DiskUsage:
    total: int
    used: int
    free: int


class _Response:
    status_code = 200

    @staticmethod
    def json() -> dict[str, str]:
        return {"status": "ok"}


async def _api_get(_url: str, *, timeout: float) -> _Response:
    return _Response()


async def _db_probe(_database_url: str) -> dict[str, Any]:
    return {"ok": True, "status": "ok", "reason": "DB_REACHABLE"}


async def _empty_workspace_view(_database_url: str) -> WorkspaceIdView:
    return WorkspaceIdView(active_ids=frozenset(), terminal_ids=frozenset(), available=True)


def _run_subprocess_missing_docker(_args: list[str], **_kwargs: object) -> Any:
    raise FileNotFoundError("docker")


def _settings(tmp_path: Path) -> ServiceSettings:
    return ServiceSettings(
        service_name="awf",
        env="local",
        api_base_url="http://localhost:8000",
        database_url="postgresql+asyncpg://awf:pw@localhost:5433/awf",
        docker_host=f"unix://{tmp_path / 'docker.sock'}",
        agent_runtime_image="awf-agent-runtime:latest",
        work_dir=str(tmp_path / "work"),
        api_token=None,
        github_token=None,
        worker_poll_interval_seconds=0.1,
        worker_max_concurrent_provisions=1,
        host_home=str(tmp_path / "home"),
    )


@pytest.mark.unit
def test_mount_propagation_check_from_environ() -> None:
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={
            "AWF_WORK_DIR_BIND_PROPAGATION": "rshared",
            "AWF_CLAUDE_AUTH_FORCE_COPY": "false",
        },
        compose_env_file=None,
    )
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["reason"] == "MOUNT_PROPAGATION_AVAILABLE"
    assert payload["propagation"] == "rshared"
    assert payload["force_copy"] is False


@pytest.mark.unit
def test_mount_propagation_check_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\nAWF_CLAUDE_AUTH_FORCE_COPY=true\n",
        encoding="utf-8",
    )
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=env_file,
    )
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["propagation"] == "rprivate"
    assert payload["force_copy"] is True


@pytest.mark.unit
def test_mount_propagation_check_unknown_when_missing() -> None:
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=None,
    )
    assert payload["ok"] is True
    assert payload["status"] == "unknown"
    assert payload["reason"] == "MOUNT_PROPAGATION_UNKNOWN"
    assert payload["propagation"] is None
    assert payload["force_copy"] is None


@pytest.mark.unit
def test_mount_propagation_check_environ_takes_precedence(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\nAWF_CLAUDE_AUTH_FORCE_COPY=true\n",
        encoding="utf-8",
    )
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={
            "AWF_WORK_DIR_BIND_PROPAGATION": "rshared",
            "AWF_CLAUDE_AUTH_FORCE_COPY": "false",
        },
        compose_env_file=env_file,
    )
    assert payload["propagation"] == "rshared"
    assert payload["force_copy"] is False


@pytest.mark.unit
def test_mount_propagation_check_force_copy_on() -> None:
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={
            "AWF_WORK_DIR_BIND_PROPAGATION": "rprivate",
            "AWF_CLAUDE_AUTH_FORCE_COPY": "on",
        },
        compose_env_file=None,
    )
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["force_copy"] is True


@pytest.mark.unit
def test_mount_propagation_check_force_copy_whitespace_trimmed() -> None:
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={
            "AWF_WORK_DIR_BIND_PROPAGATION": "rprivate",
            "AWF_CLAUDE_AUTH_FORCE_COPY": " yes ",
        },
        compose_env_file=None,
    )
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["force_copy"] is True


@pytest.mark.unit
def test_mount_propagation_check_force_copy_none_when_missing() -> None:
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared"},
        compose_env_file=None,
    )
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["propagation"] == "rshared"
    assert payload["force_copy"] is None
    assert "force_copy=unknown" in payload["detail"]


@pytest.mark.unit
def test_mount_propagation_check_force_copy_none_from_partial_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("AWF_WORK_DIR_BIND_PROPAGATION=rprivate\n", encoding="utf-8")
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=env_file,
    )
    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["propagation"] == "rprivate"
    assert payload["force_copy"] is None
    assert "force_copy=unknown" in payload["detail"]


@pytest.mark.unit
def test_mount_propagation_partial_environ_falls_back_to_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\nAWF_CLAUDE_AUTH_FORCE_COPY=true\n",
        encoding="utf-8",
    )
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared"},
        compose_env_file=env_file,
    )
    assert payload["propagation"] == "rshared"
    assert payload["force_copy"] is True


@pytest.mark.unit
def test_mount_propagation_check_corrupt_env_file_falls_back_to_unknown(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes(b"\xff\xfe INVALID UTF-8")
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=env_file,
    )
    assert payload["ok"] is True
    assert payload["status"] == "unknown"
    assert payload["reason"] == "MOUNT_PROPAGATION_UNKNOWN"
    assert payload["propagation"] is None
    assert payload["force_copy"] is None


@pytest.mark.unit
def test_mount_propagation_check_unreadable_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\n",
        encoding="utf-8",
    )
    env_file.chmod(0o000)
    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=env_file,
    )
    assert payload["ok"] is True
    assert payload["status"] == "unknown"
    assert payload["propagation"] is None


def _write_probe_evidence(work_dir: Path, payload: dict[str, object]) -> None:
    scratch_root = work_dir / "auth" / "_shared"
    scratch_root.mkdir(parents=True, exist_ok=True)
    (scratch_root / "overlay-probe.json").write_text(json.dumps(payload), encoding="utf-8")


def _home_with_claude_dir(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    return home


_UNEXPECTED_EVIDENCE: dict[str, object] = {
    "ok": False,
    "expected": False,
    "reason": "REFUSED",
    "detail": "exit=32: cannot mount overlay read-only",
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "environ",
    [
        {"AWF_WORK_DIR_BIND_PROPAGATION": "rshared", "AWF_CLAUDE_AUTH_FORCE_COPY": "false"},
        {},
    ],
)
def test_mount_propagation_payload_is_unchanged_without_probe_evidence(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    # The awf-cloud/GKE regression at the status surface: a host that never
    # probed must produce a byte-identical payload, in both the AVAILABLE and
    # UNKNOWN branches.
    baseline = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ=environ,
        compose_env_file=None,
    )

    with_work_dir = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ=environ,
        compose_env_file=None,
        work_dir=tmp_path / "work",
        host_home=_home_with_claude_dir(tmp_path),
    )

    assert with_work_dir == baseline
    assert with_work_dir["ok"] is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "environ",
    [
        {"AWF_WORK_DIR_BIND_PROPAGATION": "rshared", "AWF_CLAUDE_AUTH_FORCE_COPY": "false"},
        {},
    ],
)
def test_mount_propagation_warns_on_unexpected_overlay_probe_evidence(
    tmp_path: Path, environ: dict[str, str]
) -> None:
    work_dir = tmp_path / "work"
    _write_probe_evidence(work_dir, _UNEXPECTED_EVIDENCE)

    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ=environ,
        compose_env_file=None,
        work_dir=work_dir,
        host_home=_home_with_claude_dir(tmp_path),
    )

    # ``ok`` MUST stay True in every branch: ``collect_service_status`` ANDs each
    # check into ``overall_ok`` and ``readiness`` turns a non-ok service status
    # into a release-blocking SERVICE_STATUS_NOT_READY. Visibility flows through
    # ``status``/``reason`` instead.
    assert payload["ok"] is True
    assert payload["status"] == "warn"
    assert payload["reason"] == "CLAUDE_AUTH_OVERLAY_UNEXPECTEDLY_UNAVAILABLE"
    assert "cannot mount overlay read-only" in str(payload["detail"])


@pytest.mark.unit
def test_mount_propagation_warns_despite_stale_force_copy_in_process_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The resolved posture (passed environ, then compose env-file) is the truth
    # here; a stale truthy ``AWF_CLAUDE_AUTH_FORCE_COPY`` in the CLI process must
    # not silently suppress refusal evidence for a host running overlays.
    monkeypatch.setenv("AWF_CLAUDE_AUTH_FORCE_COPY", "true")
    work_dir = tmp_path / "work"
    _write_probe_evidence(work_dir, _UNEXPECTED_EVIDENCE)

    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared", "AWF_CLAUDE_AUTH_FORCE_COPY": "false"},
        compose_env_file=None,
        work_dir=work_dir,
        host_home=_home_with_claude_dir(tmp_path),
    )

    assert payload["ok"] is True
    assert payload["status"] == "warn"
    assert payload["reason"] == "CLAUDE_AUTH_OVERLAY_UNEXPECTEDLY_UNAVAILABLE"


@pytest.mark.unit
@pytest.mark.parametrize(
    "prepare_home",
    [
        pytest.param(lambda home: home.mkdir(parents=True), id="empty-home"),
        pytest.param(
            lambda home: (home.mkdir(parents=True), (home / ".claude.json").touch()),
            id="claude-json-only",
        ),
        pytest.param(
            lambda home: (home.mkdir(parents=True), (home / ".claude").touch()),
            id="claude-not-a-dir",
        ),
        pytest.param(lambda _home: None, id="home-absent"),
    ],
)
def test_mount_propagation_ignores_probe_evidence_without_claude_dir(
    tmp_path: Path, prepare_home: Any
) -> None:
    # Without a ``~/.claude`` directory the resolver never calls ``supported()``,
    # so nothing refreshes or discards evidence an earlier posture left behind.
    # Warning here would report a standing overlay fallback for a host that does
    # not overlay at all — gate on the directory source, like provider readiness.
    work_dir = tmp_path / "work"
    _write_probe_evidence(work_dir, _UNEXPECTED_EVIDENCE)
    home = tmp_path / "home"
    prepare_home(home)
    baseline = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared"},
        compose_env_file=None,
    )

    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared"},
        compose_env_file=None,
        work_dir=work_dir,
        host_home=home,
    )

    assert payload == baseline


@pytest.mark.unit
def test_mount_propagation_ignores_probe_evidence_without_a_host_home(tmp_path: Path) -> None:
    # No host home to inspect means no evidence that a directory source is active.
    work_dir = tmp_path / "work"
    _write_probe_evidence(work_dir, _UNEXPECTED_EVIDENCE)
    baseline = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared"},
        compose_env_file=None,
    )

    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared"},
        compose_env_file=None,
        work_dir=work_dir,
    )

    assert payload == baseline


@pytest.mark.unit
@pytest.mark.parametrize(
    ("environ", "env_file_body"),
    [
        ({"AWF_WORK_DIR_BIND_PROPAGATION": "rprivate", "AWF_CLAUDE_AUTH_FORCE_COPY": "true"}, None),
        ({}, "AWF_WORK_DIR_BIND_PROPAGATION=rprivate\nAWF_CLAUDE_AUTH_FORCE_COPY=true\n"),
    ],
)
def test_mount_propagation_ignores_probe_evidence_under_force_copy(
    tmp_path: Path, environ: dict[str, str], env_file_body: str | None
) -> None:
    # Stale evidence from a run that *did* probe must not warn once the host is
    # on force-copy: it now fails the first cheap gate, never probes, and the
    # copy fallback it takes is a fully supported posture. Resolved from the
    # environ and from the compose env-file alike.
    work_dir = tmp_path / "work"
    _write_probe_evidence(work_dir, _UNEXPECTED_EVIDENCE)
    env_file: Path | None = None
    if env_file_body is not None:
        env_file = tmp_path / ".env"
        env_file.write_text(env_file_body, encoding="utf-8")

    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ=environ,
        compose_env_file=env_file,
        work_dir=work_dir,
        host_home=_home_with_claude_dir(tmp_path),
    )

    assert payload["ok"] is True
    assert payload["status"] == "ok"
    assert payload["reason"] == "MOUNT_PROPAGATION_AVAILABLE"
    assert payload["force_copy"] is True
    assert "overlay probe" not in str(payload["detail"])


@pytest.mark.unit
@pytest.mark.parametrize(
    "evidence",
    [
        {"ok": False, "expected": True, "reason": "MOUNT_BINARY_MISSING"},
        {"ok": True, "expected": True, "reason": "OVERLAY_PROBE_OK"},
    ],
)
def test_mount_propagation_ignores_expected_probe_outcomes(
    tmp_path: Path, evidence: dict[str, object]
) -> None:
    work_dir = tmp_path / "work"
    _write_probe_evidence(work_dir, evidence)
    baseline = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared"},
        compose_env_file=None,
    )

    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={"AWF_WORK_DIR_BIND_PROPAGATION": "rshared"},
        compose_env_file=None,
        work_dir=work_dir,
        host_home=_home_with_claude_dir(tmp_path),
    )

    assert payload == baseline


@pytest.mark.unit
@pytest.mark.parametrize("body", ["{not json", "[1, 2]"])
def test_mount_propagation_ignores_corrupt_probe_evidence(tmp_path: Path, body: str) -> None:
    work_dir = tmp_path / "work"
    scratch_root = work_dir / "auth" / "_shared"
    scratch_root.mkdir(parents=True)
    (scratch_root / "overlay-probe.json").write_text(body, encoding="utf-8")
    baseline = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=None,
    )

    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=None,
        work_dir=work_dir,
        host_home=_home_with_claude_dir(tmp_path),
    )

    assert payload == baseline


@pytest.mark.unit
def test_mount_propagation_ignores_unreadable_probe_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a reader failure instead of chmod(0o000): root bypasses mode bits, so
    # a permission-only fixture can still load unexpected evidence and warn.
    work_dir = tmp_path / "work"
    scratch_root = work_dir / "auth" / "_shared"
    scratch_root.mkdir(parents=True)
    evidence_path = scratch_root / "overlay-probe.json"
    evidence_path.write_text(json.dumps(_UNEXPECTED_EVIDENCE), encoding="utf-8")
    original_read_text = Path.read_text

    def _raise_for_evidence(self: Path, *args: object, **kwargs: object) -> str:
        if self == evidence_path:
            raise PermissionError(13, "Permission denied", str(self))
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _raise_for_evidence)

    payload = status_mod._mount_propagation_check_payload(  # noqa: SLF001
        environ={},
        compose_env_file=None,
        work_dir=work_dir,
        host_home=_home_with_claude_dir(tmp_path),
    )

    assert payload["ok"] is True
    assert payload["status"] == "unknown"


@pytest.mark.unit
def test_collect_service_status_passes_the_work_dir_to_the_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[Path | None, Path | None]] = []
    real = status_mod._mount_propagation_check_payload  # noqa: SLF001

    def _spy(
        *,
        environ: dict[str, str],
        compose_env_file: Path | None,
        work_dir: Path | None = None,
        host_home: Path | None = None,
    ) -> object:
        recorded.append((work_dir, host_home))
        return real(
            environ=environ,
            compose_env_file=compose_env_file,
            work_dir=work_dir,
            host_home=host_home,
        )

    monkeypatch.setattr(status_mod, "_mount_propagation_check_payload", _spy)
    settings = _settings(tmp_path)

    asyncio.run(
        status_mod.collect_service_status(
            settings,
            api_get=_api_get,
            db_probe=_db_probe,
            run_subprocess=_run_subprocess_missing_docker,
            socket_exists=lambda _path: True,
            disk_usage=lambda _path: _DiskUsage(total=1000, used=700, free=300),
            workspace_id_lookup=_empty_workspace_view,
            provider_environ={},
        )
    )

    assert recorded == [
        (Path(settings.work_dir).expanduser(), Path(settings.host_home).expanduser())
    ]
