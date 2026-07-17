"""Monochrome UI glyph set for the Linux applet.

macOS renders its icons as monochrome SF Symbols tinted by the tool colour; on
Linux colour-emoji render garish and clash with the flat UI. Every glyph here is
a plain text symbol that inherits its (tint) colour from the pen, matching the
macOS look. All are verified to render as monochrome text - never colour-emoji -
so they tint cleanly (see widgets._draw_glyph for the ink-centred renderer).

Tool and activity-category glyphs live in core/catalog.json / core/audit-
categories.json (the shared, cross-platform assets) under the additive
``linuxGlyph`` field; the constants below are the chrome, platform and per-action
glyphs that have no home in those files.
"""

from __future__ import annotations

# --- Colour tokens (only what tinting strictly needs) ----------------------
MUTED = "#9AA0A6"  # grey glyph for an inactive/"off" chip
# Opaque neutral fill for an inactive/"off" icon chip (free devices, lookup misses).
CHIP_OFF = "#3A3D42"

# --- Chrome glyphs (header, search, section headers) -----------------------
G_APP = "⚒"      # wrench header / tray icon
G_SEARCH = "⌕"   # reverse-lookup search
G_DEVICES = "⧉"  # devices section
G_ACTIVITY = "▤" # activity feed section
G_BAN = "⊘"      # banned author (no-entry)
G_MESH = "⬡"     # mesh screen header / "run on mesh" toggle (matches the mesh
                 # activity category's linuxGlyph)

# --- Action-card glyphs (grid actions) -------------------------------------
G_REVIEW = "☑"   # Review-PRs action
G_CONFLICT = "⋔" # Resolve-conflicts action
G_AUDIT = "◉"    # Full-E2E action
G_FINAL = "✦"    # final-pass escalation

# --- Device-platform glyphs ------------------------------------------------
G_PHONE = "▯"    # handset device
G_TV = "▭"       # tv / display device
G_ROBOT = "◈"    # android device
G_FLAME = "◉"    # vega device
G_APPLE = "●"    # ios device

PLATFORM_GLYPH = {
    "ios": G_APPLE,
    "apple-tv": G_TV,
    "android": G_ROBOT,
    "android-tv": G_TV,
    "vega": G_FLAME,
}
