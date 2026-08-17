"""Pre-push validation fix-pass salvage retention tests (part 009)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_added_salvage_blob_retained_rejects_mid_line_modified_occurrence() -> None:
    """Commenting out an added call must not count as retained salvage bytes.

    ``enable_guard()\\n`` is a contiguous substring of ``# enable_guard()\\n``, so
    raw containment would reuse stale addition evidence after the functional call
    was disabled (PRRT_kwDOSJAM6s6Zm6F1).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _added_salvage_blob_retained,
    )

    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="enable_guard()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="enable_guard()\nextra\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="prefix\nenable_guard()\n",
    )
    # Salvage that defines enable/disable helpers and ends with enable_guard()
    # must fail closed when a tip appends disable_guard(): call sites produce no
    # binding key, so assignment-only matching would retain stale FIXED evidence
    # (PRRT_kwDOSJAM6s6ZrJ3a).
    _guard_salvage = (
        "def enable_guard():\n    pass\ndef disable_guard():\n    pass\nenable_guard()\n"
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "disable_guard()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "disable_guard();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "await disable_guard()\n",
    )
    # Commented / unrelated call appends stay retained (behavior-neutral).
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "# disable_guard()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "// disable_guard()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "extra()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "note = 1\n",
    )
    # Tip-extra call inside a closed block comment is not executable → retain.
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "/*\ndisable_guard()\n*/\n",
    )
    # Same-line ``/* … */`` must also be non-executable (PRRT_kwDOSJAM6s6Zrhbs).
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "/* disable_guard() */\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_guard_salvage,
        head_blob=_guard_salvage + "code; /* disable_guard() */\n",
    )
    # Scoped salvage defs: tip-extra bare call to the leaf still supersedes.
    _scoped_guard_salvage = (
        "class Guards:\n"
        "    def enable_guard(self):\n"
        "        pass\n"
        "    def disable_guard(self):\n"
        "        pass\n"
        "Guards().enable_guard()\n"
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_scoped_guard_salvage,
        head_blob=_scoped_guard_salvage + "disable_guard()\n",
    )
    # Tip member call on a different receiver sharing only the method leaf must
    # not match scoped ``Guards.disable_guard`` via unpaired leaf collision
    # (PRRT_kwDOSJAM6s6ZrWwo).
    assert _added_salvage_blob_retained(
        commit_blob=_scoped_guard_salvage,
        head_blob=_scoped_guard_salvage + "other.disable_guard()\n",
    )
    # Member-call overrides: tip ``guard.disable()`` must supersede salvage that
    # bound ``guard`` and called ``guard.enable()``. Bare identifier-then-``(``
    # matching misses dotted receivers/callees and would retain stale FIXED
    # evidence (PRRT_kwDOSJAM6s6ZrSYE).
    _member_guard_salvage = "guard = Guard()\nguard.enable()\n"
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard.disable()\n",
    )
    # Multiline member continuations must preserve the receiver: per-line
    # scanning otherwise sees only a bare ``disable`` leaf and retains stale
    # FIXED evidence (PRRT_kwDOSJAM6s6ZuG-J).
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard\n  .disable();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard\n  ?.disable();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'guard\n  ["disable"]();\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard\n  .foo\n  .disable();\n",
    )
    # Unrelated multiline receiver still retains; unclassified continuation
    # (no resolvable receiver root) fails closed.
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "other\n  .disable();\n",
    )
    # Computed multiline emits only the bare receiver (no ``.`` in names);
    # that must not be treated as unclassified (PRRT_kwDOSJAM6s6ZuQ6c).
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'other\n  ["disable"]();\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "  .disable();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + '  ["disable"]();\n',
    )
    # Import-only receivers are not bindings: salvage ``from guards import guard``
    # + ``guard.enable()`` yields empty ``_last_binding_spans``, so call
    # candidates must come from salvage call sites or tip ``guard.disable()``
    # retains stale FIXED evidence (PRRT_kwDOSJAM6s6ZryCh).
    _import_guard_salvage = "from guards import guard\nguard.enable()\n"
    assert not _added_salvage_blob_retained(
        commit_blob=_import_guard_salvage,
        head_blob=_import_guard_salvage + "guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_import_guard_salvage,
        head_blob=_import_guard_salvage + 'guard["disable"]()\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_import_guard_salvage,
        head_blob=_import_guard_salvage + "# guard.disable()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_import_guard_salvage,
        head_blob=_import_guard_salvage + "other.disable()\n",
    )
    _import_module_salvage = "import guards\nguards.enable()\n"
    assert not _added_salvage_blob_retained(
        commit_blob=_import_module_salvage,
        head_blob=_import_module_salvage + "guards.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "await guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard.disable();\n",
    )
    # Nested / mid-expression calls must supersede too: statement-leading-only
    # matching misses ``if ready: guard.disable()`` and ``result = guard.disable()``
    # and would retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZrYJk).
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "if ready: guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "result = guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "x = await guard.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "print(guard.disable())\n",
    )
    # Commented / string-only / unrelated member calls stay retained.
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "# guard.disable()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "code  # guard.disable()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'x = "guard.disable()"\n',
    )
    # JS template literals: static content is non-executable; ``${...}`` stays
    # scannable so real interpolations still supersede (PRRT_kwDOSJAM6s6ZtJG8).
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "const marker = `guard.disable()`;\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "const marker = `${guard.disable()}`;\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'const marker = `${"guard.disable()"}`;\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "const marker = `${`x${guard.disable()}`}`;\n",
    )
    # Python f-strings: static text is non-executable; ``{...}`` replacement
    # fields stay scannable (PRRT_kwDOSJAM6s6Zt7Go). Without this, quote blanking
    # (or triple-quote blanking) swallows ``guard.disable()`` and retains stale
    # salvage; the alphanumeric quote heuristic must not be relied on.
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'marker = f"guard.disable()"\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'marker = f"{guard.disable()}"\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "marker = f'{guard.disable()}'\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'marker = rf"{guard.disable()}"\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'marker = f"""{guard.disable()}"""\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'marker = f"""guard.disable()"""\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "marker = f\"{'guard.disable()'}\"\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "marker = f\"{f'{guard.disable()}'}\"\n",
    )
    # Same-line block comments must not count as tip-extra calls
    # (PRRT_kwDOSJAM6s6Zrhbs).
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "/* guard.disable() */\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "code; /* guard.disable() */\n",
    )
    # Real call after a closed same-line block comment still supersedes.
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "/* note */ guard.disable()\n",
    )
    # JS regex literals must not count as tip-extra calls (PRRT_kwDOSJAM6s6Zs-Re).
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "const matcher = /guard.disable()/;\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "x = 1 / guard.disable() / 2\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "other.disable()\n",
    )
    # Optional-chain member / call forms must preserve the receiver: a regex
    # that restarts after ``?.`` reports only bare ``disable`` and misses the
    # salvaged ``guard`` binding, retaining stale FIXED evidence
    # (PRRT_kwDOSJAM6s6ZriaJ).
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard?.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "await guard?.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "if ready: guard?.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard?.()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "other?.disable()\n",
    )
    # Computed-member calls must preserve the receiver: blanking the quoted
    # property leaves ``guard[         ]()``, which dotted/_CALL_SITE_RE cannot
    # span, so salvage would retain stale FIXED evidence
    # (PRRT_kwDOSJAM6s6ZroRa).
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'guard["disable"]()\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard['disable']()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'await guard["disable"]()\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'if ready: guard["disable"]()\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'guard?.["disable"]()\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "guard[key]()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'other["disable"]()\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + '# guard["disable"]()\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "x = 'guard[\"disable\"]()'\n",
    )
    # Parenthesized receivers must preserve identity: grouping ``(guard).disable()``
    # restarts after ``)`` under plain dotted matching and reports only bare
    # ``disable``, which misses the salvaged ``guard`` binding and retains stale
    # FIXED evidence (PRRT_kwDOSJAM6s6Zrr7R).
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "(guard).disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "await (guard).disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "if ready: (guard).disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "print((guard).disable())\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "((guard)).disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "(guard)?.disable()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + '(guard)["disable"]()\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + '(guard)?.["disable"]()\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "(other).disable()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + "# (guard).disable()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob=_member_guard_salvage,
        head_blob=_member_guard_salvage + 'x = "(guard).disable()"\n',
    )
    # Appended rebinding of a salvage assignment must fail closed: the original
    # addition remains a line-aligned prefix, but the later assignment supersedes
    # it (PRRT_kwDOSJAM6s6Zp8jM).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED = False\n",
    )
    # Subscript assign targets must bind like bare/dotted names; otherwise
    # ``FLAGS["enabled"] = False`` after salvage ``FLAGS["enabled"] = True``
    # produces no key and prefix retention reuses stale FIXED evidence
    # (PRRT_kwDOSJAM6s6ZsQFs).
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS["enabled"] = False\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=("FLAGS = {}\nFLAGS[\"enabled\"] = True\nFLAGS['enabled'] = False\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nif ready: FLAGS["enabled"] = False\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS["enabled"] &= False\n'),
    )
    assert _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob='FLAGS = {}\nFLAGS["enabled"] = True\nother = 1\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nOTHER["enabled"] = False\n'),
    )
    # Compound assigns must supersede like plain ``=``; otherwise ``&=`` / ``+=``
    # / ``-=`` / ``|=`` / ``^=`` appends keep stale FIXED evidence
    # (PRRT_kwDOSJAM6s6ZsNCC). JS logical ``&&=`` / ``||=`` / ``??=`` must too
    # (PRRT_kwDOSJAM6s6ZyImG).
    for compound_line in (
        "FEATURE_ENABLED &= False\n",
        "FEATURE_ENABLED += 1\n",
        "FEATURE_ENABLED -= 1\n",
        "FEATURE_ENABLED |= True\n",
        "FEATURE_ENABLED ^= True\n",
        "FEATURE_ENABLED &&= False\n",
        "FEATURE_ENABLED ||= False\n",
        "FEATURE_ENABLED ??= False\n",
    ):
        assert not _added_salvage_blob_retained(
            commit_blob="FEATURE_ENABLED = True\n",
            head_blob="FEATURE_ENABLED = True\n" + compound_line,
        )
    # Dotted JS property logical assigns after salvage ``guard.enabled = true``
    # (PRRT_kwDOSJAM6s6ZyImG).
    for logical_line in (
        "guard.enabled &&= false\n",
        "guard.enabled ||= false\n",
        "guard.enabled ??= false\n",
    ):
        assert not _added_salvage_blob_retained(
            commit_blob="guard.enabled = true\n",
            head_blob="guard.enabled = true\n" + logical_line,
        )
    # Nested / mid-statement assignments must supersede too: the line-start
    # assign anchor misses ``if ready: FEATURE_ENABLED = False`` and would
    # retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZsD5y).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nif ready: FEATURE_ENABLED = False\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nif ready: FEATURE_ENABLED &= False\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nx = 1; FEATURE_ENABLED = False\n",
    )
    # Nested / mid-statement typed ``name: T =`` must bind ``name``, not the
    # type token ``T``. Omitting typed forms from the inline matcher left
    # ``if ready: FEATURE_ENABLED: bool = False`` recording ``bool`` (and the
    # semicolon form skipped ``bool`` without recovering ``FEATURE_ENABLED``),
    # so prefix retention reused stale FIXED evidence (PRRT_kwDOSJAM6s6Zs0s8).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\nif ready: FEATURE_ENABLED: bool = False\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\nx = 1; FEATURE_ENABLED: bool = False\n"),
    )
    # One-line ``class`` / ``def … -> T`` suite headers must not be typed-assign
    # recovery: binding ``C`` / ``T`` skipped the real target and reused FIXED
    # salvage (PRRT_kwDOSJAM6s6Zs-so).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nclass C: FEATURE_ENABLED = False\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\ndef f() -> T: FEATURE_ENABLED = False\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\nasync def f() -> T: FEATURE_ENABLED = False\n"),
    )
    # Nested typed assign after a class/def suite header still recovers the
    # annotated target (same nested-``:`` rule as ``if ready: name: T =``).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\nclass C: FEATURE_ENABLED: bool = False\n"),
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n# if ready: FEATURE_ENABLED = False\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=('FEATURE_ENABLED = True\nmsg = "if ready: FEATURE_ENABLED = False"\n'),
    )
    # Call kwargs / default parameters / typed-assign type tokens must not count
    # as inline rebinds; those phantoms would drop still-present FIXED salvage
    # (PRRT_kwDOSJAM6s6ZsJyZ).
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nconfigure(FEATURE_ENABLED=False)\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\nconfigure(timeout=30, FEATURE_ENABLED=False)\n"),
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\ndef helper(FEATURE_ENABLED=False):\n    pass\n"),
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\ndef helper(x, FEATURE_ENABLED=False):\n    pass\n"),
    )
    # JS/TS ``=>`` must not count as equals-style assign; ``=(?!=)`` matched the
    # ``=`` in ``=>``, so tip ``const fn = FEATURE_ENABLED => false;`` treated the
    # arrow parameter as a rebind and dropped retained FIXED salvage
    # (PRRT_kwDOSJAM6s6ZtZ_2).
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = true\n",
        head_blob=("FEATURE_ENABLED = true\nconst fn = FEATURE_ENABLED => false;\n"),
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = true\n",
        head_blob="FEATURE_ENABLED = true\nFEATURE_ENABLED => false;\n",
    )
    # Bare unpacking / parenthesized walrus after ``,`` / ``(`` are real rebinds,
    # not kwargs — tip-extra must still drop FIXED salvage (PRRT_kwDOSJAM6s6ZsOT0).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\na, FEATURE_ENABLED = get_flags()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED, other = get_flags()\n",
    )
    # Parenthesized / list unpacking: no ident immediately before ``=``, so the
    # bare comma-before path misses every target and would reuse FIXED salvage
    # (PRRT_kwDOSJAM6s6ZsZ5d).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n(FEATURE_ENABLED, other) = (False, 1)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n[FEATURE_ENABLED, other] = [False, 1]\n",
    )
    # JS object destructuring: ``{name} =`` / ``({name} = …)`` place ``}`` before
    # ``=``, so paren/list-only unpack scanners emit no binding and reuse FIXED
    # salvage (PRRT_kwDOSJAM6s6ZtZ_0).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = true\n",
        head_blob=("FEATURE_ENABLED = true\n({FEATURE_ENABLED} = {FEATURE_ENABLED: false});\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = true\n",
        head_blob="FEATURE_ENABLED = true\n{FEATURE_ENABLED} = {FEATURE_ENABLED: false};\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = true\n",
        head_blob="FEATURE_ENABLED = true\n({a, FEATURE_ENABLED} = obj);\n",
    )
    # Object-pattern keys are not bindings: ``{FEATURE_ENABLED: local}`` assigns
    # only ``local``. Treating the key as a rebind falsely drops FIXED salvage
    # (PRRT_kwDOSJAM6s6Zv4pl). Value-side targets still supersede.
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = true\n",
        head_blob="FEATURE_ENABLED = true\n({FEATURE_ENABLED: local} = source);\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = true\n",
        head_blob="FEATURE_ENABLED = true\n{FEATURE_ENABLED: local} = source;\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = true\n",
        head_blob="FEATURE_ENABLED = true\n({a: FEATURE_ENABLED} = source);\n",
    )
    # Starred / trailing-comma paren-list unpack must bind too; plain-target
    # bodies miss these and keep FIXED salvage (PRRT_kwDOSJAM6s6ZsfLc).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n(FEATURE_ENABLED, *rest) = (False, 1, 2)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n(*rest, FEATURE_ENABLED) = (1, 2, False)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n(FEATURE_ENABLED, other,) = (False, 1)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n(FEATURE_ENABLED,) = (False,)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n[FEATURE_ENABLED, *rest] = [False, 1, 2]\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED, *rest = get_flags()\n",
    )
    # Nested paren/list unpacking: flat body regex misses inner (...)/[...]
    # items, so no binding is recorded and FIXED salvage is reused
    # (PRRT_kwDOSJAM6s6ZsnYi).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\n(other, (FEATURE_ENABLED, rest)) = (1, (False, 2))\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\n((FEATURE_ENABLED, rest), other) = ((False, 2), 1)\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\n[other, [FEATURE_ENABLED, rest]] = [1, [False, 2]]\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\n(other, [FEATURE_ENABLED, rest]) = (1, [False, 2])\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=(
            'FLAGS = {}\nFLAGS["enabled"] = True\n(FLAGS["enabled"], other) = get_flags()\n'
        ),
    )
    # Subscript priors in unpacking must bind too; bare/dotted-only prior
    # recovery misses ``FLAGS["enabled"], other =`` and keeps FIXED evidence
    # (PRRT_kwDOSJAM6s6ZsYZx).
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nFLAGS["enabled"], other = get_flags()\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=(
            "FLAGS = {}\nFLAGS[\"enabled\"] = True\nFLAGS['enabled'], other = get_flags()\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\nother, FLAGS["enabled"] = get_flags()\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n(FEATURE_ENABLED := False)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nx = (FEATURE_ENABLED := False)\n",
    )
    # ``++`` / ``--`` update expressions mutate a salvage binding without a
    # rebind or call site; neither assign scanner nor call scanner recorded the
    # target, so prefix retention reused stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zs-Rb).
    for update_line in (
        "retryBudget++\n",
        "retryBudget--\n",
        "++retryBudget\n",
        "--retryBudget\n",
    ):
        assert not _added_salvage_blob_retained(
            commit_blob="retryBudget = 2\n",
            head_blob="retryBudget = 2\n" + update_line,
        )
    assert not _added_salvage_blob_retained(
        commit_blob="retryBudget = 2\n",
        head_blob="retryBudget = 2\nif (ready) retryBudget--\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="obj.count = 1\n",
        head_blob="obj.count = 1\nobj.count++\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = 1\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = 1\nFLAGS["enabled"]++\n'),
    )
    assert _added_salvage_blob_retained(
        commit_blob="retryBudget = 2\n",
        head_blob="retryBudget = 2\n# retryBudget--\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="retryBudget = 2\n",
        head_blob="retryBudget = 2\nother--\n",
    )
    # ``del`` removes a salvage binding without a rebind or call site; neither
    # assign scanner nor call scanner recorded the target, so prefix retention
    # reused stale FIXED evidence (PRRT_kwDOSJAM6s6Zse8m).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\ndel FEATURE_ENABLED\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nif ready: del FEATURE_ENABLED\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\ndel FEATURE_ENABLED, other\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = True\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = True\ndel FLAGS["enabled"]\n'),
    )
    # Parenthesized ``del(NAME)`` / ``del (NAME)`` are valid Python and must
    # supersede like bare ``del NAME`` (PRRT_kwDOSJAM6s6ZsmNH).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\ndel(FEATURE_ENABLED)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\ndel (FEATURE_ENABLED)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nif ready: del(FEATURE_ENABLED)\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n# del FEATURE_ENABLED\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\ndel other\n",
    )
    # JS ``delete`` removes a salvage binding without a rebind or call site the
    # same way Python ``del`` does; the scanner previously only matched ``del``,
    # so tip ``delete guard.enabled`` kept a line-aligned salvage prefix and
    # reused stale FIXED evidence (PRRT_kwDOSJAM6s6ZtiIE).
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\ndelete guard.enabled\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nif (ready) delete guard.enabled\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS = {}\nFLAGS["enabled"] = true\n',
        head_blob=('FLAGS = {}\nFLAGS["enabled"] = true\ndelete FLAGS["enabled"]\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\ndelete(guard.enabled)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\ndelete (guard.enabled)\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\n// delete guard.enabled\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\ndelete other\n",
    )
    # Indirect attribute mutations via setattr/delattr leave no binding key and
    # only a bare ``setattr`` call name, which does not intersect salvage
    # ``guard.enabled``; without recognizing the helper target, tip appends keep
    # a line-aligned prefix and reuse stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zu8Kn).
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob='guard.enabled = True\nsetattr(guard, "enabled", False)\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob="guard.enabled = True\nsetattr(guard, 'enabled', False)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob='guard.enabled = True\ndelattr(guard, "enabled")\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob=('guard.enabled = True\nobject.__setattr__(guard, "enabled", False)\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob=('guard.enabled = True\nbuiltins.setattr(guard, "enabled", False)\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob='guard.enabled = True\nguard.__setattr__("enabled", False)\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob='guard.enabled = True\nguard.__delattr__("enabled")\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob=('guard.enabled = True\nif ready: setattr(guard, "enabled", False)\n'),
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob='guard.enabled = True\n# setattr(guard, "enabled", False)\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob='guard.enabled = True\nsetattr(other, "enabled", False)\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = True\n",
        head_blob='guard.enabled = True\nsetattr(guard, "other", False)\n',
    )
    # Collection mutation helpers vs subscript salvage: binding scanner emits
    # nothing; call names ``FLAGS`` / ``FLAGS.__setitem__`` do not match
    # ``FLAGS["enabled"]``. Without helper recognition / receiver fail-closed,
    # tip appends retain stale FIXED evidence (PRRT_kwDOSJAM6s6ZwrnH).
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.__setitem__("enabled", False)\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob=('FLAGS["enabled"] = True\ndict.__setitem__(FLAGS, "enabled", False)\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.__delitem__("enabled")\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.update(enabled=False)\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.update({"enabled": False})\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.update(other_flags)\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.clear()\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\n# FLAGS.__setitem__("enabled", False)\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nOTHER.__setitem__("enabled", False)\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.__setitem__("other", False)\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.update(other=False)\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.update({"other": False})\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='FLAGS["enabled"] = True\n',
        head_blob='FLAGS["enabled"] = True\nFLAGS.copy()\n',
    )
    # Object.assign mutates a salvage receiver without a binding key; call names
    # are only ``Object`` / ``Object.assign``, and opaque mutator fail-closed is
    # limited to subscript receivers — recognize the target or fail closed so a
    # descendant cannot keep stale FIXED evidence (PRRT_kwDOSJAM6s6Zxwhs).
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.assign(guard, {enabled: false})\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob='guard.enabled = true\nObject.assign(guard, {"enabled": false})\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.assign(guard, {enabled})\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nglobalThis.Object.assign(guard, {enabled: false})\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.assign(guard, other)\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.assign(guard, {other: false}, extra)\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\n// Object.assign(guard, {enabled: false})\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.assign(other, {enabled: false})\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.assign(guard, {other: false})\n",
    )
    # Multiline Object.assign after salvage (PRRT_kwDOSJAM6s6Zyo4_).
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.assign(\n  guard,\n  {enabled: false}\n);\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.assign(\n  guard,\n  other\n);\n",
    )
    # Object.defineProperty after salvage ``guard.enabled = true`` leaves no
    # binding key and only call names ``Object`` / ``Object.defineProperty``;
    # recognize literal property targets or fail closed on opaque ones sharing
    # the salvaged receiver (PRRT_kwDOSJAM6s6Zy4pR).
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            'guard.enabled = true\nObject.defineProperty(guard, "enabled", {value: false})\n'
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\nObject.defineProperty(guard, 'enabled', {value: false})\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\n"
            'globalThis.Object.defineProperty(guard, "enabled", {value: false})\n'
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.defineProperty(guard, key, {value: false})\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            'guard.enabled = true\n// Object.defineProperty(guard, "enabled", {value: false})\n'
        ),
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            'guard.enabled = true\nObject.defineProperty(other, "enabled", {value: false})\n'
        ),
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=('guard.enabled = true\nObject.defineProperty(guard, "other", {value: false})\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\n"
            'Object.defineProperty(\n  guard,\n  "enabled",\n  {value: false}\n);\n'
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\nObject.defineProperty(\n  guard,\n  key,\n  {value: false}\n);\n"
        ),
    )
    # Shell ``unset`` removes a salvage binding without a rebind or call site;
    # assign/del/delete scanners previously missed it, so tip
    # ``unset FEATURE_ENABLED`` kept a line-aligned salvage prefix and reused
    # stale FIXED evidence (PRRT_kwDOSJAM6s6ZuRSm).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nunset FEATURE_ENABLED\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nunset -v FEATURE_ENABLED\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nunset -- FEATURE_ENABLED\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nunset FEATURE_ENABLED OTHER\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export FEATURE_ENABLED=true\n",
        head_blob="export FEATURE_ENABLED=true\nif true; then unset -v FEATURE_ENABLED; fi\n",
    )
    # Quoted unset operands are blanked by ``_executable_call_scan_text`` before
    # the bare-name matcher runs; recover them so tip ``unset 'FEATURE_ENABLED'``
    # / ``unset "FEATURE_ENABLED"`` still supersede salvage
    # (PRRT_kwDOSJAM6s6Zu20N).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nunset 'FEATURE_ENABLED'\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob='FEATURE_ENABLED=true\nunset "FEATURE_ENABLED"\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nunset -v 'FEATURE_ENABLED'\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob='FEATURE_ENABLED=true\nunset -- "FEATURE_ENABLED"\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nunset 'FEATURE_ENABLED' OTHER\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export FEATURE_ENABLED=true\n",
        head_blob=("export FEATURE_ENABLED=true\nif true; then unset -v 'FEATURE_ENABLED'; fi\n"),
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\n# unset 'FEATURE_ENABLED'\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\n# unset FEATURE_ENABLED\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nunset other\n",
    )
    # Typed rebind still supersedes via statement-leading ``name: T =``; the
    # type token alone must not invent a second binding key.
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED: bool = False\n",
    )
    # Duplicate earlier ``False`` in the salvage blob must not hide an appended
    # override via set-membership tip-extra accounting (PRRT_kwDOSJAM6s6ZrFdv).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = False\nFEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = False\nFEATURE_ENABLED = True\nFEATURE_ENABLED = False\n"),
    )
    # Surplus identical assignment copy keeps last binding equal → retain.
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED = True\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nFEATURE_ENABLED: bool = False\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="#define FEATURE_ENABLED 1\n",
        head_blob="#define FEATURE_ENABLED 1\n#define FEATURE_ENABLED 0\n",
    )
    # ``#undef`` removes a salvage macro without a re-``#define``; discarding it
    # as a non-define ``#`` line kept a line-aligned prefix and reused stale
    # FIXED evidence (PRRT_kwDOSJAM6s6ZyImI).
    assert not _added_salvage_blob_retained(
        commit_blob="#define FEATURE_ENABLED 1\n",
        head_blob="#define FEATURE_ENABLED 1\n#undef FEATURE_ENABLED\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="#define FEATURE_ENABLED 1\n",
        head_blob="#define FEATURE_ENABLED 1\n# undef FEATURE_ENABLED\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="#define FEATURE_ENABLED 1\n",
        head_blob="#define FEATURE_ENABLED 1\n#undef OTHER\n",
    )
    # YAML-style ``key: value`` rebinds must fail closed the same way equals-
    # style assignments do; the matcher previously only handled ``=`` / ``:=``
    # and declarations, so an appended override kept a line-aligned prefix and
    # reused stale FIXED evidence (PRRT_kwDOSJAM6s6ZqNAk).
    assert not _added_salvage_blob_retained(
        commit_blob="feature_enabled: true\n",
        head_blob="feature_enabled: true\nfeature_enabled: false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature_enabled: true\n",
        head_blob="feature_enabled: true\nfeature_enabled: false\nother_key: 1\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature_enabled: true\n",
        head_blob="feature_enabled: true\n# feature_enabled: false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature_enabled: true\n",
        head_blob="feature_enabled: true\nother_key: 1\n",
    )
    # Nested YAML leaves under different parents must not collide as bare
    # ``enabled`` on the added-file append path. Flat ``_binding_names`` would
    # intersect salvage ``feature.enabled`` with an unrelated ``logging.enabled``
    # append and discard still-valid FIXED evidence (PRRT_kwDOSJAM6s6Zq76q;
    # baseline-backed path already scopes via PRRT_kwDOSJAM6s6ZqZo2).
    assert _added_salvage_blob_retained(
        commit_blob="feature:\n  enabled: true\n",
        head_blob=("feature:\n  enabled: true\nlogging:\n  enabled: false\n"),
    )
    # Same-parent nested rebind under the salvage prefix still supersedes.
    assert not _added_salvage_blob_retained(
        commit_blob="feature:\n  enabled: true\n",
        head_blob=("feature:\n  enabled: true\n  enabled: false\n"),
    )
    # TOML table siblings: ``[feature] enabled`` vs ``[logging] enabled``.
    assert _added_salvage_blob_retained(
        commit_blob="[feature]\nenabled = true\n",
        head_blob=("[feature]\nenabled = true\n[logging]\nenabled = false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="[feature]\nenabled = true\n",
        head_blob=("[feature]\nenabled = true\nenabled = false\n"),
    )
    # Quoted JSON/YAML mapping keys (incl. hyphenated) must supersede like bare
    # identifiers; identifier-only matching left `"feature-enabled"` unbound so
    # an appended duplicate kept a line-aligned prefix and reused stale FIXED
    # evidence (PRRT_kwDOSJAM6s6ZqQfh).
    assert not _added_salvage_blob_retained(
        commit_blob='"feature-enabled": true\n',
        head_blob='"feature-enabled": true\n"feature-enabled": false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='"feature-enabled": true\n',
        head_blob=('"feature-enabled": true\n"other": 1\n"feature-enabled": false\n'),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="'feature-enabled': true\n",
        head_blob="'feature-enabled': true\n'feature-enabled': false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob='"feature-enabled": true\n',
        head_blob='"feature-enabled": true\n# "feature-enabled": false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='"feature-enabled": true\n',
        head_blob='"feature-enabled": true\n"other-key": 1\n',
    )
    # YAML/JSON ``:`` treats ``"a.b"`` and ``a.b`` as one key. Re-quoting
    # non-bare segments (correct for TOML ``=``) made quote-only tip rebinds
    # miss salvage names and retain superseded FIXED evidence
    # (PRRT_kwDOSJAM6s6ZqtHj).
    assert not _added_salvage_blob_retained(
        commit_blob='"a.b": true\n',
        head_blob='"a.b": true\na.b: false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="a.b: true\n",
        head_blob='a.b: true\n"a.b": false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="'a.b': true\n",
        head_blob="'a.b': true\na.b: false\n",
    )
    # TOML bare keys may include hyphens (`feature-enabled = true`). Identifier-
    # only matching left both salvage and appended rebind unbound, so the tip
    # kept a line-aligned prefix and reused stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zqip3). Quoted TOML keys use ``=`` (not ``:``).
    assert not _added_salvage_blob_retained(
        commit_blob="feature-enabled = true\n",
        head_blob="feature-enabled = true\nfeature-enabled = false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature-enabled = true\n",
        head_blob=("feature-enabled = true\nother = 1\nfeature-enabled = false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature-enabled: true\n",
        head_blob="feature-enabled: true\nfeature-enabled: false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob='"feature-enabled" = true\n',
        head_blob='"feature-enabled" = true\n"feature-enabled" = false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="'feature-enabled' = true\n",
        head_blob="'feature-enabled' = true\n'feature-enabled' = false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature-enabled = true\n",
        head_blob="feature-enabled = true\n# feature-enabled = false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature-enabled = true\n",
        head_blob="feature-enabled = true\nother-key = 1\n",
    )
    # TOML dotted keys (`feature.enabled = true`) must bind the full path.
    # Identifier-only matching required `=` immediately after the first segment,
    # so neither salvage nor an appended `feature.enabled = false` bound and the
    # tip kept a line-aligned prefix / reused stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zql88). Quoted dotted segments normalize to the same key.
    assert not _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob="feature.enabled = true\nfeature.enabled = false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob=("feature.enabled = true\nother = 1\nfeature.enabled = false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob='feature.enabled = true\nfeature."enabled" = false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='"feature".enabled = true\n',
        head_blob='"feature".enabled = true\nfeature.enabled = false\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob='site."google.com" = true\n',
        head_blob='site."google.com" = true\nsite."google.com" = false\n',
    )
    # Quoted segments that contain dots must stay distinct from bare dotted
    # paths: site."google.com" ≠ site.google.com, and "a.b" ≠ a.b. Collapsing
    # them let tip extras look like rebinds and drop FIXED evidence
    # (PRRT_kwDOSJAM6s6ZqoYV).
    assert _added_salvage_blob_retained(
        commit_blob='site."google.com" = true\n',
        head_blob='site."google.com" = true\nsite.google.com = false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="site.google.com = true\n",
        head_blob='site.google.com = true\nsite."google.com" = false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob='"a.b" = true\n',
        head_blob='"a.b" = true\na.b = false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="a.b = true\n",
        head_blob='a.b = true\n"a.b" = false\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="'a.b' = true\n",
        head_blob="'a.b' = true\na.b = false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="a.b.c = 1\n",
        head_blob="a.b.c = 1\na.b.c = 2\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob="feature.enabled = true\n# feature.enabled = false\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob="feature.enabled = true\nother.key = 1\n",
    )
    # Tip-extra bare/root call must not match scoped binding ``feature.enabled``
    # via ``name.*`` prefix (PRRT_kwDOSJAM6s6ZrsE0).
    assert _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob="feature.enabled = true\nfeature()\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="feature.enabled = true\n",
        head_blob="feature.enabled = true\nfeature[key]()\n",
    )
    # Docstring / block-comment prose that reuses a salvage assignment name
    # (Google-style ``Args:`` / ``timeout: Seconds…``) must not count as a
    # YAML-style rebind; otherwise benign documentation drops FIXED evidence
    # (PRRT_kwDOSJAM6s6ZqPO9).
    assert _added_salvage_blob_retained(
        commit_blob="timeout = 30\n",
        head_blob=(
            "timeout = 30\n"
            '"""Client options.\n'
            "\n"
            "Args:\n"
            "    timeout: Seconds until the request fails.\n"
            '"""\n'
        ),
    )
    assert _added_salvage_blob_retained(
        commit_blob="timeout = 30\n",
        head_blob=("timeout = 30\n/*\ntimeout: Seconds until the request fails.\n*/\n"),
    )
    assert _added_salvage_blob_retained(
        commit_blob="timeout = 30\n",
        head_blob=(
            "timeout = 30\n"
            "'''Client options.\n"
            "\n"
            "Args:\n"
            "    timeout: Seconds until the request fails.\n"
            "'''\n"
        ),
    )
    # ``/*`` / nested quotes inside ordinary strings or ``#`` / ``//`` line
    # comments must not open block/triple state; otherwise a later real rebind
    # after a URL/glob/comment line is skipped and FIXED evidence is reused
    # (PRRT_kwDOSJAM6s6ZqSbO).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=(
            'FEATURE_ENABLED = True\nurl = "https://example.com/*/path"\nFEATURE_ENABLED = False\n'
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\npattern = 'foo/*bar'\nFEATURE_ENABLED = False\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=(
            "FEATURE_ENABLED = True\nhint = \"use ''' for docs\"\nFEATURE_ENABLED = False\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=(
            "FEATURE_ENABLED = True\n# see https://example.com/*/docs\nFEATURE_ENABLED = False\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob=("FEATURE_ENABLED = True\n// pattern: foo/*bar\nFEATURE_ENABLED = False\n"),
    )
    # Spaced ``# define`` is a real preprocessor binding (whitespace between ``#``
    # and the keyword is allowed, same as open-``#if`` scanning). Skipping it as
    # a comment would keep a line-aligned prefix and reuse stale salvage evidence
    # (PRRT_kwDOSJAM6s6Zp_sv).
    assert not _added_salvage_blob_retained(
        commit_blob="# define FEATURE_ENABLED 1\n",
        head_blob="# define FEATURE_ENABLED 1\n# define FEATURE_ENABLED 0\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="#define FEATURE_ENABLED 1\n",
        head_blob="#define FEATURE_ENABLED 1\n# define FEATURE_ENABLED 0\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="# define FEATURE_ENABLED 1\n",
        head_blob="# define FEATURE_ENABLED 1\n# undef FEATURE_ENABLED\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="def guard():\n    return True\n",
        head_blob="def guard():\n    return True\ndef guard():\n    return False\n",
    )
    # ``export class`` / ``export default function`` must count as bindings the
    # same way bare ``class`` / ``export function`` do; otherwise an appended
    # rebind keeps a line-aligned prefix and reuses stale FIXED evidence
    # (PRRT_kwDOSJAM6s6Zp_sx). Avoid ``name =`` bodies so retention is gated on
    # the declaration binding, not an incidental field assignment.
    assert not _added_salvage_blob_retained(
        commit_blob="export class Guard {\n  ok() { return true; }\n}\n",
        head_blob=(
            "export class Guard {\n  ok() { return true; }\n}\n"
            "export class Guard {\n  ok() { return false; }\n}\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export default class Guard {\n  ok() { return true; }\n}\n",
        head_blob=(
            "export default class Guard {\n  ok() { return true; }\n}\n"
            "export default class Guard {\n  ok() { return false; }\n}\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export default function guard() {\n  return true;\n}\n",
        head_blob=(
            "export default function guard() {\n  return true;\n}\n"
            "export default function guard() {\n  return false;\n}\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export default async function guard() {\n  return true;\n}\n",
        head_blob=(
            "export default async function guard() {\n  return true;\n}\n"
            "export default async function guard() {\n  return false;\n}\n"
        ),
    )
    # Shell ``export NAME=value`` must bind like bare assignments; otherwise an
    # appended ``export FEATURE_ENABLED=false`` keeps a line-aligned prefix and
    # reuses stale FIXED evidence (PRRT_kwDOSJAM6s6ZqseO).
    assert not _added_salvage_blob_retained(
        commit_blob="export FEATURE_ENABLED=true\n",
        head_blob="export FEATURE_ENABLED=true\nexport FEATURE_ENABLED=false\n",
    )
    # ``declare -x`` / ``typeset`` assignment forms must bind like ``export``;
    # otherwise a descendant rebind keeps a line-aligned prefix and reuses
    # stale FIXED evidence (PRRT_kwDOSJAM6s6ZqxX4).
    assert not _added_salvage_blob_retained(
        commit_blob="declare -x FEATURE_ENABLED=true\n",
        head_blob=("declare -x FEATURE_ENABLED=true\ndeclare -x FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="typeset -x FEATURE_ENABLED=true\n",
        head_blob=("typeset -x FEATURE_ENABLED=true\ntypeset -x FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="declare -rx FEATURE_ENABLED=true\n",
        head_blob=("declare -rx FEATURE_ENABLED=true\ndeclare -rx FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="declare FEATURE_ENABLED=true\n",
        head_blob="declare FEATURE_ENABLED=true\ndeclare FEATURE_ENABLED=false\n",
    )
    # ``readonly NAME=value`` (and flagged forms) must bind like declare/typeset;
    # otherwise an appended readonly rebind keeps a line-aligned prefix and
    # reuses stale FIXED evidence (PRRT_kwDOSJAM6s6ZrBJF).
    assert not _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED=true\n",
        head_blob="FEATURE_ENABLED=true\nreadonly FEATURE_ENABLED=false\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="readonly FEATURE_ENABLED=true\n",
        head_blob=("readonly FEATURE_ENABLED=true\nreadonly FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="readonly -x FEATURE_ENABLED=true\n",
        head_blob=("readonly -x FEATURE_ENABLED=true\nreadonly -x FEATURE_ENABLED=false\n"),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="export FEATURE_ENABLED=true\n",
        head_blob=("export FEATURE_ENABLED=true\nreadonly FEATURE_ENABLED=false\n"),
    )
    # Comment-only / unrelated appends cannot supersede the salvage binding.
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\n# FEATURE_ENABLED = False\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nother = 1\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard()\n",
        head_blob="# enable_guard()\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard()",
        head_blob="x_enable_guard()",
    )
    # Mid-file whole-line occurrence inside disabling wrappers must fail closed
    # even though the salvage bytes remain line-boundary-aligned
    # (PRRT_kwDOSJAM6s6ZpQKt).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#if 0\ncheck();\n#endif\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/*\ncheck();\n*/\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='"""\ncheck();\n"""\n',
    )
    # Prepended *unterminated* wrappers still leave a line-aligned suffix; that
    # must fail closed or a no-change FIXED reuses stale evidence
    # (PRRT_kwDOSJAM6s6ZpaIn).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='"""\ncheck();\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="'''\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#if 0\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#ifdef FEATURE\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#ifndef FEATURE\ncheck();\n",
    )
    # Hash-line bodies must still scan for trailing ``/*``; otherwise
    # ``#endif /*`` / ``#define X /*`` leave block-comment state closed and a
    # later no-change FIXED reuses disabled suffix evidence
    # (PRRT_kwDOSJAM6s6ZpdMC).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#endif /*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#define X /*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#if 0\n#endif /*\ncheck();\n",
    )
    # Closing ``*/`` must not clear line-start across a same-line ``#if`` after
    # a multi-line comment ends (PRRT_kwDOSJAM6s6ZpdMC).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/*\n*/ #if 0\ncheck();\n",
    )
    # Quoted / line-comment ``/*`` in a prepended prefix must not look like an
    # unterminated block comment or a still-valid salvage suffix is rejected and
    # a later no-change FIXED becomes fixed_without_head_advance
    # (PRRT_kwDOSJAM6s6Zq2m_).
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='const marker = "/*";\ncheck();\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="const marker = '/*';\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='const marker = "\\"/*";\ncheck();\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="// /*\ncheck();\n",
    )
    # After a closed ordinary string, a real open ``/*`` still disables.
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='const marker = "*/"; /*\ncheck();\n',
    )
    # Possessives / contractions / inch marks must not open ordinary-string
    # opacity, or a later real ``/*`` / ``#if`` in the prepended prefix is
    # ignored and suffix salvage is retained under a disabling region
    # (PRRT_kwDOSJAM6s6Zq7kr).
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="user's note\n/*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="don't touch\n/*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='the 5" panel\n/*\ncheck();\n',
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="it's fine\n#if 0\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="users' notes\n/*\ncheck();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="grab 'em\n/*\ncheck();\n",
    )
    # Same opener filter for binding-state scanning: a prose apostrophe must
    # not swallow a same-line ``/*`` that should hide a later rebind.
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob="FEATURE_ENABLED = True\nuser's note /*\nFEATURE_ENABLED = False\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="FEATURE_ENABLED = True\n",
        head_blob='FEATURE_ENABLED = True\npanel 5" /*\nFEATURE_ENABLED = False\n',
    )
    # Closed wrappers before the salvage suffix are fine (benign prepend region).
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/* note */\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#endif /* note */\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="/*\nnote\n*/\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob='"""doc"""\ncheck();\n',
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="'''doc'''\ncheck();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#if 0\n#endif\ncheck();\n",
    )
    # ``#iffy`` is not a preprocessor ``#if``; treat as a benign prefix line.
    assert _added_salvage_blob_retained(
        commit_blob="check();\n",
        head_blob="#iffy\ncheck();\n",
    )
    # Ordinary C/JS control-flow prefixes attach the salvage as the next
    # statement body while keeping a line-aligned suffix; reject so a later
    # no-change FIXED retry cannot reuse a disabled call (PRRT_kwDOSJAM6s6ZtJG5).
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="if (false)\nenable_guard();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="while (0)\nenable_guard();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="for (;0;)\nenable_guard();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="else\nenable_guard();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="if (false) {\nenable_guard();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="if (\nfalse\n)\nenable_guard();\n",
    )
    assert not _added_salvage_blob_retained(
        commit_blob="    enable_guard()\n",
        head_blob="if False:\n    enable_guard()\n",
    )
    # Benign complete statements / closed blocks before the suffix still retain.
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="setup();\nenable_guard();\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="enable_guard();\n",
        head_blob="if (false) {\nsetup();\n}\nenable_guard();\n",
    )
    # Empty-file addition salvage: only an exact empty tip blob retains it.
    # Vacuous ``"" in head`` / early-True would accept an overwrite and let a
    # later no-change FIXED retry reuse stale evidence (PRRT_kwDOSJAM6s6ZpEZh).
    assert _added_salvage_blob_retained(commit_blob="", head_blob="")
    assert not _added_salvage_blob_retained(commit_blob="", head_blob="anything\n")
