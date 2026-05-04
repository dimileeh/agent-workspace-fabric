import sys

filename = "src/awf/service/workspace_observability.py"
with open(filename, "r") as f:
    content = f.read()

# Revert previous
old_code_1 = """    from sqlalchemy import inspect
    insp = inspect(workspace, raiseerr=False)
    if insp is not None and "operations" in insp.unloaded:
        raise RuntimeError("operations relationship must be preloaded to compute usage summary")

    operations = getattr(workspace, "operations", [])"""

new_code_1 = """    operations = getattr(workspace, "operations", [])"""

content = content.replace(old_code_1, new_code_1)

# Now apply proper fix using sqlalchemy.orm.attributes.instance_state or inspect
new_code_2 = """    from sqlalchemy import inspect
    insp = inspect(workspace, raiseerr=False)
    if insp is not None and "operations" in insp.unloaded:
        raise ValueError("operations relationship must be preloaded to compute usage summary")

    operations = getattr(workspace, "operations", [])"""

content = content.replace(new_code_1, new_code_2)

with open(filename, "w") as f:
    f.write(content)
