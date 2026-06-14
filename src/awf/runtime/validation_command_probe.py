"""Validate-command toolchain probe: shell parsing for the adopt-PR handoff.

Extracted from :mod:`awf.runtime.validation_setup` to keep that module within the
first-party file-size guardrail. This module owns the shell-command parsing the
``PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED`` handoff relies on: it reduces each
profile ``validate`` command to the PATH-resolvable executables it *requires*, so
a tool that setup did not provision fails the handoff early rather than dying
``127`` later during ``monitoring_pr`` validation.

The low-level shell tokenization primitives (:func:`_shell_tokens`,
:data:`_ENV_ASSIGNMENT_RE`, :func:`_first_non_assignment_token_index`) live here
too because both this probe and the setup-dependency classification in
``validation_setup`` consume them; ``validation_setup`` imports them back as a
stable façade.
"""

from __future__ import annotations

import re
import shlex

from awf.profiles.models import WorkspaceProfile
from awf.runtime.validation_coverage import _command_token_name
from awf.runtime.validation_types import ValidateCommandProbeTarget


def _shell_tokens(command: str, *, comments: bool = False) -> list[str] | None:
    try:
        return shlex.split(command, comments=comments)
    except ValueError:
        return None


_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")


def _first_non_assignment_token_index(tokens: list[str]) -> int:
    index = 0
    while index < len(tokens) and _ENV_ASSIGNMENT_RE.fullmatch(tokens[index]):
        index += 1
    return index


# Shell builtins / keywords whose leading token is not a PATH-resolvable
# executable. A ``validate`` command starting with one of these (``cd build``,
# ``:`` no-op, ``echo done``, a ``for``/``if`` compound) cannot be reduced to a
# single probeable tool, so the probe skips it (fail-open) rather than treat the
# keyword as a fake tool or emit a false UNPROVISIONED gap. Reserved words and
# builtins are included even where ``command -v`` happens to resolve them in a
# given shell: they are never the project toolchain the probe exists to check.
_VALIDATE_PROBE_SHELL_BUILTINS = frozenset(
    {
        # Builtins / simple-command leading tokens.
        "cd",
        "export",
        ":",
        ".",
        "source",
        "eval",
        "exec",
        "set",
        "true",
        "false",
        "test",
        "[",
        "echo",
        "exit",
        "pwd",
        "printf",
        "read",
        "return",
        "local",
        "command",
        "break",
        "continue",
        # Reserved words introducing compound commands.
        "if",
        "then",
        "elif",
        "else",
        "fi",
        "for",
        "while",
        "until",
        "do",
        "done",
        "case",
        "esac",
        "select",
        "function",
        "time",
        # Bash conditional-expression keywords. ``shlex.split`` keeps ``[[`` and
        # ``]]`` as standalone tokens, and ``command -v [[`` exits 1 in bash, so
        # a ``[[ -f pyproject.toml ]] && ...`` command must fail open rather than
        # report ``[[`` as a missing tool.
        "[[",
        "]]",
    }
)


def _assignment_prefix_sets_path(assignments: list[str]) -> bool:
    """True if any leading ``NAME=VALUE`` env assignment binds ``PATH``.

    A validate command such as ``PATH=/workspace/node_modules/.bin:$PATH eslint .``
    relies on its env prefix to make the executable resolvable. The batched
    ``command -v`` probe checks every tool under one shared default PATH and cannot
    replay a per-command PATH prefix, so such a command must fail open (yield no
    probe target) rather than be reported ``PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED``
    while the real command — which exports the assignment — would resolve the tool.
    """
    return any(assignment.split("=", 1)[0] == "PATH" for assignment in assignments)


def _resolve_env_wrapper(tokens: list[str], index: int) -> int | object:
    """Advance past an ``env`` wrapper to the program it will exec.

    ``index`` points at an ``env``/``/usr/bin/env`` token. Returns the index of the
    program ``env`` execs, having skipped env's leading ``NAME=VALUE`` assignments,
    so a common wrapper form like ``env PYTHONPATH=src pytest`` or
    ``/usr/bin/env pytest`` is probed for the real tool (``pytest``) rather than for
    ``env`` — whose presence is almost always satisfied, so an uninstalled
    ``pytest``/``ruff`` would otherwise slip past the handoff and die later in
    ``monitoring_pr`` instead of failing early as
    PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.

    Fails open with :data:`_STOP_PROBE` for wrapper forms the shared-PATH probe
    cannot faithfully replay: any option flag (``-i`` clears the environment — and
    thus ``PATH`` —, ``-u NAME``/``--chdir DIR`` consume a following value the
    parser cannot place) or an assignment that rebinds ``PATH``. Returns
    :data:`_SKIP_STATEMENT` when the wrapper names no program to exec (only
    assignments).
    """
    index += 1  # step past the ``env`` token itself
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("-"):
            return _STOP_PROBE
        if _ENV_ASSIGNMENT_RE.fullmatch(token):
            if token.split("=", 1)[0] == "PATH":
                return _STOP_PROBE
            index += 1
            continue
        return index
    return _SKIP_STATEMENT


# Leading shell *guard* commands configure the shell (error/option flags,
# resource limits) without naming a project tool, and conventionally prefix the
# real command in a sequence (``set -e; ruff check .``). The probe skips such a
# leading statement and looks at the next one so a required ``validate`` block
# opened by ``set -euo pipefail`` still yields a probe target for the tool that
# follows. Unlike ``cd`` — which changes the directory the tool resolves
# against and so stays fail-open — these never affect tool resolution, so
# advancing past them cannot make the probe target the wrong command.
_VALIDATE_PROBE_LEADING_GUARDS = frozenset({"set", "shopt", "umask", "ulimit"})


def _split_top_level_statements(command: str) -> list[tuple[str, str]]:
    """Split a shell command into ``(statement, terminator)`` pairs.

    Breaks on ``;``, newlines, ``&&`` and ``||`` that sit outside single/double
    quotes and are not backslash-escaped — the points where ``sh -lc`` would
    start a new command. Each pair carries the operator that *terminated* the
    statement so the caller can tell an AND-chain from an OR-list: ``";"`` for a
    ``;`` or newline, ``"&&"``, ``"||"``, or ``""`` for the trailing statement.
    Pipes (``|``) and background (``&``) are *not* split points, so a pipeline or
    job stays within one statement. :func:`_statement_leading_executable` then
    fails open on a top-level pipeline — under ``sh -lc`` (no ``pipefail``) only
    its last stage sets the exit status, so probing the leading tool would
    misreport a masked failure.

    A ``#`` that begins a word (at the start, or after unquoted whitespace)
    starts a comment that ``sh -lc`` ignores to end of line, so the splitter
    drops it *before* looking for operators — otherwise an operator inside a
    comment (``ruff check . # run lint && tests``) would be treated as a real
    terminator, and the fragment after it (``tests``) probed as a required
    executable, falsely reporting PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED even
    though the real command only runs ``ruff``. The newline that ends the
    comment is still honoured as a statement separator.

    A heredoc redirection (``python - <<'PY'`` ... ``PY``, ``psql <<SQL`` ...
    ``SQL``) feeds the lines that follow to the command as stdin: the real
    runner passes the whole string to ``sh -lc``, so the body and its closing
    delimiter are input, not new commands. The splitter therefore detects ``<<``
    delimiters and keeps the body (and each closing delimiter line) attached to
    the opener statement instead of splitting on its newlines — otherwise the
    delimiter word (``PY``/``SQL``) or a body line would be probed as a required
    executable and falsely report PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED even
    though the opener tool is installed. While a heredoc is still open, ``;``,
    ``&&`` and ``||`` on the opener line are kept literal so the opener tool
    stays the statement's leading token. ``<<<`` is a here-string (no body) and
    is left to ordinary splitting.
    """
    statements: list[tuple[str, str]] = []
    current: list[str] = []
    quote: str | None = None
    pending_heredocs: list[str] = []
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if quote is not None:
            current.append(char)
            if char == "\\" and quote == '"' and index + 1 < length:
                current.append(command[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            current.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            current.append(char)
            current.append(command[index + 1])
            index += 2
            continue
        if char == "<" and index + 1 < length and command[index + 1] == "<":
            if index + 2 < length and command[index + 2] == "<":
                # ``<<<`` is a here-string (no body); leave it to ordinary text.
                current.append("<<<")
                index += 3
                continue
            operator_start = index
            index += 2  # step past ``<<``
            if index < length and command[index] == "-":
                index += 1  # ``<<-`` strips leading tabs from the delimiter line
            while index < length and command[index] in " \t":
                index += 1
            delimiter_chars: list[str] = []
            if index < length and command[index] in "'\"":
                delimiter_quote = command[index]
                index += 1
                while index < length and command[index] != delimiter_quote:
                    delimiter_chars.append(command[index])
                    index += 1
                if index < length:
                    index += 1  # closing quote
            else:
                while index < length and command[index] not in " \t\n;&|<>()":
                    if command[index] == "\\" and index + 1 < length:
                        delimiter_chars.append(command[index + 1])
                        index += 2
                        continue
                    delimiter_chars.append(command[index])
                    index += 1
            current.append(command[operator_start:index])
            delimiter = "".join(delimiter_chars)
            if delimiter:
                pending_heredocs.append(delimiter)
            continue
        if char == "\n" and pending_heredocs:
            # The newline after a heredoc redirection begins the body the real
            # ``sh -lc`` feeds to the command. Attach the body and each closing
            # delimiter line to the current statement so the opener tool stays
            # the probe target instead of a body line or the delimiter word.
            current.append("\n")
            index += 1
            while pending_heredocs:
                delimiter = pending_heredocs.pop(0)
                while index < length:
                    end_of_line = command.find("\n", index)
                    if end_of_line == -1:
                        end_of_line = length
                    line = command[index:end_of_line]
                    current.append(line)
                    closes = line.strip() == delimiter
                    if end_of_line < length:
                        current.append("\n")
                        index = end_of_line + 1
                    else:
                        index = length
                    if closes:
                        break
            continue
        if char == "#" and (not current or current[-1] in " \t"):
            # ``#`` at a word boundary opens a comment ``sh -lc`` ignores to the
            # end of the line; skip its text (operators included) so it never
            # splits the statement. The terminating newline, if any, falls
            # through to the separator handling below.
            while index < length and command[index] != "\n":
                index += 1
            continue
        if char in ";\n" and not pending_heredocs:
            statements.append(("".join(current), ";"))
            current = []
            index += 1
            continue
        if (
            char in "&|"
            and not pending_heredocs
            and index + 1 < length
            and command[index + 1] == char
        ):
            statements.append(("".join(current), char * 2))
            current = []
            index += 2
            continue
        current.append(char)
        index += 1
    statements.append(("".join(current), ""))
    return statements


def _contains_top_level_pipe(statement: str) -> bool:
    """True if ``statement`` contains a pipe (``|``) outside quotes/escapes.

    The top-level splitter has already broken on ``;``/newline/``&&``/``||``, so a
    bare ``|`` that survives here joins a pipeline. Under ``sh -lc`` (no
    ``pipefail``) a pipeline's exit status is that of its *last* stage only, so a
    missing left-hand tool (``pytest -q | tee pytest.log`` with ``pytest`` off
    PATH) is masked by a succeeding final stage. Scanning the raw string —
    matching the quote/escape handling of :func:`_split_top_level_statements` —
    catches both the spaced (``a | b``) and the ``shlex``-glued (``a|b``) forms,
    which a token-level check would miss.
    """
    quote: str | None = None
    index = 0
    length = len(statement)
    while index < length:
        char = statement[index]
        if quote is not None:
            if char == "\\" and quote == '"' and index + 1 < length:
                index += 2
                continue
            if char == quote:
                quote = None
            index += 1
            continue
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "\\" and index + 1 < length:
            index += 2
            continue
        if char == "|":
            return True
        index += 1
    return False


# Per-statement classification outcomes for :func:`_statement_leading_executable`.
# ``_SKIP_STATEMENT`` -- the statement names no tool (a shell guard, or a
# blank/comment/env-only line); look at the next statement. ``_STOP_PROBE`` --
# the statement's leading token is un-probeable (parse failure, ``cd``/builtin,
# subshell, shell-expansion, or a ``PATH=`` prefix the shared-PATH probe cannot
# replay); stop walking and keep whatever earlier tools were collected so the
# probe never false-positives on a token it cannot reduce to a real executable.
_SKIP_STATEMENT = object()
_STOP_PROBE = object()


def _statement_leading_executable(statement: str) -> str | object:
    """Classify one top-level statement for the validate-tool probe.

    Returns the probeable program token a simple command would exec (``ruff`` for
    ``FOO=bar ruff check .``, ``python`` for ``python -m ruff``), or one of the
    :data:`_SKIP_STATEMENT` / :data:`_STOP_PROBE` sentinels. Tokenization strips
    shell comments (``comments=True``) because the real runner executes the
    command under ``sh -lc``, which ignores ``#`` comments — so ``# run lint`` on
    its own line is a skippable comment, not a literal ``#`` tool.
    """
    tokens = _shell_tokens(statement, comments=True)
    if tokens is None:
        return _STOP_PROBE
    if not tokens:
        # A blank or comment-only statement (e.g. the ``# run lint`` line above a
        # command) names no tool; skip it and look at the next.
        return _SKIP_STATEMENT
    if _contains_top_level_pipe(statement):
        # A top-level pipeline's exit status under ``sh -lc`` (no ``pipefail``) is
        # that of its *last* stage only, so a missing left-hand tool
        # (``pytest -q | tee pytest.log`` with ``pytest`` off PATH) is masked by a
        # succeeding final stage and the command still passes. Probing the leading
        # tool would falsely report PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED, so the
        # whole pipeline fails open — like a ``|| true``-masked OR-list member.
        # ``_SKIP_STATEMENT`` (not ``_STOP_PROBE``) because a pipe never changes how
        # a later statement resolves, so the walk keeps probing the rest.
        return _SKIP_STATEMENT
    index = _first_non_assignment_token_index(tokens)
    if _assignment_prefix_sets_path(tokens[:index]):
        return _STOP_PROBE
    if index >= len(tokens):
        # Only env assignments in this statement (``FOO=bar``); nothing to probe
        # here, so move on to the next statement.
        return _SKIP_STATEMENT
    # An ``env`` wrapper (``env PYTHONPATH=src pytest``, ``/usr/bin/env pytest``)
    # would otherwise make the probe target ``env`` itself — almost always present
    # — and let an unprovisioned ``pytest``/``ruff`` slip past the handoff. Unwrap
    # it (looping so a nested ``env env ...`` resolves) and probe the program env
    # execs, so the remaining subshell/expansion/builtin checks run against the
    # real command.
    while _command_token_name(tokens[index]) == "env":
        resolved = _resolve_env_wrapper(tokens, index)
        if resolved is _STOP_PROBE or resolved is _SKIP_STATEMENT:
            return resolved
        index = resolved  # type: ignore[assignment]
    leading = tokens[index]
    # A leading subshell opener (``(cd frontend && npm test)`` -> ``(cd``, or
    # ``( cd ... )`` -> ``(`` when spaced) is shell grouping the real runner
    # executes under ``sh -lc``. ``shlex`` keeps the ``(`` glued to the first
    # word, so probing ``command -v "(cd"`` would falsely report the workspace
    # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED and block monorepo profiles that
    # validate from a subdirectory. No real executable name begins with ``(``,
    # so fail open.
    if leading.startswith("("):
        return _STOP_PROBE
    # A leading token that relies on shell expansion to name the executable —
    # tilde expansion (``~/bin/ruff``) or parameter/command substitution
    # (``$HOME/.local/bin/ruff``, ``${HOME}/bin/ruff``, ``` `which ruff` ```) —
    # is expanded by the ``sh -lc`` the real runner uses, so the command
    # resolves. ``shlex`` keeps the literal ``~``/``$``/`` ` `` token, and the
    # probe passes it *quoted* to ``command -v "$t"``, where it is not
    # re-expanded, so probing it would falsely report the workspace
    # PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED. Fail open rather than probe a
    # token whose real value the shared-shell probe cannot reproduce.
    if leading.startswith("~") or "$" in leading or "`" in leading:
        return _STOP_PROBE
    if leading in _VALIDATE_PROBE_LEADING_GUARDS:
        # A shell-option/limit guard names no tool; skip to the next statement,
        # which is the command the guard protects.
        return _SKIP_STATEMENT
    if leading in _VALIDATE_PROBE_SHELL_BUILTINS:
        return _STOP_PROBE
    return leading


def _segment_leading_executables(statements: list[str]) -> tuple[list[str], bool]:
    """Collect the probeable leading tool of each statement in one AND-segment.

    A *segment* is the run of ``&&``-joined statements between two ``;``/newline
    boundaries. Each statement's leading tool is required because a ``&&`` chain
    propagates a non-zero exit (a missing tool's ``127`` included), so a later
    chained tool off PATH would fail the whole command. Returns the collected
    tools and whether an un-probeable statement *stopped* the walk, so the caller
    halts the overall walk and keeps whatever earlier tools were collected — the
    documented STOP semantics (parse failure, a non-guard builtin/keyword such as
    ``cd``, a subshell, a shell-expanded path, or a ``PATH=`` prefix the
    shared-PATH probe cannot replay).
    """
    tools: list[str] = []
    for statement in statements:
        outcome = _statement_leading_executable(statement)
        if outcome is _SKIP_STATEMENT:
            continue
        if outcome is _STOP_PROBE:
            return tools, True
        tools.append(outcome)  # type: ignore[arg-type]
    return tools, False


def _required_and_suffix(segment: list[tuple[str, str]]) -> list[str]:
    """Return the statements one ``;``-segment *requires* to succeed, in order.

    ``&&`` and ``||`` share precedence and associate left-to-right under
    ``sh -lc``, so ``ruff check . || true && mypy src`` parses as
    ``((ruff || true) && mypy)``: ``mypy`` always runs and its ``127`` fails the
    command, while ``ruff``'s failure is masked by the ``|| true`` arm. The
    required statements are therefore the maximal trailing run joined by ``&&``,
    found by scanning the inter-statement operators right-to-left and stopping at
    the first ``||``. A segment whose last operator is ``||``
    (``ruff check . && mypy src || true``) requires nothing — the trailing arm
    masks every preceding failure — so it fails open with an empty result. A
    single-statement segment (no operators) is always required.

    ``segment`` is the run of ``(statement, terminator)`` pairs between two
    ``;``/newline boundaries; the terminator of every pair but the last is the
    ``&&``/``||`` operator that *follows* that statement.
    """
    statements = [statement for statement, _ in segment]
    operators = [terminator for _, terminator in segment[:-1]]
    last = len(statements) - 1
    if last == 0:
        return [statements[0]]
    if operators[last - 1] != "&&":
        # The final statement is reached via ``||``; its arm masks every failure,
        # so nothing in the segment is required.
        return []
    required = [statements[last]]
    for index in range(last - 1, 0, -1):
        if operators[index - 1] != "&&":
            # A ``||`` to the left ends the guaranteed ``&&`` run; everything
            # further left is masked.
            required.reverse()
            return required
        required.append(statements[index])
    required.append(statements[0])
    required.reverse()
    return required


def _statement_enables_errexit(statement: str) -> bool:
    """True if ``statement`` is a ``set`` command that turns on ``errexit``.

    Recognises the short-flag bundles (``set -e``, ``set -euo pipefail``,
    ``set -ex``) and the long form (``set -o errexit``); ``set -o pipefail`` /
    ``set -u`` alone do not enable it. Once errexit is on, a ``;``/newline list
    aborts on the first failing command, so every following segment's leading
    tool becomes required (see :func:`_leading_executables`). Only *enabling* is
    detected — a later ``set +e`` is not modelled because validate commands do
    not toggle errexit back off in practice.
    """
    tokens = _shell_tokens(statement, comments=True)
    if not tokens or tokens[0] != "set":
        return False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "-o":
            if index + 1 < len(tokens) and tokens[index + 1] == "errexit":
                return True
            index += 2
            continue
        if token.startswith("-") and not token.startswith("--") and "e" in token[1:]:
            return True
        index += 1
    return False


def _segment_enables_errexit(segment: list[tuple[str, str]]) -> bool:
    """True if any statement in the ``&&``/``||``-joined segment enables errexit."""
    return any(_statement_enables_errexit(statement) for statement, _ in segment)


def _segment_is_executable(segment: list[tuple[str, str]]) -> bool:
    """True if the segment runs a command rather than nothing.

    A trailing ``;``/newline (a YAML block scalar's trailing newline,
    ``ruff check .;``) or a comment-only line yields a segment whose statements
    tokenize to nothing under ``sh -lc``; such a segment executes no command and
    so never sets the list's exit status. It must not be mistaken for the
    exit-determining final segment, which would otherwise leave the real command
    before it failing open. A parse-failing statement (``_shell_tokens`` ->
    ``None``) is treated as executable: the shell would still attempt it.
    """
    for statement, _ in segment:
        tokens = _shell_tokens(statement, comments=True)
        if tokens is None or tokens:
            return True
    return False


def _leading_executables(command: str) -> list[str]:
    """Return every PATH-resolvable executable a shell command *requires*, in order.

    Walks the command's top-level statements (``ruff check . && mypy src`` ->
    ``ruff check .`` then ``mypy src``) and collects the leading executable of
    each, so a single ``validate`` command that chains multiple tools with ``&&``
    is probed for *all* of them rather than only the first — otherwise a later
    chained tool that is off PATH slips past the adopt-PR handoff and dies later
    in ``monitoring_pr`` instead of failing early with
    PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED.

    A ``;``/newline-delimited list exits with the status of its *final* executed
    member under ``sh -lc`` (``ruff check .; pytest -q`` exits with ``pytest``'s
    status), so only that final segment determines whether the command fails.
    A non-final segment's failure — a missing tool's ``127`` included — is masked
    by the succeeding final segment, so its tools fail open, mirroring the
    top-level pipeline and ``|| true`` handling. The exception is ``set -e``
    (``set -euo pipefail``, ``set -o errexit``): once enabled it aborts the list
    on the first failure, making every following segment exit-determining, so
    each of their leading tools is required again. A trailing ``;``/newline or a
    comment-only line leaves a non-executing segment that never sets the exit
    status (see :func:`_segment_is_executable`), so the real command before it
    stays the exit-determining final segment.

    Within an exit-determining segment, ``&&`` and ``||`` share precedence and
    associate left-to-right, so only the maximal trailing ``&&`` run is
    guaranteed to execute (see :func:`_required_and_suffix`). Thus
    ``ruff check . || true && mypy src`` still probes ``mypy`` — it always runs —
    while the ``|| true``-masked ``ruff`` fails open, and a segment whose last
    operator is ``||`` (``ruff check . || true``, ``black --check . || mypy src``,
    ``ruff && mypy || true``) requires nothing: under ``sh -lc`` such a list exits
    non-zero only when *every* member fails, so a missing tool's ``127`` may be
    masked by a succeeding arm and the command still passes. Probing a masked
    member would falsely report PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED for a
    profile whose real validation passes, so the masked members are skipped.

    A leading shell *guard* statement (``set -e``) or a blank/comment-only
    statement names no tool and is skipped (see
    :func:`_statement_leading_executable`). An un-probeable leading token (parse
    failure, a non-guard builtin/keyword such as ``cd``, a subshell, a
    shell-expanded path, or a ``PATH=`` prefix the shared-PATH probe cannot
    replay) stops the walk and keeps whatever earlier tools were collected: the
    walk never probes a token it cannot reduce to a real executable, and a
    directory-changing ``cd`` ends collection because tools after it resolve
    against a different directory. Every segment is walked for this halt even when
    its tools fail open, so a masked non-final ``cd`` still ends collection for
    the tools that follow it. The result is empty when no statement yields a
    probeable tool.
    """
    segments: list[list[tuple[str, str]]] = []
    segment: list[tuple[str, str]] = []
    for statement, terminator in _split_top_level_statements(command):
        segment.append((statement, terminator))
        if terminator in (";", ""):
            segments.append(segment)
            segment = []
    last_executable = -1
    for index, current in enumerate(segments):
        if _segment_is_executable(current):
            last_executable = index

    tools: list[str] = []
    errexit = False
    for index, current in enumerate(segments):
        # Only the final executed segment sets the list's exit status unless
        # ``set -e`` is active, which makes every following segment determine it.
        exit_determining = errexit or index == last_executable
        required = _required_and_suffix(current)
        if required:
            segment_tools, stopped = _segment_leading_executables(required)
            if exit_determining:
                tools.extend(segment_tools)
            if stopped:
                # A resolution-changing/un-probeable token halts the walk even in
                # a masked segment: tools after it cannot be probed reliably.
                break
        if not errexit and _segment_enables_errexit(current):
            errexit = True
    return tools


def _leading_executable(command: str) -> str | None:
    """Return the first PATH-resolvable executable of a shell command, or None.

    Thin wrapper over :func:`_leading_executables` that returns the leading tool
    (or ``None`` when the command reduces to no probeable tool). Prefer
    :func:`_leading_executables` for probe targets so chained tools are not
    missed; this single-tool view is retained for callers that only need the
    head of the command.
    """
    tools = _leading_executables(command)
    return tools[0] if tools else None


def validate_command_probe_targets(
    profile: WorkspaceProfile,
) -> list[ValidateCommandProbeTarget]:
    """Return the deduped ``validate``-tool probe targets for a profile.

    One target per distinct executable across every command the validate phase
    actually executes, keeping the first command that introduced each tool so an
    operator message can name a representative command. A single command that
    chains multiple tools with ``&&`` (``ruff check . && mypy src``) contributes
    *every* chained tool, not just the first (see :func:`_leading_executables`),
    so a later chained tool that setup did not install fails the handoff early
    instead of slipping through to die during ``monitoring_pr`` validation.
    Commands whose leading token is un-probeable are skipped.

    The targets cover the profile's ``database.pre_validation_refresh`` hooks as
    well as its ``validate`` phase: :func:`profile_phase_command_plan` prepends
    the refresh hooks (``alembic upgrade head``, ``psql ...``) as required
    ``db_refresh`` gates whenever the validate phase runs, so a refresh tool that
    setup did not install would otherwise slip past this early probe and die
    ``127`` later during pre-push validation. Refresh targets come first, matching
    runtime execution order.

    The local coverage final gate is covered last, again matching runtime order:
    when ``validation.strategy.final_gate`` is ``coverage`` and
    ``validation.coverage.command`` is set, PR-monitor pre-push validation runs
    that command after the validate phase, so its toolchain (e.g. ``coverage`` for
    ``coverage run -m pytest``) must be probed too — otherwise an adopted PR whose
    setup forgot to install it slips past this handoff and dies ``127`` later in
    ``monitoring_pr`` instead of failing early with
    ``PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED``. The gate runs whenever those two
    fields are set (the ``required`` flag does not gate it), so the coverage
    command is probed regardless of its ``required`` flag.

    Advisory commands (``required: false``) are skipped too: ``ValidationRunner``
    records their non-zero/``127`` result without blocking validation, so a
    missing optional tool must not fail the adopt-PR handoff with
    ``PROFILE_VALIDATE_TOOLCHAIN_UNPROVISIONED`` before the required commands even
    run.
    """
    targets: list[ValidateCommandProbeTarget] = []
    seen: set[str] = set()

    def _collect(command: str) -> None:
        for tool in _leading_executables(command):
            if tool in seen:
                continue
            seen.add(tool)
            targets.append(ValidateCommandProbeTarget(tool=tool, command=command))

    for command in (
        *profile.database.pre_validation_refresh,
        *profile.phases.validate_commands,
    ):
        if not command.required:
            continue
        _collect(command.command)

    coverage_command = profile.validation.coverage.command
    if profile.validation.strategy.final_gate == "coverage" and coverage_command is not None:
        _collect(coverage_command.command)

    return targets
