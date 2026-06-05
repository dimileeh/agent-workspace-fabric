from __future__ import annotations

import ast
import json
import re
import shlex
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import pytest
import yaml

from awf.cli.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
README_PATH = REPO_ROOT / "README.md"
DOCS_INDEX_CANDIDATES = (README_PATH, REPO_ROOT / "docs" / "README.md")

ROOT_PUBLIC_DOC_NAMES = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "RELEASING.md",
}
INTERNAL_DOC_PREFIXES = ("docs/awf-plans/",)
PLANNING_DOC_NAMES = {
    "docs/awf_prd_v2.2.md",
    "docs/PLAN_MVP.md",
    "docs/PLAN_PR_MONITOR.md",
    "docs/PLAN_RELEASE_PR_SYNC.md",
}
OPTIONAL_PUBLIC_GUIDES = {
    "docs/TROUBLESHOOTING.md",
}
COPY_PASTE_DOC_HINTS = {
    "docs/PROJECT_ONBOARDING.md",
}

LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\((?P<target>[^)]+)\)")
FENCE_DELIMITER_RE = re.compile(r"^ {0,3}```", re.MULTILINE)
OPENING_FENCE_RE = re.compile(
    r"^(?P<indent> {0,3})(?P<delimiter>`{3,})(?P<language>[A-Za-z0-9_+.-]*)[^\n]*$",
)
AWF_COMMAND_RE = re.compile(r"(?<![\w./-])awf(?P<tail>\s+[^`\n|;)]*)")


@dataclass(frozen=True)
class MarkdownFence:
    path: str
    line: int
    language: str
    body: str


@dataclass(frozen=True)
class AwfCommandMention:
    path: str
    command: str
    command_path: tuple[str, ...]


def _public_docs() -> set[str]:
    public_docs = _all_public_markdown_docs()
    public_docs.update(_readme_public_doc_links())
    public_docs.update(_present_docs(OPTIONAL_PUBLIC_GUIDES))
    return {doc for doc in public_docs if _is_public_doc_path(doc)}


def _all_public_markdown_docs() -> set[str]:
    docs_dir = REPO_ROOT / "docs"
    if not docs_dir.exists():
        return set()

    return {
        path.relative_to(REPO_ROOT).as_posix()
        for path in docs_dir.rglob("*.md")
        if _is_public_doc_path(path.relative_to(REPO_ROOT).as_posix())
    }


def _docs_index_links() -> set[str]:
    links: set[str] = set()
    for index_path in DOCS_INDEX_CANDIDATES:
        if index_path.exists():
            links.update(_markdown_doc_links(index_path))
    return {link for link in links if _is_public_doc_path(link)}


def _readme_public_doc_links() -> set[str]:
    return {link for link in _markdown_doc_links(README_PATH) if _is_public_doc_path(link)}


def _markdown_doc_links(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {
        resolved
        for target in LINK_RE.findall(text)
        if (resolved := _resolve_markdown_link(path, target)) is not None
    }


def _resolve_markdown_link(source_path: Path, target: str) -> str | None:
    target = target.strip()
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    raw_path = unquote(parsed.path)
    if not raw_path or raw_path.startswith("#") or not raw_path.endswith(".md"):
        return None

    base_dir = source_path.parent
    resolved = (base_dir / raw_path).resolve()
    try:
        rel_path = resolved.relative_to(REPO_ROOT)
    except ValueError:
        return None
    return rel_path.as_posix()


def _is_public_doc_path(rel_path: str) -> bool:
    if rel_path in ROOT_PUBLIC_DOC_NAMES:
        return True
    if not rel_path.startswith("docs/"):
        return False
    if any(rel_path.startswith(prefix) for prefix in INTERNAL_DOC_PREFIXES):
        return False
    return rel_path not in PLANNING_DOC_NAMES


def _present_docs(candidates: Iterable[str]) -> set[str]:
    return {rel_path for rel_path in candidates if (REPO_ROOT / rel_path).exists()}


def _typer_command_tree(typer_app: object) -> set[tuple[str, ...]]:
    command_paths: set[tuple[str, ...]] = set()
    for command in typer_app.registered_commands:
        command_paths.add((_command_name(command),))
    for group in typer_app.registered_groups:
        group_name = group.name
        for command in group.typer_instance.registered_commands:
            command_paths.add((group_name, _command_name(command)))
    return command_paths


def _command_name(command: object) -> str:
    explicit_name = command.name
    if explicit_name:
        return explicit_name
    return command.callback.__name__.replace("_", "-")


def _awf_command_mentions(paths: Iterable[Path]) -> list[AwfCommandMention]:
    root_groups = {path[0] for path in _typer_command_tree(app) if len(path) == 2}
    mentions: list[AwfCommandMention] = []
    for rel_path in paths:
        doc_path = REPO_ROOT / rel_path
        if not doc_path.exists():
            continue
        text = doc_path.read_text(encoding="utf-8")
        collapsed = re.sub(r"\\\n\s*", " ", text)
        for line in collapsed.splitlines():
            if _ignore_awf_command_line(line):
                continue
            for match in AWF_COMMAND_RE.finditer(line):
                raw_command = f"awf{match.group('tail')}".strip()
                command_path = _awf_command_path(raw_command, root_groups)
                if command_path is None:
                    continue
                mentions.append(
                    AwfCommandMention(
                        path=rel_path.as_posix(),
                        command=raw_command,
                        command_path=command_path,
                    )
                )
    return mentions


def _ignore_awf_command_line(line: str) -> bool:
    lowered = line.lower()
    return "currently not implemented" in lowered or "future" in lowered


def _awf_command_path(
    raw_command: str,
    root_groups: set[str],
) -> tuple[str, ...] | None:
    try:
        tokens = shlex.split(raw_command, comments=True)
    except ValueError:
        tokens = raw_command.split()
    tokens = [_clean_token(token) for token in tokens]
    if not tokens or tokens[0] != "awf":
        return None
    if len(tokens) == 1:
        return None

    first = tokens[1]
    if not _looks_like_command_token(first):
        return None
    if first not in root_groups:
        return (first,)
    if len(tokens) < 3:
        return None

    second = tokens[2]
    if not _looks_like_command_token(second):
        return None
    return (first, second)


def _clean_token(token: str) -> str:
    return token.strip("`'\".,: ")


def _looks_like_command_token(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    if token.startswith(("<", "{", "$")):
        return False
    if token in {".", "...", "&&", "||", "|", ";"}:
        return False
    return not ("/" in token or "=" in token)


def _copy_paste_docs() -> set[str]:
    docs = _present_docs(COPY_PASTE_DOC_HINTS)
    docs.update(
        rel_path
        for rel_path in _present_docs(_public_docs())
        if "copy-paste" in (REPO_ROOT / rel_path).read_text(encoding="utf-8").lower()
        and rel_path.endswith(".md")
    )
    return docs


def _fence_delimiter_count_is_even(text: str) -> bool:
    return len(FENCE_DELIMITER_RE.findall(text)) % 2 == 0


def _markdown_fences(
    rel_path: str,
    text: str,
    *,
    line_offset: int = 0,
) -> list[MarkdownFence]:
    fences: list[MarkdownFence] = []
    lines = text.splitlines()
    line_index = 0

    while line_index < len(lines):
        opening_match = OPENING_FENCE_RE.match(lines[line_index])
        if opening_match is None:
            line_index += 1
            continue

        opening_line = line_offset + line_index + 1
        indent_width = len(opening_match.group("indent"))
        delimiter_width = len(opening_match.group("delimiter"))
        body_lines: list[str] = []
        line_index += 1

        while line_index < len(lines):
            line = lines[line_index]
            if _is_closing_fence(line, delimiter_width):
                fences.append(
                    MarkdownFence(
                        path=rel_path,
                        line=opening_line,
                        language=opening_match.group("language").lower(),
                        body="\n".join(body_lines).strip("\n"),
                    )
                )
                break

            body_lines.append(_strip_fence_body_indent(line, indent_width))
            line_index += 1
        line_index += 1
    return fences


def _is_closing_fence(line: str, delimiter_width: int) -> bool:
    match = re.match(r"^ {0,3}(?P<delimiter>`{3,})[ \t]*$", line)
    return match is not None and len(match.group("delimiter")) >= delimiter_width


def _strip_fence_body_indent(line: str, indent_width: int) -> str:
    leading_spaces = len(line) - len(line.lstrip(" "))
    return line[min(indent_width, leading_spaces) :]


def _assert_snippet_syntax(fence: MarkdownFence) -> None:
    if not fence.body.strip():
        pytest.fail(f"{fence.path}:{fence.line} has an empty copy-paste snippet")

    language = fence.language
    if language == "json":
        json.loads(fence.body)
    elif language in {"yaml", "yml"}:
        yaml.safe_load(fence.body)
    elif language == "python":
        ast.parse(fence.body)
    elif language in {"bash", "sh", "shell"}:
        script = _strip_shell_prompts(fence.body)
        try:
            result = subprocess.run(
                ["bash", "-n"],
                input=script,
                text=True,
                capture_output=True,
                check=False,
                timeout=5,
            )
        except FileNotFoundError:
            pytest.fail(
                f"{fence.path}:{fence.line} cannot validate shell snippet because "
                "bash is not available on PATH"
            )
        assert result.returncode == 0, (
            f"{fence.path}:{fence.line} has invalid shell syntax: {result.stderr.strip()}"
        )
    else:
        assert fence.body.strip(), f"{fence.path}:{fence.line} has empty text snippet"


def _strip_shell_prompts(script: str) -> str:
    stripped_lines: list[str] = []
    for line in script.splitlines():
        if line.startswith(("$ ", "> ")):
            stripped_lines.append(line[2:])
        else:
            stripped_lines.append(line)
    return "\n".join(stripped_lines)
