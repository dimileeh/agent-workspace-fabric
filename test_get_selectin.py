import asyncio
from awf.db.session import async_session_factory
from awf.db.repositories import WorkspaceRepository

async def test():
    async with async_session_factory() as session:
        # just list the first workspace and check its state
        ws_list = await WorkspaceRepository(session).list(limit=1)
        if not ws_list:
            print("No workspaces")
            return
        ws_id = ws_list[0].id
        
        # now clear session and get it
        await session.close()
        
    async with async_session_factory() as session:
        ws = await session.get(WorkspaceRepository(session)._session.bind, ws_id) # wait, no
        ws = await WorkspaceRepository(session).get(ws_id)
        from sqlalchemy import inspect
        state = inspect(ws)
        print("unloaded:", state.unloaded)

if __name__ == "__main__":
    asyncio.run(test())
