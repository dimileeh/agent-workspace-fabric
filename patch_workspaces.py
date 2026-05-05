import re

with open("src/awf/service/workspaces.py", "r") as f:
    content = f.read()

stale_logic = """    computed_fields = dict(workspace_observability_payload(workspace))
    from datetime import datetime, UTC
    last_activity_at = getattr(workspace, "last_activity_at", None)
    is_stale_running = False
    if workspace.status == "running" and last_activity_at is not None:
        delta = datetime.now(UTC) - last_activity_at
        if delta.total_seconds() > 600:
            is_stale_running = True
    computed_fields["is_stale_running"] = is_stale_running
"""

content = content.replace(
    "    computed_fields = dict(workspace_observability_payload(workspace))\n",
    stale_logic
)

with open("src/awf/service/workspaces.py", "w") as f:
    f.write(content)
print("Patched workspaces.py")
