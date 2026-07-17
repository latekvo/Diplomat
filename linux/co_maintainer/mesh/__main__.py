"""Run or drive a mesh node from any machine (stdlib-only, no Qt needed).

    python -m co_maintainer.mesh                  # node in the foreground (Ctrl+C stops)
    python -m co_maintainer.mesh --daemon         # detach a background node
    python -m co_maintainer.mesh --status         # print the live topology
    python -m co_maintainer.mesh --stop           # stop the running node
    python -m co_maintainer.mesh --set tokens=out tier=2 name=mbp-old
    python -m co_maintainer.mesh --set plan=max-20x quotaLeft=12 usage=1  # accounting
    python -m co_maintainer.mesh --set tokens=ok --node <peer-id>   # edit a REMOTE node
    python -m co_maintainer.mesh --fingerprint                      # print this device's key fp
    python -m co_maintainer.mesh --trust <fp> --label mbp           # trust a device (personal)
    python -m co_maintainer.mesh --untrust <fp>                     # revoke trust
    python -m co_maintainer.mesh --dispatch audit --prompt "…"      # route a request
    python -m co_maintainer.mesh --dispatch review --prompt-file /tmp/p.txt
    python -m co_maintainer.mesh --dispatch review --prompt "…" --target <node-id>
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys


def _run_node() -> int:
    from .node import MeshNode

    async def main() -> None:
        node = MeshNode()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, node.request_stop)
        print(f"mesh node {node.local.name} ({node.local.id[:8]}) starting…",
              file=sys.stderr)
        await node.run()
        print("mesh node stopped", file=sys.stderr)

    asyncio.run(main())
    return 0


def _daemonize() -> int:
    from . import statefile

    if statefile.node_running():
        print("mesh node already running")
        return 0
    proc = subprocess.Popen(  # noqa: S603 — relaunching ourselves
        [sys.executable, "-m", "co_maintainer.mesh"],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    print(f"mesh node detached (pid {proc.pid})")
    return 0


def _print_status() -> int:
    from . import ctl, statefile

    try:
        state = ctl.status()
    except ctl.CtlError:
        state = statefile.read_state()
        if state is None:
            print("no mesh node has run here (no state.json)")
            return 1
        running = statefile.node_running(state)
        print(f"node not answering; last snapshot ({'live pid' if running else 'DEAD'}):")
    from . import config

    def _acct(info: dict) -> str:
        st = info.get("stats") or {}
        if not st:
            return ""
        surplus = round(float(st.get("quotaLeft", 0)) - float(st.get("usageAvg", 0)), 2)
        return f"  {st.get('plan', '?')}  surplus {surplus}"

    def _strength(info: dict) -> str:
        label = config.tier_label(int(info.get("tier", 3)))
        return f"{label}{'(auto)' if info.get('strengthAuto') else ''}"

    def _tokens(info: dict) -> str:
        auto = "auto" if info.get("tokensAuto") else "pinned"
        sess, week = info.get("tokensSessionPct"), info.get("tokensWeekPct")
        if isinstance(sess, (int, float)):
            left = f"5h {round(sess * 100)}%"
            if isinstance(week, (int, float)):
                left += f" wk {round(week * 100)}%"
        else:  # heuristic estimate (no real quota probe on that node)
            left = f"≈{round(float(info.get('tokensPct', 1.0)) * 100)}%"
        return f"{info.get('tokens')} {left} ({auto})"

    me = state.get("self", {})
    print(f"self  {me.get('name')}  {me.get('platform')}  {_strength(me)}"
          f"  tokens {_tokens(me)}{_acct(me)}"
          f"  :{state.get('tcpPort')}  id {me.get('id','')[:8]}"
          f"  fp {me.get('fingerprint','')[:16] or '(keyless)'}")
    for p in state.get("peers", []):
        vmark = "✓" if p.get("verified") else "?"
        print(f"peer  {p.get('name')}  {p.get('platform')}  {_strength(p)}"
              f"  tokens {_tokens(p)}{_acct(p)}  {p.get('trust', 'personal')}{vmark}"
              f"  link {p.get('link')}  {p.get('addr')}  fp {p.get('fingerprint','')[:16]}")
    trusted = state.get("trusted", [])
    if trusted:
        print("trusted  " + ", ".join(
            f"{e.get('fingerprint','')[:16]}{'(' + e['label'] + ')' if e.get('label') else ''}"
            for e in trusted))
    for duty, a in (state.get("assignments") or {}).items():
        names = []
        for nid in a.get("assigned", []):
            if nid == me.get("id"):
                names.append(me.get("name", "?"))
            else:
                names.append(next((p.get("name") for p in state.get("peers", [])
                                   if p.get("id") == nid), nid[:8]))
        short = ", ".join(names) if names else "∅ nobody"
        misses = "".join(f"  ⚠ missing {m['missing']}×{m['platform']}"
                         for m in a.get("shortfall", []))
        print(f"duty  {duty:<10} → {short}{misses}")
    return 0


def _parse_attrs(pairs: list[str]) -> dict:
    attrs: dict = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        if key == "tier":
            attrs["tier"] = value
        elif key == "tokens":
            attrs["tokens"] = value
        elif key == "name":
            attrs["name"] = value
        elif key in ("plan", "quotaLeft", "usageAvg", "usage"):  # accounting (stats.py)
            attrs[key] = value
        elif key.startswith("duty."):  # duty.audit=off disables a duty on the node
            attrs.setdefault("dutiesEnabled", {})[key[5:]] = value not in ("off", "0", "false")
        else:
            print(f"ignoring unknown attribute {key!r}", file=sys.stderr)
    return attrs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m co_maintainer.mesh",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--daemon", action="store_true", help="detach a background node")
    ap.add_argument("--status", action="store_true", help="print the live topology")
    ap.add_argument("--stop", action="store_true", help="stop the running node")
    ap.add_argument("--set", nargs="+", metavar="K=V", dest="set_attrs",
                    help="edit node attrs: tier=N tokens=auto|ok|low|out name=X "
                         "duty.<id>=on|off (tokens=auto tracks real usage; a tier "
                         "edit pins strength)")
    ap.add_argument("--node", default="self", metavar="ID",
                    help="target node id for --set (default: this machine)")
    ap.add_argument("--dispatch", metavar="DUTY", help="route a request through the mesh")
    ap.add_argument("--prompt", default="", help="inline prompt for --dispatch")
    ap.add_argument("--prompt-file", help="read the --dispatch prompt from a file")
    ap.add_argument("--target", metavar="ID", help="dispatch to one node directly "
                    "(the dispatcher's own pick, no failover)")
    ap.add_argument("--work-key", default="", metavar="KEY", dest="work_key",
                    help="origination-dedup key: claim it first, and stand down "
                    "with a 'suppressed' result if a peer already owns the work")
    ap.add_argument("--api-key", default="", metavar="KEY", dest="api_key",
                    help="API key to present to an API-key-gated (server) target")
    ap.add_argument("--fingerprint", action="store_true",
                    help="print this device's trust-key fingerprint and exit")
    ap.add_argument("--trust", metavar="FP",
                    help="add a device fingerprint to the local trusted allowlist")
    ap.add_argument("--untrust", metavar="FP",
                    help="remove a device fingerprint from the trusted allowlist")
    ap.add_argument("--label", default="", help="friendly label for --trust")
    args = ap.parse_args(argv)

    if args.daemon:
        return _daemonize()
    if args.fingerprint:
        from . import crypto
        key = crypto.load_or_create()
        print(key.fingerprint if key else "(keyless: cryptography not installed)")
        return 0
    if args.status:
        return _print_status()

    from . import ctl

    try:
        if args.stop:
            ctl.stop()
            print("stop requested")
            return 0
        if args.trust:
            ctl.trust_device(args.trust, args.label)
            print(f"trusting {args.trust[:16]}{' (' + args.label + ')' if args.label else ''}")
            return 0
        if args.untrust:
            ctl.untrust_device(args.untrust)
            print(f"untrusting {args.untrust[:16]}")
            return 0
        if args.set_attrs:
            attrs = _parse_attrs(args.set_attrs)
            if not attrs:
                return 2
            ctl.set_attr(args.node, attrs)
            print(f"applied to {args.node}: {json.dumps(attrs)}")
            return 0
        if args.dispatch:
            prompt = args.prompt
            if args.prompt_file:
                with open(args.prompt_file, "r", encoding="utf-8") as fh:
                    prompt = fh.read()
            if not prompt:
                print("--dispatch needs --prompt or --prompt-file", file=sys.stderr)
                return 2
            results = ctl.dispatch(args.dispatch, prompt, args.target, args.api_key,
                                   args.work_key)
            # `suppressed` is a success, not a failure: a peer already owns the
            # work, which is exactly what --work-key asks for.
            ok = all(r.get("status") in ("spawned", "suppressed") for r in results)
            marks = {"spawned": "✓", "suppressed": "◦"}
            for r in results:
                mark = marks.get(r.get("status"), "✗")
                print(f"{mark} [{r.get('slot')}] {r.get('nodeName') or '∅'}"
                      f" {r.get('status')}{': ' + r['reason'] if r.get('reason') else ''}")
            return 0 if ok else 1
    except ctl.CtlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    return _run_node()


if __name__ == "__main__":
    sys.exit(main())
