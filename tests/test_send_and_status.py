"""send.py (local notices and the peer round trip) and fleet-status.py (stubbed fleet)."""
import contextlib
import http.server
import importlib.util
import io
import json
import os
import shutil
import socket
import stat
import sys
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


send = load("fb_send", "relay/send.py")
agent = load("fb_agent_for_send", "relay/agent.py")
fstat = load("fb_fleet_status", "tools/fleet-status.py")

from test_relay_agent import FakeInbox  # noqa: E402  (same tests dir)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_main(mod, argv, func="main"):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["x"] + argv), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = getattr(mod, func)()
    return rc, out.getvalue(), err.getvalue()


class SendLocalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb-send-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = os.path.join(self.tmp, "sessions")
        os.makedirs(self.reg)
        for attr, val in (("HOME", self.tmp), ("REG", self.reg),
                          ("PEERS", os.path.join(self.tmp, "peers.json")),
                          ("SECRET_FILE", os.path.join(self.tmp, "secret")),
                          ("CONFIG", os.path.join(self.tmp, "config.json"))):
            p = mock.patch.object(send, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def register(self, sid, sock_path, cwd, at=None):
        with open(os.path.join(self.reg, sid[:12] + ".json"), "w") as fh:
            json.dump({"session_id": sid, "socket": sock_path, "token": "t", "cwd": cwd,
                       "registered_at": at or time.time()}, fh)

    def test_node_name_sources(self):
        with mock.patch.dict(os.environ, {"FLEET_NODE": "env-name"}):
            self.assertEqual(send.node_name(), "env-name")
        with open(send.CONFIG, "w") as fh:
            json.dump({"node": "cfg-name"}, fh)
        self.assertEqual(send.node_name(), "cfg-name")
        os.unlink(send.CONFIG)
        self.assertEqual(send.node_name(), os.uname().nodename)

    def test_local_notice_cwd_filter_and_dedupe(self):
        inside = os.path.join(self.tmp, "proj")
        outside = os.path.join(self.tmp, "other")
        os.makedirs(inside)
        os.makedirs(outside)
        a = FakeInbox(os.path.join(self.tmp, "a.sock"))
        b = FakeInbox(os.path.join(self.tmp, "b.sock"))
        a.start()
        b.start()
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        self.register("in-old", a.path, inside, at=1)
        self.register("in-new", a.path, inside, at=2)      # same socket: only newest delivered
        self.register("out-1", b.path, outside)
        rc, out, _ = run_main(send, ["--local", "--cwd", inside, "heads up"])
        self.assertEqual(rc, 0)
        self.assertIn("in-new", out)
        for _ in range(100):
            if a.received:
                break
            time.sleep(0.02)
        self.assertEqual(len(a.received), 1)
        self.assertEqual(b.received, [])
        self.assertIn("[fleet-bridge notice from", a.received[0])

    def test_local_no_sessions_and_dead_socket(self):
        rc, _, err = run_main(send, ["--local", "hello"])
        self.assertEqual(rc, 1)
        self.assertIn("no live sessions", err)
        dead = os.path.join(self.tmp, "dead.sock")
        open(dead, "w").close()
        self.register("dead-1", dead, self.tmp)
        rc, _, err = run_main(send, ["--local", "hello"])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", err)

    def test_local_requires_text(self):
        with self.assertRaises(SystemExit):
            run_main(send, ["--local"])

    def test_no_peers_file(self):
        with self.assertRaises(SystemExit) as cm:
            run_main(send, ["peer-a", "hi"])
        self.assertIn("no peers configured", str(cm.exception.code))


class SendPeerTest(unittest.TestCase):
    """A real relay agent on loopback as the peer."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb-sendpeer-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = os.path.join(self.tmp, "sessions")
        os.makedirs(self.reg)
        self.secret = os.path.join(self.tmp, "secret")
        with open(self.secret, "w") as fh:
            fh.write("shared-fleet-secret\n")
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w") as fh:
            json.dump({"envelope": "sdk_user", "node": "peer-a"}, fh)
        agents_sio = sys.modules["socketio"]
        for mod, attr, val in ((agent, "HOME", self.tmp), (agent, "REG", self.reg),
                               (agent, "SECRET_FILE", self.secret),
                               (agents_sio, "CONFIG", self.config)):
            p = mock.patch.object(mod, attr, val)
            p.start()
            self.addCleanup(p.stop)
        agent.SEEN.clear()
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
        threading.Thread(target=self.httpd.serve_forever,
                         kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.peers = os.path.join(self.tmp, "peers.json")
        with open(self.peers, "w") as fh:
            json.dump({"peer-a": self.url, "peer-down": "http://127.0.0.1:%d" % free_port()}, fh)
        for attr, val in (("HOME", self.tmp), ("REG", self.reg), ("PEERS", self.peers),
                          ("SECRET_FILE", self.secret), ("CONFIG", self.config)):
            p = mock.patch.object(send, attr, val)
            p.start()
            self.addCleanup(p.stop)

    def test_round_trip_with_session_and_account(self):
        box = FakeInbox(os.path.join(self.tmp, "s.sock"))
        box.start()
        self.addCleanup(box.stop)
        with open(os.path.join(self.reg, "sess.json"), "w") as fh:
            json.dump({"session_id": "cafe0001-x", "socket": "uds:" + box.path, "token": "tk",
                       "account": "ai@", "registered_at": time.time()}, fh)
        rc, out, _ = run_main(send, ["peer-a", "ship it", "--session", "cafe", "--account", "ai@",
                                     "--from", "test-sender"])
        self.assertEqual(rc, 0, out)
        self.assertIn('"delivered_to": "cafe0001-x"', out)
        for _ in range(100):
            if box.received:
                break
            time.sleep(0.02)
        self.assertIn("ship it", box.received[0])

    def test_http_error_and_unreachable_peer(self):
        rc, _, err = run_main(send, ["peer-a", "hi"])         # no sessions on the peer: 404
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 404", err)
        rc, _, err = run_main(send, ["peer-down", "hi"])
        self.assertEqual(rc, 1)
        self.assertIn("send failed", err)

    def test_peers_listing_and_unknown_peer(self):
        rc, out, _ = run_main(send, ["--peers"])
        self.assertEqual(rc, 0)
        self.assertIn("up", out)
        self.assertIn("DOWN", out)
        with self.assertRaises(SystemExit) as cm:
            run_main(send, ["stranger", "hi"])
        self.assertIn("unknown peer", str(cm.exception.code))
        with self.assertRaises(SystemExit):
            run_main(send, ["peer-a"])                         # text missing


class FleetStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb-status-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "stubs")
        os.makedirs(os.path.join(self.tmp, "bin"))
        os.makedirs(self.bin)
        for attr, val in (("HOME", self.tmp), ("CFG", os.path.join(self.tmp, "config.json")),
                          ("STATE", os.path.join(self.tmp, "status.state"))):
            p = mock.patch.object(fstat, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.urls = {}
        patcher = mock.patch.object(fstat, "http_json",
                                    side_effect=lambda url, timeout=5: self.urls[url.split("?")[0]]())
        patcher.start()
        self.addCleanup(patcher.stop)

    def stub_systemctl(self, blocks):
        path = os.path.join(self.bin, "systemctl")
        with open(path, "w") as fh:
            fh.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % blocks)
        os.chmod(path, 0o755)
        ssh = os.path.join(self.bin, "ssh")
        with open(ssh, "w") as fh:
            fh.write("#!/bin/sh\nexec systemctl\n")   # the stubbed one, via PATH
        os.chmod(ssh, 0o755)
        env = mock.patch.dict(os.environ, {"PATH": self.bin + os.pathsep + os.environ["PATH"]})
        env.start()
        self.addCleanup(env.stop)

    def write_cfg(self, obj):
        with open(fstat.CFG, "w") as fh:
            json.dump(obj, fh)

    def relay_ok(self):
        return 200, {"ok": True, "sessions": ["a", "b"]}

    def test_healthy_pass_and_quiet(self):
        self.stub_systemctl("Id=fleet-relay.service\nActiveState=active\nNRestarts=0\n\n"
                            "Id=fleet-relay-tunnel.service\nActiveState=active\nNRestarts=1")
        self.write_cfg({"node": "msi", "status": {
            "control_plane": "http://cp.test/api/v1",
            "hub_ssh": "u@hub", "hub_units": ["fleet-relay"],
            "peer_ssh": {"peer-b": "u@peer"}}})
        self.urls = {
            "http://cp.test/api/v1/workers": lambda: (200, {"workers": [
                {"name": "w-ok", "online": True, "health": "healthy"}]}),
            "http://127.0.0.1:8787/v1/health": self.relay_ok,
        }
        fstat.results.clear()
        rc, out, _ = run_main(fstat, [])
        self.assertEqual(rc, 0, out)
        self.assertIn("OK", out)
        self.assertIn("w-ok", out)
        fstat.results.clear()
        rc, out, _ = run_main(fstat, ["--quiet"])
        self.assertEqual(rc, 0)
        self.assertNotIn("OK   fleet", out)         # per-check OK lines suppressed

    def test_flapping_unit_and_failures(self):
        self.stub_systemctl("Id=fleet-relay.service\nActiveState=active\nNRestarts=5")
        self.write_cfg({"node": "msi", "status": {"units": ["fleet-relay"],
                                                  "control_plane": "http://cp.test/api/v1"}})
        with open(fstat.STATE, "w") as fh:
            json.dump({"msi/fleet-relay": 3}, fh)    # restarts moved 3 -> 5
        self.urls = {
            "http://cp.test/api/v1/workers": lambda: (200, {"workers": [
                {"name": "w-sick", "online": False, "health": "unhealthy"}]}),
            "http://127.0.0.1:8787/v1/health": lambda: (_ for _ in ()).throw(OSError("down")),
        }
        fstat.results.clear()
        rc, out, _ = run_main(fstat, [])
        self.assertEqual(rc, 1)
        self.assertIn("+2 since last pass", out)
        self.assertIn("FAIL", out)
        self.assertIn("w-sick", out)

    def test_delegate_token_log_and_probe(self):
        self.stub_systemctl("Id=fleet-relay.service\nActiveState=active\nNRestarts=0\n\n"
                            "Id=fleet-relay-tunnel.service\nActiveState=active\nNRestarts=0")
        env_file = os.path.join(self.tmp, "delegate.env")
        with open(env_file, "w") as fh:
            fh.write("CLAUDE_CODE_OAUTH_TOKEN=x\n")
        os.chmod(env_file, 0o600)
        self.write_cfg({"node": "msi", "delegate": {"env_file": env_file}})
        with open(os.path.join(self.tmp, "delegate.log"), "w") as fh:
            fh.write(json.dumps({"t": time.time(), "ok": True}) + "\n")
            fh.write(json.dumps({"t": time.time(), "ok": False, "subtype": "timeout"}) + "\n")
            fh.write("junk line\n")
        fa = os.path.join(self.tmp, "bin", "fleet-agent")
        with open(fa, "w") as fh:
            fh.write("#!/bin/sh\necho FLEET-OK\n")
        os.chmod(fa, 0o755)
        self.urls = {"http://127.0.0.1:8787/v1/health": self.relay_ok}
        fstat.results.clear()
        rc, out, _ = run_main(fstat, ["--probe"])
        self.assertEqual(rc, 1)                      # the failed delegate run in the log
        self.assertIn("delegate token", out)
        self.assertIn("timeout", out)
        self.assertIn("delegate probe", out)
        self.assertIn("1 failed", out)

    def test_unit_parser_and_no_answer(self):
        self.stub_systemctl("")
        self.write_cfg({"node": "msi", "status": {"units": ["ghost-unit"]}})
        self.urls = {"http://127.0.0.1:8787/v1/health": self.relay_ok}
        fstat.results.clear()
        rc, out, _ = run_main(fstat, [])
        self.assertEqual(rc, 1)
        self.assertIn("no answer", out)


if __name__ == "__main__":
    unittest.main()
