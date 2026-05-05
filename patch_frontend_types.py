import re

with open("apps/console/lib/types.ts", "r") as f:
    content = f.read()

new_fields = """
  subphase: string | null;
  last_activity_at: string | null;
  last_log_at: string | null;
  is_stale_running: boolean;
"""

# Patch Workspace
content = content.replace(
    "  version: number;\n",
    "  version: number;\n" + new_fields
)

# Patch WorkspaceOverview
content = content.replace(
    "  status: WorkspaceStatus;\n",
    "  status: WorkspaceStatus;\n" + new_fields
)

with open("apps/console/lib/types.ts", "w") as f:
    f.write(content)
print("Patched types.ts")
