import re

with open("src/awf/cli/main.py", "r") as f:
    content = f.read()

dx_text_escaped = "\\n\\nFor first-time users: the recommended first path is to run `awf init` \\nto verify prerequisites and bootstrap your local service stack, followed by \\n`awf init <path>` to prepare your project repository. See docs/CLI_REFERENCE.md \\nfor more details.\\n\\nSafety defaults & Dry-run: Commands that modify local state default to \\ndry-runs or previews unless explicit write flags are passed.\\n\\nMutates: Local state (.env, .awf/), Docker Compose stacks, and Git/GitHub \\nvia the async worker.\\n"

dx_text_docstring = """

For first-time users: the recommended first path is to run `awf init` 
to verify prerequisites and bootstrap your local service stack, followed by 
`awf init <path>` to prepare your project repository. See docs/CLI_REFERENCE.md 
for more details.

Safety defaults & Dry-run: Commands that modify local state default to 
dry-runs or previews unless explicit write flags are passed.

Mutates: Local state (.env, .awf/), Docker Compose stacks, and Git/GitHub 
via the async worker.
"""

# Update app
app_old = '''app = typer.Typer(
    name="awf",
    help="Aira Agent Workspace Fabric — CLI operator surface.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)'''

app_new = f'''app = typer.Typer(
    name="awf",
    help="Aira Agent Workspace Fabric — CLI operator surface."
    "{dx_text_escaped}",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)'''

content = content.replace(app_old, app_new)

# Update workspace_app
ws_app_old = '''workspace_app = typer.Typer(help="Workspace lifecycle (create/inspect/destroy).")'''
ws_app_new = f'''workspace_app = typer.Typer(help="Workspace lifecycle (create/inspect/destroy)."
    "{dx_text_escaped}")'''

content = content.replace(ws_app_old, ws_app_new)

# Update init docstring
init_old = '''"""Bootstrap AWF on this machine, or run local onboarding checks for a project path."""'''
init_new = f'''"""Bootstrap AWF on this machine, or run local onboarding checks for a project path.{dx_text_docstring}"""'''
content = content.replace(init_old, init_new)

# Update service_bootstrap docstring
sb_old = '''"""Start local Postgres, migrations, API, worker, and verify readiness."""'''
sb_new = f'''"""Start local Postgres, migrations, API, worker, and verify readiness.{dx_text_docstring}"""'''
content = content.replace(sb_old, sb_new)


with open("src/awf/cli/main.py", "w") as f:
    f.write(content)
