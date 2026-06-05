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

FENCE_RE = re.compile(r"^ {0,3}```")
# Inline (single-backtick) code spans in prose / numbered steps. R2 scans these
# alongside fenced commands so a no-path `awf init` documented inline is caught.
INLINE_CODE_RE = re.compile(r"`([^`]+)`")
# Flags that consume the following token as their value; everything else that
# starts with "-" is treated as a valueless flag.
VALUE_FLAGS = frozenset({"--project", "--format"})
# Help flags are always allowed after `awf init` even though they carry no path.
HELP_FLAGS = frozenset({"--help", "-h"})

# Prose/snippet shapes that (re)introduce no-path `awf init` as machine setup.
# Keyed on `awf init` so the legitimate `awf service bootstrap` command is never
# matched on its own.
INIT_AS_BOOTSTRAP_PATTERNS = (
    r"`awf init`\s+without a path",
    r"`awf init`\s+\(no path\)",
    r"`awf init`\s+or\s+`awf service bootstrap`",
    r"after `awf init` or `awf service bootstrap`",
    r"`awf init`\s+writes the local service environment",
    r"run `awf init` to verify prerequisites and bootstrap",
    r"`awf init`\.\s+With no arguments it bootstraps",
    r"`awf init`[^.\n]*\bbootstraps?\b[^.\n]*\b(?:local )?(?:service|core)\b",
    r"(?m)^\s*awf init\s*(?:#.*bootstrap.*)?$",
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
    """Return non-empty command lines inside ``` fences, prompt markers removed."""
    lines: list[str] = []
    inside = False
    for raw_line in text.splitlines():
        if FENCE_RE.match(raw_line):
            inside = not inside
            continue
        if not inside:
            continue
        stripped = raw_line.strip()
        if stripped.startswith(("$ ", "> ")):
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


def _split_tail(tail: str) -> list[str]:
    try:
        return shlex.split(tail, comments=True)
    except ValueError:
        return tail.split()


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
    # of any value-taking flag, mirroring ``_parse_smoke_invocation``.
    has_path = False
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("-"):
            if token in VALUE_FLAGS:
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

    has_project = any(tok == "--project" or tok.startswith("--project=") for tok in tokens)
    has_mocked_local = "--mocked-local" in tokens

    positional_path = False
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
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
    assert _init_arg_status("awf init --write-profile --yes") == "flag-only"
    assert _init_arg_status("awf init .") == "ok"
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

    assert _parse_smoke_invocation("awf service status --format pretty") is None


def test_helper_extracts_fenced_command_lines() -> None:
    text = "intro\n\n```bash\n$ awf setup\nawf start\n```\nprose `awf init` mention\n"
    assert _fenced_command_lines(text) == ["awf setup", "awf start"]


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


# --------------------------------------------------------------------------- #
# R1-R5: the real first-run docs honour the command grammar.
# --------------------------------------------------------------------------- #


def test_first_run_docs_use_setup_and_start_grammar() -> None:
    """R1: setup/start are documented as standalone entrypoints."""
    missing: list[str] = []
    for rel_path in SETUP_START_DOCS:
        commands = set(_fenced_command_lines(_read(rel_path)))
        if "awf setup" not in commands:
            missing.append(f"{rel_path}: missing standalone `awf setup`")
        if "awf start" not in commands:
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
    """R4: smoke examples targeting a project use `--project` + `--mocked-local`."""
    offenders: list[str] = []
    mocked_examples_seen = 0
    for rel_path in FIRST_RUN_DOCS:
        for line in _fenced_command_lines(_read(rel_path)):
            invocation = _parse_smoke_invocation(line)
            if invocation is None:
                continue
            if invocation.positional_path:
                offenders.append(f"{rel_path}: bare positional path in `{invocation.raw}`")
            if invocation.has_mocked_local:
                mocked_examples_seen += 1
                if not invocation.has_project:
                    offenders.append(
                        f"{rel_path}: mocked smoke missing --project in `{invocation.raw}`"
                    )

    assert not offenders, offenders
    assert mocked_examples_seen, "Expected at least one mocked `awf smoke run --project` example."


def test_first_run_install_lanes_present_and_curl_gated() -> None:
    """R5: README and Quickstart each present the four lanes and gate the curl lane.

    Each document is asserted independently rather than against a concatenation:
    these are two public first-run entry points, so dropping every lane marker
    from one must fail even if the sibling doc still carries them. README's
    summary table and Quickstart's lane headers spell the two source-checkout
    lanes with different casing, so the expected markers are tracked per file.
    """
    expected_lane_markers = {
        "README.md": (
            "release-installed",
            "package-manager",
            "Source checkout with global tool install",
            "Source checkout with no global install",
            "release-gated",
        ),
        "docs/QUICKSTART.md": (
            "release-installed",
            "package-manager",
            "Source Checkout With Global Tool Install",
            "Source Checkout With No Global Install",
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
        if "curl -fsSL" in text or "curl | bash" in text:
            curl_offenders.append(f"{rel_path}: documents an ungated curl installer lane")

    assert not missing, missing
    assert not curl_offenders, curl_offenders
