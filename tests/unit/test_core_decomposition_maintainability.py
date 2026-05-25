"""Maintainability guardrails for decomposed AWF core packages."""

from __future__ import annotations

import ast
from pathlib import Path

CORE_PACKAGE_DIRS = (
    Path("src/awf/control/executor"),
    Path("src/awf/runtime/pr_monitor_runner"),
    Path("src/awf/control/worker"),
)
MAX_CORE_FILE_LINES = 1_500
PUBLIC_FACADES = {
    Path("src/awf/control/executor/__init__.py"): {
        "ExecutorConfig",
        "WorkspaceExecutor",
    },
    Path("src/awf/runtime/pr_monitor_runner/__init__.py"): {
        "MonitorRunnerConfig",
        "PostMergeTargetReconciler",
        "PullRequestMonitorRunner",
    },
    Path("src/awf/control/worker/__init__.py"): {
        "ControlWorker",
        "SCHEDULER_SQL_AGE_BOOST_DIALECTS",
        "WorkerConfig",
    },
}
ORCHESTRATOR_MODULE_IMPORTS = {
    Path("src/awf/control/executor"): "awf.control.executor.base",
    Path("src/awf/runtime/pr_monitor_runner"): "awf.runtime.pr_monitor_runner.runner",
    Path("src/awf/control/worker"): "awf.control.worker.manager",
}
ORCHESTRATOR_FILES = {
    Path("src/awf/control/executor/base.py"): {
        "ExecutorConfig",
        "WorkspaceExecutor",
    },
    Path("src/awf/runtime/pr_monitor_runner/runner.py"): {
        "MonitorRunnerConfig",
        "PostMergeTargetReconciler",
        "PullRequestMonitorRunner",
    },
    Path("src/awf/control/worker/manager.py"): {
        "ControlWorker",
        "WorkerConfig",
    },
}


def _python_files() -> list[Path]:
    return sorted(path for root in CORE_PACKAGE_DIRS for path in root.glob("*.py"))


def test_decomposed_core_files_stay_under_line_limit() -> None:
    oversized = {
        path.as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in _python_files()
        if len(path.read_text(encoding="utf-8").splitlines()) > MAX_CORE_FILE_LINES
    }

    assert oversized == {}


def test_decomposed_core_has_no_hydration_or_file_level_suppressions() -> None:
    banned_patterns = (
        "_hydrate",
        "globals()",
        "__dict__.update",
        "# mypy: ignore-errors",
        "# ruff: noqa: E402, F401, F821",
    )
    offenders: dict[str, list[str]] = {}
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        matches = [pattern for pattern in banned_patterns if pattern in text]
        if matches:
            offenders[path.as_posix()] = matches

    assert offenders == {}


def test_public_facades_export_only_explicit_compatibility_names() -> None:
    for path, expected in PUBLIC_FACADES.items():
        module = ast.parse(path.read_text(encoding="utf-8"))
        all_assignments = [
            node
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
        ]
        assert len(all_assignments) == 1
        exported = {
            element.value
            for element in all_assignments[0].value.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
        assert exported == expected


def test_public_facades_do_not_dynamic_scan_private_modules() -> None:
    offenders: dict[str, list[str]] = {}
    for path in PUBLIC_FACADES:
        module = ast.parse(path.read_text(encoding="utf-8"))
        issues: list[str] = []
        for node in ast.walk(module):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "__getattr__"
            ):
                issues.append("__getattr__")
            if isinstance(node, ast.ImportFrom) and node.module == "importlib":
                imported = {alias.name for alias in node.names}
                if "import_module" in imported:
                    issues.append("import_module")
            if isinstance(node, ast.Name) and node.id == "import_module":
                issues.append("import_module")
            if isinstance(node, ast.Assign):
                assigned_names = {
                    target.id for target in node.targets if isinstance(target, ast.Name)
                }
                if "_COMPAT_MODULE_NAMES" in assigned_names:
                    issues.append("_COMPAT_MODULE_NAMES")
        if issues:
            offenders[path.as_posix()] = sorted(set(issues))

    assert offenders == {}


def test_implementation_modules_do_not_import_from_orchestrators() -> None:
    offenders: dict[str, list[str]] = {}
    for root, banned_module in ORCHESTRATOR_MODULE_IMPORTS.items():
        for path in sorted(root.glob("*.py")):
            if path.name in {"__init__.py", "base.py", "runner.py", "manager.py"}:
                continue
            module = ast.parse(path.read_text(encoding="utf-8"))
            imported_modules = [
                node.module
                for node in ast.walk(module)
                if isinstance(node, ast.ImportFrom) and node.module == banned_module
            ]
            if imported_modules:
                offenders[path.as_posix()] = imported_modules

    assert offenders == {}


def test_orchestrators_do_not_expose_private_import_barrels() -> None:
    offenders: dict[str, set[str]] = {}
    for path, allowed in ORCHESTRATOR_FILES.items():
        module = ast.parse(path.read_text(encoding="utf-8"))
        all_assignments = [
            node
            for node in module.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            )
        ]
        for assignment in all_assignments:
            exported = {
                element.value
                for element in assignment.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
            disallowed = exported - allowed
            if disallowed:
                offenders[path.as_posix()] = disallowed

    assert offenders == {}


def test_tests_import_private_helpers_from_implementation_modules() -> None:
    package_roots = {
        "awf.control.executor",
        "awf.control.worker",
        "awf.runtime.pr_monitor_runner",
    }
    offenders: dict[str, list[str]] = {}
    for path in sorted(Path("tests/unit").rglob("test_*.py")):
        module = ast.parse(path.read_text(encoding="utf-8"))
        private_imports: list[str] = []
        for node in ast.walk(module):
            if isinstance(node, ast.ImportFrom) and node.module in package_roots:
                private_imports.extend(
                    alias.name for alias in node.names if alias.name.startswith("_")
                )
        if private_imports:
            offenders[path.as_posix()] = sorted(private_imports)

    assert offenders == {}


def test_compatibility_imports_remain_available() -> None:
    from awf.control.executor import WorkspaceExecutor
    from awf.control.worker import ControlWorker
    from awf.db.repositories import WorkspaceRepository
    from awf.runtime.pr_monitor_runner import PullRequestMonitorRunner

    assert WorkspaceExecutor.__name__ == "WorkspaceExecutor"
    assert ControlWorker.__name__ == "ControlWorker"
    assert PullRequestMonitorRunner.__name__ == "PullRequestMonitorRunner"
    assert WorkspaceRepository.__name__ == "WorkspaceRepository"
