import re

with open("tests/unit/api/test_workspaces.py", "r") as f:
    content = f.read()

test_logic = """
    @pytest.mark.unit
    async def test_stale_running_flag(
        self,
        client: AsyncClient,
        engine: AsyncEngine,
    ) -> None:
        create = await client.post("/v1/workspaces", json=_MINIMAL_BODY)
        ws_id = create.json()["workspace_id"]
        
        # update DB directly to running and last_activity_at old
        from sqlalchemy.ext.asyncio import AsyncSession
        from awf.db.models import Workspace
        async with AsyncSession(engine) as session:
            from sqlalchemy import update
            from datetime import datetime, UTC, timedelta
            await session.execute(
                update(Workspace).where(Workspace.id == ws_id).values(
                    status="running",
                    subphase="agent",
                    last_activity_at=datetime.now(UTC) - timedelta(minutes=15)
                )
            )
            await session.commit()
            
        response = await client.get(f"/v1/workspaces/{ws_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "running"
        assert body["subphase"] == "agent"
        assert body["is_stale_running"] is True

"""

target = "class TestGetWorkspace:\n"
content = content.replace(target, target + test_logic)

with open("tests/unit/api/test_workspaces.py", "w") as f:
    f.write(content)
print("Patched test_workspaces.py")
