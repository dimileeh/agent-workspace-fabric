"""No-Docker compose coverage for profile-declared workspace services (part 1).

Shared fixtures/helpers live here; ``test_workspace_services_compose_part_002``
imports them. Split from the original monolithic module to stay under the
first-party file line limit (see ``test_core_decomposition_maintainability``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, ComposeProjectPaths, WorkspaceComposeSpec
from awf.profiles.compose import (
    AGENT_AUTH_ENV_VARS,
    agent_environment_keys_from_compose_file,
    agent_environment_with_declared_secret_leases,
    agent_environment_with_github_token,
    agent_environment_with_legacy_host_auth,
    agent_exec_env_passthrough,
    filter_hosted_env_passthrough_names,
    profile_agent_environment,
    profile_app_endpoint_environment,
    profile_services,
    resolve_app_endpoints,
    resolve_profile_app_endpoints,
)
from awf.profiles.models import EndpointVisibility, ProfileAppEndpoint, WorkspaceProfile
from awf.profiles.resolver import ProfileResolver

_FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "workspace_services" / "dockerized_app"
)
_POSTGRES_FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "workspace_services" / "python_postgres_app"
)
_NODE_BROWSER_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "workspace_services"
    / "node_next_browser_app"
)
_TEMPLATE = Path(__file__).resolve().parents[4] / "docker" / "compose" / "workspace.base.yml.j2"


class _RecordingCompose:
    def __init__(self) -> None:
        self.specs: list[WorkspaceComposeSpec] = []
        self.waits: list[bool] = []

    async def up(
        self,
        spec: WorkspaceComposeSpec,
        *,
        wait: bool = True,
        on_compose_up_started: Any | None = None,
    ) -> ComposeProjectPaths:
        if on_compose_up_started is not None:
            await on_compose_up_started()
        self.specs.append(spec)
        self.waits.append(wait)
        return ComposeProjectPaths(
            project_dir=Path("/tmp/awf-compose/ws_services"),
            compose_file=Path("/tmp/awf-compose/ws_services/compose.yml"),
        )


def _load_profile() -> WorkspaceProfile:
    assert _FIXTURE.is_dir(), "workspace-services fixture is missing"
    return ProfileResolver().resolve(worktree_path=_FIXTURE, profile_ref="auto").profile


def _load_postgres_profile() -> WorkspaceProfile:
    assert _POSTGRES_FIXTURE.is_dir(), "python-postgres workspace-services fixture is missing"
    return ProfileResolver().resolve(worktree_path=_POSTGRES_FIXTURE, profile_ref="auto").profile


def _load_node_browser_profile() -> WorkspaceProfile:
    assert _NODE_BROWSER_FIXTURE.is_dir(), "node browser workspace-services fixture is missing"
    return (
        ProfileResolver()
        .resolve(
            worktree_path=_NODE_BROWSER_FIXTURE,
            profile_ref="auto",
        )
        .profile
    )


def _load_awf_self_profile() -> tuple[Path, WorkspaceProfile]:
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root, ProfileResolver().resolve(worktree_path=repo_root, profile_ref="auto").profile


def _clear_host_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*AGENT_AUTH_ENV_VARS, "AWF_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
def test_resolve_app_endpoints_generates_deterministic_internal_urls() -> None:
    endpoints = {
        endpoint["name"]: endpoint
        for endpoint in resolve_app_endpoints(_load_node_browser_profile())
    }

    assert endpoints == {
        "app": {
            "name": "app",
            "service": "app",
            "scheme": "http",
            "port": 3000,
            "path": "/",
            "internal_url": "http://app:3000/",
            "visibility": "agent",
            "health": {
                "path": "/healthz",
                "method": "GET",
                "expected_status": 200,
                "internal_url": "http://app:3000/healthz",
            },
        },
        "browser_validation": {
            "name": "browser_validation",
            "service": "browser",
            "scheme": "http",
            "port": 9323,
            "path": "/validate",
            "internal_url": "http://browser:9323/validate",
            "visibility": "validation",
            "health": {
                "path": "/healthz",
                "method": "GET",
                "expected_status": 200,
                "internal_url": "http://browser:9323/healthz",
            },
        },
        "operator_notes": {
            "name": "operator_notes",
            "service": "app",
            "scheme": "http",
            "port": 3000,
            "path": "/operator",
            "internal_url": "http://app:3000/operator",
            "visibility": "console",
            "health": None,
        },
    }


@pytest.mark.unit
def test_profile_agent_environment_exposes_only_agent_and_validation_app_endpoints() -> None:
    env = dict(profile_agent_environment(_load_node_browser_profile()))

    assert env["AWF_APP_ENDPOINT_APP_URL"] == "http://app:3000/"
    assert env["AWF_APP_ENDPOINT_BROWSER_VALIDATION_URL"] == ("http://browser:9323/validate")
    assert "AWF_APP_ENDPOINT_OPERATOR_NOTES_URL" not in env

    endpoints = json.loads(env["AWF_APP_ENDPOINTS_JSON"])
    assert [endpoint["name"] for endpoint in endpoints] == ["app", "browser_validation"]
    assert endpoints[0]["internal_url"] == "http://app:3000/"
    assert endpoints[1]["health"]["internal_url"] == "http://browser:9323/healthz"


@pytest.mark.unit
def test_resolve_profile_app_endpoints_excludes_internal_when_disabled() -> None:
    endpoints = [
        ProfileAppEndpoint(name="agent_ep", service="app", port=3000),
        ProfileAppEndpoint(
            name="internal_ep",
            service="metrics",
            port=9090,
            visibility=EndpointVisibility.internal,
        ),
    ]

    visible = resolve_profile_app_endpoints(endpoints, include_internal=False)
    assert [ep["name"] for ep in visible] == ["agent_ep"]

    all_endpoints = resolve_profile_app_endpoints(endpoints, include_internal=True)
    assert [ep["name"] for ep in all_endpoints] == ["agent_ep", "internal_ep"]


@pytest.mark.unit
def test_profile_app_endpoint_environment_uses_resolved_endpoints_argument() -> None:
    resolved = resolve_profile_app_endpoints(
        [
            ProfileAppEndpoint(name="agent_ep", service="app", port=3000),
            ProfileAppEndpoint(
                name="console_ep",
                service="app",
                port=3000,
                path="/operator",
                visibility=EndpointVisibility.console,
            ),
        ]
    )

    env = dict(
        profile_app_endpoint_environment(_load_node_browser_profile(), resolved_endpoints=resolved)
    )

    payload = json.loads(env["AWF_APP_ENDPOINTS_JSON"])
    assert [endpoint["name"] for endpoint in payload] == ["agent_ep"]
    assert "AWF_APP_ENDPOINT_BROWSER_VALIDATION_URL" not in env


@pytest.mark.unit
def test_agent_environment_with_declared_secret_leases_merges_before_legacy() -> None:
    base: tuple[tuple[str, str], ...] = (("AWF_GITHUB_TOKEN", "legacy-token"),)
    leases: tuple[tuple[str, str], ...] = (("AWF_SECRET_DB_PASSWORD", "leased-value"),)

    merged = dict(agent_environment_with_declared_secret_leases(base, leases))

    assert merged["AWF_SECRET_DB_PASSWORD"] == "leased-value"
    assert merged["AWF_GITHUB_TOKEN"] == "legacy-token"


@pytest.mark.unit
def test_awf_self_profile_renders_workspace_local_test_postgres(
    tmp_path: Path,
) -> None:
    repo_root, profile = _load_awf_self_profile()
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)
    paths = manager.render(
        WorkspaceComposeSpec(
            workspace_id="ws_awf_self",
            worktree_host_path=repo_root,
            postgres_password="workspace-secret",
            agent_environment=profile_agent_environment(profile),
            services=profile_services(profile, base_path=repo_root),
        )
    )

    rendered = paths.compose_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(rendered)
    agent = parsed["services"]["agent"]
    postgres = parsed["services"]["postgres"]

    assert agent["environment"]["AWF_DATABASE_URL"] == (
        "postgresql+asyncpg://awf:workspace-secret@postgres:5432/awf"
    )
    assert agent["environment"]["AWF_TEST_DATABASE_URL"] == (
        "postgresql+asyncpg://awf:workspace-secret@postgres:5432/awf"
    )
    assert "host.docker.internal:5433" not in rendered
    assert agent["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["environment"] == {
        "POSTGRES_DB": "awf",
        "POSTGRES_PASSWORD": "workspace-secret",
        "POSTGRES_USER": "awf",
    }
    assert "ports" not in postgres
    assert parsed["volumes"]["postgres_data"]["name"] == "awf-ws_awf_self-postgres_data"


@pytest.mark.unit
def test_github_token_placeholder_preserves_profile_supplied_agent_env() -> None:
    env = agent_environment_with_github_token(
        (("GH_TOKEN", "${WORKSPACE_GH_TOKEN}"),),
        host_env={"AWF_GITHUB_TOKEN": "ghp_host_secret"},
    )

    assert env == (
        ("GH_TOKEN", "${WORKSPACE_GH_TOKEN}"),
        ("GITHUB_TOKEN", "${AWF_GITHUB_TOKEN}"),
    )


@pytest.mark.unit
def test_github_token_group_not_overridden_when_profile_owns_github_token() -> None:
    # The profile owns ONLY GITHUB_TOKEN (the lower-precedence alias, e.g. a generic
    # ``env`` secret lease). The GitHub CLI reads GH_TOKEN, GITHUB_TOKEN "in order of
    # precedence", so injecting the worker's higher-precedence GH_TOKEN would shadow
    # the profile-owned token and make agent ``gh`` commands use the worker credential.
    # Treat the aliases as a group: skip the worker GH_TOKEN so the profile token wins.
    env = agent_environment_with_github_token(
        (("GITHUB_TOKEN", "${MY_PROFILE_LEASE_TOKEN}"),),
        host_env={"GH_TOKEN": "ghp_worker_secret"},
    )

    assert env == (("GITHUB_TOKEN", "${MY_PROFILE_LEASE_TOKEN}"),)


@pytest.mark.unit
def test_profile_ollama_host_suppresses_worker_base_url_placeholder() -> None:
    # The profile owns the daemon by declaring only the lower-precedence
    # OLLAMA_HOST. A stale higher-precedence AWF_OPENCODE_OLLAMA_BASE_URL in the
    # worker env must NOT be injected, or the agent's OpenCode launcher would
    # talk to a different daemon than AWF's preflight readied.
    env = agent_environment_with_legacy_host_auth(
        (("OLLAMA_HOST", "http://ollama.profile:11434"),),
        host_env={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://stale.worker:11434/v1"},
    )

    assert env == (("OLLAMA_HOST", "http://ollama.profile:11434"),)


@pytest.mark.unit
def test_profile_base_url_still_allows_worker_ollama_host_placeholder() -> None:
    # The profile declares the highest-precedence key; the lower-precedence
    # OLLAMA_HOST cannot shadow it, so injecting the worker value is harmless.
    env = agent_environment_with_legacy_host_auth(
        (("AWF_OPENCODE_OLLAMA_BASE_URL", "http://ollama.profile:11434/v1"),),
        host_env={"OLLAMA_HOST": "http://worker:11434"},
    )

    assert env == (
        ("AWF_OPENCODE_OLLAMA_BASE_URL", "http://ollama.profile:11434/v1"),
        ("OLLAMA_HOST", "${OLLAMA_HOST}"),
    )


@pytest.mark.unit
def test_worker_ollama_base_url_injected_when_profile_declares_none() -> None:
    # No profile-declared Ollama key — the worker base URL flows through as a
    # placeholder unchanged (the pre-existing behavior).
    env = agent_environment_with_legacy_host_auth(
        (),
        host_env={"AWF_OPENCODE_OLLAMA_BASE_URL": "http://worker:11434/v1"},
    )

    assert env == (("AWF_OPENCODE_OLLAMA_BASE_URL", "${AWF_OPENCODE_OLLAMA_BASE_URL}"),)


@pytest.mark.unit
def test_profile_ollama_host_suppresses_worker_base_url_exec_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_host_auth(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-worker")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "environment": {
                            "WORKSPACE_ID": "ws_123",
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert "OLLAMA_HOST" not in passthrough
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in passthrough
    assert "OPENAI_API_KEY" in passthrough


@pytest.mark.unit
def test_agent_exec_env_passthrough_fails_closed_when_compose_unreadable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_host_auth(monkeypatch)
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.worker:11434")

    missing = tmp_path / "missing.yml"
    assert not missing.exists()
    passthrough = agent_exec_env_passthrough(compose_file=missing)
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in passthrough
    assert "OLLAMA_HOST" in passthrough

    bad_yaml = tmp_path / "bad-yaml.yml"
    bad_yaml.write_text("services:\n  agent:\n    - [\n", encoding="utf-8")
    passthrough = agent_exec_env_passthrough(compose_file=bad_yaml)
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in passthrough


@pytest.mark.unit
def test_agent_exec_env_passthrough_includes_worker_ollama_when_compose_has_no_env_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_host_auth(monkeypatch)
    monkeypatch.setenv("AWF_OPENCODE_OLLAMA_BASE_URL", "http://ollama.worker:11434/v1")

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump({"services": {"agent": {"image": "agent:latest"}}}),
        encoding="utf-8",
    )

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert "AWF_OPENCODE_OLLAMA_BASE_URL" in passthrough


@pytest.mark.unit
def test_agent_environment_keys_from_compose_file_rejects_invalid_shapes(
    tmp_path: Path,
) -> None:
    assert agent_environment_keys_from_compose_file(tmp_path / "missing.yml") == frozenset()

    scalar = tmp_path / "scalar.yml"
    scalar.write_text("not-a-mapping\n", encoding="utf-8")
    assert agent_environment_keys_from_compose_file(scalar) == frozenset()

    no_services = tmp_path / "no-services.yml"
    no_services.write_text("version: '3'\n", encoding="utf-8")
    assert agent_environment_keys_from_compose_file(no_services) == frozenset()

    no_agent = tmp_path / "no-agent.yml"
    no_agent.write_text(
        yaml.safe_dump({"services": {"postgres": {"image": "postgres:16"}}}),
        encoding="utf-8",
    )
    assert agent_environment_keys_from_compose_file(no_agent) == frozenset()

    bad_yaml = tmp_path / "bad-yaml.yml"
    bad_yaml.write_text("services:\n  agent:\n    - [\n", encoding="utf-8")
    assert agent_environment_keys_from_compose_file(bad_yaml) == frozenset()

    invalid_utf8 = tmp_path / "invalid-utf8.yml"
    invalid_utf8.write_bytes(b"services:\n  agent:\n    \xff\xfe\n")
    assert agent_environment_keys_from_compose_file(invalid_utf8) == frozenset()

    unsupported_env = tmp_path / "unsupported-env.yml"
    unsupported_env.write_text(
        yaml.safe_dump({"services": {"agent": {"environment": 123}}}),
        encoding="utf-8",
    )
    assert agent_environment_keys_from_compose_file(unsupported_env) == frozenset()


@pytest.mark.unit
def test_agent_environment_keys_from_compose_file_parses_list_environment(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "environment": [
                            "OLLAMA_HOST=http://ollama.profile:11434",
                            "WORKSPACE_ID=ws_123",
                            {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
                            "=skip-empty-key",
                            42,
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    keys = agent_environment_keys_from_compose_file(compose_file)

    assert keys == frozenset({"OLLAMA_HOST", "WORKSPACE_ID", "OPENAI_API_KEY"})


@pytest.mark.unit
def test_agent_exec_env_passthrough_honors_list_format_compose_environment(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "environment": [
                            "OLLAMA_HOST=http://ollama.profile:11434",
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert "OLLAMA_HOST" not in passthrough
    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in passthrough


@pytest.mark.unit
def test_profile_owned_auth_env_literals_suppressed_from_exec_passthrough(
    tmp_path: Path,
) -> None:
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "environment": {
                            "OPENAI_API_KEY": "sk-profile-owned",
                            "OPENAI_BASE_URL": "https://profile.proxy/v1",
                            "ANTHROPIC_BASE_URL": "https://anthropic.profile/v1",
                            "CODEX_API_KEY": "${CODEX_API_KEY}",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert "OPENAI_API_KEY" not in passthrough
    assert "OPENAI_BASE_URL" not in passthrough
    assert "ANTHROPIC_BASE_URL" not in passthrough
    assert "CODEX_API_KEY" not in passthrough


@pytest.mark.unit
def test_declared_env_lease_same_name_placeholder_suppressed_from_exec_passthrough(
    tmp_path: Path,
) -> None:
    """Env provider leases with target == source render ``${NAME}`` like legacy host auth.

    Exec-time ``-e NAME`` must not re-inject worker env for keys already declared in
    compose — Docker resolved them at stack launch and ``exec -e`` would override
    with a potentially stale worker value.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "environment": {
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert "OPENAI_API_KEY" not in passthrough


@pytest.mark.unit
def test_agent_exec_env_passthrough_omits_unconfigured_worker_auth_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent compose keys must not passthrough unless the worker env defines them."""

    _clear_host_auth(monkeypatch)
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump({"services": {"agent": {"image": "agent:latest"}}}),
        encoding="utf-8",
    )

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert "OPENAI_BASE_URL" not in passthrough
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in passthrough
    assert passthrough == ()


@pytest.mark.unit
def test_profile_base_url_still_allows_worker_ollama_host_exec_passthrough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_host_auth(monkeypatch)
    monkeypatch.setenv("OLLAMA_HOST", "http://ollama.worker:11434")
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "environment": {
                            "AWF_OPENCODE_OLLAMA_BASE_URL": "http://ollama.profile:11434/v1",
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in passthrough
    assert "OLLAMA_HOST" in passthrough


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_suppresses_profile_owned_auth_keys(
    tmp_path: Path,
) -> None:
    """Hosted passthrough names are filtered by the same profile-owned exclusions."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                            "CODEX_API_KEY": "sk-profile",
                            "ANTHROPIC_BASE_URL": "https://anthropic.profile/v1",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = (
        "OPENAI_API_KEY",
        "CODEX_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_API_KEY",
        # A backend-credential supplement (not in AGENT_AUTH_ENV_VARS) always
        # passes through — it is not a profile-owned slot.
        "AWS_REGION",
    )
    filtered = filter_hosted_env_passthrough_names(names, compose_file=compose_file)

    assert "OPENAI_API_KEY" not in filtered
    assert "CODEX_API_KEY" not in filtered
    assert "ANTHROPIC_BASE_URL" not in filtered
    assert "ANTHROPIC_API_KEY" in filtered
    assert "AWS_REGION" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_keeps_auth_pass_through_slot(
    tmp_path: Path,
) -> None:
    """An auth pass-through slot stays in hosted passthrough for out-of-band resolution.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PY6Rn: a Compose pass-through
    slot for an ``AGENT_AUTH_ENV_VARS`` key (``OPENAI_API_KEY:`` / ``: null`` /
    ``environment: [OPENAI_API_KEY]`` — no ``=``) declares no value; Docker
    Compose takes the value from the worker shell at stack launch, exactly like
    the non-auth pass-through slots handled in
    ``test_compose_passthrough_env_slot_not_carried_kept_in_passthrough``.
    ``_compose_env_passthrough_exclusions`` -> ``_profile_owned_auth_keys``
    treated any ``AGENT_AUTH_ENV_VARS`` key declared on the agent service as
    profile-owned regardless of its value, so the name was removed from the
    exclusion set before the pass-through exception in
    ``_filter_hosted_env_passthrough_names_from_compose_env`` could apply — the
    exception only prevents *adding* worker-resolved-defaulted names, it never
    *removes* a name already excluded by the first pass. The hosted job therefore
    missed a worker credential the local stack already injected at launch. An
    auth pass-through slot must stay in ``env_passthrough_names`` (resolved
    out-of-band), like any other pass-through slot. (An *explicit* empty auth
    value — ``ANTHROPIC_API_KEY: ""`` — is a separate case: Compose sets an
    empty literal that overrides the worker value, so it is carried in
    ``profile_env`` and excluded from passthrough; see
    ``test_compose_explicit_empty_value_carried_not_passthrough``.)
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Mapping null value -> pass-through slot.
                            "OPENAI_API_KEY": None,
                            # Explicit empty (mapping "") -> CARRIED literal
                            # "" that overrides the worker value; NOT a
                            # pass-through slot (compose-go: non-nil pointer).
                            "ANTHROPIC_API_KEY": "",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
    worker_env = {"OPENAI_API_KEY": "sk-secret", "ANTHROPIC_API_KEY": "sk-ant"}
    filtered = filter_hosted_env_passthrough_names(
        names, compose_file=compose_file, worker_env=worker_env
    )

    # Auth pass-through slot (null) stays in passthrough for hosted out-of-band
    # resolution (Docker Compose took it from the worker shell at launch).
    assert "OPENAI_API_KEY" in filtered
    # Explicit-empty auth value is EXCLUDED — it is a profile-owned literal
    # (carried as "" via profile_env), NOT a worker-resolved slot; the local
    # container received an explicit blank, so the hosted executor must not
    # re-resolve a worker value the local container never had.
    assert "ANTHROPIC_API_KEY" not in filtered
    # An auth name absent from the compose env block still passes through.
    assert "ANTHROPIC_BASE_URL" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_keeps_auth_worker_resolved_defaulted(
    tmp_path: Path,
) -> None:
    """A worker-resolved defaulted auth key stays in hosted passthrough.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PiGHK: a Compose agent env key in
    ``AGENT_AUTH_ENV_VARS`` whose value is a defaulted form with the variable
    worker-set (e.g. ``OPENAI_API_KEY: ${OPENAI_API_KEY:-sk-default}`` with
    ``OPENAI_API_KEY`` set in the worker env) classifies as
    ``WORKER_RESOLVED_DEFAULTED``. ``_compose_env_passthrough_exclusions`` ->
    ``_profile_owned_auth_keys`` treats any ``AGENT_AUTH_ENV_VARS`` key declared
    on the agent service as profile-owned regardless of its value, so the name
    sits in the baseline excluded set. Pass-through auth slots are removed from
    that set (PRRT_kwDOSJAM6s6PY6Rn), but worker-resolved defaulted auth names
    were not, so the worker-resolved-defaulted exception below only prevented
    *adding* a name — it never removed a name the first pass had already
    excluded. ``literal_profile_env_from_compose`` also skips
    ``WORKER_RESOLVED_DEFAULTED`` (carrying the worker value would embed a
    secret), so the hosted monitor launch dropped the credential the local
    Compose container received at stack launch, leaving the hosted job with
    neither the worker override nor the profile default. Such a name must stay
    in ``env_passthrough_names`` for hosted out-of-band resolution, mirroring the
    pass-through slot fix. (An explicit empty auth value ``OPENAI_API_KEY: ""``
    stays excluded — Compose sets a non-nil empty literal that overrides the
    worker value, carried via ``profile_env``.)
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # :- with the variable worker-set -> stays in
                            # passthrough (worker value resolved out-of-band).
                            "OPENAI_API_KEY": "${OPENAI_API_KEY:-sk-default}",
                            # Explicit empty -> EXCLUDED (carried literal "" via
                            # profile_env; overrides the worker value).
                            "ANTHROPIC_API_KEY": "",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
    worker_env = {"OPENAI_API_KEY": "sk-secret", "ANTHROPIC_API_KEY": "sk-ant"}
    filtered = filter_hosted_env_passthrough_names(
        names, compose_file=compose_file, worker_env=worker_env
    )

    # Worker-set defaulted auth key stays in passthrough for hosted out-of-band
    # resolution (Docker Compose injected the worker value at stack launch).
    assert "OPENAI_API_KEY" in filtered
    # Explicit-empty auth value is EXCLUDED — profile-owned literal carried via
    # profile_env, NOT a worker-resolved slot.
    assert "ANTHROPIC_API_KEY" not in filtered
    # An auth name absent from the compose env block still passes through.
    assert "ANTHROPIC_BASE_URL" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_keeps_auth_pass_through_slot_list_form(
    tmp_path: Path,
) -> None:
    """List-form auth pass-through slot stays in hosted passthrough.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PY6Rn: the list-form pass-through
    syntax ``environment: [OPENAI_API_KEY]`` is an ``AGENT_AUTH_ENV_VARS`` key
    declared with no value; Docker Compose takes it from the worker shell at stack
    launch. It must stay in ``env_passthrough_names`` like any other pass-through
    slot, not be treated as profile-owned merely because the name is in
    ``AGENT_AUTH_ENV_VARS``.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": ["OPENAI_API_KEY", "CODEX_API_KEY=sk-profile"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = ("OPENAI_API_KEY", "CODEX_API_KEY")
    worker_env = {"OPENAI_API_KEY": "sk-secret"}
    filtered = filter_hosted_env_passthrough_names(
        names, compose_file=compose_file, worker_env=worker_env
    )

    # List-form pass-through slot -> stays in passthrough (worker-resolved).
    assert "OPENAI_API_KEY" in filtered
    # List-form literal value -> profile-owned, excluded (carried via profile_env).
    assert "CODEX_API_KEY" not in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_suppresses_shadowing_worker_ollama_key(
    tmp_path: Path,
) -> None:
    """Higher-precedence worker Ollama base URL key is suppressed when profile owns the lower one."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = ("AWF_OPENCODE_OLLAMA_BASE_URL", "OLLAMA_HOST", "OLLAMA_API_KEY")
    filtered = filter_hosted_env_passthrough_names(names, compose_file=compose_file)

    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in filtered
    assert "OLLAMA_HOST" not in filtered
    assert "OLLAMA_API_KEY" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_fails_closed_on_unreadable_compose(
    tmp_path: Path,
) -> None:
    """Unreadable compose fails closed: suppress the higher-precedence Ollama base URL key."""
    missing = tmp_path / "missing.yml"
    assert not missing.exists()

    names = ("AWF_OPENCODE_OLLAMA_BASE_URL", "OLLAMA_HOST", "OPENAI_API_KEY")
    filtered = filter_hosted_env_passthrough_names(names, compose_file=missing)

    assert "AWF_OPENCODE_OLLAMA_BASE_URL" not in filtered
    assert "OLLAMA_HOST" in filtered
    assert "OPENAI_API_KEY" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_suppresses_profile_owned_backend_supplement(
    tmp_path: Path,
) -> None:
    """A backend-credential supplement declared in compose is profile-owned on the hosted path too.

    The local ``docker compose exec`` path only forwards ``AGENT_AUTH_ENV_VARS``,
    so any env key declared on the agent service's environment block — including
    adapter backend-credential supplements that are NOT in ``AGENT_AUTH_ENV_VARS``
    (e.g. Claude Code ``AWS_*`` / Vertex project / region) — is profile-owned at
    stack launch and never re-injected from the worker. The hosted path must
    apply the same broader exclusion or a profile-owned backend credential/
    endpoint declared in the compose env block would be re-resolved from the
    worker by the hosted executor, diverging from the local run.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Backend-credential supplements are NOT in
                            # AGENT_AUTH_ENV_VARS; the toggle is, the credentials
                            # are not. Declaring them in the compose env block
                            # makes them profile-owned at stack launch.
                            "AWS_REGION": "us-west-2",
                            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-123",
                            "CLAUDE_CODE_USE_VERTEX": "1",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = (
        # AGENT_AUTH_ENV_VARS-territory profile-owned: excluded.
        "CLAUDE_CODE_USE_VERTEX",
        # Backend supplements declared in compose: must also be excluded so the
        # hosted executor does not re-resolve worker-side values for them.
        "AWS_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        # A backend supplement absent from the compose env block still passes
        # through — the hosted executor resolves only names with backing values.
        "AWS_ACCESS_KEY_ID",
        # An AGENT_AUTH name absent from compose still passes through.
        "ANTHROPIC_API_KEY",
    )
    filtered = filter_hosted_env_passthrough_names(names, compose_file=compose_file)

    assert "CLAUDE_CODE_USE_VERTEX" not in filtered
    assert "AWS_REGION" not in filtered
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in filtered
    assert "AWS_ACCESS_KEY_ID" in filtered
    assert "ANTHROPIC_API_KEY" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_parses_compose_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The credential-suppression path reads/parses the compose file exactly once.

    Regression for the TOCTOU double-parse in
    ``filter_hosted_env_passthrough_names``: the exclusion set and the
    compose-env union must derive from a single read of the file so a change
    between two reads cannot make them disagree.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                            "AWS_REGION": "us-west-2",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    import awf.profiles.compose as compose_module

    real_parse = compose_module._try_agent_environment_from_compose_file
    parse_calls = 0

    def _counting_parse(path: Path) -> dict[str, str] | None:
        nonlocal parse_calls
        parse_calls += 1
        return real_parse(path)

    monkeypatch.setattr(compose_module, "_try_agent_environment_from_compose_file", _counting_parse)

    names = ("OPENAI_API_KEY", "AWS_REGION", "ANTHROPIC_API_KEY")
    filtered = filter_hosted_env_passthrough_names(names, compose_file=compose_file)

    assert parse_calls == 1
    assert "OPENAI_API_KEY" not in filtered
    assert "AWS_REGION" not in filtered
    assert "ANTHROPIC_API_KEY" in filtered


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_keeps_worker_resolved_defaulted(
    tmp_path: Path,
) -> None:
    """A defaulted form whose variable is worker-set stays in hosted passthrough.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PVH0t: when a profile env value
    uses a Compose default/override such as ``AWS_REGION: ${AWS_REGION:-us-west-2}``
    and the worker env has ``AWS_REGION`` set, Docker Compose interpolates the
    worker value into the local agent container at stack launch. The hosted path
    must not drop that name entirely: ``literal_profile_env_from_compose`` skips
    it (worker-resolved; carrying the worker value would embed a secret), so if
    ``filter_hosted_env_passthrough_names`` also excluded it the hosted job would
    receive neither the worker override nor the profile default — diverging from
    the local Compose container. The name must stay in ``env_passthrough_names``
    so the hosted executor resolves the same worker value out-of-band.

    Pure literals and bare ``${NAME}`` / ``${NAME:?...}`` slots stay excluded
    (carried via ``profile_env`` or suppressed as profile-owned secret slots).
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # :- with the variable worker-set -> stays in
                            # passthrough (worker value resolved out-of-band).
                            "AWS_REGION": "${AWS_REGION:-us-west-2}",
                            # - with the variable worker-set -> stays in
                            # passthrough (worker value resolved out-of-band).
                            "AWS_DEFAULT_REGION": "${AWS_DEFAULT_REGION-us-west-2}",
                            # Pure literal -> excluded (carried via profile_env).
                            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-123",
                            # Bare ${NAME} -> excluded (profile-owned secret slot).
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                            # :- with the variable unset -> excluded (concrete
                            # default carried via profile_env).
                            "UNSET_REGION": "${UNSET_REGION:-us-east-1}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = (
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "OPENAI_API_KEY",
        "UNSET_REGION",
        "ANTHROPIC_API_KEY",
    )
    worker_env = {"AWS_REGION": "eu-central-1", "AWS_DEFAULT_REGION": "eu-central-1"}
    filtered = filter_hosted_env_passthrough_names(
        names, compose_file=compose_file, worker_env=worker_env
    )

    # Worker-set defaulted forms stay available for hosted out-of-band resolution.
    assert "AWS_REGION" in filtered
    assert "AWS_DEFAULT_REGION" in filtered
    # Pure literal -> excluded (carried via profile_env instead).
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in filtered
    # Bare ${NAME} -> excluded (profile-owned secret slot).
    assert "OPENAI_API_KEY" not in filtered
    # :- with variable unset -> excluded (concrete default carried via
    # profile_env).
    assert "UNSET_REGION" not in filtered
    # A name absent from the compose env block still passes through.
    assert "ANTHROPIC_API_KEY" in filtered


@pytest.mark.unit
def test_compose_passthrough_env_slot_not_carried_kept_in_passthrough(
    tmp_path: Path,
) -> None:
    """A Compose pass-through env slot is resolved from the worker, not carried.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PYnJJ: a Compose pass-through
    slot — ``environment: [NAME]`` (list item with no ``=``) or ``NAME:`` /
    ``NAME: null`` (mapping value that is ``None``) — declares no value; Docker
    Compose takes the value from the worker shell at stack launch, exactly like
    a bare ``${NAME}`` reference. The local agent container receives the
    worker's ``AWS_REGION`` when it is set. (An *explicit* empty value —
    mapping ``NAME: ""`` or list ``NAME=`` — is a separate case covered by
    ``test_compose_explicit_empty_value_carried_not_passthrough``: Compose sets
    an empty literal that overrides the worker value, so it is carried in
    ``profile_env`` and excluded from passthrough.)

    Previously ``_compose_environment_mapping`` normalized a pass-through slot
    to ``""`` (or ``"None"`` for a YAML null value) and
    ``literal_profile_env_from_compose`` carried it as a literal empty/
    ``"None"`` value into ``profile_env``, which overrode the real worker region
    in the hosted request, while ``filter_hosted_env_passthrough_names`` excluded
    the name from passthrough (it resolved to ``LITERAL``), so the hosted job
    received neither the worker value nor the passthrough slot.

    Now a pass-through slot is skipped from ``profile_env`` (an empty literal
    would clobber the real worker value) and kept in ``env_passthrough_names``
    for hosted out-of-band resolution, mirroring the local Compose container. A
    literal empty resolved from an interpolation default (e.g. ``${MISSING:-}``
    with ``MISSING`` unset) is still carried as ``LITERAL`` (the local container
    received that empty default, not a worker shell value).
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Pass-through slots (mapping form): no value / null.
                            "AWS_REGION": None,
                            "AWS_NULL_REGION": None,
                            # Explicit empty (mapping "") -> CARRIED literal ""
                            # (overrides the worker value; NOT a pass-through).
                            "AWS_EMPTY_REGION": "",
                            # Pure literal -> carried via profile_env.
                            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-123",
                            # Bare ${NAME} -> profile-owned secret slot, skipped.
                            "OPENAI_API_KEY": "${OPENAI_API_KEY}",
                            # Empty interpolation default (unset var) -> carried
                            # as a literal empty string (the local container
                            # received the empty default, not a worker value).
                            "EMPTY_DEFAULT": "${MISSING:-}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    worker_env = {"AWS_REGION": "us-west-2", "OPENAI_API_KEY": "sk-secret"}
    profile_env = literal_profile_env_from_compose(compose_file, worker_env=worker_env)
    carried = dict(profile_env)

    # Pass-through slots are NOT carried as empty/None literals (that would
    # override the real worker value in the hosted request).
    assert "AWS_REGION" not in carried
    assert "AWS_NULL_REGION" not in carried
    # Explicit empty (mapping "") IS carried as a literal "" — the local
    # container received an explicit empty value that overrides the worker
    # shell value, so the hosted job must mirror that blank (NOT inherit a
    # worker value the local container never had).
    assert carried.get("AWS_EMPTY_REGION") == ""
    # The string "None" must never appear as a carried value (regression for the
    # YAML null normalization defect).
    assert "None" not in carried.values()
    # Pure literal is carried.
    assert carried.get("ANTHROPIC_VERTEX_PROJECT_ID") == "proj-123"
    # Bare ${NAME} secret slot is skipped.
    assert "OPENAI_API_KEY" not in carried
    # Empty interpolation default is carried as a literal "".
    assert carried.get("EMPTY_DEFAULT") == ""

    # Pass-through slots stay in env_passthrough_names for hosted out-of-band
    # resolution; literals and bare ${NAME} secret slots are excluded.
    names = (
        "AWS_REGION",
        "AWS_NULL_REGION",
        "AWS_EMPTY_REGION",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "OPENAI_API_KEY",
        "EMPTY_DEFAULT",
        "ANTHROPIC_API_KEY",
    )
    filtered = filter_hosted_env_passthrough_names(
        names, compose_file=compose_file, worker_env=worker_env
    )
    # Pass-through slots stay available for hosted out-of-band resolution.
    assert "AWS_REGION" in filtered
    assert "AWS_NULL_REGION" in filtered
    # Explicit empty (mapping "") is EXCLUDED — it is a profile-owned literal
    # carried via profile_env, not a worker-resolved slot.
    assert "AWS_EMPTY_REGION" not in filtered
    # Pure literal -> excluded (carried via profile_env).
    assert "ANTHROPIC_VERTEX_PROJECT_ID" not in filtered
    # Bare ${NAME} -> excluded (profile-owned secret slot).
    assert "OPENAI_API_KEY" not in filtered
    # Empty interpolation default -> excluded (carried as literal "").
    assert "EMPTY_DEFAULT" not in filtered
    # A name absent from the compose env block still passes through.
    assert "ANTHROPIC_API_KEY" in filtered
