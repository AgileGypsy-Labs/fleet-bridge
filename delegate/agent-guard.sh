#!/usr/bin/env bash
# PreToolUse hook (matcher "Agent|Task"), user-level in ~/.claude/settings.json.
# In the repos listed under "delegate" -> "repos" in ~/.fleet-bridge/config.json, and
# anything below them (worktrees included), it refuses the built-in Agent tool and points
# the session at fleet-agent, so agent work there runs on the delegate account. Elsewhere
# it says nothing. Inside a fleet-agent run the session already is the delegate account,
# so its own subagents pass.
[ -z "${FLEET_AGENT:-}" ] || exit 0
exec python3 -c '
import json, os, sys
try:
    cwd = json.load(sys.stdin).get("cwd") or os.getcwd()
    cfg = json.load(open(os.path.expanduser("~/.fleet-bridge/config.json"))).get("delegate", {})
    repos, account = cfg.get("repos", []), cfg.get("account") or "the delegate account"
except (OSError, ValueError):
    sys.exit(0)
cwd = os.path.realpath(cwd)
if not any(cwd == r or cwd.startswith(r + "/") for r in (os.path.realpath(p) for p in repos)):
    sys.exit(0)
print(json.dumps({"hookSpecificOutput": {
  "hookEventName": "PreToolUse", "permissionDecision": "deny",
  "permissionDecisionReason": (
    "Agent work in this repo runs on " + account + ", not this session. "
    "Use fleet-agent through Bash instead of the Agent tool, with the same prompt: "
    "~/.fleet-bridge/bin/fleet-agent \"<prompt>\" for read-only work (search, research, review), "
    "or add --write for edits and commands. Use run_in_background for long or parallel tasks. "
    "Each run starts fresh, so put all the context it needs in the prompt. "
    "The answer comes back on stdout. See ~/.fleet-bridge/bin/fleet-agent --help.")}}))
'
