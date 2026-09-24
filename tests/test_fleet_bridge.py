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
            self.assertEqual(json.load(r)["sessions"], ["bbbb2222", "aaaa1111"])

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
        env = {k: v for k, v in os.environ.items() if k != "FLEET_AGENT"}
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
        return json.load(open(self.log))

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


if __name__ == "__main__":
    unittest.main()
