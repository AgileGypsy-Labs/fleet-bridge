"""End-to-end tests that need no Claude account, no network and no systemd.

    python3 -m unittest discover -s tests -v

The relay runs as a real process against a fake inbox socket; fleet-agent runs against a
fake `claude` on PATH that records what it was given.
"""
import hashlib
import hmac
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "relay")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeInbox(threading.Thread):
    """A Unix socket standing in for a Claude Code session's inbox."""

    def __init__(self, path):
        super().__init__(daemon=True)
        self.path, self.received = path, []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(4)

    def run(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            conn.close()
            self.received.append([json.loads(line) for line in data.decode().splitlines() if line])


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        fb = os.path.join(self.home, ".fleet-bridge")
        os.makedirs(os.path.join(fb, "sessions"))
        self.secret = b"s3cret-for-tests"
        with open(os.path.join(fb, "secret"), "wb") as fh:
            fh.write(self.secret)
        with open(os.path.join(fb, "config.json"), "w") as fh:
            json.dump({"node": "laptop-b", "envelope": "sdk_user"}, fh)
        self.inboxes = {}
        for sid, age in (("aaaa1111", 10), ("bbbb2222", 5)):   # bbbb is the newer session
            path = os.path.join(self.home, sid + ".sock")
            inbox = FakeInbox(path)
            inbox.start()
            self.inboxes[sid] = inbox
            with open(os.path.join(fb, "sessions", sid + ".json"), "w") as fh:
                json.dump({"session_id": sid, "node": "laptop-b", "socket": path,
                           "token": "tok-" + sid, "cwd": "/x",
                           "account": {"aaaa1111": "john@", "bbbb2222": "ai@"}[sid],
                           "registered_at": time.time() - age}, fh)
        self.port = free_port()
        env = dict(os.environ, HOME=self.home, FLEET_RELAY_PORT=str(self.port))
        self.proc = subprocess.Popen([sys.executable, os.path.join(RELAY, "agent.py")], env=env,
                                     stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/v1/health" % self.port, timeout=1)
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait()
        for inbox in self.inboxes.values():
            inbox.srv.close()
        shutil.rmtree(self.home)

    def post(self, payload, secret=None, ts=None):
        body = json.dumps(payload).encode()
        ts = str(int(time.time()) if ts is None else ts)
        sig = hmac.new(secret or self.secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        req = urllib.request.Request("http://127.0.0.1:%d/v1/message" % self.port, data=body,
                                     headers={"X-Fleet-Ts": ts, "X-Fleet-Sig": sig,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def wait_for(self, sid, n=1):
        for _ in range(50):
            if len(self.inboxes[sid].received) >= n:
                return
            time.sleep(0.05)

    def test_health_lists_live_sessions_newest_first(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/v1/health" % self.port) as r:
            info = json.load(r)
        self.assertEqual(info["sessions"], ["bbbb2222", "aaaa1111"])
        self.assertEqual(info["accounts"], {"ai@": 1, "john@": 1})

    def test_account_targets_the_newest_session_on_that_account(self):
        status, info = self.post({"text": "hi john", "from": "laptop-a", "account": "john@"})
        self.assertEqual((status, info["delivered_to"], info["account"]), (200, "aaaa1111", "john@"))
        self.wait_for("aaaa1111")
        self.assertEqual(len(self.inboxes["aaaa1111"].received), 1)
        self.assertEqual(self.inboxes["bbbb2222"].received, [])
        status, info = self.post({"text": "x", "from": "a", "account": "nobody@"})
        self.assertEqual((status, info["error"]), (404, "no live registered session on nobody@"))

    def test_signed_message_reaches_newest_session_with_auth_line(self):
        status, body = self.post({"text": "hello", "from": "laptop-a"})
        self.assertEqual(status, 200)
        self.assertEqual(body["delivered_to"], "bbbb2222")
        self.wait_for("bbbb2222")
        auth, msg = self.inboxes["bbbb2222"].received[0]
        self.assertEqual(auth, {"type": "auth", "token": "tok-bbbb2222"})
        self.assertEqual(msg["type"], "user")
        self.assertEqual(msg["message"]["role"], "user")
        self.assertIn("hello", msg["message"]["content"])
        self.assertIn("laptop-a", msg["message"]["content"])
        self.assertEqual(self.inboxes["aaaa1111"].received, [])

    def test_session_prefix_targets_one_session(self):
        status, body = self.post({"text": "just you", "from": "laptop-a", "session": "aaaa"})
        self.assertEqual((status, body["delivered_to"]), (200, "aaaa1111"))

    def test_bad_signature_is_refused_before_any_socket(self):
        status, _ = self.post({"text": "x", "from": "evil"}, secret=b"wrong")
        self.assertEqual(status, 401)
        time.sleep(0.2)
        self.assertEqual([i.received for i in self.inboxes.values()], [[], []])

    def test_replay_is_refused(self):
        payload, ts = {"text": "once", "from": "laptop-a"}, int(time.time())
        self.assertEqual(self.post(payload, ts=ts)[0], 200)
        status, body = self.post(payload, ts=ts)
        self.assertEqual((status, body["error"]), (401, "replayed message"))
        self.wait_for("bbbb2222")
        time.sleep(0.2)
        self.assertEqual(len(self.inboxes["bbbb2222"].received), 1)

    def test_local_notice_reaches_each_live_session_once(self):
        env = dict(os.environ, HOME=self.home)
        r = subprocess.run([sys.executable, os.path.join(RELAY, "send.py"), "--local", "--cwd", "/x",
                            "new rule"], capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        for sid in ("aaaa1111", "bbbb2222"):
            self.wait_for(sid)
            (auth, msg), = self.inboxes[sid].received
            self.assertEqual(auth["token"], "tok-" + sid)
            self.assertIn("[fleet-bridge notice from laptop-b]", msg["message"]["content"])
            self.assertNotIn("reply with", msg["message"]["content"])
        r = subprocess.run([sys.executable, os.path.join(RELAY, "send.py"), "--local", "--cwd", "/elsewhere",
                            "x"], capture_output=True, text=True, env=env)
        self.assertNotEqual(r.returncode, 0)

    def test_stale_timestamp_is_refused(self):
        status, _ = self.post({"text": "x"}, ts=int(time.time()) - 600)
        self.assertEqual(status, 401)

    def test_unknown_session_is_404(self):
        status, _ = self.post({"text": "x", "session": "zzzz"})
        self.assertEqual(status, 404)

    def test_dead_session_is_skipped(self):
        self.inboxes["bbbb2222"].srv.close()
        os.unlink(self.inboxes["bbbb2222"].path)
        status, body = self.post({"text": "x"})
        self.assertEqual((status, body["delivered_to"]), (200, "aaaa1111"))


class FakeHub(threading.Thread):
    """Caravan's GET /v1/route, checking the signature the way the hub does."""

    def __init__(self, secret, answer):
        super().__init__(daemon=True)
        self.calls, self.answer = [], answer
        hub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, obj):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                hub.calls.append(self.path)
                ts = self.headers.get("X-Caravan-Ts") or ""
                nonce = self.headers.get("X-Caravan-Nonce") or ""
                sig = self.headers.get("X-Caravan-Sig") or ""
                msg = b".".join([ts.encode(), nonce.encode(), b"GET", self.path.encode(), b""])
                expect = hmac.new(secret, msg, hashlib.sha256).hexdigest()
                if not ts.isdigit() or abs(time.time() - int(ts)) > 120 or not hmac.compare_digest(expect, sig):
                    return self._json(401, {"error": "bad signature"})
                self._json(200, hub.answer)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def run(self):
        self.server.serve_forever(poll_interval=0.1)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class RouteTest(unittest.TestCase):
    """delegate/route.py: the hub's verdict, the cache while the hub is away, then nothing
    (the caller's static rule), and an environment with exactly one credential."""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "delegate"))
        import route
        self.route = route
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.secret = b"x" * 40
        self.secret_file = os.path.join(self.home, "caravan-secret")
        with open(self.secret_file, "wb") as fh:
            fh.write(self.secret)
        os.chmod(self.secret_file, 0o600)
        self.hub = FakeHub(self.secret, {"account": "ai@", "reason": "ai@: headroom (highest 20%)",
                                         "ttl": 60, "headroom": {"ai@": 80, "john@": 60}})
        self.hub.start()
        self.addCleanup(self.hub.stop)
        self.env_file = os.path.join(self.home, "ai.env")
        with open(self.env_file, "w") as fh:
            fh.write("CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-test\n")
        os.chmod(self.env_file, 0o600)
        self.cfg = {"node": "laptop-a",
                    "route": {"hub": self.hub.url, "secret_file": self.secret_file, "timeout": 2},
                    "delegate": {"accounts": [
                        {"label": "john@", "config_dir": os.path.join(self.home, "claude-john"), "own": True},
                        {"label": "ai@", "env_file": self.env_file}]}}
        route.CACHE = os.path.join(self.home, "route-cache.json")
        route.LOG = os.path.join(self.home, "route.log")

    def test_hub_verdict_then_cache_then_nothing(self):
        v = self.route.decide("session", "fable", self.cfg, now=1000.0)
        self.assertEqual((v["label"], v["source"]), ("ai@", "hub"))
        self.assertEqual(v["account"]["env_file"], self.env_file)
        self.assertEqual(v["headroom"], {"ai@": 80, "john@": 60})
        self.assertIn("accounts=john%40%2Cai%40", self.hub.calls[0])
        self.assertIn("model=fable", self.hub.calls[0])
        self.assertIn("node=laptop-a", self.hub.calls[0])
        self.hub.stop()   # hub away: the cached answer stands in for 15 minutes
        v = self.route.decide("session", "fable", self.cfg, now=1600.0)
        self.assertEqual((v["label"], v["source"]), ("ai@", "cache"))
        self.assertIn("cached 10m ago", v["reason"])
        v = self.route.decide("agent", "fable", self.cfg, now=1600.0)   # another key: nothing cached
        self.assertEqual((v["label"], v["source"]), (None, None))
        v = self.route.decide("session", "fable", self.cfg, now=1901.0)
        self.assertEqual((v["label"], v["source"]), (None, None))
        self.assertIn("hub unreachable", v["reason"])
        with open(self.route.LOG) as fh:
            self.assertEqual(len(fh.read().splitlines()), 4)

    def test_a_refusal_or_an_unusable_secret_is_no_verdict(self):
        with open(self.secret_file, "wb") as fh:
            fh.write(b"y" * 40)
        v = self.route.decide("session", None, self.cfg)
        self.assertEqual(v["label"], None)
        self.assertIn("401", v["reason"])
        os.chmod(self.secret_file, 0o644)
        v = self.route.decide("session", None, self.cfg)
        self.assertEqual(v["label"], None)
        self.assertIn("readable by others", v["reason"])

    def test_apply_sets_exactly_one_credential(self):
        env = {"ANTHROPIC_API_KEY": "leak", "CLAUDE_CODE_OAUTH_TOKEN": "leak",
               "CLAUDE_CONFIG_DIR": "/elsewhere", "HOME": self.home}
        out = self.route.apply(dict(env), self.cfg["delegate"]["accounts"][0])
        self.assertEqual(out["CLAUDE_CONFIG_DIR"], os.path.join(self.home, "claude-john"))
        self.assertEqual(out["FLEET_ACCOUNT"], "john@")
        self.assertNotIn("ANTHROPIC_API_KEY", out)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", out)
        out = self.route.apply(dict(env), self.cfg["delegate"]["accounts"][1])
        self.assertEqual((out["CLAUDE_CODE_OAUTH_TOKEN"], out["FLEET_ACCOUNT"]), ("sk-ant-oat01-test", "ai@"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", out)
        os.chmod(self.env_file, 0o644)
        with self.assertRaises(self.route.RouteError):
            self.route.apply(dict(env), self.cfg["delegate"]["accounts"][1])

    def test_not_configured_means_no_verdict(self):
        v = self.route.decide("session", None, {"node": "x"})
        self.assertEqual((v["label"], v["reason"]), (None, "routing not configured"))
        self.assertEqual(self.route.model_from(["--model", "opus", "-p"]), "opus")
        self.assertEqual(self.route.model_from(["--model=fable"]), "fable")
        self.assertIsNone(self.route.model_from(["-p"]))


class AgentGuardTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.repo = os.path.join(self.home, "work", "repo")
        os.makedirs(os.path.join(self.repo, ".claude", "worktrees", "T-1"))
        os.makedirs(os.path.join(self.home, "work", "repo2"))
        os.makedirs(os.path.join(self.home, ".fleet-bridge"))
        with open(os.path.join(self.home, ".fleet-bridge", "config.json"), "w") as fh:
            json.dump({"delegate": {"account": "ops@example.com", "repos": [self.repo]}}, fh)

    def tearDown(self):
        shutil.rmtree(self.home)

    def guard(self, cwd, **extra):
        # A test run started from a delegate session inherits its marker, which silences the guard.
        env = {k: v for k, v in os.environ.items() if k not in ("FLEET_AGENT", "FLEET_DELEGATE_SESSION")}
        env.update(HOME=self.home, **extra)
        r = subprocess.run(["bash", os.path.join(ROOT, "delegate", "agent-guard.sh")],
                           input=json.dumps({"cwd": cwd}), capture_output=True, text=True, env=env)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_denies_in_repo_and_its_worktrees(self):
        for cwd in (self.repo, os.path.join(self.repo, ".claude", "worktrees", "T-1")):
            out = self.guard(cwd)
            self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny", cwd)
            self.assertIn("ops@example.com", out["hookSpecificOutput"]["permissionDecisionReason"])

    def test_silent_outside_and_for_similar_names(self):
        self.assertIsNone(self.guard(os.path.join(self.home, "work")))
        self.assertIsNone(self.guard(os.path.join(self.home, "work", "repo2")))

    def test_silent_inside_a_fleet_agent_run(self):
        self.assertIsNone(self.guard(self.repo, FLEET_AGENT="1"))

    def test_silent_in_a_session_already_on_the_delegate_account(self):
        self.assertIsNone(self.guard(self.repo, FLEET_DELEGATE_SESSION="1"))


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys
task = sys.stdin.read()
json.dump({"argv": sys.argv[1:], "task": task,
           "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
           "leaked": [k for k in ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_MESSAGING_SOCKET")
                      if k in os.environ],
           "fleet_agent": os.environ.get("FLEET_AGENT")},
          open(os.environ["FAKE_CLAUDE_LOG"], "w"))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "did: " + task.strip(), "num_turns": 2}))
'''


class FleetAgentTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.bin = os.path.join(self.home, "bin")
        os.makedirs(self.bin)
        os.makedirs(os.path.join(self.home, ".fleet-bridge"))
        with open(os.path.join(self.bin, "claude"), "w") as fh:
            fh.write(FAKE_CLAUDE)
        os.chmod(os.path.join(self.bin, "claude"), 0o755)
        self.env_file = os.path.join(self.home, ".fleet-bridge", "delegate.env")
        fd = os.open(self.env_file, os.O_WRONLY | os.O_CREAT, 0o600)
        os.write(fd, b"CLAUDE_CODE_OAUTH_TOKEN=delegate-token\n")
        os.close(fd)
        with open(os.path.join(self.home, ".fleet-bridge", "config.json"), "w") as fh:
            json.dump({"delegate": {"account": "ops@example.com", "env_file": self.env_file}}, fh)
        self.log = os.path.join(self.home, "claude.json")

    def tearDown(self):
        shutil.rmtree(self.home)

    def run_agent(self, *args, **extra):
        env = dict(os.environ, HOME=self.home, PATH=self.bin + os.pathsep + os.environ["PATH"],
                   FAKE_CLAUDE_LOG=self.log, ANTHROPIC_API_KEY="must-not-leak", CLAUDECODE="1",
                   CLAUDE_CODE_MESSAGING_SOCKET="/must/not/leak", CLAUDE_CODE_OAUTH_TOKEN="caller-token")
        env.pop("FLEET_AGENT", None)
        env.update(extra)
        return subprocess.run([sys.executable, os.path.join(ROOT, "delegate", "fleet-agent.py"), *args],
                              capture_output=True, text=True, env=env, cwd=self.home)

    def seen(self):
        with open(self.log) as fh:
            return json.load(fh)

    def test_runs_on_delegate_token_with_a_clean_environment(self):
        r = self.run_agent("count the files")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "did: count the files")
        self.assertIn("ops@example.com", r.stderr)
        seen = self.seen()
        self.assertEqual(seen["token"], "delegate-token")
        self.assertEqual(seen["leaked"], [])
        self.assertEqual(seen["fleet_agent"], "1")

    def test_read_only_by_default(self):
        self.run_agent("look")
        argv = self.seen()["argv"]
        self.assertIn("--disallowedTools", argv)
        self.assertNotIn("acceptEdits", argv)
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        self.assertNotIn("Bash", allowed)          # only specific read-only Bash patterns
        self.assertNotIn("Edit", allowed)

    def test_write_mode(self):
        self.run_agent("--write", "fix it")
        argv = self.seen()["argv"]
        self.assertIn("acceptEdits", argv)
        self.assertIn("Edit", argv)
        self.assertNotIn("--disallowedTools", argv)

    def test_refuses_recursion(self):
        r = self.run_agent("x", FLEET_AGENT="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.log))

    def test_refuses_a_token_file_others_can_read(self):
        os.chmod(self.env_file, 0o644)
        r = self.run_agent("x")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("chmod 600", r.stderr)
        self.assertFalse(os.path.exists(self.log))


class OwnershipGuardTest(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp()
        git = lambda *a: subprocess.run(["git", "-C", self.repo, *a], check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "t")
        shutil.copy(os.path.join(ROOT, "workspace", "OWNERSHIP.toml.example"),
                    os.path.join(self.repo, "OWNERSHIP.toml"))
        hook = os.path.join(self.repo, ".git", "hooks", "pre-commit")
        os.symlink(os.path.join(ROOT, "workspace", "bin", "ownership-guard.sh"), hook)

    def tearDown(self):
        shutil.rmtree(self.repo)

    def commit(self, path, node):
        full = os.path.join(self.repo, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w") as fh:
            fh.write("x\n")
        subprocess.run(["git", "-C", self.repo, "add", path], check=True)
        return subprocess.run(["git", "-C", self.repo, "commit", "-qm", "t"],
                              capture_output=True, text=True, env=dict(os.environ, FLEET_NODE=node))

    def test_refuses_a_path_owned_by_another_node(self):
        r = self.commit("src/core/a.py", "laptop-b")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("owned by laptop-a", r.stdout + r.stderr)

    def test_allows_own_and_open_paths(self):
        self.assertEqual(self.commit("src/core/a.py", "laptop-a").returncode, 0)
        self.assertEqual(self.commit("docs/notes.md", "laptop-b").returncode, 0)


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env=dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
                            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com"))


class StatusEndpointTest(unittest.TestCase):
    """GET /v1/status from a real relay process: the numbers are right and nothing secret leaks."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        fb = os.path.join(self.home, ".fleet-bridge")
        os.makedirs(os.path.join(fb, "sessions"))
        with open(os.path.join(fb, "secret"), "w") as fh:
            fh.write("s3cret-never-shown")
        with open(os.path.join(fb, "config.json"), "w") as fh:
            json.dump({"node": "laptop-b", "envelope": "sdk_user", "status": {"units": []},
                       "delegate": {"account": "ops@example.com"}}, fh)
        # a repo and a worktree of it: both must count as "alpha"
        self.repo = os.path.join(self.home, "work", "alpha")
        os.makedirs(self.repo)
        git("init", "-q", "-b", "main", cwd=self.repo)
        git("commit", "-q", "--allow-empty", "-m", "x", cwd=self.repo)
        wt = os.path.join(self.repo, ".claude", "worktrees", "T-1")
        git("worktree", "add", "-q", wt, cwd=self.repo)
        now = time.time()
        regs = [("s1", self.repo, "a.sock", now - 5), ("s2", wt, "b.sock", now - 4),
                ("s0", self.repo, "a.sock", now - 50)]   # an older registration of the same socket
        for sid, cwd, sock, t in regs:
            path = os.path.join(self.home, sock)
            open(path, "a").close()
            with open(os.path.join(fb, "sessions", sid + ".json"), "w") as fh:
                json.dump({"session_id": sid, "socket": path, "token": "tok-never-shown-" + sid,
                           "cwd": cwd, "registered_at": t}, fh)
        with open(os.path.join(fb, "delegate.log"), "w") as fh:
            # appended as each run finishes; t is when it started
            fh.write(json.dumps({"t": now - 90000, "cwd": self.repo, "ok": True, "task": "old"}) + "\n")
            fh.write(json.dumps({"t": now - 120, "cwd": wt, "write": False, "ok": True, "secs": 30,
                                 "task": "TOP-SECRET-PROMPT one"}) + "\n")
            fh.write(json.dumps({"t": now - 60, "cwd": self.repo, "write": True, "ok": False, "secs": 9,
                                 "task": "TOP-SECRET-PROMPT two"}) + "\n")
        self.sysfs = os.path.join(self.home, "sysfs")
        for name, files in (("ADP1", {"type": "Mains", "online": "0"}),
                            ("BAT0", {"type": "Battery", "capacity": "8", "status": "Discharging"})):
            os.makedirs(os.path.join(self.sysfs, name))
            for k, v in files.items():
                with open(os.path.join(self.sysfs, name, k), "w") as fh:
                    fh.write(v + "\n")
        self.port = free_port()
        env = dict(os.environ, HOME=self.home, FLEET_RELAY_PORT=str(self.port), FLEET_SYSFS=self.sysfs)
        self.proc = subprocess.Popen([sys.executable, os.path.join(RELAY, "agent.py")], env=env,
                                     stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/v1/health" % self.port, timeout=1)
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.1)

    def tearDown(self):
        self.proc.terminate()
        self.proc.wait()
        shutil.rmtree(self.home)

    def test_status(self):
        with urllib.request.urlopen("http://127.0.0.1:%d/v1/status" % self.port, timeout=10) as r:
            raw = r.read().decode()
        st = json.loads(raw)
        self.assertEqual(st["node"], "laptop-b")
        self.assertEqual(st["sessions"], {"total": 2, "by_repo": {"alpha": 2}, "by_account": {"?": 2}})
        self.assertEqual(st["power"], {"ac": False, "battery": 8, "status": "Discharging", "on_battery": True})
        d = st["delegate"]
        self.assertEqual((d["account"], d["runs_1h"], d["runs_24h"], d["failed_24h"]), ("ops@example.com", 2, 2, 1))
        self.assertEqual((d["last"]["repo"], d["last"]["ok"], d["last"]["write"]), ("alpha", False, True))
        self.assertEqual(st["accounts"], [])
        self.assertIn("per_core", st["load"])
        # the hub dashboard's host strip and routing line
        self.assertEqual(set(st["host"]), {"cpu_temp_c", "disk_used_pct", "uptime_s", "updates", "reboot_required"})
        self.assertIsInstance(st["host"]["disk_used_pct"], int)
        self.assertIsInstance(st["host"]["uptime_s"], int)
        self.assertEqual(d["config"], {"session_repos": [], "failover": False,
                                       "routed": False, "accounts": []})
        for leak in ("s3cret-never-shown", "tok-never-shown", "TOP-SECRET-PROMPT"):
            self.assertNotIn(leak, raw)


class FakeAnthropic(threading.Thread):
    """Stands in for the usage API and the messages endpoint's rate-limit headers."""

    def __init__(self):
        super().__init__(daemon=True)
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        outer = self
        self.hits = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def reply(self, code, body=b"{}", headers=()):
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.hits.append((self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer good":
                    return self.reply(401)
                self.reply(200, json.dumps({"five_hour": {"utilization": 2.0}, "limits": [
                    {"kind": "session", "group": "session", "percent": 2, "severity": "normal",
                     "resets_at": "2026-09-25T18:40:00Z", "scope": None, "is_active": False},
                    {"kind": "weekly_scoped", "group": "weekly", "percent": 100, "severity": "critical",
                     "resets_at": "2026-09-27T03:00:00Z", "is_active": True,
                     "scope": {"model": {"display_name": "Fable"}}}]}).encode())

            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.hits.append((self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer good":
                    return self.reply(401)
                pre = "anthropic-ratelimit-unified-"
                self.reply(200, headers=[(pre + "5h-utilization", "0.02"), (pre + "5h-reset", "1790359800"),
                                         (pre + "5h-status", "allowed"), (pre + "7d-utilization", "0.95"),
                                         (pre + "7d-reset", "1790553600"), (pre + "7d-status", "allowed_warning"),
                                         (pre + "status", "allowed_warning")])

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def run(self):
        self.srv.serve_forever()


class UsageSamplerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, RELAY)
        import nodestatus
        cls.ns = nodestatus
        cls.api = FakeAnthropic()
        cls.api.start()
        os.environ["FLEET_USAGE_API"] = cls.api.url

    @classmethod
    def tearDownClass(cls):
        cls.api.srv.shutdown()
        os.environ.pop("FLEET_USAGE_API", None)

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.api.hits.clear()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def creds(self, token, expires_in):
        path = os.path.join(self.dir, "creds.json")
        with open(path, "w") as fh:
            json.dump({"claudeAiOauth": {"accessToken": token, "subscriptionType": "max",
                                         "expiresAt": int((time.time() + expires_in) * 1000)}}, fh)
        return path

    def env_file(self, token):
        path = os.path.join(self.dir, "t.env")
        with open(path, "w") as fh:
            fh.write("CLAUDE_CODE_OAUTH_TOKEN=%s\n" % token)
        return path

    def test_login_source_reads_every_limit_including_per_model(self):
        got = self.ns.sample_login(self.creds("good", 3600))
        self.assertEqual((got["state"], got["plan"]), ("ok", "max"))
        fable = [l for l in got["limits"] if l["model"] == "Fable"][0]
        self.assertEqual((fable["percent"], fable["severity"], fable["active"]), (100, "critical", True))
        self.assertEqual(self.api.hits, [("/api/oauth/usage", "Bearer good")])

    def test_an_expired_login_is_reported_without_calling_out(self):
        got = self.ns.sample_login(self.creds("good", -60))
        self.assertEqual(got["state"], "login-expired")
        self.assertEqual(self.api.hits, [])

    def test_a_refused_login_is_reported_as_expired(self):
        self.assertEqual(self.ns.sample_login(self.creds("stale", 3600))["state"], "login-expired")

    def test_header_source(self):
        got = self.ns.sample_headers(self.env_file("good"))
        self.assertEqual((got["state"], got["status"]), ("ok", "allowed_warning"))
        by = {l["name"]: l for l in got["limits"]}
        self.assertEqual((by["session"]["percent"], by["session"]["severity"]), (2, "normal"))
        self.assertEqual((by["weekly_all"]["percent"], by["weekly_all"]["severity"]), (95, "critical"))
        self.assertEqual(by["weekly_all"]["resets_at"], "2026-09-28T00:00:00Z")

    def test_a_failed_sample_keeps_the_last_good_numbers(self):
        path = self.env_file("good")
        s = self.ns.Sampler([{"label": "ops@example.com", "source": "token-headers", "env_file": path}])
        s.sample(s.accounts[0])
        first = s.snapshot()[0]
        self.env_file("revoked")
        s.sample(s.accounts[0])
        second = s.snapshot()[0]
        self.assertEqual(second["state"], "token-invalid")
        self.assertEqual((second["limits"], second["as_of"]), (first["limits"], first["as_of"]))

    def test_no_power_supply_means_no_power_block(self):
        self.assertIsNone(self.ns.power(self.dir))


SHOW_ENV = r"""#!/usr/bin/env python3
import json, os, sys
json.dump({"argv": sys.argv[1:], "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
           "api_key": os.environ.get("ANTHROPIC_API_KEY"),
           "marker": os.environ.get("FLEET_DELEGATE_SESSION")}, sys.stdout)
"""


class DelegateSessionTest(unittest.TestCase):
    """delegate-session, run the way an IDE extension runs its process wrapper."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.repo = os.path.join(self.home, "work", "repo")
        os.makedirs(os.path.join(self.repo, ".claude", "worktrees", "T-1"))
        os.makedirs(os.path.join(self.home, "work", "repo2"))
        fb = os.path.join(self.home, ".fleet-bridge")
        os.makedirs(fb)
        self.env_file = os.path.join(fb, "delegate.env")
        with open(self.env_file, "w") as fh:
            fh.write("CLAUDE_CODE_OAUTH_TOKEN=tok-delegate\n")
        os.chmod(self.env_file, 0o600)
        with open(os.path.join(fb, "config.json"), "w") as fh:
            json.dump({"delegate": {"account": "ops@example.com", "env_file": self.env_file,
                                    "repos": [self.repo], "session_repos": [self.repo]}}, fh)
        self.claude = os.path.join(self.home, "claude")
        with open(self.claude, "w") as fh:
            fh.write(SHOW_ENV)
        os.chmod(self.claude, 0o755)

    def tearDown(self):
        shutil.rmtree(self.home)

    def launch(self, cwd):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CLAUDE_CODE_OAUTH_TOKEN", "FLEET_DELEGATE_SESSION", "FLEET_CONFIG")}
        env.update(HOME=self.home, ANTHROPIC_API_KEY="sk-machine")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "delegate", "delegate-session.py"),
                            self.claude, "--output-format", "stream-json", "--dangerously-skip-permissions"],
                           cwd=cwd, capture_output=True, text=True, env=env)
        return r.returncode, (json.loads(r.stdout) if r.stdout.strip() else None), r.stderr

    def test_in_a_listed_repo_and_its_worktrees_the_session_gets_the_delegate_token(self):
        for cwd in (self.repo, os.path.join(self.repo, ".claude", "worktrees", "T-1")):
            rc, out, _ = self.launch(cwd)
            self.assertEqual(rc, 0, cwd)
            self.assertEqual((out["token"], out["marker"], out["api_key"]), ("tok-delegate", "1", None), cwd)
            self.assertEqual(out["argv"], ["--output-format", "stream-json", "--dangerously-skip-permissions"])

    def test_elsewhere_the_session_starts_unchanged(self):
        for cwd in (os.path.join(self.home, "work"), os.path.join(self.home, "work", "repo2")):
            rc, out, _ = self.launch(cwd)
            self.assertEqual((rc, out["token"], out["marker"], out["api_key"]), (0, None, None, "sk-machine"), cwd)

    def test_a_token_file_readable_by_others_stops_the_session(self):
        os.chmod(self.env_file, 0o644)
        rc, out, err = self.launch(self.repo)
        self.assertNotEqual(rc, 0)
        self.assertIsNone(out)
        self.assertIn("chmod 600", err)

    def test_a_missing_token_stops_the_session_instead_of_using_the_machine_login(self):
        with open(self.env_file, "w") as fh:
            fh.write("# emptied\n")
        rc, out, err = self.launch(self.repo)
        self.assertNotEqual(rc, 0)
        self.assertIsNone(out)
        self.assertIn("no CLAUDE_CODE_OAUTH_TOKEN", err)

    def test_repos_without_session_opt_in_keep_the_machine_login(self):
        cfg = os.path.join(self.home, ".fleet-bridge", "config.json")
        with open(cfg) as fh:
            d = json.load(fh)
        del d["delegate"]["session_repos"]
        with open(cfg, "w") as fh:
            json.dump(d, fh)
        rc, out, _ = self.launch(self.repo)
        self.assertEqual((rc, out["token"], out["api_key"]), (0, None, "sk-machine"))

    def set_failover(self, mode):
        env = {k: v for k, v in os.environ.items() if k != "FLEET_CONFIG"}
        env["HOME"] = self.home
        return subprocess.run([sys.executable, os.path.join(ROOT, "delegate", "delegate-session.py"),
                               "--failover", mode], capture_output=True, text=True, env=env)

    def test_with_failover_on_every_folder_gets_the_delegate_token_even_new_ones(self):
        self.assertIn("failover on", self.set_failover("on").stdout)
        made_later = os.path.join(self.home, "made-after-failover", "project")
        os.makedirs(made_later)
        for cwd in (self.home, os.path.join(self.home, "work", "repo2"), made_later, self.repo):
            rc, out, _ = self.launch(cwd)
            self.assertEqual((rc, out["token"], out["marker"], out["api_key"]), (0, "tok-delegate", "1", None), cwd)
        cfg = os.path.join(self.home, ".fleet-bridge", "config.json")
        self.assertEqual(os.stat(cfg).st_mode & 0o777, 0o600)
        with open(cfg) as fh:
            d = json.load(fh)["delegate"]
        self.assertEqual((d["failover"], d["session_repos"], d["account"]), (True, [self.repo], "ops@example.com"))

    def test_failover_off_goes_back_to_the_listed_repos(self):
        self.set_failover("on")
        self.assertIn("failover off", self.set_failover("off").stdout)
        rc, out, _ = self.launch(os.path.join(self.home, "work", "repo2"))
        self.assertEqual((rc, out["token"], out["api_key"]), (0, None, "sk-machine"))
        rc, out, _ = self.launch(self.repo)
        self.assertEqual(out["token"], "tok-delegate")
        self.assertIn("failover off", self.set_failover("status").stdout)

    def test_failover_still_fails_closed(self):
        self.set_failover("on")
        os.chmod(self.env_file, 0o644)
        rc, out, err = self.launch(self.home)
        self.assertNotEqual(rc, 0)
        self.assertIsNone(out)
        self.assertIn("chmod 600", err)


if __name__ == "__main__":
    unittest.main()
