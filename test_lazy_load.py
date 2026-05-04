import asyncio
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy import Integer, String, ForeignKey, select

Base = declarative_base()

class Parent(Base):
    __tablename__ = 'parent'
    id: Mapped[int] = mapped_column(primary_key=True)
    children = relationship("Child", back_populates="parent")

class Child(Base):
    __tablename__ = 'child'
    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey('parent.id'))
    parent = relationship("Parent", back_populates="children")

async def main():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        session.add(Parent(id=1))
        await session.commit()
    
    async with Session() as session:
        parent = await session.get(Parent, 1)
        # Attempt to access children synchronously
        try:
            print("children:", parent.children)
        except Exception as e:
            print("Exception:", type(e), e)

asyncio.run(main())
