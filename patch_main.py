import re

with open("src/awf/cli/main.py", "r") as f:
    content = f.read()

# Add _DX_HELP right after _COMMON_HELP_TEXT definition block
dx_help_str = '_DX_HELP = "DX smoke proof: validate local service, profile, and PR path."\n'
content = content.replace('app = typer.Typer(', dx_help_str + '\napp = typer.Typer(', 1)

# Update smoke_app
content = content.replace(
    'smoke_app = typer.Typer(help="DX smoke proof: validate local service, profile, PR path.")',
    'smoke_app = typer.Typer(help=_DX_HELP)'
)

# Update @smoke_app.command("run")
content = content.replace(
    '@smoke_app.command("run")',
    '@smoke_app.command("run", help=_DX_HELP)'
)

# Remove the docstring in smoke_run
content = content.replace(
    '    """Run a DX smoke proof validating local service, profile, and PR path."""\n',
    ''
)

with open("src/awf/cli/main.py", "w") as f:
    f.write(content)
