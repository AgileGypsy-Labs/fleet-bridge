"""fleet-agent: delegated `claude -p` runs with a PATH-stubbed claude."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fa = load("fb_fleet_agent", "delegate/fleet-agent.py")

CLAUDE_OK = """#!/usr/bin/env python3
import json, os, sys
task = sys.stdin.read()
with open(os.environ["STUB_RECORD"], "w") as fh:
    json.dump({"argv": sys.argv[1:], "task": task,
               "env": {k: os.environ.get(k) for k in
                       ("CLAUDE_CODE_OAUTH_TOKEN", "FLEET_AGENT", "CLAUDECODE",
                        "ANTHROPIC_API_KEY", "CLAUDE_CODE_MESSAGING_SOCKET")}}, fh)
print(json.dumps({"result": "the answer is 4", "is_error": False, "num_turns": 2,
                  "subtype": "success"}))
"""

CLAUDE_FAILS = """#!/usr/bin/env python3
import sys
sys.stderr.write("exploded spectacularly\\n")
sys.exit(2)
"""

CLAUDE_SLOW = """#!/usr/bin/env python3
import time
time.sleep(10)
"""


def run_main(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["fleet-agent"] + argv), \
         mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fa.main()
        except SystemExit as exc:
            raise
    return rc, out.getvalue(), err.getvalue()


class FleetAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb-agent-run-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.record = os.path.join(self.tmp, "record.json")
        self.env_file = os.path.join(self.tmp, "delegate.env")
        with open(self.env_file, "w") as fh:
            fh.write("CLAUDE_CODE_OAUTH_TOKEN=delegate-token-1\n")
        os.chmod(self.env_file, 0o600)
        self.config = os.path.join(self.tmp, "config.json")
        with open(self.config, "w") as fh:
            json.dump({"delegate": {"account": "ops@", "env_file": self.env_file}}, fh)
        for attr, val in (("CONFIG", self.config), ("LOG", os.path.join(self.tmp, "delegate.log"))):
            p = mock.patch.object(fa, attr, val)
            p.start()
            self.addCleanup(p.stop)
        env = mock.patch.dict(os.environ, {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "STUB_RECORD": self.record, "CLAUDECODE": "1",
            "ANTHROPIC_API_KEY": "parent-key-must-not-leak",
            "CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/parent.sock"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("FLEET_AGENT", None)
        # hermetic by default: the real machine may have live routing configured
        p = mock.patch.object(fa.route, "configured", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def claude(self, body):
        path = os.path.join(self.bin, "claude")
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, 0o755)

    def recorded(self):
        return json.load(open(self.record))

    def test_read_only_run_strips_env_and_logs(self):
        self.claude(CLAUDE_OK)
        rc, out, err = run_main(["--cwd", self.tmp, "what is 2+2"])
        self.assertEqual(rc, 0)
        self.assertIn("the answer is 4", out)
        self.assertIn("read-only", err)
        self.assertIn("ops@", err)
        rec = self.recorded()
        self.assertIn("--disallowedTools", rec["argv"])
        self.assertIn("Bash(git log:*)", rec["argv"])
        self.assertEqual(rec["task"], "what is 2+2")
        self.assertEqual(rec["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "delegate-token-1")
        self.assertEqual(rec["env"]["FLEET_AGENT"], "1")
        self.assertIsNone(rec["env"]["CLAUDECODE"])
        self.assertIsNone(rec["env"]["ANTHROPIC_API_KEY"])
        self.assertIsNone(rec["env"]["CLAUDE_CODE_MESSAGING_SOCKET"])
        log = [json.loads(l) for l in open(fa.LOG)]
        self.assertTrue(log[-1]["ok"])

    def test_write_mode_model_agent_and_stdin_task(self):
        self.claude(CLAUDE_OK)
        rc, out, _ = run_main(["--write", "--cwd", self.tmp, "--model", "haiku",
                               "--agent", "explorer", "-"], stdin="task from stdin")
        self.assertEqual(rc, 0)
        rec = self.recorded()
        self.assertIn("acceptEdits", rec["argv"])
        self.assertIn("haiku", rec["argv"])
        self.assertIn("explorer", rec["argv"])
        self.assertEqual(rec["task"], "task from stdin")

    def test_routing_chooses_account_and_route_error_exits(self):
        self.claude(CLAUDE_OK)
        chosen = {"label": "john@"}
        with mock.patch.object(fa.route, "configured", return_value=True), \
             mock.patch.object(fa.route, "decide", return_value={"account": chosen}), \
             mock.patch.object(fa.route, "apply") as applied:
            rc, _, err = run_main(["--cwd", self.tmp, "go"])
        self.assertEqual(rc, 0)
        self.assertIn("john@", err)
        applied.assert_called_once()
        with mock.patch.object(fa.route, "configured", return_value=True), \
             mock.patch.object(fa.route, "decide", return_value={"account": chosen}), \
             mock.patch.object(fa.route, "apply", side_effect=fa.route.RouteError("no token")):
            with self.assertRaises(SystemExit) as cm:
                run_main(["--cwd", self.tmp, "go"])
        self.assertIn("no token", str(cm.exception.code))

    def test_recursion_guard_and_empty_task(self):
        self.claude(CLAUDE_OK)
        with mock.patch.dict(os.environ, {"FLEET_AGENT": "1"}):
            with self.assertRaises(SystemExit) as cm:
                run_main(["--cwd", self.tmp, "go"])
        self.assertIn("already inside", str(cm.exception.code))
        with self.assertRaises(SystemExit) as cm:
            run_main(["--cwd", self.tmp, "   "])
        self.assertIn("empty task", str(cm.exception.code))

    def test_token_guards(self):
        os.chmod(self.env_file, 0o644)
        with self.assertRaises(SystemExit) as cm:
            fa.token(self.env_file)
        self.assertIn("chmod 600", str(cm.exception.code))
        os.chmod(self.env_file, 0o600)
        empty = os.path.join(self.tmp, "empty.env")
        with open(empty, "w") as fh:
            fh.write("OTHER=1\n")
        os.chmod(empty, 0o600)
        with self.assertRaises(SystemExit):
            fa.token(empty)
        with self.assertRaises(SystemExit):
            fa.token(os.path.join(self.tmp, "missing.env"))

    def test_settings_fallback_on_broken_config(self):
        with open(self.config, "w") as fh:
            fh.write("not json")
        account, env_file = fa.settings()
        self.assertEqual(account, "delegate account")
        self.assertTrue(env_file.endswith("delegate.env"))

    def test_timeout_kills_the_group(self):
        self.claude(CLAUDE_SLOW)
        rc, out, err = run_main(["--cwd", self.tmp, "--minutes", "0.02", "slow task"])
        self.assertEqual(rc, 1)
        self.assertIn("stopped after", out)
        log = [json.loads(l) for l in open(fa.LOG)]
        self.assertFalse(log[-1]["ok"])

    def test_claude_dies_without_result(self):
        self.claude(CLAUDE_FAILS)
        rc, out, err = run_main(["--cwd", self.tmp, "doomed"])
        self.assertEqual(rc, 1)
        self.assertIn("without a result", out)
        self.assertIn("exploded", out)


if __name__ == "__main__":
    unittest.main()
