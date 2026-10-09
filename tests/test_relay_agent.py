"""The relay agent end to end on loopback: auth, replay, delivery into a fake inbox socket;
plus the socket codec and the envelope probe."""
import contextlib
import hashlib
import hmac
import http.client
import importlib.util
import io
import json
import os
import shutil
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


socketio = load("fb_socketio", "relay/socketio.py")
agent = load("fb_agent", "relay/agent.py")
probe = load("fb_probe", "relay/probe.py")


class FakeInbox(threading.Thread):
    """A Claude Code inbox socket: accepts, reads lines, optionally answers."""

    def __init__(self, path, reply=b"got-it\n", answer=True):
        super().__init__(daemon=True)
        self.path, self.reply, self.answer = path, reply, answer
        self.received = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(4)

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b""
                try:
                    while True:
                        b = conn.recv(4096)
                        if not b:
                            break
                        data += b
                except OSError:
                    pass
                self.received.append(data.decode())
                if self.answer:
                    try:
                        conn.sendall(self.reply)
                    except OSError:
                        pass

    def stop(self):
        try:
            self.srv.close()
        except OSError:
            pass


class RelayAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb-agent-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = os.path.join(self.tmp, "sessions")
        os.makedirs(self.reg)
        self.secret_file = os.path.join(self.tmp, "secret")
        with open(self.secret_file, "w") as fh:
            fh.write("relay-secret-123\n")
        self.config = os.path.join(self.tmp, "config.json")
        self.write_config({"envelope": "sdk_user"})
        import sys as _sys
        agents_sio = _sys.modules["socketio"]          # the instance agent.py imported
        for mod, attr, val in ((agent, "HOME", self.tmp), (agent, "REG", self.reg),
                               (agent, "SECRET_FILE", self.secret_file),
                               (socketio, "CONFIG", self.config),
                               (agents_sio, "CONFIG", self.config)):
            patcher = mock.patch.object(mod, attr, val)
            patcher.start()
            self.addCleanup(patcher.stop)
        agent.SEEN.clear()
        self.srv = socketserver.ThreadingTCPServer.__mro__  # noqa: keep import honest
        self.httpd = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(
            ("127.0.0.1", 0), agent.Handler)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.port = self.httpd.server_address[1]

    def write_config(self, obj):
        with open(self.config, "w") as fh:
            json.dump(obj, fh)

    def inbox(self, name="s1.sock", **kw):
        path = os.path.join(self.tmp, name)
        box = FakeInbox(path, **kw)
        box.start()
        self.addCleanup(box.stop)
        return box

    def register(self, session_id, sock_path, token="tok-1", account="ai@", uds_prefix=True):
        entry = {"session_id": session_id, "socket": ("uds:" + sock_path) if uds_prefix else sock_path,
                 "token": token, "account": account, "node": "test-node",
                 "registered_at": time.time()}
        with open(os.path.join(self.reg, session_id[:8] + ".json"), "w") as fh:
            json.dump(entry, fh)
        return entry

    def post(self, body, ts=None, sig=None):
        raw = json.dumps(body).encode()
        ts = str(int(time.time())) if ts is None else ts
        if sig is None:
            sig = hmac.new(b"relay-secret-123", ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/v1/message", body=raw,
                  headers={"X-Fleet-Ts": ts, "X-Fleet-Sig": sig, "Content-Type": "application/json"})
        r = c.getresponse()
        out = json.loads(r.read() or b"{}")
        c.close()
        return r.status, out

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path)
        r = c.getresponse()
        out = json.loads(r.read() or b"{}")
        c.close()
        return r.status, out

    # ── delivery ────────────────────────────────────────────────────────────────────────────

    def test_delivery_with_auth_and_reply(self):
        box = self.inbox()
        self.register("abcd1234-session", box.path)
        status, out = self.post({"text": "hello there", "from": "laptop-a"})
        self.assertEqual(status, 200, out)
        self.assertEqual(out["delivered_to"], "abcd1234-session")
        self.assertEqual(out["socket_reply"], "got-it\n")
        for _ in range(100):
            if box.received:
                break
            time.sleep(0.02)
        wire = box.received[0]
        lines = wire.strip().split("\n")
        self.assertEqual(json.loads(lines[0]), {"type": "auth", "token": "tok-1"})
        msg = json.loads(lines[1])
        self.assertEqual(msg["type"], "user")
        self.assertIn("[fleet-bridge message from laptop-a]", msg["message"]["content"])
        self.assertIn("hello there", msg["message"]["content"])

    def test_session_and_account_filters(self):
        a = self.inbox("a.sock")
        b = self.inbox("b.sock")
        self.register("aaaa1111-x", a.path, account="john@")
        time.sleep(0.02)
        self.register("bbbb2222-x", b.path, account="ai@")
        status, out = self.post({"text": "to a", "session": "aaaa"})
        self.assertEqual(out["delivered_to"], "aaaa1111-x")
        status, out = self.post({"text": "to john", "account": "john@"})
        self.assertEqual(out["delivered_to"], "aaaa1111-x")
        status, out = self.post({"text": "nobody", "account": "ghost@"})
        self.assertEqual(status, 404)
        self.assertIn("ghost@", out["error"])

    def test_dead_socket_is_502(self):
        dead = os.path.join(self.tmp, "dead.sock")
        open(dead, "w").close()                       # exists, but is not a socket
        self.register("dddd4444-x", dead)
        status, out = self.post({"text": "x"})
        self.assertEqual(status, 502)
        self.assertIn("socket delivery failed", out["error"])

    def test_notice_envelope_and_tokenless_delivery(self):
        self.write_config({"envelope": "notice"})
        box = self.inbox()
        self.register("eeee5555-x", box.path, token=None)
        status, out = self.post({"text": "fyi", "from": "msi"})
        self.assertEqual(status, 200)
        for _ in range(100):
            if box.received:
                break
            time.sleep(0.02)
        lines = box.received[0].strip().split("\n")
        self.assertEqual(len(lines), 1)               # no auth line without a token
        self.assertIn("[fleet-bridge notice from msi]", json.loads(lines[0])["message"]["content"])

    # ── refusals ────────────────────────────────────────────────────────────────────────────

    def test_auth_refusals(self):
        box = self.inbox()
        self.register("ffff6666-x", box.path)
        raw = json.dumps({"text": "x"}).encode()
        old = str(int(time.time()) - 999)
        sig = hmac.new(b"relay-secret-123", old.encode() + b"." + raw, hashlib.sha256).hexdigest()
        self.assertEqual(self.post({"text": "x"}, ts=old, sig=sig)[0], 401)       # stale
        self.assertEqual(self.post({"text": "x"}, ts="soon")[1]["error"],
                         "stale or missing timestamp")
        self.assertEqual(self.post({"text": "x"}, sig="0" * 64)[0], 401)          # bad sig
        ts = str(int(time.time()))
        raw2 = json.dumps({"text": "once"}).encode()
        sig2 = hmac.new(b"relay-secret-123", ts.encode() + b"." + raw2, hashlib.sha256).hexdigest()
        self.assertEqual(self.post({"text": "once"}, ts=ts, sig=sig2)[0], 200)
        status, out = self.post({"text": "once"}, ts=ts, sig=sig2)                # replay
        self.assertEqual((status, out["error"]), (401, "replayed message"))

    def test_body_refusals(self):
        self.assertEqual(self.post({"nothing": True})[0], 400)                    # no text
        ts = str(int(time.time()))
        raw = b"this is not json"
        sig = hmac.new(b"relay-secret-123", ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/v1/message", body=raw, headers={"X-Fleet-Ts": ts, "X-Fleet-Sig": sig})
        self.assertEqual(c.getresponse().status, 400)
        c.close()
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/v1/message", body=b"x",
                  headers={"X-Fleet-Ts": "1", "X-Fleet-Sig": "x", "Content-Length": "2000001"})
        self.assertEqual(c.getresponse().status, 413)
        c.close()
        self.assertEqual(self.post({"text": "x"})[0], 404)                        # no sessions
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/v1/elsewhere", body=b"{}")
        self.assertEqual(c.getresponse().status, 404)
        c.close()

    def test_no_envelope_is_503(self):
        self.write_config({})
        box = self.inbox()
        self.register("gggg7777-x", box.path)
        status, out = self.post({"text": "x"})
        self.assertEqual(status, 503)
        self.assertIn("envelope", out["error"])

    # ── reads ───────────────────────────────────────────────────────────────────────────────

    def test_health_lists_sessions_and_accounts(self):
        box = self.inbox()
        self.register("hhhh8888-x", box.path, account="ai@")
        status, out = self.get("/v1/health")
        self.assertEqual(status, 200)
        self.assertTrue(out["ok"])
        self.assertEqual(out["envelope"], "sdk_user")
        self.assertEqual(out["sessions"], ["hhhh8888-x"])
        self.assertEqual(out["accounts"], {"ai@": 1})
        self.assertEqual(self.get("/v1/other")[0], 404)

    def test_status_uses_nodestatus(self):
        with mock.patch.object(agent.nodestatus, "status", return_value={"stub": True}) as st:
            status, out = self.get("/v1/status")
        self.assertEqual((status, out), (200, {"stub": True}))
        st.assert_called_once()

    def test_registry_ignores_broken_and_gone_entries(self):
        with open(os.path.join(self.reg, "current.json"), "w") as fh:
            fh.write("{}")
        with open(os.path.join(self.reg, "junk.json"), "w") as fh:
            fh.write("not json")
        self.register("iiii9999-x", os.path.join(self.tmp, "vanished.sock"))  # no such socket
        self.assertEqual(agent.live_sessions(), [])

    def test_first_use_prunes_old_signatures(self):
        agent.SEEN["ancient"] = time.time() - 10 * agent.SKEW
        self.assertTrue(agent.first_use("fresh-sig", int(time.time())))
        self.assertNotIn("ancient", agent.SEEN)
        self.assertFalse(agent.first_use("fresh-sig", int(time.time())))

    def test_main_guards(self):
        with mock.patch.object(agent, "SECRET_FILE", os.path.join(self.tmp, "nope")):
            with self.assertRaises(SystemExit) as cm:
                agent.main()
        self.assertIn("install.sh", str(cm.exception.code))
        with mock.patch.dict(os.environ, {"FLEET_RELAY_BIND": "0.0.0.0"}):
            with self.assertRaises(SystemExit) as cm:
                agent.main()
        self.assertIn("refusing to bind", str(cm.exception.code))


class SocketioTest(unittest.TestCase):
    def test_build_envelopes_and_unknown(self):
        wire = socketio.build("hi", "msi", "sdk_user")
        self.assertIn("reply with", json.loads(wire)["message"]["content"])
        wire = socketio.build("hi", "msi", "notice")
        self.assertNotIn("reply with", json.loads(wire)["message"]["content"])
        with self.assertRaises(ValueError):
            socketio.build("hi", "msi", "telegram")

    def test_deliver_strips_uds_prefix_and_times_out_quietly(self):
        tmp = tempfile.mkdtemp(prefix="fb-sio-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "s.sock")
        box = FakeInbox(path, answer=False)
        box.start()
        self.addCleanup(box.stop)
        reply = socketio.deliver("uds:" + path, None, "quiet", "t", "sdk_user", timeout=0.4)
        self.assertEqual(reply, "")

    def test_load_config_missing_is_empty(self):
        with mock.patch.object(socketio, "CONFIG", "/nonexistent/path/config.json"):
            self.assertEqual(socketio.load_config(), {})


class ProbeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb-probe-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = os.path.join(self.tmp, "sessions")
        os.makedirs(self.reg)
        self.config = os.path.join(self.tmp, "config.json")
        for mod, attr, val in ((probe, "REG", self.reg), (probe, "CONFIG", self.config)):
            p = mock.patch.object(mod, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def run_probe(self, argv, stdin=""):
        out = io.StringIO()
        with mock.patch.object(probe.sys, "argv", ["probe.py"] + argv), \
             mock.patch.object(probe.sys, "stdin", io.StringIO(stdin)), \
             mock.patch("builtins.input", side_effect=lambda *_: stdin), \
             contextlib.redirect_stdout(out):
            rc = probe.main()
        return rc, out.getvalue()

    def register(self, session_id, sock_path):
        with open(os.path.join(self.reg, session_id[:8] + ".json"), "w") as fh:
            json.dump({"session_id": session_id, "socket": sock_path, "token": "t",
                       "node": "n", "cwd": "/x", "registered_at": time.time()}, fh)

    def test_no_sessions(self):
        rc, out = self.run_probe([])
        self.assertEqual(rc, 1)
        self.assertIn("no registered sessions", out)

    def test_list(self):
        self.register("abc-1", "/tmp/none")
        rc, out = self.run_probe(["--list"])
        self.assertEqual(rc, 0)
        self.assertIn("abc-1", out)

    def test_probe_writes_confirmed_envelope(self):
        path = os.path.join(self.tmp, "p.sock")
        box = FakeInbox(path)
        box.start()
        self.addCleanup(box.stop)
        self.register("abc-2", path)
        rc, out = self.run_probe(["--delay", "0"], stdin="1")
        self.assertEqual(rc, 0, out)
        self.assertEqual(json.load(open(self.config))["envelope"], "sdk_user")
        self.assertTrue(box.received)

    def test_probe_abort_and_out_of_range_and_missing_session(self):
        path = os.path.join(self.tmp, "p2.sock")
        box = FakeInbox(path)
        box.start()
        self.addCleanup(box.stop)
        self.register("abc-3", path)
        rc, _ = self.run_probe(["--delay", "0"], stdin="")
        self.assertEqual(rc, 1)
        rc, _ = self.run_probe(["--delay", "0"], stdin="99")
        self.assertEqual(rc, 1)
        rc, out = self.run_probe(["--session", "zzz", "--delay", "0"])
        self.assertEqual(rc, 1)
        self.assertIn("no session matching", out)

    def test_send_failure_is_reported_not_raised(self):
        self.register("abc-4", os.path.join(self.tmp, "never.sock"))
        rc, out = self.run_probe(["--delay", "0"], stdin="")
        self.assertIn("SEND FAILED", out)


if __name__ == "__main__":
    unittest.main()
