"""CLI tests for ``awf profile doctor``."""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from typer.testing import CliRunner

from awf.cli.main import app

_runner = CliRunner()

pytestmark = pytest.mark.unit


def _ok_report(repo: str = "/repo") -> dict:
    return {
        "status": "ok",
        "repo": repo,
        "phases": [
            {
                "name": "profile_resolution",
                "status": "ok",
                "reason_code": "PROFILE_DOCTOR_PROFILE_RESOLVED",
                "message": "Resolved profile 'generic'.",
                "evidence": {},
                "action": "No action required.",
            },
        ],
        "next_actions": [],
    }


def _stub(monkeypatch: pytest.MonkeyPatch, report: dict) -> None:
    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        lambda *_a, **_k: report,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # Isolate the report-shaping tests from the real settings resolver, which
    # runs ``validate_production_settings`` and can raise depending on ambient
    # env (e.g. AWF_ENV/database URL). These tests assert CLI output shape only,
    # so the resolved settings just need to be deterministic and non-throwing.
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home="~",
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {},
    )


def test_doctor_json_exit_zero_on_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _stub(monkeypatch, _ok_report(str(tmp_path)))

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["status"] == "ok"
    assert output["phases"][0]["name"] == "profile_resolution"


def test_doctor_json_exit_one_on_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = _ok_report(str(tmp_path))
    report["status"] = "fail"
    report["phases"][0]["status"] = "fail"
    report["phases"][0]["reason_code"] = "SECRET_LEASE_SOURCE_MISSING"
    _stub(monkeypatch, report)

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 1
    output = json.loads(result.stdout)
    assert output["status"] == "fail"


def test_doctor_pretty_renders_human_lines(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    report = _ok_report(str(tmp_path))
    report["phases"][0]["action"] = "No action required."
    report["phases"].append(
        {
            "name": "secret_leases",
            "status": "warn",
            "reason_code": "PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING",
            "message": "1 optional secret lease(s) have no source.",
            "evidence": {},
            "action": "Provide the optional secret sources.",
        }
    )
    report["status"] = "warn"
    report["next_actions"] = ["Provide the optional secret sources."]
    _stub(monkeypatch, report)

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path), "--format", "pretty"])

    assert result.exit_code == 0
    assert "AWF profile doctor: warn" in result.stdout
    assert f"Repo: {tmp_path}" in result.stdout
    assert "[ok] profile_resolution" in result.stdout
    assert "[warn] secret_leases" in result.stdout
    assert "reason: PROFILE_DOCTOR_SECRET_LEASES_OPTIONAL_MISSING" in result.stdout
    assert "action: Provide the optional secret sources." in result.stdout
    assert "Next actions:" in result.stdout


def test_doctor_rejects_missing_repo_path(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    result = _runner.invoke(app, ["profile", "doctor", str(missing)])

    assert result.exit_code != 0
    assert "does not exist" in result.output


def test_doctor_rejects_file_repo_path(tmp_path: Path) -> None:
    a_file = tmp_path / "checkout.txt"
    a_file.write_text("not a directory")

    result = _runner.invoke(app, ["profile", "doctor", str(a_file)])

    assert result.exit_code != 0
    assert "is a file" in result.output


def test_doctor_probes_worker_host_home_not_process_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The doctor must probe secret leases against the worker's ``settings.host_home``.

    ``build_worker_runtime`` resolves leases with ``Path(settings.host_home)``;
    when ``AWF_HOST_HOME`` diverges from the shell's ``HOME`` the CLI must forward
    that same home so the preflight checks the directory provisioning actually
    uses, not ``Path.home()``.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # Pin the merged Compose env view so the assertion does not depend on a real
    # docker/compose/.env (which may inject provider creds in some environments).
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {},
    )
    host_home = tmp_path / "worker-host-home"
    host_home.mkdir()
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(host_home),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    assert captured["host_home"] == host_home.expanduser().resolve()


def test_doctor_forwards_service_github_token_to_host_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The doctor must probe leases with the worker's effective token env.

    ``build_worker_runtime`` exports ``settings.github_token`` into
    ``GH_TOKEN``/``GITHUB_TOKEN`` before constructing its secret-lease resolver.
    When the token lives in service settings / .env but is not exported in the
    current shell, the CLI must forward it via ``host_env`` so a ``provider:
    github`` lease provisioning would satisfy is not falsely reported as
    ``SECRET_LEASE_SOURCE_MISSING``.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # Pin the merged Compose env view so the explicit token forward is asserted
    # against a known-empty base, not whatever a real docker/compose/.env holds.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token="ghp_doctor",
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    host_env = captured["host_env"]
    assert isinstance(host_env, dict)
    assert host_env["GH_TOKEN"] == "ghp_doctor"
    assert host_env["GITHUB_TOKEN"] == "ghp_doctor"


def test_doctor_host_env_includes_service_compose_env_file_creds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The doctor must probe leases with the env Compose forwards to the worker.

    ``build_worker_runtime`` constructs its ``LocalSecretLeaseMountResolver`` with
    ``host_env=os.environ`` from inside the service container, where Compose
    forwards ``BITBUCKET_API_TOKEN``/``BITBUCKET_EMAIL`` from
    ``docker/compose/.env``. When those credentials live only in that env file and
    are not exported in the caller shell, the CLI must source ``host_env`` from the
    same merged Compose view (``local_service_environ``) so a ``provider:
    bitbucket`` lease provisioning would satisfy is not falsely reported as
    ``SECRET_LEASE_SOURCE_MISSING``.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    monkeypatch.delenv("BITBUCKET_API_TOKEN", raising=False)
    monkeypatch.delenv("BITBUCKET_EMAIL", raising=False)
    # Simulate creds present only in docker/compose/.env (not the caller shell).
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {
            "BITBUCKET_API_TOKEN": "bb_token",
            "BITBUCKET_EMAIL": "dev@example.com",
        },
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    host_env = captured["host_env"]
    assert isinstance(host_env, dict)
    assert host_env["BITBUCKET_API_TOKEN"] == "bb_token"
    assert host_env["BITBUCKET_EMAIL"] == "dev@example.com"


def test_doctor_host_env_omits_token_when_settings_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No service token => the doctor forwards the shell env unchanged.

    A ``None`` ``settings.github_token`` must not inject empty GH_TOKEN/GITHUB_TOKEN
    keys, which would otherwise mask a genuinely missing token source.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    # Pin the merged Compose env view so the absence assertion does not depend on
    # a real docker/compose/.env that may carry GH_TOKEN/GITHUB_TOKEN; otherwise
    # this guard would pass without the file and fail with it.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    host_env = captured["host_env"]
    assert isinstance(host_env, dict)
    assert "GH_TOKEN" not in host_env
    assert "GITHUB_TOKEN" not in host_env


def test_doctor_forwards_configured_agent_runtime_image(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The doctor must probe the worker's configured agent runtime image.

    ``build_worker_runtime`` renders an ``agent`` service from
    ``settings.agent_runtime_image`` into every workspace stack. The CLI must
    forward that image so a missing/private custom ``AWF_AGENT_RUNTIME_IMAGE``
    fails preflight instead of breaking ``docker compose up`` at provision time.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # Pin the merged Compose env view so this test does not touch a real
    # docker/compose/.env while exercising the image-forwarding path.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="registry.example.com/custom-agent:9",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    assert captured["agent_runtime_image"] == "registry.example.com/custom-agent:9"


def test_doctor_prefers_compose_env_agent_runtime_image_over_bare_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A custom AWF_AGENT_RUNTIME_IMAGE in the Compose env must win over bare settings.

    The worker resolves ``settings.agent_runtime_image`` from INSIDE the service
    container, where Compose forwards ``AWF_AGENT_RUNTIME_IMAGE`` from
    ``docker/compose/.env`` so ``Settings()`` reads the custom image. The doctor's
    ``resolve_service_settings()`` only reads an ``AWF_``-prefixed, cwd-relative
    ``.env``, so when the doctor runs from another cwd ``settings.agent_runtime_image``
    falls back to the bare default while the worker pulls the custom image. The CLI
    must source the image from the merged Compose view (``host_env``) first -- exactly
    like the ``DOCKER_HOST`` pin -- so preflight probes the image the worker really uses.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # The merged Compose view carries the custom image the worker receives, while
    # the cwd-relative Settings() only sees the bare default.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {"AWF_AGENT_RUNTIME_IMAGE": "registry.example.com/custom-agent:9"},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    assert captured["agent_runtime_image"] == "registry.example.com/custom-agent:9"


def test_doctor_threads_service_docker_environ_to_image_probes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The doctor must probe images against the worker's Docker daemon/config.

    The worker selects its daemon from the resolved service environment
    (``AWF_DOCKER_HOST`` -> ``settings.docker_host``) and reads client config
    (``DOCKER_CONFIG``) from ``docker/compose/.env``. The CLI must forward a
    ``docker_environ`` that merges that service env and forces ``DOCKER_HOST`` to
    the resolved daemon so the probes inspect the same daemon the worker's compose
    pulls use -- not whatever the caller shell points at.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # Service env carries a DOCKER_CONFIG the caller shell does not export.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {"DOCKER_CONFIG": "/svc/.docker"},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="tcp://remote:2375",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    docker_environ = captured["docker_environ"]
    assert isinstance(docker_environ, dict)
    # DOCKER_HOST forced to the resolved daemon; DOCKER_CONFIG carried from the
    # merged service env so the probe reads the worker's client config.
    assert docker_environ["DOCKER_HOST"] == "tcp://remote:2375"
    assert docker_environ["DOCKER_CONFIG"] == "/svc/.docker"


def test_doctor_scrubs_docker_context_when_forcing_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A stray DOCKER_CONTEXT must not survive when an env host pins the daemon.

    Docker's CLI treats ``DOCKER_CONTEXT`` as overriding ``DOCKER_HOST``, so a
    context inherited from the caller shell or the Compose env file would
    redirect the image probes to a different daemon than the env-selected
    ``DOCKER_HOST`` this block pins. When the service env names an
    ``AWF_DOCKER_HOST``/``DOCKER_HOST``, ``bootstrap._docker_cli_environ`` scrubs
    ``DOCKER_CONTEXT`` so the pinned daemon wins; the doctor must mirror that or a
    stale context would redirect the probe to the wrong daemon.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # The merged service env selects a daemon via DOCKER_HOST and carries a stale
    # DOCKER_CONTEXT alongside config -- exactly the case the worker scrubs.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {
            "DOCKER_HOST": "tcp://remote:2375",
            "DOCKER_CONTEXT": "desktop-linux",
            "DOCKER_CONFIG": "/svc/.docker",
        },
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    docker_environ = captured["docker_environ"]
    assert isinstance(docker_environ, dict)
    assert docker_environ["DOCKER_HOST"] == "tcp://remote:2375"
    # DOCKER_CONTEXT scrubbed so it cannot override the forced DOCKER_HOST.
    assert "DOCKER_CONTEXT" not in docker_environ
    # Other client config still threads through to the probe.
    assert docker_environ["DOCKER_CONFIG"] == "/svc/.docker"


def test_doctor_preserves_context_only_daemon_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A context-only daemon selection must survive into the probe environment.

    When the service env selects the daemon via ``DOCKER_CONTEXT`` alone -- no
    ``AWF_DOCKER_HOST``/``DOCKER_HOST`` -- ``bootstrap._docker_cli_environ`` does
    NOT scrub the context (its scrub is guarded by ``if docker_host or
    clears_docker_host``), so the worker provisions against the context-backed
    daemon. The doctor must mirror that: preserve ``DOCKER_CONTEXT`` and pin no
    ``DOCKER_HOST``, rather than stripping the context and falling back to
    ``settings.docker_host`` -- which would probe the default socket while the
    worker pulls against the context daemon.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # The service env selects the daemon via DOCKER_CONTEXT only -- no host pinned.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {"DOCKER_CONTEXT": "desktop-linux", "DOCKER_CONFIG": "/svc/.docker"},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    docker_environ = captured["docker_environ"]
    assert isinstance(docker_environ, dict)
    # The context-backed daemon selection is preserved; no DOCKER_HOST is pinned
    # over it (which would otherwise redirect the probe to the wrong daemon).
    assert docker_environ["DOCKER_CONTEXT"] == "desktop-linux"
    assert "DOCKER_HOST" not in docker_environ
    # Other client config still threads through to the probe.
    assert docker_environ["DOCKER_CONFIG"] == "/svc/.docker"


def test_doctor_scrubs_cleared_docker_client_keys_from_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Docker CLI client key the service env clears must not survive into the probe.

    The worker's ``bootstrap._docker_cli_environ`` scrubs Docker CLI client keys
    (e.g. ``DOCKER_TLS_VERIFY``/``DOCKER_CONFIG``) that the resolved service
    environment explicitly blanks while the caller shell still exports them, via
    ``cleared_docker_cli_client_keys``. The doctor must mirror that scrub or a stale
    caller client key would redirect the image probe to a different daemon/config
    than the worker's compose pulls, so a green preflight would not match
    provisioning.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # The caller shell exports a stale DOCKER_TLS_VERIFY the service env blanks.
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {"DOCKER_TLS_VERIFY": "", "DOCKER_CONFIG": "/svc/.docker"},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="tcp://remote:2375",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    docker_environ = captured["docker_environ"]
    assert isinstance(docker_environ, dict)
    assert docker_environ["DOCKER_HOST"] == "tcp://remote:2375"
    # The cleared client key is scrubbed so it cannot redirect the probe daemon.
    assert "DOCKER_TLS_VERIFY" not in docker_environ
    # Unrelated client config still threads through to the probe.
    assert docker_environ["DOCKER_CONFIG"] == "/svc/.docker"


def test_doctor_prefers_compose_env_docker_host_over_bare_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The probe must target the daemon the merged Compose env names.

    ``settings.docker_host`` comes from a bare ``Settings()`` that only reads an
    ``AWF_``-prefixed, cwd-relative ``.env``, while the lease context (``host_env``)
    comes from ``local_service_environ()`` which resolves the Compose ``.env`` the
    worker actually receives. When ``AWF_DOCKER_HOST`` lives only in that Compose
    env file, the merged view carries the worker's real daemon while the bare
    settings value stays on the default socket. The probe must follow the merged
    view's ``AWF_DOCKER_HOST`` so preflight matches provisioning.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # AWF_DOCKER_HOST is present only in the merged Compose env, not in the bare
    # settings value (which stays on the default socket).
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {"AWF_DOCKER_HOST": "tcp://compose-env:2375"},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    docker_environ = captured["docker_environ"]
    assert isinstance(docker_environ, dict)
    # The merged Compose env's AWF_DOCKER_HOST wins over the bare settings socket.
    assert docker_environ["DOCKER_HOST"] == "tcp://compose-env:2375"


def test_doctor_honours_service_selected_bare_docker_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A service-selected bare DOCKER_HOST must steer the probe, matching the worker.

    The worker's ``bootstrap._docker_cli_environ`` derives its daemon from the
    resolved service environment as ``AWF_DOCKER_HOST`` OR a bare ``DOCKER_HOST``.
    When the merged Compose view (``local_service_environ()`` -- the same view the
    worker's ``raw_service_env`` is built from) names a bare ``DOCKER_HOST`` without
    an ``AWF_DOCKER_HOST``, the worker pulls against that daemon, so the doctor must
    probe it too rather than falling back to the default ``settings.docker_host``
    socket -- otherwise a green preflight would not match provisioning.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # The merged Compose env selects the daemon via a bare DOCKER_HOST only.
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {"DOCKER_HOST": "tcp://compose-bare:2375"},
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    docker_environ = captured["docker_environ"]
    assert isinstance(docker_environ, dict)
    # The merged Compose env's bare DOCKER_HOST wins over the default settings socket.
    assert docker_environ["DOCKER_HOST"] == "tcp://compose-bare:2375"


def test_doctor_prefers_awf_docker_host_over_bare_docker_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``AWF_DOCKER_HOST`` must take precedence over a bare ``DOCKER_HOST``.

    This mirrors ``bootstrap._docker_cli_environ``'s ``AWF_DOCKER_HOST`` OR
    ``DOCKER_HOST`` order so the doctor and worker agree on the daemon when both
    keys are present in the merged Compose view.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {
            "AWF_DOCKER_HOST": "tcp://awf-host:2375",
            "DOCKER_HOST": "tcp://bare-host:2375",
        },
    )
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="unix:///var/run/docker.sock",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    docker_environ = captured["docker_environ"]
    assert isinstance(docker_environ, dict)
    assert docker_environ["DOCKER_HOST"] == "tcp://awf-host:2375"


def test_doctor_mirrors_cleared_docker_host_without_pinning_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A service env that clears DOCKER_HOST must not redirect the probe to settings.

    ``bootstrap._docker_cli_environ`` treats a present-but-empty service DOCKER_HOST
    -- while the caller shell still exports a non-empty host -- as
    ``clears_docker_host``: it scrubs DOCKER_HOST/DOCKER_CONTEXT and pins NOTHING, so
    the worker's compose pulls fall back to the default daemon. The doctor must mirror
    that: leave DOCKER_HOST unset rather than treating the empty host as absent and
    pinning ``settings.docker_host`` -- which would probe a different daemon than the
    worker uses.
    """
    captured: dict[str, object] = {}

    def _capture(*_args: object, **kwargs: object) -> dict:
        captured.update(kwargs)
        return _ok_report(str(tmp_path))

    monkeypatch.setattr(
        "awf.service.profile_doctor.collect_profile_doctor_report",
        _capture,
    )
    monkeypatch.setattr(
        "awf.common.git_remote.detect_repo_url_from_checkout",
        lambda _path: None,
    )
    # The caller shell exports a stray host the service env explicitly clears.
    monkeypatch.setenv("DOCKER_HOST", "tcp://stray-caller:2375")
    monkeypatch.setattr(
        "awf.service.config.local_service_environ",
        lambda: {"DOCKER_HOST": "", "DOCKER_CONFIG": "/svc/.docker"},
    )
    # settings.docker_host is a non-default socket: if the doctor pinned it the probe
    # would diverge from the worker's default-daemon fallback.
    monkeypatch.setattr(
        "awf.service.config.resolve_service_settings",
        lambda: types.SimpleNamespace(
            host_home=str(tmp_path),
            github_token=None,
            agent_runtime_image="awf-agent-runtime:latest",
            docker_host="tcp://settings-only:2375",
        ),
    )

    result = _runner.invoke(app, ["profile", "doctor", str(tmp_path)])

    assert result.exit_code == 0
    docker_environ = captured["docker_environ"]
    assert isinstance(docker_environ, dict)
    # No DOCKER_HOST is pinned -- the probe falls back to the same default daemon the
    # worker's compose pulls use, instead of settings.docker_host.
    assert "DOCKER_HOST" not in docker_environ
    # Unrelated client config still threads through to the probe.
    assert docker_environ["DOCKER_CONFIG"] == "/svc/.docker"


def test_doctor_appears_in_profile_help() -> None:
    result = _runner.invoke(app, ["profile", "--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout
