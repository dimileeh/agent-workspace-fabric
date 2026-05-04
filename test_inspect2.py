from sqlalchemy.orm import declarative_base, relationship, Session
from sqlalchemy import Column, Integer, ForeignKey, create_engine
from sqlalchemy.orm.attributes import instance_state

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

session = Session(engine)
ws = WS()
session.add(ws)
session.commit()

ws = session.query(WS).first()
state = instance_state(ws)
print("Unloaded:", state.unloaded)

# detached
session.close()

try:
    print(getattr(ws, "operations", []))
except Exception as e:
    print("Error:", type(e))

