"""`widgets.muted` - the one definition of the panel's secondary-text style.

Roughly forty labels across the panel, the mesh screen, Settings and the wizards
take their stylesheet from this builder, and two of them append further
declarations to what it returns. Neither property is visible in a snapshot on
macOS (Qt resolves the generic `monospace` family to the default face there), so
they are pinned here instead.
"""

from __future__ import annotations

import pytest

from diplomat_app.widgets import muted


def _declarations(css: str) -> dict[str, str]:
    """Parse a stylesheet fragment into {property: value}, rejecting duplicates."""
    out: dict[str, str] = {}
    for chunk in css.split(";"):
        if not chunk.strip():
            continue
        prop, _, value = chunk.partition(":")
        prop = prop.strip()
        assert prop not in out, f"duplicate declaration for {prop!r} in {css!r}"
        out[prop] = value.strip()
    return out


def test_the_default_is_ten_pixel_dimmed_text():
    assert _declarations(muted()) == {"color": "palette(mid)", "font-size": "10px"}


@pytest.mark.parametrize("size", [8, 9, 10, 11])
def test_the_size_is_honoured(size):
    assert _declarations(muted(size))["font-size"] == f"{size}px"


def test_mono_selects_the_monospace_family():
    """Used for anything column-aligned (node ids, counts, timings). Without it the
    columns wobble on Linux, where the generic family does resolve to a real face."""
    assert _declarations(muted(9, mono=True)) == {
        "color": "palette(mid)", "font-family": "monospace", "font-size": "9px"}


def test_bold_thickens_without_changing_the_dimming():
    assert _declarations(muted(9, bold=True)) == {
        "color": "palette(mid)", "font-weight": "700", "font-size": "9px"}


def test_the_plain_style_carries_neither_flag():
    """A flag that leaked into the default would thicken or re-face every caption."""
    plain = _declarations(muted())
    assert "font-family" not in plain
    assert "font-weight" not in plain


@pytest.mark.parametrize("css", [muted(), muted(11), muted(9, mono=True),
                                 muted(9, bold=True)])
def test_the_result_is_terminated_so_callers_can_append(css):
    """The panel's telemetry empty-state, `SettingRow`'s explanation box and the audit
    wizard's blurb each append their own declarations to this string. A missing final
    `;` would silently merge into the appended property and drop both."""
    assert css.endswith(";")
    appended = _declarations(css + " padding: 24px 8px;")
    assert appended["padding"] == "24px 8px"
    assert appended["color"] == "palette(mid)"
