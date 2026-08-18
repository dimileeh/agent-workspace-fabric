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


@pytest.mark.unit
def test_tip_extra_control_flow_in_changed_callable_supersedes_modified_salvage() -> None:
    """Tip-extra control-flow in a salvage-modified callable must supersede.

    Salvage that flips ``guard.disable()`` → ``guard.enable()`` inside a
    function marks the callable binding changed. A descendant that inserts
    ``return`` (or other transfer/header control-flow) near the start—with
    enough unchanged lines that ``git merge-file`` still equals HEAD—is neither
    a rebind nor a call on the tip-extra line, so binding/call checks retain
    stale FIXED evidence while the salvaged fix is unreachable
    (PRRT_kwDOSJAM6s6ZvVZK).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _salvage_changed_binding_names,
        _tip_extra_can_supersede_modified_salvage,
    )

    parent = (
        "def apply():\n"
        "    setup()\n"
        "    prepare()\n"
        "    validate()\n"
        "    finalize()\n"
        "    guard.disable()\n"
    )
    commit = (
        "def apply():\n"
        "    setup()\n"
        "    prepare()\n"
        "    validate()\n"
        "    finalize()\n"
        "    guard.enable()\n"
    )
    assert "apply" in _salvage_changed_binding_names(parent_blob=parent, commit_blob=commit)
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "def apply():\n"
            "    return\n"
            "    setup()\n"
            "    prepare()\n"
            "    validate()\n"
            "    finalize()\n"
            "    guard.enable()\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "def apply():\n"
            "    raise RuntimeError('skip')\n"
            "    setup()\n"
            "    prepare()\n"
            "    validate()\n"
            "    finalize()\n"
            "    guard.enable()\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "def apply():\n"
            "    if False:\n"
            "        pass\n"
            "    setup()\n"
            "    prepare()\n"
            "    validate()\n"
            "    finalize()\n"
            "    guard.enable()\n"
        ),
    )
    # Commented / unrelated tip-extra inside the callable stays retained.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "def apply():\n"
            "    # return\n"
            "    setup()\n"
            "    prepare()\n"
            "    validate()\n"
            "    finalize()\n"
            "    guard.enable()\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=(
            "def apply():\n"
            "    setup()\n"
            "    prepare()\n"
            "    log('x')\n"
            "    validate()\n"
            "    finalize()\n"
            "    guard.enable()\n"
        ),
    )
    # Control-flow in an unrelated function must not drop still-reachable salvage.
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent,
        commit_blob=commit,
        head_blob=commit + "def other():\n    return\n",
    )
    parent_js = (
        "function apply() {\n"
        "  setup();\n"
        "  prepare();\n"
        "  validate();\n"
        "  finalize();\n"
        "  guard.disable();\n"
        "}\n"
    )
    commit_js = (
        "function apply() {\n"
        "  setup();\n"
        "  prepare();\n"
        "  validate();\n"
        "  finalize();\n"
        "  guard.enable();\n"
        "}\n"
    )
    assert "apply" in _salvage_changed_binding_names(parent_blob=parent_js, commit_blob=commit_js)
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js,
        commit_blob=commit_js,
        head_blob=(
            "function apply() {\n"
            "  return;\n"
            "  setup();\n"
            "  prepare();\n"
            "  validate();\n"
            "  finalize();\n"
            "  guard.enable();\n"
            "}\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js,
        commit_blob=commit_js,
        head_blob=(
            "function apply() {\n"
            "  if (false) {\n"
            "  setup();\n"
            "  prepare();\n"
            "  validate();\n"
            "  finalize();\n"
            "  guard.enable();\n"
            "  }\n"
            "}\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js,
        commit_blob=commit_js,
        head_blob=(
            "function apply() {\n"
            "  throw new Error('skip');\n"
            "  setup();\n"
            "  prepare();\n"
            "  validate();\n"
            "  finalize();\n"
            "  guard.enable();\n"
            "}\n"
        ),
    )
    # Class leaf salvage must not be superseded by tip-extra return inside a
    # nested method (unchanged nested def/function bodies stay out of scope).
    parent_cls = "class C:\n    FEATURE_ENABLED = False\n    def helper(self):\n        pass\n"
    commit_cls = "class C:\n    FEATURE_ENABLED = True\n    def helper(self):\n        pass\n"
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_cls,
        commit_blob=commit_cls,
        head_blob=(
            "class C:\n"
            "    FEATURE_ENABLED = True\n"
            "    def helper(self):\n"
            "        return\n"
            "        pass\n"
        ),
    )
    # JS class method shorthand is not a separate binding — only ``C`` changes —
    # so tip-extra return in that method must still supersede (PRRT_kwDOSJAM6s6Zvk1G).
    parent_js_cls = (
        "class C {\n"
        "  apply() {\n"
        "    setup();\n"
        "    prepare();\n"
        "    validate();\n"
        "    finalize();\n"
        "    guard.disable();\n"
        "  }\n"
        "}\n"
    )
    commit_js_cls = (
        "class C {\n"
        "  apply() {\n"
        "    setup();\n"
        "    prepare();\n"
        "    validate();\n"
        "    finalize();\n"
        "    guard.enable();\n"
        "  }\n"
        "}\n"
    )
    assert _salvage_changed_binding_names(parent_blob=parent_js_cls, commit_blob=commit_js_cls) == {
        "C"
    }
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_js_cls,
        commit_blob=commit_js_cls,
        head_blob=(
            "class C {\n"
            "  apply() {\n"
            "    return;\n"
            "    setup();\n"
            "    prepare();\n"
            "    validate();\n"
            "    finalize();\n"
            "    guard.enable();\n"
            "  }\n"
            "}\n"
        ),
    )
    # File-level multiset tip-extra can greedily attribute an elsewhere identical
    # control-flow line onto the new wrapper inside the earlier changed callable,
    # so per-callable body tip-extra must still supersede (PRRT_kwDOSJAM6s6Zvll3).
    parent_dup = (
        "function apply() {\n"
        "  setup();\n"
        "  prepare();\n"
        "  validate();\n"
        "  finalize();\n"
        "  guard.disable();\n"
        "}\n"
        "function other() {\n"
        "  if (false) {\n"
        "  helper();\n"
        "  }\n"
        "}\n"
    )
    commit_dup = (
        "function apply() {\n"
        "  setup();\n"
        "  prepare();\n"
        "  validate();\n"
        "  finalize();\n"
        "  guard.enable();\n"
        "}\n"
        "function other() {\n"
        "  if (false) {\n"
        "  helper();\n"
        "  }\n"
        "}\n"
    )
    assert "apply" in _salvage_changed_binding_names(parent_blob=parent_dup, commit_blob=commit_dup)
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_dup,
        commit_blob=commit_dup,
        head_blob=(
            "function apply() {\n"
            "  if (false) {\n"
            "  setup();\n"
            "  prepare();\n"
            "  validate();\n"
            "  finalize();\n"
            "  guard.enable();\n"
            "  }\n"
            "}\n"
            "function other() {\n"
            "  if (false) {\n"
            "  helper();\n"
            "  }\n"
            "}\n"
        ),
    )
    # Arrow bindings are changed callables too: tip-extra return near the top
    # must supersede when salvage flipped a later call (PRRT_kwDOSJAM6s6ZyaxJ).
    parent_arrow = (
        "const apply = () => {\n"
        "  setup();\n"
        "  prepare();\n"
        "  validate();\n"
        "  finalize();\n"
        "  guard.disable();\n"
        "};\n"
    )
    commit_arrow = (
        "const apply = () => {\n"
        "  setup();\n"
        "  prepare();\n"
        "  validate();\n"
        "  finalize();\n"
        "  guard.enable();\n"
        "};\n"
    )
    assert "apply" in _salvage_changed_binding_names(
        parent_blob=parent_arrow, commit_blob=commit_arrow
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_arrow,
        commit_blob=commit_arrow,
        head_blob=(
            "const apply = () => {\n"
            "  return;\n"
            "  setup();\n"
            "  prepare();\n"
            "  validate();\n"
            "  finalize();\n"
            "  guard.enable();\n"
            "};\n"
        ),
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_arrow,
        commit_blob=commit_arrow,
        head_blob=(
            "const apply = () => {\n"
            "  if (false) {\n"
            "  setup();\n"
            "  prepare();\n"
            "  validate();\n"
            "  finalize();\n"
            "  guard.enable();\n"
            "  }\n"
            "};\n"
        ),
    )
    parent_async_arrow = (
        "const apply = async () => {\n"
        "  setup();\n"
        "  prepare();\n"
        "  validate();\n"
        "  finalize();\n"
        "  guard.disable();\n"
        "};\n"
    )
    commit_async_arrow = (
        "const apply = async () => {\n"
        "  setup();\n"
        "  prepare();\n"
        "  validate();\n"
        "  finalize();\n"
        "  guard.enable();\n"
        "};\n"
    )
    assert _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_async_arrow,
        commit_blob=commit_async_arrow,
        head_blob=(
            "const apply = async () => {\n"
            "  throw new Error('skip');\n"
            "  setup();\n"
            "  prepare();\n"
            "  validate();\n"
            "  finalize();\n"
            "  guard.enable();\n"
            "};\n"
        ),
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_arrow,
        commit_blob=commit_arrow,
        head_blob=commit_arrow + "const other = () => {\n  return;\n};\n",
    )
    # Unchanged nested class-field arrows stay out of class-body tip-extra scope
    # (same leaf retention as nested def/function; PRRT_kwDOSJAM6s6Zvk1G).
    parent_cls_arrow = (
        "class C {\n  FEATURE_ENABLED = false;\n  helper = () => {\n    pass;\n  }\n}\n"
    )
    commit_cls_arrow = (
        "class C {\n  FEATURE_ENABLED = true;\n  helper = () => {\n    pass;\n  }\n}\n"
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_cls_arrow,
        commit_blob=commit_cls_arrow,
        head_blob=(
            "class C {\n"
            "  FEATURE_ENABLED = true;\n"
            "  helper = () => {\n"
            "    return;\n"
            "    pass;\n"
            "  }\n"
            "}\n"
        ),
    )
    # Class headers may contain ``=>`` only in type positions; those must not
    # be treated as arrow callables or nested-helper tip-extra wrongly
    # supersedes leaf salvage (PRRT_kwDOSJAM6s6Zylhi).
    parent_cls_type_arrow = (
        "class C implements Handler<() => void> {\n"
        "  FEATURE_ENABLED = false;\n"
        "  helper = () => {\n"
        "    pass;\n"
        "  }\n"
        "}\n"
    )
    commit_cls_type_arrow = (
        "class C implements Handler<() => void> {\n"
        "  FEATURE_ENABLED = true;\n"
        "  helper = () => {\n"
        "    pass;\n"
        "  }\n"
        "}\n"
    )
    assert not _tip_extra_can_supersede_modified_salvage(
        parent_blob=parent_cls_type_arrow,
        commit_blob=commit_cls_type_arrow,
        head_blob=(
            "class C implements Handler<() => void> {\n"
            "  FEATURE_ENABLED = true;\n"
            "  helper = () => {\n"
            "    return;\n"
            "    pass;\n"
            "  }\n"
            "}\n"
        ),
    )
