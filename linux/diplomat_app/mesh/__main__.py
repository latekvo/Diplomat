"""Run or drive a mesh node from any machine (stdlib-only, no Qt needed).

    python -m diplomat_app.mesh                  # node in the foreground (Ctrl+C stops)
    python -m diplomat_app.mesh --daemon         # detach a background node
    python -m diplomat_app.mesh --status         # print the live topology
    python -m diplomat_app.mesh --stop           # stop the running node
    python -m diplomat_app.mesh --set tokens=out tier=2 name=mbp-old
    python -m diplomat_app.mesh --set plan=max-20x quotaLeft=12 usage=1  # accounting
    python -m diplomat_app.mesh --set tokens=ok --node <peer-id>   # edit a REMOTE node
    python -m diplomat_app.mesh --fingerprint                      # print this device's key fp
    python -m diplomat_app.mesh --trust <fp> --label mbp           # trust a device (personal)
    python -m diplomat_app.mesh --untrust <fp>                     # revoke trust
    python -m diplomat_app.mesh --ban <fp-or-node-id>              # ban a device (declines all)
    python -m diplomat_app.mesh --unban <fp-or-node-id>            # lift a ban
    python -m diplomat_app.mesh --dispatch audit --prompt "…"      # route a request
    python -m diplomat_app.mesh --dispatch review --prompt-file /tmp/p.txt
    python -m diplomat_app.mesh --dispatch review --prompt "…" --target <node-id>
    python -m diplomat_app.mesh --claim "review:github.com/o/r#1@sha"  # claim gate only
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
    from . import singlelock, singleton
    from .node import MeshNode

    # One node per state directory. The pre-launch node_running() checks (Swift
    # ensureRunning + _daemonize) are time-of-check/time-of-use races: several
    # launches inside the window before the first child writes state.json all read
    # "not running" and each spawn a node. Sharing one mesh_dir, they then share one
    # identity and clobber one state.json — only whichever a peer dials is truly
    # linked, the rest overwrite the snapshot with an empty `sees`. This flock is the
    # single point that can't be raced; keyed to the state dir, so the tests'
    # many-nodes-per-host affordance (a distinct DIPLOMAT_MESH_DIR each) is untouched.
    lock = singlelock.acquire()
    if lock is None:
        print("mesh node already running for this state directory — exiting",
              file=sys.stderr)
        return 0

    # Cross-state-dir reaper, complementing the flock above. The flock is keyed to
    # THIS node's state dir, so it cannot see a ghost running under a DIFFERENT dir
    # — exactly the pre-rename case (~/.argent/mesh vs ~/.diplomat/mesh) that let a
    # detached old-incarnation node survive and spawn duplicate fix terminals. So a
    # fresh node also reaps every OTHER live mesh node of this uid by /proc argv,
    # under any module name it has launched as. Ordered AFTER the flock so a losing
    # same-dir racer exits above without reaping anyone; stands down in loopback
    # (the multi-node test/dev fleet). See mesh/singleton.py.
    reaped = singleton.terminate_other_nodes()
    if reaped:
        print(f"mesh singleton: reaped {len(reaped)} stale node(s) {sorted(reaped)}",
              file=sys.stderr)

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

    try:
        asyncio.run(main())
    finally:
        singlelock.release(lock)
    return 0


def _daemonize() -> int:
    from . import statefile

    if statefile.node_running():
        print("mesh node already running")
        return 0
    proc = subprocess.Popen(  # noqa: S603 — relaunching ourselves
        [sys.executable, "-m", "diplomat_app.mesh"],
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
        # The advertised surplus is a burn-down ratio (budget left ÷ clock left);
        # 1.0 is on pace, higher is flush. Read the field the node computed rather
        # than the absolute quotaLeft−usageAvg, which is a different scale.
        try:
            surplus = round(float(st["surplus"]), 2)
        except (KeyError, TypeError, ValueError):
            surplus = 1.0  # legacy peer with no surplus field → neutral (on pace)
        return f"  {st.get('plan', '?')}  surplus {surplus}×"

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
              f"  tokens {_tokens(p)}{_acct(p)}  {p.get('trust', 'foreign')}{vmark}"
              f"  link {p.get('link')}  {p.get('addr')}  fp {p.get('fingerprint','')[:16]}")
    print(f"default trust for new devices: {state.get('defaultTrust', 'foreign')}")
    trusted = state.get("trusted", [])
    if trusted:
        print("trusted (personal)  " + ", ".join(
            f"{e.get('fingerprint','')[:16]}{'(' + e['label'] + ')' if e.get('label') else ''}"
            for e in trusted))
    for e in state.get("banned", []):
        who = e.get("fingerprint", "")[:16] or e.get("node", "")[:8]
        label = f"({e['label']})" if e.get("label") else ""
        print(f"banned  {who}{label}  — {e.get('reason', '')}")
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
    ap = argparse.ArgumentParser(prog="python -m diplomat_app.mesh",
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
    ap.add_argument("--claim", metavar="KEY", dest="claim_key",
                    help="run the origination claim gate for a work key without "
                         "dispatching (docs/szpontnet/12) — exit 0 when this node "
                         "should originate, 3 when a peer owns the work")
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
    ap.add_argument("--label", default="", help="friendly label for --trust / --ban")
    ap.add_argument("--ban", metavar="FP|ID",
                    help="ban a device (64-hex fingerprint, or a node id for a "
                         "keyless device) — declines all its requests and never "
                         "dispatches to it")
    ap.add_argument("--unban", metavar="FP|ID", help="lift a ban")
    ap.add_argument("--ban-reason", default="", dest="ban_reason",
                    help="reason recorded with --ban (default: manual)")
    ap.add_argument("--default-trust", metavar="LEVEL", dest="default_trust",
                    choices=("personal", "foreign"),
                    help="set the trust level for UNKNOWN devices (personal|foreign); "
                    "ships foreign (a new device is untrusted until you promote it)")
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
        if args.ban:
            # A fingerprint is 64 hex chars; anything else is taken as a node id
            # (the keyless-device fallback).
            fp = args.ban if len(args.ban) == 64 else ""
            ctl.ban_device(fp, "" if fp else args.ban, label=args.label,
                           reason=args.ban_reason)
            print(f"banned {args.ban[:16]}")
            return 0
        if args.unban:
            fp = args.unban if len(args.unban) == 64 else ""
            ctl.unban_device(fp, "" if fp else args.unban)
            print(f"unbanned {args.unban[:16]}")
            return 0
        if args.default_trust:
            ctl.set_default_trust(args.default_trust)
            print(f"default trust for new devices → {args.default_trust}")
            return 0
        if args.set_attrs:
            attrs = _parse_attrs(args.set_attrs)
            if not attrs:
                return 2
            ctl.set_attr(args.node, attrs)
            print(f"applied to {args.node}: {json.dumps(attrs)}")
            return 0
        if args.claim_key:
            res = ctl.claim_work(args.claim_key)
            if res["owned"]:
                print(f"✓ owned — originate {args.claim_key} here")
                return 0
            owner = res.get("ownerName") or res.get("owner") or "?"
            print(f"◦ suppressed — {owner} owns {args.claim_key}")
            return 3
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
