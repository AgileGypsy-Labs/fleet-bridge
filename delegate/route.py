#!/usr/bin/env python3
"""Which account a new Claude Code session or agent run on this node starts on.

Asks the Caravan hub (GET /v1/route, signed with the Caravan secret this node already holds)
for the plan account with the most headroom for the model, among the accounts this node can
run. The hub's answer is cached per kind and model; when the hub cannot be reached, a cached
answer younger than CACHE_FOR seconds stands in, and after that the caller falls back to its
static rule. Nothing here holds a session up for longer than the hub timeout.

Configuration, in ~/.fleet-bridge/config.json:
    "route": {"hub": "http://127.0.0.1:7450", "secret_file": "~/.config/caravan/secret",
              "timeout": 2.0},
    "delegate": {"accounts": [
        {"label": "you@example.com", "config_dir": "~/.claude", "own": true},
        {"label": "second@example.com", "config_dir": "~/.claude-second"},
        {"label": "third@example.com", "env_file": "~/.fleet-bridge/third.env"}]}
An account with config_dir is a Claude Code login kept in that directory (what
CLAUDE_CONFIG_DIR points at); one with env_file is a setup-token file holding
CLAUDE_CODE_OAUTH_TOKEN=..., mode 0600. "own" marks the machine's own login: a session routed
anywhere else is marked FLEET_DELEGATE_SESSION=1 for the agent guard, as before.

    route.py [--kind session|agent] [--model M]     print the decision as JSON
"""
import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG = os.environ.get("FLEET_CONFIG") or os.path.expanduser("~/.fleet-bridge/config.json")
CACHE = os.environ.get("FLEET_ROUTE_CACHE") or os.path.expanduser("~/.fleet-bridge/route-cache.json")
LOG = os.environ.get("FLEET_ROUTE_LOG") or os.path.expanduser("~/.fleet-bridge/route.log")
CACHE_FOR = 900       # seconds a cached decision stands in for an unreachable hub
CREDENTIALS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
               "CLAUDE_CONFIG_DIR")


class RouteError(RuntimeError):
    """A credential file that must not be used: missing, empty or readable by others."""


def load_config():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def accounts(cfg=None):
    """The accounts this node can start sessions on, in the configured order."""
    cfg = load_config() if cfg is None else cfg
    out = []
    for a in (cfg.get("delegate") or {}).get("accounts") or []:
        if isinstance(a, dict) and a.get("label") and (a.get("config_dir") or a.get("env_file")):
            out.append(a)
    return out


def configured(cfg=None):
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("route")) and bool(accounts(cfg))


def _secret(path):
    path = os.path.expanduser(path)
    st = os.stat(path)
    if st.st_mode & 0o077:
        raise RouteError("%s is readable by others; chmod 600 it" % path)
    with open(path, "rb") as fh:
        s = fh.read().strip()
    if len(s) < 32:
        raise RouteError("%s holds fewer than 32 bytes" % path)
    return s


def signed_request(method, path, body, cfg):
    """A request to the Caravan hub named by cfg["route"], carrying Caravan's signature:
    HMAC-SHA256 over ts.nonce.METHOD.path(with query).body. Returns the JSON answer."""
    r = cfg.get("route") or {}
    secret = _secret(r.get("secret_file") or "~/.config/caravan/secret")
    data = json.dumps(body).encode() if body is not None else b""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    msg = b".".join([ts.encode(), nonce.encode(), method.upper().encode(), path.encode(), data])
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()
    req = urllib.request.Request((r.get("hub") or "http://127.0.0.1:7450").rstrip("/") + path,
                                 data=data if body is not None else None, method=method.upper(),
                                 headers={"X-Caravan-Ts": ts, "X-Caravan-Nonce": nonce,
                                          "X-Caravan-Sig": sig, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=float(r.get("timeout") or 2.0)) as resp:
        return json.load(resp)


def ask_hub(kind, model, node, labels, cfg):
    q = {"kind": kind, "node": node, "accounts": ",".join(labels)}
    if model:
        q["model"] = model
    return signed_request("GET", "/v1/route?" + urllib.parse.urlencode(q), None, cfg)


def _cache_read():
    try:
        with open(CACHE) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _cache_write(cache):
    tmp = CACHE + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        json.dump(cache, fh)
    os.replace(tmp, CACHE)


def _log(kind, model, verdict):
    try:
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"t": round(time.time()), "kind": kind, "model": model,
                                 "account": verdict.get("label"), "source": verdict.get("source"),
                                 "reason": (verdict.get("reason") or "")[:200]}) + "\n")
    except OSError:
        pass


def decide(kind, model=None, cfg=None, now=None):
    """{"account": config entry or None, "label", "reason", "source": "hub" | "cache" | None}.
    account is None when the hub refuses every account (all over the line), when routing is
    not configured, or when neither the hub nor a fresh cached answer is available; the caller
    then applies its static rule."""
    cfg = load_config() if cfg is None else cfg
    now = now or time.time()
    by_label = {a["label"]: a for a in accounts(cfg)}
    if not by_label or not cfg.get("route"):
        return {"account": None, "label": None, "reason": "routing not configured", "source": None}
    key = "%s:%s" % (kind, model or "")
    node = cfg.get("node") or os.uname().nodename
    try:
        got = ask_hub(kind, model, node, list(by_label), cfg)
        label = got.get("account")
        verdict = {"account": by_label.get(label), "label": label if label in by_label else None,
                   "reason": got.get("reason") or "", "source": "hub",
                   "headroom": got.get("headroom")}
        cache = _cache_read()
        cache[key] = {"label": verdict["label"], "reason": verdict["reason"], "ts": now}
        try:
            _cache_write(cache)
        except OSError:
            pass
    except (OSError, ValueError, urllib.error.URLError, RouteError) as exc:
        c = _cache_read().get(key)
        if c and now - (c.get("ts") or 0) <= CACHE_FOR and c.get("label") in by_label:
            verdict = {"account": by_label[c["label"]], "label": c["label"], "source": "cache",
                       "reason": "%s (cached %dm ago; hub: %s)"
                                 % (c.get("reason"), (now - c["ts"]) // 60, str(exc)[:80])}
        else:
            verdict = {"account": None, "label": None, "source": None,
                       "reason": "hub unreachable: %s" % str(exc)[:120]}
    _log(kind, model, verdict)
    return verdict


def token(env_file):
    path = os.path.expanduser(env_file)
    try:
        if os.stat(path).st_mode & 0o077:
            raise RouteError("%s is readable by others; chmod 600 it" % env_file)
        with open(path) as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                if key == "CLAUDE_CODE_OAUTH_TOKEN" and value:
                    return value.strip("'\"")
    except OSError as exc:
        raise RouteError("cannot read %s: %s" % (env_file, exc.strerror)) from None
    raise RouteError("no CLAUDE_CODE_OAUTH_TOKEN in %s" % env_file)


def apply(env, account, cfg=None):
    """The environment that starts a child on `account`: any inherited credential goes first,
    so the choice cannot be overridden by the caller's. With `cfg`, the child is also pointed at
    this node's quota proxy when one is configured and up. Raises RouteError on a bad token file."""
    for key in CREDENTIALS:
        env.pop(key, None)
    if account.get("config_dir"):
        d = os.path.expanduser(account["config_dir"])
        # The default folder stays implicit. CLAUDE_CONFIG_DIR, even when it names ~/.claude, moves
        # the global config from ~/.claude.json to <dir>/.claude.json, and the session would start
        # without its user MCP servers, project trust and onboarding state.
        if os.path.realpath(d) != os.path.realpath(os.path.expanduser("~/.claude")):
            env["CLAUDE_CONFIG_DIR"] = d
    else:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token(account["env_file"])
    env["FLEET_ACCOUNT"] = account["label"]
    if cfg is not None:
        proxy_env(env, cfg, account["label"])
    return env


def proxy_listening(listen, timeout=0.3):
    import socket
    host, _, port = listen.rpartition(":")
    try:
        with socket.create_connection((host.strip("[]") or "127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def proxy_env(env, cfg, label=None):
    """Point a child at this node's quota proxy (proxy/quota-proxy.py): ANTHROPIC_BASE_URL plus
    the device key and the account label as custom headers. Left alone when no proxy is
    configured, when it is switched off (~/.fleet-bridge/proxy.off), when its key is missing or
    when nothing listens on its port, so a proxy that is down never costs a session."""
    p = cfg.get("proxy") or {}
    home = os.path.dirname(CONFIG)
    if not p or os.path.exists(os.path.join(home, "proxy.off")):
        return env
    key_file = os.path.expanduser(p.get("device_key") or os.path.join(home, "proxy.key"))
    try:
        with open(key_file) as fh:
            key = fh.read().strip()
    except OSError:
        return env
    listen = p.get("listen") or "127.0.0.1:8899"
    if not key or not proxy_listening(listen):
        return env
    env["ANTHROPIC_BASE_URL"] = "http://" + listen
    headers = ["X-Fleet-Device: " + key] + (["X-Fleet-Account: " + label] if label else [])
    env["ANTHROPIC_CUSTOM_HEADERS"] = "\n".join(headers)
    # Claude Code switches tool search off behind any base URL that is not Anthropic's own, and
    # then sends every MCP tool schema with each request (134k prompt tokens against 47k on a
    # node with many servers). The proxy forwards bodies unchanged, so it stays on unless the
    # caller has set it.
    env.setdefault("ENABLE_TOOL_SEARCH", "true")
    return env


def model_from(argv):
    """The --model a Claude Code command line names, if any."""
    for i, arg in enumerate(argv):
        if arg == "--model" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1]
    return None


def settings_model(config_dir=None):
    """The model in a config directory's settings.json (the session's default)."""
    path = os.path.join(os.path.expanduser(config_dir or "~/.claude"), "settings.json")
    try:
        with open(path) as fh:
            m = json.load(fh).get("model")
        return m if isinstance(m, str) and m else None
    except (OSError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser(prog="route.py", description=__doc__.split("\n")[0])
    ap.add_argument("--kind", default="session", choices=("session", "agent"))
    ap.add_argument("--model")
    args = ap.parse_args()
    v = decide(args.kind, args.model)
    v["account"] = v["account"] and {k: v["account"][k] for k in ("label", "config_dir", "env_file", "own")
                                     if k in v["account"]}
    print(json.dumps(v, indent=2))
    return 0 if v["label"] else 1


if __name__ == "__main__":
    sys.exit(main())
