"""Pre-push validation fix-pass tip-extra supersession tests (part 010)."""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_tip_extra_can_supersede_modified_salvage_call_site_override() -> None:
    """Tip-extra calls must supersede call-only modified salvage flips.

    Salvage that only rewrites ``disable_guard()`` → ``enable_guard()`` changes
    no bindings, so a binding-only check retains stale FIXED evidence when a
    descendant appends ``disable_guard()`` and ``git merge-file`` still equals
    HEAD (PRRT_kwDOSJAM6s6ZrN5J). Mirrors added-file call-site fail-closed
    behavior (PRRT_kwDOSJAM6s6ZrJ3a).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _salvage_changed_binding_names,
        _tip_extra_can_supersede_modified_salvage,
    )

    parent = "x = 1\ndisable_guard()\ny = 2\n"
    commit = "x = 1\nenable_guard()\ny = 2\n"
    assert _salvage_changed_binding_names(parent_blob=parent, commit_blob=commit) == set()
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nenable_guard()\ny = 2\ndisable_guard()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nenable_guard()\ny = 2\nawait disable_guard()\n",
    )
    # Commented / unrelated tip-extra calls stay retained.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nenable_guard()\ny = 2\n# disable_guard()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob="x = 1\nenable_guard()\ny = 2\nextra()\n",
    )
    # Same flip with helper defs present: bindings unchanged, tip restore
    # of disable_guard() must still supersede.
    parent_defs = "def enable_guard():\n    pass\ndef disable_guard():\n    pass\ndisable_guard()\n"
    commit_defs = "def enable_guard():\n    pass\ndef disable_guard():\n    pass\nenable_guard()\n"
    assert _salvage_changed_binding_names(parent_blob=parent_defs, commit_blob=commit_defs) == set()
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_defs,
        commit_blob=commit_defs,
        head_blob=commit_defs + "disable_guard()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_defs,
        commit_blob=commit_defs,
        head_blob=commit_defs + "# disable_guard()\n",
    )
    # Tip-extra call to an *unchanged* salvage helper must not supersede when
    # only a binding (or call) fix changed — unioning every commit binding as
    # call candidates would drop valid FIXED evidence (PRRT_kwDOSJAM6s6ZrR2e).
    parent_helper = "def helper():\n    pass\nFEATURE_ENABLED = False\n"
    commit_helper = "def helper():\n    pass\nFEATURE_ENABLED = True\n"
    assert _salvage_changed_binding_names(parent_blob=parent_helper, commit_blob=commit_helper) == {
        "FEATURE_ENABLED"
    }
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_helper,
        commit_blob=commit_helper,
        head_blob=commit_helper + "helper()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_helper,
        commit_blob=commit_helper,
        head_blob=commit_helper + "FEATURE_ENABLED = False\n",
    )
    # Member-call-only flips (``guard.disable()`` → ``guard.enable()``) must
    # supersede when a tip restores ``guard.disable()`` (PRRT_kwDOSJAM6s6ZrSYE).
    parent_member = "x = 1\nguard.disable()\ny = 2\n"
    commit_member = "x = 1\nguard.enable()\ny = 2\n"
    assert (
        _salvage_changed_binding_names(parent_blob=parent_member, commit_blob=commit_member)
        == set()
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "guard.disable()\n",
    )
    # Multiline member continuations preserve the receiver (PRRT_kwDOSJAM6s6ZuG-J).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "guard\n  .disable();\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "guard\n  ?.disable();\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'guard\n  ["disable"]();\n',
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "other\n  .disable();\n",
    )
    # Joined computed tips emit bare receivers; do not fail-closed supersede
    # an unrelated multiline computed tip (PRRT_kwDOSJAM6s6ZuQ6c).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'other\n  ["disable"]();\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "  .disable();\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + '  ["disable"]();\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "await guard.disable()\n",
    )
    # Nested / mid-expression restores must supersede (PRRT_kwDOSJAM6s6ZrYJk).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "if ready: guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "result = guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "print(guard.disable())\n",
    )
    # Optional-chain restores must preserve receiver identity too; a bare
    # ``disable`` leaf from restarting after ``?.`` would also falsely match
    # ``guard.disable`` via suffix and supersede ``other?.disable()``
    # (PRRT_kwDOSJAM6s6ZriaJ).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "guard?.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "await guard?.disable()\n",
    )
    # Computed-member restores must intersect the salvaged ``guard`` receiver
    # (PRRT_kwDOSJAM6s6ZroRa).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'guard["disable"]()\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "guard['disable']()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'guard?.["disable"]()\n',
    )
    # Parenthesized receivers must intersect the salvaged ``guard`` binding;
    # restarting after ``)`` would only see bare ``disable`` (PRRT_kwDOSJAM6s6Zrr7R).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "(guard).disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "await (guard).disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "(guard)?.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + '(guard)["disable"]()\n',
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'other["disable"]()\n',
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "other?.disable()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "(other).disable()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "# guard.disable()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "code  # guard.disable()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'x = "guard.disable()"\n',
    )
    # JS template literals: static text is non-executable; ``${...}`` remains
    # scannable (PRRT_kwDOSJAM6s6ZtJG8).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "const marker = `guard.disable()`;\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "const marker = `${guard.disable()}`;\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'const marker = `${"guard.disable()"}`;\n',
    )
    # Python f-strings: static text is non-executable; ``{...}`` remains
    # scannable (PRRT_kwDOSJAM6s6Zt7Go).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'marker = f"guard.disable()"\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'marker = f"{guard.disable()}"\n',
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "marker = f'{guard.disable()}'\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'marker = f"""{guard.disable()}"""\n',
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + 'marker = f"""guard.disable()"""\n',
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "marker = f\"{'guard.disable()'}\"\n",
    )
    # Same-line ``/* … */`` tip extras are non-executable (PRRT_kwDOSJAM6s6Zrhbs).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "/* guard.disable() */\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "code; /* guard.disable() */\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "/* note */ guard.disable()\n",
    )
    # JS regex literals are non-executable call text (PRRT_kwDOSJAM6s6Zs-Re).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "const matcher = /guard.disable()/;\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "const matcher = /guard.disable()/gi;\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "const matcher = /[guard.disable()]/;\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "return /guard.disable()/;\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "/guard.disable()/;\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "const matcher = /guard\\.disable\\(\\)/;\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "const matcher = /guard.disable()\n",
    )
    # Division keeps real call sites executable.
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "x = 1 / guard.disable() / 2\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "vals[i] / guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "a.b / guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "block} / guard.disable()\n",
    )
    # Postfix ``++`` / ``--`` leave a value, so ``/`` is division — not a
    # regex opener that would blank ``guard.disable()`` (PRRT_kwDOSJAM6s6ZtHbn).
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "retries++ / guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "retries-- / guard.disable()\n",
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "retries++/guard.disable()\n",
    )
    # Binary ``+`` / ``-`` still allow a following regex literal.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "x + /guard.disable()/\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "x - /guard.disable()/\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "const matcher = /\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "other.noop()\n",
    )
    # Unrelated tip member call sharing only the method leaf must not supersede
    # (mirrors added-salvage retain for ``other.disable()``; PRRT_kwDOSJAM6s6ZrWwo).
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_member,
        commit_blob=commit_member,
        head_blob=commit_member + "other.disable()\n",
    )
    # Same-line multiplicity: parent ``disable_guard(); disable_guard()`` →
    # salvage ``disable_guard(); enable_guard()`` must count both disable calls
    # so ``disable_guard`` is a changed candidate; otherwise a tip that appends
    # ``disable_guard()`` retains stale salvage (PRRT_kwDOSJAM6s6ZriaK).
    parent_dup = "x = 1\ndisable_guard(); disable_guard()\ny = 2\n"
    commit_dup = "x = 1\ndisable_guard(); enable_guard()\ny = 2\n"
    assert _salvage_changed_binding_names(parent_blob=parent_dup, commit_blob=commit_dup) == set()
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dup,
        commit_blob=commit_dup,
        head_blob=commit_dup + "disable_guard()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dup,
        commit_blob=commit_dup,
        head_blob=commit_dup + "# disable_guard()\n",
    )
    parent_dup_member = "x = 1\nguard.disable(); guard.disable()\ny = 2\n"
    commit_dup_member = "x = 1\nguard.disable(); guard.enable()\ny = 2\n"
    assert (
        _salvage_changed_binding_names(parent_blob=parent_dup_member, commit_blob=commit_dup_member)
        == set()
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dup_member,
        commit_blob=commit_dup_member,
        head_blob=commit_dup_member + "guard.disable()\n",
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dup_member,
        commit_blob=commit_dup_member,
        head_blob=commit_dup_member + "other.disable()\n",
    )
