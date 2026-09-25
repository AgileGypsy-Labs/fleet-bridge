#!/usr/bin/env python3
"""Start a Claude Code session on the delegate account when it opens in one of the repos
listed under "delegate" -> "session_repos" in ~/.fleet-bridge/config.json (anything below
them too, worktrees included). Anywhere else, the session starts unchanged.

    delegate-session <claude> [args...]

IDE extensions call their process wrapper (claudeCode.claudeProcessWrapper) as
`<wrapper> <real claude> <args>`, so this can be that wrapper, or be chained from an
existing one with `exec ~/.fleet-bridge/bin/delegate-session "$@"`. From a shell:
`delegate-session claude`.

The whole session then counts against the delegate account, subagents included. It fails
closed like fleet-agent: a token file readable by others, or one without a token, stops the
session instead of letting it fall back to the machine's own login.
"""
import json
import os
import sys

CONFIG = os.environ.get("FLEET_CONFIG") or os.path.expanduser("~/.fleet-bridge/config.json")


def env_file_for(cwd):
    try:
        with open(CONFIG) as fh:
            d = json.load(fh).get("delegate") or {}
    except (OSError, ValueError):
        return None
    cwd = os.path.realpath(cwd)
    for repo in d.get("session_repos") or []:
        repo = os.path.realpath(os.path.expanduser(repo))
        if cwd == repo or cwd.startswith(repo + os.sep):
            return os.path.expanduser(d.get("env_file") or "~/.fleet-bridge/delegate.env")
    return None


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: delegate-session <claude> [args...]")
    env_file = env_file_for(os.getcwd())
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
