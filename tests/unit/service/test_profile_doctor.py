"""Unit tests for the ``awf profile doctor`` profile-readiness preflight."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from awf.profiles.models import (
    ProfileLintFinding,
    ProfileLintSeverity,
)
from awf.profiles.resolver import ProfileResolutionError
from awf.service import profile_doctor
from awf.service.profile_doctor import (
    DOCKER_MODE_NOT_DIND,
    DOCKER_UNAVAILABLE,
    IMAGE_PRESENT,
    IMAGE_PULLABLE,
    IMAGE_UNAVAILABLE,
    IMAGE_UNREACHABLE,
    PROFILE_DOCTOR_IMAGE_UNREACHABLE,
    PROFILE_DOCTOR_IMAGES_PRESENT,
    PROFILE_DOCTOR_IMAGES_PULLABLE,
    PROFILE_DOCTOR_LINT_CLEAN,
    PROFILE_DOCTOR_LINT_ERRORS,
    PROFILE_DOCTOR_LINT_WARNINGS,
    PROFILE_DOCTOR_PROFILE_RESOLVED,
    PROFILE_DOCTOR_PROFILE_UNRESOLVED,
    PROFILE_DOCTOR_SECRET_LEASES_OK,
    PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING,
    PROFILE_RESOLUTION_FAILED,
    _default_image_probe,
    _lint_phase,
    collect_profile_doctor_report,
)

pytestmark = pytest.mark.unit


def _write_profile(repo: Path, body: str) -> None:
    awf_dir = repo / ".awf"
    awf_dir.mkdir(parents=True, exist_ok=True)
    (awf_dir / "workspace.yml").write_text(body, encoding="utf-8")


def _phase(report: dict, name: str) -> dict:
    return next(p for p in report["phases"] if p["name"] == name)


def test_happy_path_all_ok(tmp_path: Path) -> None:
    """A clean profile (no secrets, open egress, docker none) is all ok/skipped."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  security:\n"
        "    egress:\n"
        "      mode: open\n"
        "  phases:\n"
        "    validate:\n"
        "      - pytest -q\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    assert report["status"] == "ok"
    assert report["repo"] == str(tmp_path)
    resolution = _phase(report, "profile_resolution")
    assert resolution["status"] == "ok"
    assert resolution["reason_code"] == PROFILE_DOCTOR_PROFILE_RESOLVED
    assert resolution["evidence"]["profile"] == "generic"
    assert _phase(report, "profile_lint")["reason_code"] == PROFILE_DOCTOR_LINT_CLEAN
    assert _phase(report, "secret_leases")["reason_code"] == PROFILE_DOCTOR_SECRET_LEASES_OK
    assert _phase(report, "egress")["status"] == "ok"
    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "skipped"
    assert docker_phase["reason_code"] == DOCKER_MODE_NOT_DIND


def test_secret_lease_source_missing_fails(tmp_path: Path) -> None:
    """A required local-file secret whose source is absent fails the preflight."""
    missing = tmp_path / "creds" / "token"
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  secrets:\n"
        "    - name: api-token\n"
        "      kind: mount\n"
        "      target: /run/awf/secrets/api-token\n"
        "      provider: local-file\n"
        f"      ref: {missing}\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    secret_phase = _phase(report, "secret_leases")
    assert secret_phase["status"] == "fail"
    assert secret_phase["reason_code"] == "SECRET_LEASE_SOURCE_MISSING"
    assert secret_phase["evidence"]["secret_name"] == "api-token"
    assert secret_phase["evidence"]["provider"] == "local-file"
    assert secret_phase["evidence"]["target"] == "/run/awf/secrets/api-token"
    assert "not visible to the worker context" in secret_phase["message"]
    assert report["status"] == "fail"
    # No raw source path or value leaks into the evidence.
    assert str(missing) not in json.dumps(report)


def test_optional_missing_lease_warns(tmp_path: Path) -> None:
    """An optional local-file secret with no source warns, not fails."""
    missing = tmp_path / "creds" / "token"
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  secrets:\n"
        "    - name: api-token\n"
        "      kind: mount\n"
        "      target: /run/awf/secrets/api-token\n"
        "      provider: local-file\n"
        "      required: false\n"
        f"      ref: {missing}\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    secret_phase = _phase(report, "secret_leases")
    assert secret_phase["status"] == "warn"
    assert secret_phase["reason_code"] == PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING
    omitted = secret_phase["evidence"]["omitted_optional"]
    assert omitted[0]["secret_name"] == "api-token"
    assert omitted[0]["reason_code"] == "SECRET_LEASE_SOURCE_MISSING"
    assert report["status"] == "warn"


def test_env_lease_resolves_ok_without_value(tmp_path: Path) -> None:
    """A ``provider: env`` lease with the var present resolves with no raw value."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  secrets:\n"
        "    - name: openai\n"
        "      kind: env\n"
        "      target: OPENAI_API_KEY\n"
        "      provider: env\n"
        "      ref: env/OPENAI_API_KEY\n",
    )

    report = collect_profile_doctor_report(
        tmp_path,
        repo_url=None,
        host_env={"OPENAI_API_KEY": "sk-super-secret-value"},
    )

    secret_phase = _phase(report, "secret_leases")
    assert secret_phase["status"] == "ok"
    assert secret_phase["reason_code"] == PROFILE_DOCTOR_SECRET_LEASES_OK
    assert secret_phase["evidence"]["targets"] == ["OPENAI_API_KEY"]
    assert secret_phase["evidence"]["env_count"] == 1
    assert "sk-super-secret-value" not in json.dumps(report)


def test_lint_warning_profile_warns(tmp_path: Path) -> None:
    """A host-home credential mount under warn policy surfaces a lint warning."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  security:\n"
        "    egress:\n"
        "      mode: open\n"
        "    host_home_auth_mounts:\n"
        "      mode: warn\n"
        "  services:\n"
        "    - name: app\n"
        "      image: example/app:latest\n"
        "      volumes:\n"
        "        - ['~/.ssh', '/home/agent/.ssh:ro']\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    lint_phase = _phase(report, "profile_lint")
    assert lint_phase["status"] == "warn"
    assert lint_phase["reason_code"] == PROFILE_DOCTOR_LINT_WARNINGS
    assert lint_phase["evidence"]["findings"]


def test_lint_phase_maps_error_findings_to_fail() -> None:
    """``_lint_phase`` flags blocking error findings (bypassing the resolver gate)."""
    finding = ProfileLintFinding(
        reason_code="SECRET_TARGET_TOO_BROAD",
        message="too broad",
        path="secrets[0].target",
        severity=ProfileLintSeverity.error,
    )

    phase = _lint_phase([finding])

    assert phase["status"] == "fail"
    assert phase["reason_code"] == PROFILE_DOCTOR_LINT_ERRORS
    assert phase["evidence"]["findings"][0]["severity"] == "error"


def test_lint_phase_clean() -> None:
    phase = _lint_phase([])
    assert phase["status"] == "ok"
    assert phase["reason_code"] == PROFILE_DOCTOR_LINT_CLEAN


@pytest.mark.parametrize(
    ("mode", "expected_status"),
    [("open", "ok"), ("restricted", "warn"), ("offline", "warn")],
)
def test_egress_modes(tmp_path: Path, mode: str, expected_status: str) -> None:
    _write_profile(
        tmp_path,
        f"awf:\n  name: generic\n  security:\n    egress:\n      mode: {mode}\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    egress_phase = _phase(report, "egress")
    assert egress_phase["status"] == expected_status
    assert egress_phase["evidence"]["mode"] == mode
    if mode == "open":
        assert egress_phase["reason_code"] == "LOCAL_EGRESS_OPEN_UNRESTRICTED"
    elif mode == "offline":
        assert egress_phase["reason_code"] == "LOCAL_EGRESS_OFFLINE_NETWORK"
    else:
        assert egress_phase["reason_code"] == "LOCAL_EGRESS_RESTRICTED_LOCAL_ONLY"


def test_profile_resolution_failure_skips_downstream(tmp_path: Path) -> None:
    """A malformed profile fails resolution and skips profile-dependent probes."""

    def _boom(**_kwargs: object) -> object:
        raise ProfileResolutionError(
            "invalid workspace profile: SECRET_TARGET_TOO_BROAD: bad",
            reason_code="SECRET_TARGET_TOO_BROAD",
        )

    report = collect_profile_doctor_report(
        tmp_path,
        repo_url=None,
        host_env={},
        resolve=_boom,
    )

    resolution_phase = _phase(report, "profile_resolution")
    assert resolution_phase["status"] == "fail"
    assert resolution_phase["reason_code"] == "SECRET_TARGET_TOO_BROAD"
    for name in ("profile_lint", "secret_leases", "egress", "docker_images"):
        skipped = _phase(report, name)
        assert skipped["status"] == "skipped"
        assert skipped["reason_code"] == PROFILE_DOCTOR_PROFILE_UNRESOLVED
    assert report["status"] == "fail"


def test_secret_lease_unresolved_provider_surfaces_skipped_count(tmp_path: Path) -> None:
    """A secret with neither provider nor ref is skipped and counted in evidence."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  secrets:\n"
        "    - name: undeclared\n"
        "      kind: mount\n"
        "      target: /run/awf/secrets/undeclared\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    secret_phase = _phase(report, "secret_leases")
    assert secret_phase["status"] == "ok"
    assert secret_phase["evidence"]["skipped_unresolved_count"] == 1


def test_docker_images_dedupes_repeated_image(tmp_path: Path) -> None:
    """Two services sharing an image are probed once (the dedup branch)."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  docker:\n"
        "    mode: dind\n"
        "  services:\n"
        "    - name: db\n"
        "      image: postgres:16\n"
        "    - name: cache\n"
        "      image: postgres:16\n",
    )

    probed: list[str] = []

    def _probe(image: str) -> str:
        probed.append(image)
        return IMAGE_PRESENT

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={}, image_probe=_probe)

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "ok"
    # dind_image + a single postgres:16 entry despite two services declaring it.
    assert probed == ["docker:27-dind", "postgres:16"]
    assert len(docker_phase["evidence"]["images"]) == 2


def test_profile_resolution_failure_without_reason_code(tmp_path: Path) -> None:
    """A reason-code-less resolution error falls back to the generic code."""

    def _boom(**_kwargs: object) -> object:
        raise ProfileResolutionError("could not read workspace profile")

    report = collect_profile_doctor_report(
        tmp_path,
        repo_url=None,
        host_env={},
        resolve=_boom,
    )

    resolution_phase = _phase(report, "profile_resolution")
    assert resolution_phase["status"] == "fail"
    assert resolution_phase["reason_code"] == PROFILE_RESOLUTION_FAILED


def test_docker_images_present_ok(tmp_path: Path) -> None:
    """DinD mode with all images present is ok; build_context services are skipped."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  docker:\n"
        "    mode: dind\n"
        "  services:\n"
        "    - name: db\n"
        "      image: postgres:16\n"
        "    - name: app\n"
        "      build_context: .\n",
    )

    probed: list[str] = []

    def _probe(image: str) -> str:
        probed.append(image)
        return IMAGE_PRESENT

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={}, image_probe=_probe)

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "ok"
    assert docker_phase["reason_code"] == PROFILE_DOCTOR_IMAGES_PRESENT
    # dind_image + the postgres service image; the build_context service is excluded.
    assert "postgres:16" in probed
    assert "docker:27-dind" in probed
    assert "." not in probed
    assert len(probed) == 2


def test_docker_images_unreachable_fails(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  docker:\n"
        "    mode: dind\n"
        "  services:\n"
        "    - name: db\n"
        "      image: private/db:latest\n",
    )

    def _probe(image: str) -> str:
        return IMAGE_UNREACHABLE if image == "private/db:latest" else IMAGE_PRESENT

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={}, image_probe=_probe)

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "fail"
    assert docker_phase["reason_code"] == PROFILE_DOCTOR_IMAGE_UNREACHABLE
    assert "private/db:latest" in docker_phase["message"]
    assert report["status"] == "fail"


def test_docker_images_pullable_warns(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n  docker:\n    mode: dind\n",
    )

    report = collect_profile_doctor_report(
        tmp_path, repo_url=None, host_env={}, image_probe=lambda _img: IMAGE_PULLABLE
    )

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "warn"
    assert docker_phase["reason_code"] == PROFILE_DOCTOR_IMAGES_PULLABLE


def test_docker_images_unavailable_warns(tmp_path: Path) -> None:
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n  docker:\n    mode: dind\n",
    )

    report = collect_profile_doctor_report(
        tmp_path, repo_url=None, host_env={}, image_probe=lambda _img: IMAGE_UNAVAILABLE
    )

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "warn"
    assert docker_phase["reason_code"] == DOCKER_UNAVAILABLE


def test_overall_status_skipped_stays_ok(tmp_path: Path) -> None:
    """A report of only ok + skipped phases rolls up to ok."""
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n  security:\n    egress:\n      mode: open\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    statuses = {p["status"] for p in report["phases"]}
    assert statuses == {"ok", "skipped"}
    assert report["status"] == "ok"


def test_default_image_probe_present(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(args, **_kwargs):
        assert args[:3] == ["docker", "image", "inspect"]
        return SimpleNamespace(returncode=0, stdout="sha256:abc", stderr="")

    monkeypatch.setattr(profile_doctor.subprocess, "run", _run)
    assert _default_image_probe("postgres:16") == IMAGE_PRESENT


def test_default_image_probe_pullable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(args, **_kwargs):
        if args[1] == "image":
            return SimpleNamespace(returncode=1, stdout="", stderr="No such image")
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(profile_doctor.subprocess, "run", _run)
    assert _default_image_probe("postgres:16") == IMAGE_PULLABLE


def test_default_image_probe_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        profile_doctor.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="denied"),
    )
    assert _default_image_probe("private/db:latest") == IMAGE_UNREACHABLE


def test_default_image_probe_unavailable_when_cli_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(*_a, **_k):
        raise FileNotFoundError("docker not found")

    monkeypatch.setattr(profile_doctor.subprocess, "run", _run)
    assert _default_image_probe("postgres:16") == IMAGE_UNAVAILABLE


def test_default_image_probe_unavailable_when_manifest_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(args, **_kwargs):
        if args[1] == "image":
            return SimpleNamespace(returncode=1, stdout="", stderr="No such image")
        raise subprocess.TimeoutExpired(cmd=args, timeout=30.0)

    monkeypatch.setattr(profile_doctor.subprocess, "run", _run)
    assert _default_image_probe("postgres:16") == IMAGE_UNAVAILABLE
