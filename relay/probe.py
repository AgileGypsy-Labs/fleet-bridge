#!/usr/bin/env python3
"""Confirm the inbox socket's message format against a session you are watching.

The format is known (socketio.py says how it was determined); this re-checks it after a
Claude Code upgrade. It sends each envelope in socketio.ENVELOPES to a session whose
transcript you can see, pausing between each, and asks which one actually rendered.

    ./probe.py                 # probe the most recently registered session
    ./probe.py --session <id>
    ./probe.py --list

Whatever you confirm is written to ~/.fleet-bridge/config.json. Nothing else in this
repo sends a message until that file names an envelope.
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from socketio import ENVELOPES, CONFIG, deliver  # noqa: E402

REG = os.path.expanduser("~/.fleet-bridge/sessions")


def sessions():
    out = []
    for path in sorted(glob.glob(os.path.join(REG, "*.json"))):
        if os.path.basename(path) == "current.json":
            continue
        try:
            with open(path) as fh:
                out.append(json.load(fh))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda e: e.get("registered_at", 0), reverse=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--delay", type=float, default=6.0)
    args = ap.parse_args()

    found = sessions()
    if args.list:
        for e in found:
            print("%-40s %s  %s" % (e["session_id"], e.get("node"), e.get("cwd")))
        return 0
    if not found:
        print("no registered sessions. Is the SessionStart hook installed, and does")
        print("/status show a Peer address for the session you want to probe?")
        return 1

    target = found[0]
    if args.session:
        match = [e for e in found if e["session_id"].startswith(args.session)]
        if not match:
            print("no session matching %r" % args.session)
            return 1
        target = match[0]

    print("target session : %s" % target["session_id"])
    print("socket         : %s" % target["socket"])
    print("token          : %s" % ("present" if target.get("token") else "absent"))
    print()
    print("Watch that session's transcript. Each candidate below is sent %.0fs apart."
          % args.delay)
    print("Note which candidate NUMBER appears. Ctrl-C once you have it.")
    print()

    names = [n for n in ENVELOPES if n != "notice"]   # notice shares sdk_user's wire shape
    for i, name in enumerate(names, 1):
        text = "fleet-bridge probe candidate %d of %d (%s)" % (i, len(names), name)
        try:
            reply = deliver(
                target["socket"], target.get("token"), text, "fleet-probe", name
            )
        except Exception as exc:  # noqa: BLE001 - probe reports, never raises
            print("  %d %-16s SEND FAILED  %s" % (i, name, exc))
            continue
        print("  %d %-16s sent%s" % (i, name, ("  reply=%r" % reply) if reply else ""))
        time.sleep(args.delay)

    print()
    answer = input("which candidate number rendered in the session? (blank to abort) ")
    if not answer.strip().isdigit():
        print("nothing written.")
        return 1
    idx = int(answer.strip())
    if not 1 <= idx <= len(names):
        print("out of range; nothing written.")
        return 1

    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    try:
        with open(CONFIG) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        cfg = {}
    cfg["envelope"] = names[idx - 1]
    cfg["envelope_determined_at"] = time.time()
    with open(CONFIG, "w") as fh:
        json.dump(cfg, fh, indent=2)
    print("wrote envelope=%s to %s" % (names[idx - 1], CONFIG))
    return 0


if __name__ == "__main__":
    sys.exit(main())
