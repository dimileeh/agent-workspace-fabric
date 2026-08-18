"""Pre-push validation fix-pass salvage retention tests (part 016).

Moved out of part_009 to stay under the first-party line limit.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_added_salvage_blob_retained_collection_and_object_mutations() -> None:
    """Collection / Object.* / Reflect tip-extra mutations must drop added salvage.

    Continuation of ``test_added_salvage_blob_retained_rejects_mid_line_modified_occurrence``
    coverage for mutators that leave no intersecting binding or call key.
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass import (
        _added_salvage_blob_retained,
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
    # Object.defineProperties after salvage ``guard.enabled = true`` leaves no
    # binding key and only call names ``Object`` / ``Object.defineProperties``;
    # recognize object-literal property targets or fail closed on opaque ones
    # sharing the salvaged receiver (PRRT_kwDOSJAM6s6ZzifG).
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\nObject.defineProperties(guard, {enabled: {value: false}})\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            'guard.enabled = true\nObject.defineProperties(guard, {"enabled": {value: false}})\n'
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\n"
            "globalThis.Object.defineProperties(guard, {enabled: {value: false}})\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob="guard.enabled = true\nObject.defineProperties(guard, props)\n",
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\n// Object.defineProperties(guard, {enabled: {value: false}})\n"
        ),
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\nObject.defineProperties(other, {enabled: {value: false}})\n"
        ),
    )
    assert _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\nObject.defineProperties(guard, {other: {value: false}})\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=(
            "guard.enabled = true\n"
            "Object.defineProperties(\n  guard,\n  {enabled: {value: false}}\n);\n"
        ),
    )
    assert not _added_salvage_blob_retained(
        commit_blob="guard.enabled = true\n",
        head_blob=("guard.enabled = true\nObject.defineProperties(\n  guard,\n  props\n);\n"),
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
