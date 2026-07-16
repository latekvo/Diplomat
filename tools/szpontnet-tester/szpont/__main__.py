"""CLI entry point for the SzpontNet conformance tester.

    # prove the tester's own codec/oracle first (no candidate needed):
    python -m szpont --selftest

    # run the full suite against a candidate launcher command:
    python -m szpont --node-cmd "python adapters/reference.py"

    # a subset, verbosely (categories A–J; I/J = chapter-11 trust + server/API-key):
    python -m szpont --node-cmd "…" --only A,C,E,I,J --verbose

The ``--node-cmd`` is any command that starts ONE node configured from the
``SZPONTNET_*`` environment (the candidate contract — see the README). The
tester launches it many times, once per scenario, in an isolated loopback mesh.
"""

from __future__ import annotations

import argparse
import sys
import traceback

from .model import load_model
from .report import Reporter
from .suites import CATEGORY_TITLES, SUITES, Context


def _run_suites(rep: Reporter, ctx: Context, categories: list[str]) -> None:
    for cat in categories:
        cases = SUITES.get(cat, [])
        rep._line(f"\n{'─' * 64}\nCategory {cat} — {CATEGORY_TITLES.get(cat, '')}\n{'─' * 64}",
                  bold=True)
        for case in cases:
            try:
                case(rep, ctx)
            except Exception:  # a crashing case is a tester bug or a hard candidate failure
                rep.case_error(traceback.format_exc().strip().splitlines()[-1])
                if rep.verbose:
                    rep._line(traceback.format_exc())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m szpont", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--node-cmd", help="command that launches one candidate node "
                    "(configured via the SZPONTNET_* environment)")
    ap.add_argument("--only", help="comma-separated categories to run (A-J); default all")
    ap.add_argument("--selftest", action="store_true",
                    help="run the tester's own pure codec/oracle self-tests and exit")
    ap.add_argument("--list", action="store_true", help="list categories and cases, then exit")
    ap.add_argument("--verbose", action="store_true", help="show passing-check details + tracebacks")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI color")
    args = ap.parse_args(argv)

    rep = Reporter(verbose=args.verbose, color=False if args.no_color else None)
    model = load_model()

    if args.list:
        for cat, cases in SUITES.items():
            print(f"{cat}  {CATEGORY_TITLES.get(cat, '')}")
            for c in cases:
                print(f"     - {c.__name__}")
        return 0

    if args.selftest:
        from . import selftest
        selftest.run(rep)
        return rep.summary()

    if not args.node_cmd:
        ap.error("either --selftest or --node-cmd is required")

    rep._line("SzpontNet conformance tester", bold=True)
    rep._line(f"  model source : {model.source}")
    rep._line(f"  duties       : {', '.join(model.duty_ids)}")
    rep._line(f"  candidate cmd: {args.node_cmd}")

    categories = [c.strip().upper() for c in args.only.split(",")] if args.only else list(SUITES)
    unknown = [c for c in categories if c not in SUITES]
    if unknown:
        ap.error(f"unknown categories: {unknown}; valid: {list(SUITES)}")

    ctx = Context(node_cmd=args.node_cmd, model=model)
    # Always run the pure self-tests first — a broken oracle invalidates every verdict.
    from . import selftest
    rep._line("\n" + "─" * 64 + "\nTester self-tests (oracle sanity)\n" + "─" * 64, bold=True)
    selftest.run(rep)
    _run_suites(rep, ctx, categories)
    return rep.summary()


if __name__ == "__main__":
    sys.exit(main())
