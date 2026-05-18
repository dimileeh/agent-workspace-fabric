import tomllib
from pathlib import Path


def test_packaging_metadata() -> None:
    """Test that the pyproject.toml package name and entrypoints match the plan."""
    pyproject_path = Path(__file__).parents[3] / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)

    assert data["project"]["name"] == "agent-workspace-fabric"

    # 2. project.scripts.awf exists as the canonical operator CLI
    scripts = data["project"].get("scripts", {})
    assert "awf" in scripts
    assert "awf-watchdog" not in scripts


def test_readme_install_paths() -> None:
    """Test that the README explicitly documents the primary install paths."""
    readme_path = Path(__file__).parents[3] / "docs" / "GETTING_STARTED.md"
    content = readme_path.read_text()

    assert "uv tool install agent-workspace-fabric" in content
    assert "uv pip install agent-workspace-fabric" in content

    # 4. README preserves the `git clone` path
    assert "git clone" in content, "README must preserve the git clone contributor path"
