"""Shared-base signature stability and overlay-probe wiring (part 8, #874).

Two coupled #874 concerns:

* the host-content signature must ignore top-level state that changes on every
  Claude Code interaction (so a ~1.9 GB base is not rebuilt and reaped on every
  provision) while still signing everything else, including *nested* dirs whose
  basename happens to collide with an excluded top-level name;
* ``_SubprocessOverlayMounter.supported()`` must consult the real scratch-mount
  probe instead of trusting ``/proc/filesystems`` + ``CAP_SYS_ADMIN``, which are
  blind to the AppArmor ``deny mount,`` rule that made every mount fail.

No test here performs a real mount: the probe is driven through an injected
``run`` callable and the mounter through monkeypatched cheap gates.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from structlog.testing import capture_logs

from awf.node import auth_mounts as auth_mounts_facade
from awf.node import auth_mounts_claude as auth_mounts_mod
from awf.node.auth_mounts import (
    _host_claude_signature,
    default_overlay_mounter,
    resolve_service_auth_mounts,
)
from awf.node.auth_mounts_claude import _ensure_shared_claude_base
from awf.node.auth_mounts_overlay_probe import (
    OverlayProbeResult,
    overlay_probe_evidence_path,
    overlay_probe_scratch_root,
    overlay_unexpectedly_unavailable,
    write_overlay_probe_evidence,
)

from .test_claude_auth_overlay_part_001 import FakeOverlayMounter

_VOLATILE_TOP_LEVEL_DIRS = (
    "file-history",
    "cache",
    "paste-cache",
    "session-env",
    "sessions",
    "backups",
    "debug",
    "jobs",
    "tasks",
)


def _seed_signature_host(host_home: Path) -> Path:
    """Seed a ``~/.claude`` carrying auth, content, volatile state and a nested cache."""

    claude = host_home / ".claude"
    claude.mkdir(parents=True)
    (claude / "settings.json").write_text('{"theme": "dark"}\n')
    (claude / ".credentials.json").write_text('{"token": "t0"}\n')
    (claude / "CLAUDE.md").write_text("# rules\n")
    (claude / "skills" / "demo").mkdir(parents=True)
    (claude / "skills" / "demo" / "SKILL.md").write_text("# demo\n")
    # An installed-plugin store whose basename collides with a volatile top-level
    # name — it must keep being signed and copied (#874 trap (a)).
    (claude / "plugins" / "cache").mkdir(parents=True)
    (claude / "plugins" / "cache" / "pkg.tgz").write_text("plugin payload\n")
    (claude / "projects" / "repo").mkdir(parents=True)
    (claude / "projects" / "repo" / "session.jsonl").write_text('{"usage": "historical"}\n')
    (claude / "history.jsonl").write_text('{"turn": 1}\n')
    for name in _VOLATILE_TOP_LEVEL_DIRS:
        (claude / name).mkdir()
        (claude / name / "state").write_text("v1\n")
    (claude / "daemon.sock").write_text("")
    (claude / "plugin-cache.json").write_text("{}\n")
    return claude


@pytest.mark.unit
def test_signature_ignores_volatile_top_level_state(tmp_path: Path) -> None:
    # #874: ``history.jsonl`` and the per-interaction state dirs churn on every
    # Claude Code turn on the host. Signing them rebuilt (and reaped) a ~1.9 GB
    # shared base on essentially every provision.
    host_home = tmp_path / "host-home"
    claude = _seed_signature_host(host_home)
    before = _host_claude_signature(host_home)

    # ``history.jsonl`` is the trap-(b) case: the signature walk filtered only
    # ``dirs``, so no edit to the exclusion constant alone could ever drop it.
    (claude / "history.jsonl").write_text('{"turn": 1}\n{"turn": 2}\n')
    for name in _VOLATILE_TOP_LEVEL_DIRS:
        (claude / name / "state").write_text("v2 — churned\n")
        (claude / name / "extra").write_text("new volatile entry\n")
    (claude / "daemon.sock").write_text("pid=2\n")
    (claude / "plugin-cache.json").write_text('{"warm": true}\n')
    (claude / "projects" / "repo" / "session.jsonl").write_text('{"usage": "more"}\n')

    assert _host_claude_signature(host_home) == before


@pytest.mark.unit
def test_signature_is_stable_when_host_claude_is_absent(tmp_path: Path) -> None:
    # ``source.stat()`` raises on a host with no ``~/.claude``; the walk records a
    # ``missing`` root marker instead of failing, and stays deterministic.
    host_home = tmp_path / "empty-home"
    host_home.mkdir()
    seeded_home = tmp_path / "seeded-home"
    _seed_signature_host(seeded_home)

    signature = _host_claude_signature(host_home)

    assert len(signature) == 16
    assert signature == _host_claude_signature(host_home)
    assert signature != _host_claude_signature(seeded_home)


@pytest.mark.unit
@pytest.mark.parametrize("relative", ["settings.json", ".credentials.json", "CLAUDE.md"])
def test_signature_changes_when_auth_or_config_changes(tmp_path: Path, relative: str) -> None:
    host_home = tmp_path / "host-home"
    claude = _seed_signature_host(host_home)
    before = _host_claude_signature(host_home)

    (claude / relative).write_text('{"rotated": true, "padding": "xxxxxxxx"}\n')

    assert _host_claude_signature(host_home) != before


@pytest.mark.unit
def test_signature_changes_for_nested_cache_dir(tmp_path: Path) -> None:
    # The anchoring guarantee from the signature side: ``plugins/cache`` is an
    # installed-plugin store, not host usage state, so updating a plugin must
    # still mint a fresh base.
    host_home = tmp_path / "host-home"
    claude = _seed_signature_host(host_home)
    before = _host_claude_signature(host_home)

    (claude / "plugins" / "cache" / "pkg.tgz").write_text("updated plugin payload\n")

    assert _host_claude_signature(host_home) != before


@pytest.mark.unit
def test_shared_base_copy_keeps_nested_cache_and_omits_top_level_usage_history(
    tmp_path: Path,
) -> None:
    # ``shutil.ignore_patterns`` matched at every depth; the anchored ignore must
    # strip only the top-level usage-history dirs.
    host_home = tmp_path / "host-home"
    _seed_signature_host(host_home)
    work_dir = tmp_path / "work"

    base = _ensure_shared_claude_base(
        host_home=host_home,
        work_dir=work_dir,
        signature=_host_claude_signature(host_home),
        workspace_owner_uid=None,
        workspace_owner_gid=None,
    )

    assert (base / "plugins" / "cache" / "pkg.tgz").read_text() == "plugin payload\n"
    assert not (base / "projects").exists()
    # Volatile state is *copied* (the agent gets a frozen snapshot); it is only
    # left out of the signature.
    assert (base / "history.jsonl").exists()
    assert (base / "cache" / "state").exists()


@pytest.mark.unit
def test_shared_base_copy_keeps_nested_usage_history_dirs(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    claude = _seed_signature_host(host_home)
    (claude / "plugins" / "projects").mkdir()
    (claude / "plugins" / "projects" / "manifest.json").write_text("{}\n")
    work_dir = tmp_path / "work"

    base = _ensure_shared_claude_base(
        host_home=host_home,
        work_dir=work_dir,
        signature=_host_claude_signature(host_home),
        workspace_owner_uid=None,
        workspace_owner_gid=None,
    )

    assert (base / "plugins" / "projects" / "manifest.json").read_text() == "{}\n"


def _all_cheap_gates_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_mounts_mod, "_force_copy_isolation_requested", lambda *_a: False)
    monkeypatch.setattr(auth_mounts_mod, "_overlay_filesystem_available", lambda *_a: True)
    monkeypatch.setattr(auth_mounts_mod, "_has_cap_sys_admin", lambda *_a: True)


class _RecordingProbe:
    """Stands in for ``cached_overlay_probe``; records whether it ran at all."""

    def __init__(self, result: OverlayProbeResult) -> None:
        self.result = result
        self.scratch_roots: list[Path] = []

    def __call__(self, *, scratch_root: Path) -> OverlayProbeResult:
        self.scratch_roots.append(scratch_root)
        return self.result


@pytest.mark.unit
def test_supported_short_circuits_when_no_scratch_root(monkeypatch: pytest.MonkeyPatch) -> None:
    # The teardown-constructed mounter has no work-dir context and only calls
    # ``is_mounted``/``unmount``; it must keep the pre-#874 behavior and never
    # attempt a probe.
    _all_cheap_gates_pass(monkeypatch)
    probe = _RecordingProbe(OverlayProbeResult(ok=False, reason="REFUSED"))
    monkeypatch.setattr(auth_mounts_mod, "cached_overlay_probe", probe)

    assert auth_mounts_mod._SubprocessOverlayMounter().supported() is True  # noqa: SLF001
    assert probe.scratch_roots == []


@pytest.mark.unit
@pytest.mark.parametrize("failing_gate", ["force_copy", "kernel_overlay", "cap_sys_admin"])
def test_cheap_gates_short_circuit_before_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_gate: str
) -> None:
    # The awf-cloud/GKE constraint, made explicit: a host that fails any cheap
    # gate never probes, therefore never writes evidence, therefore can never
    # produce a standing warning. Absence of evidence means silence, by
    # construction — not by accident.
    _all_cheap_gates_pass(monkeypatch)
    gates = {
        "force_copy": ("_force_copy_isolation_requested", True),
        "kernel_overlay": ("_overlay_filesystem_available", False),
        "cap_sys_admin": ("_has_cap_sys_admin", False),
    }
    attribute, value = gates[failing_gate]
    monkeypatch.setattr(auth_mounts_mod, attribute, lambda *_a, _v=value: _v)
    probe = _RecordingProbe(OverlayProbeResult(ok=True, reason="OVERLAY_PROBE_OK"))
    monkeypatch.setattr(auth_mounts_mod, "cached_overlay_probe", probe)
    scratch_root = overlay_probe_scratch_root(tmp_path)

    mounter = auth_mounts_mod._SubprocessOverlayMounter(scratch_root=scratch_root)  # noqa: SLF001

    assert mounter.supported() is False
    assert probe.scratch_roots == []
    assert overlay_unexpectedly_unavailable(tmp_path) is False
    assert not overlay_probe_evidence_path(scratch_root).exists()


@pytest.mark.unit
@pytest.mark.parametrize("failing_gate", ["force_copy", "kernel_overlay", "cap_sys_admin"])
def test_cheap_gate_short_circuit_discards_evidence_from_an_earlier_posture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_gate: str
) -> None:
    # A worker that was refused once wrote evidence; it is then restarted under
    # force-copy (or without CAP_SYS_ADMIN, or on a kernel without overlayfs) and
    # no longer probes. The stale file would otherwise keep status and provider
    # readiness reporting CLAUDE_AUTH_OVERLAY_UNEXPECTEDLY_UNAVAILABLE for what is
    # now an intentional, fully supported copy-fallback posture.
    _all_cheap_gates_pass(monkeypatch)
    gates = {
        "force_copy": ("_force_copy_isolation_requested", True),
        "kernel_overlay": ("_overlay_filesystem_available", False),
        "cap_sys_admin": ("_has_cap_sys_admin", False),
    }
    attribute, value = gates[failing_gate]
    monkeypatch.setattr(auth_mounts_mod, attribute, lambda *_a, _v=value: _v)
    scratch_root = overlay_probe_scratch_root(tmp_path)
    scratch_root.mkdir(parents=True)
    write_overlay_probe_evidence(
        scratch_root, OverlayProbeResult(ok=False, reason="REFUSED", detail="exit=32: denied")
    )
    assert overlay_unexpectedly_unavailable(tmp_path) is True

    mounter = auth_mounts_mod._SubprocessOverlayMounter(scratch_root=scratch_root)  # noqa: SLF001

    assert mounter.supported() is False
    assert not overlay_probe_evidence_path(scratch_root).exists()
    assert overlay_unexpectedly_unavailable(tmp_path) is False


@pytest.mark.unit
def test_unsupported_overlay_logs_info_under_force_copy_despite_old_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The same staleness at the node log surface: with force-copy requested the
    # copy fallback is the intended posture, so the provision log stays INFO on
    # the uncataloged reason code rather than warning on the cataloged one.
    host_home = tmp_path / "host-home"
    _seed_signature_host(host_home)
    work_dir = tmp_path / "work"
    scratch_root = overlay_probe_scratch_root(work_dir)
    scratch_root.mkdir(parents=True)
    write_overlay_probe_evidence(
        scratch_root, OverlayProbeResult(ok=False, reason="REFUSED", detail="exit=32: denied")
    )
    monkeypatch.setenv("AWF_CLAUDE_AUTH_FORCE_COPY", "true")

    with capture_logs() as logs:
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_forced_copy",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=False),
        )

    events = [entry for entry in logs if entry["event"].startswith("claude_auth_overlay")]
    assert [entry["event"] for entry in events] == ["claude_auth_overlay_unavailable"]
    assert events[0]["log_level"] == "info"
    assert events[0]["reason_code"] == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE"


@pytest.mark.unit
def test_supported_is_false_when_the_probe_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_cheap_gates_pass(monkeypatch)
    probe = _RecordingProbe(OverlayProbeResult(ok=False, reason="REFUSED", detail="exit=32"))
    monkeypatch.setattr(auth_mounts_mod, "cached_overlay_probe", probe)
    scratch_root = overlay_probe_scratch_root(tmp_path)

    mounter = auth_mounts_mod._SubprocessOverlayMounter(scratch_root=scratch_root)  # noqa: SLF001

    assert mounter.supported() is False
    assert probe.scratch_roots == [scratch_root]


@pytest.mark.unit
def test_supported_is_true_when_the_probe_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _all_cheap_gates_pass(monkeypatch)
    probe = _RecordingProbe(OverlayProbeResult(ok=True, reason="OVERLAY_PROBE_OK"))
    monkeypatch.setattr(auth_mounts_mod, "cached_overlay_probe", probe)

    mounter = auth_mounts_mod._SubprocessOverlayMounter(  # noqa: SLF001
        scratch_root=overlay_probe_scratch_root(tmp_path)
    )

    assert mounter.supported() is True


@pytest.mark.unit
def test_default_overlay_mounter_threads_the_scratch_root(tmp_path: Path) -> None:
    scratch_root = overlay_probe_scratch_root(tmp_path)

    mounter = default_overlay_mounter(scratch_root=scratch_root)

    assert isinstance(mounter, auth_mounts_mod._SubprocessOverlayMounter)  # noqa: SLF001
    assert mounter.scratch_root == scratch_root
    assert default_overlay_mounter().scratch_root is None


@pytest.mark.unit
def test_resolver_builds_the_default_mounter_with_the_shared_auth_scratch_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_home = tmp_path / "host-home"
    _seed_signature_host(host_home)
    work_dir = tmp_path / "work"
    recorded: list[Path | None] = []

    def _fake_default(*, scratch_root: Path | None = None) -> FakeOverlayMounter:
        recorded.append(scratch_root)
        return FakeOverlayMounter(supported=False)

    monkeypatch.setattr(auth_mounts_facade, "default_overlay_mounter", _fake_default)

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_probe",
        host_env={},
    )

    assert recorded == [work_dir / "auth" / "_shared"]


@pytest.mark.unit
def test_unsupported_overlay_logs_info_without_probe_evidence(tmp_path: Path) -> None:
    # GKE / force-copy hosts: the copy fallback is correct and expected, so the
    # log must stay INFO and carry the unchanged reason code.
    host_home = tmp_path / "host-home"
    _seed_signature_host(host_home)
    work_dir = tmp_path / "work"

    with capture_logs() as logs:
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_copy",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=False),
        )

    events = [entry for entry in logs if entry["event"].startswith("claude_auth_overlay")]
    assert [entry["event"] for entry in events] == ["claude_auth_overlay_unavailable"]
    assert events[0]["log_level"] == "info"
    assert events[0]["reason_code"] == "CLAUDE_AUTH_OVERLAY_UNAVAILABLE"
    assert events[0]["reason"] == "overlayfs_unsupported"


@pytest.mark.unit
def test_unsupported_overlay_warns_on_unexpected_probe_evidence(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    _seed_signature_host(host_home)
    work_dir = tmp_path / "work"
    scratch_root = overlay_probe_scratch_root(work_dir)
    scratch_root.mkdir(parents=True)
    write_overlay_probe_evidence(
        scratch_root,
        OverlayProbeResult(ok=False, reason="REFUSED", detail="exit=32: denied"),
    )

    with capture_logs() as logs:
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_refused",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=False),
        )

    events = [entry for entry in logs if entry["event"].startswith("claude_auth_overlay")]
    assert [entry["event"] for entry in events] == ["claude_auth_overlay_unexpectedly_unavailable"]
    assert events[0]["log_level"] == "warning"
    assert events[0]["reason_code"] == "CLAUDE_AUTH_OVERLAY_UNEXPECTEDLY_UNAVAILABLE"
    assert events[0]["probe_reason"] == "REFUSED"
    assert "denied" in events[0]["probe_detail"]


@pytest.mark.unit
def test_unsupported_overlay_stays_info_for_expected_probe_failure(tmp_path: Path) -> None:
    host_home = tmp_path / "host-home"
    _seed_signature_host(host_home)
    work_dir = tmp_path / "work"
    scratch_root = overlay_probe_scratch_root(work_dir)
    scratch_root.mkdir(parents=True)
    write_overlay_probe_evidence(
        scratch_root, OverlayProbeResult(ok=False, reason="MOUNT_BINARY_MISSING")
    )

    with capture_logs() as logs:
        resolve_service_auth_mounts(
            host_home=host_home,
            work_dir=work_dir,
            workspace_id="ws_no_mount_bin",
            host_env={},
            overlay_mounter=FakeOverlayMounter(supported=False),
        )

    events = [entry for entry in logs if entry["event"].startswith("claude_auth_overlay")]
    assert [entry["event"] for entry in events] == ["claude_auth_overlay_unavailable"]
    assert events[0]["log_level"] == "info"


@pytest.mark.unit
def test_unsupported_overlay_never_builds_the_shared_base(tmp_path: Path) -> None:
    # The ~1.9 GB copytree used to run *before* the mount attempt, so an
    # AppArmor-blocked host paid for a base it could never mount (#874).
    host_home = tmp_path / "host-home"
    _seed_signature_host(host_home)
    work_dir = tmp_path / "work"

    resolve_service_auth_mounts(
        host_home=host_home,
        work_dir=work_dir,
        workspace_id="ws_no_base",
        host_env={},
        overlay_mounter=FakeOverlayMounter(supported=False),
    )

    assert not (work_dir / "auth" / "_shared" / "claude-base").exists()


@pytest.mark.unit
def test_usage_history_exclusion_alias_still_exported() -> None:
    # ``awf.node.auth_mounts.<name>`` is the stable import surface; the legacy
    # tuple name stays available there and keeps its pre-#874 membership.
    assert set(auth_mounts_facade._CLAUDE_USAGE_HISTORY_DIRS) == {  # noqa: SLF001
        "projects",
        "todos",
        "shell-snapshots",
        "statsig",
    }
    assert (
        auth_mounts_facade._CLAUDE_USAGE_HISTORY_DIRS  # noqa: SLF001
        is auth_mounts_mod._CLAUDE_USAGE_HISTORY_DIRS  # noqa: SLF001
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    [
        "CLAUDE_COPY_EXCLUDED_TOP_LEVEL",
        "CLAUDE_SIGNATURE_EXCLUDED_TOP_LEVEL",
        "claude_copy_ignore",
        "claude_signature_excludes_rel",
        "cached_overlay_probe",
        "discard_overlay_probe_evidence",
        "CLAUDE_AUTH_FORCE_COPY_ENV",
    ],
)
def test_exclusion_and_probe_names_reexported_through_facade(name: str) -> None:
    # ``auth_mounts_claude`` re-exports every public name of its leaf modules and
    # ``auth_mounts`` re-exports every public name of ``auth_mounts_claude`` — the
    # #874 additions must not break that documented import surface.
    assert hasattr(auth_mounts_facade, name)
    assert getattr(auth_mounts_facade, name) is getattr(auth_mounts_mod, name)
