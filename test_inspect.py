from sqlalchemy.orm import declarative_base, relationship
from sqlalchemy import Column, Integer, ForeignKey
from sqlalchemy.orm import class_mapper
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

ws = WS()
# If we haven't added it to session, it's transient
state = instance_state(ws)
print(state.unloaded)
print(state.dict)

try:
    print(getattr(ws, "operations", []))
except Exception as e:
    print("Error:", type(e))
