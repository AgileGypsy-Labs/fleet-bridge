#!/usr/bin/env python3
"""Relay agent: accepts a message from a peer node and delivers it into a local
Claude Code session's inbox socket.

Binds 127.0.0.1 only. Peers reach it through fleet-relay-tunnel.service: each node
reverse-forwards its relay onto its own loopback port on the hub and forwards the peers'
ports back, so nothing new listens on any LAN or tailnet address. Auth is HMAC-SHA256
over timestamp + body with a shared secret, so a message that reaches the port without
the secret is dropped before it touches a socket. A timestamp outside +-SKEW seconds, or a
signature already accepted within that window (a replay), is refused the same way.

    POST /v1/message   {"text": "...", "from": "laptop-a", "session": "<optional id prefix>"}
    GET  /v1/health    relay up, envelope, live session ids
    GET  /v1/status    node status for a dashboard: sessions by repo, services, power,
                       load, delegated work, account usage (nodestatus.py). Counts and
                       states only, never message text or tokens.
"""
import glob
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from socketio import deliver, load_config  # noqa: E402
import nodestatus  # noqa: E402

HOME = os.path.expanduser("~/.fleet-bridge")
REG = os.path.join(HOME, "sessions")
SECRET_FILE = os.path.join(HOME, "secret")
SKEW = 120  # seconds
PORT = int(os.environ.get("FLEET_RELAY_PORT", "8787"))
SEEN = {}                 # signature -> timestamp, for replay refusal within SKEW
SEEN_LOCK = threading.Lock()
SAMPLER = None            # nodestatus.Sampler, started in main() when accounts are configured


def first_use(sig, ts):
    """True the first time a signature is presented; False for a replay."""
    now = time.time()
    with SEEN_LOCK:
        for old in [s for s, t in SEEN.items() if t < now - 2 * SKEW]:
            del SEEN[old]
        if sig in SEEN:
            return False
        SEEN[sig] = ts
        return True


def secret():
    with open(SECRET_FILE, "rb") as fh:
        return fh.read().strip()


def live_sessions():
    out = []
    for path in sorted(glob.glob(os.path.join(REG, "*.json"))):
        if os.path.basename(path) == "current.json":
            continue
        try:
            with open(path) as fh:
                entry = json.load(fh)
        except (OSError, ValueError):
            continue
        sock = entry.get("socket", "")
        if sock.startswith("uds:"):
            sock = sock[4:]
        if not sock or not os.path.exists(sock):
            continue  # session gone; its socket went with it
        out.append(entry)
    return sorted(out, key=lambda e: e.get("registered_at", 0), reverse=True)


class Handler(BaseHTTPRequestHandler):
    server_version = "fleet-relay/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/v1/status":
            return self._json(200, nodestatus.status(load_config(), live_sessions(), SAMPLER))
        if self.path != "/v1/health":
            return self._json(404, {"error": "not found"})
        cfg = load_config()
        self._json(200, {
            "ok": True,
            "envelope": cfg.get("envelope"),
            "sessions": [e["session_id"] for e in live_sessions()],
        })

    def do_POST(self):
        if self.path != "/v1/message":
            return self._json(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length") or 0)
        if length > 1_000_000:
            return self._json(413, {"error": "too large"})
        body = self.rfile.read(length)

        ts = self.headers.get("X-Fleet-Ts", "")
        sig = self.headers.get("X-Fleet-Sig", "")
        if not ts.isdigit() or abs(time.time() - int(ts)) > SKEW:
            return self._json(401, {"error": "stale or missing timestamp"})
        expect = hmac.new(secret(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return self._json(401, {"error": "bad signature"})
        if not first_use(sig, int(ts)):
            return self._json(401, {"error": "replayed message"})

        try:
            msg = json.loads(body)
        except ValueError:
            return self._json(400, {"error": "body is not JSON"})
        text = (msg.get("text") or "").strip()
        if not text:
            return self._json(400, {"error": "text is required"})

        cfg = load_config()
        envelope = cfg.get("envelope")
        if not envelope:
            return self._json(
                503,
                {"error": "no envelope in ~/.fleet-bridge/config.json; re-run relay/install.sh"},
            )

        found = live_sessions()
        if msg.get("session"):
            found = [e for e in found if e["session_id"].startswith(msg["session"])]
        if not found:
            return self._json(404, {"error": "no live registered session"})
        target = found[0]

        try:
            reply = deliver(
                target["socket"], target.get("token"), text,
                msg.get("from") or "peer", envelope,
            )
        except (OSError, socket.timeout) as exc:
            return self._json(502, {"error": "socket delivery failed: %s" % exc})

        self._json(200, {
            "delivered_to": target["session_id"],
            "node": target.get("node"),
            "socket_reply": reply or None,
        })


def main():
    if not os.path.exists(SECRET_FILE):
        sys.exit("missing %s — run relay/install.sh first" % SECRET_FILE)
    addr = os.environ.get("FLEET_RELAY_BIND") or "127.0.0.1"
    if addr == "0.0.0.0":
        sys.exit("refusing to bind 0.0.0.0: peers arrive through the hub tunnel, not the network")
    global SAMPLER
    accounts = load_config().get("accounts")
    if accounts:
        SAMPLER = nodestatus.Sampler(accounts)
        SAMPLER.start()
    srv = ThreadingHTTPServer((addr, PORT), Handler)
    sys.stderr.write("fleet-relay listening on %s:%d\n" % (addr, PORT))
    srv.serve_forever()


if __name__ == "__main__":
    main()
