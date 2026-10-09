"""live-session.py against a PATH-stubbed TUI claude in a real pty."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ls = load("fb_live_session", "relay/live-session.py")

# A TUI that walks the real sequence: prompt -> Enter -> DA1 query -> register -> idle.
TUI_HAPPY = r'''#!/usr/bin/env python3
import json, os, sys, time
reg = os.environ["STUB_REG"]
rec = os.environ["STUB_RECORD"]
with open(rec, "w") as fh:
    json.dump({"argv": sys.argv[1:], "token": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"),
               "no_parent_inbox": "CLAUDE_CODE_MESSAGING_SOCKET" not in os.environ}, fh)
print("Welcome. Press Enter to continue", flush=True)
# wait for the Enter that live-session sends
data = os.read(0, 10)
sys.stdout.write("\x1b[c")        # who are you? (DA1)
sys.stdout.flush()
time.sleep(0.3)                   # live-session answers DA1; never block reading it (canonical pty)
sock = os.path.join(os.path.dirname(reg), "fake.sock")
open(sock, "w").close()
with open(os.path.join(reg, "live.json"), "w") as fh:
    json.dump({"session_id": "live-0001", "socket": sock, "registered_at": time.time()}, fh)
print("ready and idle", flush=True)
time.sleep(30)
'''

TUI_ONBOARDING = '''#!/usr/bin/env python3
import time
print("Select login method", flush=True)
time.sleep(30)
'''

TUI_TRUST = '''#!/usr/bin/env python3
import time
print("Do you trust this folder?", flush=True)
time.sleep(30)
'''

TUI_EXITS = '''#!/usr/bin/env python3
print("goodbye")
'''


class LiveSessionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb-live-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.reg = os.path.join(self.tmp, "sessions")
        os.makedirs(self.reg)
        self.bin = os.path.join(self.tmp, "lbin")
        os.makedirs(os.path.join(self.bin))
        os.makedirs(os.path.join(self.tmp, ".local", "bin"))
        self.record = os.path.join(self.tmp, "record.json")
        for attr, val in (("HOME", self.tmp), ("REG", self.reg),
                          ("LOG", os.path.join(self.tmp, "live.log")),
                          ("SEND", os.path.join(self.tmp, "send.py"))):
            p = mock.patch.object(ls, attr, val)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {"PATH": self.bin + ":/usr/bin:/bin",
                                           "STUB_REG": self.reg, "STUB_RECORD": self.record})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)

    def claude(self, body):
        path = os.path.join(self.bin, "claude")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def run_ls(self, argv):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["live-session.py"] + argv), \
             contextlib.redirect_stdout(out):
            rc = ls.main()
        return rc, out.getvalue()

    def test_ready_flow_with_env_file_and_allow_send(self):
        self.claude(TUI_HAPPY)
        env_file = os.path.join(self.tmp, "ai.env")
        with open(env_file, "w") as fh:
            fh.write("CLAUDE_CODE_OAUTH_TOKEN=live-token-9\n")
        rc, out = self.run_ls(["--cwd", self.tmp, "--seconds", "8", "--allow-send",
                               "--env-file", env_file])
        self.assertEqual(rc, 0, out)
        self.assertIn("READY live-0001", out)
        rec = json.load(open(self.record))
        self.assertEqual(rec["token"], "live-token-9")
        self.assertTrue(rec["no_parent_inbox"])
        self.assertIn("--allowedTools", rec["argv"])

    def test_onboarding_and_trust_stop_with_instructions(self):
        self.claude(TUI_ONBOARDING)
        rc, out = self.run_ls(["--cwd", self.tmp, "--seconds", "8"])
        self.assertEqual(rc, 1)
        self.assertIn("NEEDS ONBOARDING", out)
        self.claude(TUI_TRUST)
        rc, out = self.run_ls(["--cwd", self.tmp, "--seconds", "8"])
        self.assertEqual(rc, 1)
        self.assertIn("NEEDS TRUST", out)

    def test_immediate_exit_is_reported(self):
        self.claude(TUI_EXITS)
        rc, out = self.run_ls(["--cwd", self.tmp, "--seconds", "4"])
        self.assertEqual(rc, 1)
        self.assertIn("EXITED", out)
        self.assertIn("never READY", out)

    def test_not_ready_deadline(self):
        self.claude('#!/usr/bin/env python3\nimport time\nprint("quiet")\ntime.sleep(30)\n')
        rc, out = self.run_ls(["--cwd", self.tmp, "--seconds", "2"])
        self.assertEqual(rc, 1)
        self.assertIn("NOT READY", out)

    def test_claude_missing(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_ls(["--seconds", "1"])
        self.assertIn("claude not found", str(cm.exception.code))

    def test_registered_since_ignores_stale_and_broken(self):
        old = os.path.join(self.reg, "old.json")
        with open(old, "w") as fh:
            json.dump({"session_id": "old", "socket": "/nonexistent"}, fh)
        os.utime(old, (1, 1))
        with open(os.path.join(self.reg, "junk.json"), "w") as fh:
            fh.write("junk")
        os.utime(os.path.join(self.reg, "junk.json"), (time.time(), time.time()))
        self.assertIsNone(ls.registered_since(10))


if __name__ == "__main__":
    unittest.main()
