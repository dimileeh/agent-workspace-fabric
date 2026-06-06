"""Shared constants and parsing helpers for the command-grammar drift suite.

T18 locks the public setup/start/init/smoke command grammar so future doc edits
cannot silently regress it. The substantive checks live in the sibling
``test_command_grammar_drift_part_*`` modules; this module holds the
``FIRST_RUN_DOCS`` corpus and the rule-engine helpers they share.

The grammar these parts enforce:

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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent

# Single source of truth for the first-run public docs the grammar contract
# spans. Read it in one place so a doc joining the first-run experience is added
# here once. `RELEASING.md` is the maintainer release runbook rather than a
# first-run page, but T18 scopes the release docs into the `awf init`/bootstrap
# grammar sweep (R2/R3) so a future stale no-path-init or service-bootstrap
# mention in the runbook is caught alongside README/quickstart/upgrade/uninstall/
# troubleshooting. Its deliberate *negative* "no-path `awf init`" prohibition is
# allowed (see `_without_init_prohibitions`) instead of being read as a command.
# `docs/MCP_CLIENT_PARITY.md` is the REST/CLI/MCP parity matrix; its first-run
# row documents the public `awf init <path>` command *surface*, so — like the
# release runbook — it is swept for the `awf init`/bootstrap grammar so a future
# edit cannot reintroduce the bare no-path spelling in that public surface table.
# `docs/MCP_REFERENCE.md` is intentionally *not* listed: its `awf init` mentions
# are prose cross-references ("the same onboarding writer as `awf init`"), not a
# command surface, and the inline R2 scan would mis-read each as a no-path command
# offender — so widening the sweep to it would manufacture false positives.
FIRST_RUN_DOCS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/MCP_SETUP.md",
    "docs/MCP_CLIENT_PARITY.md",
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
# `docs/PROJECT_ONBOARDING.md` is the dedicated `awf init <path>` onboarding
# guide (its First-run section calls init "the project-onboarding pass described
# on this page"), so it is the strongest init context of all and is tracked here
# — without it R2 would let that page drop every `awf init <path>` example with
# no offender and no missing-context entry to flag the regression.
INIT_CONTEXT_DOCS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/MCP_SETUP.md",
    "docs/PROJECT_ONBOARDING.md",
)

# First-run contexts that must each demonstrate at least one valid
# `awf smoke run --mocked-local` example (R4), the smoke analog of
# `INIT_CONTEXT_DOCS`. Tracked per-doc rather than via a single global counter so
# a page that silently drops every `--mocked-local` example fails on its own —
# `docs/UPGRADE.md`'s rerun step is the motivating case (a future editor removing
# it would otherwise get no CI signal as long as one mocked example survives
# anywhere across `FIRST_RUN_DOCS`). README/quickstart/getting-started teach the
# no-token mocked smoke as the first-run proof, and upgrade reruns it post-bump.
# The remaining first-run docs (mcp-setup/uninstall/troubleshooting/releasing and
# the onboarding page's project-free `awf smoke run --format pretty` proof) do not
# document a mocked-local smoke and so stay out.
MOCKED_SMOKE_CONTEXT_DOCS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/UPGRADE.md",
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
# Like the after-span sibling below, it is gated on an explicit prohibition
# lead-in (`do not`/`don't`/`never`/`avoid`) kept within the same clause (the
# `[^.`\n]` bridge stops `between` spanning a sentence break). That gate is
# essential: a *positive* reintroduction such as "Use no-path `awf init` for
# service setup" carries no lead-in, so it keeps its backticks and is still
# surfaced as a bare no-path R2 offender — ungated, the strip would unwrap that
# legacy guidance and let the very wording this drift test rejects pass. The
# qualifier sits before the span, but the bootstrap framing of R3's broad
# pattern (7) ("`awf init`…bootstraps…service") sits *after* it, so this form can
# still collide there — which is why R3's `_bootstrap_offenders` applies the same
# symmetric `_without_init_prohibitions` strip rather than only the after-span
# one. The sibling AFTER_SPAN_NO_PATH_INIT_PROHIBITION_RE below handles the
# qualifier-*after* wording. A path-bearing `awf init <path>` span has
# its backtick after the path, not after `init`, so it is left untouched.
# The lead-in carries a negative lookahead that rejects double-negative
# *reminders* such as "Do not forget to use no-path `awf init`": "do not forget
# to X" prescribes X, so it is legacy no-path guidance, not a prohibition, and
# must stay an R2 offender rather than be unwrapped.
NO_PATH_INIT_PROHIBITION_RE = re.compile(
    r"(?P<lead>do not|don't|never|avoid)(?!\s+"
    r"(?:forget|neglect|hesitate|fail|omit|skip|miss|overlook)\b)(?P<between>[^.`\n]*?)"
    r"(?P<qualifier>no[- ]path|without a path)\s+`awf init`",
    re.IGNORECASE,
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
# reintroduction. The lead-in also carries a negative lookahead rejecting
# double-negative *reminders* ("Do not forget to run `awf init` without a path to
# bootstrap"): "do not forget to X" prescribes X, so the strip must not unwrap it —
# otherwise R2 would stop extracting the backticked bare `awf init` and R3 would
# stop matching the "without a path" bootstrap text, waving prescriptive legacy
# no-path guidance through both scans.
AFTER_SPAN_NO_PATH_INIT_PROHIBITION_RE = re.compile(
    r"(?P<lead>do not|don't|never|avoid)(?!\s+"
    r"(?:forget|neglect|hesitate|fail|omit|skip|miss|overlook)\b)"
    r"(?P<between>[^.`\n]*?)`awf init`"
    r"(?P<qualifier>\s+(?:without a path|no[- ]path))",
    re.IGNORECASE,
)
# The R4 analog of the init prohibition above: a prose prohibition that backticks
# an `awf smoke run` span as the *disallowed* shape (e.g. a TROUBLESHOOTING note
# "Do not run `awf smoke run <path>` — pass `--project <path>` instead"). Because
# `_parse_smoke_invocation` parses each command segment anchored on a span that
# *begins with* `awf smoke run`, such a cautionary span would otherwise be read as
# a real bare-positional-path
# invocation and flagged as a spurious R4 offender. It is gated on the same
# `do not`/`don't`/`never`/`avoid` lead-in kept within the clause (the `[^.`\n]`
# bridge stops it crossing a sentence break), so a *positive* example such as
# "Run `awf smoke run --project <p> --mocked-local`" carries no lead-in, keeps its
# backticks, and is still scanned.
# Like the init prohibition siblings, the lead-in carries a negative lookahead that
# rejects double-negative *reminders* such as "Do not forget to run
# `awf smoke run <path>`": "do not forget to X" prescribes X, so it is prescriptive
# legacy bare-positional guidance, not a prohibition, and must stay an R4 offender
# rather than have its backticks unwrapped out of the inline scan.
SMOKE_RUN_PROHIBITION_RE = re.compile(
    r"(?P<lead>do not|don't|never|avoid)(?!\s+"
    r"(?:forget|neglect|hesitate|fail|omit|skip|miss|overlook)\b)(?P<between>[^.`\n]*?)"
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
    # Whether the example supplies a project path *outside* of `--project` — a bare
    # positional path or a `--demo-path <value>` (the CLI's "fallback project path").
    # A `--project <path>` example does *not* set this (ask `has_project` for that);
    # the field exists only to gate R4's missing-`--project` check, which is
    # conditional on an implicit project path: a project-free proof such as
    # `awf smoke run --mocked-local --format pretty` supplies no implicit path
    # (the CLI defaults `--project` to the cwd), so it must not be flagged.
    has_implicit_project_path: bool = False
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
    form (e.g. "Do not run ``awf init`` without a path") are unwrapped, and both
    are gated on a prohibition lead-in (``do not``/``don't``/``never``/``avoid``).
    The gate matters in *both* directions: a *positive* reintroduction such as
    "Use no-path ``awf init`` for service setup" carries no lead-in, so it keeps
    its backticks and stays an R2 offender — the exact legacy guidance this drift
    test must reject. An *unqualified* bare ``awf init`` likewise keeps its
    backticks and is surfaced as an R2 offender.
    """
    text = NO_PATH_INIT_PROHIBITION_RE.sub(r"\g<lead>\g<between>\g<qualifier> awf init", text)
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

# A shell output-redirection operator at the start of a token: an optional file
# descriptor (`2`) prefix followed by `>` or `>>`. `shlex.split` keeps these as
# ordinary characters, so a redirect arrives as its own token (`> init.log`,
# `2>`) or glued to its target (`>init.log`). A redirection and its target are
# not command arguments, so they must never be read as a path. Input redirection
# `<` is deliberately excluded: the docs use `<path>`/`<repo>` as placeholder
# arguments, so a leading `<` denotes a positional path here, not a redirect.
_SHELL_REDIRECT_RE = re.compile(r"^\d*>>?")


def _split_tail(tail: str) -> list[str]:
    try:
        tokens = shlex.split(tail, comments=True)
    except ValueError:
        # `shlex.split` choked on the line (e.g. an unclosed quote in a fence
        # such as `awf init "$HOME/proj`). Fall back to a plain whitespace
        # split, but still honour shell comment semantics: a `#` that begins a
        # word starts a comment that runs to end of line. Mirror `comments=True`
        # by truncating at the first such token. Without this, a trailing
        # `# bootstrap` annotation would survive as `['#', 'bootstrap']`, and
        # the bare `#` — which is not flag-like — would be miscounted by the
        # path scan as the required repo argument, silently relabelling a
        # no-path init as "ok" and hiding it from R2/R3.
        tokens = []
        for word in tail.split():
            if word.startswith("#"):
                break
            tokens.append(word)
    # Stop at the first shell operator so chained commands (e.g. `awf init &&
    # awf start`) don't contribute their continuation tokens to the argument
    # scan. `shlex.split` keeps `&&`/`|`/`;` as ordinary characters, so an
    # operator can arrive either as its own token (`... --yes ; awf start`) or
    # glued to a neighbour (`... --yes; awf start` → `--yes;`, `.;awf`). Splitting
    # each token on the operator characters keeps only the head before the first
    # separator, so the chained continuation command (e.g. `awf`) is never read
    # as a positional path/argument.
    #
    # Shell *redirections* are likewise not arguments: a documented no-path init
    # that merely captures output (`awf init --write-profile --yes > init.log`,
    # `... 2>&1`) would otherwise leave the operator (`>`, `2>`) and its target
    # file as non-flag tokens that the path scan reads as the required repo path,
    # so R2 would report the stale example as "ok". Drop each redirection
    # operator and — when the target is a separate token (a bare `>`/`2>` rather
    # than a glued `>init.log`) — its operand, so the redirect never stands in
    # for a path.
    result: list[str] = []
    skip_operand = False
    for token in tokens:
        if skip_operand:
            # The target file of a preceding bare redirection operator.
            skip_operand = False
            continue
        head = _SHELL_OPERATOR_RE.split(token, maxsplit=1)[0]
        redirect = _SHELL_REDIRECT_RE.match(head)
        if redirect is not None:
            # A bare operator (`>`, `2>`) consumes the next token as its target;
            # a glued form (`>init.log`) carries the target inline, so only the
            # operator token itself is dropped.
            skip_operand = redirect.group(0) == head
            # A redirect token that also carries a chained-command separator
            # (`>init.log&&awf`, where `head` is the redirect-bearing prefix and
            # the `&&awf` tail was split off) is a command boundary: stop here so
            # the continuation command is never scanned as a positional path,
            # mirroring the `head != token` break taken on the non-redirect path.
            if head != token:
                break
            continue
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


def _command_segment_starts(line: str) -> list[str]:
    """Return ``line`` plus the suffix beginning after each top-level separator.

    A documented one-liner often chains setup before the init, e.g.
    ``cd "$repo" && awf init .``. The start-anchored classifier
    (:func:`_classify_init_command`) only inspects a command at the *start* of
    the string it is handed, so feeding it the raw line would miss the ``awf
    init`` after ``&&`` entirely — :func:`re.match` returns ``None`` before the
    separator is reached. Yielding the whole line *and* the (left-stripped)
    remainder after every ``;``/``&``/``|`` lets that classifier see each chained
    command in turn, so a no-path init after a separator is still caught.

    The scan is quote-aware (single/double, mirroring
    :func:`_strip_shell_comment`) so an operator character *inside* a quoted
    path argument — ``awf init "$HOME/a&&b"`` — is treated as part of the path,
    not a command separator, and never manufactures a spurious extra segment.
    Each emitted suffix is left-stripped so the anchored classifier sees the
    command at offset zero; a suffix that does not begin with ``awf init`` (or a
    ``uv run`` prefix) simply classifies to ``None`` and is dropped.
    """
    starts = [line.lstrip()]
    in_single = in_double = False
    for index, char in enumerate(line):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char in ";&|" and not in_single and not in_double:
            starts.append(line[index + 1 :].lstrip())
    return starts


def _classify_init_command(segment: str) -> str | None:
    """Classify a single ``awf init`` command at the start of ``segment``.

    Returns ``"ok"`` when a path/repo argument follows, ``"help"`` when only a
    help flag follows (a legitimate non-offender that nonetheless demonstrates no
    path), ``"bare"`` when nothing follows, ``"flag-only"`` when only non-help
    flags follow (no path), or ``None`` when ``segment`` does not invoke ``awf
    init`` (e.g. ``awf service bootstrap`` or ``awf profile init``).

    ``"help"`` is kept distinct from ``"ok"`` so a help-only invocation is not
    flagged as a missing-path offender, yet also cannot stand in for the
    path-bearing example each first-run init context must demonstrate.

    The match is anchored at the start of ``segment`` (``re.match``, not
    ``re.search``), with a leading ``uv run ...`` runner prefix allowed before
    ``awf init`` so the documented ``uv run --python 3.12 --extra dev awf init
    .`` lane is still classified — mirroring :func:`_parse_smoke_invocation`.
    Anchoring keeps a prose backtick span that merely references the command
    mid-sentence (e.g. ``run awf init later`` or ``see awf init below``) from
    being parsed with the trailing prose token as a path and miscounted as a
    valid ``awf init <path>`` example: such a span neither begins with ``awf
    init`` nor with a ``uv run`` prefix, so it returns ``None``. An unbounded
    ``re.search`` here would let that prose hit silently satisfy R2's
    per-context "at least one valid example" gate. :func:`_init_arg_status` first
    splits the line into command segments (:func:`_command_segment_starts`) so an
    init *chained after* a separator is still reached despite this anchoring.
    """
    match = re.match(r"(?:uv\s+run\b.*?\s+)?awf init\b(?P<tail>.*)", segment)
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


def _init_arg_status(line: str) -> str | None:
    """Classify the ``awf init`` invocation(s) on a documented command line.

    The line is first split into shell command segments
    (:func:`_command_segment_starts`) so a chained one-liner such as
    ``cd "$repo" && awf init`` is scanned even though it does not begin with
    ``awf init`` — R2 enforces *every* documented init, not only the ones that
    open their line. Each segment is classified by :func:`_classify_init_command`
    and the worst status wins: an offender (``"bare"``/``"flag-only"``) anywhere
    on the line is reported over a valid ``"ok"`` example, and ``"help"`` ranks
    last so a line carrying a real path still counts as the example. ``None`` is
    returned only when no segment invokes ``awf init`` at all.
    """
    statuses = [
        status
        for segment in _command_segment_starts(line)
        if (status := _classify_init_command(segment)) is not None
    ]
    if not statuses:
        return None
    for offender in ("bare", "flag-only"):
        if offender in statuses:
            return offender
    return "ok" if "ok" in statuses else "help"


def _parse_smoke_segment(segment: str) -> SmokeInvocation | None:
    """Parse an ``awf smoke run`` command at the *start* of ``segment``.

    The match is anchored at the start of ``segment`` (``re.match``, not
    ``re.search``), but a leading ``uv run ...`` runner prefix is allowed before
    ``awf smoke run`` so the documented ``uv run --python 3.12 --extra dev awf
    smoke run ...`` lane (README/UPGRADE/QUICKSTART/GETTING_STARTED) is still
    classified — without it R4 silently skipped those lines while R2's
    segment-scanning ``_init_arg_status`` checked the sibling ``uv run ... awf
    init`` lane, leaving the mocked-smoke grammar on the ``uv run`` lanes able to
    regress undetected.

    Anchoring (rather than an unbounded ``re.search``) keeps a prose mention that
    merely references the command mid-sentence (e.g. ``the awf smoke run command``
    or ``see awf smoke run --mocked-local below``) from being parsed with the
    trailing prose as a positional path and flagged as a spurious ``bare positional
    path`` offender: such a segment neither begins with ``awf smoke run`` nor with a
    ``uv run`` runner prefix, so it returns ``None``. :func:`_parse_smoke_invocation`
    first splits the line into command segments (:func:`_command_segment_starts`) so
    a smoke run *chained after* a separator is still reached despite this anchoring.
    """
    match = re.match(r"(?:uv\s+run\b.*?\s+)?awf smoke run\b(?P<tail>.*)", segment)
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
        has_implicit_project_path=positional_path or has_demo_path,
        project_value_dropped=project_value_dropped,
    )


def _parse_smoke_invocation(line: str) -> SmokeInvocation | None:
    """Parse the ``awf smoke run`` invocation on a documented command line.

    The line is first split into shell command segments
    (:func:`_command_segment_starts`) so a chained one-liner such as
    ``cd "$repo" && awf smoke run "$HOME/p" --mocked-local`` is scanned even
    though it does not *begin* with ``awf smoke run`` — R4 enforces the smoke
    grammar on every documented invocation, not only the ones that open their
    line. Without this the anchored :func:`_parse_smoke_segment` returns ``None``
    before the separator is ever inspected, silently waving exactly the bare
    positional path R4 exists to reject past the gate, mirroring the same
    segment-scan R2's :func:`_init_arg_status` applies to ``awf init``.

    Each segment is parsed by :func:`_parse_smoke_segment` and, when a line chains
    more than one smoke run, an offending invocation (per
    :func:`_smoke_invocation_offense`) wins over a conforming one so the line is
    flagged rather than silently counting on the valid example — matching how
    ``_init_arg_status`` reports an offender anywhere on the line. ``None`` is
    returned only when no segment invokes ``awf smoke run`` at all.
    """
    invocations = [
        invocation
        for segment in _command_segment_starts(line)
        if (invocation := _parse_smoke_segment(segment)) is not None
    ]
    if not invocations:
        return None
    for invocation in invocations:
        if _smoke_invocation_offense(invocation) is not None:
            return invocation
    return invocations[0]


def _smoke_invocation_offense(invocation: SmokeInvocation) -> str | None:
    """Return why a smoke example violates R4, or ``None`` if it conforms.

    R4 requires a project-targeting `awf smoke run` example to pair
    `--project <path>` *with* `--mocked-local`. Enforcement is symmetric so a
    regression in either direction is caught: a bare positional path, a
    `--mocked-local` example that *references a project path* yet dropped
    `--project`, and a `--project` example that dropped `--mocked-local` are each
    offenders. The missing-`--project` check is conditional on an implicit project
    path actually being supplied (`has_implicit_project_path`): per the plan's
    contract it rejects "omits `--project` *when a project path is referenced in
    that example*", so a project-free proof like
    `awf smoke run --mocked-local --format pretty` (the CLI defaults `--project`
    to the cwd) is valid usage and is not flagged. Without the last branch a
    first-run doc could keep its project target but silently lose the no-token
    `--mocked-local` grammar.

    A `--project` that dropped its value (`awf smoke run --project --mocked-local
    ...`) is malformed grammar and is rejected *unconditionally* — independent of
    `has_implicit_project_path` — because the example explicitly wrote `--project`
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
        and invocation.has_implicit_project_path
    ):
        return "mocked smoke missing --project"
    if invocation.has_project and not invocation.has_mocked_local:
        return "project smoke missing --mocked-local"
    return None


def _bootstrap_offenders(text: str) -> list[str]:
    # A no-path `awf init` *prohibition* must not be read as a bootstrap
    # reintroduction: unwrap its backticks first so the R3 patterns (which key on
    # the backticked span) do not fire on the very wording that forbids the legacy
    # form. Use the same symmetric strip R2 applies (:func:`_without_init_prohibitions`)
    # so *both* phrasings are exempted: the after-span "Do not run `awf init`
    # without a path" *and* the before-span "Do not use no-path `awf init` to
    # bootstrap the local service". The before-span form would otherwise pass R2
    # (which unwraps it) yet still trip R3's broad `awf init`…bootstrap…service
    # pattern (7), since the bootstrap framing sits *after* the span even when the
    # no-path qualifier sits before it. The strip is gated on a prohibition lead-in
    # (and rejects double-negative "do not forget" reminders), so a prescriptive
    # "run `awf init` without a path to bootstrap" keeps its backticks and is still
    # flagged below.
    text = _without_init_prohibitions(text)
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
