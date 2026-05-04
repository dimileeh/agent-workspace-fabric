import sys

filename = "src/awf/service/workspace_observability.py"
with open(filename, "r") as f:
    content = f.read()

old_code = """def _latest_recovery_operation(
    workspace: Workspace,
    *,
    active_only: bool,
) -> _RecoveryOperationLike | None:
    operations = cast(Sequence[object], getattr(workspace, "operations", None) or [])"""

new_code = """def _latest_recovery_operation(
    workspace: Workspace,
    *,
    active_only: bool,
) -> _RecoveryOperationLike | None:
    from sqlalchemy import inspect
    insp = inspect(workspace, raiseerr=False)
    if insp is not None and "operations" in insp.unloaded:
        raise ValueError("operations relationship must be preloaded")

    operations = cast(Sequence[object], getattr(workspace, "operations", None) or [])"""

content = content.replace(old_code, new_code)

with open(filename, "w") as f:
    f.write(content)
