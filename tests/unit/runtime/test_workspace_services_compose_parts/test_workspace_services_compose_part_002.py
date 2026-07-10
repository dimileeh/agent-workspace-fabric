"""No-Docker compose coverage for profile-declared workspace services (part 2).

Shared fixtures/helpers live in
``test_workspace_services_compose_part_001``; this part imports them. Split
from the original monolithic module to stay under the first-party file line
limit (see ``test_core_decomposition_maintainability``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from awf.profiles.compose import (
    filter_hosted_env_passthrough_names,
    literal_profile_env_from_compose,
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
def test_hosted_github_token_passthrough_names_surfaces_source_when_worker_only_has_awf_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hosted path surfaces the chosen token SOURCE name so hosted ``gh`` works.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PYNGv: ``_github_token_placeholder``
    orders ``AWF_GITHUB_TOKEN`` first, so a worker that only has
    ``AWF_GITHUB_TOKEN`` set (the documented service token) returns the
    ``${AWF_GITHUB_TOKEN}`` placeholder. Local Compose substitutes that
    placeholder into the ``GH_TOKEN`` / ``GITHUB_TOKEN`` aliases at stack
    launch, so the local agent container can run ``gh``. The hosted (non-compose)
    path has no compose env-block substitution: the hosted executor resolves
    ``env_passthrough_names`` out-of-band *by name*, so resolving ``GH_TOKEN`` /
    ``GITHUB_TOKEN`` finds nothing when the worker only has ``AWF_GITHUB_TOKEN``.
    Without the source name the hosted executor cannot resolve the credential
    and the hosted monitor-repair agent loses GitHub CLI access even though the
    same workspace has it under Compose.

    The helper must therefore surface the chosen source name
    (``AWF_GITHUB_TOKEN``) alongside the aliases so the hosted executor can
    resolve the credential from the source name and mirror it into the aliases
    (the same ``AWF_GITHUB_TOKEN`` -> ``GH_TOKEN`` / ``GITHUB_TOKEN`` mirroring
    ``_service_git_environment`` / ``_check_github`` / ``_gh_probe_environ``
    already apply). Names only — never the placeholder value or the secret.
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
    # The common setup: only the documented AWF source token is set; the
    # gh-visible aliases are absent from the worker env.
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    names = hosted_github_token_passthrough_names(compose_file)

    # The chosen source name is surfaced so the hosted executor can resolve the
    # credential from it (the aliases alone would resolve to nothing in this
    # setup). The aliases are still surfaced for hosted executors that resolve
    # them directly when present.
    assert "AWF_GITHUB_TOKEN" in names
    assert "GH_TOKEN" in names
    assert "GITHUB_TOKEN" in names
    # Names only — no secret value and no placeholder string is returned.
    assert "ghp_worker_secret" not in names
    assert "${AWF_GITHUB_TOKEN}" not in names


@pytest.mark.unit
def test_hosted_github_token_passthrough_names_surfaces_source_once_when_source_is_alias(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The source name is surfaced once even when it is also a surfaced alias.

    When the worker token source is ``GH_TOKEN`` (the first gh-visible alias in
    ``_github_token_placeholder``'s order after ``AWF_GITHUB_TOKEN``), the source
    name is the same as a surfaced alias. The result must contain it exactly
    once (de-duplicated), not twice.
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
                            "GH_TOKEN": "${GH_TOKEN}",
                            "GITHUB_TOKEN": "${GH_TOKEN}",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("AWF_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "ghp_worker_secret")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    names = hosted_github_token_passthrough_names(compose_file)

    # GH_TOKEN is both the chosen source and a surfaced alias; it appears once.
    assert names.count("GH_TOKEN") == 1
    assert "GH_TOKEN" in names
    assert "GITHUB_TOKEN" in names
    # The AWF source name is not surfaced (it is not the chosen source here).
    assert "AWF_GITHUB_TOKEN" not in names


@pytest.mark.unit
@pytest.mark.parametrize(
    ("worker_env", "compose_environment", "expected_names", "unexpected_values"),
    [
        (
            {
                "GH_TOKEN": "ghp_worker_secret",
                "GITHUB_TOKEN": "ghp_worker_fallback_secret",
            },
            {
                "GH_TOKEN": "${GH_TOKEN:-}",
                "GITHUB_TOKEN": "${GITHUB_TOKEN:-}",
            },
            ("GH_TOKEN", "GITHUB_TOKEN"),
            ("ghp_worker_secret", "ghp_worker_fallback_secret", "${GH_TOKEN:-}"),
        ),
        (
            {"AWF_GITHUB_TOKEN": "ghp_awf_worker_secret"},
            {
                "AWF_GITHUB_TOKEN": "${AWF_GITHUB_TOKEN:-}",
                "GH_TOKEN": "${AWF_GITHUB_TOKEN}",
            },
            ("AWF_GITHUB_TOKEN", "GH_TOKEN"),
            ("ghp_awf_worker_secret", "${AWF_GITHUB_TOKEN:-}"),
        ),
    ],
)
def test_hosted_github_token_passthrough_names_accepts_same_name_defaulted_worker_tokens(
    tmp_path: Path,
    worker_env: dict[str, str],
    compose_environment: dict[str, str],
    expected_names: tuple[str, ...],
    unexpected_values: tuple[str, ...],
) -> None:
    """Defaulted same-name GitHub token slots are worker-resolved, not profile-owned.

    Regression for PR #754 thread PRRT_kwDOSJAM6s6Pse2h: when a profile declares
    a GitHub token source/alias as a defaulted same-name expression such as
    ``GH_TOKEN: ${GH_TOKEN:-}`` or
    ``AWF_GITHUB_TOKEN: ${AWF_GITHUB_TOKEN:-}``, Docker Compose gives the local
    agent the worker token at stack launch when that worker token is set. The
    hosted path must surface the matching names for out-of-band resolution
    instead of suppressing the whole GitHub token group as profile-owned.
    """
    from awf.profiles.compose import hosted_github_token_passthrough_names

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": compose_environment,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    names = hosted_github_token_passthrough_names(compose_file, worker_env=worker_env)

    for expected_name in expected_names:
        assert expected_name in names
    for unexpected_value in unexpected_values:
        assert unexpected_value not in names


@pytest.mark.unit
def test_hosted_github_token_passthrough_names_skips_profile_owned_awf_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile-owned AWF source token is not shadowed by the worker source."""
    from awf.profiles.compose import hosted_github_token_passthrough_names

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Profile owns the documented AWF source token.
                            "AWF_GITHUB_TOKEN": "ghp_profile_token_secret",
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

    assert names == ()


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
@pytest.mark.parametrize(
    "compose_alias_value",
    [
        None,  # ``GH_TOKEN:`` / ``GH_TOKEN: null`` — pass-through slot
        "list",  # ``environment: [GH_TOKEN]`` — bare-name pass-through slot
    ],
)
def test_hosted_github_token_passthrough_names_pass_through_alias_matches_worker_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compose_alias_value: object,
) -> None:
    """A pass-through GitHub alias is treated as the worker source, not suppressed.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PZkRH: when a compose-declared
    GitHub alias is a true pass-through slot (``environment: [GH_TOKEN]`` with
    no ``=``, ``GH_TOKEN:`` / ``GH_TOKEN: null``), ``_compose_environment_mapping``
    normalizes it to the :data:`_COMPOSE_PASSTHROUGH` sentinel. The local Compose
    agent still receives the worker token at stack launch (Docker Compose takes
    the pass-through slot's value from the worker shell), so the hosted path must
    treat the pass-through alias as matching the corresponding worker source
    rather than suppressing the whole group. Without this, hosted monitor-repair
    loses ``gh`` auth in that pass-through configuration.

    The pass-through alias itself is a worker-resolved slot the hosted executor
    resolves out-of-band (its name is surfaced so the hosted executor mirrors
    the worker shell value, the same way local Compose took it from the worker
    shell); it is NOT a profile-owned distinct token, so it does not trigger the
    group-suppression branch.
    """
    from awf.profiles.compose import _COMPOSE_PASSTHROUGH, hosted_github_token_passthrough_names

    if compose_alias_value == "list":
        compose_env_block: object = ["GH_TOKEN", "GITHUB_TOKEN=${AWF_GITHUB_TOKEN}"]
    else:
        compose_env_block = {"GH_TOKEN": compose_alias_value, "GITHUB_TOKEN": "${AWF_GITHUB_TOKEN}"}

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {"services": {"agent": {"image": "agent:latest", "environment": compose_env_block}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AWF_GITHUB_TOKEN", "ghp_worker_secret")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    names = hosted_github_token_passthrough_names(compose_file)

    # The pass-through GH_TOKEN is worker-resolved (the local container received
    # the worker shell value), so it does NOT suppress the group. The matching
    # GITHUB_TOKEN alias and the AWF source name are surfaced so the hosted
    # executor can resolve the credential out-of-band. The pass-through sentinel
    # value itself is never returned (names only).
    assert "AWF_GITHUB_TOKEN" in names
    assert "GITHUB_TOKEN" in names
    assert "GH_TOKEN" in names
    assert _COMPOSE_PASSTHROUGH not in names
    assert "ghp_worker_secret" not in names


@pytest.mark.unit
def test_literal_profile_env_from_compose_unreadable_is_empty(
    tmp_path: Path,
) -> None:
    """An unreadable compose yields no profile env (fail-closed, no values)."""

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
def test_literal_profile_env_from_compose_skips_profile_owned_auth_literals(
    tmp_path: Path,
) -> None:
    """A profile-owned auth literal (a concrete API key / token string) is NOT
    carried to the hosted executor.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PaYta: rendered compose files
    can hold profile-owned auth literals — for example
    ``OPENAI_API_KEY: "sk-profile-key"`` or ``CODEX_API_KEY: "sk-codex"``. Those
    resolve to ``LITERAL`` and do not bear a postgres password, so
    ``literal_profile_env_from_compose`` carried them verbatim into
    ``AgentRuntimeExecRequest.profile_env``, breaking the documented
    secret-free hosted contract even though the same keys are correctly kept
    out of ``env_passthrough_names`` by ``_profile_owned_auth_keys`` (the hosted
    executor resolves auth out-of-band). Any ``AGENT_AUTH_ENV_VARS`` key
    declared on the agent service is a profile-owned auth slot and its literal
    value must never reach ``profile_env``.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Profile-owned auth-secret literals -> skipped.
                            "OPENAI_API_KEY": "sk-profile-openai-key",
                            "CODEX_API_KEY": "sk-profile-codex-key",
                            "ANTHROPIC_AUTH_TOKEN": "sk-ant-profile-token",
                            "OLLAMA_API_KEY": "ollama-profile-key",
                            # Non-secret profile literals (some are in
                            # AGENT_AUTH_ENV_VARS but not secret-bearing) ->
                            # still carried.
                            "OLLAMA_HOST": "http://ollama.profile:11434",
                            "OPENAI_BASE_URL": "https://profile.proxy/v1",
                            "ANTHROPIC_BASE_URL": "https://anthropic.profile/v1",
                            # Non-auth literal that merely looks token-like must
                            # still be carried (only secret-bearing auth keys
                            # redact).
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    # Non-secret profile literals are still carried to the hosted executor.
    assert carried.get("OLLAMA_HOST") == "http://ollama.profile:11434"
    assert carried.get("OPENAI_BASE_URL") == "https://profile.proxy/v1"
    assert carried.get("ANTHROPIC_BASE_URL") == "https://anthropic.profile/v1"
    assert carried.get("APP_BASE_URL") == "http://app:8080"
    # Profile-owned auth-secret literals are NOT carried to the hosted executor.
    assert "OPENAI_API_KEY" not in carried
    assert "CODEX_API_KEY" not in carried
    assert "ANTHROPIC_AUTH_TOKEN" not in carried
    assert "OLLAMA_API_KEY" not in carried
    # The concrete secret strings never reach the hosted request object.
    blob = "".join(v for _k, v in profile_env)
    assert "sk-profile-openai-key" not in blob
    assert "sk-profile-codex-key" not in blob
    assert "sk-ant-profile-token" not in blob
    assert "ollama-profile-key" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_carries_empty_auth_literals(
    tmp_path: Path,
) -> None:
    """Explicit empty auth literals are carried to blank hosted credentials.

    Regression for PR #754 thread PRRT_kwDOSJAM6s6P0GRE: local Compose treats
    ``ANTHROPIC_API_KEY: ""`` as a concrete empty value that overrides any
    worker credential at stack launch. The hosted path also excludes that
    compose-declared name from passthrough, so ``literal_profile_env_from_compose``
    must carry the empty literal in ``profile_env``. Concrete secret-bearing
    literals remain redacted.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OPENAI_API_KEY": "",
                            "ANTHROPIC_API_KEY": "",
                            "CODEX_API_KEY": "sk-profile-codex-key",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(
        compose_file,
        worker_env={
            "OPENAI_API_KEY": "sk-worker-openai",
            "ANTHROPIC_API_KEY": "sk-worker-anthropic",
        },
    )
    carried = dict(profile_env)

    assert carried["OPENAI_API_KEY"] == ""
    assert carried["ANTHROPIC_API_KEY"] == ""
    assert carried["APP_BASE_URL"] == "http://app:8080"
    assert "CODEX_API_KEY" not in carried
    blob = "\x00".join(v for _k, v in profile_env)
    assert "sk-worker-openai" not in blob
    assert "sk-worker-anthropic" not in blob
    assert "sk-profile-codex-key" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_profile_owned_claude_backend_secrets(
    tmp_path: Path,
) -> None:
    """Profile-owned Claude Bedrock backend credential literals are NOT carried.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PiiaQ: when a Claude Code
    Bedrock profile declares backend credentials such as
    ``AWS_SECRET_ACCESS_KEY`` / ``AWS_SESSION_TOKEN`` /
    ``AWS_BEARER_TOKEN_BEDROCK`` as literal agent env values, the hosted
    passthrough filter already treats those compose-declared names as
    profile-owned (``_filter_hosted_env_passthrough_names_from_compose_env``
    excludes any compose-declared non-pass-through, non-worker-resolved-
    defaulted name), so the hosted executor resolves them out-of-band. But
    ``literal_profile_env_from_compose`` only redacted keys in
    ``_AGENT_AUTH_SECRET_ENV_VARS``, and those Claude backend credential names
    were absent from the set, so their raw secret literals were appended to
    ``AgentRuntimeExecRequest.profile_env``, violating the secret-free hosted
    contract and exposing AWS credentials to the hosted executor/request
    object. ``AWS_ACCESS_KEY_ID`` is a credential identifier and must not be
    carried in direct job env either. Backend credential material/identifiers
    must be added to the redaction set while still carrying non-secret backend
    config (regions, profile names, project ids, endpoint regions).
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Profile-owned Claude Bedrock backend credential
                            # literals -> skipped (secret-bearing).
                            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
                            "AWS_SESSION_TOKEN": "IQoJb3JpZ2luX2VjEGgaCXVzLWVhc3QtMSJIMEY",
                            "AWS_BEARER_TOKEN_BEDROCK": "BEDROCK-BEARER-TOKEN-SECRET",
                            # Credential identifier -> skipped; hosted resolves
                            # it out-of-band via name-only passthrough.
                            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
                            # Non-secret backend config (region / profile /
                            # project / endpoint region) -> still carried.
                            "AWS_REGION": "us-west-2",
                            "AWS_DEFAULT_REGION": "us-west-2",
                            "AWS_PROFILE": "bedrock-profile",
                            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-123",
                            "CLOUD_ML_REGION": "us-east5",
                            # Non-auth literal still carried.
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    # Non-secret backend config is still carried to the hosted executor.
    assert carried.get("AWS_REGION") == "us-west-2"
    assert carried.get("AWS_DEFAULT_REGION") == "us-west-2"
    assert carried.get("AWS_PROFILE") == "bedrock-profile"
    assert carried.get("ANTHROPIC_VERTEX_PROJECT_ID") == "proj-123"
    assert carried.get("CLOUD_ML_REGION") == "us-east5"
    assert carried.get("APP_BASE_URL") == "http://app:8080"
    # Profile-owned backend credential secret/identifier literals are NOT carried.
    assert "AWS_ACCESS_KEY_ID" not in carried
    assert "AWS_SECRET_ACCESS_KEY" not in carried
    assert "AWS_SESSION_TOKEN" not in carried
    assert "AWS_BEARER_TOKEN_BEDROCK" not in carried
    # The concrete secret strings never reach the hosted request object.
    blob = "".join(v for _k, v in profile_env)
    assert "AKIAIOSFODNN7EXAMPLE" not in blob
    assert "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY" not in blob
    assert "IQoJb3JpZ2luX2VjEGgaCXVzLWVhc3QtMSJIMEY" not in blob
    assert "BEDROCK-BEARER-TOKEN-SECRET" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_profile_owned_github_token_literals(
    tmp_path: Path,
) -> None:
    """A profile-owned literal GitHub token is NOT carried to the hosted executor.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PiwKv: in hosted runs where a
    profile/compose agent env owns a literal GitHub credential such as
    ``GH_TOKEN`` / ``GITHUB_TOKEN`` / ``AWF_GITHUB_TOKEN``, those names were
    absent from ``_AGENT_AUTH_SECRET_ENV_VARS``, so
    ``literal_profile_env_from_compose`` appended the raw token literal to
    ``AgentRuntimeExecRequest.profile_env``. The local Compose path keeps the
    value inside the already-started container, but the hosted path transports
    it in the request object despite the secret-free contract. The GitHub token
    names must be redacted before carrying profile env values, while non-secret
    profile config still reaches the hosted executor.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Profile-owned GitHub token literals -> skipped
                            # (secret-bearing).
                            "GH_TOKEN": "ghp_profile_github_token_secret",
                            "GITHUB_TOKEN": "ghp_profile_legacy_token_secret",
                            "AWF_GITHUB_TOKEN": "ghp_profile_awf_token_secret",
                            # Non-secret profile literal still carried.
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    # Non-secret profile literal is still carried to the hosted executor.
    assert carried.get("APP_BASE_URL") == "http://app:8080"
    # Profile-owned GitHub token literals are NOT carried to the hosted executor.
    assert "GH_TOKEN" not in carried
    assert "GITHUB_TOKEN" not in carried
    assert "AWF_GITHUB_TOKEN" not in carried
    # The concrete secret strings never reach the hosted request object.
    blob = "".join(v for _k, v in profile_env)
    assert "ghp_profile_github_token_secret" not in blob
    assert "ghp_profile_legacy_token_secret" not in blob
    assert "ghp_profile_awf_token_secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_embedded_auth_header_literals(
    tmp_path: Path,
) -> None:
    """Embedded auth header credentials are NOT carried to hosted profile env.

    Regression for PR #754 threads PRRT_kwDOSJAM6s6PwWHe and
    PRRT_kwDOSJAM6s6PypKt: non-secret-looking profile env names such as
    ``HTTP_HEADERS`` / ``CURL_ARGS`` can still carry literal
    ``Authorization: Bearer ...``, ``Authorization: Basic ...``, or token-style
    ``Authorization: token ...`` credentials. Those values must be skipped
    before building the hosted ``profile_env`` request payload.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "HTTP_HEADERS": (
                                "Accept: application/json\n"
                                "Authorization: Bearer embedded-bearer-secret"
                            ),
                            "CURL_ARGS": ("-fsS -H 'Authorization: Basic embedded-basic-secret'"),
                            "GH_CURL_ARGS": (
                                "-fsS -H 'Authorization: token embedded-token-secret'"
                            ),
                            "ALT_CURL_ARGS": (
                                '-fsS -H "Authorization: Token embedded-alt-token-secret"'
                            ),
                            # Non-secret profile literal still carried.
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried.get("APP_BASE_URL") == "http://app:8080"
    assert "HTTP_HEADERS" not in carried
    assert "CURL_ARGS" not in carried
    assert "GH_CURL_ARGS" not in carried
    assert "ALT_CURL_ARGS" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "embedded-bearer-secret" not in blob
    assert "embedded-basic-secret" not in blob
    assert "embedded-token-secret" not in blob
    assert "embedded-alt-token-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_standard_auth_credential_literals(
    tmp_path: Path,
) -> None:
    """Standard AUTH-bearing credential env literals are NOT carried.

    Regression for PR #754 thread PRRT_kwDOSJAM6s6PweEC: names such as
    ``REDISCLI_AUTH`` and ``DOCKER_AUTH_CONFIG`` carry concrete credentials but
    do not match the generic secret-name tokens because ``AUTH`` itself is not a
    redaction token.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "REDISCLI_AUTH": "redis-profile-password",
                            "DOCKER_AUTH_CONFIG": (
                                '{"auths":{"registry.example":{"auth":"registry-profile-secret"}}}'
                            ),
                            "AUTH_MODE": "basic",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried.get("APP_BASE_URL") == "http://app:8080"
    assert carried.get("AUTH_MODE") == "basic"
    assert "REDISCLI_AUTH" not in carried
    assert "DOCKER_AUTH_CONFIG" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "redis-profile-password" not in blob
    assert "registry-profile-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_concatenated_password_token_literals(
    tmp_path: Path,
) -> None:
    """No-underscore password/token env literals are NOT carried.

    Regression for PR #754 thread PRRT_kwDOSJAM6s6P0bNG: names such as
    ``DBPASSWORD`` and ``SESSIONTOKEN`` become a single tokenizer token, so they
    must be covered by the concatenated-token redaction list.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "DBPASSWORD": "db-profile-password",
                            "POSTGRESPASSWORD": "postgres-profile-password",
                            "REFRESHTOKEN": "refresh-profile-token",
                            "SESSIONTOKEN": "session-profile-token",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried.get("APP_BASE_URL") == "http://app:8080"
    assert "DBPASSWORD" not in carried
    assert "POSTGRESPASSWORD" not in carried
    assert "REFRESHTOKEN" not in carried
    assert "SESSIONTOKEN" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "db-profile-password" not in blob
    assert "postgres-profile-password" not in blob
    assert "refresh-profile-token" not in blob
    assert "session-profile-token" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_public_key_literals(
    tmp_path: Path,
) -> None:
    """Public frontend key literals are profile config, not hosted secrets."""

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "VAPID_PUBLIC_KEY": "vapid-public-key",
                            "RECAPTCHA_SITE_KEY": "recaptcha-site-key",
                            "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY": "pk_test_publishable",
                            "PRIVATE_KEY": "private-key-secret",
                            "CUSTOM_API_KEY": "custom-api-secret",
                            "APP_BASE_URL": "http://app:8080",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried["VAPID_PUBLIC_KEY"] == "vapid-public-key"
    assert carried["RECAPTCHA_SITE_KEY"] == "recaptcha-site-key"
    assert carried["NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY"] == "pk_test_publishable"
    assert carried["APP_BASE_URL"] == "http://app:8080"
    assert "PRIVATE_KEY" not in carried
    assert "CUSTOM_API_KEY" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "private-key-secret" not in blob
    assert "custom-api-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_preserves_token_endpoint_literals(
    tmp_path: Path,
) -> None:
    """OAuth/OIDC token endpoint names are config, not credentials.

    Regression for PR #754 thread PRRT_kwDOSJAM6s6Pydva: endpoint variables such
    as ``OIDC_TOKEN_URL`` and ``OAUTH_TOKEN_ENDPOINT`` include the token
    vocabulary but their literal values are provider URLs required by hosted
    repairs/builds. Credential-bearing endpoint values must still be redacted by
    the value checks.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "OIDC_TOKEN_URL": "https://issuer.example/oauth/token",
                            "OAUTH_TOKEN_ENDPOINT": "https://auth.example/token",
                            "OAUTH_TOKEN_URI": "https://auth.example/token-uri",
                            "NPM_TOKEN": "npm-profile-secret",
                            "OAUTH_TOKEN_ENDPOINT_URL": (
                                "https://issuer.example/oauth/token?token=endpoint-secret"
                            ),
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried["OIDC_TOKEN_URL"] == "https://issuer.example/oauth/token"
    assert carried["OAUTH_TOKEN_ENDPOINT"] == "https://auth.example/token"
    assert carried["OAUTH_TOKEN_URI"] == "https://auth.example/token-uri"
    assert "NPM_TOKEN" not in carried
    assert "OAUTH_TOKEN_ENDPOINT_URL" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "npm-profile-secret" not in blob
    assert "endpoint-secret" not in blob


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_jdbc_url_userinfo(
    tmp_path: Path,
) -> None:
    """JDBC-style URL literals with userinfo are NOT carried to hosted profile env.

    Regression for PR #754 thread PRRT_kwDOSJAM6s6PvGqq: ``urlsplit`` treats the
    outer ``jdbc:`` prefix as the URL scheme and leaves ``netloc`` empty for
    values like ``jdbc:postgresql://user:secret@db.example/app``. The generic
    URL-userinfo guard therefore missed embedded credentials when the env name
    was not itself secret-like, and ``literal_profile_env_from_compose`` carried
    the raw JDBC URL into ``AgentRuntimeExecRequest.profile_env``.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            # Non-secret literal still carried.
                            "APP_BASE_URL": "http://app:8080",
                            # JDBC URL embeds credentials but the key is not
                            # secret-like -> must still be skipped.
                            "JDBC_DATABASE_URL": (
                                "jdbc:postgresql://app_user:db-secret@db.example/app"
                            ),
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried.get("APP_BASE_URL") == "http://app:8080"
    assert "JDBC_DATABASE_URL" not in carried
    assert "db-secret" not in "".join(v for _k, v in profile_env)


@pytest.mark.unit
def test_literal_profile_env_from_compose_skips_signed_url_query_credentials(
    tmp_path: Path,
) -> None:
    """Signed URL literals are NOT carried to hosted profile env.

    Regression for PR #754 thread PRRT_kwDOSJAM6s6Pxulk: non-secret-looking env
    names can carry bearer-equivalent signed URLs, and query fields such as
    ``X-Amz-Signature`` or Azure SAS ``sig`` must be treated as credentials even
    though they do not include broader secret-name tokens.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "agent": {
                        "image": "agent:latest",
                        "environment": {
                            "APP_BASE_URL": "http://app:8080",
                            "ARTIFACT_URL": (
                                "https://bucket.s3.amazonaws.com/key"
                                "?X-Amz-Algorithm=AWS4-HMAC-SHA256"
                                "&X-Amz-Credential=AKIA/20260710/us-east-1/s3/aws4_request"
                                "&X-Amz-Signature=s3-signed-secret"
                            ),
                            "EXPORT_URL": (
                                "https://account.blob.core.windows.net/container/blob"
                                "?sp=r&st=2026-07-10T00:00:00Z&sig=azure-sas-secret"
                            ),
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    profile_env = literal_profile_env_from_compose(compose_file, worker_env={})
    carried = dict(profile_env)

    assert carried.get("APP_BASE_URL") == "http://app:8080"
    assert "ARTIFACT_URL" not in carried
    assert "EXPORT_URL" not in carried
    blob = "\x00".join(f"{key}={value}" for key, value in profile_env)
    assert "s3-signed-secret" not in blob
    assert "azure-sas-secret" not in blob


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
def test_literal_profile_env_from_compose_redacts_postgres_password_bare_slot(
    tmp_path: Path,
) -> None:
    """A service may declare ``POSTGRES_PASSWORD`` as a bare Compose
    interpolation slot (e.g. ``POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}``), which
    ``_compose_resolve_value`` classifies as ``WORKER_RESOLVED_SLOT`` (the bare
    ``${NAME}`` form has no default operator). Docker Compose resolves it against
    the worker env at stack launch, so the local agent container receives the
    concrete worker password — and a rendered agent env DB URL embedding that
    resolved value carries the workspace credential.

    ``_collect_postgres_password`` previously only recovered the concrete worker
    value for the ``WORKER_RESOLVED_DEFAULTED`` branch (``:-`` / ``-`` / ``:?`` /
    ``?`` with the variable set), leaving the bare ``${NAME}`` slot tracked only
    by its raw placeholder string. A rendered DB URL embedding the *resolved*
    worker password (not the unexpanded ``${POSTGRES_PASSWORD}`` placeholder)
    then slipped past substring redaction and ``literal_profile_env_from_compose``
    forwarded the secret-bearing URL in ``profile_env`` despite the secret-free
    contract. The redaction source must also recover the concrete worker value
    for ``WORKER_RESOLVED_SLOT`` so a bare-slot DB URL is redacted the same way a
    defaulted one is.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PaFeB.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            # Bare interpolation slot (no default): Compose
                            # resolves it against the worker env at stack launch.
                            "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
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
def test_literal_profile_env_from_compose_redacts_postgres_password_passthrough_slot(
    tmp_path: Path,
) -> None:
    """A service may declare ``POSTGRES_PASSWORD`` as a Compose *pass-through*
    slot (``POSTGRES_PASSWORD:`` / ``POSTGRES_PASSWORD: null`` /
    ``environment: [POSTGRES_PASSWORD]``), which ``_compose_environment_mapping``
    normalizes to the :data:`_COMPOSE_PASSTHROUGH` sentinel. Docker Compose
    resolves a pass-through slot from the worker shell at stack launch, so the
    local agent container receives the concrete worker password — and a rendered
    agent env DB URL embedding that resolved value carries the workspace
    credential.

    ``_collect_postgres_password`` previously early-returned on the
    ``_COMPOSE_PASSTHROUGH`` sentinel before recovering the concrete worker
    value, so a rendered ``DATABASE_URL`` / ``AWF_DATABASE_URL`` embedding the
    resolved worker password slipped past substring redaction and
    ``literal_profile_env_from_compose`` forwarded the secret-bearing URL in
    ``profile_env`` despite the secret-free contract. A pass-through slot must
    resolve ``POSTGRES_PASSWORD`` from ``worker_env`` for redaction the same way
    the ``WORKER_RESOLVED_SLOT`` / ``WORKER_RESOLVED_DEFAULTED`` branches do.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PjBsM.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": {
                            "POSTGRES_USER": "awf",
                            # Pass-through slot (mapping null form): Compose
                            # resolves it against the worker env at stack launch.
                            "POSTGRES_PASSWORD": None,
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
def test_literal_profile_env_from_compose_redacts_postgres_password_passthrough_slot_list_form(
    tmp_path: Path,
) -> None:
    """The list bare-name pass-through form
    (``environment: [POSTGRES_PASSWORD]`` with no ``=``) must redact the
    resolved worker password too, mirroring the mapping null-form slot (see
    ``test_literal_profile_env_from_compose_redacts_postgres_password_passthrough_slot``).
    Both normalize to the :data:`_COMPOSE_PASSTHROUGH` sentinel and resolve from
    the worker shell at stack launch.

    Regression for PR #751 thread PRRT_kwDOSJAM6s6PjBsM.
    """

    compose_file = tmp_path / "compose.yml"
    compose_file.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "postgres": {
                        "image": "postgres:16-alpine",
                        "environment": [
                            "POSTGRES_USER=awf",
                            # List bare-name pass-through slot (no ``=``):
                            # Compose resolves it against the worker env at
                            # stack launch.
                            "POSTGRES_PASSWORD",
                            "POSTGRES_DB=awf",
                        ],
                    },
                    "agent": {
                        "image": "agent:latest",
                        "environment": [
                            "OLLAMA_HOST=http://ollama.profile:11434",
                            # A rendered DB URL embedding the *resolved* worker
                            # value -> must be skipped (secret-bearing).
                            "DATABASE_URL=postgresql://awf:resolved-pw@postgres:5432/awf",
                        ],
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
