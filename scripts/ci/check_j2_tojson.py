#!/usr/bin/env python3
"""Fail CI when structured-config Jinja2 values render without tojson escaping."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from jinja2 import Environment, TemplateSyntaxError, nodes

APPROVED_FILTERS = frozenset({"tojson"})
ALLOW_DIRECTIVE_PREFIX = "awf-j2-tojson-allow:"
ALLOW_DIRECTIVE_SEPARATOR = " -- "
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class TemplateFile:
    """A template path plus the label to use in diagnostics."""

    path: Path
    label: str


@dataclass(frozen=True)
class Interpolation:
    """A Jinja2 output expression found in a template."""

    line: int
    expression: str


@dataclass(frozen=True)
class AllowDirective:
    """A documented exception for a raw output expression."""

    line: int
    expression: str
    rationale: str


@dataclass(frozen=True)
class Diagnostic:
    """A lint diagnostic tied to a template line."""

    template: str
    line: int
    message: str
    title: str = "Jinja2 raw interpolation"

    def format(self) -> str:
        """Return the diagnostic as a GitHub Actions annotation."""
        message = f"{self.template}:{self.line}: {self.message}"
        return (
            f"::error file={_escape_workflow_command_property(self.template)},"
            f"line={self.line},"
            f"title={_escape_workflow_command_property(self.title)}::"
            f"{_escape_workflow_command_data(message)}"
        )


class TemplateReadError(Exception):
    """Raised when a template cannot be loaded or tokenized."""


_ENVIRONMENT = Environment()


def _escape_workflow_command_data(value: str) -> str:
    """Escape GitHub Actions workflow command message data."""
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_workflow_command_property(value: str) -> str:
    """Escape GitHub Actions workflow command property values."""
    return _escape_workflow_command_data(value).replace(":", "%3A").replace(",", "%2C")


def _format_expression_tokens(tokens: Sequence[str]) -> str:
    """Return a stable, whitespace-normalized representation of expression tokens."""
    normalized = ""
    previous = ""
    for token in tokens:
        if not normalized:
            normalized = token
        elif (
            token in {")", "]", "}", ",", ".", ":"}
            or (token == "(" and previous and _is_identifier_like(previous))
            or previous in {"(", "[", "{", "."}
        ):
            normalized += token
        else:
            normalized += f" {token}"
        previous = token
    return normalized


def _is_identifier_like(token: str) -> bool:
    """Return whether a token can be followed by a call parenthesis."""
    return token.replace("_", "").isalnum()


def _expression_tokens(expression: str) -> list[str]:
    """Lex one Jinja2 expression and return non-whitespace tokens."""
    tokens: list[str] = []
    in_variable = False
    try:
        stream = _ENVIRONMENT.lex(f"{{{{ {expression} }}}}")
        for _line, token_type, value in stream:
            if token_type == "variable_begin":
                in_variable = True
            elif token_type == "variable_end":
                return tokens
            elif in_variable and token_type != "whitespace":
                tokens.append(value)
    except TemplateSyntaxError as exc:
        raise ValueError(f"invalid allowlist expression {expression!r}: {exc.message}") from exc
    return tokens


def _normalize_expression(expression: str) -> str:
    """Normalize expression whitespace for diagnostics and allowlist matching."""
    tokens = _expression_tokens(expression)
    if not tokens:
        raise ValueError("allowlist expression is empty")
    return _format_expression_tokens(tokens)


def _parse_allow_directive(comment: str) -> tuple[str, str] | str | None:
    """Parse an allowlist directive comment.

    Returns ``None`` when the comment is not a directive, an error string when
    the directive is invalid, or a normalized expression plus rationale.
    """
    text = comment.strip()
    if not text.startswith(ALLOW_DIRECTIVE_PREFIX):
        return None

    remainder = text.removeprefix(ALLOW_DIRECTIVE_PREFIX).strip()
    if ALLOW_DIRECTIVE_SEPARATOR not in remainder:
        return "allowlist directive is missing a rationale after ' -- '"

    expression, rationale = remainder.split(ALLOW_DIRECTIVE_SEPARATOR, 1)
    if not rationale.strip():
        return "allowlist directive is missing a rationale"
    try:
        normalized_expression = _normalize_expression(expression)
    except ValueError as exc:
        return str(exc)
    return normalized_expression, rationale.strip()


def _scan_template(
    template: TemplateFile,
) -> tuple[list[Interpolation], list[AllowDirective], list[Diagnostic]]:
    """Read and lex a Jinja2 template for output expressions and allow directives."""
    try:
        source = template.path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TemplateReadError(f"unable to read {template.label}: {exc}") from exc

    interpolations: list[Interpolation] = []
    allow_directives: list[AllowDirective] = []
    diagnostics: list[Diagnostic] = []
    in_variable = False
    variable_line = 0
    variable_tokens: list[str] = []
    in_comment = False
    comment_line = 0
    comment_parts: list[str] = []

    try:
        stream = _ENVIRONMENT.lex(source)
        for line, token_type, value in stream:
            if token_type == "variable_begin":
                in_variable = True
                variable_line = line
                variable_tokens = []
            elif token_type == "variable_end" and in_variable:
                in_variable = False
                expression = _format_expression_tokens(variable_tokens)
                if expression:
                    interpolations.append(Interpolation(line=variable_line, expression=expression))
            elif in_variable:
                if token_type != "whitespace":
                    variable_tokens.append(value)
            elif token_type == "comment_begin":
                in_comment = True
                comment_line = line
                comment_parts = []
            elif token_type == "comment_end" and in_comment:
                in_comment = False
                directive = _parse_allow_directive("".join(comment_parts))
                if isinstance(directive, str):
                    diagnostics.append(
                        Diagnostic(
                            template.label,
                            comment_line,
                            directive,
                            title="Jinja2 allowlist directive",
                        )
                    )
                elif directive is not None:
                    expression, rationale = directive
                    allow_directives.append(
                        AllowDirective(
                            line=comment_line,
                            expression=expression,
                            rationale=rationale,
                        )
                    )
            elif in_comment:
                comment_parts.append(value)
    except TemplateSyntaxError as exc:
        raise TemplateReadError(f"unable to lex {template.label}: {exc.message}") from exc

    return interpolations, allow_directives, diagnostics


def _final_filter(expression: str) -> str | None:
    """Return the final top-level filter name for a Jinja2 output expression."""
    try:
        parsed = _ENVIRONMENT.parse(f"{{{{ {expression} }}}}")
    except TemplateSyntaxError:
        return None
    if len(parsed.body) != 1:
        return None
    output = parsed.body[0]
    if not isinstance(output, nodes.Output):
        return None
    output_nodes = cast(Sequence[object], getattr(output, "nodes", ()))
    if len(output_nodes) != 1:
        return None
    expression_node = output_nodes[0]
    if not isinstance(expression_node, nodes.Filter):
        return None
    return cast(str, getattr(expression_node, "name", ""))


def _is_escaped(expression: str) -> bool:
    """Return whether an expression ends in an approved escaping filter."""
    final_filter = _final_filter(expression)
    return final_filter in APPROVED_FILTERS


def _diagnostics_for_template(template: TemplateFile) -> list[Diagnostic]:
    """Return lint diagnostics for one template."""
    interpolations, allow_directives, diagnostics = _scan_template(template)
    allow_by_expression: dict[str, AllowDirective] = {}
    for directive in allow_directives:
        if directive.expression in allow_by_expression:
            diagnostics.append(
                Diagnostic(
                    template=template.label,
                    line=directive.line,
                    message=(
                        f"duplicate allowlist entry for raw interpolation: {directive.expression}"
                    ),
                    title="Jinja2 allowlist directive",
                )
            )
            continue
        allow_by_expression[directive.expression] = directive
    used_allowlist_expressions: set[str] = set()

    for interpolation in interpolations:
        if _is_escaped(interpolation.expression):
            continue
        if interpolation.expression in allow_by_expression:
            used_allowlist_expressions.add(interpolation.expression)
            continue
        approved = ", ".join(sorted(APPROVED_FILTERS))
        diagnostics.append(
            Diagnostic(
                template=template.label,
                line=interpolation.line,
                message=(
                    "raw Jinja2 interpolation must end in an approved escaping "
                    f"filter ({approved}) or have an allowlist directive: "
                    f"{interpolation.expression}"
                ),
            )
        )

    for directive in allow_by_expression.values():
        if directive.expression not in used_allowlist_expressions:
            diagnostics.append(
                Diagnostic(
                    template=template.label,
                    line=directive.line,
                    message=f"stale allowlist entry for raw interpolation: {directive.expression}",
                    title="Jinja2 allowlist directive",
                )
            )

    return diagnostics


def _discover_tracked_templates(repo_root: Path) -> list[TemplateFile]:
    """Return tracked docker/compose Jinja2 templates from git."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "docker/compose/*.j2"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise TemplateReadError(f"unable to execute git: {exc}") from exc
    if result.returncode != 0:
        stderr = result.stderr.strip() or "git ls-files failed"
        raise TemplateReadError(f"unable to discover tracked compose templates: {stderr}")
    return [
        TemplateFile(path=repo_root / line, label=line)
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def _templates_from_args(paths: Iterable[Path], repo_root: Path) -> list[TemplateFile]:
    """Resolve explicit CLI paths or discover the default tracked templates."""
    path_list = list(paths)
    if not path_list:
        return _discover_tracked_templates(repo_root)
    return [TemplateFile(path=path, label=str(path)) for path in path_list]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Jinja2 tojson checker CLI."""
    parser = argparse.ArgumentParser(description=__doc__, exit_on_error=False)
    parser.add_argument("paths", nargs="*", type=Path)
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as exc:
        print(f"::error title=Jinja2 tojson checker invalid::{exc}", file=sys.stderr)
        return 2

    try:
        templates = _templates_from_args(args.paths, REPO_ROOT)
        diagnostics = [
            diagnostic
            for template in templates
            for diagnostic in _diagnostics_for_template(template)
        ]
    except TemplateReadError as exc:
        print(f"::error title=Jinja2 tojson checker invalid::{exc}", file=sys.stderr)
        return 2

    for diagnostic in diagnostics:
        print(diagnostic.format(), file=sys.stderr)
    return 1 if diagnostics else 0


if __name__ == "__main__":
    raise SystemExit(main())
