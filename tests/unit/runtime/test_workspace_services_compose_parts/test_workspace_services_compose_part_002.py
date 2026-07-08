"""No-Docker compose coverage for profile-declared workspace services (part 2).

Shared fixtures/helpers live in
``test_workspace_services_compose_part_001``; this part imports them. Split
from the original monolithic module to stay under the first-party file line
limit (see ``test_core_decomposition_maintainability``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from awf.node.compose_manager import ComposeManager, WorkspaceComposeSpec
from awf.node.git_manager import WorktreeLayout
from awf.node.stack_launcher import ComposeStackLauncher, WorkspaceStackLaunchRequest
from awf.profiles.compose import (
    agent_environment_with_legacy_host_auth,
    agent_exec_env_passthrough,
    filter_hosted_env_passthrough_names,
)
from tests.unit.runtime.test_workspace_services_compose_parts import (
    test_workspace_services_compose_part_001 as _part_001,
)

_FIXTURE = _part_001._FIXTURE
_POSTGRES_FIXTURE = _part_001._POSTGRES_FIXTURE
_NODE_BROWSER_FIXTURE = _part_001._NODE_BROWSER_FIXTURE
_TEMPLATE = _part_001._TEMPLATE
_RecordingCompose = _part_001._RecordingCompose
_load_profile = _part_001._load_profile
_load_postgres_profile = _part_001._load_postgres_profile
_load_node_browser_profile = _part_001._load_node_browser_profile
_clear_host_auth = _part_001._clear_host_auth


@pytest.mark.unit
def test_filter_hosted_env_passthrough_names_keeps_required_set_worker_value(
    tmp_path: Path,
) -> None:
    """``:?`` / ``?`` with the variable set keep the name in hosted passthrough.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PVhhm: for an agent env value
    such as ``API_KEY: ${API_KEY:?set}``, Docker Compose resolves the worker value
    of ``API_KEY`` into the local agent container at stack launch when ``API_KEY``
    is set (non-empty for ``:?``, set for ``?``). The previous code classified every
    ``:?`` / ``?`` form as ``WORKER_RESOLVED_SLOT``, dropping the key from
    ``profile_env`` (correct — carrying the worker value would embed a secret) AND
    excluding it from ``env_passthrough_names`` — so the hosted job received
    neither the worker value nor any profile default, while the local container
    received the worker value. Such a name is classified
    ``WORKER_RESOLVED_DEFAULTED`` so it stays in ``env_passthrough_names`` for
    hosted out-of-band resolution, mirroring the local Compose container. When the
    variable is unset the local stack would fail to launch (``:?`` / ``?`` raise),
    so that branch is unreachable for a running container and stays classified as a
    worker-resolved slot.
    """
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # :? with API_KEY set & non-empty -> worker value
                            # resolved out-of-band on the hosted path.
                            "API_KEY": "${API_KEY:?set}",
                            # ? with API_KEY_Q set (even empty) -> worker value
                            # resolved out-of-band on the hosted path.
                            "API_KEY_Q": "${API_KEY_Q?set}",
                            # Pure literal -> excluded (carried via profile_env).
                            "LITERAL_CONFIG": "static-value",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = ("API_KEY", "API_KEY_Q", "LITERAL_CONFIG", "OPENAI_API_KEY")
    worker_env = {"API_KEY": "sk-worker", "API_KEY_Q": ""}
    filtered = filter_hosted_env_passthrough_names(
        names, compose_file=compose_file, worker_env=worker_env
    )

    # :? / ? with variable set -> stays in passthrough for hosted out-of-band
    # resolution (the local container received the worker value at stack launch).
    assert "API_KEY" in filtered
    assert "API_KEY_Q" in filtered
    # Pure literal -> excluded (carried via profile_env instead).
    assert "LITERAL_CONFIG" not in filtered
    # A name absent from the compose env block still passes through.
    assert "OPENAI_API_KEY" in filtered


@pytest.mark.unit
def test_hosted_github_token_passthrough_names_surfaces_worker_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hosted path surfaces the GitHub token alias names the local path injects.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PXFPz: when the worker env
    carries a GitHub token source, the local Compose path injects
    ``GH_TOKEN: ${AWF_GITHUB_TOKEN}`` and ``GITHUB_TOKEN: ${AWF_GITHUB_TOKEN}``
    into the agent env block so the local agent container can run ``gh``. The
    hosted (non-compose) path has no compose env block substitution, so
    without surfacing these alias names the hosted executor cannot resolve the
    credential and the hosted monitor-repair agent loses GitHub CLI access.
    The helper returns the alias NAMES only (never the placeholder value),
    applying the same group-precedence rule as
    ``agent_environment_with_github_token``.
    """
    from awf.profiles.compose import hosted_github_token_passthrough_names

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GH_TOKEN": "${AWF_GITHUB_TOKEN}",
                            "GITHUB_TOKEN": "${AWF_GITHUB_TOKEN}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    names = hosted_github_token_passthrough_names(compose_file)

    # Both aliases the local Compose path would inject are surfaced so the
    # hosted executor can resolve them out-of-band. Names only — no value.
    assert "GH_TOKEN" in names
    assert "GITHUB_TOKEN" in names
    assert all(not name.startswith("ghp_") for name in names)


@pytest.mark.unit
def test_hosted_github_token_passthrough_names_skips_profile_owned_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile-owned GitHub token alias is not shadowed by a worker alias.

    Mirrors the local Compose path's group-precedence rule
    (``agent_environment_with_github_token``): when the profile owns
    ``GITHUB_TOKEN`` (e.g. via a secret lease rendering
    ``GITHUB_TOKEN: ${MY_LEASE_TOKEN}``), the local path does NOT inject the
    worker ``GH_TOKEN`` (higher precedence) or ``gh`` would use the worker
    credential instead of the profile-owned token. The hosted path must apply
    the same rule: only the worker alias that does not shadow a profile-owned
    lower-precedence alias is surfaced.
    """
    from awf.profiles.compose import hosted_github_token_passthrough_names

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Profile owns the lower-precedence alias.
                            "GITHUB_TOKEN": "${MY_PROFILE_LEASE_TOKEN}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")

    names = hosted_github_token_passthrough_names(compose_file)

    # The higher-precedence worker GH_TOKEN is suppressed so the profile-owned
    # GITHUB_TOKEN wins. The lower-precedence GITHUB_TOKEN is profile-owned so
    # it is not surfaced either (the hosted executor does not re-resolve
    # profile-owned slots). Nothing is surfaced.
    assert names == ()


@pytest.mark.unit
def test_hosted_github_token_passthrough_names_empty_without_worker_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No worker GitHub token source -> no aliases surfaced.

    Mirrors the local Compose path, which injects nothing when no GitHub
    token source (``AWF_GITHUB_TOKEN`` / ``GH_TOKEN`` / ``GITHUB_TOKEN``) is
    present in the worker env.
    """
    from awf.profiles.compose import hosted_github_token_passthrough_names

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "GH_TOKEN": "${AWF_GITHUB_TOKEN}",
                            "GITHUB_TOKEN": "${AWF_GITHUB_TOKEN}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("AWF_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert hosted_github_token_passthrough_names(compose_file) == ()


@pytest.mark.unit
def test_hosted_github_token_passthrough_names_unreadable_compose_is_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable compose yields no aliases (fail-closed).

    Mirrors ``literal_profile_env_from_compose`` / the hosted filter's
    fail-closed behaviour: when the compose file cannot be parsed, assume the
    profile owns both aliases rather than surface a worker alias that could
    shadow a profile-owned token the unreadable parse could not see.
    """
    from awf.profiles.compose import hosted_github_token_passthrough_names

    missing = tmp_path / "missing.yml"
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")

    assert hosted_github_token_passthrough_names(missing) == ()


@pytest.mark.unit
def test_literal_profile_env_from_compose_unreadable_is_empty(
    tmp_path: Path,
) -> None:
    """An unreadable compose yields no profile env (fail-closed, no values)."""
    from awf.profiles.compose import literal_profile_env_from_compose

    missing = tmp_path / "missing.yml"
    assert not missing.exists()

    assert literal_profile_env_from_compose(missing) == ()


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_postgres_password_bearing_values(
    tmp_path: Path,
) -> None:
    """A rendered compose bakes the generated postgres password into agent env
    values (e.g. ``DATABASE_URL``/``AWF_DATABASE_URL``) so the local container
    can connect. ``literal_profile_env_from_compose`` must NOT carry those
    expanded-secret-bearing values to the hosted executor: the runtime seam's
    ``profile_env`` is a secret-free contract (see
    ``AgentRuntimeExecRequest``). The hosted path resolves DB credentials via
    its own adapter contract, not from ``profile_env``.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PUp80: ComposeManager expands
    ``${AWF_POSTGRES_PASSWORD}`` into the agent environment before writing the
    rendered compose file, and ``literal_profile_env_from_compose`` would carry
    the resulting literal verbatim, handing the workspace DB password to a
    hosted executor / request object.
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "workspace-secret",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Non-secret profile literal -> carried (must survive).
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # AWF-expanded DB URLs embed the generated postgres
                            # password -> must be skipped (secret-bearing).
                            "DATABASE_URL": ("postgresql://awf:workspace-secret@postgres:5432/awf"),
                            "AWF_DATABASE_URL": (
                                "postgresql+asyncpg://awf:workspace-secret@postgres:5432/awf"
                            ),
                            "AWF_TEST_DATABASE_URL": (
                                "postgresql+asyncpg://awf:workspace-secret@postgres:5432/awf"
                            ),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    # Non-secret profile literal is still carried to the hosted executor.
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    # Secret-bearing expanded values are NOT carried to the hosted executor.
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "AWF_DATABASE_URL" not in carried
    assert "AWF_TEST_DATABASE_URL" not in carried
    # The workspace DB password never reaches the hosted request object.
    assert "workspace-secret" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_carries_values_without_postgres_service(
    tmp_path: Path,
) -> None:
    """When no postgres service declares a password, nothing is redacted.

    A profile without a postgres sidecar (e.g. a pure-Ollama profile) has no
    generated DB password to redact; its agent env literals are carried as
    before. This guards against over-redacting when the postgres password is
    absent.
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
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # A literal that merely contains a colon-separated
                            # token must NOT be mistaken for a secret when no
                            # postgres password is declared to redact.
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    assert ("APP_BASE_URL", "http://app:8080") in profile_env


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_postgres_password_under_nonstandard_service_name(
    tmp_path: Path,
) -> None:
    """A custom profile may name its database service ``db`` / ``database``
    (or anything else) while still setting ``POSTGRES_PASSWORD`` and expanding
    that same password into the agent env ``DATABASE_URL`` /
    ``AWF_DATABASE_URL``. The redaction source must collect
    ``POSTGRES_PASSWORD`` from every compose service, not only from a service
    literally named ``postgres``; otherwise ``file_postgres_password`` stays
    ``None`` for a valid custom profile and ``literal_profile_env_from_compose``
    carries the rendered DB URL in ``AgentRuntimeExecRequest.profile_env``,
    leaking the workspace credential to the hosted executor despite the
    secret-free contract.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PWUIl.
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    # A custom-profile DB sidecar named ``db`` (not ``postgres``)
                    # still declares ``POSTGRES_PASSWORD`` and shares it with
                    # the agent env DB URLs below.
                    "db": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "workspace-secret",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Non-secret profile literal -> carried (must survive).
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # AWF-expanded DB URLs embed the shared postgres
                            # password -> must be skipped (secret-bearing).
                            "DATABASE_URL": ("postgresql://awf:workspace-secret@db:5432/awf"),
                            "AWF_DATABASE_URL": (
                                "postgresql+asyncpg://awf:workspace-secret@db:5432/awf"
                            ),
                            "AWF_TEST_DATABASE_URL": (
                                "postgresql+asyncpg://awf:workspace-secret@db:5432/awf"
                            ),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    # Non-secret profile literal is still carried to the hosted executor.
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    # Secret-bearing expanded values are NOT carried to the hosted executor.
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "AWF_DATABASE_URL" not in carried
    assert "AWF_TEST_DATABASE_URL" not in carried
    # The workspace DB password never reaches the hosted request object.
    assert "workspace-secret" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_all_distinct_postgres_passwords(
    tmp_path: Path,
) -> None:
    """A profile may run several DB sidecars each declaring a *different*
    ``POSTGRES_PASSWORD``. The redaction source must collect every declared
    value, not only the first; otherwise a rendered agent env value (e.g. a
    second service's ``WAREHOUSE_URL``) embedding a later service's password
    slips past a redaction that only compares against the first service's
    password, leaking the workspace credential to the hosted executor.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PWsKk.
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "workspace-secret",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "warehouse": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            "POSTGRES_PASSWORD": "warehouse-secret",
                            "POSTGRES_DB": "warehouse",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Non-secret profile literal -> carried (must survive).
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # First DB URL embeds the first service's password.
                            "DATABASE_URL": ("postgresql://awf:workspace-secret@postgres:5432/awf"),
                            # Second DB URL embeds the *other* service's password
                            # -> must also be skipped (not only the first one).
                            "WAREHOUSE_URL": (
                                "postgresql://awf:warehouse-secret@warehouse:5432/warehouse"
                            ),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})

    # Non-secret profile literal is still carried to the hosted executor.
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    # Secret-bearing expanded values are NOT carried, regardless of which
    # service's password they embed.
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    assert "WAREHOUSE_URL" not in carried
    # Neither workspace DB password reaches the hosted request object.
    assert "workspace-secret" not in "".join(v for _k, v in profile_env)
    assert "warehouse-secret" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_redacts_postgres_password_interpolation_default(
    tmp_path: Path,
) -> None:
    """A service may express ``POSTGRES_PASSWORD`` via Compose
    interpolation/defaults (e.g. ``${POSTGRES_PASSWORD:-fallback}``), which
    ComposeManager does not expand (it only expands bare
    ``${AWF_POSTGRES_PASSWORD}``). The rendered service env therefore retains the
    ``${...}`` form, and Docker Compose resolves it against the worker env at
    stack launch. The redaction source must resolve each declared password
    against the worker env (mirroring Compose) so a rendered agent env DB URL
    that embeds the *resolved* worker value is redacted; comparing only against
    the raw ``${...}`` placeholder string would miss the expanded secret.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PWsKk.
    """
    from awf.profiles.compose import literal_profile_env_from_compose

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            # Interpolation with a default: Compose resolves this
                            # against the worker env at stack launch.
                            "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:-fallback-pw}",
                            "POSTGRES_DB": "awf",
                        },
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Non-secret profile literal -> carried (must survive).
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            # A rendered DB URL embedding the *resolved* worker
                            # value -> must be skipped (secret-bearing).
                            "DATABASE_URL": ("postgresql://awf:resolved-pw@postgres:5432/awf"),
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    # Worker env supplies the resolved password that Compose would inject.
    profile_env = literal_profile_env_from_compose(
        compose_file, worker_env={"POSTGRES_PASSWORD": "resolved-pw"}
    )

    # Non-secret profile literal is still carried to the hosted executor.
    assert ("OLLAMA_HOST", "http://ollama.profile:11434") in profile_env
    # The resolved-password-bearing DB URL is NOT carried.
    carried = dict(profile_env)
    assert "DATABASE_URL" not in carried
    # The resolved worker password never reaches the hosted request object.
    assert "resolved-pw" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_agent_exec_env_passthrough_parses_compose_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exec-time passthrough reads/parses the compose file exactly once."""
    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {"OPENAI_API_KEY": "${OPENAI_API_KEY}"},
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
    monkeypatch.setenv("OPENAI_API_KEY", "sk-worker")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-worker")

    passthrough = agent_exec_env_passthrough(compose_file=compose_file)

    assert parse_calls == 1
    assert "OPENAI_API_KEY" not in passthrough
    assert "ANTHROPIC_API_KEY" in passthrough


@pytest.mark.unit
def test_opencode_bash_timeout_env_reaches_agent_as_placeholder() -> None:
    env = agent_environment_with_legacy_host_auth(
        (),
        host_env={"OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS": "600000"},
    )

    assert env == (
        (
            "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
            "${OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS}",
        ),
    )


async def _launched_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceComposeSpec:
    _clear_host_auth(monkeypatch)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="awf-agent-runtime:test",
    )
    layout = WorktreeLayout(
        mirror_path=tmp_path / "mirror.git",
        worktree_path=_FIXTURE,
        branch_name="awf/ws-services",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_services",
            layout=layout,
            profile=_load_profile(),
        )
    )

    assert compose.waits == [True]
    return compose.specs[0]


async def _launched_postgres_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceComposeSpec:
    _clear_host_auth(monkeypatch)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="python:3.12-alpine",
    )
    layout = WorktreeLayout(
        mirror_path=tmp_path / "mirror.git",
        worktree_path=_POSTGRES_FIXTURE,
        branch_name="awf/ws-python-postgres",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_python_pg",
            layout=layout,
            profile=_load_postgres_profile(),
        )
    )

    assert compose.waits == [True]
    return compose.specs[0]


async def _launched_node_browser_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> WorkspaceComposeSpec:
    _clear_host_auth(monkeypatch)
    compose = _RecordingCompose()
    launcher = ComposeStackLauncher(
        compose=compose,  # type: ignore[arg-type]
        agent_runtime_image="node:22-bookworm-slim",
    )
    layout = WorktreeLayout(
        mirror_path=tmp_path / "mirror.git",
        worktree_path=_NODE_BROWSER_FIXTURE,
        branch_name="awf/ws-node-browser",
    )

    await launcher.launch(
        WorkspaceStackLaunchRequest(
            workspace_id="ws_node_browser",
            layout=layout,
            profile=_load_node_browser_profile(),
        )
    )

    assert compose.waits == [True]
    return compose.specs[0]


@pytest.mark.unit
async def test_stack_launcher_builds_profile_service_spec_from_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_spec(tmp_path, monkeypatch)

    assert spec.workspace_id == "ws_services"
    assert spec.worktree_host_path == _FIXTURE
    assert spec.agent_runtime_image == "awf-agent-runtime:test"
    assert spec.docker_mode == "none"
    assert dict(spec.agent_environment) == {
        "APP_BASE_URL": "http://app:8080",
        "CACHE_URL": "redis://redis:6379/0",
    }
    assert spec.git_name == "AWF Agent"
    assert spec.git_email == "awf@example.com"
    assert spec.auth_mounts[0].source == str(tmp_path / "mirror.git")
    assert spec.auth_mounts[0].mode == "rw"

    services = {service.name: service for service in spec.services}
    assert set(services) == {"app", "redis"}
    assert services["app"].build_context == str(_FIXTURE.resolve())
    assert services["app"].env_file == str((_FIXTURE / "app.env").resolve())
    assert services["app"].depends_on == ("redis",)
    assert services["redis"].image == "redis:7-alpine"


@pytest.mark.unit
async def test_rendered_workspace_services_compose_expresses_sidecar_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_spec(tmp_path, monkeypatch)
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)

    parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

    assert set(parsed["services"]) == {"agent", "app", "redis"}
    assert "docker" not in parsed["services"]

    app = parsed["services"]["app"]
    assert app["build"] == {
        "context": str(_FIXTURE.resolve()),
        "dockerfile": "Dockerfile",
    }
    assert app["env_file"] == [str((_FIXTURE / "app.env").resolve())]
    assert app["environment"] == {
        "CACHE_URL": "redis://redis:6379/0",
        "PORT": "8080",
    }
    assert app["depends_on"] == {"redis": {"condition": "service_healthy"}}
    assert app["healthcheck"]["test"] == [
        "CMD-SHELL",
        "wget -qO- http://127.0.0.1:8080/healthz >/dev/null",
    ]
    assert app["ports"] == ["18080:8080"]
    assert app["networks"] == ["awf_net"]

    redis = parsed["services"]["redis"]
    assert redis["image"] == "redis:7-alpine"
    assert redis["environment"] == {"REDIS_PORT": "6379"}
    assert redis["healthcheck"]["test"] == ["CMD-SHELL", "redis-cli ping"]
    assert redis["ports"] == ["16379:6379"]
    assert redis["networks"] == ["awf_net"]

    agent = parsed["services"]["agent"]
    assert agent["environment"]["APP_BASE_URL"] == "http://app:8080"
    assert agent["environment"]["CACHE_URL"] == "redis://redis:6379/0"
    assert agent["depends_on"] == {
        "app": {"condition": "service_healthy"},
        "redis": {"condition": "service_healthy"},
    }
    assert parsed["networks"]["awf_net"]["name"] == "awf-ws_services-net"


@pytest.mark.unit
async def test_stack_launcher_builds_python_postgres_profile_service_spec_from_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_postgres_spec(tmp_path, monkeypatch)

    assert spec.workspace_id == "ws_python_pg"
    assert spec.worktree_host_path == _POSTGRES_FIXTURE
    assert spec.agent_runtime_image == "python:3.12-alpine"
    assert spec.docker_mode == "none"
    assert dict(spec.agent_environment) == {
        "APP_BASE_URL": "http://app:8080",
        "DATABASE_URL": "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf",
    }

    services = {service.name: service for service in spec.services}
    assert set(services) == {"app", "postgres"}
    assert services["app"].build_context == str(_POSTGRES_FIXTURE.resolve())
    assert services["app"].depends_on == ("postgres",)
    assert services["app"].environment == (
        ("DATABASE_URL", "postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf"),
        ("PORT", "8080"),
    )
    assert services["postgres"].image == "postgres:16-alpine"
    assert services["postgres"].volumes == (("postgres_data", "/var/lib/postgresql/data"),)


@pytest.mark.unit
async def test_rendered_python_postgres_compose_expresses_db_backed_service_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_postgres_spec(tmp_path, monkeypatch)
    spec = WorkspaceComposeSpec(
        workspace_id=spec.workspace_id,
        worktree_host_path=spec.worktree_host_path,
        agent_runtime_image=spec.agent_runtime_image,
        agent_environment=spec.agent_environment,
        docker_mode=spec.docker_mode,
        postgres_password="deterministic-postgres-password",
        auth_mounts=spec.auth_mounts,
        git_name=spec.git_name,
        git_email=spec.git_email,
        services=spec.services,
    )
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)

    parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

    assert set(parsed["services"]) == {"agent", "app", "postgres"}
    assert "docker" not in parsed["services"]

    app = parsed["services"]["app"]
    assert app["build"] == {
        "context": str(_POSTGRES_FIXTURE.resolve()),
        "dockerfile": "Dockerfile",
    }
    assert app["environment"] == {
        "DATABASE_URL": "postgresql://awf:deterministic-postgres-password@postgres:5432/awf",
        "PORT": "8080",
    }
    assert app["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    assert app["healthcheck"]["test"] == [
        "CMD-SHELL",
        (
            'python -c "import urllib.request; '
            "urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read()\""
        ),
    ]
    assert app["networks"] == ["awf_net"]
    assert "ports" not in app

    postgres = parsed["services"]["postgres"]
    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["environment"] == {
        "POSTGRES_DB": "awf",
        "POSTGRES_HOST_AUTH_METHOD": "trust",
        "POSTGRES_PASSWORD": "deterministic-postgres-password",
        "POSTGRES_USER": "awf",
    }
    assert postgres["healthcheck"]["test"] == ["CMD-SHELL", "pg_isready -U awf -d awf"]
    assert postgres["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
    assert postgres["networks"] == ["awf_net"]
    assert "ports" not in postgres

    agent = parsed["services"]["agent"]
    assert agent["environment"]["APP_BASE_URL"] == "http://app:8080"
    assert (
        agent["environment"]["DATABASE_URL"]
        == "postgresql://awf:deterministic-postgres-password@postgres:5432/awf"
    )
    assert agent["depends_on"] == {
        "app": {"condition": "service_healthy"},
        "postgres": {"condition": "service_healthy"},
    }
    assert parsed["volumes"]["postgres_data"]["name"] == "awf-ws_python_pg-postgres_data"
    assert parsed["networks"]["awf_net"]["name"] == "awf-ws_python_pg-net"


@pytest.mark.unit
async def test_stack_launcher_builds_node_next_browser_profile_service_spec_from_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_node_browser_spec(tmp_path, monkeypatch)

    assert spec.workspace_id == "ws_node_browser"
    assert spec.worktree_host_path == _NODE_BROWSER_FIXTURE
    assert spec.agent_runtime_image == "node:22-bookworm-slim"
    assert spec.docker_mode == "none"
    agent_environment = dict(spec.agent_environment)
    assert agent_environment["APP_BASE_URL"] == "http://app:3000"
    assert agent_environment["BROWSER_VALIDATE_URL"] == "http://browser:9323/validate"
    assert agent_environment["AWF_APP_ENDPOINT_APP_URL"] == "http://app:3000/"
    assert agent_environment["AWF_APP_ENDPOINT_BROWSER_VALIDATION_URL"] == (
        "http://browser:9323/validate"
    )

    services = {service.name: service for service in spec.services}
    assert set(services) == {"app", "browser"}
    assert services["app"].build_context == str(_NODE_BROWSER_FIXTURE.resolve())
    assert services["app"].environment == (("PORT", "3000"),)
    assert services["app"].depends_on == ()
    assert services["browser"].build_context == str(_NODE_BROWSER_FIXTURE.resolve())
    assert services["browser"].dockerfile == "Dockerfile.playwright"
    assert services["browser"].depends_on == ("app",)


@pytest.mark.unit
async def test_rendered_node_next_browser_compose_expresses_browser_validation_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = await _launched_node_browser_spec(tmp_path, monkeypatch)
    manager = ComposeManager(work_dir=tmp_path / "work", template_path=_TEMPLATE)

    parsed = yaml.safe_load(manager.render(spec).compose_file.read_text())

    assert set(parsed["services"]) == {"agent", "app", "browser"}
    assert "docker" not in parsed["services"]

    app = parsed["services"]["app"]
    assert app["build"] == {
        "context": str(_NODE_BROWSER_FIXTURE.resolve()),
        "dockerfile": "Dockerfile",
    }
    assert app["environment"] == {"PORT": "3000"}
    assert app["healthcheck"]["test"] == [
        "CMD-SHELL",
        "node /app/scripts/container-healthcheck.mjs http://127.0.0.1:3000/healthz ok",
    ]
    assert app["command"] == "node /app/server.mjs"
    assert app["networks"] == ["awf_net"]
    assert "ports" not in app

    browser = parsed["services"]["browser"]
    assert browser["build"] == {
        "context": str(_NODE_BROWSER_FIXTURE.resolve()),
        "dockerfile": "Dockerfile.playwright",
    }
    assert browser["environment"] == {
        "APP_BASE_URL": "http://app:3000",
        "PORT": "9323",
    }
    assert browser["depends_on"] == {"app": {"condition": "service_healthy"}}
    assert browser["healthcheck"]["test"] == [
        "CMD-SHELL",
        "node /app/scripts/container-healthcheck.mjs http://127.0.0.1:9323/healthz ok",
    ]
    assert browser["command"] == "node /app/browser/validator-server.mjs"
    assert browser["networks"] == ["awf_net"]
    assert "ports" not in browser

    agent = parsed["services"]["agent"]
    assert agent["environment"]["APP_BASE_URL"] == "http://app:3000"
    assert agent["environment"]["BROWSER_VALIDATE_URL"] == "http://browser:9323/validate"
    assert agent["environment"]["AWF_APP_ENDPOINT_APP_URL"] == "http://app:3000/"
    assert agent["environment"]["AWF_APP_ENDPOINT_BROWSER_VALIDATION_URL"] == (
        "http://browser:9323/validate"
    )
    assert [
        endpoint["name"] for endpoint in json.loads(agent["environment"]["AWF_APP_ENDPOINTS_JSON"])
    ] == ["app", "browser_validation"]
    assert agent["depends_on"] == {
        "app": {"condition": "service_healthy"},
        "browser": {"condition": "service_healthy"},
    }
    assert parsed["volumes"] == {}
    assert parsed["networks"]["awf_net"]["name"] == "awf-ws_node_browser-net"
