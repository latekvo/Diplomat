"""Entry point: headless self-tests (no display needed) or the Qt6 tray applet.

    python -m argent_utils                      # launch the tray applet
    ARGENT_UTILS_DUMP=1 python -m argent_utils  # headless pipeline dump
    ARGENT_UTILS_LOOKUP=337 python -m argent_utils
    ARGENT_UTILS_PRINT_PROMPT=mine python -m argent_utils   # mine|user|single
    ARGENT_UTILS_SELF_UPDATE=1 python -m argent_utils       # headless 6AM update
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    env = os.environ

    if env.get("ARGENT_UTILS_SELF_UPDATE") == "1":
        from .selfupdate import run_scheduled

        return run_scheduled()

    if env.get("ARGENT_UTILS_DUMP") == "1":
        from .selftest import run_dump

        return run_dump()

    lk = env.get("ARGENT_UTILS_LOOKUP")
    if lk:
        from .selftest import run_lookup

        try:
            n = int(lk)
        except ValueError:
            print(f"ARGENT_UTILS_LOOKUP must be an integer, got {lk!r}")
            return 2
        return run_lookup(n)

    mode = env.get("ARGENT_UTILS_PRINT_PROMPT")
    if mode:
        from .selftest import run_print_prompt

        return run_print_prompt(mode)

    what = env.get("ARGENT_UTILS_RENDER")
    if what:
        from .render import run as render_run

        out = env.get("ARGENT_UTILS_RENDER_OUT", f"/tmp/argent-utils-{what}.png")
        return render_run(what, out)

    # No headless mode requested → launch the GUI.
    from .app import run_app

    return run_app()


if __name__ == "__main__":
    sys.exit(main())
