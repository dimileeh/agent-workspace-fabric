"""Helper-level tests for the command-grammar drift rule engine.

These fixture-driven tests exercise the parsing helpers in isolation so the
real-doc sweeps (part 002) fail red on stale grammar rather than on a parser
bug. See ``_helpers`` for the shared corpus and rule engine.
"""

from __future__ import annotations

import pytest

from tests.unit.docs.command_grammar_drift_parts._helpers import (
    CURL_PIPE_INSTALLER_RE,
    SmokeInvocation,
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
    # An `awf init` chained *after* another command via a shell operator is still
    # scanned: the start-anchored classifier is applied to every command segment,
    # so a no-path init in a one-liner such as `cd "$repo" && awf init` is not
    # skipped just because the line does not *begin* with `awf init`. Without this
    # the anchored `re.match` returns None before the separator is ever inspected,
    # silently waving a no-path chained init past R2.
    assert _init_arg_status('cd "$repo" && awf init') == "bare"
    assert _init_arg_status('cd "$repo" && awf init --write-profile --yes') == "flag-only"
    assert _init_arg_status('cd "$repo" && awf init .') == "ok"
    assert _init_arg_status("cd repo ; awf init") == "bare"
    assert _init_arg_status("export AWF=1 | awf init .") == "ok"
    # The `uv run ... awf init` lane after a separator is unwrapped just the same.
    assert _init_arg_status("cd repo && uv run --extra dev awf init") == "bare"
    # A help-only init after a separator stays the distinct non-offender status.
    assert _init_arg_status("cd repo && awf init --help") == "help"
    # When a line documents *both* a valid and a no-path init, the offender wins so
    # the line is flagged rather than silently counting as a valid path example.
    assert _init_arg_status("awf init . && awf init") == "bare"
    # A shell operator *inside a quoted argument* is part of the path, not a command
    # separator, so it neither splits the command nor manufactures a bogus segment
    # whose trailing quote is misread as another invocation.
    assert _init_arg_status('awf init "$HOME/a&&b"') == "ok"
    assert _init_arg_status("awf init '$HOME/a&&b'") == "ok"
    assert _init_arg_status('echo "x; awf init" && awf init .') == "ok"
    # A shell redirection is not a path: a no-path init that merely captures its
    # output (`> init.log`, glued `>init.log`, `2>&1`) must stay an offender, not
    # be read as "ok" because the operator/target slipped in as a positional. A
    # real path before the redirect still counts.
    assert _init_arg_status("awf init --write-profile --yes > init.log") == "flag-only"
    assert _init_arg_status("awf init --write-profile --yes >init.log") == "flag-only"
    assert _init_arg_status("awf init --write-profile --yes 2>&1") == "flag-only"
    assert _init_arg_status("awf init --yes > init.log 2>&1") == "flag-only"
    assert _init_arg_status("awf init > init.log") == "bare"
    assert _init_arg_status("awf init . > init.log") == "ok"
    assert _init_arg_status("awf init . >init.log") == "ok"
    # When `shlex.split` raises (an unclosed quote glued to a flag value), the
    # fallback split must still strip a trailing `# comment`: otherwise the bare
    # `#`/`bootstrap` tokens slip in as the required path and a no-path init is
    # silently relabelled "ok", evading R2/R3.
    assert _init_arg_status('awf init --provider=" # bootstrap') == "flag-only"
    assert (
        _init_arg_status('awf init --yes " # bootstrap') == "ok"
    )  # the `"` token is a path-like positional


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

    # The canonical example supplies no *implicit* project path — its path goes
    # through `--project`, not a bare positional — so `has_implicit_project_path`
    # stays False (use `has_project` to ask whether `--project` itself was given).
    assert mocked is not None
    assert mocked.has_implicit_project_path is False

    bare_path = _parse_smoke_invocation('awf smoke run "$HOME/awf-eval-project" --mocked-local')
    assert bare_path is not None
    assert bare_path.has_project is False
    assert bare_path.positional_path is True
    # A bare positional path is itself an implicit project path.
    assert bare_path.has_implicit_project_path is True

    no_project_proof = _parse_smoke_invocation("awf smoke run --format pretty")
    assert no_project_proof is not None
    assert no_project_proof.positional_path is False
    assert no_project_proof.has_project is False
    # A project-free proof supplies no implicit project path at all.
    assert no_project_proof.has_implicit_project_path is False

    # `--demo-path` (the CLI's fallback project path) is an implicit project path even
    # without `--project` — both the spaced and `=`-glued forms.
    demo_only = _parse_smoke_invocation("awf smoke run --mocked-local --demo-path /tmp/demo")
    assert demo_only is not None
    assert demo_only.has_project is False
    assert demo_only.has_implicit_project_path is True
    glued_demo = _parse_smoke_invocation("awf smoke run --mocked-local --demo-path=/tmp/demo")
    assert glued_demo is not None
    assert glued_demo.has_implicit_project_path is True
    # A `--demo-path` that dropped its value (next token is another flag) is not an
    # implicit project path and does not swallow the following flag as its value.
    dropped_demo = _parse_smoke_invocation("awf smoke run --demo-path --mocked-local")
    assert dropped_demo is not None
    assert dropped_demo.has_implicit_project_path is False
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

    # An `awf smoke run` chained *after* a setup step via a shell operator is still
    # parsed: the start-anchored `_parse_smoke_segment` is applied to every command
    # segment, so a bare-positional smoke run in a one-liner such as
    # `cd "$repo" && awf smoke run "$HOME/p" --mocked-local` is not skipped just
    # because the line does not *begin* with `awf smoke run`. Without the segment
    # scan the anchored `re.match` returns None before the separator is inspected,
    # silently waving exactly the bare positional path R4 exists to reject past the
    # gate — mirroring `_init_arg_status`'s chained-init handling.
    after_sep = _parse_smoke_invocation('cd "$repo" && awf smoke run "$HOME/p" --mocked-local')
    assert after_sep is not None
    assert after_sep.positional_path is True
    assert after_sep.has_mocked_local is True
    # The `uv run ... awf smoke run` lane after a separator is unwrapped just the
    # same, and a conforming `--project` + `--mocked-local` example stays clean.
    after_sep_uv = _parse_smoke_invocation(
        "cd repo ; uv run --extra dev awf smoke run --project /p --mocked-local"
    )
    assert after_sep_uv is not None
    assert after_sep_uv.has_project is True
    assert after_sep_uv.has_mocked_local is True
    assert after_sep_uv.positional_path is False
    # A separator glued to a neighbouring token (no surrounding spaces) is split
    # too, so a one-line chained form does not hide the offending smoke run.
    glued_sep = _parse_smoke_invocation('cd "$repo"&&awf smoke run /tmp/proj --mocked-local')
    assert glued_sep is not None
    assert glued_sep.positional_path is True
    # When a line documents *both* a conforming and a bare-positional smoke run, the
    # offender wins so the line is flagged rather than counting on the valid example.
    both = _parse_smoke_invocation(
        "awf smoke run --project /p --mocked-local && awf smoke run /q --mocked-local"
    )
    assert both is not None
    assert _smoke_invocation_offense(both) == "bare positional path"

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
    # The "without a path" before-span qualifier variant is unwrapped the same
    # way, still gated on the prohibition lead-in.
    variant = _without_init_prohibitions("Never use without a path `awf init` here.")
    assert "without a path awf init" in variant
    # A *positive* no-path init reintroduction with no prohibition lead-in — the
    # exact legacy "Use no-path `awf init` for service setup" wording this drift
    # test must reject — keeps its backticks so R2's inline scan still surfaces it
    # as a bare no-path offender. Ungated, the before-span strip would unwrap this
    # and let the legacy guidance pass whenever the no-path qualifier precedes the
    # span without actually forbidding it.
    positive = "Use no-path `awf init` for service setup.\n"
    assert "awf init" in _inline_command_mentions(_without_init_prohibitions(positive))
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
    # A *before-span* no-path prohibition whose bootstrap framing follows the span
    # ("Do not use no-path `awf init` to bootstrap the local service") must be
    # exempted too: R2 already unwraps it, so R3 must not trip the broad
    # `awf init`…bootstrap…service pattern (7) on the very wording that forbids the
    # legacy form. R3 applies the same symmetric `_without_init_prohibitions` strip
    # as R2 so the before- and after-span phrasings agree.
    for before_span in (
        "Do not use no-path `awf init` to bootstrap the local service.",
        "Never use no-path `awf init` to bootstrap the local service.",
    ):
        assert _bootstrap_offenders(before_span) == []
        # R2 likewise reads it as a prohibition, not a no-path command example.
        assert "awf init" not in _inline_command_mentions(_without_init_prohibitions(before_span))
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


def test_helper_does_not_exempt_do_not_forget_init_reminder() -> None:
    # A double-negative *reminder* such as "Do not forget to run `awf init`
    # without a path to bootstrap Core" is prescriptive legacy no-path guidance,
    # not a prohibition: the `do not` lead-in is inverted by `forget` ("do not
    # forget to X" means "do X"). It must NOT be unwrapped, so both R2's inline
    # scan and R3's bootstrap scan still reject it. Without the inverting-verb
    # exclusion the strip swallowed the backticked span and the drift guard waved
    # the legacy no-path init through.
    reminder = "Do not forget to run `awf init` without a path to bootstrap Core."
    # R2: the backticked bare `awf init` is still surfaced as an inline mention.
    assert "awf init" in _inline_command_mentions(_without_init_prohibitions(reminder))
    # R3: the no-path-init-as-bootstrap framing is still flagged.
    assert _bootstrap_offenders(reminder)
    # The same applies to the other inverting reminder verbs and lead-ins.
    for variant in (
        "Don't forget to run `awf init` without a path to bootstrap.",
        "Never neglect to run `awf init` without a path to bootstrap.",
    ):
        assert "awf init" in _inline_command_mentions(_without_init_prohibitions(variant))
        assert _bootstrap_offenders(variant)
    # The before-span "no-path `awf init`" reminder is likewise not exempted by
    # R2's inline scan.
    before = "Do not forget to use no-path `awf init` for service setup."
    assert "awf init" in _inline_command_mentions(_without_init_prohibitions(before))
    # A genuine prohibition (no inverting verb) stays exempted exactly as before,
    # so the exclusion never weakens the legitimate "do not run no-path" guidance.
    genuine = "Do not run `awf init` without a path for service setup."
    assert "awf init" not in _inline_command_mentions(_without_init_prohibitions(genuine))
    assert _bootstrap_offenders(genuine) == []


def test_helper_does_not_exempt_do_not_forget_smoke_reminder() -> None:
    # The R4 analog of `test_helper_does_not_exempt_do_not_forget_init_reminder`:
    # a double-negative *reminder* such as "Do not forget to run
    # `awf smoke run /tmp/proj`" is prescriptive legacy bare-positional guidance,
    # not a prohibition ("do not forget to X" means "do X"). It must NOT be
    # unwrapped, so R4's inline scan still surfaces the backticked span and rejects
    # it as a bare-positional-path offender. Without the inverting-verb exclusion
    # the strip swallowed the span and the drift guard waved the legacy example
    # through.
    def smoke_offenders(text: str) -> list[str]:
        return [
            offense
            for line in _inline_command_mentions(_without_smoke_prohibitions(text))
            if (invocation := _parse_smoke_invocation(line)) is not None
            and (offense := _smoke_invocation_offense(invocation)) is not None
        ]

    reminder = "Do not forget to run `awf smoke run /tmp/proj` before release."
    assert "bare positional path" in smoke_offenders(reminder)
    # The same applies to the other inverting reminder verbs and lead-ins.
    for variant in (
        "Don't forget to run `awf smoke run /tmp/proj` first.",
        "Never neglect to run `awf smoke run /tmp/proj` here.",
    ):
        assert "bare positional path" in smoke_offenders(variant)
    # A genuine prohibition (no inverting verb) stays exempted exactly as before,
    # so the exclusion never weakens the legitimate "do not run bare smoke" warning.
    genuine = "Do not run `awf smoke run /tmp/proj` directly; pass `--project`."
    assert smoke_offenders(genuine) == []
