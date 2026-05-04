import asyncio
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

Base = declarative_base()

class Op(Base):
    __tablename__ = 'op'
    id = Column(Integer, primary_key=True)
    ws_id = Column(Integer, ForeignKey('ws.id'))

class WS(Base):
    __tablename__ = 'ws'
    id = Column(Integer, primary_key=True)
    operations = relationship("Op", lazy="selectin")

async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        ws = WS()
        session.add(ws)
        await session.commit()
    
    # New session to fetch without loading operations
    async with async_session() as session:
        result = await session.execute(
            __import__('sqlalchemy').select(WS)
            .execution_options(compiled_cache=None) # Just for clean start
        )
        ws_fetched = result.scalar_one()
        
        try:
            # this would raise MissingGreenletException because it's unloaded
            ops = getattr(ws_fetched, "operations", None)
            print("OPS IS:", ops)
        except Exception as e:
            print("ERROR IS:", type(e))

asyncio.run(main())
