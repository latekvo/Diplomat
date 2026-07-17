"""Entry point: headless self-tests (no display needed) or the Qt6 tray applet.

    python -m co_maintainer                      # launch the tray applet
    CO_MAINTAINER_DUMP=1 python -m co_maintainer  # headless pipeline dump
    CO_MAINTAINER_LOOKUP=337 python -m co_maintainer
    CO_MAINTAINER_PRINT_PROMPT=mine python -m co_maintainer   # mine|user|single
    CO_MAINTAINER_SELF_UPDATE=1 python -m co_maintainer       # headless 6AM update
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    env = os.environ

    if env.get("CO_MAINTAINER_SELF_UPDATE") == "1":
        from .selfupdate import run_scheduled

        return run_scheduled()

    if env.get("CO_MAINTAINER_DUMP") == "1":
        from .selftest import run_dump

        return run_dump()

    lk = env.get("CO_MAINTAINER_LOOKUP")
    if lk:
        from .selftest import run_lookup

        try:
            n = int(lk)
        except ValueError:
            print(f"CO_MAINTAINER_LOOKUP must be an integer, got {lk!r}")
            return 2
        return run_lookup(n)

    mode = env.get("CO_MAINTAINER_PRINT_PROMPT")
    if mode:
        from .selftest import run_print_prompt

        return run_print_prompt(mode)

    what = env.get("CO_MAINTAINER_RENDER")
    if what:
        from .render import run as render_run

        out = env.get("CO_MAINTAINER_RENDER_OUT", f"/tmp/co-maintainer-{what}.png")
        return render_run(what, out)

    # No headless mode requested → launch the GUI.
    from .app import run_app

    return run_app()


if __name__ == "__main__":
    sys.exit(main())
