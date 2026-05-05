import re

with open("src/awf/control/executor.py", "r") as f:
    content = f.read()

new_method = """
    async def _update_subphase(self, workspace_id: str, subphase: str) -> None:
        async with self._session_factory() as session:
            repo = WorkspaceRepository(session)
            await repo.update_activity(workspace_id, subphase=subphase)
            await session.commit()
"""

target = "    async def _recheck_status("
if target in content:
    content = content.replace(target, new_method + "\n" + target)
    with open("src/awf/control/executor.py", "w") as f:
        f.write(content)
    print("Patched WorkspaceExecutor helper")
else:
    print("Target not found")
