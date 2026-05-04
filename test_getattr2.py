import asyncio
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy import create_engine
import traceback

Base = declarative_base()

class Op(Base):
    __tablename__ = 'op'
    id = Column(Integer, primary_key=True)
    ws_id = Column(Integer, ForeignKey('ws.id'))

class WS(Base):
    __tablename__ = 'ws'
    id = Column(Integer, primary_key=True)
    operations = relationship("Op")

engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(engine)
Session = sessionmaker(engine)
session = Session()

ws = WS()
session.add(ws)
session.commit()

session.close()

try:
    print(getattr(ws, "operations", []))
except Exception as e:
    print(f"Exception: {type(e)}")
