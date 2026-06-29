"""Dependency setup network classification helpers for validation setup."""

from __future__ import annotations

import re
import shlex
from dataclasses import replace
from urllib.parse import urlparse

from awf.common.audit import redact_audit_text
from awf.runtime.validation_command_probe import (
    _ENV_ASSIGNMENT_RE,
    _first_non_assignment_token_index,
)
from awf.runtime.validation_coverage import (
    _command_token_name,
    _execution_command_metadata,
    _format_seconds,
    _read_text_if_present,
)
from awf.runtime.validation_types import (
    ProfileExecutionCommand,
    SetupDependencyNetworkClassification,
    ValidationCommandResult,
)

SETUP_DEPENDENCY_NETWORK_FAILURE = "SETUP_DEPENDENCY_NETWORK_FAILURE"
SETUP_DEPENDENCY_NETWORK_RETRY = "SETUP_DEPENDENCY_NETWORK_RETRY"
SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED = "SETUP_DEPENDENCY_NETWORK_RETRY_EXHAUSTED"
SETUP_DEPENDENCY_NETWORK_METADATA_KEY = "setup_dependency_network"
DB_GENERATED_SETUP_PHASE = "db_generated_setup"
DB_REFRESH_PHASE = "db_refresh"
_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT = 1000
_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_SCAN_LIMIT = 4 * _SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT
_SETUP_DEPENDENCY_NETWORK_COMMAND_LIMIT = 500
_SETUP_DEPENDENCY_NETWORK_FAILURE_BLOCK_RADIUS = 6
_SETUP_DEPENDENCY_NETWORK_DEFAULT_RETRY_BUDGET = 2
_SETUP_DEPENDENCY_NETWORK_DEFAULT_BACKOFF_SECONDS = (1.0, 3.0)


def _shell_tokens(command: str, *, comments: bool = False) -> list[str] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        if not comments:
            lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


_SETUP_DEPENDENCY_COMMAND_VERBS: dict[str, frozenset[str]] = {
    "bun": frozenset({"add", "i", "install", "update", "upgrade"}),
    "bundle": frozenset({"install", "update"}),
    "cargo": frozenset({"fetch", "install", "update"}),
    "composer": frozenset({"install", "require", "update"}),
    "gem": frozenset({"install", "update"}),
    "go": frozenset({"get", "install"}),
    "gradle": frozenset({"dependencies"}),
    "mvn": frozenset({"dependency:go-offline", "dependency:resolve", "dependency:resolve-plugins"}),
    "npm": frozenset({"add", "ci", "i", "install", "up", "update"}),
    "pip": frozenset({"download", "install", "wheel"}),
    "pip3": frozenset({"download", "install", "wheel"}),
    "pnpm": frozenset({"add", "fetch", "i", "install", "up", "update"}),
    "poetry": frozenset({"add", "install", "lock", "sync", "update"}),
    "yarn": frozenset({"add", "install", "up", "upgrade"}),
}
_SETUP_DEPENDENCY_NESTED_COMMAND_VERBS: dict[str, dict[str, frozenset[str]]] = {
    "go": {"mod": frozenset({"download"})},
}
_SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS: dict[str, frozenset[str]] = {
    "yarn": frozenset({"--immutable", "--immutable-cache"}),
}
_SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS = frozenset({"--help", "--version", "-h", "-v"})
_SETUP_DEPENDENCY_OPTION_VALUE_FLAGS = frozenset(
    {
        "--cache",
        "--cache-dir",
        "--cert",
        "--client-cert",
        "--config",
        "--cwd",
        "--dir",
        "--directory",
        "--exists-action",
        "--file",
        "--filter",
        "--globalconfig",
        "--home",
        "--index-url",
        "--jobs",
        "--keyring-provider",
        "--log",
        "--prefix",
        "--project-dir",
        "--project-directory",
        "--python",
        "--proxy",
        "--registry",
        "--repository",
        "--resume-retries",
        "--retries",
        "--root",
        "--settings",
        "--settings-file",
        "--store-dir",
        "--timeout",
        "--trusted-host",
        "--use-deprecated",
        "--use-feature",
        "--userconfig",
        "--workspace",
        "-C",
        "-F",
        "-b",
        "-f",
        "-p",
        "-w",
    }
)
_PYTHON_OPTION_VALUE_FLAGS = frozenset({"-W", "-X"})
_UV_OPTION_VALUE_FLAGS = frozenset(
    {
        "--config-setting",
        "--config-settings-package",
        "--default-index",
        "--directory",
        "--env-file",
        "--exclude-newer",
        "--extra",
        "--extra-index-url",
        "--find-links",
        "--from",
        "--group",
        "--index",
        "--index-strategy",
        "--keyring-provider",
        "--link-mode",
        "--no-binary",
        "--no-build",
        "--no-build-package",
        "--no-extra",
        "--only-binary",
        "--only-group",
        "--project",
        "--python",
        "--python-platform",
        "--refresh-package",
        "--resolution",
        "--upgrade-package",
        "--with",
        "--with-editable",
        "--with-requirements",
        "-C",
        "-p",
    }
)
_UV_SETUP_DEPENDENCY_SUBCOMMAND_TOKENS = frozenset({"add", "i", "install", "sync", "update"})
_UV_SETUP_DEPENDENCY_NESTED_SUBCOMMAND_TOKENS = {
    "pip": frozenset({"compile", "install", "sync"}),
    "tool": frozenset({"install", "upgrade"}),
}
_SHELL_COMPOUND_CONTROL_TOKENS = frozenset({"&&", "||", ";", "|", "|&", "&"})
_SHELL_STATEFUL_TOOLCHAIN_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "asdf": frozenset({"shell"}),
    "fnm": frozenset({"use"}),
    "mise": frozenset({"shell", "use"}),
    "nvm": frozenset({"install", "use"}),
    "pyenv": frozenset({"activate", "shell"}),
    "rbenv": frozenset({"shell"}),
    "rtx": frozenset({"shell", "use"}),
}
_SETUP_DEPENDENCY_SIMPLE_INDEX_RE = re.compile(r"(?i)/simple(?:[/?#:\s]|$)")
_SETUP_DEPENDENCY_KNOWN_INDEX_HOSTS = frozenset(
    {
        "crates.io",
        "files.pythonhosted.org",
        "index.crates.io",
        "packagist.org",
        "plugins.gradle.org",
        "proxy.golang.org",
        "pypi.org",
        "registry.npmjs.org",
        "registry.yarnpkg.com",
        "repo.maven.apache.org",
        "repo.packagist.org",
        "rubygems.org",
        "sum.golang.org",
    }
)
_SETUP_DETERMINISTIC_FAILURE_RE = re.compile(
    r"(?i)("
    r"\bauth(?:entication)? (?:failed|required)\b|"
    r"\bcommand not found\b|"
    r"\b(?:"
    r"http(?:s)?(?:/\d+(?:\.\d+)?)? +403|"
    r"http(?:s)? status(?: code)?[:= ]+403|"
    r"status code[:= ]+403"
    r")\b[^\n]{0,80}\bforbidden\b|"
    r"\binvalid credentials\b|"
    r"\blockfile\b.*\b(out of date|conflict|mismatch)\b|"
    r"\bmissing local\b|"
    r"\bno matching distribution\b|"
    r"\bno solution found\b|"
    r"\bno such file or directory\b|"
    r"\bpermission denied\b|"
    r"\brequires python\b|"
    r"\bresolution failed\b|"
    r"\bsyntaxerror\b|"
    r"\btoml\b.*\b(parse|invalid)\b|"
    r"\bunauthorized\b|"
    r"\bversion solving failed\b|"
    r"\b(?:"
    r"http(?:s)?(?:/\d+(?:\.\d+)?)? +(?:401|403)|"
    r"http(?:s)? status(?: code)?[:= ]+(?:401|403)|"
    r"status code[:= ]+(?:401|403)"
    r")\b"
    r")"
)
_SETUP_TRANSIENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "dns",
        re.compile(
            r"(?i)("
            r"failed to lookup address information|"
            r"temporary failure in name resolution|"
            r"no address associated with hostname|"
            r"nodename nor servname provided|"
            r"name or service not known|"
            r"could not resolve host|"
            r"dns error|"
            r"\bENOTFOUND\b|"
            r"\bEAI_AGAIN\b"
            r")"
        ),
    ),
    (
        "tls",
        re.compile(r"(?i)(tls handshake timeout|ssl handshake timeout|handshake timed out)"),
    ),
    (
        "read_timeout",
        re.compile(
            r"(?i)("
            r"read timed out|read timeout|idle timeout|timeout while reading|"
            r"\bread +E(?:SOCKET)?TIMEDOUT\b"
            r")"
        ),
    ),
    (
        "connect_timeout",
        re.compile(
            r"(?i)("
            r"connection timed out|connect timeout|timed out connecting|"
            r"\b(?:connect +)?E(?:SOCKET)?TIMEDOUT\b"
            r")"
        ),
    ),
    (
        "connection",
        re.compile(
            r"(?i)("
            r"connection reset|connection refused|connection aborted|"
            r"connection closed|connection error|network is unreachable|"
            r"proxy error|temporary network failure|tunnel error|"
            r"\bECONN(?:RESET|REFUSED)\b"
            r")"
        ),
    ),
    (
        "http_5xx",
        re.compile(
            r"(?i)("
            r"http(?:s)?(?:/\d+(?:\.\d+)?)? +5\d\d\b|"
            r"http(?:s)? status(?: code)?[:= ]+5\d\d|"
            r"(?:http(?:s)?|package index|response)\b[^\n]{0,120}\b"
            r"status code[:= ]+5\d\d|"
            r"status code[:= ]+5\d\d\b[^\n]{0,120}\b"
            r"(?:http(?:s)?|package index|response)\b|"
            r"(?:http(?:s)?|status code|package index|response)\b[^\n]{0,120}\b"
            r"(?:server error|bad gateway|service unavailable|gateway timeout)\b|"
            r"\b(?:server error|bad gateway|service unavailable|gateway timeout)\b"
            r"[^\n]{0,120}\b(?:http(?:s)?|status code|package index|response)\b"
            r")"
        ),
    ),
)
_SETUP_PACKAGE_SPEC_RE = re.compile(
    r"(?i)(?:`|['\"])?(?P<package>[A-Za-z0-9][A-Za-z0-9_.-]*"
    r"(?P<operator>==|~=|!=|<=|>=|=|@)[A-Za-z0-9][A-Za-z0-9_.!+\-]*)"
    r"(?:`|['\"])?"
)
_SETUP_PACKAGE_NAME_VERSION_RE = re.compile(
    r"(?i)\b(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]*)-"
    r"(?P<version>\d+(?:\.\d+)+(?:[A-Za-z0-9_.!+\-]*))"
    r"(?:\.tar\.gz|\.tgz|\.zip|\.whl)\b"
)
_SETUP_URL_RE = re.compile(r"https?://[^\s`'\"<>)]+", re.IGNORECASE)
_SETUP_HOST_FALLBACK_RE = re.compile(
    r"(?i)\b([A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+)\b"
)
_SETUP_DEPENDENCY_FAILURE_CONTEXT_RE = re.compile(
    r"(?i)\b("
    r"could not (?:download|fetch|install|resolve)|"
    r"error sending request for url|"
    r"failed .{0,80}\b(?:download|fetch|install|resolve)|"
    r"failed to (?:download|fetch|install|resolve)|"
    r"package index .{0,160}\breturned http(?:s)?(?:/\d+(?:\.\d+)?)?|"
    r"unable to (?:download|fetch|install|resolve)"
    r")\b"
)


def _classify_setup_dependency_network_failure(
    *,
    command: str,
    returncode: int,
    stdout: str,
    stderr: str,
) -> SetupDependencyNetworkClassification | None:
    """Classify transient dependency/index fetch failures from setup output."""

    if returncode == 0 or returncode == 127:
        return None
    raw_output = _combined_setup_dependency_output(stdout=stdout, stderr=stderr)
    raw_context = f"{command}\n{raw_output}"
    if _SETUP_DETERMINISTIC_FAILURE_RE.search(raw_context):
        return None
    if not _looks_like_dependency_setup(command=command, output=raw_output):
        return None
    transient_category = _setup_transient_category(raw_context)
    if transient_category is None:
        return None
    safe_command = redact_audit_text(command, limit=_SETUP_DEPENDENCY_NETWORK_COMMAND_LIMIT)
    diagnostic = _setup_dependency_network_diagnostic(stdout=stdout, stderr=stderr)
    return SetupDependencyNetworkClassification(
        reason_code=SETUP_DEPENDENCY_NETWORK_FAILURE,
        transient_category=transient_category,
        retryable=True,
        command=safe_command,
        package=_extract_setup_dependency_package(raw_context),
        host=_extract_setup_dependency_host(raw_context),
        diagnostic=diagnostic,
    )


def _classify_setup_dependency_network_result(
    result: ValidationCommandResult,
) -> SetupDependencyNetworkClassification | None:
    stdout = (
        result.captured_stdout
        if result.captured_stdout is not None
        else (_read_text_if_present(result.stdout_path) or "")
    )
    stderr = (
        result.captured_stderr
        if result.captured_stderr is not None
        else (_read_text_if_present(result.stderr_path) or "")
    )
    return _classify_setup_dependency_network_failure(
        command=result.command,
        returncode=result.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _combined_setup_dependency_output(*, stdout: str, stderr: str) -> str:
    return "\n".join(part for part in (stdout, stderr) if part)


def _looks_like_dependency_setup(*, command: str, output: str) -> bool:
    tokens = _shell_tokens(command) or []
    compound_command = _has_shell_compound_control_operator(command)
    dependency_command_match = _non_uv_dependency_setup_command_match(tokens)
    if dependency_command_match is not None:
        if dependency_command_match and compound_command:
            return _setup_dependency_output_has_specific_transient_context(output)
        return dependency_command_match
    if _looks_like_uv_dependency_setup_command(tokens):
        return (
            _setup_dependency_output_has_specific_transient_context(output)
            if compound_command
            else True
        )
    # Unknown setup wrappers still get the bounded dependency-network retry only
    # when package or package-index evidence is co-located with the transient
    # failure. This keeps profile-specific bootstrap scripts covered without
    # retrying unrelated API calls made later by the same wrapper.
    return _setup_dependency_output_has_specific_transient_context(output)


def _setup_dependency_output_has_specific_context(output: str) -> bool:
    if _extract_setup_dependency_package(output) is not None:
        return True
    if _SETUP_DEPENDENCY_SIMPLE_INDEX_RE.search(output):
        return True
    for match in _SETUP_URL_RE.finditer(output):
        if _is_setup_dependency_index_host(urlparse(match.group(0)).hostname):
            return True
    for match in _SETUP_HOST_FALLBACK_RE.finditer(output):
        if _is_setup_dependency_index_host(match.group(1).strip(".")):
            return True
    return False


def _setup_dependency_output_has_specific_transient_context(output: str) -> bool:
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if _setup_transient_category(line) is None:
            continue
        if _setup_dependency_output_has_specific_context(line):
            return True
        if _setup_dependency_failure_block_has_specific_context(
            _setup_dependency_bounded_failure_block(lines=lines, transient_index=index)
        ):
            return True
    return False


def _setup_dependency_bounded_failure_block(*, lines: list[str], transient_index: int) -> list[str]:
    start = max(0, transient_index - _SETUP_DEPENDENCY_NETWORK_FAILURE_BLOCK_RADIUS)
    end = min(len(lines), transient_index + _SETUP_DEPENDENCY_NETWORK_FAILURE_BLOCK_RADIUS + 1)
    return lines[start:end]


def _setup_dependency_failure_block_has_specific_context(lines: list[str]) -> bool:
    return any(_setup_dependency_line_has_specific_failure_context(line) for line in lines)


def _setup_dependency_line_has_specific_failure_context(line: str) -> bool:
    return (
        _setup_dependency_output_has_specific_context(line)
        and _SETUP_DEPENDENCY_FAILURE_CONTEXT_RE.search(line) is not None
    )


def _is_setup_dependency_index_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.lower().strip(".")
    return any(
        normalized == known_host or normalized.endswith(f".{known_host}")
        for known_host in _SETUP_DEPENDENCY_KNOWN_INDEX_HOSTS
    )


def _has_shell_compound_control_operator(command: str) -> bool:
    if _has_unquoted_shell_newline(command):
        return True
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return False
    return any(token in _SHELL_COMPOUND_CONTROL_TOKENS for token in tokens)


def _has_unquoted_shell_newline(command: str) -> bool:
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote != "'":
            escaped = True
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if quote == '"':
            if char == '"':
                quote = None
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "\n":
            return True
    return False


def _non_uv_dependency_setup_command_match(tokens: list[str]) -> bool | None:
    start = _first_non_assignment_token_index(tokens)
    match = _direct_dependency_setup_command_match(tokens, start=start)
    if match is not None:
        return match
    return _python_module_pip_dependency_setup_command_match(tokens, start=start)


def _direct_dependency_setup_command_match(tokens: list[str], *, start: int) -> bool | None:
    if start >= len(tokens):
        return None
    command = _command_token_name(tokens[start]).lower()
    allowed_verbs = _SETUP_DEPENDENCY_COMMAND_VERBS.get(command)
    if allowed_verbs is None:
        return None
    if len(tokens) == start + 1:
        return True
    subcommand_index = _next_dependency_tool_subcommand_index(tokens, start=start + 1)
    if subcommand_index is None:
        return _option_only_dependency_install_command_match(
            tokens,
            command=command,
            start=start + 1,
        )
    subcommand = _command_token_name(tokens[subcommand_index]).lower()
    if subcommand in allowed_verbs:
        return True
    nested_allowed_verbs = _SETUP_DEPENDENCY_NESTED_COMMAND_VERBS.get(command, {}).get(subcommand)
    if nested_allowed_verbs is None:
        return False
    nested_subcommand_index = _next_dependency_tool_subcommand_index(
        tokens,
        start=subcommand_index + 1,
    )
    if nested_subcommand_index is None:
        return False
    nested_subcommand = _command_token_name(tokens[nested_subcommand_index]).lower()
    return nested_subcommand in nested_allowed_verbs


def _option_only_dependency_install_command_match(
    tokens: list[str], *, command: str, start: int
) -> bool:
    install_flags = _SETUP_DEPENDENCY_OPTION_ONLY_INSTALL_FLAGS.get(command)
    if install_flags is None:
        return False
    saw_install_flag = False
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--" or not token.startswith("-"):
            return False
        option_name = token.split("=", 1)[0]
        if option_name in _SETUP_DEPENDENCY_NON_INSTALL_OPTION_FLAGS:
            return False
        if option_name in install_flags:
            saw_install_flag = True
        if option_name in _SETUP_DEPENDENCY_OPTION_VALUE_FLAGS and "=" not in token:
            if index + 1 >= len(tokens):
                return False
            index += 2
            continue
        index += 1
    return saw_install_flag


def _python_module_pip_dependency_setup_command_match(
    tokens: list[str], *, start: int
) -> bool | None:
    if start >= len(tokens) or not _is_python_command_token(tokens[start]):
        return None
    index = start + 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if token == "-m":
            if index + 1 >= len(tokens):
                return False
            module_name = _command_token_name(tokens[index + 1]).lower()
            if module_name not in {"pip", "pip3"}:
                return None
            return _direct_dependency_setup_command_match(tokens, start=index + 1)
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            index += 2 if option_name in _PYTHON_OPTION_VALUE_FLAGS and "=" not in token else 1
            continue
        return None
    return None


def _next_dependency_tool_subcommand_index(tokens: list[str], *, start: int) -> int | None:
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1 if index + 1 < len(tokens) else None
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            index += (
                2 if option_name in _SETUP_DEPENDENCY_OPTION_VALUE_FLAGS and "=" not in token else 1
            )
            continue
        return index
    return None


def _is_python_command_token(token: str) -> bool:
    command = _command_token_name(token).lower()
    return (
        command in {"python", "python3"}
        or re.fullmatch(r"python\d+(?:\.\d+)?", command) is not None
    )


def _looks_like_uv_dependency_setup_command(tokens: list[str]) -> bool:
    start = _first_non_assignment_token_index(tokens)
    if start >= len(tokens):
        return False
    token_names = [_command_token_name(token) for token in tokens]
    if token_names[start] != "uv":
        return False
    subcommand_index = _next_uv_subcommand_index(tokens, start=start + 1)
    if subcommand_index is None:
        return False
    subcommand = token_names[subcommand_index]
    if subcommand in _UV_SETUP_DEPENDENCY_SUBCOMMAND_TOKENS:
        return True
    nested_subcommands = _UV_SETUP_DEPENDENCY_NESTED_SUBCOMMAND_TOKENS.get(subcommand)
    if nested_subcommands is None:
        return False
    nested_index = _next_uv_subcommand_index(tokens, start=subcommand_index + 1)
    return nested_index is not None and token_names[nested_index] in nested_subcommands


def _next_uv_subcommand_index(tokens: list[str], *, start: int) -> int | None:
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return None
        if token.startswith("-"):
            option_name = token.split("=", 1)[0]
            index += 2 if option_name in _UV_OPTION_VALUE_FLAGS and "=" not in token else 1
            continue
        return index
    return None


def _setup_transient_category(text: str) -> str | None:
    for category, pattern in _SETUP_TRANSIENT_PATTERNS:
        if pattern.search(text):
            return category
    return None


def _extract_setup_dependency_package(text: str) -> str | None:
    safe_text = _setup_dependency_package_search_text(text)
    for match in _SETUP_PACKAGE_SPEC_RE.finditer(safe_text):
        package = match.group("package")
        if _is_assignment_like_setup_package_spec(match):
            continue
        if _is_safe_setup_dependency_package(package):
            return package
    for match in _SETUP_PACKAGE_NAME_VERSION_RE.finditer(safe_text):
        package = f"{match.group('name')}=={match.group('version')}"
        if _is_safe_setup_dependency_package(package):
            return package
    return None


def _is_assignment_like_setup_package_spec(match: re.Match[str]) -> bool:
    return (
        match.group("operator") == "="
        and _ENV_ASSIGNMENT_RE.fullmatch(match.group("package")) is not None
    )


def _setup_dependency_package_search_text(text: str) -> str:
    return _SETUP_URL_RE.sub(_strip_setup_dependency_url_userinfo, text)


def _strip_setup_dependency_url_userinfo(match: re.Match[str]) -> str:
    url = match.group(0)
    parsed = urlparse(url)
    if "@" not in parsed.netloc or parsed.hostname is None:
        return url
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is not None:
        host = f"{host}:{port}"
    return parsed._replace(netloc=host).geturl()


def _is_safe_setup_dependency_package(package: str) -> bool:
    redacted = redact_audit_text(package, limit=len(package) + len("...[truncated]"))
    return redacted == package


def _is_safe_setup_dependency_host(host: str) -> bool:
    redacted = redact_audit_text(host, limit=len(host) + len("...[truncated]"))
    return redacted == host


def _extract_setup_dependency_host(text: str) -> str | None:
    for match in _SETUP_URL_RE.finditer(text):
        host = urlparse(match.group(0)).hostname
        if host and _is_safe_setup_dependency_host(host):
            return host
    for match in _SETUP_HOST_FALLBACK_RE.finditer(text):
        candidate = match.group(1).strip(".")
        if candidate.lower().endswith(
            (
                ".cfg",
                ".gz",
                ".json",
                ".lock",
                ".py",
                ".toml",
                ".whl",
                ".yml",
                ".yaml",
            )
        ):
            continue
        if re.fullmatch(r"[\d.]+", candidate):
            continue
        if not _is_safe_setup_dependency_host(candidate):
            continue
        return candidate
    return None


def _setup_dependency_network_diagnostic(*, stdout: str, stderr: str) -> str:
    output = _combined_setup_dependency_output(stdout=stdout, stderr=stderr)
    diagnostic_input = output[:_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_SCAN_LIMIT]
    normalized = re.sub(r"\s+", " ", diagnostic_input).strip()
    return redact_audit_text(
        normalized,
        limit=_SETUP_DEPENDENCY_NETWORK_DIAGNOSTIC_LIMIT,
    )


def _setup_dependency_retry_output_prefix(
    *,
    retry_number: int,
    elapsed_seconds: float | None = None,
) -> str:
    if elapsed_seconds is not None:
        elapsed = _format_seconds(elapsed_seconds)
        return f"\n[setup dependency network retry {retry_number} at {elapsed}s]\n"
    return f"\n[setup dependency network retry {retry_number}]\n"


def _setup_dependency_attempt_metadata(
    *,
    classification: SetupDependencyNetworkClassification,
    attempt: int,
    retry_number: int | None,
) -> dict[str, object]:
    metadata = dict(classification.metadata)
    metadata["attempt"] = attempt
    if retry_number is not None:
        metadata["retry_number"] = retry_number
    return metadata


def _with_setup_dependency_network_metadata(
    result: ValidationCommandResult,
    *,
    step: ProfileExecutionCommand,
    classification: SetupDependencyNetworkClassification,
    setup_retry_count: int,
    flaky_retry_count: int,
    total_retry_count: int,
    retry_budget: int,
    retry_exhausted: bool,
    recovered: bool,
    attempts: list[dict[str, object]],
) -> ValidationCommandResult:
    metadata = _execution_command_metadata(step)
    setup_metadata = dict(classification.metadata)
    setup_metadata.update(
        {
            "retry_count": setup_retry_count,
            "setup_retry_count": setup_retry_count,
            "flaky_retry_count": flaky_retry_count,
            "total_retry_count": total_retry_count,
            "retry_budget": retry_budget,
            "retry_exhausted": retry_exhausted,
            "recovered": recovered,
            "attempts": attempts,
            "stream_ids": dict(result.stream_ids),
        }
    )
    metadata[SETUP_DEPENDENCY_NETWORK_METADATA_KEY] = setup_metadata
    return replace(
        result,
        reason_code=(SETUP_DEPENDENCY_NETWORK_FAILURE if retry_exhausted else result.reason_code),
        retry_count=total_retry_count,
        metadata=metadata,
        captured_stdout=None,
        captured_stderr=None,
    )


def _setup_dependency_retry_applies(step: ProfileExecutionCommand) -> bool:
    # Optional commands are finalized before retry classification; keep dependency-network
    # retries scoped to required setup gates.
    return step.phase == "setup" and not step.database_hook and step.command.required
