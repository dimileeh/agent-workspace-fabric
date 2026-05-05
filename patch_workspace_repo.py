import re

with open("src/awf/db/repositories.py", "r") as f:
    content = f.read()

new_method = """
    async def update_activity(self, workspace_id: str, *, subphase: str | None = None) -> None:
        from sqlalchemy import update
        from datetime import datetime, UTC
        stmt = update(Workspace).where(Workspace.id == workspace_id).values(
            last_activity_at=datetime.now(UTC),
        )
        if subphase is not None:
            stmt = stmt.values(subphase=subphase)
        await self._session.execute(stmt)
        await self._session.flush()
"""

target = "    async def get(self, workspace_id: str) -> Workspace | None:"
if target in content:
    content = content.replace(target, new_method + "\n" + target)
    with open("src/awf/db/repositories.py", "w") as f:
        f.write(content)
    print("Patched WorkspaceRepository")
else:
    print("Target not found")
