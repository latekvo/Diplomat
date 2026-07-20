"""Entry point: headless self-tests (no display needed) or the Qt6 tray applet.

    python -m diplomat_app                      # launch the tray applet
    DIPLOMAT_DUMP=1 python -m diplomat_app  # headless pipeline dump
    DIPLOMAT_LOOKUP=337 python -m diplomat_app
    DIPLOMAT_PRINT_PROMPT=mine python -m diplomat_app   # mine|user|single
    DIPLOMAT_SELF_UPDATE=1 python -m diplomat_app       # headless 6AM update
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    env = os.environ

    if env.get("DIPLOMAT_SELF_UPDATE") == "1":
        from .selfupdate import run_scheduled

        return run_scheduled()

    if env.get("DIPLOMAT_DUMP") == "1":
        from .selftest import run_dump

        return run_dump()

    lk = env.get("DIPLOMAT_LOOKUP")
    if lk:
        from .selftest import run_lookup

        try:
            n = int(lk)
        except ValueError:
            print(f"DIPLOMAT_LOOKUP must be an integer, got {lk!r}")
            return 2
        return run_lookup(n)

    mode = env.get("DIPLOMAT_PRINT_PROMPT")
    if mode:
        from .selftest import run_print_prompt

        return run_print_prompt(mode)

    what = env.get("DIPLOMAT_RENDER")
    if what:
        from .render import run as render_run

        out = env.get("DIPLOMAT_RENDER_OUT", f"/tmp/diplomat-{what}.png")
        return render_run(what, out)

    # No headless mode requested → launch the GUI. Migrate the pre-rename mesh
    # identity (~/.argent/mesh → ~/.diplomat/mesh) before the node comes up, so a
    # rename never regenerates this node's keypair and breaks fleet-wide trust.
    from .migrate import migrate_legacy_state_dir

    migrate_legacy_state_dir()

    from .app import run_app

    return run_app()


if __name__ == "__main__":
    sys.exit(main())
