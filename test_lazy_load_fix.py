from sqlalchemy import inspect
from src.awf.db.models import Workspace

ws = Workspace(id="test")
print(getattr(ws, "operations", []))
