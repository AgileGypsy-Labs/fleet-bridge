#!/usr/bin/env python3
"""Start a Claude Code session on the delegate account when it opens in one of the repos
listed under "delegate" -> "session_repos" in ~/.fleet-bridge/config.json (anything below
them too, worktrees included), or anywhere at all while failover is on. Otherwise the
session starts unchanged.

    delegate-session <claude> [args...]
    delegate-session --failover on|off|status

IDE extensions call their process wrapper (claudeCode.claudeProcessWrapper) as
`<wrapper> <real claude> <args>`, so this can be that wrapper, or be chained from an
existing one with `exec ~/.fleet-bridge/bin/delegate-session "$@"`. From a shell:
`delegate-session claude`.

Failover is for when the machine's own login can't be used, say a plan that is suspended
or used up. `--failover on` starts every new session on the delegate account, in any
folder, until `--failover off`. Sessions already running keep the account they started with.

The whole session then counts against the delegate account, subagents included. It fails
closed like fleet-agent: a token file readable by others, or one without a token, stops the
session instead of letting it fall back to the machine's own login.

With routing configured (route.py: a "route" block and "delegate" -> "accounts" in the config),
every new session instead starts on the account the Caravan hub names as having the most
headroom for the session's model (its --model, else the settings.json model), whatever the
folder. The rules above are the fallback when the hub and the route cache cannot answer. A
session routed anywhere but the machine's own login ("own": true) is marked
FLEET_DELEGATE_SESSION=1, as before, for the agent guard.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import route  # noqa: E402  (installed beside this script)

CONFIG = os.environ.get("FLEET_CONFIG") or os.path.expanduser("~/.fleet-bridge/config.json")


def env_file_for(cwd):
    try:
        with open(CONFIG) as fh:
            d = json.load(fh).get("delegate") or {}
    except (OSError, ValueError):
        return None
    env_file = os.path.expanduser(d.get("env_file") or "~/.fleet-bridge/delegate.env")
    if d.get("failover") is True:
        return env_file
    cwd = os.path.realpath(cwd)
    for repo in d.get("session_repos") or []:
        repo = os.path.realpath(os.path.expanduser(repo))
        if cwd == repo or cwd.startswith(repo + os.sep):
            return env_file
    return None


def failover(mode):
    try:
        with open(CONFIG) as fh:
            conf = json.load(fh)
    except (OSError, ValueError) as exc:
        sys.exit("delegate-session: cannot read %s: %s" % (CONFIG, exc))
    d = conf.setdefault("delegate", {})
    if mode in ("on", "off"):
        d["failover"] = mode == "on"
        tmp = CONFIG + ".tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
            json.dump(conf, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, CONFIG)
    elif mode != "status":
        sys.exit("usage: delegate-session --failover on|off|status")
    if d.get("failover") is True:
        print("failover on: every new session starts on %s" % (d.get("account") or "the delegate account"))
    else:
        print("failover off: only sessions in session_repos start on the delegate account")


def main():
    if sys.argv[1:2] == ["--failover"]:
        return failover(sys.argv[2] if len(sys.argv) > 2 else "status")
    if len(sys.argv) < 2:
        sys.exit("usage: delegate-session <claude> [args...] | delegate-session --failover on|off|status")
    cfg = route.load_config()
    chosen = None
    if route.configured(cfg):
        model = route.model_from(sys.argv[2:]) or route.settings_model()
        chosen = route.decide("session", model, cfg)["account"]
    if chosen:
        try:
            route.apply(os.environ, chosen, cfg)
        except route.RouteError as exc:
            sys.exit("delegate-session: %s" % exc)
        if not chosen.get("own"):
            os.environ["FLEET_DELEGATE_SESSION"] = "1"
        env_file = None
    else:
        env_file = env_file_for(os.getcwd())
        if not env_file and route.configured(cfg):
            # No verdict (the hub and its cache are silent, or every account is over the line):
            # the session keeps the machine's own login, and the proxy, when it is up, still
            # balances it request by request.
            route.proxy_env(os.environ, cfg)
    if env_file:
        token = None
        try:
            if os.stat(env_file).st_mode & 0o077:
                sys.exit("delegate-session: %s is readable by others; chmod 600 it" % env_file)
            with open(env_file) as fh:
                for line in fh:
                    key, _, value = line.strip().partition("=")
                    if key == "CLAUDE_CODE_OAUTH_TOKEN" and value:
                        token = value
        except OSError:
            pass
        if not token:
            sys.exit("delegate-session: no CLAUDE_CODE_OAUTH_TOKEN in %s (docs/delegate.md)" % env_file)
        # An API key or auth token would take precedence over the delegate token.
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            os.environ.pop(key, None)
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = token
        # A plain marker for the guard, so it can tell this session already runs on the delegate
        # account without ever handling the token.
        os.environ["FLEET_DELEGATE_SESSION"] = "1"
    try:
        os.execvp(sys.argv[1], sys.argv[1:])
    except OSError as exc:
        sys.exit("delegate-session: cannot run %s: %s" % (sys.argv[1], exc))


if __name__ == "__main__":
    main()
