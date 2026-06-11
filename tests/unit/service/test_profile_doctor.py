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
    DOCKER_UNAVAILABLE,
    IMAGE_PRESENT,
    IMAGE_PULLABLE,
    IMAGE_UNAVAILABLE,
    IMAGE_UNREACHABLE,
    PROFILE_DOCTOR_IMAGE_UNREACHABLE,
    PROFILE_DOCTOR_IMAGES_NONE,
    PROFILE_DOCTOR_IMAGES_PRESENT,
    PROFILE_DOCTOR_IMAGES_PULLABLE,
    PROFILE_DOCTOR_LINT_CLEAN,
    PROFILE_DOCTOR_LINT_ERRORS,
    PROFILE_DOCTOR_LINT_WARNINGS,
    PROFILE_DOCTOR_PROFILE_RESOLVED,
    PROFILE_DOCTOR_PROFILE_UNRESOLVED,
    PROFILE_DOCTOR_SECRET_LEASES_OK,
    PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING,
    PROFILE_DOCTOR_SECRET_LEASES_PROBE_ERROR,
    PROFILE_DOCTOR_SERVICE_GRAPH_NONE,
    PROFILE_DOCTOR_SERVICE_GRAPH_OK,
    PROFILE_DOCTOR_SERVICE_GRAPH_UNRESOLVED,
    PROFILE_DOCTOR_SERVICE_PATH_INVALID,
    PROFILE_DOCTOR_SERVICE_PATHS_NONE,
    PROFILE_DOCTOR_SERVICE_PATHS_OK,
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
    assert docker_phase["reason_code"] == PROFILE_DOCTOR_IMAGES_NONE


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


def test_secret_lease_unexpected_oserror_fails_structured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unexpected ``OSError`` from the resolver becomes a structured fail phase.

    The resolver materializes a bitbucket askpass script (mkdir/write/chmod) on the
    git-token path, so a ``PermissionError`` (or similar) can escape the structured
    ``SecretLeaseResolutionError`` handling. The doctor must report it as a phase
    failure — never an uncaught traceback — and must not leak the exception message
    (which can embed a source-adjacent path).
    """
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n",
    )

    class _BoomResolver:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def resolve(self, *_args: object, **_kwargs: object) -> object:
            raise PermissionError(13, "Permission denied", "/secret/adjacent/path")

    monkeypatch.setattr(profile_doctor, "LocalSecretLeaseMountResolver", _BoomResolver)

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    secret_phase = _phase(report, "secret_leases")
    assert secret_phase["status"] == "fail"
    assert secret_phase["reason_code"] == PROFILE_DOCTOR_SECRET_LEASES_PROBE_ERROR
    assert secret_phase["evidence"] == {"error": "PermissionError"}
    assert report["status"] == "fail"
    assert "/secret/adjacent/path" not in json.dumps(report)


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
    for name in (
        "profile_lint",
        "secret_leases",
        "egress",
        "service_paths",
        "service_graph",
        "docker_images",
    ):
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


def test_docker_images_probed_without_dind(tmp_path: Path) -> None:
    """Service images are probed even when docker.mode is none.

    ``docker compose up`` pulls profile service images regardless of
    ``docker.mode``, so an unreachable/private service image must fail the
    preflight instead of being skipped. The managed DinD daemon image is not
    probed because no DinD daemon is started in ``none`` mode.
    """
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n  services:\n    - name: db\n      image: private/db:latest\n",
    )

    probed: list[str] = []

    def _probe(image: str) -> str:
        probed.append(image)
        return IMAGE_UNREACHABLE

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={}, image_probe=_probe)

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "fail"
    assert docker_phase["reason_code"] == PROFILE_DOCTOR_IMAGE_UNREACHABLE
    # The service image is probed; the DinD daemon image is not (mode is none).
    assert probed == ["private/db:latest"]
    assert report["status"] == "fail"


def test_docker_images_skipped_without_images_or_dind(tmp_path: Path) -> None:
    """With no service images and docker.mode none, there is nothing to probe."""
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n",
    )

    probed: list[str] = []

    def _probe(image: str) -> str:
        probed.append(image)
        return IMAGE_PRESENT

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={}, image_probe=_probe)

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "skipped"
    assert docker_phase["reason_code"] == PROFILE_DOCTOR_IMAGES_NONE
    assert probed == []


def test_docker_images_probes_configured_agent_runtime_image(tmp_path: Path) -> None:
    """The configured agent runtime image is probed even without DinD or services.

    Every workspace stack renders an ``agent`` service from
    ``settings.agent_runtime_image``; ``docker compose up`` pulls it like any other
    image. A missing/private custom agent image must therefore fail the preflight
    rather than letting the phase report green and breaking at provision time.
    """
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n",
    )

    probed: list[str] = []

    def _probe(image: str) -> str:
        probed.append(image)
        return IMAGE_UNREACHABLE if image == "private/agent:latest" else IMAGE_PRESENT

    report = collect_profile_doctor_report(
        tmp_path,
        repo_url=None,
        host_env={},
        image_probe=_probe,
        agent_runtime_image="private/agent:latest",
    )

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "fail"
    assert docker_phase["reason_code"] == PROFILE_DOCTOR_IMAGE_UNREACHABLE
    assert "private/agent:latest" in docker_phase["message"]
    assert probed == ["private/agent:latest"]
    assert report["status"] == "fail"


def test_docker_images_agent_runtime_image_deduped_against_services(tmp_path: Path) -> None:
    """An agent image equal to a service image is probed once (dedup branch)."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  docker:\n"
        "    mode: dind\n"
        "  services:\n"
        "    - name: app\n"
        "      image: awf-agent-runtime:latest\n",
    )

    probed: list[str] = []

    def _probe(image: str) -> str:
        probed.append(image)
        return IMAGE_PRESENT

    report = collect_profile_doctor_report(
        tmp_path,
        repo_url=None,
        host_env={},
        image_probe=_probe,
        agent_runtime_image="awf-agent-runtime:latest",
    )

    docker_phase = _phase(report, "docker_images")
    assert docker_phase["status"] == "ok"
    # agent image first, then dind; the duplicate service entry is collapsed.
    assert probed == ["awf-agent-runtime:latest", "docker:27-dind"]


def test_service_paths_no_services_skipped(tmp_path: Path) -> None:
    """A profile that declares no services has no repo-relative paths to validate."""
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n  security:\n    egress:\n      mode: open\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    service_phase = _phase(report, "service_paths")
    assert service_phase["status"] == "skipped"
    assert service_phase["reason_code"] == PROFILE_DOCTOR_SERVICE_PATHS_NONE


def test_service_paths_valid_relative_ok(tmp_path: Path) -> None:
    """A build-only service with a workspace-relative build_context resolves ok."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  docker:\n"
        "    mode: none\n"
        "  services:\n"
        "    - name: app\n"
        "      build_context: .\n"
        "      dockerfile: Dockerfile\n",
    )

    report = collect_profile_doctor_report(
        tmp_path, repo_url=None, host_env={}, image_probe=lambda _image: IMAGE_PRESENT
    )

    service_phase = _phase(report, "service_paths")
    assert service_phase["status"] == "ok"
    assert service_phase["reason_code"] == PROFILE_DOCTOR_SERVICE_PATHS_OK
    assert service_phase["evidence"]["services"] == ["app"]


def test_service_paths_escaping_build_context_fails(tmp_path: Path) -> None:
    """A build-only service whose build_context escapes the worktree fails preflight.

    The image phase skips build-only services (no image to pull), so this is the
    only phase that catches a path provisioning would later reject via
    ``profile_services(profile, base_path=worktree_path)``.
    """
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  docker:\n"
        "    mode: none\n"
        "  services:\n"
        "    - name: app\n"
        "      build_context: ../escape\n"
        "      dockerfile: Dockerfile\n",
    )

    report = collect_profile_doctor_report(
        tmp_path, repo_url=None, host_env={}, image_probe=lambda _image: IMAGE_PRESENT
    )

    service_phase = _phase(report, "service_paths")
    assert service_phase["status"] == "fail"
    assert service_phase["reason_code"] == PROFILE_DOCTOR_SERVICE_PATH_INVALID
    assert "escapes workspace root" in service_phase["message"]
    # The docker_images phase still skips the build-only service (nothing to pull),
    # so the green build path would otherwise mask the rejection.
    assert _phase(report, "docker_images")["status"] == "skipped"
    # The graph phase cannot validate edges it could not resolve, so it defers to
    # the service_paths failure rather than double-reporting the same root cause.
    graph_phase = _phase(report, "service_graph")
    assert graph_phase["status"] == "skipped"
    assert graph_phase["reason_code"] == PROFILE_DOCTOR_SERVICE_GRAPH_UNRESOLVED
    assert report["status"] == "fail"


def test_service_graph_no_services_skipped(tmp_path: Path) -> None:
    """A profile that declares no services has no dependency graph to validate."""
    _write_profile(
        tmp_path,
        "awf:\n  name: generic\n  security:\n    egress:\n      mode: open\n",
    )

    report = collect_profile_doctor_report(tmp_path, repo_url=None, host_env={})

    graph_phase = _phase(report, "service_graph")
    assert graph_phase["status"] == "skipped"
    assert graph_phase["reason_code"] == PROFILE_DOCTOR_SERVICE_GRAPH_NONE


def test_service_graph_valid_dependency_ok(tmp_path: Path) -> None:
    """A service depending on a healthchecked sibling resolves to a green graph."""
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  docker:\n"
        "    mode: none\n"
        "  services:\n"
        "    - name: redis\n"
        "      image: redis:7-alpine\n"
        "      healthcheck_cmd: redis-cli ping\n"
        "    - name: app\n"
        "      image: app:latest\n"
        "      depends_on:\n"
        "        - redis\n",
    )

    report = collect_profile_doctor_report(
        tmp_path, repo_url=None, host_env={}, image_probe=lambda _image: IMAGE_PRESENT
    )

    graph_phase = _phase(report, "service_graph")
    assert graph_phase["status"] == "ok"
    assert graph_phase["reason_code"] == PROFILE_DOCTOR_SERVICE_GRAPH_OK
    assert graph_phase["evidence"]["services"] == ["redis", "app"]


def test_service_graph_unknown_dependency_fails(tmp_path: Path) -> None:
    """A depends_on target that is not a declared service fails the graph phase.

    Provisioning runs the same ``validate_companion_service_graph`` in
    ``ComposeStackLauncher.launch``, so without this phase the doctor would report
    ``service_paths`` ok for a graph provisioning later rejects.
    """
    _write_profile(
        tmp_path,
        "awf:\n"
        "  name: generic\n"
        "  docker:\n"
        "    mode: none\n"
        "  services:\n"
        "    - name: app\n"
        "      image: app:latest\n"
        "      depends_on:\n"
        "        - missing\n",
    )

    report = collect_profile_doctor_report(
        tmp_path, repo_url=None, host_env={}, image_probe=lambda _image: IMAGE_PRESENT
    )

    # The path probe is green -- only the graph probe catches the dangling edge.
    assert _phase(report, "service_paths")["status"] == "ok"
    graph_phase = _phase(report, "service_graph")
    assert graph_phase["status"] == "fail"
    assert graph_phase["reason_code"] == "COMPANION_SERVICE_DEPENDENCY_UNKNOWN"
    assert "app->missing" in graph_phase["evidence"]["error"]
    assert report["status"] == "fail"


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


def test_default_image_probe_unavailable_when_daemon_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-zero exit with a daemon-connection error degrades to ``unavailable``.

    When the docker daemon is not running, ``docker image inspect`` still exits
    non-zero with a connection error on stderr. That must read as
    ``DOCKER_UNAVAILABLE`` (a warning), not flow through to ``manifest inspect``
    and report a hard ``IMAGE_UNREACHABLE`` failure.
    """

    def _run(args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
                "Is the docker daemon running?"
            ),
        )

    monkeypatch.setattr(profile_doctor.subprocess, "run", _run)
    assert _default_image_probe("postgres:16") == IMAGE_UNAVAILABLE
