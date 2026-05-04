import asyncio
from sqlalchemy import select, inspect
from awf.db.models import Workspace
from awf.db.session import make_session_factory
from sqlalchemy.ext.asyncio import create_async_engine

async def test():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        from awf.db.base import Base
        await conn.run_sync(Base.metadata.create_all)
        
    factory = make_session_factory(engine)
    async with factory() as s:
        ws = Workspace(
            id="ws_1", status="requested", repo_url="x", branch_base="y"
        )
        s.add(ws)
        await s.commit()

    async with factory() as s:
        ws2 = await s.get(Workspace, "ws_1")
        state = inspect(ws2)
        print("unloaded:", state.unloaded)

if __name__ == "__main__":
    asyncio.run(test())
