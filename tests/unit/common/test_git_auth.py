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
    apply_bitbucket_agent_git_auth,
    apply_bitbucket_git_auth,
    bitbucket_agent_git_config_entries,
    bitbucket_askpass_script,
    bitbucket_git_config_entries,
    is_bitbucket_repo,
    verify_bitbucket_git_auth,
)

_AGENT_INSTEADOF_KEY = "url.https://x-bitbucket-api-token-auth@bitbucket.org/.insteadOf"
_ASKPASS_PATH = "/run/awf/secrets/bb-askpass.sh"

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
def test_worker_bitbucket_git_config_entries_unchanged_byte_for_byte() -> None:
    # The worker credential-helper path is an independent mechanism from the
    # agent askpass + insteadOf path (#465/#466) and must stay byte-for-byte
    # unchanged. This locks the exact worker entries as a regression guard.
    assert bitbucket_git_config_entries() == (
        ("credential.https://bitbucket.org.helper", ""),
        (
            "credential.https://bitbucket.org.helper",
            '!f() { test "$1" = get && '
            "printf 'username=x-bitbucket-api-token-auth\\npassword=%s\\n' "
            '"$BITBUCKET_API_TOKEN"; }; f',
        ),
        ("credential.https://bitbucket.org.useHttpPath", "true"),
        ("url.https://bitbucket.org/.insteadOf", "git@bitbucket.org:"),
        ("url.https://bitbucket.org/.insteadOf", "ssh://git@bitbucket.org/"),
    )


@pytest.mark.unit
def test_bitbucket_askpass_script_is_static_and_references_env_name() -> None:
    script = bitbucket_askpass_script("BITBUCKET_API_TOKEN")

    # A POSIX-sh script that prints the token read from the container runtime env.
    assert script.startswith("#!/bin/sh")
    assert '"$BITBUCKET_API_TOKEN"' in script
    # No token value, no sentinel username, no credential-helper/username syntax.
    assert _TOKEN not in script
    assert "x-bitbucket-api-token-auth" not in script
    assert "username=" not in script
    # Static for the same env-var name (no nonce / no randomness).
    assert bitbucket_askpass_script("BITBUCKET_API_TOKEN") == script


@pytest.mark.unit
def test_bitbucket_askpass_script_references_exactly_the_injected_env_name() -> None:
    # The script references the env-var *name* passed in, not a hard-coded alias.
    script = bitbucket_askpass_script("CUSTOM_TOKEN_VAR")
    assert '"$CUSTOM_TOKEN_VAR"' in script
    assert "BITBUCKET_API_TOKEN" not in script


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad_name",
    [
        'MYVAR"; evil_cmd; echo "',  # shell metacharacters / command injection
        "1LEADING_DIGIT",  # must start with a letter or underscore
        "HAS SPACE",
        "HAS-DASH",
        "WITH$DOLLAR",
        "",
    ],
)
def test_bitbucket_askpass_script_rejects_non_env_name(bad_name: str) -> None:
    # The name is interpolated into the shell script body, so anything that is not
    # a valid POSIX env-var name is rejected rather than smuggled into the script.
    with pytest.raises(ValueError, match="invalid token env-var name"):
        bitbucket_askpass_script(bad_name)


def _run_askpass(prompt: str, *, token: str = _TOKEN) -> str:
    """Execute the generated askpass script for ``prompt`` and return its stdout.

    Drives the *real* POSIX-sh host gate so a regression in the ``case`` pattern
    (e.g. dropping the host scope and leaking the token to every prompt) fails
    loudly instead of passing a string-shape assertion.
    """
    import subprocess
    import tempfile
    from pathlib import Path

    script = bitbucket_askpass_script("BITBUCKET_API_TOKEN")
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, test-only
            ["/bin/sh", path, prompt],
            capture_output=True,
            text=True,
            env={"BITBUCKET_API_TOKEN": token},
            check=True,
        )
    finally:
        Path(path).unlink()
    return result.stdout


@pytest.mark.unit
@pytest.mark.parametrize(
    "prompt",
    [
        # The sentinel-username password prompt git actually issues after the
        # insteadOf rewrite, plus the bare-host, path, and port shapes.
        "Password for 'https://x-bitbucket-api-token-auth@bitbucket.org': ",
        "Password for 'https://bitbucket.org/ws/repo.git': ",
        "Username for 'https://bitbucket.org': ",
        "Password for 'https://bitbucket.org:443/ws/repo': ",
    ],
)
def test_bitbucket_askpass_script_emits_token_only_for_bitbucket_host(prompt: str) -> None:
    assert _run_askpass(prompt) == _TOKEN


@pytest.mark.unit
@pytest.mark.parametrize(
    "prompt",
    [
        # GIT_ASKPASS is process-wide: a foreign HTTPS host that prompts for
        # credentials must NOT receive the BitBucket token.
        "Password for 'https://github.com': ",
        "Password for 'https://x@github.com': ",
        "Password for 'https://gitlab.com/foo': ",
        # Look-alike hosts must not match the bitbucket.org host gate.
        "Password for 'https://bitbucket.org.evil.com/ws/repo': ",
        "Password for 'https://user@bitbucket.org.evil.com': ",
        "Password for 'https://notbitbucket.org/foo': ",
        # bitbucket.org embedded in another host's user-info or path must NOT
        # satisfy the gate (a substring match would leak the token; the script
        # parses the real host instead). See PR #471 thread PRRT_kwDOSJAM6s6H1x5T.
        "Password for 'https://user@github.com/foo@bitbucket.org/repo': ",
        "Password for 'https://user@evil.com/https://bitbucket.org/repo': ",
    ],
)
def test_bitbucket_askpass_script_withholds_token_from_other_hosts(prompt: str) -> None:
    assert _run_askpass(prompt) == ""


@pytest.mark.unit
@pytest.mark.parametrize(
    "prompt",
    [
        # ``RepoRef.from_url`` accepts http:// bitbucket remotes, so a plaintext
        # HTTP prompt can reach the process-wide askpass even though the host is
        # bitbucket.org. The script must require the https:// scheme and emit
        # nothing for HTTP (or any non-HTTPS) scheme — otherwise it would leak
        # BITBUCKET_API_TOKEN over the wire, violating the git-over-HTTPS/no-token-
        # leak contract. See PR #471 thread PRRT_kwDOSJAM6s6H2gN5.
        "Password for 'http://bitbucket.org/ws/repo.git': ",
        "Username for 'http://bitbucket.org': ",
        "Password for 'http://x-bitbucket-api-token-auth@bitbucket.org': ",
        "Password for 'http://bitbucket.org:80/ws/repo': ",
    ],
)
def test_bitbucket_askpass_script_requires_https_scheme(prompt: str) -> None:
    assert _run_askpass(prompt) == ""


@pytest.mark.unit
def test_bitbucket_agent_git_config_entries_rewrite_all_remote_forms() -> None:
    entries = bitbucket_agent_git_config_entries()

    # All three rewrites land on the sentinel-username insteadOf key.
    assert all(key == _AGENT_INSTEADOF_KEY for key, _ in entries)
    values = [value for _, value in entries]
    # HTTPS form (bare + explicit :443 port) + both SSH-form shapes
    # RepoRef.from_url accepts (bare + explicit :22 port). The explicit-port
    # entries are required because insteadOf matches sources as literal prefixes,
    # so the bare rules do not cover the explicit-port shapes (regression:
    # token/token auth on :443 remotes; SSH fallback on :22 remotes).
    assert values == [
        "https://bitbucket.org/",
        "https://bitbucket.org:443/",
        "git@bitbucket.org:",
        "ssh://git@bitbucket.org/",
        "ssh://git@bitbucket.org:22/",
    ]
    # No token, no shell metacharacters (plain URLs only).
    assert all(_TOKEN not in value for value in values)
    assert all("$" not in value and "!" not in value for value in values)


def _apply_instead_of(url: str, entries: tuple[tuple[str, str], ...]) -> str:
    """Mimic git's ``insteadOf`` longest-literal-prefix rewrite for ``url``.

    git rewrites a URL by replacing the longest configured ``insteadOf`` *source*
    (the config *value*) that is a literal prefix of the URL with the URL embedded
    in the config *key* (``url.<rewritten>.insteadOf``). This lets the test assert
    the end-to-end effect of the config without invoking git.
    """
    target = _AGENT_INSTEADOF_KEY[len("url.") : -len(".insteadOf")]
    best_source = ""
    for _key, source in entries:
        if url.startswith(source) and len(source) > len(best_source):
            best_source = source
    if not best_source:
        return url
    return target + url[len(best_source) :]


@pytest.mark.unit
def test_bitbucket_agent_rewrite_covers_explicit_default_port_https() -> None:
    # Regression: an explicit-:443 HTTPS remote must be rewritten to the
    # sentinel-username form so git reads the username from the URL and only asks
    # the askpass for the *password*. Without the :443 insteadOf source git would
    # leave the URL unrewritten, prompt for a username, and the host-gated askpass
    # would answer it with the token — authenticating as token/token and failing
    # private clones/fetches.
    entries = bitbucket_agent_git_config_entries()
    rewritten = _apply_instead_of("https://bitbucket.org:443/ws/repo.git", entries)
    assert rewritten == "https://x-bitbucket-api-token-auth@bitbucket.org/ws/repo.git"
    # The rewritten URL carries the sentinel prefix, so it never re-matches any
    # source rule (no rewrite loop).
    assert _apply_instead_of(rewritten, entries) == rewritten


@pytest.mark.unit
def test_bitbucket_agent_rewrite_covers_explicit_default_port_ssh() -> None:
    # Regression: an SSH remote with the explicit default port
    # ``ssh://git@bitbucket.org:22/…`` must be rewritten to the sentinel-username
    # HTTPS form. RepoRef.from_url classifies it as bitbucket (it parses the host
    # without the port), but insteadOf matches sources as literal prefixes, so the
    # bare ``ssh://git@bitbucket.org/`` source does not cover the ``:22`` shape.
    # Without the ``:22`` source git would leave the URL as SSH and the token-only
    # agent container would fail private fetches/pushes.
    entries = bitbucket_agent_git_config_entries()
    rewritten = _apply_instead_of("ssh://git@bitbucket.org:22/ws/repo.git", entries)
    assert rewritten == "https://x-bitbucket-api-token-auth@bitbucket.org/ws/repo.git"
    # The rewritten URL carries the sentinel prefix, so it never re-matches any
    # source rule (no rewrite loop).
    assert _apply_instead_of(rewritten, entries) == rewritten


@pytest.mark.unit
def test_apply_bitbucket_agent_git_auth_sets_askpass_and_terminal_prompt() -> None:
    env: dict[str, str] = {}

    apply_bitbucket_agent_git_auth(env, askpass_path=_ASKPASS_PATH)

    assert env["GIT_ASKPASS"] == _ASKPASS_PATH
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    count = int(env["GIT_CONFIG_COUNT"])
    rendered = {(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)}
    assert (_AGENT_INSTEADOF_KEY, "https://bitbucket.org/") in rendered
    assert (_AGENT_INSTEADOF_KEY, "https://bitbucket.org:443/") in rendered
    assert (_AGENT_INSTEADOF_KEY, "git@bitbucket.org:") in rendered
    assert (_AGENT_INSTEADOF_KEY, "ssh://git@bitbucket.org/") in rendered
    assert (_AGENT_INSTEADOF_KEY, "ssh://git@bitbucket.org:22/") in rendered
    # No token value anywhere in the agent git env.
    assert all(_TOKEN not in value for value in env.values())


@pytest.mark.unit
def test_apply_bitbucket_agent_git_auth_accumulates_onto_existing_git_config() -> None:
    # Pre-seed two entries (e.g. as another block would) at indices 0 and 1.
    env: dict[str, str] = {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
    }

    apply_bitbucket_agent_git_auth(env, askpass_path=_ASKPASS_PATH)

    # Exact final indexed layout: the five insteadOf rewrites land at 2, 3, 4, 5, 6.
    assert env["GIT_CONFIG_COUNT"] == "7"
    assert env["GIT_CONFIG_KEY_2"] == _AGENT_INSTEADOF_KEY
    assert env["GIT_CONFIG_VALUE_2"] == "https://bitbucket.org/"
    assert env["GIT_CONFIG_KEY_3"] == _AGENT_INSTEADOF_KEY
    assert env["GIT_CONFIG_VALUE_3"] == "https://bitbucket.org:443/"
    assert env["GIT_CONFIG_KEY_4"] == _AGENT_INSTEADOF_KEY
    assert env["GIT_CONFIG_VALUE_4"] == "git@bitbucket.org:"
    assert env["GIT_CONFIG_KEY_5"] == _AGENT_INSTEADOF_KEY
    assert env["GIT_CONFIG_VALUE_5"] == "ssh://git@bitbucket.org/"
    assert env["GIT_CONFIG_KEY_6"] == _AGENT_INSTEADOF_KEY
    assert env["GIT_CONFIG_VALUE_6"] == "ssh://git@bitbucket.org:22/"
    # Pre-existing entries are preserved untouched.
    assert env["GIT_CONFIG_KEY_0"] == "safe.directory"
    assert env["GIT_CONFIG_VALUE_0"] == "*"
    assert env["GIT_CONFIG_KEY_1"] == "credential.helper"


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


@pytest.mark.unit
@pytest.mark.parametrize(
    "repo_url",
    [
        "https://alice@bitbucket.org/ws/repo.git",
        "https://alice:secret@bitbucket.org/ws/repo.git",
        "https://alice@bitbucket.org:443/ws/repo.git",
    ],
)
def test_verify_bitbucket_git_auth_rejects_non_sentinel_userinfo(repo_url: str) -> None:
    # A username embedded in the HTTPS URL shadows the fixed sentinel username:
    # git uses the embedded name and only asks the askpass/helper for the
    # password, so the API token authenticates under the wrong username and
    # private fetch/push fails opaquely. The preflight must reject it instead.
    with pytest.raises(GitAuthNotConfiguredError) as raised:
        verify_bitbucket_git_auth(
            repo_url,
            {"BITBUCKET_API_TOKEN": _TOKEN, "BITBUCKET_EMAIL": _EMAIL},
        )

    assert raised.value.reason_code == BITBUCKET_GIT_AUTH_NOT_CONFIGURED
    message = str(raised.value)
    # Names the required sentinel username but never echoes the embedded
    # userinfo (which may carry a secret password).
    assert "x-bitbucket-api-token-auth" in message
    assert "alice" not in message
    assert "secret" not in message


@pytest.mark.unit
def test_verify_bitbucket_git_auth_rejects_embedded_password_under_sentinel() -> None:
    # Even with the correct sentinel username, an embedded password must be
    # rejected: git would clone with the URL verbatim, using/storing the embedded
    # password as the remote instead of AWF's lease-provided token.
    with pytest.raises(GitAuthNotConfiguredError) as raised:
        verify_bitbucket_git_auth(
            "https://x-bitbucket-api-token-auth:stale-secret@bitbucket.org/ws/repo.git",
            {"BITBUCKET_API_TOKEN": _TOKEN, "BITBUCKET_EMAIL": _EMAIL},
        )

    assert raised.value.reason_code == BITBUCKET_GIT_AUTH_NOT_CONFIGURED
    # Never echoes the embedded secret.
    assert "stale-secret" not in str(raised.value)


@pytest.mark.unit
def test_verify_bitbucket_git_auth_allows_sentinel_userinfo() -> None:
    # The sentinel username already in the URL is exactly what token auth needs,
    # so it must NOT be rejected.
    verify_bitbucket_git_auth(
        "https://x-bitbucket-api-token-auth@bitbucket.org/ws/repo.git",
        {"BITBUCKET_API_TOKEN": _TOKEN, "BITBUCKET_EMAIL": _EMAIL},
    )
