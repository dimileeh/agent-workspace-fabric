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
# here once.
FIRST_RUN_DOCS = (
    "README.md",
    "docs/QUICKSTART.md",
    "docs/GETTING_STARTED.md",
    "docs/MCP_SETUP.md",
    "docs/UPGRADE.md",
    "docs/UNINSTALL.md",
    "docs/TROUBLESHOOTING.md",
    "docs/PROJECT_ONBOARDING.md",
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
# Flags that consume the following token as their value; everything else that
# starts with "-" is treated as a valueless flag.
VALUE_FLAGS = frozenset({"--project", "--format"})
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


def _read(rel_path: str) -> str:
    return (REPO_ROOT / rel_path).read_text(encoding="utf-8")


def _fenced_command_lines(text: str) -> list[str]:
    """Return non-empty command lines inside shell ``` fences, markers removed.

    Only shell-tagged (or untagged) fences contribute lines; ``yaml``/``json``/
    ``toml``/``text`` fences carry config or sample output, so their lines are
    skipped to avoid manufacturing false offenders in the grammar classifiers.
    The ``$ ``/``> ``/``% `` prompt prefix is stripped from each collected line.
    """
    lines: list[str] = []
    inside = False
    collecting = False
    for raw_line in text.splitlines():
        fence = FENCE_RE.match(raw_line)
        if fence is not None:
            if inside:
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
        if stripped:
            lines.append(stripped)
    return lines


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


_SHELL_OPERATORS = frozenset({"&&", "||", "|", ";", "&"})


def _split_tail(tail: str) -> list[str]:
    try:
        tokens = shlex.split(tail, comments=True)
    except ValueError:
        tokens = tail.split()
    # Stop at the first shell operator so chained commands (e.g. `awf init &&
    # awf start`) don't contribute their continuation tokens to the argument
    # scan — `shlex.split` keeps `&&`/`|`/`;` as ordinary words, and a bare `&&`
    # would otherwise be mistaken for a positional path/argument.
    for index, token in enumerate(tokens):
        if token in _SHELL_OPERATORS:
            return tokens[:index]
    return tokens


def _is_standalone(line: str, command: str) -> bool:
    """True if ``line`` invokes ``command`` as a standalone entrypoint.

    Tokenised comment-aware (via :func:`_split_tail`) so an annotated example —
    ``awf setup  # first-time setup`` — still counts as the bare entrypoint
    instead of triggering a spurious R1 failure. Trailing flags/args
    (``awf setup --x``) are intentionally *not* standalone, preserving R1's
    "documented as a standalone entrypoint" intent rather than relaxing it to any
    invocation.
    """
    return _split_tail(line) == command.split()


def _looks_pathlike(token: str) -> bool:
    # In these command lines any positional argument (a non-flag token, i.e. one
    # that does not start with "-" and is not a preceding flag's value) is a path
    # or repository. Treat every such token as path-like so bare names without a
    # slash or recognised prefix (e.g. ``my-project``) are still flagged.
    return not token.startswith("-")


def _init_arg_status(line: str) -> str | None:
    """Classify an ``awf init`` command line.

    Returns ``"ok"`` when a path/repo argument follows, ``"bare"`` when nothing
    follows, ``"flag-only"`` when only flags follow (no path), or ``None`` when
    the line does not invoke ``awf init`` (e.g. ``awf service bootstrap`` or
    ``awf profile init``).
    """
    match = re.search(r"(?<![\w-])awf init\b(?P<tail>.*)", line)
    if match is None:
        return None
    tokens = _split_tail(match.group("tail"))
    if not tokens:
        return "bare"
    if any(tok in HELP_FLAGS for tok in tokens):
        return "ok"

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
    """Parse an ``awf smoke run`` command line, or ``None`` if it is not one."""
    match = re.search(r"awf smoke run\b(?P<tail>.*)", line)
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
    # the value — otherwise R4 would wave through invalid mocked smoke guidance.
    has_project = False
    positional_path = False
    skip_next = False
    for index, token in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--project="):
            if token[len("--project=") :]:
                has_project = True
            continue
        if token == "--project":
            nxt = tokens[index + 1] if index + 1 < len(tokens) else None
            if nxt is not None and not nxt.startswith("-"):
                has_project = True
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
    )


def _smoke_invocation_offense(invocation: SmokeInvocation) -> str | None:
    """Return why a smoke example violates R4, or ``None`` if it conforms.

    R4 requires a project-targeting `awf smoke run` example to pair
    `--project <path>` *with* `--mocked-local`. Enforcement is symmetric so a
    regression in either direction is caught: a bare positional path, a
    `--mocked-local` example that dropped `--project`, and a `--project` example
    that dropped `--mocked-local` are each offenders. Without the last branch a
    first-run doc could keep its project target but silently lose the no-token
    `--mocked-local` grammar.
    """
    if invocation.positional_path:
        return "bare positional path"
    if invocation.has_mocked_local and not invocation.has_project:
        return "mocked smoke missing --project"
    if invocation.has_project and not invocation.has_mocked_local:
        return "project smoke missing --mocked-local"
    return None


def _bootstrap_offenders(text: str) -> list[str]:
    return [
        pattern
        for pattern in INIT_AS_BOOTSTRAP_PATTERNS
        if re.search(pattern, text, flags=re.IGNORECASE)
    ]


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
    assert _init_arg_status("awf init --help") == "ok"
    # The legitimate lower-level command and project-init alias are not flagged.
    assert _init_arg_status("awf service bootstrap") is None
    assert _init_arg_status("awf profile init . --write") is None


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

    bare_path = _parse_smoke_invocation('awf smoke run "$HOME/awf-eval-project" --mocked-local')
    assert bare_path is not None
    assert bare_path.has_project is False
    assert bare_path.positional_path is True

    no_project_proof = _parse_smoke_invocation("awf smoke run --format pretty")
    assert no_project_proof is not None
    assert no_project_proof.positional_path is False
    assert no_project_proof.has_project is False

    # A `--project` flag that drops its path value (the next token is another
    # flag) must not count as a satisfied `--project`, and must not swallow that
    # flag as the value — otherwise R4 would pass invalid mocked smoke guidance
    # like `awf smoke run --project --mocked-local --format pretty`.
    dropped_value = _parse_smoke_invocation(
        "awf smoke run --project --mocked-local --format pretty"
    )
    assert dropped_value is not None
    assert dropped_value.has_project is False
    assert dropped_value.has_mocked_local is True
    assert dropped_value.positional_path is False

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
    # A `--mocked-local` proof that dropped `--project` is rejected.
    assert (
        offense("awf smoke run --mocked-local --format pretty") == "mocked smoke missing --project"
    )
    # The reverse direction the reviewer flagged: a `--project` example that
    # quietly drops `--mocked-local` must also be rejected so R4 enforces the
    # `--project <path>` + `--mocked-local` grammar symmetrically.
    assert (
        offense('awf smoke run --project "$HOME/p" --format pretty')
        == "project smoke missing --mocked-local"
    )


def test_helper_extracts_fenced_command_lines() -> None:
    text = "intro\n\n```bash\n$ awf setup\nawf start\n```\nprose `awf init` mention\n"
    assert _fenced_command_lines(text) == ["awf setup", "awf start"]


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


def test_helper_extracts_inline_command_mentions() -> None:
    text = (
        "Run `awf init <path>` to onboard.\n\n"
        "```bash\nawf setup\n```\n"
        "Then `awf start` and see `awf init .`.\n"
    )
    # Fenced `awf setup` is excluded (it comes back via _fenced_command_lines);
    # the inline mentions are returned in order so R2 can classify each.
    assert _inline_command_mentions(text) == ["awf init <path>", "awf start", "awf init ."]


def test_helper_flags_no_path_init_as_bootstrap_prose() -> None:
    assert _bootstrap_offenders("Run `awf init` without a path to bootstrap Core.")
    assert _bootstrap_offenders("awf init  # bootstrap the local service")
    # `awf service bootstrap` as a command must never be flagged.
    assert _bootstrap_offenders("Run `awf service bootstrap` to start Postgres.") == []
    assert _bootstrap_offenders("awf init .") == []
    assert _bootstrap_offenders('awf init "$HOME/awf-eval-project"') == []
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
        for line in _fenced_command_lines(text) + _inline_command_mentions(text):
            status = _init_arg_status(line)
            if status is None:
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
        offenders.extend(f"{rel_path}: {pattern}" for pattern in _bootstrap_offenders(text))
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
        for line in _fenced_command_lines(text) + _inline_command_mentions(text):
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
    per-document independence holds.
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
        if "curl -fsSL" in text or "curl | bash" in text or "curl | sh" in text:
            curl_offenders.append(f"{rel_path}: documents an ungated curl installer lane")

    assert not missing, missing
    assert not curl_offenders, curl_offenders
