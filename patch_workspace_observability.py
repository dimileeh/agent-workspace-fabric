import re

with open("src/awf/service/workspace_observability.py", "r") as f:
    content = f.read()

stale_logic = """    from datetime import datetime, UTC
    last_activity_at = getattr(ws, "last_activity_at", None)
    is_stale_running = False
    if ws.status == "running" and last_activity_at is not None:
        delta = datetime.now(UTC) - last_activity_at
        if delta.total_seconds() > 600:
            is_stale_running = True

    return WorkspaceOverviewResponse(
        subphase=getattr(ws, "subphase", None),
        last_activity_at=last_activity_at,
        last_log_at=getattr(ws, "last_log_at", None),
        is_stale_running=is_stale_running,
"""

content = content.replace(
    "    return WorkspaceOverviewResponse(\n",
    stale_logic
)

with open("src/awf/service/workspace_observability.py", "w") as f:
    f.write(content)
print("Patched workspace_observability.py")
