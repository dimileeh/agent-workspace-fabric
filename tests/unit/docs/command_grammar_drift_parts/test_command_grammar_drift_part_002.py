"""R1-R5: the real first-run docs honour the command grammar.

These sweeps read the shipped docs through the shared rule engine in
``_helpers`` and fail when an edit regresses the locked setup/start/init/smoke
grammar.
"""

from __future__ import annotations

import pytest

from tests.unit.docs.command_grammar_drift_parts._helpers import (
    CURL_PIPE_INSTALLER_RE,
    FIRST_RUN_DOCS,
    INIT_CONTEXT_DOCS,
    SETUP_START_DOCS,
    _bootstrap_offenders,
    _fenced_command_lines,
    _init_arg_status,
    _inline_command_mentions,
    _is_standalone,
    _parse_smoke_invocation,
    _read,
    _smoke_invocation_offense,
    _without_init_prohibitions,
    _without_smoke_prohibitions,
)

pytestmark = pytest.mark.unit


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
    assert mocked_examples_seen, (
        "Expected at least one `awf smoke run --mocked-local` example across first-run docs."
    )


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
