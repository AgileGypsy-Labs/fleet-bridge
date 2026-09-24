#!/usr/bin/env python3
"""Send a message to a Claude Code session on a peer node.

    ./send.py laptop-b "schema migration landed, rebase is safe"
    ./send.py laptop-a --session 4f2a "hold off, I'm mid-refactor in src/audit"
    ./send.py --peers
    ./send.py --local --cwd ~/code/api "new rule: agent work goes through fleet-agent"

Peers live in ~/.fleet-bridge/peers.json, written by install.sh. Each is a loopback port
that fleet-relay-tunnel.service forwards to that peer's relay through the hub:
    on laptop-a:  {"laptop-b": "http://127.0.0.1:8802"}
    on laptop-b:  {"laptop-a": "http://127.0.0.1:8801"}

Without --session a message goes to the peer's most recently started live session.
--local sends a one-way notice to live sessions on THIS node instead (all of them, or those
whose working directory is --cwd or below it); no relay, secret or peer involved.
"""
import argparse
import glob
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

HOME = os.path.expanduser("~/.fleet-bridge")
PEERS = os.path.join(HOME, "peers.json")
SECRET_FILE = os.path.join(HOME, "secret")
CONFIG = os.path.join(HOME, "config.json")
REG = os.path.join(HOME, "sessions")


def node_name():
    """This node's fleet name from config.json, not its hostname: a hostname often says
    nothing about which machine or account a message came from."""
    if os.environ.get("FLEET_NODE"):
        return os.environ["FLEET_NODE"]
    try:
        with open(CONFIG) as fh:
            return json.load(fh).get("node") or os.uname().nodename
    except (OSError, ValueError):
        return os.uname().nodename


def local_notice(text, cwd, sender):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from socketio import deliver
    root = os.path.realpath(os.path.expanduser(cwd)) if cwd else None
    newest = {}                                   # one delivery per live socket
    for path in glob.glob(os.path.join(REG, "*.json")):
        if os.path.basename(path) == "current.json":
            continue
        try:
            e = json.load(open(path))
        except (OSError, ValueError):
            continue
        sock = e.get("socket", "").removeprefix("uds:")
        where = os.path.realpath(e.get("cwd", ""))
        if not sock or not os.path.exists(sock):
            continue
        if root and not (where == root or where.startswith(root + "/")):
            continue
        if sock not in newest or e.get("registered_at", 0) > newest[sock].get("registered_at", 0):
            newest[sock] = e
    if not newest:
        print("no live sessions%s" % (" under " + root if root else ""), file=sys.stderr)
        return 1
    failed = 0
    for sock, e in newest.items():
        try:
            deliver(sock, e.get("token"), text, sender, "notice")
            print("sent  %s  %s" % (e["session_id"][:8], e.get("cwd")))
        except OSError as exc:
            failed += 1
            print("FAIL  %s  %s" % (e["session_id"][:8], exc), file=sys.stderr)
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("peer", nargs="?")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--session", help="target a specific session id prefix")
    ap.add_argument("--from", dest="sender", default=node_name())
    ap.add_argument("--peers", action="store_true", help="list configured peers and their health")
    ap.add_argument("--local", action="store_true", help="notify live sessions on this node")
    ap.add_argument("--cwd", help="with --local: only sessions working in this directory or below")
    args = ap.parse_args()

    if args.local:
        text = args.text or args.peer                 # `send.py --local "text"`
        if not text:
            ap.error("--local needs the text")
        return local_notice(text, args.cwd, args.sender)

    try:
        with open(PEERS) as fh:
            peers = json.load(fh)
    except (OSError, ValueError):
        sys.exit("no peers configured at %s" % PEERS)

    if args.peers:
        for name, url in peers.items():
            try:
                with urllib.request.urlopen(url + "/v1/health", timeout=5) as r:
                    info = json.load(r)
                print("%-12s %-46s up    envelope=%s sessions=%d"
                      % (name, url, info.get("envelope"), len(info.get("sessions", []))))
            except Exception as exc:  # noqa: BLE001
                print("%-12s %-46s DOWN  %s" % (name, url, exc))
        return 0

    if not args.peer or not args.text:
        ap.error("peer and text are required (or use --peers)")
    if args.peer not in peers:
        sys.exit("unknown peer %r; known: %s" % (args.peer, ", ".join(peers)))

    with open(SECRET_FILE, "rb") as fh:
        secret = fh.read().strip()

    payload = {"text": args.text, "from": args.sender}
    if args.session:
        payload["session"] = args.session
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()

    req = urllib.request.Request(
        peers[args.peer] + "/v1/message",
        data=body,
        headers={"Content-Type": "application/json", "X-Fleet-Ts": ts, "X-Fleet-Sig": sig},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(json.dumps(json.load(r), indent=2))
    except urllib.error.HTTPError as exc:
        print("HTTP %d: %s" % (exc.code, exc.read().decode("utf-8", "replace")), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print("send failed: %s" % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
