"""The remaining branches: delegate-session, route.py edges, nodestatus samplers, proxy internals."""
import contextlib
import http.client
import importlib.util
import io
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
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ds = load("fb_delegate_session", "delegate/delegate-session.py")
rt = sys.modules["route"]                       # the instance every delegate tool imports
ns = load("fb_nodestatus", "relay/nodestatus.py")
qp = load("fb_quota_proxy_left", "proxy/quota-proxy.py")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fb-left-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def path(self, name, text=None, mode=None):
        p = os.path.join(self.tmp, name)
        if text is not None:
            with open(p, "w") as fh:
                fh.write(text)
        if mode is not None:
            os.chmod(p, mode)
        return p

    def patch(self, obj, attr, val):
        p = mock.patch.object(obj, attr, val)
        p.start()
        self.addCleanup(p.stop)


# ── delegate-session ────────────────────────────────────────────────────────────────────────

class DelegateSessionTest(Tmp):
    def setUp(self):
        super().setUp()
        self.config = self.path("config.json")
        self.patch(ds, "CONFIG", self.config)
        self.patch(rt, "configured", lambda cfg=None: False)   # hermetic unless a test routes
        self.env_file = self.path("delegate.env", "CLAUDE_CODE_OAUTH_TOKEN=dlg-token\n", 0o600)
        self.exec_calls = []
        self.patch(ds.os, "execvp", lambda f, argv: self.exec_calls.append((f, argv)))

    def cfg(self, **delegate):
        with open(self.config, "w") as fh:
            json.dump({"delegate": delegate}, fh)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "argv", ["delegate-session"] + argv), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = ds.main()
        return rc, out.getvalue()

    def test_env_file_for_rules(self):
        self.assertIsNone(ds.env_file_for(self.tmp))                 # no config at all
        self.cfg(env_file=self.env_file, failover=True)
        self.assertEqual(ds.env_file_for(self.tmp), self.env_file)
        self.cfg(env_file=self.env_file, session_repos=[self.tmp])
        sub = os.path.join(self.tmp, "deeper")
        os.makedirs(sub)
        self.assertEqual(ds.env_file_for(sub), self.env_file)
        self.assertIsNone(ds.env_file_for("/"))

    def test_failover_switch_and_status(self):
        self.cfg(account="ops@")
        rc, out = self.run_main(["--failover", "on"])
        self.assertIn("failover on", out)
        self.assertTrue(json.load(open(self.config))["delegate"]["failover"])
        rc, out = self.run_main(["--failover", "off"])
        self.assertIn("failover off", out)
        rc, out = self.run_main(["--failover"])                      # status
        self.assertIn("failover off", out)
        with self.assertRaises(SystemExit):
            self.run_main(["--failover", "sideways"])
        os.unlink(self.config)
        with self.assertRaises(SystemExit):
            self.run_main(["--failover", "status"])

    def test_usage_without_command(self):
        with self.assertRaises(SystemExit):
            self.run_main([])

    def test_delegated_exec_sets_token_and_marker(self):
        self.cfg(env_file=self.env_file, failover=True)
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "must-go"}):
            self.run_main(["claude", "--model", "haiku"])
            self.assertEqual(os.environ["CLAUDE_CODE_OAUTH_TOKEN"], "dlg-token")
            self.assertEqual(os.environ["FLEET_DELEGATE_SESSION"], "1")
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        self.assertEqual(self.exec_calls[-1], ("claude", ["claude", "--model", "haiku"]))
        os.environ.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        os.environ.pop("FLEET_DELEGATE_SESSION", None)

    def test_plain_exec_outside_session_repos(self):
        self.cfg(env_file=self.env_file, session_repos=["/nowhere/special"])
        self.run_main(["claude"])
        self.assertEqual(self.exec_calls[-1], ("claude", ["claude"]))

    def test_token_file_guards(self):
        loose = self.path("loose.env", "CLAUDE_CODE_OAUTH_TOKEN=x\n", 0o644)
        self.cfg(env_file=loose, failover=True)
        with self.assertRaises(SystemExit) as cm:
            self.run_main(["claude"])
        self.assertIn("chmod 600", str(cm.exception.code))
        empty = self.path("empty.env", "OTHER=1\n", 0o600)
        self.cfg(env_file=empty, failover=True)
        with self.assertRaises(SystemExit) as cm:
            self.run_main(["claude"])
        self.assertIn("no CLAUDE_CODE_OAUTH_TOKEN", str(cm.exception.code))

    def test_routed_session_applies_account(self):
        self.cfg(env_file=self.env_file)
        chosen = {"label": "ai@", "config_dir": self.tmp}
        with mock.patch.object(rt, "configured", return_value=True), \
             mock.patch.object(rt, "decide", return_value={"account": chosen}), \
             mock.patch.object(rt, "apply") as applied:
            self.run_main(["claude", "--model", "fable"])
        applied.assert_called_once()
        self.assertEqual(os.environ.get("FLEET_DELEGATE_SESSION"), "1")   # not the own account
        os.environ.pop("FLEET_DELEGATE_SESSION", None)
        with mock.patch.object(rt, "configured", return_value=True), \
             mock.patch.object(rt, "decide", return_value={"account": chosen}), \
             mock.patch.object(rt, "apply", side_effect=rt.RouteError("bad token file")):
            with self.assertRaises(SystemExit) as cm:
                self.run_main(["claude"])
        self.assertIn("bad token file", str(cm.exception.code))

    def test_no_verdict_still_points_at_the_proxy(self):
        self.cfg()
        with mock.patch.object(rt, "proxy_env") as proxied:
            self.run_main(["claude"])                                  # routing not configured
        proxied.assert_not_called()
        with mock.patch.object(rt, "configured", return_value=True), \
             mock.patch.object(rt, "decide", return_value={"account": None}), \
             mock.patch.object(rt, "proxy_env") as proxied:
            self.run_main(["claude"])
        proxied.assert_called_once()
        self.assertIs(proxied.call_args[0][0], os.environ)
        self.assertEqual(self.exec_calls[-1], ("claude", ["claude"]))

    def test_exec_failure_is_reported(self):
        self.cfg()
        with mock.patch.object(ds.os, "execvp", side_effect=OSError("nope")):
            with self.assertRaises(SystemExit) as cm:
                self.run_main(["no-such-binary"])
        self.assertIn("cannot run", str(cm.exception.code))


# ── route.py edges ──────────────────────────────────────────────────────────────────────────

class RouteEdgesTest(Tmp):
    def setUp(self):
        super().setUp()
        self.patch(rt, "CONFIG", self.path("config.json"))
        self.patch(rt, "CACHE", self.path("route-cache.json"))
        self.patch(rt, "LOG", self.path("route.log"))
        self.cfg = {"node": "n", "route": {"hub": "http://127.0.0.1:1", "secret": self.path("secret", "s" * 40)},
                    "delegate": {"accounts": [
                        {"label": "john@", "config_dir": self.tmp, "own": True},
                        {"label": "ai@", "env_file": self.path("ai.env", "CLAUDE_CODE_OAUTH_TOKEN='tok-ai'\n", 0o600)}]}}

    def test_config_and_secret_edges(self):
        self.path("config.json", "{broken")
        self.assertEqual(rt.load_config(), {})
        self.assertFalse(rt.configured({"route": {"hub": "x"}}))          # no accounts
        with self.assertRaises(rt.RouteError):
            rt._secret(self.path("short", "tiny"))

    def test_decide_hub_then_cache_then_nothing(self):
        with mock.patch.object(rt, "ask_hub", return_value={"account": "ai@", "reason": "headroom"}):
            v = rt.decide("session", "fable", self.cfg, now=1000)
        self.assertEqual((v["label"], v["source"]), ("ai@", "hub"))
        self.assertTrue(os.path.exists(rt.CACHE))
        with mock.patch.object(rt, "ask_hub", side_effect=OSError("hub away")):
            v = rt.decide("session", "fable", self.cfg, now=1000 + 60)
        self.assertEqual(v["source"], "cache")
        self.assertIn("cached", v["reason"])
        with mock.patch.object(rt, "ask_hub", side_effect=OSError("hub away")):
            v = rt.decide("session", "fable", self.cfg, now=1000 + rt.CACHE_FOR + 1)
        self.assertIsNone(v["account"])
        self.assertIn("hub unreachable", v["reason"])
        self.assertEqual(rt.decide("session", None, {"route": {}})["reason"], "routing not configured")

    def test_log_and_cache_write_failures_are_swallowed(self):
        self.patch(rt, "LOG", self.tmp)                                   # a directory: open() fails
        self.patch(rt, "CACHE", os.path.join(self.tmp, "no", "dir", "cache.json"))
        with mock.patch.object(rt, "ask_hub", return_value={"account": "john@", "reason": "r"}):
            v = rt.decide("agent", None, self.cfg, now=5)
        self.assertEqual(v["label"], "john@")

    def test_token_and_apply(self):
        with self.assertRaises(rt.RouteError):
            rt.token(self.tmp)                                            # a directory
        with self.assertRaises(rt.RouteError):
            rt.token(self.path("nokey.env", "X=1\n", 0o600))
        env = rt.apply({"ANTHROPIC_API_KEY": "gone"}, self.cfg["delegate"]["accounts"][1])
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok-ai")
        self.assertEqual(env["FLEET_ACCOUNT"], "ai@")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        env = rt.apply({}, self.cfg["delegate"]["accounts"][0], cfg={"proxy": {}})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.tmp)

    def test_proxy_env_conditions(self):
        self.assertEqual(rt.proxy_env({}, {}), {})
        key = self.path("proxy.key", "device-key\n")
        cfg = {"proxy": {"device_key": key, "listen": "127.0.0.1:%d" % free_port()}}
        self.assertEqual(rt.proxy_env({}, cfg), {})                       # nothing listening
        cfg["proxy"]["device_key"] = self.path("missing.key")
        self.assertEqual(rt.proxy_env({}, cfg), {})                       # no key file
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        cfg = {"proxy": {"device_key": key, "listen": "127.0.0.1:%d" % srv.getsockname()[1]}}
        env = rt.proxy_env({}, cfg, "ai@")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://" + cfg["proxy"]["listen"])
        self.assertIn("X-Fleet-Account: ai@", env["ANTHROPIC_CUSTOM_HEADERS"])
        self.path("proxy.off", "")
        self.assertEqual(rt.proxy_env({}, cfg), {})                       # switched off

    def test_apply_leaves_the_default_config_dir_implicit(self):
        own = {"label": "john@", "config_dir": "~/.claude", "own": True}
        env = rt.apply({"CLAUDE_CONFIG_DIR": "/elsewhere"}, own)
        self.assertNotIn("CLAUDE_CONFIG_DIR", env)                      # the default folder stays implicit
        self.assertEqual(env["FLEET_ACCOUNT"], "john@")
        env = rt.apply({}, {"label": "second@", "config_dir": self.tmp})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], self.tmp)

    def test_proxy_env_keeps_tool_search_on(self):
        key = self.path("proxy.key", "device-key\n")
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        self.addCleanup(srv.close)
        cfg = {"proxy": {"device_key": key, "listen": "127.0.0.1:%d" % srv.getsockname()[1]}}
        self.assertEqual(rt.proxy_env({}, cfg)["ENABLE_TOOL_SEARCH"], "true")
        self.assertEqual(rt.proxy_env({"ENABLE_TOOL_SEARCH": "false"}, cfg)["ENABLE_TOOL_SEARCH"], "false")
        self.assertNotIn("ENABLE_TOOL_SEARCH", rt.proxy_env({}, {}))      # no proxy, nothing added

    def test_settings_model_and_main(self):
        d = os.path.join(self.tmp, "cfgdir")
        os.makedirs(d)
        self.assertIsNone(rt.settings_model(d))
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"model": "fable"}, fh)
        self.assertEqual(rt.settings_model(d), "fable")
        with open(os.path.join(d, "settings.json"), "w") as fh:
            json.dump({"model": 7}, fh)
        self.assertIsNone(rt.settings_model(d))
        self.assertEqual(rt.model_from(["--model", "haiku", "x"]), "haiku")
        self.assertIsNone(rt.model_from(["--verbose"]))
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["route.py", "--kind", "agent", "--model", "haiku"]), \
             mock.patch.object(rt, "decide", return_value={"account": {"label": "ai@", "env_file": "e"},
                                                          "label": "ai@", "reason": "r", "source": "hub"}), \
             contextlib.redirect_stdout(out):
            self.assertEqual(rt.main(), 0)
        self.assertIn('"label": "ai@"', out.getvalue())
        with mock.patch.object(sys, "argv", ["route.py"]), \
             mock.patch.object(rt, "decide", return_value={"account": None, "label": None, "reason": "r", "source": None}), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rt.main(), 1)


# ── nodestatus ──────────────────────────────────────────────────────────────────────────────

class NodeStatusTest(Tmp):
    def stub_path(self, systemctl_body):
        b = os.path.join(self.tmp, "sbin")
        os.makedirs(b, exist_ok=True)
        with open(os.path.join(b, "systemctl"), "w") as fh:
            fh.write("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % systemctl_body)
        os.chmod(os.path.join(b, "systemctl"), 0o755)
        p = mock.patch.dict(os.environ, {"PATH": b + os.pathsep + os.environ["PATH"]})
        p.start()
        self.addCleanup(p.stop)

    def test_units_parser(self):
        self.assertEqual(ns.units([]), [])
        self.stub_path("Id=fleet-relay.service\nActiveState=active\nNRestarts=2\n\n"
                       "Id=odd.service\nActiveState=failed\nNRestarts=n/a")
        got = ns.units(["fleet-relay", "odd", "absent"])
        self.assertEqual(got[0], {"name": "fleet-relay", "state": "active", "restarts": 2})
        self.assertEqual(got[1]["restarts"], None)
        self.assertEqual(got[2]["state"], "unknown")

    def test_machine_samplers_degrade(self):
        with mock.patch.object(ns.os, "getloadavg", side_effect=OSError):
            self.assertIsNone(ns.load())
        with mock.patch.object(ns.os, "statvfs", side_effect=OSError):
            h = ns.host(self.tmp)
        self.assertIsNone(h.get("disk"))
        self.assertIsNotNone(ns.load())
        self.assertEqual(ns._read("/nonexistent/file"), "")

    def test_repo_of(self):
        plain = os.path.join(self.tmp, "plain")
        os.makedirs(plain)
        self.assertEqual(ns.repo_of(plain), "plain")
        repo = os.path.join(self.tmp, "myrepo")
        subprocess.run(["git", "init", "-q", repo], check=True)
        inner = os.path.join(repo, "src")
        os.makedirs(inner)
        self.assertEqual(ns.repo_of(inner), "myrepo")
        self.assertEqual(ns.repo_of(inner), "myrepo")                      # cached

    def test_delegate_activity(self):
        self.assertIsNone(ns.delegate({}))
        log = self.path("delegate.log", "junk\n" + json.dumps({"t": time.time(), "ok": True, "cwd": "/r"}) + "\n"
                        + json.dumps({"t": "bad"}) + "\n")
        got = ns.delegate({"delegate": {}}, log=log)
        self.assertEqual(got["day"] if "day" in got else got.get("runs_24h", 1), 1)
        self.assertIsNotNone(ns.delegate({"delegate": {}}, log=self.path("missing.log")))

    def test_severity_and_iso(self):
        self.assertEqual(ns._severity(95), "critical")
        self.assertEqual(ns._severity(50, "rejected"), "critical")
        self.assertEqual(ns._severity(80), "warning")
        self.assertEqual(ns._severity(10, "allowed_warning"), "warning")
        self.assertEqual(ns._severity(10), "normal")
        self.assertIsNone(ns._iso("soon"))
        self.assertEqual(ns._iso(0), "1970-01-01T00:00:00Z")

    def creds(self, name="creds.json", token="tok", expires=None):
        return self.path(name, json.dumps({"claudeAiOauth": {"accessToken": token, "subscriptionType": "max",
                                                             "expiresAt": expires}}))

    def test_sample_login_paths(self):
        self.assertEqual(ns.sample_login(self.path("none.json"))["state"], "no-login")
        self.assertEqual(ns.sample_login(self.creds(token=""))["state"], "no-login")
        self.assertEqual(ns.sample_login(self.creds(expires=1000))["state"], "login-expired")
        good = self.creds()
        with mock.patch.object(ns, "_request", side_effect=OSError("net down")):
            self.assertEqual(ns.sample_login(good)["state"], "error")
        with mock.patch.object(ns, "_request", return_value=(401, {}, b"")):
            self.assertEqual(ns.sample_login(good)["state"], "login-expired")
        with mock.patch.object(ns, "_request", return_value=(500, {}, b"")):
            self.assertIn("HTTP 500", ns.sample_login(good)["detail"])
        new_shape = json.dumps({"limits": [
            {"kind": "session", "group": "session", "percent": 55, "severity": "normal",
             "is_active": True, "resets_at": "2026-10-10T00:00:00Z"},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 100,
             "scope": {"model": {"display_name": "Fable"}}}]}).encode()
        with mock.patch.object(ns, "_request", return_value=(200, {}, new_shape)):
            got = ns.sample_login(good)
        self.assertEqual(got["state"], "ok")
        self.assertEqual(got["plan"], "max")
        self.assertTrue(any(l["active"] for l in got["limits"]))
        old_shape = json.dumps({"five_hour": {"utilization": 20.2, "resets_at": "x"},
                                "seven_day": {"utilization": None}}).encode()
        with mock.patch.object(ns, "_request", return_value=(200, {}, old_shape)):
            got = ns.sample_login(good)
        self.assertEqual([l["name"] for l in got["limits"]], ["five_hour"])

    def test_sample_headers_paths(self):
        self.assertEqual(ns.sample_headers(self.path("nope.env"))["state"], "no-token")
        env = self.path("tok.env", "CLAUDE_CODE_OAUTH_TOKEN=abc\n")
        with mock.patch.object(ns, "_request", side_effect=OSError("down")):
            self.assertEqual(ns.sample_headers(env)["state"], "error")
        with mock.patch.object(ns, "_request", return_value=(401, {}, b"")):
            self.assertEqual(ns.sample_headers(env)["state"], "token-invalid")
        with mock.patch.object(ns, "_request", return_value=(200, {}, b"")):
            self.assertIn("without rate-limit headers", ns.sample_headers(env)["detail"])
        h = {"anthropic-ratelimit-unified-5h-utilization": "0.5",
             "anthropic-ratelimit-unified-5h-status": "allowed",
             "anthropic-ratelimit-unified-5h-reset": "1800000000",
             "anthropic-ratelimit-unified-7d-utilization": "0.95",
             "anthropic-ratelimit-unified-7d-status": "rejected",
             "anthropic-ratelimit-unified-status": "rejected"}
        with mock.patch.object(ns, "_request", return_value=(429, h, b"")):
            got = ns.sample_headers(env)
        self.assertEqual(got["state"], "ok")
        self.assertEqual([l["percent"] for l in got["limits"]], [50, 95])
        self.assertTrue(got["limits"][1]["active"])

    def test_sampler_sources_and_error_path(self):
        s = ns.Sampler([{"label": "a", "source": "claude-login", "credentials": self.creds()},
                        {"label": "b", "source": "token-headers", "env_file": self.path("b.env", "CLAUDE_CODE_OAUTH_TOKEN=t\n")}])
        with mock.patch.object(ns, "_request", return_value=(200, {}, json.dumps({"limits": []}).encode())):
            s.sample(s.accounts[0])
        with mock.patch.object(ns, "_request", return_value=(500, {}, b"")):
            s.sample(s.accounts[1])
        snap = s.snapshot()
        self.assertEqual({r["label"] for r in snap}, {"a", "b"})
        with mock.patch.object(s, "sample", side_effect=RuntimeError("boom")), \
             mock.patch.object(ns.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                s.run()
        self.assertEqual(s.results["a"]["state"], "error")
        self.assertIn("boom", s.results["a"]["detail"])


# ── quota-proxy internals ───────────────────────────────────────────────────────────────────

class ProxyInternalsTest(Tmp):
    def setUp(self):
        super().setUp()
        self.patch(qp, "FB", self.tmp)
        self.patch(qp, "CONFIG", self.path("config.json"))
        self.patch(qp, "OFF_FLAG", self.path("proxy.off"))
        self.patch(qp, "LOG", self.path("proxy.log"))
        os.makedirs(os.path.join(self.tmp, "accounts"))
        self.john = self.path("accounts/john.env", "CLAUDE_CODE_OAUTH_TOKEN=tok-john\n", 0o600)
        self.ai = self.path("accounts/ai.env", "CLAUDE_CODE_OAUTH_TOKEN=tok-ai\n", 0o600)
        self.key = self.path("proxy.key", "device-key-123\n")
        self.cfg = {"node": "n",
                    "proxy": {"listen": "127.0.0.1:0", "upstream": "http://127.0.0.1:%d" % free_port(),
                              "device_key": self.key},
                    "delegate": {"accounts": [
                        {"label": "john@", "config_dir": "~/.claude", "own": True, "token_file": self.john},
                        {"label": "ai@", "config_dir": "~/.claude-ai", "token_file": self.ai}]}}

    def test_helpers(self):
        self.path("config.json", "{nope")
        self.assertEqual(qp.load_config(), {})
        self.assertEqual(qp._epoch(None), None)
        self.assertEqual(qp._epoch("12.5"), 12.5)
        self.assertAlmostEqual(qp._epoch("1970-01-01T00:00:10Z"), 10.0)
        self.assertIsNone(qp._epoch("not a date"))
        self.assertIsNone(qp._iso(None))
        self.assertEqual(qp.model_of(b'{"x": 1, "model": "claude-fable-5-1"}'), "claude-fable-5-1")
        self.assertEqual(qp.model_of(json.dumps({"model": "m" * 130}).encode()), "m" * 130)   # json fallback
        self.assertIsNone(qp.model_of(b"[]"))
        self.assertIsNone(qp.model_of(b'{"model": 5}'))
        self.assertIsNone(qp.quota_rejected(200, {}))
        rej = {"anthropic-ratelimit-unified-status": "rejected"}
        self.assertEqual(qp.quota_rejected(429, {**rej, "anthropic-ratelimit-unified-7d-status": "rejected"}), "7d")
        self.assertEqual(qp.quota_rejected(429, {**rej, "anthropic-ratelimit-unified-representative-claim": "seven_day"}), "7d")
        self.assertEqual(qp.quota_rejected(429, rej), "5h")
        self.assertIsNone(qp.read_key(self.path("empty.key", "")))
        with mock.patch.object(qp, "LOG", self.tmp):
            qp.audit({"t": 1})                                            # unwritable: swallowed

    def test_accounts_token_cache_and_failures(self):
        acc = qp.Accounts(self.cfg)
        self.assertEqual(acc.token("john@"), "tok-john")
        self.assertEqual(acc.token("john@"), "tok-john")                  # cached by mtime
        self.assertIsNone(acc.token("ghost@"))
        os.chmod(self.ai, 0o644)
        self.assertIsNone(acc.token("ai@"))                               # route refuses a loose file
        os.unlink(self.john)
        self.assertIsNone(acc.token("john@"))

    def test_ledger_worst_and_choose(self):
        led = qp.Ledger(["john@", "ai@"])
        now = 1_000_000.0
        led.note_headers("john@", {"anthropic-ratelimit-unified-5h-utilization": "bad"}, now, 200)
        led.note_headers("john@", {"anthropic-ratelimit-unified-5h-utilization": "0.4",
                                   "anthropic-ratelimit-unified-5h-status": "allowed",
                                   "anthropic-ratelimit-unified-5h-reset": str(now + 5000)}, now, 200)
        led.note_headers("ai@", {"anthropic-ratelimit-unified-5h-utilization": "0.9",
                                 "anthropic-ratelimit-unified-5h-status": "rejected"}, now, 429)
        self.assertIn(("ai@", "5h"), led.in_force)                           # rejected: in force
        self.assertEqual(led.worst("ai@", None, now), 100)
        led.hub["john@"] = {"as_of": now - 10, "limits": [
            {"name": "weekly_scoped", "model": "Fable", "percent": 90},
            {"name": "session", "percent": 20, "resets_at": now + 60},     # resets soon -> 0
            {"name": "weekly_all", "percent": 30, "active": True}]}
        self.assertEqual(led.worst("john@", "claude-haiku", now), 100)      # active window
        led.hub["john@"]["limits"] = [{"name": "session", "percent": 20, "resets_at": now + 5000}]
        self.assertEqual(led.worst("john@", None, now), 40)                 # fresher header sample wins
        led.hub.pop("john@")
        self.assertEqual(led.worst("john@", None, now), 40)
        led.seen["john@"]["5h"]["reset"] = now + 30
        self.assertEqual(led.worst("john@", None, now), 0)                  # resets soon
        self.assertIsNone(led.worst("john@", None, now + qp.STALE + 1_000))
        led.mark_in_force("john@", "5h", now + 50)
        self.assertEqual(led.worst("john@", None, now), 100)                 # marked in force
        led.in_force.clear()
        led.seen = {"john@": {"5h": {"pct": 99, "reset": None, "status": None, "ts": now}},
                    "ai@": {"5h": {"pct": 98, "reset": None, "status": None, "ts": now}}}
        label, why, switched = led.choose("k", "john@", None, now, 90, 3)
        self.assertIn("every account over the line", why)
        self.assertEqual(led.take_events(limit=1)[0]["account"], "john@")

    def test_own_account_outranks_the_sticky_choice(self):
        led = qp.Ledger(["john@", "ai@"])
        now = 1_000_000.0
        led.seen = {"john@": {"5h": {"pct": 10, "reset": None, "status": None, "ts": now}},
                    "ai@": {"5h": {"pct": 60, "reset": None, "status": None, "ts": now}}}
        self.assertEqual(led.choose("k", "", None, now, 90, 3)[0], "john@")        # no own: the first label
        self.assertEqual(led.choose("k", "ai@", None, now, 90, 3)[0], "ai@")       # own wins over the sticky john@
        self.assertEqual(led.choose("k", "", None, now, 90, 3)[0], "ai@")          # and is what the key sticks to now
        led.seen["ai@"]["5h"]["pct"] = 95
        label, why, switched = led.choose("k", "ai@", None, now, 90, 3)            # own over the line: the best other
        self.assertEqual((label, switched), ("john@", True))
        self.assertEqual(led.choose("k", "nobody@", None, now, 90, 3)[0], "john@")  # an unknown own: the sticky choice

    def test_upstream_reconnect_and_drop(self):
        up = qp.Upstream("http://127.0.0.1:%d" % free_port())
        with self.assertRaises(OSError):
            up.request("GET", "/", {}, None)                                 # both attempts fail
        c1 = up.conn()
        c2 = up.conn(fresh=True)
        self.assertIsNot(c1, c2)
        up.drop()
        self.assertIsNone(up.local.conn)
        self.assertTrue(qp.Upstream("https://api.example.com").https)

    def test_hub_sync_once_and_error_loop(self):
        self.cfg["route"] = {"hub": "http://127.0.0.1:1"}
        st = qp.State(self.cfg)
        st.ledger.events.append({"ts": 1, "account": "john@", "window": "5h", "utilization": 0.5})
        calls = []

        def fake_signed(method, path, body, cfg):
            calls.append((method, path, body))
            if method == "GET":
                return {"accounts": [{"label": "john@", "usage": {"state": "ok", "as_of": 5, "limits": [{"name": "session", "percent": 1}]}},
                                     {"label": "stranger@", "usage": {"state": "ok", "as_of": 5}}]}
            return {}
        with mock.patch.object(qp.route, "signed_request", side_effect=fake_signed):
            qp.HubSync(st).once()
        self.assertEqual(st.ledger.hub["john@"]["as_of"], 5)
        self.assertEqual(calls[1][0], "POST")
        sync = qp.HubSync(st)

        def boom():
            sync.stop.set()                                                   # one pass, then exit
            raise RuntimeError("hub away")
        with mock.patch.object(sync, "once", side_effect=boom):
            sync.run()
        self.assertIn("hub away", open(qp.LOG).read())

    def test_serve_guards_and_cli(self):
        bad = dict(self.cfg, proxy=dict(self.cfg["proxy"], listen="0.0.0.0:8899"))
        with self.assertRaises(SystemExit):
            qp.serve(bad)
        self.cfg["route"] = {"hub": "http://127.0.0.1:1"}
        srv = qp.serve(self.cfg)
        self.addCleanup(srv.server_close)
        self.assertIsNotNone(srv.sync)
        srv.sync.stop.set()
        out = io.StringIO()
        for argv, expect in ((["--off"], "OFF"), (["--status"], "OFF"), (["--on"], "on")):
            with mock.patch.object(sys, "argv", ["quota-proxy"] + argv), contextlib.redirect_stdout(out):
                self.assertEqual(qp.main(), 0)
            self.assertIn(expect, out.getvalue().splitlines()[-1])
        self.assertFalse(os.path.exists(qp.OFF_FLAG))


if __name__ == "__main__":
    unittest.main()
