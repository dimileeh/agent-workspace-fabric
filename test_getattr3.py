import asyncio
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
import os

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
    db_path = "sqlite+aiosqlite:///:memory:"
    # fallback to sqlite if aiosqlite not installed, wait we can just use sqlite with sync and see if it throws?
    # but the app uses async. Let's install aiosqlite for the test.
