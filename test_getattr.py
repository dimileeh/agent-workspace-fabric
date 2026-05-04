import asyncio
from sqlalchemy import inspect
from awf.db.models import Workspace
from awf.db.repositories import WorkspaceRepository
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Workspace.metadata.create_all)
        
    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        ws = Workspace()
        session.add(ws)
        await session.commit()
        await session.refresh(ws)
        
        try:
            ops = getattr(ws, "operations", [])
            print("ops:", ops)
        except Exception as e:
            print("Exception:", type(e))

asyncio.run(main())
