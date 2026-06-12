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
    """A stray DOCKER_CONTEXT must not survive into the probe environment.

    Docker's CLI treats ``DOCKER_CONTEXT`` as overriding ``DOCKER_HOST``, so a
    context inherited from the caller shell or the Compose env file would
    redirect the image probes to a different daemon than ``settings.docker_host``
    even though this block pins ``DOCKER_HOST``. Drop ``DOCKER_CONTEXT`` (matching
    the service Docker helpers' scrub) so the probe cannot inspect the wrong
    daemon.
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
    # The merged service env carries a stale DOCKER_CONTEXT alongside config.
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
            docker_host="tcp://remote:2375",
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


def test_doctor_appears_in_profile_help() -> None:
    result = _runner.invoke(app, ["profile", "--help"])
    assert result.exit_code == 0
    assert "doctor" in result.stdout
