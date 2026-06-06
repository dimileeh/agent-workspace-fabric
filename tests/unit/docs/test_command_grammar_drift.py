"""Focused drift contract for the first-run command grammar.

T18 locks the public setup/start/init/smoke command grammar so future doc edits
cannot silently regress it. The substantive checks live here as a single
contract (``FIRST_RUN_DOCS`` + rules R1-R5) rather than being scattered across
the larger lane-specific suites in ``test_public_docs_status.py``.

The grammar this module enforces:

* ``awf setup`` and ``awf start`` are the documented first-run entrypoints.
* ``awf init`` always takes a path/repo argument; bare ``awf init`` is gone.
* No first-run doc describes no-path ``awf init`` as *service bootstrap* (while
  the legitimate ``awf service bootstrap`` command stays allowed).
* Mocked smoke examples that target a project path use ``--project <path>``
  together with ``--mocked-local`` rather than a bare positional path.
* README + Quickstart present the four first-run install lanes and keep the
  public ``curl | bash`` installer release-gated.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent

# Single source of truth for the first-run public docs the grammar contract
# spans. Read it in one place so a doc joining the first-run experience is added
# here once. `RELEASING.md` is the maintainer release runbook rather than a
# first-run page, but T18 scopes the release docs into the `awf init`/bootstrap
# grammar sweep (R2/R3) so a future stale no-path-init or service-bootstrap
# mention in the runbook is caught alongside README/quickstart/upgrade/uninstall/
# troubleshooting. Its deliberate *negative* "no-path `awf init`" prohibition is
# allowed (see `_without_init_prohibitions`) instead of being read as a command.
FIRST_RUN_DOCS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/MCP_SETUP.md",
    "docs/UPGRADE.md",
    "docs/UNINSTALL.md",
    "docs/TROUBLESHOOTING.md",
    "docs/PROJECT_ONBOARDING.md",
    "RELEASING.md",
)

# Docs that must spell out the standalone setup/start entrypoints (R1). The
# remaining first-run docs are upgrade/uninstall/troubleshooting flows that
# reference the commands without re-teaching them.
SETUP_START_DOCS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/MCP_SETUP.md",
)

# First-run contexts that must each demonstrate at least one valid
# `awf init <path>` example (R2). T18 requires `awf init <repo>` examples in the
# right contexts, so these are tracked per-doc — a first-run page dropping its
# init snippet fails even if another doc still carries one. Upgrade/uninstall
# flows reference init without re-teaching onboarding, so they stay out.
INIT_CONTEXT_DOCS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/MCP_SETUP.md",
)

# Fenced blocks: capture the info string's leading language token so the command
# scanners only read shell/command examples. Non-shell fences (yaml/json/toml/
# text) hold config or sample output, not commands — feeding their lines to the
# grammar classifiers would manufacture spurious offenders (e.g. a YAML value
# containing `awf init` misread as a bare-init regression).
FENCE_RE = re.compile(r"^ {0,3}```(?P<lang>[^\s`]*)")
SHELL_FENCE_LANGS = frozenset({"", "bash", "sh", "shell", "console", "zsh"})
# Inline (single-backtick) code spans in prose / numbered steps. R2 scans these
# alongside fenced commands so a no-path `awf init` documented inline is caught.
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# A prose *prohibition* that explicitly labels a backticked `awf init` as the
# disallowed no-path form (qualifier-*before*, e.g. RELEASING.md's "Do not use
# no-path `awf init` for service setup"). Such a mention is teaching the reader
# *not* to run no-path init, so R2 must not read it as a no-path command example.
# The qualifier sits before the span, so this form never collides with R3's
# bootstrap-framing patterns (which key on `awf init` *followed by* "without a
# path"); the sibling AFTER_SPAN_NO_PATH_INIT_PROHIBITION_RE below handles the
# qualifier-*after* wording that does. A path-bearing `awf init <path>` span has
# its backtick after the path, not after `init`, so it is left untouched.
NO_PATH_INIT_PROHIBITION_RE = re.compile(
    r"(?P<qualifier>no[- ]path|without a path)\s+`awf init`", re.IGNORECASE
)
# The same prohibition written with the qualifier *after* the span — the natural
# release/troubleshooting wording "Do not run `awf init` without a path for
# service setup". Left untouched this phrasing would be (a) extracted by R2's
# inline scan as a bare `awf init` command and (b) matched by R3's "without a
# path" bootstrap pattern, so a doc could not forbid the legacy form in it. Unlike
# the before-span form it is gated on an explicit prohibition lead-in
# (`do not`/`don't`/`never`/`avoid`) kept within the same clause (the `[^.`\n]`
# bridge stops it spanning a sentence break before the code span). That gate makes
# unwrapping it safe even for R3: a *prescriptive* "run `awf init` without a path
# to bootstrap" carries no lead-in, keeps its backticks, and is still flagged as a
# reintroduction.
AFTER_SPAN_NO_PATH_INIT_PROHIBITION_RE = re.compile(
    r"(?P<lead>do not|don't|never|avoid)(?P<between>[^.`\n]*?)`awf init`"
    r"(?P<qualifier>\s+(?:without a path|no[- ]path))",
    re.IGNORECASE,
)
# The R4 analog of the init prohibition above: a prose prohibition that backticks
# an `awf smoke run` span as the *disallowed* shape (e.g. a TROUBLESHOOTING note
# "Do not run `awf smoke run <path>` — pass `--project <path>` instead"). Because
# `_parse_smoke_invocation` anchors on a span that *begins with* `awf smoke run`,
# such a cautionary span would otherwise be read as a real bare-positional-path
# invocation and flagged as a spurious R4 offender. It is gated on the same
# `do not`/`don't`/`never`/`avoid` lead-in kept within the clause (the `[^.`\n]`
# bridge stops it crossing a sentence break), so a *positive* example such as
# "Run `awf smoke run --project <p> --mocked-local`" carries no lead-in, keeps its
# backticks, and is still scanned.
SMOKE_RUN_PROHIBITION_RE = re.compile(
    r"(?P<lead>do not|don't|never|avoid)(?P<between>[^.`\n]*?)"
    r"`(?P<span>awf smoke run[^`\n]*)`",
    re.IGNORECASE,
)
# Flags that consume the following token as their value; everything else that
# starts with "-" is treated as a valueless flag. Covers every value-taking
# `awf smoke run` option (src/awf/cli/profile_smoke_commands.py): `--project`,
# `--format`, and `--demo-path` (the fallback project path). Omitting `--demo-path`
# made `_parse_smoke_invocation` misread the path following `--demo-path` as a bare
# positional, raising a spurious "bare positional path" R4 offense.
VALUE_FLAGS = frozenset({"--project", "--format", "--demo-path"})
# `awf init`'s own value-taking options (src/awf/cli/main.py). Tracked separately
# so `_init_arg_status` does not misread an option's value token as the required
# path: a no-path example like `awf init --template python --yes` must classify
# as "flag-only", not "ok". Covers the public `--template`/`--format` plus the
# hidden legacy bootstrap flags (`--provider`, `--timeout-seconds`,
# `--poll-interval-seconds`) that still consume a following token.
INIT_VALUE_FLAGS = VALUE_FLAGS | frozenset(
    {"--template", "--provider", "--timeout-seconds", "--poll-interval-seconds"}
)
# Help flags are always allowed after `awf init` even though they carry no path.
HELP_FLAGS = frozenset({"--help", "-h"})

# An ungated remote-script installer pipes a `curl` download straight into an
# interpreter (`curl ... | bash`/`| sh`/`| zsh`). R5 keys on that pipe-to-shell
# shape rather than on the bare `curl -fsSL` download flag: `curl -fsSL <url>` is
# a legitimate idiom for fetching a checksum or release asset, so matching the
# flag alone would false-positive on such prose (and on a cautionary note that
# explains why the public installer is release-gated). Matched per line so a
# `curl` on one line and an unrelated interpreter token on another never combine.
CURL_PIPE_INSTALLER_RE = re.compile(r"curl\b.*\|\s*(?:bash|sh|zsh)\b")

# Prose/snippet shapes that (re)introduce no-path `awf init` as machine setup.
# Keyed on `awf init` so the legitimate `awf service bootstrap` command is never
# matched on its own. The fenced-line pattern (last entry) requires an explicit
# `# ...bootstrap...` comment so R3 stays complementary to R2: a *bare* no-path
# `awf init` is R2's offender (`_init_arg_status` → "bare"), while R3 only fires
# when the snippet additionally frames that no-path init as service bootstrap.
INIT_AS_BOOTSTRAP_PATTERNS = (
    r"`awf init`\s+without a path",
    r"`awf init`\s+\(no path\)",
    r"`awf init`\s+or\s+`awf service bootstrap`",
    r"after `awf init` or `awf service bootstrap`",
    r"`awf init`\s+writes the local service environment",
    r"run `awf init` to verify prerequisites and bootstrap",
    r"`awf init`\.\s+With no arguments it bootstraps",
    r"`awf init`[^.\n]*\bbootstraps?\b[^.\n]*\b(?:local )?(?:service|core)\b",
    r"(?m)^\s*awf init\s*#.*bootstrap.*$",
)


@dataclass(frozen=True)
class SmokeInvocation:
    """Parsed view of a documented ``awf smoke run`` command line."""

    raw: str
    has_project: bool
    has_mocked_local: bool
    positional_path: bool
    # Whether the example references a project path at all — a bare positional path
    # or a `--demo-path <value>` (the CLI's "fallback project path"). R4's
    # missing-`--project` check is conditional on this: a project-free proof such
    # as `awf smoke run --mocked-local --format pretty` references no project path
    # (the CLI defaults `--project` to the cwd), so it must not be flagged.
    references_project_path: bool = False
    # Whether the example wrote `--project` (spaced or `=`-glued) but dropped its
    # value — the next token is another flag, the line ended, or the glued form is
    # empty. This is malformed grammar regardless of whether a project path is
    # otherwise referenced, so R4 flags it unconditionally (unlike the *conditional*
    # missing-`--project` check above): a project-free proof simply omits `--project`
    # entirely, it never writes a valueless `--project`.
    project_value_dropped: bool = False


def _read(rel_path: str) -> str:
    """Read a first-run doc, failing with a drift-specific message if it is gone.

    The R1-R4 sweeps read every entry in ``FIRST_RUN_DOCS`` (and the per-rule doc
    tuples) in a loop, so a raw ``FileNotFoundError`` would surface a bare
    filesystem path with no hint of which contract tuple to update. Converting it
    to a ``pytest.fail`` keeps the failure actionable — a renamed/removed doc
    points the reader straight at the tuple to fix.
    """
    path = REPO_ROOT / rel_path
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        pytest.fail(
            f"{rel_path}: first-run doc not found — update FIRST_RUN_DOCS "
            "(or the per-rule doc tuples) when a doc is renamed or removed."
        )


def _strip_shell_comment(line: str) -> str:
    """Drop a shell ``#`` comment from ``line``, respecting single/double quotes.

    A ``#`` only opens a comment at the start of a word (line start or after
    whitespace), matching shell tokenisation, so ``awf init`` arguments such as
    ``"$HOME/proj#1"`` are preserved. This stops the grammar classifiers from
    reading ``awf init`` mentioned inside a comment — a comment-only line like
    ``# use awf init later`` or a trailing ``awf start  # then awf init <path>``
    — and tokenising the prose after it as a fake path that would falsely count
    as a valid ``awf init <path>`` example (weakening R2's per-context tally).
    """
    in_single = in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif (
            char == "#"
            and not in_single
            and not in_double
            and (index == 0 or line[index - 1].isspace())
        ):
            return line[:index].rstrip()
    return line


def _fenced_command_lines(text: str) -> list[str]:
    """Return non-empty command lines inside shell ``` fences, markers removed.

    Only shell-tagged (or untagged) fences contribute lines; ``yaml``/``json``/
    ``toml``/``text`` fences carry config or sample output, so their lines are
    skipped to avoid manufacturing false offenders in the grammar classifiers.
    The ``$ ``/``> ``/``% `` prompt prefix is stripped from each collected line,
    and any trailing ``#`` shell comment is removed (via :func:`_strip_shell_comment`)
    so a comment that merely mentions ``awf init``/``awf smoke run`` never reaches
    the classifiers as a fake command line.

    Shell line-continuations are rejoined: a physical line ending in a trailing
    backslash is the head of a command split across lines, so it is held and glued
    to the next collected line before being emitted. Otherwise a multi-line example
    such as ``awf init`` + backslash then ``--write-profile --yes`` would reach
    :func:`_init_arg_status` with the dangling backslash as a lone token — read as
    the required path (any non-flag token is path-like) — waving the no-path init
    regression through as ``"ok"``.
    """
    lines: list[str] = []
    inside = False
    collecting = False
    pending = ""  # head of a command split across physical lines with a trailing `\`
    for raw_line in text.splitlines():
        fence = FENCE_RE.match(raw_line)
        if fence is not None:
            if inside:
                # A fence that closes mid-continuation flushes the accumulated head
                # so a dangling `awf init \` is still classified, not dropped.
                if pending:
                    lines.append(pending)
                    pending = ""
                inside = collecting = False
            else:
                inside = True
                collecting = fence.group("lang").lower() in SHELL_FENCE_LANGS
            continue
        if not collecting:
            continue
        stripped = raw_line.strip()
        if stripped.startswith(("$ ", "> ", "% ")):
            stripped = stripped[2:].strip()
        stripped = _strip_shell_comment(stripped)
        if not stripped:
            continue
        if pending:
            stripped = f"{pending} {stripped}"
            pending = ""
        if stripped.endswith("\\"):
            # Hold the continuation head (minus the trailing `\`) and join it with
            # the next physical line so the classifiers see the whole command.
            pending = stripped[:-1].rstrip()
            continue
        lines.append(stripped)
    if pending:
        lines.append(pending)
    return lines


def _strip_after_span_init_prohibition(text: str) -> str:
    """Unwrap a no-path ``awf init`` prohibition whose qualifier follows the span.

    Handles the natural wording "Do not run ``awf init`` without a path", which
    places the disallowed-form qualifier *after* the code span (the before-span
    sibling :data:`NO_PATH_INIT_PROHIBITION_RE` cannot see it). It is gated on an
    explicit prohibition lead-in (``do not``/``don't``/``never``/``avoid``) so it
    can be applied to R3's bootstrap scan as well as R2's inline scan without
    masking a *prescriptive* reintroduction ("run ``awf init`` without a path to
    bootstrap"), which carries no lead-in, keeps its backticks, and stays flagged.
    """
    return AFTER_SPAN_NO_PATH_INIT_PROHIBITION_RE.sub(
        r"\g<lead>\g<between>awf init\g<qualifier>", text
    )


def _without_init_prohibitions(text: str) -> str:
    """Unwrap the backticks of an explicitly no-path-qualified ``awf init``.

    A prohibition such as RELEASING.md's "Do not use no-path ``awf init`` for
    service setup" documents the *disallowed* no-path form so readers avoid it.
    Stripping only the inner backticks (leaving the prose qualifier and words
    intact) keeps that guidance readable while stopping R2's inline scan from
    extracting the span and miscounting a documented prohibition as a no-path
    command example. Both the qualifier-*before* form and the qualifier-*after*
    form (e.g. "Do not run ``awf init`` without a path") are unwrapped. An
    *unqualified* bare ``awf init`` still keeps its backticks and is surfaced as
    an R2 offender.
    """
    text = NO_PATH_INIT_PROHIBITION_RE.sub(r"\g<qualifier> awf init", text)
    return _strip_after_span_init_prohibition(text)


def _without_smoke_prohibitions(text: str) -> str:
    """Unwrap the backticks of a prohibited ``awf smoke run`` span.

    Mirrors :func:`_without_init_prohibitions` for R4: a prohibition such as "Do
    not run ``awf smoke run <path>`` directly" documents the *disallowed* shape, so
    stripping only the inner backticks (leaving the lead-in and prose intact) keeps
    the warning readable while stopping R4's inline scan from extracting the span
    and parsing it as a real bare-positional-path invocation. A positive example
    carries no prohibition lead-in, keeps its backticks, and is still scanned.
    """
    return SMOKE_RUN_PROHIBITION_RE.sub(r"\g<lead>\g<between>\g<span>", text)


def _inline_command_mentions(text: str) -> list[str]:
    """Return inline (single-backtick) code spans from prose, fences excluded.

    Several first-run docs document ``awf init`` as inline backticked commands in
    prose and numbered steps rather than only inside ``` fences. Those mentions
    must honour the same public grammar, so R2 feeds them through the same
    classifier. Fenced blocks are skipped here to avoid double-counting the lines
    already returned by :func:`_fenced_command_lines`.
    """
    mentions: list[str] = []
    inside = False
    for raw_line in text.splitlines():
        if FENCE_RE.match(raw_line):
            inside = not inside
            continue
        if inside:
            continue
        mentions.extend(match.group(1).strip() for match in INLINE_CODE_RE.finditer(raw_line))
    return mentions


# Operator characters that begin/are a shell command separator (`;`, `&`, `&&`,
# `|`, `||`). Split on the characters rather than the whole-word operators so a
# separator glued to a neighbouring token (`--yes;`, `.;awf`, `init&&awf`) is
# detected just like a spaced one.
_SHELL_OPERATOR_RE = re.compile(r"[;&|]")


def _split_tail(tail: str) -> list[str]:
    try:
        tokens = shlex.split(tail, comments=True)
    except ValueError:
        tokens = tail.split()
    # Stop at the first shell operator so chained commands (e.g. `awf init &&
    # awf start`) don't contribute their continuation tokens to the argument
    # scan. `shlex.split` keeps `&&`/`|`/`;` as ordinary characters, so an
    # operator can arrive either as its own token (`... --yes ; awf start`) or
    # glued to a neighbour (`... --yes; awf start` → `--yes;`, `.;awf`). Splitting
    # each token on the operator characters keeps only the head before the first
    # separator, so the chained continuation command (e.g. `awf`) is never read
    # as a positional path/argument.
    result: list[str] = []
    for token in tokens:
        head = _SHELL_OPERATOR_RE.split(token, maxsplit=1)[0]
        if head:
            result.append(head)
        if head != token:
            break
    return result


def _is_standalone(line: str, command: str) -> bool:
    """True if ``line`` invokes ``command`` as a standalone entrypoint.

    Tokenised comment-aware (``shlex.split(..., comments=True)``) so an annotated
    example — ``awf setup  # first-time setup`` — still counts as the bare
    entrypoint instead of triggering a spurious R1 failure. Trailing flags/args
    (``awf setup --x``) are intentionally *not* standalone, preserving R1's
    "documented as a standalone entrypoint" intent rather than relaxing it to any
    invocation.

    Unlike :func:`_split_tail` (which stops at the first shell operator so R2/R3
    don't read a chained continuation's tokens as init arguments), R1 must *not*
    truncate at operators: a chained line like ``awf setup && awf start`` is two
    commands, not a standalone ``awf setup``. Comparing the full tokenisation
    keeps such chained examples from satisfying R1 in place of a bare entrypoint.
    """
    try:
        tokens = shlex.split(line, comments=True)
    except ValueError:
        tokens = line.split()
    return tokens == command.split()


def _looks_pathlike(token: str) -> bool:
    # In these command lines any positional argument (a non-flag token, i.e. one
    # that does not start with "-" and is not a preceding flag's value) is a path
    # or repository. Treat every such token as path-like so bare names without a
    # slash or recognised prefix (e.g. ``my-project``) are still flagged.
    return not token.startswith("-")


def _init_arg_status(line: str) -> str | None:
    """Classify an ``awf init`` command line.

    Returns ``"ok"`` when a path/repo argument follows, ``"help"`` when only a
    help flag follows (a legitimate non-offender that nonetheless demonstrates no
    path), ``"bare"`` when nothing follows, ``"flag-only"`` when only non-help
    flags follow (no path), or ``None`` when the line does not invoke ``awf init``
    (e.g. ``awf service bootstrap`` or ``awf profile init``).

    ``"help"`` is kept distinct from ``"ok"`` so a help-only invocation is not
    flagged as a missing-path offender, yet also cannot stand in for the
    path-bearing example each first-run init context must demonstrate.

    The match is anchored at the start of ``line`` (``re.match``, not
    ``re.search``), with a leading ``uv run ...`` runner prefix allowed before
    ``awf init`` so the documented ``uv run --python 3.12 --extra dev awf init
    .`` lane is still classified — mirroring :func:`_parse_smoke_invocation`.
    Anchoring keeps a prose backtick span that merely references the command
    mid-sentence (e.g. ``run awf init later`` or ``see awf init below``) from
    being parsed with the trailing prose token as a path and miscounted as a
    valid ``awf init <path>`` example: such a span neither begins with ``awf
    init`` nor with a ``uv run`` prefix, so it returns ``None``. An unbounded
    ``re.search`` here would let that prose hit silently satisfy R2's
    per-context "at least one valid example" gate.
    """
    match = re.match(r"(?:uv\s+run\b.*?\s+)?awf init\b(?P<tail>.*)", line)
    if match is None:
        return None
    tokens = _split_tail(match.group("tail"))
    if not tokens:
        return "bare"
    if any(tok in HELP_FLAGS for tok in tokens):
        return "help"

    # Scan every token (not just the first) so a path that follows leading flags
    # — e.g. ``awf init --yes .`` — is still recognised. Skip flags and the value
    # of any value-taking flag (``INIT_VALUE_FLAGS``, not just the smoke subset)
    # so an option value such as the ``python`` in ``awf init --template python``
    # is never miscounted as the required path.
    has_path = False
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            if token in INIT_VALUE_FLAGS:
                skip_next = True
            continue
        has_path = True
        break

    return "ok" if has_path else "flag-only"


def _parse_smoke_invocation(line: str) -> SmokeInvocation | None:
    """Parse an ``awf smoke run`` command line, or ``None`` if it is not one.

    The match is anchored at the start of ``line`` (``re.match``, not
    ``re.search``), but a leading ``uv run ...`` runner prefix is allowed before
    ``awf smoke run`` so the documented ``uv run --python 3.12 --extra dev awf
    smoke run ...`` lane (README/UPGRADE/QUICKSTART/GETTING_STARTED) is still
    classified — without it R4 silently skipped those lines while R2's
    ``re.search``-based ``_init_arg_status`` checked the sibling ``uv run ... awf
    init`` lane, leaving the mocked-smoke grammar on the ``uv run`` lanes able to
    regress undetected.

    Anchoring (rather than an unbounded ``re.search``) still keeps a prose mention
    that merely references the command mid-sentence (e.g. ``the awf smoke run
    command`` or ``see awf smoke run --mocked-local below``) from being parsed with
    the trailing prose as a positional path and flagged as a spurious ``bare
    positional path`` offender: such a line neither begins with ``awf smoke run``
    nor with a ``uv run`` runner prefix, so it returns ``None``.
    """
    match = re.match(r"(?:uv\s+run\b.*?\s+)?awf smoke run\b(?P<tail>.*)", line)
    if match is None:
        return None
    tail = match.group("tail")
    tokens = _split_tail(tail)

    has_mocked_local = "--mocked-local" in tokens

    # `--project` is the path-to-smoke option (src/awf/cli/profile_smoke_commands.py),
    # so it only counts as satisfied when an actual path value follows. A bare
    # `--project` whose next token is another flag (e.g.
    # `awf smoke run --project --mocked-local ...`) dropped its path: it must not
    # set `has_project`, and must not swallow that following flag as if it were
    # the value. Instead it records `project_value_dropped` so R4 can reject the
    # malformed line outright — otherwise R4 would wave through invalid mocked
    # smoke guidance (the parser correctly keeps `has_project` false, but with no
    # project path otherwise referenced `_smoke_invocation_offense` would emit no
    # offense and silently pass it).
    has_project = False
    has_demo_path = False
    positional_path = False
    project_value_dropped = False
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--project="):
            if token[len("--project=") :]:
                has_project = True
            else:
                project_value_dropped = True
            continue
        if token == "--project":
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt is not None and not nxt.startswith("-"):
                has_project = True
                skip_next = True
            else:
                project_value_dropped = True
            continue
        # `--demo-path` is the CLI's "fallback project path", so its presence means
        # the example references a project path even without `--project`.
        if token.startswith("--demo-path="):
            if token[len("--demo-path=") :]:
                has_demo_path = True
            continue
        if token == "--demo-path":
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt is not None and not nxt.startswith("-"):
                has_demo_path = True
                skip_next = True
            continue
        if token.startswith("-"):
            if token in VALUE_FLAGS:
                skip_next = True
            continue
        if _looks_pathlike(token):
            positional_path = True

    return SmokeInvocation(
        raw=f"awf smoke run{tail}".strip(),
        has_project=has_project,
        has_mocked_local=has_mocked_local,
        positional_path=positional_path,
        references_project_path=positional_path or has_demo_path,
        project_value_dropped=project_value_dropped,
    )


def _smoke_invocation_offense(invocation: SmokeInvocation) -> str | None:
    """Return why a smoke example violates R4, or ``None`` if it conforms.

    R4 requires a project-targeting `awf smoke run` example to pair
    `--project <path>` *with* `--mocked-local`. Enforcement is symmetric so a
    regression in either direction is caught: a bare positional path, a
    `--mocked-local` example that *references a project path* yet dropped
    `--project`, and a `--project` example that dropped `--mocked-local` are each
    offenders. The missing-`--project` check is conditional on a project path
    actually being referenced (`references_project_path`): per the plan's
    contract it rejects "omits `--project` *when a project path is referenced in
    that example*", so a project-free proof like
    `awf smoke run --mocked-local --format pretty` (the CLI defaults `--project`
    to the cwd) is valid usage and is not flagged. Without the last branch a
    first-run doc could keep its project target but silently lose the no-token
    `--mocked-local` grammar.

    A `--project` that dropped its value (`awf smoke run --project --mocked-local
    ...`) is malformed grammar and is rejected *unconditionally* — independent of
    `references_project_path` — because the example explicitly wrote `--project`
    yet gave it no path. This is distinct from the conditional missing-`--project`
    check: a valid project-free proof omits `--project` outright, it never writes a
    valueless one.
    """
    if invocation.positional_path:
        return "bare positional path"
    if invocation.project_value_dropped:
        return "smoke --project missing value"
    if (
        invocation.has_mocked_local
        and not invocation.has_project
        and invocation.references_project_path
    ):
        return "mocked smoke missing --project"
    if invocation.has_project and not invocation.has_mocked_local:
        return "project smoke missing --mocked-local"
    return None


def _bootstrap_offenders(text: str) -> list[str]:
    # An after-span no-path `awf init` *prohibition* ("Do not run `awf init`
    # without a path") must not be read as a bootstrap reintroduction: unwrap its
    # backticks first so the R3 patterns (which key on the backticked span) do not
    # fire on the very wording that forbids the legacy form. The strip is gated on
    # a prohibition lead-in, so a prescriptive "run `awf init` without a path to
    # bootstrap" keeps its backticks and is still flagged below.
    text = _strip_after_span_init_prohibition(text)
    # Return the *matched snippet* (``match.group(0)``), not the raw regex
    # pattern, so an R3 failure message names the offending text a developer can
    # grep for instead of an opaque pattern string they would have to re-search
    # by hand.
    #
    # Several patterns overlap on purpose (e.g. the "without a path" form (0) and
    # the broader "...bootstraps the local service" form (7) both fire on "Run
    # `awf init` without a path to bootstrap Core."). Deduplicate by matched span
    # so one offending sentence yields one snippet, not one per pattern that
    # happens to hit it — otherwise the R3 failure message lists the same line
    # twice. Genuinely distinct offenses sit at non-overlapping spans, so they are
    # each still reported.
    #
    # Scan with ``re.finditer`` (not ``re.search``) so *every* match of a pattern
    # is collected, not just the first: a doc with two non-overlapping violations
    # of the same pattern (two paragraphs each framing a no-path `awf init` as
    # bootstrap) surfaces both, giving the full repair surface rather than masking
    # the second behind the first. The span-deduplication above still collapses
    # overlapping cross-pattern hits to a single snippet.
    offenders: list[str] = []
    seen_spans: list[tuple[int, int]] = []
    for pattern in INIT_AS_BOOTSTRAP_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            start, end = match.span()
            if any(start < seen_end and seen_start < end for seen_start, seen_end in seen_spans):
                continue
            seen_spans.append((start, end))
            offenders.append(match.group(0))
    return offenders


# --------------------------------------------------------------------------- #
# Helper-level tests (fixture-driven; red-on-stale before the real-doc sweeps).
# --------------------------------------------------------------------------- #


def test_helper_flags_bare_awf_init_command() -> None:
    assert _init_arg_status("awf init") == "bare"
    assert _init_arg_status("awf init  # bootstrap the service") == "bare"
    # A no-path init chained with another command stays "bare": the shell
    # operator and the continuation command must not be read as a path argument.
    assert _init_arg_status("awf init && awf start") == "bare"
    assert _init_arg_status("awf init ; awf start") == "bare"
    assert _init_arg_status("awf init | tee log") == "bare"
    # A separator glued to a neighbouring token (no surrounding spaces) must be
    # detected too, so a one-line chained form does not smuggle the continuation
    # command in as the required path.
    assert _init_arg_status("awf init;awf start") == "bare"
    assert _init_arg_status("awf init&&awf start") == "bare"
    assert _init_arg_status("awf init --write-profile --yes; awf start") == "flag-only"
    assert _init_arg_status("awf init --write-profile --yes;awf start") == "flag-only"
    # A real path before an attached separator still counts as a path.
    assert _init_arg_status("awf init .;awf start") == "ok"
    assert _init_arg_status("awf init --write-profile --yes") == "flag-only"
    # An option value (the `python` after `--template`) is not a path: a no-path
    # init written with a value-taking flag is still flag-only, not "ok".
    assert _init_arg_status("awf init --template python --write-profile --yes") == "flag-only"
    assert _init_arg_status("awf init --provider openai --yes") == "flag-only"
    assert _init_arg_status("awf init .") == "ok"
    # A real path that follows a value-taking flag and its value is still a path.
    assert _init_arg_status("awf init --template python .") == "ok"
    # A path that follows leading flags is still a path, not a flag-only line.
    assert _init_arg_status("awf init --yes .") == "ok"
    assert _init_arg_status("awf init --write-profile --yes <path>") == "ok"
    assert _init_arg_status("awf init <path>") == "ok"
    assert _init_arg_status('awf init "$HOME/awf-eval-project"') == "ok"
    # Help-only invocations are a distinct status: not a missing-path offender,
    # but not a path example a first-run init context can lean on either.
    assert _init_arg_status("awf init --help") == "help"
    assert _init_arg_status("awf init -h") == "help"
    # The legitimate lower-level command and project-init alias are not flagged.
    assert _init_arg_status("awf service bootstrap") is None
    assert _init_arg_status("awf profile init . --write") is None
    # A prose backtick span that merely references `awf init` mid-sentence is not
    # an invocation: it must return None rather than misreading the trailing prose
    # token as a path (which would inflate R2's per-context example count and let a
    # doc with no real `awf init <path>` example silently pass the gate). This
    # mirrors `_parse_smoke_invocation`'s start-anchored guard.
    assert _init_arg_status("run awf init later") is None
    assert _init_arg_status("see awf init below") is None
    # The documented `uv run ... awf init .` lane is a real command line, not a
    # prose reference, so the runner prefix is unwrapped and the path classified.
    assert _init_arg_status("uv run --python 3.12 --extra dev awf init .") == "ok"
    assert _init_arg_status("uv run --extra dev awf init") == "bare"
    assert _init_arg_status("uv run --extra dev awf init --write-profile --yes") == "flag-only"


def test_helper_read_missing_doc_fails_with_actionable_message() -> None:
    # A renamed/removed first-run doc must surface a drift-specific failure that
    # points at the contract tuple, not a raw FileNotFoundError with a bare path.
    with pytest.raises(pytest.fail.Exception, match="first-run doc not found"):
        _read("docs/__definitely_not_a_real_doc__.md")


def test_helper_smoke_parser_requires_command_at_span_start() -> None:
    # Only a span that *begins with* `awf smoke run` is parsed as an invocation, so
    # a prose backtick span that merely references the command mid-sentence is not
    # misread (with the trailing prose as a path) as a positional-path offender.
    assert _parse_smoke_invocation("the awf smoke run command verifies the stack") is None
    assert _parse_smoke_invocation("see awf smoke run --mocked-local below") is None
    # A span that does start with the command is still parsed as before.
    assert _parse_smoke_invocation("awf smoke run --mocked-local") is not None
    # The documented `uv run ... awf smoke run` lane is a real command line, not a
    # prose reference, so the runner prefix is unwrapped and the smoke grammar is
    # classified (mirroring how `_init_arg_status` checks the `uv run ... awf init`
    # lane). Otherwise the mocked-smoke grammar could silently regress on those
    # lanes without failing R4.
    uv_lane = _parse_smoke_invocation(
        "uv run --python 3.12 --extra dev awf smoke run --project <path> --mocked-local"
    )
    assert uv_lane is not None
    assert uv_lane.has_project is True
    assert uv_lane.has_mocked_local is True
    assert uv_lane.positional_path is False
    # A bare positional path on the `uv run` lane is still surfaced as an offender.
    uv_bare = _parse_smoke_invocation("uv run --extra dev awf smoke run /tmp/proj --mocked-local")
    assert uv_bare is not None
    assert uv_bare.positional_path is True
    assert uv_bare.has_project is False


def test_helper_curl_installer_pattern_targets_pipe_to_interpreter() -> None:
    # The actual grammar violation is piping a remote `curl` download into a shell.
    assert CURL_PIPE_INSTALLER_RE.search("curl -fsSL https://example.com/install.sh | bash")
    assert CURL_PIPE_INSTALLER_RE.search("curl -fsSL https://example.com/i | sh")
    # A bare `curl -fsSL` download (no pipe to an interpreter) is a legitimate
    # idiom — fetching a checksum or release asset — and must not be flagged, nor
    # should release-gating prose that merely names the curl installer lane.
    assert not CURL_PIPE_INSTALLER_RE.search("curl -fsSL https://example.com/SHA256SUMS -o sums")
    assert not CURL_PIPE_INSTALLER_RE.search("The public curl installer lane is release-gated")


def test_helper_extracts_awf_smoke_invocations() -> None:
    mocked = _parse_smoke_invocation(
        'awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty'
    )
    assert mocked == SmokeInvocation(
        raw='awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty',
        has_project=True,
        has_mocked_local=True,
        positional_path=False,
    )

    # The canonical example references no project path (it goes through `--project`,
    # not a bare positional), so `references_project_path` stays False.
    assert mocked is not None
    assert mocked.references_project_path is False

    bare_path = _parse_smoke_invocation('awf smoke run "$HOME/awf-eval-project" --mocked-local')
    assert bare_path is not None
    assert bare_path.has_project is False
    assert bare_path.positional_path is True
    # A bare positional path is itself a project-path reference.
    assert bare_path.references_project_path is True

    no_project_proof = _parse_smoke_invocation("awf smoke run --format pretty")
    assert no_project_proof is not None
    assert no_project_proof.positional_path is False
    assert no_project_proof.has_project is False
    # A project-free proof references no project path at all.
    assert no_project_proof.references_project_path is False

    # `--demo-path` (the CLI's fallback project path) references a project path even
    # without `--project` — both the spaced and `=`-glued forms.
    demo_only = _parse_smoke_invocation("awf smoke run --mocked-local --demo-path /tmp/demo")
    assert demo_only is not None
    assert demo_only.has_project is False
    assert demo_only.references_project_path is True
    glued_demo = _parse_smoke_invocation("awf smoke run --mocked-local --demo-path=/tmp/demo")
    assert glued_demo is not None
    assert glued_demo.references_project_path is True
    # A `--demo-path` that dropped its value (next token is another flag) is not a
    # project-path reference and does not swallow the following flag as its value.
    dropped_demo = _parse_smoke_invocation("awf smoke run --demo-path --mocked-local")
    assert dropped_demo is not None
    assert dropped_demo.references_project_path is False
    assert dropped_demo.has_mocked_local is True

    # A `--project` flag that drops its path value (the next token is another
    # flag) must not count as a satisfied `--project`, and must not swallow that
    # flag as the value — otherwise R4 would pass invalid mocked smoke guidance
    # like `awf smoke run --project --mocked-local --format pretty`. It is recorded
    # as `project_value_dropped` so R4 rejects the malformed line.
    dropped_value = _parse_smoke_invocation(
        "awf smoke run --project --mocked-local --format pretty"
    )
    assert dropped_value is not None
    assert dropped_value.has_project is False
    assert dropped_value.has_mocked_local is True
    assert dropped_value.positional_path is False
    assert dropped_value.project_value_dropped is True

    # A trailing `--project` (line ends with no value) is likewise a dropped value.
    trailing_project = _parse_smoke_invocation("awf smoke run --mocked-local --project")
    assert trailing_project is not None
    assert trailing_project.has_project is False
    assert trailing_project.project_value_dropped is True

    # The `=`-glued empty form (`--project=`) drops its value too.
    glued_empty = _parse_smoke_invocation("awf smoke run --project= --mocked-local")
    assert glued_empty is not None
    assert glued_empty.has_project is False
    assert glued_empty.project_value_dropped is True

    # `--demo-path` is a value-taking smoke option (the fallback project path),
    # so the path token following it must be consumed as the flag's value rather
    # than misread as a bare positional path — otherwise a documented
    # `awf smoke run --project <p> --mocked-local --demo-path <q>` line would raise
    # a spurious "bare positional path" R4 offense.
    demo = _parse_smoke_invocation(
        "awf smoke run --project /tmp/proj --mocked-local --demo-path /tmp/demo"
    )
    assert demo is not None
    assert demo.has_project is True
    assert demo.has_mocked_local is True
    assert demo.positional_path is False

    # A mocked smoke run chained with a follow-up command must not read the
    # shell operator (or the continuation command) as a positional path, which
    # would otherwise produce a spurious R4 failure.
    chained = _parse_smoke_invocation("awf smoke run --mocked-local && awf setup")
    assert chained is not None
    assert chained.has_mocked_local is True
    assert chained.positional_path is False

    assert _parse_smoke_invocation("awf service status --format pretty") is None


def test_helper_flags_smoke_invocation_offenses() -> None:
    def offense(line: str) -> str | None:
        invocation = _parse_smoke_invocation(line)
        assert invocation is not None
        return _smoke_invocation_offense(invocation)

    # The canonical project smoke example pairs `--project` with `--mocked-local`.
    assert offense('awf smoke run --project "$HOME/p" --mocked-local --format pretty') is None
    # A bare positional path is rejected regardless of the other flags.
    assert offense('awf smoke run "$HOME/p" --mocked-local') == "bare positional path"
    # A project-free `--mocked-local` proof references no project path (the CLI
    # defaults `--project` to the cwd), so it is valid usage and not an offender —
    # R4 is a conditional pairing rule, not an absolute one.
    assert offense("awf smoke run --mocked-local --format pretty") is None
    # But a `--mocked-local` example that *does* reference a project path — here via
    # `--demo-path`, the CLI's fallback project path — while dropping `--project` is
    # still rejected: a referenced project path must go through `--project`.
    assert (
        offense("awf smoke run --mocked-local --demo-path /tmp/demo")
        == "mocked smoke missing --project"
    )
    # The reverse direction the reviewer flagged: a `--project` example that
    # quietly drops `--mocked-local` must also be rejected so R4 enforces the
    # `--project <path>` + `--mocked-local` grammar symmetrically.
    assert (
        offense('awf smoke run --project "$HOME/p" --format pretty')
        == "project smoke missing --mocked-local"
    )
    # A `--project` that dropped its value is malformed grammar and is rejected
    # unconditionally — even when no project path is otherwise referenced, so the
    # conditional missing-`--project` exemption cannot let it slip through.
    assert (
        offense("awf smoke run --project --mocked-local --format pretty")
        == "smoke --project missing value"
    )


def test_helper_extracts_fenced_command_lines() -> None:
    text = "intro\n\n```bash\n$ awf setup\nawf start\n```\nprose `awf init` mention\n"
    assert _fenced_command_lines(text) == ["awf setup", "awf start"]


def test_helper_joins_backslash_continued_fenced_lines() -> None:
    # A shell line-continuation (`\`) splits one logical command across physical
    # lines; `_fenced_command_lines` must rejoin them so the grammar classifiers
    # see the whole command. Otherwise a stale no-path `awf init \` reaches
    # `_init_arg_status` with the dangling `\` as a lone token — and because any
    # non-flag token is read as the required path — it is misclassified "ok",
    # hiding the backslash-wrapped no-path init regression and even satisfying the
    # per-doc init example tally.
    no_path = "```bash\nawf init \\\n  --write-profile --yes\n```\n"
    assert _fenced_command_lines(no_path) == ["awf init --write-profile --yes"]
    assert _init_arg_status(_fenced_command_lines(no_path)[0]) == "flag-only"
    # A continuation whose path sits on the next physical line still resolves to a
    # valid path example once rejoined, so the join never false-flags a real one.
    with_path = "```bash\nawf init \\\n  /tmp/repo\n```\n"
    assert _fenced_command_lines(with_path) == ["awf init /tmp/repo"]
    assert _init_arg_status(_fenced_command_lines(with_path)[0]) == "ok"
    # A fence that closes while a continuation is still open flushes the dangling
    # head rather than dropping it, so the no-path form is still surfaced as bare.
    dangling = "```bash\nawf init \\\n```\n"
    assert _fenced_command_lines(dangling) == ["awf init"]
    assert _init_arg_status(_fenced_command_lines(dangling)[0]) == "bare"


def test_helper_drops_shell_comments_from_fenced_lines() -> None:
    # A comment-only line that merely mentions `awf init` in prose, and a trailing
    # `# ...` comment on a real command, must not reach the grammar classifiers:
    # otherwise the prose after `awf init` ("later"/"<path>") is tokenised as a
    # fake path and miscounted as a valid `awf init <path>` example, weakening R2.
    text = "```bash\n# use awf init later\nawf start  # then run awf init <path>\nawf init .\n```\n"
    assert _fenced_command_lines(text) == ["awf start", "awf init ."]
    # A `#` inside quotes is part of the argument, not a comment, so a path
    # containing `#` survives intact.
    quoted = '```bash\nawf init "$HOME/proj#1"\n```\n'
    assert _fenced_command_lines(quoted) == ['awf init "$HOME/proj#1"']


def test_helper_strips_zsh_prompt_in_zsh_fence() -> None:
    # ``zsh`` is a declared shell fence, so a ``%``-prefixed zsh prompt must be
    # stripped just like ``$ ``/``> `` — otherwise ``% awf setup`` would reach the
    # R1 classifier as a non-standalone literal and trip a false drift failure.
    text = "```zsh\n% awf setup\n% awf start\n```\n"
    assert _fenced_command_lines(text) == ["awf setup", "awf start"]


def test_helper_skips_non_shell_fenced_blocks() -> None:
    # Only shell-tagged (or untagged) fences feed the command scanners; a
    # yaml/json/text block that happens to contain an `awf` token must not
    # contribute a line that the classifiers would misread as an offender.
    text = (
        "```yaml\ncommand: awf init\n```\n"
        "```bash\nawf init .\n```\n"
        "```\nawf start\n```\n"
        "```text\nawf smoke run /tmp/proj\n```\n"
    )
    assert _fenced_command_lines(text) == ["awf init .", "awf start"]


def test_helper_standalone_command_ignores_inline_comment() -> None:
    # An annotated standalone entrypoint still counts as standalone (R1), while a
    # genuinely argument-bearing invocation does not.
    assert _is_standalone("awf setup", "awf setup")
    assert _is_standalone("awf setup  # first-time setup", "awf setup")
    assert _is_standalone("awf start", "awf start")
    assert not _is_standalone("awf setup --source-checkout .", "awf setup")
    assert not _is_standalone("awf service status", "awf start")
    # A chained line is two commands, not a standalone entrypoint: it must not
    # satisfy R1 in place of a bare `awf setup` / `awf start` line (operator
    # spaced, glued, or as the continuation head).
    assert not _is_standalone("awf setup && awf start", "awf setup")
    assert not _is_standalone("awf setup && awf start", "awf start")
    assert not _is_standalone("awf setup; awf start", "awf setup")
    assert not _is_standalone("awf setup&&awf start", "awf setup")


def test_helper_extracts_inline_command_mentions() -> None:
    text = (
        "Run `awf init <path>` to onboard.\n\n"
        "```bash\nawf setup\n```\n"
        "Then `awf start` and see `awf init .`.\n"
    )
    # Fenced `awf setup` is excluded (it comes back via _fenced_command_lines);
    # the inline mentions are returned in order so R2 can classify each.
    assert _inline_command_mentions(text) == ["awf init <path>", "awf start", "awf init ."]


def test_helper_excludes_no_path_init_prohibition_from_inline_scan() -> None:
    # A doc that *prohibits* no-path init (the release runbook's "Do not use
    # no-path `awf init` for service setup") must not have that warning read as a
    # no-path command example: the qualified mention is unwrapped before the R2
    # inline scan, while the positive `awf init <path>` example survives intact.
    text = (
        "Do not use no-path `awf init` for service setup; project onboarding is\n"
        "the separate `awf init <path>` flow after the local service is up.\n"
    )
    mentions = _inline_command_mentions(_without_init_prohibitions(text))
    assert "awf init" not in mentions
    assert "awf init <path>" in mentions
    # The "without a path" qualifier variant is handled the same way.
    variant = _without_init_prohibitions("Never run `awf init` without a path `awf init` here.")
    assert "without a path awf init" in variant
    # The same prohibition with the qualifier *after* the span — the natural
    # release/troubleshooting wording "Do not run `awf init` without a path" — is
    # unwrapped too, so R2's inline scan does not read it as a bare command, while
    # the positive `awf init <path>` example in the same sentence survives.
    after = _without_init_prohibitions(
        "Do not run `awf init` without a path for service setup; use `awf init <path>`."
    )
    after_mentions = _inline_command_mentions(after)
    assert "awf init" not in after_mentions
    assert "awf init <path>" in after_mentions
    # An *unqualified* bare `awf init` mention keeps its backticks and is still
    # surfaced as an offender, so the strip never weakens R2's core check.
    plain = "Run `awf init` to onboard the project.\n"
    assert "awf init" in _inline_command_mentions(_without_init_prohibitions(plain))


def test_helper_excludes_smoke_run_prohibition_from_inline_scan() -> None:
    # The R4 analog of the init prohibition handling: a doc that *warns against* a
    # bare-positional `awf smoke run <path>` (e.g. a TROUBLESHOOTING negation
    # example) must not have that warning parsed as a real invocation. The
    # prohibited span is unwrapped before the inline scan, so it never reaches
    # `_parse_smoke_invocation` as a spurious "bare positional path" offender,
    # while the positive `--project ... --mocked-local` example survives intact.
    text = (
        "Do not run `awf smoke run /tmp/proj` directly; instead run\n"
        "`awf smoke run --project /tmp/proj --mocked-local` so the stack is mocked.\n"
    )
    mentions = _inline_command_mentions(_without_smoke_prohibitions(text))
    offenders = [
        offense
        for line in mentions
        if (invocation := _parse_smoke_invocation(line)) is not None
        and (offense := _smoke_invocation_offense(invocation)) is not None
    ]
    assert offenders == []
    # The positive example is still scanned and recognised as a conforming
    # invocation, so the strip never blinds R4 to a real example.
    assert any(
        (invocation := _parse_smoke_invocation(line)) is not None and invocation.has_mocked_local
        for line in mentions
    )
    # Without the strip the cautionary span is misread as a bare-positional-path
    # offender — this is the spurious failure the guard prevents.
    raw_mentions = _inline_command_mentions(text)
    raw_offenders = [
        offense
        for line in raw_mentions
        if (invocation := _parse_smoke_invocation(line)) is not None
        and (offense := _smoke_invocation_offense(invocation)) is not None
    ]
    assert "bare positional path" in raw_offenders
    # An *unqualified* bare-positional example (no prohibition lead-in) keeps its
    # backticks and is still flagged, so the strip never weakens R4's core check.
    plain = "Run `awf smoke run /tmp/proj` to verify.\n"
    plain_mentions = _inline_command_mentions(_without_smoke_prohibitions(plain))
    plain_offenders = [
        offense
        for line in plain_mentions
        if (invocation := _parse_smoke_invocation(line)) is not None
        and (offense := _smoke_invocation_offense(invocation)) is not None
    ]
    assert "bare positional path" in plain_offenders


def test_helper_flags_no_path_init_as_bootstrap_prose() -> None:
    # Offenders are the *matched snippet*, not the raw regex pattern, so an R3
    # failure message names the offending text (a developer can grep for it)
    # rather than an opaque pattern. Each returned snippet must be literal text
    # drawn from the input, never a regex source string.
    prose = "Run `awf init` without a path to bootstrap Core."
    prose_offenders = _bootstrap_offenders(prose)
    assert prose_offenders
    assert all(snippet in prose for snippet in prose_offenders)
    fenced = "awf init  # bootstrap the local service"
    fenced_offenders = _bootstrap_offenders(fenced)
    assert fenced_offenders
    assert all(snippet in fenced for snippet in fenced_offenders)
    # An after-span no-path prohibition ("Do not run `awf init` without a path")
    # is the natural way a release/troubleshooting doc forbids the legacy form, so
    # it must not be read as a bootstrap reintroduction.
    assert _bootstrap_offenders("Do not run `awf init` without a path for service setup.") == []
    assert _bootstrap_offenders("Never run `awf init` without a path; pass a repo.") == []
    # The exemption is gated on the prohibition lead-in: a *prescriptive* reuse of
    # the same wording (no `do not`/`never`) is still flagged as a reintroduction,
    # and a sentence break between the lead-in and the span breaks the exemption so
    # a `do not` from an earlier sentence cannot smuggle a reintroduction through.
    assert _bootstrap_offenders("Run `awf init` without a path to bootstrap Core.")
    assert _bootstrap_offenders("Do not panic. Run `awf init` without a path to bootstrap.")
    # A single offending sentence is matched by several overlapping patterns (here
    # the "without a path" form and the broader "...bootstraps Core" form), but the
    # overlapping spans are deduplicated so it yields exactly one snippet rather
    # than one per pattern — the R3 failure message must not list the same line
    # twice.
    double_match = _bootstrap_offenders("Run `awf init` without a path to bootstrap Core.")
    assert len(double_match) == 1
    # Two genuinely distinct offenses sit at non-overlapping spans, so both are
    # still surfaced.
    two_offenses = _bootstrap_offenders(
        "Run `awf init` without a path here. Later try `awf init` (no path) there."
    )
    assert len(two_offenses) == 2
    # Two violations of the *same* pattern (both the "without a path" form, in
    # separate sentences) are each surfaced — the scan uses `re.finditer`, so the
    # second is not masked behind the first, giving the full repair surface.
    same_pattern = _bootstrap_offenders(
        "Run `awf init` without a path here. Then `awf init` without a path again."
    )
    assert len(same_pattern) == 2
    assert all(snippet == "`awf init` without a path" for snippet in same_pattern)
    # `awf service bootstrap` as a command must never be flagged.
    assert _bootstrap_offenders("Run `awf service bootstrap` to start Postgres.") == []
    assert _bootstrap_offenders("awf init .") == []
    assert _bootstrap_offenders('awf init "$HOME/awf-eval-project"') == []
    # R2 reads `awf init --help` as a non-offender (so help snippets don't trip a
    # false drift failure), but it must not count as the path example each
    # first-run init context owes: only a path-bearing status ("ok") does.
    assert _init_arg_status("awf init --help") not in (None, "bare", "flag-only")
    assert _init_arg_status("awf init --help") != "ok"
    # A *bare* no-path `awf init` (no bootstrap comment) is R2's offender, not
    # R3's: the fenced-line pattern requires an explicit `# ...bootstrap...`
    # comment, so R3 stays complementary instead of double-flagging the same
    # root cause that `_init_arg_status` already reports as "bare".
    assert _bootstrap_offenders("awf init") == []
    assert _init_arg_status("awf init") == "bare"


# --------------------------------------------------------------------------- #
# R1-R5: the real first-run docs honour the command grammar.
# --------------------------------------------------------------------------- #


def test_first_run_docs_use_setup_and_start_grammar() -> None:
    """R1: setup/start are documented as standalone entrypoints."""
    missing: list[str] = []
    for rel_path in SETUP_START_DOCS:
        commands = _fenced_command_lines(_read(rel_path))
        if not any(_is_standalone(line, "awf setup") for line in commands):
            missing.append(f"{rel_path}: missing standalone `awf setup`")
        if not any(_is_standalone(line, "awf start") for line in commands):
            missing.append(f"{rel_path}: missing standalone `awf start`")

    assert not missing, missing


def test_documented_awf_init_always_takes_a_path() -> None:
    """R2: every documented `awf init` command carries a path/repo arg, and each
    first-run init context demonstrates at least one valid example.

    Covers both fenced examples and inline backticked mentions in prose/list
    steps so a no-path regression in either shape is flagged. The per-context
    tally (not a single global counter) keeps the T18 requirement to show
    `awf init <repo>` in the right contexts enforced: a first-run page that drops
    its `awf init <path>` snippet fails even if another doc still carries one.
    """
    offenders: list[str] = []
    init_examples_by_context = dict.fromkeys(INIT_CONTEXT_DOCS, 0)
    for rel_path in FIRST_RUN_DOCS:
        text = _read(rel_path)
        # Inline mentions are scanned with explicit no-path prohibitions unwrapped
        # so a doc that *warns against* no-path `awf init` (e.g. the release
        # runbook) is not itself read as a no-path command example.
        inline = _inline_command_mentions(_without_init_prohibitions(text))
        for line in _fenced_command_lines(text) + inline:
            status = _init_arg_status(line)
            if status is None or status == "help":
                # `awf init --help`/`-h` carries no path but is a legitimate
                # invocation, so it is neither an offender nor a path example.
                continue
            if status != "ok":
                offenders.append(f"{rel_path}: `{line}` ({status})")
            elif rel_path in init_examples_by_context:
                init_examples_by_context[rel_path] += 1

    missing_contexts = sorted(
        rel_path for rel_path, count in init_examples_by_context.items() if count == 0
    )
    assert not offenders, offenders
    assert not missing_contexts, (
        f"first-run init contexts missing a valid `awf init <path>` example: {missing_contexts}"
    )


def test_no_path_init_is_not_described_as_service_bootstrap() -> None:
    """R3: no doc reintroduces no-path init-as-bootstrap; the real command stays."""
    offenders: list[str] = []
    bootstrap_command_docs = 0
    for rel_path in FIRST_RUN_DOCS:
        text = _read(rel_path)
        offenders.extend(f"{rel_path}: {snippet}" for snippet in _bootstrap_offenders(text))
        if "awf service bootstrap" in text:
            bootstrap_command_docs += 1

    assert not offenders, offenders
    # The legitimate lower-level command is still documented and not flagged.
    assert bootstrap_command_docs, "`awf service bootstrap` should remain documented."


def test_mocked_smoke_examples_use_project_flag() -> None:
    """R4: smoke examples targeting a project use `--project` + `--mocked-local`.

    Covers both fenced examples and inline backticked mentions in prose/list
    steps (e.g. the `awf smoke run --project ... --mocked-local` rerun step in
    docs/UPGRADE.md) so a bare-positional-path, missing-`--project`, or
    missing-`--mocked-local` regression is flagged in either shape, mirroring
    R2's fenced+inline scan. The `--project`/`--mocked-local` pairing is enforced
    symmetrically (see `_smoke_invocation_offense`) so a doc cannot keep a project
    target while quietly dropping the no-token `--mocked-local` grammar.
    """
    offenders: list[str] = []
    mocked_examples_seen = 0
    for rel_path in FIRST_RUN_DOCS:
        text = _read(rel_path)
        # Inline mentions are scanned with smoke-run prohibitions unwrapped so a
        # doc that *warns against* a bare-positional `awf smoke run <path>` (e.g. a
        # TROUBLESHOOTING negation example) is not itself read as an offending
        # invocation, mirroring R2's no-path init prohibition handling.
        inline = _inline_command_mentions(_without_smoke_prohibitions(text))
        for line in _fenced_command_lines(text) + inline:
            invocation = _parse_smoke_invocation(line)
            if invocation is None:
                continue
            if invocation.has_mocked_local:
                mocked_examples_seen += 1
            offense = _smoke_invocation_offense(invocation)
            if offense is not None:
                offenders.append(f"{rel_path}: {offense} in `{invocation.raw}`")

    assert not offenders, offenders
    assert mocked_examples_seen, "Expected at least one mocked `awf smoke run --project` example."


def test_first_run_install_lanes_present_and_curl_gated() -> None:
    """R5: README and Quickstart each present the four lanes and gate the curl lane.

    Each document is asserted independently rather than against a concatenation:
    these are two public first-run entry points, so dropping every lane marker
    from one must fail even if the sibling doc still carries them. The two
    source-checkout lanes are anchored on the distinctive install *command token*
    (`uv tool install . --force` vs `--extra dev awf`) rather than the section
    heading text, so a benign heading reword or capitalisation tweak does not trip
    a false "lane missing", while dropping the lane's actual install instruction
    still fails. The `uv run` lane token deliberately omits the pinned Python minor
    version (`--python 3.12`) so a docs bump to a newer interpreter does not report
    an intact lane as missing, while `awf` keeps it distinct from non-`awf`
    `uv run` commands. The expected markers are tracked per file so the
    per-document independence holds. The ungated-installer check keys on the
    pipe-to-interpreter shape (`curl ... | bash`, see `CURL_PIPE_INSTALLER_RE`)
    rather than the bare `curl -fsSL` download flag, so a legitimate non-installer
    `curl -fsSL` download or release-gating prose note does not false-positive.
    """
    expected_lane_markers = {
        "README.md": (
            "release-installed",
            "package-manager",
            "uv tool install . --force",
            "--extra dev awf",
            "release-gated",
        ),
        "docs/QUICKSTART.md": (
            "release-installed",
            "package-manager",
            "uv tool install . --force",
            "--extra dev awf",
            "release-gated",
        ),
    }

    missing: list[str] = []
    curl_offenders: list[str] = []
    for rel_path, markers in expected_lane_markers.items():
        text = _read(rel_path)
        missing.extend(
            f"{rel_path}: missing first-run lane marker {expected!r}"
            for expected in markers
            if expected not in text
        )
        if any(CURL_PIPE_INSTALLER_RE.search(line) for line in text.splitlines()):
            curl_offenders.append(f"{rel_path}: documents an ungated curl installer lane")

    assert not missing, missing
    assert not curl_offenders, curl_offenders
