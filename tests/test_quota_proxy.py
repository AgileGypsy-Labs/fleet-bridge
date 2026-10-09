"""proxy/quota-proxy.py against a fake upstream: the token swap, stickiness and the switch at
the line, the quota-429 retry, streaming, pass-through and an audit log without tokens."""
import http.client
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "delegate"))


def load_proxy():
    spec = importlib.util.spec_from_file_location("quota_proxy", os.path.join(ROOT, "proxy", "quota-proxy.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeUpstream(threading.Thread):
    """api.anthropic.com as far as the proxy can tell: streamed answers with unified rate-limit
    headers per token, quota 429s on demand, and a record of every request."""

    def __init__(self):
        super().__init__(daemon=True)
        self.requests = []
        self.util = {"tok-john": 0.95, "tok-ai": 0.20, "login-token": 0.10}   # 7d utilization per token
        self.reject = set()           # tokens answered with a quota 429
        self.burst = set()            # tokens answered with a burst 429 (no unified status)
        self.revoked = set()          # tokens answered with 401
        up = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _headers(self, tok, status):
                u = up.util.get(tok, 0.5)
                self.send_response(status)
                self.send_header("request-id", "req_test")
                self.send_header("anthropic-ratelimit-unified-5h-utilization", "0.30")
                self.send_header("anthropic-ratelimit-unified-5h-reset", str(int(time.time()) + 4000))
                self.send_header("anthropic-ratelimit-unified-5h-status", "allowed")
                self.send_header("anthropic-ratelimit-unified-7d-utilization", "%.2f" % u)
                self.send_header("anthropic-ratelimit-unified-7d-reset", str(int(time.time()) + 86400))
                self.send_header("anthropic-ratelimit-unified-7d-status", "rejected" if status == 429 and tok in up.reject else "allowed")
                self.send_header("anthropic-ratelimit-unified-status", "rejected" if status == 429 and tok in up.reject else "allowed")

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                up.requests.append({"path": self.path, "token": tok, "body": body,
                                    "fleet_headers": [k for k in self.headers if k.lower().startswith("x-fleet")],
                                    "beta": self.headers.get("anthropic-beta")})
                if tok in up.revoked:
                    err = json.dumps({"type": "error", "error": {"type": "authentication_error"}}).encode()
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return
                if tok in up.reject or tok in up.burst:
                    self._headers(tok, 429)
                    if tok in up.burst:
                        self.send_header("retry-after", "5")
                    err = json.dumps({"type": "error", "error": {"type": "rate_limit_error"}}).encode()
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return
                self._headers(tok, 200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i in range(3):
                    ev = ("event: content_block_delta\ndata: {\"i\": %d}\n\n" % i).encode()
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(ev), ev))
                    self.wfile.flush()
                    time.sleep(0.15)
                self.wfile.write(b"0\r\n\r\n")

            def do_GET(self):
                tok = (self.headers.get("Authorization") or "").replace("Bearer ", "")
                up.requests.append({"path": self.path, "token": tok, "body": b""})
                body = json.dumps({"seen": tok}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def run(self):
        self.server.serve_forever(poll_interval=0.05)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class QuotaProxyTest(unittest.TestCase):
    def setUp(self):
        self.qp = load_proxy()
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.qp.FB = self.home
        self.qp.CONFIG = os.path.join(self.home, "config.json")
        self.qp.OFF_FLAG = os.path.join(self.home, "proxy.off")
        self.qp.LOG = os.path.join(self.home, "proxy.log")
        self.upstream = FakeUpstream()
        self.upstream.start()
        self.addCleanup(self.upstream.stop)
        os.makedirs(os.path.join(self.home, "accounts"))
        for name, tok in (("john", "tok-john"), ("ai", "tok-ai")):
            path = os.path.join(self.home, "accounts", name + ".env")
            with open(path, "w") as fh:
                fh.write("CLAUDE_CODE_OAUTH_TOKEN=%s\n" % tok)
            os.chmod(path, 0o600)
        self.key_file = os.path.join(self.home, "proxy.key")
        with open(self.key_file, "w") as fh:
            fh.write("device-key-123\n")
        self.cfg = {"node": "laptop-a",
                    "proxy": {"listen": "127.0.0.1:0", "upstream": self.upstream.url,
                              "device_key": self.key_file},
                    "delegate": {"accounts": [
                        {"label": "john@", "config_dir": "~/.claude", "own": True,
                         "token_file": os.path.join(self.home, "accounts", "john.env")},
                        {"label": "ai@", "config_dir": "~/.claude-ai",
                         "token_file": os.path.join(self.home, "accounts", "ai.env")}]}}
        self.srv = self.qp.serve(self.cfg)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, path="/v1/messages", model="claude-fable-5-1", device=True, account="john@",
             method="POST"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Authorization": "Bearer login-token", "Content-Type": "application/json",
                   "anthropic-beta": "oauth-2025-04-20", "Host": "api.anthropic.com"}
        if device:
            headers["X-Fleet-Device"] = "device-key-123"
        if account:
            headers["X-Fleet-Account"] = account
        body = json.dumps({"model": model, "stream": True, "messages": [{"role": "user", "content": "hi"}]})
        c.request(method, path, body=body if method == "POST" else None, headers=headers)
        r = c.getresponse()
        t0 = time.time()
        chunks = []
        while True:
            data = r.read1(65536)
            if not data:
                break
            chunks.append((round(time.time() - t0, 2), data))
        c.close()
        return r.status, dict((k.lower(), v) for k, v in r.getheaders()), chunks

    def audit(self, n=1):
        """The request lines of the audit log, once at least n are there (the proxy writes the
        line after the last byte reached the client, so a reader can be a moment early)."""
        for _ in range(100):
            try:
                with open(self.qp.LOG) as fh:
                    lines = [json.loads(l) for l in fh if '"method"' in l]
            except OSError:
                lines = []
            if len(lines) >= n:
                return lines
            time.sleep(0.02)
        return lines

    def test_sticky_then_switch_at_the_line_and_streaming_intact(self):
        status, headers, chunks = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.requests[-1]["token"], "tok-john")     # own account's token
        self.assertEqual(self.upstream.requests[-1]["fleet_headers"], [])    # X-Fleet-* never go upstream
        self.assertEqual(self.upstream.requests[-1]["beta"], "oauth-2025-04-20")
        self.assertEqual(headers["content-type"], "text/event-stream")
        self.assertEqual(headers["anthropic-ratelimit-unified-7d-utilization"], "0.95")
        body = b"".join(d for _, d in chunks)
        self.assertEqual(body.count(b"event: content_block_delta"), 3)
        self.assertGreaterEqual(len(chunks), 3, chunks)                      # arrived event by event
        self.assertGreaterEqual(chunks[-1][0] - chunks[0][0], 0.2)
        # john@ at 95% now: the next request moves to ai@ and stays there
        status, _, _ = self.call()
        self.assertEqual(self.upstream.requests[-1]["token"], "tok-ai")
        self.call()
        self.assertEqual(self.upstream.requests[-1]["token"], "tok-ai")
        log = self.audit(3)
        self.assertEqual([e["account"] for e in log], ["john@", "ai@", "ai@"])
        self.assertTrue(log[1]["switched"])
        self.assertIn("john@ 95% -> ai@", log[1]["why"])
        self.assertEqual((log[0]["model"], log[0]["own"], log[0]["status"]), ("claude-fable-5-1", "john@", 200))

    def test_quota_429_is_retried_once_on_the_other_account(self):
        self.upstream.reject.add("tok-john")
        status, _, chunks = self.call()
        self.assertEqual(status, 200)
        self.assertEqual([r["token"] for r in self.upstream.requests], ["tok-john", "tok-ai"])
        log = self.audit()
        self.assertTrue(log[0]["retried"])
        self.assertIn("refused (7d used up), retry on ai@", log[0]["why"])
        # a burst 429 (no unified rejection) is returned as it is, no retry
        self.upstream.reject.clear()
        self.upstream.burst.add("tok-ai")
        status, headers, _ = self.call()
        self.assertEqual((status, headers.get("retry-after")), (429, "5"))
        self.assertEqual(self.upstream.requests[-1]["token"], "tok-ai")
        self.assertEqual(len(self.upstream.requests), 3)

    def test_a_refused_token_falls_back_to_the_session_and_is_left_alone(self):
        self.upstream.revoked.add("tok-john")
        status, _, _ = self.call()
        self.assertEqual(status, 200)
        self.assertEqual([r["token"] for r in self.upstream.requests], ["tok-john", "login-token"])
        self.assertIn("token refused (401), pass-through", self.audit()[0]["why"])
        # the next request does not try that token again; it goes straight to the other account
        self.call()
        self.assertEqual(self.upstream.requests[-1]["token"], "tok-ai")

    def test_pass_through_without_device_key_off_flag_or_for_non_inference(self):
        self.call(device=False)
        self.assertEqual(self.upstream.requests[-1]["token"], "login-token")
        self.call(path="/api/oauth/usage", method="GET")
        self.assertEqual(self.upstream.requests[-1]["token"], "login-token")
        open(self.qp.OFF_FLAG, "a").close()
        self.call()
        self.assertEqual(self.upstream.requests[-1]["token"], "login-token")
        os.unlink(self.qp.OFF_FLAG)
        self.call()
        self.assertEqual(self.upstream.requests[-1]["token"], "tok-john")

    def test_audit_log_never_holds_a_token(self):
        self.call()
        self.upstream.reject.add("tok-john")
        self.call()
        with open(self.qp.LOG) as fh:
            text = fh.read()
        for secret in ("tok-john", "tok-ai", "login-token", "device-key-123"):
            self.assertNotIn(secret, text)
        self.assertIn('"account": "john@"', text)

    def _raw(self, send, read_after=True):
        """Open a raw socket to the proxy, run send(sock), return the status line + headers text."""
        import socket
        s = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(s.close)
        try:
            send(s)
        except (BrokenPipeError, ConnectionResetError):
            pass                                          # the proxy may refuse mid-send; the reply still stands
        out = b""
        try:
            while b"\r\n\r\n" not in out:
                data = s.recv(65536)
                if not data:
                    break
                out += data
        except (ConnectionResetError, TimeoutError):
            pass
        return out.decode("latin-1", "replace")

    def test_oversize_content_length_is_refused_with_413_unread(self):
        huge = self.qp.MAX_BODY + 1
        def send(s):
            s.sendall(("POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n"
                       "Authorization: Bearer login-token\r\nX-Fleet-Device: device-key-123\r\n"
                       "Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % huge).encode())
            s.sendall(b"x" * 1024)                        # only a sliver: the 413 must not wait for the body
        t0 = time.time()
        reply = self._raw(send)
        self.assertIn(" 413 ", reply.splitlines()[0])
        self.assertLess(time.time() - t0, 5)              # refused up front, not after a 32MB read
        self.assertEqual(self.upstream.requests, [])      # nothing went upstream

    def test_oversize_chunked_body_is_refused_with_413(self):
        def send(s):
            s.sendall(b"POST /v1/messages HTTP/1.1\r\nHost: api.anthropic.com\r\n"
                      b"Authorization: Bearer login-token\r\nX-Fleet-Device: device-key-123\r\n"
                      b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n")
            chunk = b"y" * 65536
            frame = b"%x\r\n%s\r\n" % (len(chunk), chunk)
            for _ in range(self.qp.MAX_BODY // len(chunk) + 2):
                s.sendall(frame)                          # never sends the final 0 chunk: the cap must fire first
        reply = self._raw(send)
        self.assertIn(" 413 ", reply.splitlines()[0])
        self.assertEqual(self.upstream.requests, [])

    def test_header_flood_is_refused_with_431(self):
        def send(s):
            lines = ["POST /v1/messages HTTP/1.1", "Host: api.anthropic.com",
                     "Authorization: Bearer login-token", "X-Fleet-Device: device-key-123",
                     "Content-Length: 2"]
            lines += ["X-Pad-%d: a" % i for i in range(self.qp.MAX_HEADERS + 1)]
            s.sendall(("\r\n".join(lines) + "\r\n\r\nhi").encode())
        reply = self._raw(send)
        self.assertIn(" 431 ", reply.splitlines()[0])
        self.assertEqual(self.upstream.requests, [])

    def test_body_at_the_limit_still_relays(self):
        # a large-but-legal body passes through untouched (regression for the new guard)
        import socket
        payload = json.dumps({"model": "claude-fable-5-1", "stream": True,
                              "messages": [{"role": "user", "content": "z" * 200000}]}).encode()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        c.request("POST", "/v1/messages", body=payload,
                  headers={"Authorization": "Bearer login-token", "X-Fleet-Device": "device-key-123",
                           "Content-Type": "application/json", "Host": "api.anthropic.com",
                           "anthropic-beta": "oauth-2025-04-20"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual(r.status, 200)
        self.assertEqual(len(self.upstream.requests[-1]["body"]), len(payload))

    def test_proxy_env_points_a_child_at_a_live_proxy_only(self):
        import route
        route.CONFIG = os.path.join(self.home, "config.json")
        cfg = dict(self.cfg, proxy=dict(self.cfg["proxy"], listen="127.0.0.1:%d" % self.port))
        env = route.proxy_env({}, cfg, "ai@")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:%d" % self.port)
        self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "X-Fleet-Device: device-key-123\nX-Fleet-Account: ai@")
        open(self.qp.OFF_FLAG, "a").close()
        self.assertEqual(route.proxy_env({}, cfg, "ai@"), {})
        os.unlink(self.qp.OFF_FLAG)
        dead = dict(self.cfg, proxy=dict(self.cfg["proxy"], listen="127.0.0.1:1"))
        self.assertEqual(route.proxy_env({}, dead, "ai@"), {})
        self.assertEqual(route.proxy_env({}, {"node": "x"}, "ai@"), {})


if __name__ == "__main__":
    unittest.main()
