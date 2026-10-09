#!/usr/bin/env python3
"""quota-proxy: a loopback proxy per node that lets a running Claude Code session move between
your own accounts without a restart.

Claude Code sends every API request to ANTHROPIC_BASE_URL. Pointed at this proxy, each
inference request (/v1/messages and anything under it) goes upstream with the token of
whichever of your accounts has the most headroom for the request's model; everything else
(profile, usage, Remote Control) passes through on the session's own login, untouched.
Headroom comes from the Caravan hub's ledger, refreshed every HUB_EVERY seconds, overlaid with
the rate-limit headers this proxy sees itself on every answer (fresher, but without the
per-model windows).

Rules:
- A session sticks to one account until that account's worst applicable window reaches
  `switch_at` (90%). It then moves to the best other account that is under `switch_at` minus
  `hysteresis`, and moves again only by the same rule.
- A 429 whose unified status is `rejected` (a used-up window, not a burst) marks that window in
  force until its reset, and the request is retried once on the best other account. Any other
  429 or error is returned as it is.
- With no account under the line, or with the proxy off, the request passes through on the
  credential the session sent. Nothing here can refuse a request the session could have made.

Configuration (~/.fleet-bridge/config.json):
    "proxy": {"listen": "127.0.0.1:8899", "upstream": "https://api.anthropic.com",
              "switch_at": 90, "hysteresis": 5, "device_key": "~/.fleet-bridge/proxy.key"},
    "route": {...},                        the hub, as for route.py (optional: without it only
                                           the headers this proxy sees drive the choice)
    "delegate": {"accounts": [{"label": "...", "token_file": "~/.fleet-bridge/accounts/x.env", ...}]}
Each token_file holds CLAUDE_CODE_OAUTH_TOKEN=<setup-token of that account>, mode 0600. An
account without token_file is never swapped in. Clients send `X-Fleet-Device: <the contents of
device_key>` (route.proxy_env sets it through ANTHROPIC_CUSTOM_HEADERS); a request without it
is passed through unchanged. ~/.fleet-bridge/proxy.off, while present, makes the proxy a plain
pass-through without a restart (`quota-proxy --off` / `--on` / `--status`).

Audit: one JSON line per request in ~/.fleet-bridge/proxy.log, labels and numbers only, never
a token. Telemetry (the headers seen) goes to the hub's POST /v1/proxy-events.
"""
import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route  # noqa: E402  (installed beside this script)

FB = os.environ.get("FLEET_HOME") or os.path.expanduser("~/.fleet-bridge")
CONFIG = os.environ.get("FLEET_CONFIG") or os.path.join(FB, "config.json")
OFF_FLAG = os.path.join(FB, "proxy.off")
LOG = os.path.join(FB, "proxy.log")
HUB_EVERY = 10            # seconds between ledger refreshes and telemetry posts
STALE = 900               # a reading older than this says nothing
RESET_SOON = 900          # a window resetting this soon counts as empty (as in Caravan's policy)
UPSTREAM_TIMEOUT = 600            # a long generation
MAX_BODY = 32 * 1024 * 1024   # larger requests are refused with 413, never buffered or forwarded
MAX_HEADERS = 64              # stdlib caps at 100/65536-per-line; this tightens the count
INFERENCE = re.compile(r"^/v1/messages(?:/|\?|$)")
MODEL_RE = re.compile(rb'"model"\s*:\s*"([^"]{1,120})"')
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
       "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
WINDOW_NAMES = {"5h": "session", "7d": "weekly_all"}     # header window -> usage API limit name
NAME_WINDOWS = {v: k for k, v in WINDOW_NAMES.items()}


def load_config():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _epoch(value):
    """Epoch seconds from a header (epoch) or a usage API resets_at (ISO 8601), else None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    s = str(value).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        import datetime
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _iso(epoch):
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _fmt(pct):
    return "?" if pct is None else "%d" % pct


def audit(rec):
    try:
        with open(LOG, "a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def model_of(body):
    m = MODEL_RE.search(body[:8192])
    if m:
        return m.group(1).decode("utf-8", "replace")
    try:
        m = json.loads(body).get("model")
    except (ValueError, AttributeError):
        return None
    return m if isinstance(m, str) else None


def quota_rejected(status, h):
    """The window a quota 429 names, or None for any other answer (a burst 429 included)."""
    if status != 429 or h.get("anthropic-ratelimit-unified-status") != "rejected":
        return None
    for win in ("5h", "7d"):
        if h.get("anthropic-ratelimit-unified-%s-status" % win) == "rejected":
            return win
    claim = (h.get("anthropic-ratelimit-unified-representative-claim") or "").lower()
    if "seven" in claim or "7d" in claim or "week" in claim:
        return "7d"
    return "5h"


class Accounts:
    """The accounts with a setup-token file; tokens read on demand, re-read when a file changes."""

    def __init__(self, cfg):
        self.entries = [a for a in (cfg.get("delegate") or {}).get("accounts") or []
                        if isinstance(a, dict) and a.get("label") and a.get("token_file")]
        self.labels = [a["label"] for a in self.entries]
        self._cache = {}
        self.bad = {}        # label -> until epoch: a token the API refused (401) is left alone
        self.lock = threading.Lock()

    def refuse(self, label, now, for_seconds=600):
        with self.lock:
            self.bad[label] = now + for_seconds

    def token(self, label):
        with self.lock:
            if self.bad.get(label, 0) > time.time():
                return None
        for a in self.entries:
            if a["label"] != label:
                continue
            path = os.path.expanduser(a["token_file"])
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                return None
            with self.lock:
                c = self._cache.get(path)
                if c and c[0] == mtime:
                    return c[1]
            try:
                tok = route.token(a["token_file"])
            except route.RouteError:
                tok = None
            with self.lock:
                self._cache[path] = (mtime, tok)
            return tok
        return None


class Ledger:
    """What the proxy knows about each account's windows: the hub's samples (which carry the
    per-model windows) and the headers it has seen itself (fresher). Thread-safe."""

    def __init__(self, labels):
        self.labels = list(labels)
        self.hub = {}        # label -> {"as_of": epoch, "limits": [...]}
        self.seen = {}       # label -> {window: {"pct", "reset", "status", "ts"}}
        self.in_force = {}   # (label, window) -> until epoch
        self.sticky = {}     # session key -> label
        self.events = []     # telemetry waiting for the hub
        self.lock = threading.Lock()

    def note_headers(self, label, h, now, status):
        """Record the unified rate-limit headers of an answer on `label`."""
        with self.lock:
            for win in ("5h", "7d"):
                u = h.get("anthropic-ratelimit-unified-%s-utilization" % win)
                if u is None:
                    continue
                try:
                    pct = float(u) * 100
                except ValueError:
                    continue
                reset = _epoch(h.get("anthropic-ratelimit-unified-%s-reset" % win))
                st = h.get("anthropic-ratelimit-unified-%s-status" % win)
                self.seen.setdefault(label, {})[win] = {"pct": pct, "reset": reset, "status": st, "ts": now}
                self.events.append({"ts": round(now, 3), "account": label, "window": win,
                                    "utilization": round(pct / 100, 4), "reset_at": _iso(reset),
                                    "status": st, "http_status": status})
                if st == "rejected":
                    self.in_force[(label, win)] = reset or now + 300

    def mark_in_force(self, label, win, until):
        with self.lock:
            self.in_force[(label, win)] = until

    def worst(self, label, model, now):
        """The highest applicable percent for `label`, or None when nothing is known."""
        pcts = []
        with self.lock:
            rec = self.hub.get(label)
            seen = self.seen.get(label, {})
            if rec and now - rec.get("as_of", 0) <= STALE:
                for l in rec.get("limits") or []:
                    scoped = l.get("model")
                    if scoped and not (model and scoped.lower() in model.lower()):
                        continue
                    pct, resets = l.get("percent"), _epoch(l.get("resets_at"))
                    s = seen.get(NAME_WINDOWS.get(l.get("name") or ""))
                    if s and s["ts"] > rec["as_of"] and now - s["ts"] <= STALE:
                        pct, resets = s["pct"], s["reset"] or resets
                    if l.get("active"):
                        pcts.append(100)
                        continue
                    if resets is not None and 0 <= resets - now <= RESET_SOON:
                        pct = 0
                    if pct is not None:
                        pcts.append(pct)
            else:
                for s in seen.values():
                    if now - s["ts"] > STALE:
                        continue
                    soon = s["reset"] is not None and 0 <= s["reset"] - now <= RESET_SOON
                    pcts.append(0 if soon else s["pct"])
            for (lab, win), until in self.in_force.items():
                if lab == label and until > now:
                    pcts.append(100)
        return max(pcts) if pcts else None

    def choose(self, key, own, model, now, switch_at, hysteresis, avoid=None, labels=None):
        """(label, why, switched). Sticky per session: the account it is on stays until its worst
        applicable window reaches switch_at; then the best other account under switch_at minus
        hysteresis. `avoid` forces a move away from an account that just refused; `labels`
        narrows the choice to the accounts with a usable token right now."""
        labels = list(labels) if labels else self.labels
        with self.lock:
            cur = self.sticky.get(key)
        if cur not in labels:
            cur = own if own in labels else labels[0]
        w = {l: self.worst(l, model, now) for l in labels}

        def rank(label):
            return 50 if w[label] is None else w[label]

        if avoid == cur or (w[cur] is not None and w[cur] >= switch_at):
            others = [l for l in labels if l != cur and l != avoid and rank(l) <= switch_at - hysteresis]
            if others:
                best = min(others, key=rank)
                with self.lock:
                    self.sticky[key] = best
                return best, "%s %s%% -> %s %s%%" % (cur, _fmt(w[cur]), best, _fmt(w[best])), True
            return cur, "every account over the line, staying on %s" % cur, False
        with self.lock:
            self.sticky[key] = cur
        return cur, "%s %s%%" % (cur, _fmt(w[cur])), False

    def take_events(self, limit=500):
        with self.lock:
            out, self.events = self.events[:limit], self.events[limit:]
        return out


class Upstream:
    """One keep-alive connection per thread to the API, reconnecting once when it has died."""

    def __init__(self, url):
        p = urllib.parse.urlsplit(url)
        self.https = p.scheme == "https"
        self.host, self.port = p.hostname, p.port
        self.ctx = ssl.create_default_context() if self.https else None
        self.local = threading.local()

    def conn(self, fresh=False):
        c = getattr(self.local, "conn", None)
        if c is None or fresh:
            if c is not None:
                c.close()
            if self.https:
                c = http.client.HTTPSConnection(self.host, self.port, timeout=UPSTREAM_TIMEOUT, context=self.ctx)
            else:
                c = http.client.HTTPConnection(self.host, self.port, timeout=UPSTREAM_TIMEOUT)
            self.local.conn = c
        return c

    def request(self, method, path, headers, body):
        for attempt in (0, 1):
            c = self.conn(fresh=attempt == 1)
            try:
                c.request(method, path, body=body, headers=headers)
                return c.getresponse()
            except (http.client.HTTPException, OSError):
                if attempt == 1:
                    raise

    def drop(self):
        c = getattr(self.local, "conn", None)
        if c is not None:
            c.close()
            self.local.conn = None


class State:
    def __init__(self, cfg):
        p = cfg.get("proxy") or {}
        self.cfg = cfg
        self.listen = p.get("listen") or "127.0.0.1:8899"
        self.upstream = Upstream(p.get("upstream") or "https://api.anthropic.com")
        self.switch_at = float(p.get("switch_at", 90))
        self.hysteresis = float(p.get("hysteresis", 5))
        self.accounts = Accounts(cfg)
        self.ledger = Ledger(self.accounts.labels)
        self.device_key = read_key(p.get("device_key") or os.path.join(FB, "proxy.key"))
        self.node = cfg.get("node") or os.uname().nodename

    def enabled(self):
        return not os.path.exists(OFF_FLAG)


def read_key(path):
    try:
        with open(os.path.expanduser(path)) as fh:
            return fh.read().strip() or None
    except OSError:
        return None


class HubSync(threading.Thread):
    """Every HUB_EVERY seconds: the hub's view of each account (per-model windows included),
    and this node's telemetry to the hub. A hub that is away costs nothing but a log line."""

    def __init__(self, state):
        super().__init__(daemon=True, name="hub-sync")
        self.state = state
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                self.once()
            except Exception as exc:  # noqa: BLE001 - the hub being away is data, not a crash
                audit({"t": round(time.time()), "hub": "error", "why": str(exc)[:120]})
            self.stop.wait(HUB_EVERY)

    def once(self):
        st = self.state
        got = route.signed_request("GET", "/v1/accounts", None, st.cfg)
        with st.ledger.lock:
            for a in got.get("accounts") or []:
                u = a.get("usage") or {}
                if a.get("label") in st.ledger.labels and u.get("state") == "ok" and u.get("as_of"):
                    st.ledger.hub[a["label"]] = {"as_of": u["as_of"], "limits": u.get("limits") or []}
        events = st.ledger.take_events()
        if events:
            route.signed_request("POST", "/v1/proxy-events", {"node": st.node, "events": events}, st.cfg)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "quota-proxy/0.1"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.relay("GET")

    def do_POST(self):
        self.relay("POST")

    def do_PUT(self):
        self.relay("PUT")

    def do_PATCH(self):
        self.relay("PATCH")

    def do_DELETE(self):
        self.relay("DELETE")

    def _body(self):
        """The request body, or None when it exceeds MAX_BODY (the caller answers 413 and
        closes: an oversized request is never buffered, swapped or sent upstream)."""
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out, total = [], 0
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass
                    break
                total += size
                if total > MAX_BODY:
                    return None
                out.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(out)
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            return None
        return self.rfile.read(n) if n else b""

    def _reply(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def relay(self, method):
        st = self.server.state
        t0 = time.time()
        if len(self.headers) > MAX_HEADERS:
            self.close_connection = True          # the body is unread; this connection is out of sync
            return self._reply(431, {"error": "too many headers"})
        try:
            body = self._body()
        except (ValueError, OSError):
            return self._reply(400, {"error": "bad request body"})
        if body is None:
            self.close_connection = True          # oversize: refused before buffering or forwarding
            return self._reply(413, {"error": "body exceeds %d bytes" % MAX_BODY})
        own = self.headers.get("X-Fleet-Account") or ""
        device_ok = bool(st.device_key) and hmac.compare_digest(self.headers.get("X-Fleet-Device", ""), st.device_key)
        usable = [l for l in st.accounts.labels if st.accounts.token(l)]
        swap = bool(st.enabled() and device_ok and method == "POST" and INFERENCE.match(self.path)
                    and usable)
        model = model_of(body) if swap else None
        key = hashlib.sha256((self.headers.get("Authorization") or "").encode()).hexdigest()[:16]
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP and not k.lower().startswith("x-fleet-")}
        chosen, why, switched, retried = None, "pass-through", False, False
        if swap:
            chosen, why, switched = st.ledger.choose(key, own, model, t0, st.switch_at, st.hysteresis,
                                                     labels=usable)
        resp, used = None, None
        try:
            for attempt in (1, 2):
                h = dict(headers)
                tok = st.accounts.token(chosen) if chosen else None
                if chosen and not tok:
                    why, chosen = "no usable token for %s, pass-through" % chosen, None
                if tok:
                    h["Authorization"] = "Bearer " + tok
                resp = st.upstream.request(method, self.path, h, body)
                rh = {k.lower(): v for k, v in resp.getheaders()}
                used = chosen
                if chosen and resp.status == 401 and attempt == 1:
                    # The swapped-in token was refused (revoked, expired): never let that reach the
                    # session. Leave that token alone for a while and send the request as it came.
                    st.accounts.refuse(chosen, time.time())
                    resp.read()
                    why = "%s; %s token refused (401), pass-through" % (why, chosen)
                    chosen, retried = None, True
                    continue
                if chosen:
                    st.ledger.note_headers(chosen, rh, time.time(), resp.status)
                win = quota_rejected(resp.status, rh)
                if win and chosen and attempt == 1:
                    reset = _epoch(rh.get("anthropic-ratelimit-unified-%s-reset" % win))
                    st.ledger.mark_in_force(chosen, win, reset or time.time() + 300)
                    resp.read()   # finish this answer so the connection stays usable
                    alt, _, _ = st.ledger.choose(key, own, model, time.time(), st.switch_at,
                                                 st.hysteresis, avoid=chosen, labels=usable)
                    if alt != chosen:
                        why = "%s; %s refused (%s used up), retry on %s" % (why, chosen, win, alt)
                        chosen, retried = alt, True
                        continue
                break
        except (http.client.HTTPException, OSError) as exc:
            st.upstream.drop()
            self._reply(502, {"error": "upstream: %s" % str(exc)[:120]})
            return audit({"t": round(t0, 3), "method": method, "path": self.path.split("?")[0],
                          "model": model, "own": own or None, "account": used, "status": 502,
                          "ms": int((time.time() - t0) * 1000), "why": "upstream: %s" % str(exc)[:80]})
        self._stream(resp)
        seen = st.ledger.seen.get(used or "", {})
        audit({"t": round(t0, 3), "method": method, "path": self.path.split("?")[0], "model": model,
               "own": own or None, "account": used, "status": resp.status,
               "ms": int((time.time() - t0) * 1000), "why": why, "switched": switched, "retried": retried,
               "u5h": seen.get("5h", {}).get("pct"), "u7d": seen.get("7d", {}).get("pct")})

    def _stream(self, resp):
        """The upstream answer, status, headers and body, as it arrives: an SSE stream reaches
        the session event by event."""
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP:
                self.send_header(k, v)
        length = resp.getheader("Content-Length")
        chunked = length is None
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        else:
            self.send_header("Content-Length", length)
        self.end_headers()
        try:
            while True:
                data = resp.read1(65536)
                if not data:
                    break
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data) if chunked else data)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            # The session went away mid-answer: drop the upstream connection too, which ends the
            # generation instead of letting it run to the end unread.
            self.server.state.upstream.drop()
            self.close_connection = True


def serve(cfg=None):
    """Bind and return the server (the caller runs serve_forever); loopback only."""
    cfg = load_config() if cfg is None else cfg
    st = State(cfg)
    host, _, port = st.listen.rpartition(":")
    if host.strip("[]") not in ("127.0.0.1", "localhost", "::1"):
        sys.exit("quota-proxy: proxy.listen must be a loopback address, not %s" % st.listen)
    srv = ThreadingHTTPServer((host.strip("[]"), int(port)), Handler)
    srv.daemon_threads = True
    srv.state = st
    srv.sync = HubSync(st) if cfg.get("route") else None
    if srv.sync:
        srv.sync.start()
    audit({"t": round(time.time()), "proxy": "start", "listen": st.listen, "accounts": st.accounts.labels,
           "device_key": bool(st.device_key), "hub": bool(srv.sync), "off": not st.enabled()})
    return srv


def main():
    ap = argparse.ArgumentParser(prog="quota-proxy", description=__doc__.split("\n")[0])
    ap.add_argument("--off", action="store_true", help="make the running proxy a pass-through (no restart)")
    ap.add_argument("--on", action="store_true", help="switch account rotation back on")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.off or args.on or args.status:
        if args.off:
            open(OFF_FLAG, "a").close()
        if args.on and os.path.exists(OFF_FLAG):
            os.unlink(OFF_FLAG)
        print("quota-proxy: %s" % ("OFF (pass-through)" if os.path.exists(OFF_FLAG) else "on"))
        return 0
    srv = serve()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
