"""Forge-neutral git-over-HTTPS authentication wiring.

This keeps forge-specific git credential plumbing in one place so the generic
``node.git_manager`` and ``service.worker`` stay forge-agnostic. Today it covers
**BitBucket Cloud** (``bitbucket.org``); the GitHub credential helper lives in
``service.worker`` and is intentionally left untouched.

Security contract (mirrors how the GitHub token reaches git):

- The Atlassian API token is **never** embedded in a clone/push URL, in
  git-config text, or in a log/error message.
- The git username is the fixed, account-agnostic sentinel
  ``x-bitbucket-api-token-auth`` (BitBucket rejects the email as the git
  username with 401 — only the REST API accepts ``email:token``; see #467).
- The host-scoped credential helper references the token env var *name* only
  (``$BITBUCKET_API_TOKEN``). git invokes the helper and reads the secret value
  from the process environment on demand, exactly like the GitHub
  ``!gh auth git-credential`` helper.
- The helper is scoped to ``https://bitbucket.org`` so it can only ever fire for
  bitbucket.org URLs — GitHub git auth is byte-for-byte unchanged.

Two intentionally divergent mechanisms (#465/#466)
--------------------------------------------------

There are **two** BitBucket git-auth paths because they cross different layers:

- **Worker** (``apply_bitbucket_git_auth`` + ``bitbucket_git_config_entries``):
  a host-scoped shell **credential helper** wired straight into the worker's git
  process environment. No compose layer sits between the helper string and git,
  so embedding a token-referencing ``$BITBUCKET_API_TOKEN`` shell snippet is
  safe. This path is already shipped (#461/#464/#467) and is left byte-for-byte
  unchanged.
- **Agent** (``apply_bitbucket_agent_git_auth`` + ``bitbucket_agent_git_config_entries``
  + ``bitbucket_askpass_script``): an **askpass file + URL-username rewrite**. The
  agent env crosses four layers (Jinja2 template → YAML → docker-compose
  ``${VAR}`` interpolation → container shell); rendering a token-referencing
  shell helper through that stack is fragile and risks leaking the token into the
  rendered ``compose.yml``. Instead a *static* askpass script (no token, only the
  env-var *name*) reads ``$BITBUCKET_API_TOKEN`` from the container runtime env at
  git call time, and ``insteadOf`` rewrites carry the non-secret sentinel username
  in the URL. The token VALUE never appears in any rendered string — only at
  runtime in the container env via the existing ``${BITBUCKET_API_TOKEN}`` lease.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import urlsplit

from awf.common.github_client import RepoRef

BITBUCKET_GIT_AUTH_NOT_CONFIGURED = "BITBUCKET_GIT_AUTH_NOT_CONFIGURED"
BITBUCKET_GIT_HOST = "bitbucket.org"

_BITBUCKET_TOKEN_ENV = "BITBUCKET_API_TOKEN"
_BITBUCKET_EMAIL_ENV = "BITBUCKET_EMAIL"

# Host-scoped credential helper. The git username for an Atlassian API token over
# HTTPS is the fixed, account-agnostic sentinel ``x-bitbucket-api-token-auth`` —
# **not** the account email. BitBucket's git endpoint rejects the email with 401
# (the REST API accepts ``email:token`` Basic auth, but git over HTTPS does not;
# see issue #467). Only ``$BITBUCKET_API_TOKEN`` is expanded — by the shell git
# spawns for the helper at call time — so the literal token never appears in this
# string, in git-config, or in any log.
_BITBUCKET_GIT_USERNAME = "x-bitbucket-api-token-auth"
_BITBUCKET_CREDENTIAL_HELPER = (
    '!f() { test "$1" = get && '
    f"printf 'username={_BITBUCKET_GIT_USERNAME}\\npassword=%s\\n' "
    '"$BITBUCKET_API_TOKEN"; }; f'
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


def _is_ssh_transport(repo_url: str) -> bool:
    """Return whether ``repo_url`` uses git's SSH transport (not HTTPS).

    SSH clones (``git@bitbucket.org:…`` scp-like form or ``ssh://git@bitbucket.org/…``)
    authenticate with SSH keys, not the HTTPS credential helper, so the BitBucket
    HTTPS-credential preflight must not apply to them.
    """
    value = repo_url.strip()
    if value.startswith("git@"):
        return True
    return urlsplit(value).scheme.lower() == "ssh"


def bitbucket_git_config_entries() -> tuple[tuple[str, str], ...]:
    """Return host-scoped git-config entries wiring the BitBucket credential helper.

    The first (empty) helper value clears any inherited helper, then the real
    helper is appended (standard git multi-value semantics). ``useHttpPath``
    keeps the credential scoped correctly for bitbucket.org paths.

    The ``insteadOf`` rewrites mirror the GitHub
    ``url.https://github.com/.insteadOf = git@github.com:`` entry so an SSH-form
    bitbucket remote is rewritten to HTTPS and actually authenticates with the
    configured token instead of silently falling back to SSH and ignoring
    ``BITBUCKET_API_TOKEN`` / ``BITBUCKET_EMAIL``. Both SSH URL shapes that
    ``RepoRef.from_url`` accepts are covered: the scp-like
    ``git@bitbucket.org:ws/repo.git`` form and the
    ``ssh://git@bitbucket.org/ws/repo.git`` form (``insteadOf`` is multi-valued,
    so both rewrites apply).
    """
    return (
        ("credential.https://bitbucket.org.helper", ""),
        ("credential.https://bitbucket.org.helper", _BITBUCKET_CREDENTIAL_HELPER),
        ("credential.https://bitbucket.org.useHttpPath", "true"),
        ("url.https://bitbucket.org/.insteadOf", "git@bitbucket.org:"),
        ("url.https://bitbucket.org/.insteadOf", "ssh://git@bitbucket.org/"),
    )


# Agent-side rewrite target carrying the non-secret sentinel username. Any
# bitbucket.org remote (HTTPS or either SSH-form shape) is rewritten to this base
# URL so git authenticates over HTTPS with ``GIT_ASKPASS`` supplying the token as
# the password. The sentinel username is **not** secret, so embedding it in the
# URL is safe; the rewrite target itself carries the sentinel prefix, so it never
# re-matches the bare ``https://bitbucket.org/`` source rule (no rewrite loop).
_BITBUCKET_AGENT_GIT_URL = f"https://{_BITBUCKET_GIT_USERNAME}@bitbucket.org/"
_BITBUCKET_AGENT_INSTEADOF_KEY = f"url.{_BITBUCKET_AGENT_GIT_URL}.insteadOf"

# A valid POSIX shell environment-variable name: a leading letter/underscore
# followed by letters, digits, or underscores. ``bitbucket_askpass_script``
# interpolates ``token_env_var`` straight into a shell script body, so anything
# outside this character set could smuggle shell metacharacters into the script.
_ASKPASS_TOKEN_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def bitbucket_askpass_script(token_env_var: str) -> str:
    """Return the **static** ``GIT_ASKPASS`` script body for agent-side bitbucket auth.

    The returned content carries **no token value** — only the env-var *name*
    ``token_env_var``. git invokes the script and reads the secret from the
    container runtime environment (``$BITBUCKET_API_TOKEN``) on demand. Because
    the sentinel username is supplied via the ``insteadOf`` URL rewrite, git only
    ever asks the askpass for the *password*, so the script just prints the token.

    ``GIT_ASKPASS`` is **process-wide**: git invokes it for *every* credential
    prompt, not just bitbucket.org ones, so an unconditional script would hand the
    BitBucket token to any other HTTPS remote that happens to prompt for
    credentials (e.g. a later ``git fetch`` against a different host that 401s).
    The script therefore gates on both the prompt **scheme** and **host** — git's
    askpass prompt embeds the target URL (``Password for 'https://…@bitbucket.org': ``)
    — and emits the token only when that URL is ``https://`` *and* its host is
    exactly ``bitbucket.org``. The scheme gate is load-bearing: ``RepoRef.from_url``
    also accepts ``http://bitbucket.org/…`` remotes, so without it a plaintext-HTTP
    bitbucket prompt would still match the host check and leak
    ``BITBUCKET_API_TOKEN`` over the wire, violating the git-over-HTTPS/no-token-leak
    contract. Requiring ``https://`` rejects such HTTP remotes before any token is
    emitted. Rather than
    substring-matching ``bitbucket.org`` anywhere in the prompt (which a foreign
    remote could satisfy by embedding the string in its user-info or path — e.g.
    ``https://x@github.com/foo@bitbucket.org/repo`` or
    ``https://evil.com/https://bitbucket.org/repo`` — leaking the token to the
    other host), the script **parses the host out of the URL**: it extracts the
    text between git's single quotes, takes the authority after ``://``, drops the
    user-info before the first ``@`` and any ``:port`` / ``/path`` tail, then
    compares the remaining host verbatim. A look-alike host such as
    ``bitbucket.org.evil.com`` therefore never matches. For any other host (or a
    prompt without a URL) the script emits nothing and git falls back as if no
    askpass answered.

    The script is materialized to a per-workspace file and mounted read-only +
    executable into the agent (see ``node.secret_mounts``).

    ``token_env_var`` is interpolated verbatim into the shell script body, so it
    must be a valid POSIX environment-variable name; anything else (shell
    metacharacters, whitespace, ``$``/quotes) is rejected with ``ValueError``
    rather than risking arbitrary code in the generated script. The production
    call site always passes the validated constant ``BITBUCKET_API_TOKEN``; this
    guard keeps the public function safe for any caller.
    """
    if not _ASKPASS_TOKEN_ENV_RE.match(token_env_var):
        raise ValueError(f"invalid token env-var name: {token_env_var!r}")
    host = BITBUCKET_GIT_HOST
    # POSIX-sh host parse of git's ``Password for 'URL': `` prompt. ``\\'`` is a
    # literal single quote (git wraps the URL in '…'); the expansions peel the
    # URL, then its authority, user-info, and ``:port`` to recover git's real
    # target host. The ``case`` requires the ``https://`` scheme: an ``http://``
    # (or any non-HTTPS) prompt exits before any token is emitted, so a plaintext
    # bitbucket remote never receives the token. Substring matching is unsafe —
    # see the docstring.
    return (
        "#!/bin/sh\n"
        "url=${1#*\\'}\n"
        "url=${url%%\\'*}\n"
        'case "$url" in\n'
        "  https://*) ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
        "authority=${url#*://}\n"
        "authority=${authority%%/*}\n"
        "host=${authority#*@}\n"
        "host=${host%%:*}\n"
        f'if [ "$host" = "{host}" ]; then\n'
        f"  printf '%s' \"${token_env_var}\"\n"
        "fi\n"
    )


def bitbucket_agent_git_config_entries() -> tuple[tuple[str, str], ...]:
    """Return the agent-side ``insteadOf`` rewrites for bitbucket.org remotes.

    Every bitbucket.org remote shape that ``RepoRef.from_url`` accepts — the
    HTTPS form, the scp-like ``git@bitbucket.org:ws/repo.git`` form, and the
    ``ssh://git@bitbucket.org/ws/repo.git`` form — is rewritten to the
    sentinel-username HTTPS base URL (``insteadOf`` is multi-valued). The
    explicit-default-port shapes ``https://bitbucket.org:443/…`` and
    ``ssh://git@bitbucket.org:22/…`` are rewritten too: ``insteadOf`` matches the
    source as a literal prefix, so the bare ``https://bitbucket.org/`` /
    ``ssh://git@bitbucket.org/`` rules do **not** cover the explicit-port shapes,
    and ``RepoRef.from_url`` still classifies them as bitbucket (it parses the
    host without the port). Without these entries git would leave such a URL
    unrewritten and either prompt for a *username* on the HTTPS ``:443`` shape
    (the host-gated askpass answers it with the token, authenticating as
    ``token/token``) or fall back to SSH on the ``:22`` shape — both failing
    private clones/fetches in token-only agent containers. The entries
    contain **no token** and **no shell syntax** — plain URLs only — so they are
    safe to render through the compose ``GIT_CONFIG_*`` env layer. The token is
    supplied separately at runtime by the mounted askpass script.
    """
    return (
        (_BITBUCKET_AGENT_INSTEADOF_KEY, "https://bitbucket.org/"),
        (_BITBUCKET_AGENT_INSTEADOF_KEY, "https://bitbucket.org:443/"),
        (_BITBUCKET_AGENT_INSTEADOF_KEY, "git@bitbucket.org:"),
        (_BITBUCKET_AGENT_INSTEADOF_KEY, "ssh://git@bitbucket.org/"),
        (_BITBUCKET_AGENT_INSTEADOF_KEY, "ssh://git@bitbucket.org:22/"),
    )


def apply_bitbucket_agent_git_auth(env: dict[str, str], *, askpass_path: str) -> None:
    """Wire agent-side bitbucket.org HTTPS auth into ``env`` in place.

    Sets ``GIT_ASKPASS`` to the mounted askpass path, ``GIT_TERMINAL_PROMPT=0``
    (fail fast instead of hanging on a TTY prompt), and **accumulates** the
    ``insteadOf`` ``GIT_CONFIG_*`` rewrites onto any existing ``GIT_CONFIG_COUNT``
    so it composes with other blocks. The token name flows only into the askpass
    *script content* (produced by :func:`bitbucket_askpass_script`), never into
    ``env`` — so no secret value is ever placed in the agent environment.
    """
    env["GIT_ASKPASS"] = askpass_path
    env["GIT_TERMINAL_PROMPT"] = "0"
    add_git_config_entries(env, bitbucket_agent_git_config_entries())


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

    No-op for non-bitbucket repos (GitHub git auth is unaffected) and for SSH
    bitbucket URLs (``git@bitbucket.org:…`` / ``ssh://git@bitbucket.org/…``), which
    authenticate with SSH keys rather than the HTTPS credential helper. For an
    HTTPS bitbucket.org repo missing ``BITBUCKET_API_TOKEN`` and/or ``BITBUCKET_EMAIL``,
    raises :class:`GitAuthNotConfiguredError` with
    ``reason_code == BITBUCKET_GIT_AUTH_NOT_CONFIGURED`` and a message that names
    only the missing env var(s) — never any value — turning an otherwise opaque
    clone failure (or TTY hang) into a fast, diagnosable error.

    Also rejects an HTTPS bitbucket.org URL that embeds a **non-sentinel**
    username (e.g. ``https://alice@bitbucket.org/ws/repo.git``). Atlassian
    API-token auth over HTTPS requires the fixed sentinel username
    ``x-bitbucket-api-token-auth``; an embedded username shadows it (git uses the
    URL's username verbatim and the ``insteadOf`` rewrites — which match a bare
    ``https://bitbucket.org/`` prefix — never fire), so the credential helper /
    askpass token would authenticate under the wrong username and private
    clone/fetch/push fails opaquely. An **embedded password** is also rejected
    even under the sentinel username (``https://x-bitbucket-api-token-auth:<pw>@
    bitbucket.org/…``): git would clone with that URL verbatim, using/storing the
    embedded password as the remote instead of AWF's lease-provided token —
    breaking configured-token clones if it is stale and defeating the
    no-secrets-in-URLs contract if it is real. The bare sentinel username (no
    password) is allowed (it is exactly what token auth needs). The message names
    the required sentinel but never echoes the embedded userinfo, which may carry
    a secret password.
    """
    if not is_bitbucket_repo(repo_url) or _is_ssh_transport(repo_url):
        return
    missing = [
        name
        for name in (_BITBUCKET_TOKEN_ENV, _BITBUCKET_EMAIL_ENV)
        if not (env.get(name) or "").strip()
    ]
    if missing:
        raise GitAuthNotConfiguredError(
            reason_code=BITBUCKET_GIT_AUTH_NOT_CONFIGURED,
            message=(
                "BitBucket git authentication is not configured for a bitbucket.org "
                f"repository: missing {', '.join(missing)}. Set these in the AWF "
                "service environment so git can authenticate over HTTPS."
            ),
        )
    parsed = urlsplit(repo_url.strip())
    embedded_user = parsed.username
    # Reject any embedded password outright (even under the sentinel username):
    # git clones with the URL verbatim, so the password would be used/stored as
    # the remote instead of AWF's lease-provided token — breaking configured-token
    # clones if stale and defeating the no-secrets-in-URLs contract if real.
    # A non-sentinel username is also rejected (it shadows the fixed sentinel).
    if parsed.password is not None or (
        embedded_user is not None and embedded_user != _BITBUCKET_GIT_USERNAME
    ):
        raise GitAuthNotConfiguredError(
            reason_code=BITBUCKET_GIT_AUTH_NOT_CONFIGURED,
            message=(
                "BitBucket git authentication cannot use the credentials embedded "
                "in the bitbucket.org repository URL: Atlassian API-token auth over "
                f"HTTPS requires the fixed username {_BITBUCKET_GIT_USERNAME!r}. "
                "Remove the userinfo from the repo URL (use "
                "https://bitbucket.org/<workspace>/<repo>.git) so AWF can supply the "
                "API token under the correct username."
            ),
        )
