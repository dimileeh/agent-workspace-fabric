"""Forge-neutral git-over-HTTPS auth wiring tests (BitBucket Cloud).

Security focus: the Atlassian API token must never appear in a git-config
value, a clone/push URL, or an error message. The credential helper references
the env var *names* only; git invokes it and reads the values at call time.
"""

from __future__ import annotations

import pytest

from awf.common.git_auth import (
    BITBUCKET_GIT_AUTH_NOT_CONFIGURED,
    GitAuthNotConfiguredError,
    apply_bitbucket_git_auth,
    bitbucket_git_config_entries,
    is_bitbucket_repo,
    verify_bitbucket_git_auth,
)

_TOKEN = "ATATT-do-not-render-secret"
_EMAIL = "agent@example.com"


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo_url",
    [
        "https://bitbucket.org/ws/repo.git",
        "git@bitbucket.org:ws/repo.git",
        "ssh://git@bitbucket.org/ws/repo.git",
    ],
)
def test_is_bitbucket_repo_true_for_bitbucket_urls(repo_url: str) -> None:
    assert is_bitbucket_repo(repo_url) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo_url",
    [
        "https://github.com/ws/repo.git",
        "git@github.com:ws/repo.git",
        "ws/repo",  # bare slug defaults to GitHub
        "not a url at all",  # malformed -> ValueError -> False
        "",  # empty -> False, never raises
    ],
)
def test_is_bitbucket_repo_false_for_non_bitbucket_or_malformed(repo_url: str) -> None:
    assert is_bitbucket_repo(repo_url) is False


@pytest.mark.unit
def test_bitbucket_git_config_entries_reference_env_names_not_token() -> None:
    entries = dict(bitbucket_git_config_entries())

    # Host-scoped to bitbucket.org only (GitHub path stays untouched).
    assert all("bitbucket.org" in key for key in entries)
    helper = entries["credential.https://bitbucket.org.helper"]
    # The git username for an Atlassian API token over HTTPS is the fixed
    # account-agnostic sentinel ``x-bitbucket-api-token-auth`` — NOT the email.
    # BitBucket's git endpoint rejects the email with 401 (the REST API accepts
    # email:token, but git over HTTPS does not). See issue #467.
    assert "x-bitbucket-api-token-auth" in helper
    # The token is still expanded from its env var *name* at git call time.
    assert "$BITBUCKET_API_TOKEN" in helper
    # The email is NOT used for git auth (only the REST ForgeClient uses it).
    assert "$BITBUCKET_EMAIL" not in helper
    # No literal secret in any config value.
    assert all(_TOKEN not in value for value in entries.values())
    assert all(_EMAIL not in value for value in entries.values())


@pytest.mark.unit
def test_bitbucket_git_config_entries_rewrite_ssh_remotes_to_https() -> None:
    # Mirrors the GitHub ``url.https://github.com/.insteadOf = git@github.com:``
    # rewrite so an SSH-form bitbucket remote is rewritten to HTTPS and actually
    # uses the token credential helper. ``insteadOf`` is multi-valued: both SSH
    # URL shapes that ``RepoRef.from_url`` accepts must be covered — the scp-like
    # ``git@bitbucket.org:ws/repo.git`` form and the
    # ``ssh://git@bitbucket.org/ws/repo.git`` form.
    insteadof_values = [
        value
        for key, value in bitbucket_git_config_entries()
        if key == "url.https://bitbucket.org/.insteadOf"
    ]
    assert "git@bitbucket.org:" in insteadof_values
    assert "ssh://git@bitbucket.org/" in insteadof_values


@pytest.mark.unit
def test_bitbucket_git_config_entries_clear_inherited_helper_first() -> None:
    entries = bitbucket_git_config_entries()
    helper_values = [
        value for key, value in entries if key == "credential.https://bitbucket.org.helper"
    ]
    # First a clearing empty value, then the real helper (git semantics).
    assert helper_values[0] == ""
    assert helper_values[1] != ""


@pytest.mark.unit
def test_apply_bitbucket_git_auth_wires_helper_without_leaking_token() -> None:
    env: dict[str, str] = {}
    applied = apply_bitbucket_git_auth(
        env, {"BITBUCKET_API_TOKEN": _TOKEN, "BITBUCKET_EMAIL": _EMAIL}
    )

    assert applied is True
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    count = int(env["GIT_CONFIG_COUNT"])
    rendered = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert "credential.https://bitbucket.org.helper" in rendered
    # The token value must never appear in the returned git env.
    assert all(_TOKEN not in value for value in env.values())


@pytest.mark.unit
def test_apply_bitbucket_git_auth_accumulates_onto_existing_git_config() -> None:
    # Pre-seed an env as the GitHub block would (safe.directory at index 0).
    env: dict[str, str] = {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }
    apply_bitbucket_git_auth(env, {"BITBUCKET_API_TOKEN": _TOKEN, "BITBUCKET_EMAIL": _EMAIL})

    count = int(env["GIT_CONFIG_COUNT"])
    rendered = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    # The pre-existing entry is preserved, bitbucket entries are appended.
    assert rendered["safe.directory"] == "*"
    assert "credential.https://bitbucket.org.useHttpPath" in rendered


@pytest.mark.unit
@pytest.mark.parametrize(
    "source_env",
    [
        {},
        {"BITBUCKET_API_TOKEN": _TOKEN},  # missing email
        {"BITBUCKET_EMAIL": _EMAIL},  # missing token
        {"BITBUCKET_API_TOKEN": "  ", "BITBUCKET_EMAIL": _EMAIL},  # blank token
        {"BITBUCKET_API_TOKEN": _TOKEN, "BITBUCKET_EMAIL": "  "},  # blank email
    ],
)
def test_apply_bitbucket_git_auth_noop_when_not_configured(
    source_env: dict[str, str],
) -> None:
    env: dict[str, str] = {}
    assert apply_bitbucket_git_auth(env, source_env) is False
    assert env == {}


@pytest.mark.unit
def test_verify_bitbucket_git_auth_passes_when_configured() -> None:
    # Should not raise.
    verify_bitbucket_git_auth(
        "https://bitbucket.org/ws/repo.git",
        {"BITBUCKET_API_TOKEN": _TOKEN, "BITBUCKET_EMAIL": _EMAIL},
    )


@pytest.mark.unit
def test_verify_bitbucket_git_auth_noop_for_github_repo() -> None:
    # GitHub repo: never raises regardless of (missing) bitbucket env.
    verify_bitbucket_git_auth("https://github.com/ws/repo.git", {})


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo_url",
    [
        "git@bitbucket.org:ws/repo.git",
        "ssh://git@bitbucket.org/ws/repo.git",
    ],
)
def test_verify_bitbucket_git_auth_noop_for_ssh_bitbucket_repo(repo_url: str) -> None:
    # SSH clones authenticate with SSH keys, not the HTTPS credential helper, so
    # the preflight must not fail fast even when the HTTPS env vars are absent.
    verify_bitbucket_git_auth(repo_url, {})


@pytest.mark.unit
@pytest.mark.parametrize(
    "source_env",
    [
        {},
        {"BITBUCKET_API_TOKEN": _TOKEN},
        {"BITBUCKET_EMAIL": _EMAIL},
    ],
)
def test_verify_bitbucket_git_auth_raises_reason_coded_when_missing(
    source_env: dict[str, str],
) -> None:
    with pytest.raises(GitAuthNotConfiguredError) as raised:
        verify_bitbucket_git_auth("https://bitbucket.org/ws/repo.git", source_env)

    assert raised.value.reason_code == BITBUCKET_GIT_AUTH_NOT_CONFIGURED
    # The message names only the missing env var, never any value.
    message = str(raised.value)
    assert "BITBUCKET_API_TOKEN" in message or "BITBUCKET_EMAIL" in message
    assert _TOKEN not in message
    assert _EMAIL not in message
