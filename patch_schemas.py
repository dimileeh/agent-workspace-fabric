import re

with open("src/awf/api/schemas.py", "r") as f:
    content = f.read()

new_fields = """
    subphase: str | None = None
    last_activity_at: datetime | None = None
    last_log_at: datetime | None = None
    is_stale_running: bool = False
"""

content = content.replace(
    "class WorkspaceResponse(BaseModel):\n    \"\"\"Representation of a workspace in API responses.\"\"\"\n\n    model_config = ConfigDict(from_attributes=True)\n\n    id: str\n    status: WorkspaceStatus\n    version: int\n",
    "class WorkspaceResponse(BaseModel):\n    \"\"\"Representation of a workspace in API responses.\"\"\"\n\n    model_config = ConfigDict(from_attributes=True)\n\n    id: str\n    status: WorkspaceStatus\n    version: int\n" + new_fields
)

content = content.replace(
    "    provider_readiness_preflight: ProviderReadinessPreflightResponse | None = None\n    status: WorkspaceStatus\n    current_phase: str",
    "    provider_readiness_preflight: ProviderReadinessPreflightResponse | None = None\n    status: WorkspaceStatus\n" + new_fields + "    current_phase: str"
)

with open("src/awf/api/schemas.py", "w") as f:
    f.write(content)
print("Patched schemas.py")
