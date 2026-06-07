"""Forge-neutral git-over-HTTPS authentication wiring.

This keeps forge-specific git credential plumbing in one place so the generic
``node.git_manager`` and ``service.worker`` stay forge-agnostic. Today it covers
**BitBucket Cloud** (``bitbucket.org``); the GitHub credential helper lives in
``service.worker`` and is intentionally left untouched.

Security contract (mirrors how the GitHub token reaches git):

- The Atlassian API token is **never** embedded in a clone/push URL, in
  git-config text, or in a log/error message.
- The host-scoped credential helper references the env var *names* only
  (``$BITBUCKET_EMAIL`` / ``$BITBUCKET_API_TOKEN``). git invokes the helper and
  reads the secret values from the process environment on demand, exactly like
  the GitHub ``!gh auth git-credential`` helper.
- The helper is scoped to ``https://bitbucket.org`` so it can only ever fire for
  bitbucket.org URLs — GitHub git auth is byte-for-byte unchanged.
"""

from __future__ import annotations

from collections.abc import Mapping

from awf.common.github_client import RepoRef

BITBUCKET_GIT_AUTH_NOT_CONFIGURED = "BITBUCKET_GIT_AUTH_NOT_CONFIGURED"
BITBUCKET_GIT_HOST = "bitbucket.org"

_BITBUCKET_TOKEN_ENV = "BITBUCKET_API_TOKEN"
_BITBUCKET_EMAIL_ENV = "BITBUCKET_EMAIL"

# Host-scoped credential helper. ``$BITBUCKET_EMAIL`` / ``$BITBUCKET_API_TOKEN``
# are expanded by the shell git spawns for the helper at call time — the literal
# token never appears in this string, in git-config, or in any log.
_BITBUCKET_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get && '
    "printf 'username=%s\\npassword=%s\\n' "
    '"$BITBUCKET_EMAIL" "$BITBUCKET_API_TOKEN"; }; f'
)


class GitAuthNotConfiguredError(Exception):
    """Raised when a forge repo needs git credentials that are not configured.

    Carries a ``reason_code`` so the failure flows end-to-end like other git
    faults. The message names only the missing env var, never any secret value.
    """

    def __init__(self, *, reason_code: str, message: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def is_bitbucket_repo(repo_url: str) -> bool:
    """Return whether ``repo_url`` resolves to a bitbucket.org repository.

    Best-effort and never raises: a malformed URL (``RepoRef.from_url`` raises
    ``ValueError``) is treated as not-bitbucket so callers can leave the GitHub
    path untouched.
    """
    try:
        return RepoRef.from_url(repo_url).forge == "bitbucket"
    except ValueError:
        return False


def bitbucket_git_config_entries() -> tuple[tuple[str, str], ...]:
    """Return host-scoped git-config entries wiring the BitBucket credential helper.

    The first (empty) helper value clears any inherited helper, then the real
    helper is appended (standard git multi-value semantics). ``useHttpPath``
    keeps the credential scoped correctly for bitbucket.org paths.
    """
    return (
        ("credential.https://bitbucket.org.helper", ""),
        ("credential.https://bitbucket.org.helper", _BITBUCKET_CREDENTIAL_HELPER),
        ("credential.https://bitbucket.org.useHttpPath", "true"),
    )


def add_git_config_entries(
    env: dict[str, str],
    entries: tuple[tuple[str, str], ...],
) -> None:
    """Accumulate ``entries`` onto the ``GIT_CONFIG_KEY_n/VALUE_n/COUNT`` protocol.

    Appends onto any existing ``GIT_CONFIG_COUNT`` so multiple callers (e.g. the
    GitHub block then the BitBucket block) compose without clobbering each other.
    """
    start_index = int(env.get("GIT_CONFIG_COUNT", "0"))
    for offset, (key, value) in enumerate(entries):
        index = start_index + offset
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env["GIT_CONFIG_COUNT"] = str(start_index + len(entries))


def bitbucket_credentials_present(source_env: Mapping[str, str]) -> bool:
    """Return whether both BitBucket git credentials are present in ``source_env``."""
    token = (source_env.get(_BITBUCKET_TOKEN_ENV) or "").strip()
    email = (source_env.get(_BITBUCKET_EMAIL_ENV) or "").strip()
    return bool(token and email)


def apply_bitbucket_git_auth(env: dict[str, str], source_env: Mapping[str, str]) -> bool:
    """Wire bitbucket.org HTTPS auth into ``env`` in place; return whether applied.

    No-op (returns ``False``, ``env`` untouched) unless both
    ``BITBUCKET_API_TOKEN`` and ``BITBUCKET_EMAIL`` are present in ``source_env``.
    When applied, sets ``GIT_TERMINAL_PROMPT=0`` (fail fast instead of hanging on
    a TTY prompt) and **accumulates** the host-scoped ``GIT_CONFIG_*`` helper
    entries onto any existing ``GIT_CONFIG_COUNT`` so it composes with the GitHub
    block. The secret values are **never** written into ``env`` — they stay solely
    in ``source_env`` (the live process environment), which the credential helper
    reads at git call time.
    """
    if not bitbucket_credentials_present(source_env):
        return False
    env["GIT_TERMINAL_PROMPT"] = "0"
    add_git_config_entries(env, bitbucket_git_config_entries())
    return True


def verify_bitbucket_git_auth(repo_url: str, env: Mapping[str, str]) -> None:
    """Raise a reason-coded error if a bitbucket.org repo lacks git credentials.

    No-op for non-bitbucket repos (GitHub git auth is unaffected). For a
    bitbucket.org repo missing ``BITBUCKET_API_TOKEN`` and/or ``BITBUCKET_EMAIL``,
    raises :class:`GitAuthNotConfiguredError` with
    ``reason_code == BITBUCKET_GIT_AUTH_NOT_CONFIGURED`` and a message that names
    only the missing env var(s) — never any value — turning an otherwise opaque
    clone failure (or TTY hang) into a fast, diagnosable error.
    """
    if not is_bitbucket_repo(repo_url):
        return
    missing = [
        name
        for name in (_BITBUCKET_TOKEN_ENV, _BITBUCKET_EMAIL_ENV)
        if not (env.get(name) or "").strip()
    ]
    if not missing:
        return
    raise GitAuthNotConfiguredError(
        reason_code=BITBUCKET_GIT_AUTH_NOT_CONFIGURED,
        message=(
            "BitBucket git authentication is not configured for a bitbucket.org "
            f"repository: missing {', '.join(missing)}. Set these in the AWF "
            "service environment so git can authenticate over HTTPS."
        ),
    )
