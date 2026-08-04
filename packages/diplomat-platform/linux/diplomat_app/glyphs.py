"""Monochrome UI glyph set for the Linux applet.

macOS renders its icons as monochrome SF Symbols tinted by the tool colour; on
Linux colour-emoji render garish and clash with the flat UI. Every glyph here is
a plain text symbol that inherits its (tint) colour from the pen, matching the
macOS look. All are verified to render as monochrome text - never colour-emoji -
so they tint cleanly (see widgets._draw_glyph for the ink-centred renderer).

Tool and activity-category glyphs live in assets/catalog.json /
assets/audit-categories.json (the shared, cross-platform files) under the additive
``linuxGlyph`` field; the constants below are the chrome, platform and per-action
glyphs that have no home in those files.
"""

from __future__ import annotations

# --- Colour tokens (only what tinting strictly needs) ----------------------
MUTED = "#9AA0A6"  # grey glyph for an inactive/"off" chip
# Opaque neutral fill for an inactive/"off" icon chip (free devices, lookup misses).
CHIP_OFF = "#3A3D42"
# A task on its way to running, in the blue macOS gives a running agent's status
# (AgentTaskStatus): the click's answer is the row leaving the grey of the queue.
STARTING = "#0A84FF"

# --- Chrome glyphs (header, search, section headers) -----------------------
G_APP = "⚒"      # wrench header / tray icon
G_SEARCH = "⌕"   # reverse-lookup search
G_DEVICES = "⧉"  # devices section
G_ACTIVITY = "▤" # activity feed section
G_BAN = "⊘"      # banned author (no-entry)
G_TASKS = "◷"    # agent-tasks section header, and a queued task awaiting its turn
G_STARTING = "◍"  # a task whose spawn is under way (the bay filling in)
G_FREE_SLOT = "◌"  # an empty bay: a slot of the task cap with nothing in it
G_GRIP = "≡"     # the drag grip that orders the queue
G_MESH = "⬡"     # mesh screen header / "run on mesh" toggle (matches the mesh
                 # activity category's linuxGlyph)
G_TELEMETRY = "◫" # telemetry screen header (the per-figure glyphs live in
                  # assets/telemetry.json, like the tool and category ones)

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
