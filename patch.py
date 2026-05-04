import sys

filename = "src/awf/service/workspace_observability.py"
with open(filename, "r") as f:
    content = f.read()

old_code = """    operations = getattr(workspace, "operations", [])

    if operations:"""

new_code = """    from sqlalchemy import inspect
    insp = inspect(workspace, raiseerr=False)
    if insp is not None and "operations" in insp.unloaded:
        raise RuntimeError("operations relationship must be preloaded to compute usage summary")

    operations = getattr(workspace, "operations", [])

    if operations:"""

content = content.replace(old_code, new_code)

with open(filename, "w") as f:
    f.write(content)
