"""Regression: ``pr_monitor.__all__`` must not shrink ``import *`` exports.

``pr_monitor`` re-exports the wire-shape value types that moved into
``pr_monitor_models``. Adding ``__all__`` purely for those re-exports would
silently drop the module's *own* public API (``decide``, the state/config
types, the monitor-action dataclasses) from ``from awf.runtime.pr_monitor
import *`` — which used to export every public name because no ``__all__``
existed. This guards that ``__all__`` stays a superset of the public surface
(PRRT_kwDOSJAM6s6MtQin).
"""

from __future__ import annotations

import awf.runtime.pr_monitor as pr_monitor

# Names this module historically exposed via a bare ``import *`` and that real
# consumers resolve from here (e.g. ``runner.py`` imports ``decide``,
# ``MonitorState``, ``MonitorConfig``; ``merge_loop.py`` imports the actions).
_EXPECTED_PUBLIC_API = {
    "decide",
    "MonitorState",
    "MonitorConfig",
    "OperatorHint",
    "AbortReason",
    "MonitorAction",
    "BOT_REVIEWER_LOGINS",
    "sync_base_no_progress_signature",
    "AddressComments",
    "AddressOperatorHint",
    "ReportCiFailure",
    "RerunTransientCI",
    "SyncBase",
    "WaitForCI",
    "Merge",
    "NotifyHuman",
    "ShortCircuitCompleted",
    "Abort",
}


def test_all_includes_own_public_api() -> None:
    assert set(pr_monitor.__all__) >= _EXPECTED_PUBLIC_API


def test_all_entries_resolve_on_the_module() -> None:
    # No typo'd or stale export: every ``__all__`` name binds to an attribute.
    for name in pr_monitor.__all__:
        assert hasattr(pr_monitor, name), name


def test_star_import_exposes_decision_core() -> None:
    namespace: dict[str, object] = {}
    exec("from awf.runtime.pr_monitor import *", namespace)  # noqa: S102
    assert namespace.keys() >= _EXPECTED_PUBLIC_API
    # The re-exported wire types must keep resolving here too.
    for name in ("PRStatus", "ReviewThread", "CheckFailure"):
        assert name in namespace
