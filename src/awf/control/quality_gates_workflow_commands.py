"""Quality-gate protections for agent-authored workspace changes."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any, Final

PROTECTED_QUALITY_GATE_PATHS: Final[tuple[str, ...]] = (
    ".awf/workspace.yml",
    ".coveragerc",
    ".github/workflows/",
    "pyproject.toml",
    "pytest.ini",
    "setup.cfg",
    "setup.py",
    "tox.ini",
)
INTERNAL_PLAN_ARTIFACT_PREFIX: Final[str] = "docs/awf-plans/"
PLAN_ONLY_OUTPUT_REASON_CODE: Final[str] = "PLAN_ONLY_OUTPUT"
_WORKFLOW_PREFIX: Final[str] = ".github/workflows/"
_COMMENT_STEP_MARKERS: Final[tuple[str, ...]] = (
    "comment",
    "pr comment",
    "pr-comment",
    "notify",
    "notification",
)
_COMMENT_NOTIFY_ACTION_USES: Final[frozenset[str]] = frozenset(
    {
        "peter-evans/create-or-update-comment",
    }
)
_COMMENT_NOTIFY_ACTION_ALLOWED_WITH_KEYS: Final[dict[str, frozenset[str]]] = {
    "peter-evans/create-or-update-comment": frozenset(
        {
            "append-separator",
            "body",
            "comment-id",
            "edit-mode",
            "issue-number",
            "reactions",
            "reactions-edit-mode",
        }
    )
}
_COMMENT_NOTIFY_CAPABLE_ACTION_USES: Final[frozenset[str]] = frozenset(
    {
        "actions/github-script",
    }
)
_GITHUB_SCRIPT_COMMENT_ALLOWED_REST_METHODS: Final[frozenset[tuple[str, str]]] = frozenset(
    {
        ("issues", "createComment"),
        ("issues", "updateComment"),
        ("pulls", "createReviewComment"),
    }
)
_GITHUB_SCRIPT_COMMENT_ALLOWED_WITH_KEYS: Final[frozenset[str]] = frozenset(
    {"debug", "result-encoding", "retries", "retry-exempt-status-codes", "script"}
)
_GITHUB_SCRIPT_REST_METHOD_RE: Final = re.compile(
    r"\bgithub\.rest\.([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_GITHUB_SCRIPT_BLOCKED_ACCESS_RE: Final = re.compile(
    r"\b(?:core|exec|glob|io)\s*\."
    r"|\b(?:eval|fetch|Function|require|setInterval|setTimeout)\s*\("
    r"|\bimport\s*\("
    r"|\bprocess\s*(?:\.|\?\.|\[)"
    r"|\b(?:github|context)\s*(?:\.|\?\.)\s*token\b"
    r"|\b(?:github|context)\s*(?:\?\.)?\[\s*(?:['\"]token['\"]|`token`)\s*\]"
    r"|\bgithub\s*\["
    r"|\bgithub\.(?:graphql|paginate|request)\s*\("
)
_SENSITIVE_WORKFLOW_WITH_INPUT_PARTS: Final[frozenset[str]] = frozenset(
    {"credential", "credentials", "password", "secret", "secrets", "token"}
)
_SENSITIVE_WORKFLOW_WITH_INPUT_NAMES: Final[frozenset[str]] = frozenset(
    {"access-key", "api-key", "client-secret", "deploy-key", "private-key", "ssh-key"}
)
_WORKFLOW_PINNED_BUMP_ALLOWED_WITH_KEYS: Final[dict[str, frozenset[str]]] = {
    "actions/cache": frozenset(
        {
            "enablecrossosarchive",
            "fail-on-cache-miss",
            "key",
            "lookup-only",
            "path",
            "restore-keys",
            "save-always",
            "upload-chunk-size",
        }
    ),
    "actions/cache/restore": frozenset(
        {
            "enablecrossosarchive",
            "fail-on-cache-miss",
            "key",
            "lookup-only",
            "path",
            "restore-keys",
        }
    ),
    "actions/cache/save": frozenset(
        {
            "enablecrossosarchive",
            "key",
            "path",
            "upload-chunk-size",
        }
    ),
    "actions/setup-python": frozenset({"python-version"}),
}
_INFORMATIONAL_MARKERS: Final[tuple[str, ...]] = (
    "comment",
    "pr comment",
    "pr-comment",
    "notify",
    "notification",
    "info",
    "informational",
    "summary",
    "report",
)
_INFORMATIONAL_JOB_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"env", "if", "name", "needs", "permissions", "runs-on", "steps"}
)
_INFORMATIONAL_JOB_COMMENT_PERMISSION_SCOPES: Final[frozenset[str]] = frozenset(
    {"issues", "pull-requests"}
)
_INFORMATIONAL_JOB_READ_PERMISSION_SCOPES: Final[frozenset[str]] = frozenset({"contents"})
_INFORMATIONAL_STEP_ALLOWED_KEYS: Final[frozenset[str]] = frozenset(
    {"continue-on-error", "env", "id", "if", "name", "run", "uses", "with"}
)
_INFORMATIONAL_RUN_COMMAND_NAMES: Final[frozenset[str]] = frozenset({"echo", "printf"})
_INFORMATIONAL_RUN_SEPARATORS: Final[frozenset[str]] = frozenset({";", "&&"})
_INFORMATIONAL_RUN_BLOCKED_OPERATORS: Final[frozenset[str]] = frozenset(
    {"|", "|&", "||", "&", "<", ">", "<<", ">>", "<>", ">|", "<<<", "&>", "&>>", ">&", "<&"}
)
_VALIDATION_RUN_APPEND_BLOCKED_OPERATORS: Final[frozenset[str]] = (
    _INFORMATIONAL_RUN_BLOCKED_OPERATORS | frozenset({";"})
)
_VALIDATION_RUN_DIRECT_COMMAND_NAMES: Final[frozenset[str]] = frozenset({"mypy", "pytest", "ruff"})
_VALIDATION_RUN_MODULE_NAMES: Final[frozenset[str]] = (
    _VALIDATION_RUN_DIRECT_COMMAND_NAMES | frozenset({"coverage", "unittest"})
)
_VALIDATION_RUN_COVERAGE_REPORT_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {"annotate", "html", "json", "lcov", "report", "xml"}
)
_VALIDATION_RUN_TEST_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {"cargo", "go", "gradle", "gradlew", "make", "mvn", "nox", "tox"}
)
_VALIDATION_RUN_TARGET_NAMES: Final[frozenset[str]] = frozenset(
    {"check", "coverage", "lint", "test", "tests"}
)
_VALIDATION_COMMAND_TOKEN_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_-])"
    r"(?:pytest|ruff|mypy|coverage)"
    r"(?![A-Za-z0-9_-])"
)
_BROAD_VALIDATION_COMMAND_NAMES: Final[frozenset[str]] = frozenset(
    {"build", "lint", "deploy", "publish", "release"}
)
_BROAD_VALIDATION_SCRIPT_STEMS: Final[frozenset[str]] = frozenset(
    {"build", "lint", "deploy", "publish", "release"}
)
_SHELL_SEGMENT_SPLIT_RE: Final = re.compile(r"(?:&&|\|\||;|\n)")
_SHELL_COMMAND_SEPARATORS: Final[frozenset[str]] = frozenset({";", "&&", "||", "|", "|&", "&"})
_COMMAND_PREFIX_WORDS: Final[frozenset[str]] = frozenset({"command", "sudo", "time"})
_ENV_ASSIGNMENT_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_BRACED_SHELL_PARAMETER_RE: Final = re.compile(r"\$\{(?!\{)")
_UNBRACED_SHELL_PARAMETER_RE: Final = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")
_GITHUB_ACTIONS_EXPRESSION_RE: Final = re.compile(r"\$\{\{\s*(?P<expression>.*?)\s*\}\}")
_SAFE_INFORMATIONAL_GITHUB_ACTIONS_EXPRESSION_RE: Final = re.compile(
    r"(?:"
    r"github\.(?:sha|run_id|run_number|run_attempt|event_name|repository|server_url|actor|"
    r"triggering_actor|job|action)"
    r"|github\.event\.pull_request\.(?:number|head\.sha|base\.ref)"
    r"|steps\.[A-Za-z_][A-Za-z0-9_-]*\.(?:outcome|conclusion)"
    r"|needs\.[A-Za-z_][A-Za-z0-9_-]*\.result"
    r")"
)
_SENSITIVE_ENV_NAME_RE: Final = re.compile(
    r"(?:^|_)(?:ACCESS_KEY|API_KEY|AUTH|CREDENTIAL|PASSWD|PASSWORD|PAT|PRIVATE_KEY|SECRET|TOKEN)(?:_|$)",
    re.IGNORECASE,
)
_ENV_NAME_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BLOCKED_INFORMATIONAL_ENV_NAMES: Final[frozenset[str]] = frozenset(
    {"BASH_ENV", "ENV", "GITHUB_ENV", "GITHUB_PATH", "IFS", "PATH", "SHELL", "SHELLOPTS"}
)
_SAFE_INFORMATIONAL_RUN_ENV_NAMES: Final[frozenset[str]] = frozenset({"PATH"})
_PACKAGE_MANAGER_OPTIONS_WITH_VALUE: Final[frozenset[str]] = frozenset(
    {"--cwd", "--dir", "--filter", "--prefix", "--workspace", "-c", "-w"}
)
_COVERAGE_OPTIONS_WITH_VALUE: Final[frozenset[str]] = frozenset(
    {"--data-file", "--debug", "--rcfile"}
)
_SCRIPT_SUFFIXES: Final[tuple[str, ...]] = (
    ".bash",
    ".fish",
    ".ps1",
    ".py",
    ".sh",
    ".zsh",
)
_VALIDATION_TEST_COMMAND_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:npm|pnpm|yarn|bun|go|cargo|make|mvn|gradle|gradlew|tox|nox|uv|poetry|pipenv)"
    r"(?:\s+(?:run|exec|--?[A-Za-z0-9_.=:/-]+))*"
    r"\s+test(?:\s|$)"
)
_VALIDATION_UNITTEST_COMMAND_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])python(?:3(?:\.\d+)?)?\s+-m\s+unittest(?:\s|$)"
)
_VALIDATION_TEST_PATH_RE: Final = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"(?:(?:uv|poetry|pipenv)\s+run\s+)?python(?:3(?:\.\d+)?)?"
    r"(?:\s+--?[A-Za-z0-9_.=:/-]+)*"
    r"\s+tests/[^\s;&|]*"
)
_PINNED_WORKFLOW_USES_SHA_RE: Final = re.compile(r"^[0-9a-fA-F]{40}$")
_PINNED_WORKFLOW_USES_VERSION_RE: Final = re.compile(
    r"^[vV]?(?:\d+|\d+\.\d+|\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)$"
)
_PRERELEASE_NUMERIC_SUFFIX_RE: Final = re.compile(r"^([A-Za-z]+)(\d+)$")
type _WorkflowPrereleaseIdentifierKey = tuple[int, int | str]
type _WorkflowVersionRefSortKey = tuple[
    tuple[int, ...], int, tuple[_WorkflowPrereleaseIdentifierKey, ...]
]
_PYPROJECT_POLICY_SECTIONS: Final[tuple[tuple[str, ...], ...]] = (
    ("build-system",),
    ("tool", "hatch"),
    ("tool", "pytest"),
    ("tool", "coverage"),
    ("tool", "ruff"),
    ("tool", "mypy"),
)
_ALLOWED_PROJECT_METADATA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "version",
        "description",
        "readme",
        "requires-python",
        "license",
        "authors",
        "maintainers",
        "keywords",
        "classifiers",
        "urls",
        "dependencies",
        "optional-dependencies",
    }
)


from awf.control.quality_gates_workflow_actions import (  # noqa: E402
    _comment_notify_action_with_inputs_are_safe,
    _github_script_comment_notify_inputs_are_safe,
    _is_pinned_workflow_uses_ref,
    _uses_action_and_ref,
)


def _is_informational_run_command(
    command: str | None,
    safe_env_names: set[str] | None = None,
) -> bool:
    if command is None:
        return True
    command_safe_env_names = set(_SAFE_INFORMATIONAL_RUN_ENV_NAMES)
    if safe_env_names is not None:
        command_safe_env_names.update(safe_env_names)
    logical_lines = _logical_shell_lines(command)
    if logical_lines is None:
        return False
    for line in logical_lines:
        tokens = _shell_tokens(line)
        if tokens is None:
            return False
        if not _informational_shell_tokens_are_safe(tokens, command_safe_env_names):
            return False
    return True


def _logical_shell_lines(command: str) -> tuple[str, ...] | None:
    lines: list[str] = []
    current_line: str | None = None
    for physical_line in command.splitlines():
        if current_line is None:
            current_line = physical_line
        else:
            current_line += physical_line

        if _has_trailing_shell_line_continuation(current_line):
            current_line = current_line[:-1]
            continue

        lines.append(current_line)
        current_line = None

    if current_line is not None:
        return None
    return tuple(lines)


def _has_trailing_shell_line_continuation(line: str) -> bool:
    trailing_backslashes = len(line) - len(line.rstrip("\\"))
    return trailing_backslashes % 2 == 1


def _shell_tokens(command: str) -> tuple[str, ...] | None:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.wordchars += "$%{}()"
        return tuple(token for token in lexer if token)
    except ValueError:
        return None


def _informational_shell_tokens_are_safe(
    tokens: Sequence[str], safe_env_names: set[str] | None = None
) -> bool:
    if safe_env_names is None:
        safe_env_names = set(_SAFE_INFORMATIONAL_RUN_ENV_NAMES)
    if not tokens:
        return True
    command_tokens: list[str] = []
    for token in tokens:
        if token in _INFORMATIONAL_RUN_BLOCKED_OPERATORS:
            return False
        if token in _INFORMATIONAL_RUN_SEPARATORS:
            if not command_tokens or not _informational_shell_command_is_safe(
                command_tokens, safe_env_names
            ):
                return False
            _remember_safe_informational_assignments(command_tokens, safe_env_names)
            command_tokens = []
            continue
        command_tokens.append(token)
    if not command_tokens or not _informational_shell_command_is_safe(
        command_tokens, safe_env_names
    ):
        return False
    _remember_safe_informational_assignments(command_tokens, safe_env_names)
    return True


def _informational_shell_command_is_safe(
    tokens: Sequence[str], safe_env_names: set[str] | None = None
) -> bool:
    if safe_env_names is None:
        safe_env_names = set(_SAFE_INFORMATIONAL_RUN_ENV_NAMES)
    remaining = tuple(tokens)
    if not remaining:
        return True
    if any("$(" in token or "`" in token for token in remaining):
        return False
    if _has_unsafe_informational_parameter_expansion(remaining, safe_env_names):
        return False
    while remaining and _ENV_ASSIGNMENT_RE.match(remaining[0]) is not None:
        remaining = remaining[1:]
    if not remaining:
        return True
    return _command_basename(remaining[0]) in _INFORMATIONAL_RUN_COMMAND_NAMES


def _remember_safe_informational_assignments(
    tokens: Sequence[str], safe_env_names: set[str]
) -> None:
    if not tokens or any(_ENV_ASSIGNMENT_RE.match(token) is None for token in tokens):
        return
    for token in tokens:
        name = token.split("=", 1)[0]
        if _SENSITIVE_ENV_NAME_RE.search(name) is None:
            safe_env_names.add(name)


def _has_unsafe_informational_parameter_expansion(
    tokens: Sequence[str], safe_env_names: set[str] | None = None
) -> bool:
    if safe_env_names is None:
        safe_env_names = set(_SAFE_INFORMATIONAL_RUN_ENV_NAMES)
    if _has_unsafe_github_actions_expression(tokens):
        return True
    for token in tokens:
        if _BRACED_SHELL_PARAMETER_RE.search(token) is not None:
            return True
        for match in _UNBRACED_SHELL_PARAMETER_RE.finditer(token):
            name = match.group(1)
            if _SENSITIVE_ENV_NAME_RE.search(name) is not None:
                return True
            if name not in safe_env_names:
                return True
    return False


def _has_unsafe_github_actions_expression(tokens: Sequence[str]) -> bool:
    command_fragment = " ".join(tokens)
    if "${{" not in command_fragment:
        return False
    if "${{" in _GITHUB_ACTIONS_EXPRESSION_RE.sub("", command_fragment):
        return True
    for match in _GITHUB_ACTIONS_EXPRESSION_RE.finditer(command_fragment):
        expression = " ".join(match.group("expression").split())
        if not _is_safe_informational_github_actions_expression(expression):
            return True
    return False


def _is_safe_informational_github_actions_expression(expression: str) -> bool:
    return _SAFE_INFORMATIONAL_GITHUB_ACTIONS_EXPRESSION_RE.fullmatch(expression) is not None


def _is_validation_command(command: str | None) -> bool:
    if command is None:
        return False
    for line in command.splitlines():
        tokens = _shell_tokens(line)
        if tokens is None:
            if _raw_validation_command_match(line.lower()):
                return True
            continue
        if _shell_tokens_include_validation_command(tokens):
            return True
    return _has_broad_validation_command_invocation(command.lower())


def _raw_validation_command_match(normalized: str) -> bool:
    return (
        _VALIDATION_TEST_PATH_RE.search(normalized) is not None
        or _VALIDATION_COMMAND_TOKEN_RE.search(normalized) is not None
        or _VALIDATION_TEST_COMMAND_RE.search(normalized) is not None
        or _VALIDATION_UNITTEST_COMMAND_RE.search(normalized) is not None
    )


def _shell_tokens_include_validation_command(tokens: Sequence[str]) -> bool:
    command_tokens: list[str] = []
    for token in tokens:
        if token in _SHELL_COMMAND_SEPARATORS:
            if _shell_command_tokens_are_validation(command_tokens):
                return True
            command_tokens = []
            continue
        command_tokens.append(token)
    return _shell_command_tokens_are_validation(command_tokens)


def _shell_command_tokens_are_validation(tokens: Sequence[str]) -> bool:
    words = _strip_validation_command_prefixes(tuple(token.lower() for token in tokens))
    if not words:
        return False
    command_name = _command_basename(words[0])
    if command_name in {"pipenv", "poetry", "uv"} and len(words) > 1 and words[1] == "run":
        return _run_wrapper_runs_validation_command(words[2:])
    return _command_words_start_validation_command(words)


def _strip_validation_command_prefixes(words: Sequence[str]) -> tuple[str, ...]:
    remaining = tuple(words)
    while remaining:
        command = _command_basename(remaining[0])
        if remaining[0] in _COMMAND_PREFIX_WORDS or _ENV_ASSIGNMENT_RE.match(remaining[0]):
            remaining = remaining[1:]
            continue
        if command == "env":
            remaining = _strip_env_prefix(remaining[1:])
            continue
        return remaining
    return ()


def _run_wrapper_runs_validation_command(words: Sequence[str]) -> bool:
    remaining = _strip_run_wrapper_options(words)
    if not remaining:
        return False
    command_name = _command_basename(remaining[0])
    return _is_validation_run_target(command_name) or _command_words_start_validation_command(
        remaining
    )


def _command_words_start_validation_command(words: Sequence[str]) -> bool:
    if not words:
        return False
    command_name = _command_basename(words[0])
    if command_name in _VALIDATION_RUN_DIRECT_COMMAND_NAMES or command_name == "coverage":
        return True
    if command_name.startswith("python"):
        return _python_runs_validation_command(words[1:])
    if command_name in {"npm", "pnpm", "yarn", "bun"}:
        return _package_manager_runs_any_validation_command(words[1:])
    if command_name in _VALIDATION_RUN_TEST_COMMAND_NAMES:
        return _command_args_include_validation_target(words[1:])
    return False


def _python_runs_validation_command(words: Sequence[str]) -> bool:
    remaining = tuple(words)
    while remaining and remaining[0].startswith("-") and remaining[0] != "-m":
        remaining = remaining[1:]
    if len(remaining) >= 2 and remaining[0] == "-m":
        return remaining[1] in _VALIDATION_RUN_MODULE_NAMES
    return any(
        word == "tests" or word.startswith("tests/")
        for word in remaining
        if not word.startswith("-")
    )


def _package_manager_runs_any_validation_command(words: Sequence[str]) -> bool:
    remaining = tuple(word for word in words if word != "--")
    remaining = _strip_package_manager_options(remaining)
    if not remaining:
        return False
    if remaining[0] in {"exec", "run"}:
        remaining = _strip_package_manager_options(remaining[1:])
        if not remaining:
            return False
    command_name = _command_basename(remaining[0])
    return _is_validation_run_target(command_name) or _command_words_start_validation_command(
        remaining
    )


def _preserves_existing_validation_run(old_run: str | None, new_run: str | None) -> bool:
    if old_run is None or new_run is None:
        return False
    old_command = old_run.strip()
    new_command = new_run.strip()
    if not old_command or not new_command:
        return False
    if new_command == old_command:
        return True
    if not new_command.startswith(old_command):
        return False
    suffix = new_command[len(old_command) :]
    if suffix and suffix[0] not in (" ", "\t", "\n", "\r"):
        return False
    appended_commands = _validation_run_append_commands(suffix)
    return appended_commands is not None and all(
        _validation_run_append_command_is_safe(command) for command in appended_commands
    )


def _validation_run_append_commands(suffix: str) -> tuple[tuple[str, ...], ...] | None:
    logical_lines = _logical_shell_lines(suffix.replace("\r\n", "\n").replace("\r", "\n"))
    if logical_lines is None:
        return None

    commands: list[tuple[str, ...]] = []
    command_tokens: list[str] = []
    has_append_boundary = False
    expecting_command = True
    pending_and_separator = False
    for line_index, line in enumerate(logical_lines):
        tokens = _shell_tokens(line)
        if tokens is None:
            return None
        for token in tokens:
            if token == "&&":
                if expecting_command:
                    if has_append_boundary:
                        return None
                    has_append_boundary = True
                    pending_and_separator = True
                    continue
                commands.append(tuple(command_tokens))
                command_tokens = []
                has_append_boundary = True
                pending_and_separator = True
                expecting_command = True
                continue
            if token in _VALIDATION_RUN_APPEND_BLOCKED_OPERATORS:
                return None
            if expecting_command and not has_append_boundary:
                return None
            command_tokens.append(token)
            pending_and_separator = False
            expecting_command = False

        if line_index < len(logical_lines) - 1:
            if command_tokens:
                commands.append(tuple(command_tokens))
                command_tokens = []
            expecting_command = True
            has_append_boundary = True

    if pending_and_separator:
        return None
    if command_tokens:
        commands.append(tuple(command_tokens))
    if not commands:
        return None
    return tuple(commands)


def _validation_run_append_command_is_safe(tokens: Sequence[str]) -> bool:
    if not tokens or any("$(" in token or "`" in token for token in tokens):
        return False

    command_words = _strip_shell_command_prefixes(tokens)
    if not command_words:
        return False
    command_name = _command_basename(command_words[0])
    if command_name == "coverage":
        return _coverage_runs_safe_report_command(command_words[1:])
    if command_name in _VALIDATION_RUN_DIRECT_COMMAND_NAMES:
        return True
    if command_name.startswith("python"):
        return _python_runs_validation_module(command_words[1:])
    if command_name in {"npm", "pnpm", "yarn", "bun"}:
        return _package_manager_runs_validation_command(command_words[1:])
    if command_name in _VALIDATION_RUN_TEST_COMMAND_NAMES:
        return _command_args_include_validation_target(command_words[1:])
    return False


def _python_runs_validation_module(words: Sequence[str]) -> bool:
    remaining = tuple(words)
    while remaining and remaining[0].startswith("-") and remaining[0] != "-m":
        remaining = remaining[1:]
    if len(remaining) < 2 or remaining[0] != "-m":
        return False
    if remaining[1] == "coverage":
        return _coverage_runs_safe_report_command(remaining[2:])
    return remaining[1] in _VALIDATION_RUN_MODULE_NAMES


def _package_manager_runs_validation_command(words: Sequence[str]) -> bool:
    remaining = tuple(word for word in words if word != "--")
    remaining = _strip_package_manager_options(remaining)
    if not remaining:
        return False
    if remaining[0] == "exec":
        remaining = _strip_package_manager_options(remaining[1:])
    elif remaining[0] == "run":
        remaining = _strip_package_manager_options(remaining[1:])
        if not remaining:
            return False
        command_name = _command_basename(remaining[0])
        return command_name in _VALIDATION_RUN_DIRECT_COMMAND_NAMES or _is_validation_run_target(
            command_name
        )
    if not remaining:
        return False
    command_name = _command_basename(remaining[0])
    if command_name == "coverage":
        return _coverage_runs_safe_report_command(remaining[1:])
    return command_name in _VALIDATION_RUN_DIRECT_COMMAND_NAMES or _is_validation_run_target(
        command_name
    )


def _coverage_runs_safe_report_command(words: Sequence[str]) -> bool:
    remaining = _strip_coverage_options(words)
    if not remaining:
        return False
    return remaining[0].lower() in _VALIDATION_RUN_COVERAGE_REPORT_COMMAND_NAMES


def _strip_coverage_options(words: Sequence[str]) -> tuple[str, ...]:
    remaining = tuple(words)
    while remaining and remaining[0].startswith("-"):
        option = remaining[0]
        option_name = option.split("=", 1)[0]
        remaining = remaining[1:]
        if "=" not in option and option_name in _COVERAGE_OPTIONS_WITH_VALUE and remaining:
            remaining = remaining[1:]
    return remaining


def _command_args_include_validation_target(words: Sequence[str]) -> bool:
    return any(_is_validation_run_target(word) for word in words if not word.startswith("-"))


def _is_validation_run_target(value: str) -> bool:
    return value in _VALIDATION_RUN_TARGET_NAMES or value.startswith(
        ("check:", "check-", "coverage:", "coverage-", "lint:", "lint-", "test:", "test-")
    )


def _has_broad_validation_command_invocation(command: str) -> bool:
    segments = _shell_command_word_segments(command)
    if segments is None:
        segments = tuple(
            _shell_words(segment) for segment in _SHELL_SEGMENT_SPLIT_RE.split(command)
        )
    return any(_words_start_broad_validation_command(words) for words in segments)


def _shell_command_word_segments(command: str) -> tuple[tuple[str, ...], ...] | None:
    normalized = command.replace("\r\n", "\n").replace("\r", "\n").replace("\n", ";")
    tokens = _shell_tokens(normalized)
    if tokens is None:
        return None

    segments: list[tuple[str, ...]] = []
    segment: list[str] = []
    for token in tokens:
        if token in _SHELL_COMMAND_SEPARATORS:
            if segment:
                segments.append(tuple(segment))
                segment = []
            continue
        segment.append(token)
    if segment:
        segments.append(tuple(segment))
    return tuple(segments)


def _shell_words(segment: str) -> tuple[str, ...]:
    stripped = segment.strip()
    if not stripped:
        return ()
    try:
        words = shlex.split(stripped, comments=False, posix=True)
    except ValueError:
        words = stripped.split()
    return tuple(word for word in words if word)


def _words_start_broad_validation_command(words: Sequence[str]) -> bool:
    command_words = _strip_shell_command_prefixes(words)
    if not command_words:
        return False
    command = command_words[0]
    command_name = _command_basename(command)
    if (
        command_name in _BROAD_VALIDATION_COMMAND_NAMES
        or _script_stem(command_name) in _BROAD_VALIDATION_SCRIPT_STEMS
    ):
        return True
    if command_name in {"npm", "pnpm", "yarn", "bun"}:
        return _package_manager_runs_broad_validation_command(command_words[1:])
    if command_name == "make":
        return any(
            word in _BROAD_VALIDATION_COMMAND_NAMES
            for word in command_words[1:]
            if not word.startswith("-") and "=" not in word
        )
    if command_name.startswith("python") and _python_runs_broad_validation_module(
        command_words[1:]
    ):
        return True
    if command_name == "docker":
        return _docker_runs_broad_validation_command(command_words[1:])
    if command_name in {"bash", "fish", "sh", "zsh"} and len(command_words) > 1:
        script_name = _command_basename(command_words[1])
        return _script_stem(script_name) in _BROAD_VALIDATION_SCRIPT_STEMS
    return _known_broad_validation_command_pair(command_name, command_words[1:])


def _strip_shell_command_prefixes(words: Sequence[str]) -> tuple[str, ...]:
    remaining = tuple(words)
    while remaining:
        command = _command_basename(remaining[0])
        if remaining[0] in _COMMAND_PREFIX_WORDS or _ENV_ASSIGNMENT_RE.match(remaining[0]):
            remaining = remaining[1:]
            continue
        if command == "env":
            remaining = _strip_env_prefix(remaining[1:])
            continue
        if command in {"pipenv", "poetry", "uv"} and len(remaining) > 1 and remaining[1] == "run":
            remaining = _strip_run_wrapper_options(remaining[2:])
            continue
        return remaining
    return ()


def _strip_env_prefix(words: Sequence[str]) -> tuple[str, ...]:
    remaining = tuple(words)
    while remaining and (
        remaining[0].startswith("-") or _ENV_ASSIGNMENT_RE.match(remaining[0]) is not None
    ):
        remaining = remaining[1:]
    return remaining


def _strip_run_wrapper_options(words: Sequence[str]) -> tuple[str, ...]:
    remaining = tuple(words)
    while remaining and remaining[0].startswith("-"):
        option = remaining[0]
        remaining = remaining[1:]
        if option in {"--directory", "--env-file", "--extra", "--group", "--index-url", "--python"}:
            remaining = remaining[1:]
    return remaining


def _package_manager_runs_broad_validation_command(words: Sequence[str]) -> bool:
    remaining = tuple(word for word in words if word != "--")
    remaining = _strip_package_manager_options(remaining)
    if not remaining:
        return False
    if remaining[0] in {"exec", "run"}:
        remaining = _strip_package_manager_options(remaining[1:])
    return bool(remaining) and remaining[0] in _BROAD_VALIDATION_COMMAND_NAMES


def _strip_package_manager_options(words: Sequence[str]) -> tuple[str, ...]:
    remaining = tuple(words)
    while remaining and remaining[0].startswith("-"):
        option = remaining[0]
        option_name = option.split("=", 1)[0]
        remaining = remaining[1:]
        if "=" not in option and option_name in _PACKAGE_MANAGER_OPTIONS_WITH_VALUE and remaining:
            remaining = remaining[1:]
    return remaining


def _python_runs_broad_validation_module(words: Sequence[str]) -> bool:
    remaining = tuple(words)
    while remaining and remaining[0].startswith("-") and remaining[0] != "-m":
        remaining = remaining[1:]
    if len(remaining) < 2 or remaining[0] != "-m":
        return False
    return remaining[1] in _BROAD_VALIDATION_COMMAND_NAMES


def _docker_runs_broad_validation_command(words: Sequence[str]) -> bool:
    if not words:
        return False
    if words[0] == "build":
        return True
    return len(words) > 1 and words[0] == "compose" and words[1] == "build"


def _known_broad_validation_command_pair(command: str, args: Sequence[str]) -> bool:
    if command == "gh":
        return bool(args) and args[0] == "release"
    if command == "gcloud":
        return len(args) > 1 and args[0] == "run" and args[1] == "deploy"
    if command in {"firebase", "netlify", "vercel", "wrangler"}:
        return "deploy" in args
    if command == "twine":
        return "upload" in args
    return False


def _command_basename(command: str) -> str:
    return command.rsplit("/", 1)[-1]


def _script_stem(command: str) -> str:
    for suffix in _SCRIPT_SUFFIXES:
        if command.endswith(suffix):
            return command[: -len(suffix)]
    return command


def _is_comment_or_notify_capable_step_uses(step: Mapping[str, Any], uses: str) -> bool:
    parts = _uses_action_and_ref(uses)
    if parts is None:
        return False
    action, ref = parts
    normalized_action = action.lower()
    if not _is_pinned_workflow_uses_ref(ref):
        return False
    if normalized_action in _COMMENT_NOTIFY_ACTION_USES:
        return _comment_notify_action_with_inputs_are_safe(
            normalized_action,
            step.get("with"),
        )
    if normalized_action not in _COMMENT_NOTIFY_CAPABLE_ACTION_USES:
        return False
    if normalized_action == "actions/github-script":
        return _github_script_comment_notify_inputs_are_safe(step.get("with"))
    return "with" not in step
