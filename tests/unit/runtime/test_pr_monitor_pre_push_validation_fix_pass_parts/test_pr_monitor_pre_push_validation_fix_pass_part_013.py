"""Coverage edges for salvage call-scan masking helpers (part 013).

Moved out of part_009 to stay under the first-party line limit.
"""

from __future__ import annotations

import pytest


@pytest.mark.unit
def test_call_site_names_mask_js_template_literal_edges() -> None:
    """JS template static text is masked; ``${...}`` edges stay scannable.

    Covers escapes, nested braces/strings/templates, regex/block-comment braces
    inside interpolations, and unclosed forms so salvage retention does not
    treat static template text as tip-extra calls (PRRT_kwDOSJAM6s6ZtJG8,
    PRRT_kwDOSJAM6s6ZtYk3).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
        _call_site_names_for_line,
    )

    assert _call_site_names_for_line("const marker = `guard.disable()`;") == ()
    assert _call_site_names_for_line("const marker = `guard.disable()`") == ()
    assert _call_site_names_for_line(r"const marker = `a\`guard.disable()`;") == ()
    assert _call_site_names_for_line("const marker = `${guard.disable()}`;") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("const marker = `${foo({a: 1}) + guard.disable()}`;") == (
        "foo",
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('const marker = `${obj["}"] + guard.disable()}`;') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line(r'const marker = `${obj["\""] + 1}`;') == ()
    assert _call_site_names_for_line("const marker = `${obj['}'] + 1}`;") == ()
    assert _call_site_names_for_line(r"const marker = `${obj['\''] + 1}`;") == ()
    assert _call_site_names_for_line("const marker = `${`x${guard.disable()}`}`;") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line(r"const marker = `${`a\`b`}`;") == ()
    assert _call_site_names_for_line("const marker = `${`a${b`;") == ()
    assert _call_site_names_for_line("const marker = `${guard.disable()`") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("const marker = `${`x${guard.disable()}`") == (
        "guard",
        "guard.disable",
    )
    # ``}`` inside regex / block-comment bodies must not close ``${...}`` early
    # or a following real call is blanked as static template text
    # (PRRT_kwDOSJAM6s6ZtYk3).
    assert _call_site_names_for_line("const marker = `${/}/; guard.disable()}`;") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("const marker = `${/* } */ guard.disable()}`;") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("const marker = `${/guard.disable()/}`;") == ()
    assert _call_site_names_for_line("const marker = `${/* guard.disable() */}`;") == ()


@pytest.mark.unit
def test_call_site_names_mask_python_fstring_edges() -> None:
    """Python f-string static text is masked; ``{...}`` edges stay scannable.

    Mirrors JS template interpolation handling so tip-extra call detection does
    not miss ``f\"{guard.disable()}\"`` / triple-quoted forms, and does not
    treat static ``f\"guard.disable()\"`` as a call (PRRT_kwDOSJAM6s6Zt7Go).
    """
    from awf.runtime.pr_monitor_runner.pre_push_validation_fix_pass_presence_calls import (
        _call_site_names_for_line,
    )

    assert _call_site_names_for_line('marker = f"guard.disable()"') == ()
    assert _call_site_names_for_line("marker = f'guard.disable()'") == ()
    assert _call_site_names_for_line('marker = f"{guard.disable()}"') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("marker = f'{guard.disable()}'") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('marker = rf"{guard.disable()}"') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('marker = Fr"{guard.disable()}"') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('marker = f"""{guard.disable()}"""') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("marker = f'''{guard.disable()}'''") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('marker = f"""guard.disable()"""') == ()
    assert _call_site_names_for_line(r'marker = f"a\"guard.disable()"') == ()
    assert _call_site_names_for_line('marker = f"{{guard.disable()}"') == ()
    assert _call_site_names_for_line('marker = f"{foo({a: 1}) + guard.disable()}"') == (
        "foo",
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("marker = f\"{obj[']'] + guard.disable()}\"") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line("marker = f\"{'guard.disable()'}\"") == ()
    assert _call_site_names_for_line("marker = f\"{f'{guard.disable()}'}\"") == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('marker = f"{guard.disable()"') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('marker = f"{guard.disable()!r}"') == (
        "guard",
        "guard.disable",
    )
    assert _call_site_names_for_line('marker = f"{guard.disable():.2f}"') == (
        "guard",
        "guard.disable",
    )
    # Non-f strings still blank fully (including literal braces).
    assert _call_site_names_for_line('marker = "{guard.disable()}"') == ()
