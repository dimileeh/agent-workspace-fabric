from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PARITY_DOC = REPO_ROOT / "docs" / "MCP_CLIENT_PARITY.md"


def _parse_markdown_table(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    in_table = False
    headers: list[str] = []
    rows: list[dict[str, str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            if in_table:
                in_table = False
            continue
        cells = [c.strip() for c in stripped.split("|")]
        cells = [c for c in cells if c != ""]
        if not headers:
            headers = cells
            in_table = True
            continue
        if all(set(c) <= {"-", ":", " "} for c in cells):
            continue
        row = {}
        for i, h in enumerate(headers):
            row[h] = cells[i] if i < len(cells) else ""
        rows.append(row)
    return rows


def _parity_rows() -> list[dict[str, str]]:
    doc = PARITY_DOC.read_text(encoding="utf-8")
    rows = _parse_markdown_table(doc)
    assert rows, "Parity matrix table should not be empty"
    return rows


def _strip_backticks(s: str) -> str:
    return re.sub(r"`+", "", s)
