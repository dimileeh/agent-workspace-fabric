"""ES-module binding readers for JS/TS cross-file callee evidence (issue #1047).

The Python import readers in ``..._ancestry_cross_file_imports`` return nothing
for a JS/TS call site, which used to leave every JS/TS callee on the name-only
rule: ``import { record } from './telemetry'; record()`` was then "fixed" by an
edit to an unrelated ``export function record`` anywhere in the range. This
module is the JS/TS counterpart of those readers. It is pure and dependency
free: a small tokenizer, the import and export tables of one file, a
deliberately over-inclusive shadow detector, and relative-specifier probing.
The Git reads that tie them together live in
``..._ancestry_cross_file_es_evidence``.

Every reader here fails closed. Text the tokenizer cannot lex, an import or
export statement it cannot read, and a specifier it cannot resolve to exactly
one file all yield None, which the evidence gate treats as "no evidence".

The readers live in focused siblings and are re-exported here, so callers and
tests keep importing them from this module: the tokenizer in ``..._es_lexer``,
the import/export tables in ``..._es_bindings``, definition-span and rebinding
analysis in ``..._es_spans``, and module-path resolution in ``..._es_resolution``.
"""

from __future__ import annotations

from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_bindings import (
    _es_import_bindings as _es_import_bindings,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_bindings import (
    _es_module_exports as _es_module_exports,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_bindings import (
    _EsBinding as _EsBinding,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_bindings import (
    _EsExports as _EsExports,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_lexer import (
    _JS_TS_CALL_SITE_SUFFIXES as _JS_TS_CALL_SITE_SUFFIXES,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_lexer import (
    _es_module_tokens as _es_module_tokens,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_lexer import (
    _EsToken as _EsToken,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_lexer import (
    _path_is_es_module_source as _path_is_es_module_source,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_resolution import (
    _directory_manifest_paths as _directory_manifest_paths,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_resolution import (
    _relative_specifier_probe_paths as _relative_specifier_probe_paths,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_resolution import (
    _resolved_module_path as _resolved_module_path,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_assigned_names as _es_assigned_names,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_calls as _es_calls,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_chained_member_refs as _es_chained_member_refs,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_definition_heads as _es_definition_heads,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_definition_span as _es_definition_span,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_direct_member_calls as _es_direct_member_calls,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_line_calls as _es_line_calls,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_line_non_call_names as _es_line_non_call_names,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_module_definition_span as _es_module_definition_span,
)
from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_ancestry_cross_file_es_spans import (
    _es_rebound_names as _es_rebound_names,
)


def _es_ref_target(
    imports: dict[str, _EsBinding],
    rebound: frozenset[str],
    chained: frozenset[tuple[str, str]],
    qualifier: str | None,
    name: str,
) -> tuple[str, str] | None:
    """``(specifier, export_name)`` one callee ref reaches, or None to fail closed.

    A bare call reaches the export its named or default import binds; a call
    on a namespace import reaches the namespace's export of that name. A
    namespace called bare, a named import used as a receiver (a member of an
    object is out of scope), an unbound or rebound key, a receiver the file
    reaches as a property on the anchored line (``chained``, see
    ``_es_chained_member_refs``)
    and an unfollowable binding all fail closed.
    """
    key = name if qualifier is None else qualifier
    binding = imports.get(key)
    if binding is None or binding.specifier is None or key in rebound:
        return None
    if (key, name) in chained:
        return None
    if qualifier is None:
        return None if binding.imported == "*" else (binding.specifier, binding.imported)
    return (binding.specifier, name) if binding.imported == "*" else None
