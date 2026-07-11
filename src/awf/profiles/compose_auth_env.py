"""Auth and profile-env constants shared by compose profile helpers."""

from __future__ import annotations

import re

AGENT_AUTH_ENV_VARS = (
    # Codex/OpenAI static-token fallback auth. Prefer isolated ~/.codex copies
    # when present; these keep local shells compatible without writing values.
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODEX_API_KEY",
    "CODEX_AUTH_TOKEN",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_ORGANIZATION",
    "OPENAI_PROJECT",
    "OPENAI_PROJECT_ID",
    # Claude Code portable/API-key auth. Host claude.ai OAuth can live in
    # macOS Keychain, which is not available inside a Linux container.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    # Cursor CLI headless auth.
    "CURSOR_API_KEY",
    # Gemini CLI headless auth.
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_AUTH_MECHANISM",
    "GOOGLE_API_KEY",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_GENAI_USE_GCA",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_ACCESS_TOKEN",
    # OpenCode via Ollama/Ollama Cloud.
    "AWF_OPENCODE_OLLAMA_BASE_URL",
    "OLLAMA_HOST",
    "OLLAMA_API_KEY",
    # xAI Grok Build headless auth.
    "XAI_API_KEY",
    # OpenCode shell-tool runtime tuning. This is not auth, but it must follow
    # the same service -> workspace placeholder path to affect agent containers.
    "OPENCODE_EXPERIMENTAL_BASH_DEFAULT_TIMEOUT_MS",
)

# Secret-bearing subset of :data:`AGENT_AUTH_ENV_VARS` — names whose literal
# value is a credential (API key / token / OAuth token / service-account
# credentials / access token). A profile-owned literal for one of these keys is
# a concrete secret and must never be carried in ``profile_env``: the hosted
# executor resolves it out-of-band (the name is kept out of
# ``env_passthrough_names`` by ``_profile_owned_auth_keys``), so ``profile_env``
# must not duplicate it (PR #751 thread PRRT_kwDOSJAM6s6PaYta). Non-secret
# profile config in :data:`AGENT_AUTH_ENV_VARS` — endpoints (``OLLAMA_HOST`` /
# ``*_BASE_URL``), org/project/region, model names, backend toggles — stays
# carried, matching the ``AgentRuntimeExecRequest`` contract.
_AGENT_AUTH_SECRET_ENV_VARS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_API_TOKEN",
        "CODEX_API_KEY",
        "CODEX_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CURSOR_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_ACCESS_TOKEN",
        "OLLAMA_API_KEY",
        "XAI_API_KEY",
        # Claude Code Bedrock backend credentials (used when
        # ``CLAUDE_CODE_USE_BEDROCK=1``). The toggle is in
        # ``AGENT_AUTH_ENV_VARS`` but the credentials it requires are not; they
        # are surfaced as a static backend supplement in
        # ``ClaudeCodeAdapter.hosted_env_passthrough_names``. When a profile
        # declares one of these as a literal agent env value, the hosted
        # passthrough filter already treats the compose-declared name as
        # profile-owned (excluded from ``env_passthrough_names``), so the hosted
        # executor resolves it out-of-band. ``literal_profile_env_from_compose``
        # must also redact these from ``profile_env`` or the raw secret literal
        # would be appended to ``AgentRuntimeExecRequest.profile_env``, violating
        # the secret-free hosted contract and exposing AWS credentials to the
        # hosted executor/request object (PR #751 thread PRRT_kwDOSJAM6s6PiiaQ).
        # Non-secret backend config (``AWS_REGION`` / ``AWS_DEFAULT_REGION`` /
        # ``AWS_PROFILE`` / ``ANTHROPIC_VERTEX_PROJECT_ID`` /
        # ``CLOUD_ML_REGION``) is NOT redacted and stays carried, matching the
        # ``AgentRuntimeExecRequest`` contract. ``AWS_ACCESS_KEY_ID`` is handled
        # separately as a name-only credential identifier: it is not secret
        # material by itself, but hosted jobs still must not receive it through
        # direct ``profile_env`` values.
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_BEARER_TOKEN_BEDROCK",
        # GitHub CLI token literals. The aliases ``GH_TOKEN`` / ``GITHUB_TOKEN``
        # and the documented AWF source ``AWF_GITHUB_TOKEN`` are concrete
        # credentials; a profile-owned literal for any of these is a secret that
        # must never be carried in ``profile_env``. The hosted executor resolves
        # GitHub auth out-of-band via ``env_passthrough_names`` (see
        # ``hosted_github_token_passthrough_names`` / the GitHub token aliases
        # surfaced by ``agent_environment_with_github_token``), so
        # ``profile_env`` must not duplicate the raw token literal. Without this
        # redaction, a profile/compose agent env owning a literal GitHub token
        # had the raw token appended to ``AgentRuntimeExecRequest.profile_env``,
        # violating the secret-free hosted contract (PR #751 thread
        # PRRT_kwDOSJAM6s6PiwKv). The local Compose path keeps the value inside
        # the already-started container, so redaction only affects the hosted
        # transport path. Non-secret profile config (e.g. ``OLLAMA_HOST`` /
        # ``APP_BASE_URL``) is unaffected and still carried.
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "AWF_GITHUB_TOKEN",
    }
)

_HOSTED_FILE_BACKED_ENV_ONLY_UNSUPPORTED_NAMES = frozenset(
    {
        # ADC is a filesystem path whose local Compose contract includes an auth
        # bind mount. Hosted requests currently carry env values only, so
        # profile passthrough names and aliases must not re-add it after
        # adapters omit it.
        "GOOGLE_APPLICATION_CREDENTIALS",
    }
)

# Credential identifiers that are not secrets by themselves but still must not
# be transported as direct hosted job env values. Hosted executors should resolve
# them from name-only passthrough alongside the corresponding secret material.
_HOSTED_NAME_ONLY_CREDENTIAL_IDENTIFIER_ENV_VARS = frozenset({"AWS_ACCESS_KEY_ID"})

_NON_SECRET_SECRET_LIKE_PROFILE_ENV_NAMES = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "GEMINI_API_KEY_AUTH_MECHANISM",
    }
)

_SECRET_LIKE_PROFILE_ENV_EXACT_NAMES = frozenset(
    {
        # Standard database client password env aliases do not split into the
        # generic PASSWORD/PASSWD tokens below (`PGPASSWORD` is one token and
        # `MYSQL_PWD` uses `PWD`), but their literal values are credentials.
        # Standard auth-bearing client/env aliases likewise use ``AUTH`` as an
        # auth-mode noun in some non-secret config, so keep them exact-name
        # redactions instead of making ``AUTH`` a generic secret token.
        "AUTHORIZATION",
        "DOCKER_AUTH_CONFIG",
        "HTTP_AUTHORIZATION",
        "JWT",
        "MYSQL_PWD",
        "NPM_CONFIG__AUTH",
        "PGPASSWORD",
        "PROXY_AUTHORIZATION",
        "REDISCLI_AUTH",
    }
)

_SECRET_LIKE_PROFILE_ENV_NAME_TOKENS = frozenset(
    {
        "CREDENTIAL",
        "CREDENTIALS",
        "PASSPHRASE",
        "PASSWD",
        "PASSWORD",
        "PAT",
        "SECRET",
        "TOKEN",
    }
)
_SECRET_LIKE_PROFILE_ENV_NAME_CONCATENATED_TOKENS = frozenset(
    {
        "ACCESSKEY",
        "ACCESSTOKEN",
        "APIKEY",
        "AUTHTOKEN",
        "CLIENTSECRET",
        "DBPASSWORD",
        "JWTSECRET",
        "PRIVATEKEY",
        "POSTGRESPASSWORD",
        "REFRESHTOKEN",
        "SECRETKEY",
        "SESSIONTOKEN",
    }
)

_SECRET_LIKE_PROFILE_ENV_NAME_ABBREVIATION_TOKENS = frozenset({"PASS", "PWD"})

_SECRET_LIKE_PROFILE_ENV_NAME_TOKEN_PAIRS = frozenset(
    {
        ("ACCESS", "KEY"),
        ("API", "KEY"),
        ("PRIVATE", "KEY"),
    }
)

_PUBLIC_PROFILE_ENV_NAME_KEY_QUALIFIERS = frozenset({"PUBLIC", "PUBLISHABLE", "SITE"})
_PUBLIC_PROFILE_ENV_NAME_PREFIX_TOKENS = frozenset({"GATSBY", "VITE"})
_PUBLIC_PROFILE_ENV_NAME_PREFIX_TOKEN_SEQUENCES = (("REACT", "APP"),)
_NON_SECRET_PROFILE_ENV_NAME_ENDPOINT_SUFFIX_TOKENS = frozenset({"ENDPOINT", "URI", "URL"})

_AUTH_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?:^\s*(?:basic|bearer)\s+\S+|(?<![A-Za-z0-9_-])[\"']?_?(?:(?:proxy[-_]?)?authorization|set[-_]?cookie|cookie|credentials?|jwt|(?:[A-Za-z0-9]+[-_])*pat|(?:(?:x[-_]?)|(?:[A-Za-z0-9]+[-_])+)?api[-_]?key|(?:[A-Za-z0-9]+[-_])*subscription[-_]?key|shared[-_]?access[-_]?(?:key|signature)|(?:[A-Za-z0-9]+[-_])*encryption[-_]?key|(?:account|access|private|secret)[-_]?key|(?:client[-_]?)?key[-_]?data|private[-_]?key[-_]?data|(?:aws[-_]?)?secret[-_]?access[-_]?key|client[-_]?secret|(?:[A-Za-z0-9]+[-_])*(?:client[-_]?|signing[-_]?)?secret|(?-i:[A-Za-z0-9]+SigningSecret)|(?:[A-Za-z0-9]+[-_]?)*(?:passphrase|password|passwd|pwd)|(?:x[-_]?)?(?:(?:[A-Za-z0-9]+[-_])*(?:access|api|auth|bearer|csrf|id|identity|job|oauth|private|refresh|session)[-_]?)?token|(?:x[-_]?)?(?:amz[-_]?)?security[-_]?token)[\"']?\s*[:=]\s*[\"']?\S+|[\"']auth[\"']\s*:\s*[\"'][^\"']+|[\"']private[_-]key[\"']\s*:\s*[\"'][^\"']+|-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----)",
    re.IGNORECASE,
)
_CAMELCASE_API_KEY_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?[A-Za-z0-9]+ApiKey[\"']?\s*[:=]\s*[\"']?\S+"
)
_CAMELCASE_ENCRYPTION_KEY_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?[A-Za-z0-9]+EncryptionKey[\"']?\s*[:=]\s*[\"']?\S+"
)
_PREFIXED_SECRET_KEY_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?"
    r"(?:[A-Za-z0-9]+[-_])+secret[-_]?key"
    r"[\"']?\s*[:=]\s*[\"']?\S+",
    re.IGNORECASE,
)
_CAMELCASE_SECRET_KEY_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?[A-Za-z0-9]+SecretKey[\"']?\s*[:=]\s*[\"']?\S+"
)
_CAMELCASE_SECRET_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?[A-Za-z0-9]+(?:Client)?Secret[\"']?\s*[:=]\s*[\"']?\S+"
)
_PREFIXED_COOKIE_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?"
    r"(?:[A-Za-z0-9]+[-_])+(?:set[-_]?)?cookie"
    r"[\"']?\s*[:=]\s*[\"']?\S+",
    re.IGNORECASE,
)
_CAMELCASE_COOKIE_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?[A-Za-z0-9]+(?:Set)?Cookie[\"']?\s*[:=]\s*[\"']?\S+"
)
_PREFIXED_PRIVATE_KEY_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])[\"']?"
    r"(?:(?:[A-Za-z0-9]+[-_])+private[-_]?key(?:[-_]?data)?|[A-Za-z0-9]+PrivateKey(?:Data)?)"
    r"[\"']?\s*[:=]\s*[\"']?\S+"
)
_NPMRC_AUTH_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?:^|[\r\n])\s*(?://[^\s=]+:\s*)?_?auth\s*=\s*\S+",
    re.IGNORECASE,
)
_NETRC_AUTH_CREDENTIAL_LIKE_VALUE_PATTERN = re.compile(
    r"(?:^|[\r\n])\s*(?:machine[^\S\r\n]+\S+|default)(?:[^\S\r\n]+\S+)*"
    r"[^\S\r\n]+password[^\S\r\n]+\S+",
    re.IGNORECASE,
)
_URL_SECRET_CREDENTIAL_FIELD_NAMES = frozenset(
    {"JWT", "PASSWORD", "PASSWD", "SECRET", "SIGNATURE", "TOKEN"}
)
_URL_SECRET_CREDENTIAL_FIELD_EXACT_NAMES = frozenset(
    {
        "ACCESSKEY",
        "APIKEY",
        "AUTH",
        "CLIENTSECRET",
        "JSESSIONID",
        "KEY",
        "PRIVATEKEY",
        "SECRETKEY",
        "SESSION",
        "SESSIONID",
        "SIG",
        "SSLPASSWORD",
    }
)
_URL_SECRET_CREDENTIAL_FIELD_NAME_TOKEN_PAIRS = frozenset(
    {
        ("ACCESS", "KEY"),
        ("API", "KEY"),
        ("CLIENT", "SECRET"),
        ("PRIVATE", "KEY"),
        ("SUBSCRIPTION", "KEY"),
    }
)

# Ollama base-URL env keys in precedence order (highest first) — the OpenCode
# launcher prelude and the worker-side Ollama preflight both resolve the daemon
# from the first non-empty key here. Mirrors ``provider_readiness_helpers``'
# ``_OLLAMA_BASE_URL_ENV_KEYS``; kept local so ``profiles`` does not import the
# ``service`` layer.
_OLLAMA_BASE_URL_ENV_KEYS = ("AWF_OPENCODE_OLLAMA_BASE_URL", "OLLAMA_HOST")

# GitHub CLI token aliases in precedence order (highest first). ``gh`` reads
# ``GH_TOKEN`` before ``GITHUB_TOKEN`` (https://cli.github.com/manual/gh_help_environment),
# so injecting a worker token under an earlier alias shadows a profile-owned later one.
_GITHUB_TOKEN_ALIAS_PRECEDENCE = ("GH_TOKEN", "GITHUB_TOKEN")

_URL_LIKE_SUBSTRING_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>]+")
