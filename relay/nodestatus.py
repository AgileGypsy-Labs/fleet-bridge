"""What the relay's GET /v1/status reports about this node: counts, states and percentages.

The endpoint has no auth. Like /v1/health it listens on loopback only, and a hub dashboard
reads it through the node's tunnel. So nothing here may return message text, prompts,
tokens or session tokens. tests/test_fleet_bridge.py checks that the shared secret, session
tokens and a delegated prompt never appear in the output.

Account usage comes from a background Sampler, configured in ~/.fleet-bridge/config.json:

    "accounts": [
      {"label": "you@example.com", "source": "claude-login"},
      {"label": "ops@example.com", "source": "token-headers",
       "env_file": "~/.fleet-bridge/delegate.env"}
    ]

- claude-login: the access token of this machine's Claude Code login, read from
  ~/.claude/.credentials.json, sent to the usage API (the same endpoint as /usage). Free.
  The token needs the user:profile scope, which interactive logins have. The refresh token
  is never used: refreshing would rotate Claude Code's own login out from under it. An
  expired token is reported as such; opening Claude Code on the machine refreshes it.
- token-headers: for a token without user:profile (e.g. from `claude setup-token`). A
  1-token request returns the account's rate-limit headers (5-hour and weekly utilisation,
  resets, status). Costs about 23 tokens a sample. Per-model limits are not visible this way.
"""
import glob
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request

FB = os.path.expanduser("~/.fleet-bridge")
UA = "fleet-bridge/0.2 (usage monitor; +https://github.com/AgileGypsy-Labs/fleet-bridge)"
DEFAULT_UNITS = ["fleet-relay", "fleet-relay-tunnel"]
EVERY = {"claude-login": 300, "token-headers": 600}
_repo_cache = {}


def _read(path):
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return ""


# ---- machine --------------------------------------------------------------------------

def units(names):
    """[{name, state, restarts}] for systemd user units. NRestarts counts restarts since the
    unit was last started by hand, so a climbing number means a restart loop."""
    if not names:
        return []
    ids = [n if "." in n else n + ".service" for n in names]
    try:
        out = subprocess.run(["systemctl", "--user", "show", "-p", "Id,ActiveState,NRestarts", *ids],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    seen = {}
    for block in out.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if kv.get("Id"):
            seen[kv["Id"]] = kv
    result = []
    for name, uid in zip(names, ids):
        kv = seen.get(uid, {})
        restarts = kv.get("NRestarts", "")
        result.append({"name": name, "state": kv.get("ActiveState") or "unknown",
                       "restarts": int(restarts) if restarts.isdigit() else None})
    return result


def power(root=None):
    """AC and battery, or None on a machine with neither (desktop, VM)."""
    root = root or os.environ.get("FLEET_SYSFS") or "/sys/class/power_supply"
    ac, battery = None, None
    for d in sorted(glob.glob(os.path.join(root, "*"))):
        kind = _read(os.path.join(d, "type"))
        if kind in ("Mains", "USB"):
            online = _read(os.path.join(d, "online")) == "1"
            ac = bool(ac) or online
        elif kind == "Battery" and battery is None:
            cap = _read(os.path.join(d, "capacity"))
            battery = {"percent": int(cap) if cap.isdigit() else None,
                       "status": _read(os.path.join(d, "status")) or None}
    if ac is None and battery is None:
        return None
    b = battery or {}
    # "Discharging" is the reliable signal: it is true on battery whatever the adapter
    # reports, including a USB-C dock that has lost its own power.
    return {"ac": ac, "battery": b.get("percent"), "status": b.get("status"),
            "on_battery": b.get("status") == "Discharging" or ac is False}


def load():
    try:
        l1 = os.getloadavg()[0]
    except OSError:
        return None
    cpus = os.cpu_count() or 1
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip().split()[:1]:
            mem[k] = int(v.split()[0])
    used = None
    if mem.get("MemTotal") and "MemAvailable" in mem:
        used = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]))
    return {"load1": round(l1, 2), "cpus": cpus, "per_core": round(l1 / cpus, 2), "mem_used_pct": used}


# ---- Claude sessions and delegated work ----------------------------------------------

def repo_of(cwd):
    """The repository a directory belongs to, by name. A git worktree counts under its
    main repository, so KTHULHU/.claude/worktrees/KTH-1 is KTHULHU."""
    if cwd in _repo_cache:
        return _repo_cache[cwd]
    name = None
    try:
        common = subprocess.run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=3).stdout.strip()
        if common:
            common = common.rstrip("/")
            name = os.path.basename(os.path.dirname(common)) if common.endswith("/.git") \
                else os.path.basename(common).removesuffix(".git")
    except (OSError, subprocess.SubprocessError):
        pass
    name = name or os.path.basename(cwd.rstrip("/")) or "/"
    _repo_cache[cwd] = name
    return name


def sessions(entries):
    """Live interactive sessions by repo. One process can hold several registrations
    (a resumed session registers again), so each live socket counts once."""
    newest = {}
    for e in entries:
        sock = (e.get("socket") or "").removeprefix("uds:")
        if sock and (sock not in newest or e.get("registered_at", 0) > newest[sock].get("registered_at", 0)):
            newest[sock] = e
    by_repo = {}
    for e in newest.values():
        repo = repo_of(e.get("cwd") or "/")
        by_repo[repo] = by_repo.get(repo, 0) + 1
    return {"total": len(newest), "by_repo": dict(sorted(by_repo.items(), key=lambda kv: -kv[1]))}


def delegate(cfg, log=None, now=None):
    """fleet-agent activity from its log. Counts and the last run's repo only, never the
    prompt: a delegated task can say anything."""
    if "delegate" not in cfg:
        return None
    log = log or os.path.join(FB, "delegate.log")
    now = now or time.time()
    runs = []
    try:
        with open(log) as fh:
            lines = fh.readlines()[-1000:]
    except OSError:
        lines = []
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if isinstance(r.get("t"), (int, float)):
            runs.append(r)
    day = [r for r in runs if r["t"] > now - 86400]
    try:
        running = int(subprocess.run(["pgrep", "-fc", "/.fleet-bridge/bin/fleet-agent"],
                                     capture_output=True, text=True, timeout=3).stdout.strip() or 0)
    except (OSError, subprocess.SubprocessError, ValueError):
        running = None
    last = runs[-1] if runs else None   # fleet-agent appends as each run finishes
    return {"account": cfg["delegate"].get("account"),
            "runs_1h": sum(1 for r in day if r["t"] > now - 3600), "runs_24h": len(day),
            "failed_24h": sum(1 for r in day if not r.get("ok")), "running": running,
            "last": {"t": last["t"], "repo": repo_of(last.get("cwd") or "/"), "secs": last.get("secs"),
                     "ok": bool(last.get("ok")), "write": bool(last.get("write"))} if last else None}


# ---- account usage ----------------------------------------------------------------------

def _severity(pct, status=None):
    if status == "rejected" or (pct is not None and pct >= 90):
        return "critical"
    if status == "allowed_warning" or (pct is not None and pct >= 75):
        return "warning"
    return "normal"


def _iso(epoch):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(float(epoch))))
    except (TypeError, ValueError):
        return None


def _request(path, token, body=None, timeout=20):
    base = os.environ.get("FLEET_USAGE_API") or "https://api.anthropic.com"
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body else None,
                                 method="POST" if body else "GET",
                                 headers={"Authorization": "Bearer " + token, "User-Agent": UA,
                                          "anthropic-beta": "oauth-2025-04-20",
                                          "anthropic-version": "2023-06-01",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def sample_login(credentials=None):
    path = os.path.expanduser(credentials or "~/.claude/.credentials.json")
    try:
        with open(path) as fh:
            oauth = json.load(fh).get("claudeAiOauth") or {}
    except (OSError, ValueError):
        return {"state": "no-login", "detail": "no Claude Code login on this machine"}
    token, expires = oauth.get("accessToken"), oauth.get("expiresAt")
    if not token:
        return {"state": "no-login", "detail": "no Claude Code login on this machine"}
    if expires and expires / 1000 < time.time():
        return {"state": "login-expired", "detail": "open Claude Code on this machine to refresh its login"}
    try:
        code, _, raw = _request("/api/oauth/usage", token)
    except OSError as exc:
        return {"state": "error", "detail": str(exc)[:120]}
    if code == 401:
        return {"state": "login-expired", "detail": "open Claude Code on this machine to refresh its login"}
    if code != 200:
        return {"state": "error", "detail": "usage API HTTP %d" % code}
    d = json.loads(raw)
    limits = []
    for l in d.get("limits") or []:
        model = ((l.get("scope") or {}).get("model") or {}).get("display_name")
        limits.append({"name": l.get("kind"), "group": l.get("group"), "model": model,
                       "percent": l.get("percent"), "severity": l.get("severity"),
                       "active": bool(l.get("is_active")), "resets_at": l.get("resets_at")})
    if not limits:                       # older response shape: the named windows only
        for key, group in (("five_hour", "session"), ("seven_day", "weekly")):
            w = d.get(key) or {}
            if w.get("utilization") is not None:
                pct = round(w["utilization"])
                limits.append({"name": key, "group": group, "model": None, "percent": pct,
                               "severity": _severity(pct), "active": False, "resets_at": w.get("resets_at")})
    return {"state": "ok", "plan": oauth.get("subscriptionType"), "limits": limits}


def sample_headers(env_file):
    token = None
    try:
        with open(os.path.expanduser(env_file)) as fh:
            for line in fh:
                k, _, v = line.strip().partition("=")
                if k == "CLAUDE_CODE_OAUTH_TOKEN" and v:
                    token = v
    except OSError:
        pass
    if not token:
        return {"state": "no-token", "detail": "no CLAUDE_CODE_OAUTH_TOKEN in %s" % env_file}
    body = {"model": os.environ.get("FLEET_PROBE_MODEL") or "claude-haiku-4-5-20251001",
            "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}
    try:
        code, h, _ = _request("/v1/messages", token, body)
    except OSError as exc:
        return {"state": "error", "detail": str(exc)[:120]}
    if code == 401:
        return {"state": "token-invalid", "detail": "the token in %s was refused" % env_file}
    get = lambda k: h.get("anthropic-ratelimit-unified-" + k)
    limits = []
    for win, name, group in (("5h", "session", "session"), ("7d", "weekly_all", "weekly")):
        u = get(win + "-utilization")
        if u is None:
            continue
        pct = round(float(u) * 100)
        status = get(win + "-status")
        limits.append({"name": name, "group": group, "model": None, "percent": pct,
                       "severity": _severity(pct, status), "active": status == "rejected",
                       "resets_at": _iso(get(win + "-reset"))})
    if not limits:
        return {"state": "error", "detail": "HTTP %d without rate-limit headers" % code}
    return {"state": "ok", "status": get("status"), "limits": limits}


class Sampler(threading.Thread):
    """Samples each configured account on its own schedule and keeps the last result. A
    failed sample keeps the last good limits, so the dashboard shows their age, not a gap."""

    def __init__(self, accounts):
        super().__init__(daemon=True, name="usage-sampler")
        self.accounts = [a for a in accounts or [] if a.get("label") and a.get("source") in EVERY]
        self.results, self.due, self.lock = {}, {}, threading.Lock()

    def sample(self, acct):
        if acct["source"] == "claude-login":
            got = sample_login(acct.get("credentials"))
        else:
            got = sample_headers(acct.get("env_file") or "~/.fleet-bridge/delegate.env")
        now = time.time()
        with self.lock:
            prev = self.results.get(acct["label"], {})
            rec = {"label": acct["label"], "source": acct["source"], "checked": round(now),
                   "state": got["state"], "detail": got.get("detail")}
            if got["state"] == "ok":
                rec.update(as_of=round(now), limits=got["limits"],
                           plan=got.get("plan") or prev.get("plan"), status=got.get("status"))
            else:
                rec.update(as_of=prev.get("as_of"), limits=prev.get("limits", []), plan=prev.get("plan"))
            self.results[acct["label"]] = rec

    def run(self):
        while True:
            for acct in self.accounts:
                if time.time() >= self.due.get(acct["label"], 0):
                    try:
                        self.sample(acct)
                    except Exception as exc:  # noqa: BLE001 - a sampler must not die
                        with self.lock:
                            rec = self.results.setdefault(acct["label"], {"label": acct["label"], "source": acct["source"],
                                                                          "limits": [], "as_of": None})
                            rec.update(state="error", checked=round(time.time()), detail=str(exc)[:120])
                    self.due[acct["label"]] = time.time() + int(acct.get("every") or EVERY[acct["source"]])
            time.sleep(10)

    def snapshot(self):
        with self.lock:
            return [dict(v) for v in self.results.values()]


def status(cfg, entries, sampler=None):
    st = cfg.get("status", {})
    return {
        "ok": True,
        "node": cfg.get("node"),
        "ts": round(time.time()),
        "sessions": sessions(entries),
        "units": units(st.get("units", DEFAULT_UNITS)),
        "power": power(),
        "load": load(),
        "delegate": delegate(cfg),
        "accounts": sampler.snapshot() if sampler else [],
    }
