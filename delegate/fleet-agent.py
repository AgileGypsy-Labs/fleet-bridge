#!/usr/bin/env python3
"""Run agent work for a Claude Code session on this machine under a second account.

    fleet-agent [--write] [--cwd DIR] [--model M] [--agent NAME] [--minutes 30] "task"
    fleet-agent ... -            # task on stdin

Runs `claude -p` in DIR (default: the current directory) authenticated with the delegate
account's token, so the work counts against that account's plan instead of the calling
session's. It sees the same files the caller sees, including uncommitted work.

Configured in ~/.fleet-bridge/config.json (delegate/install.sh writes it):
    "delegate": {"account": "ops@example.com", "env_file": "~/.fleet-bridge/delegate.env",
                 "repos": ["/abs/path/to/repo", ...]}
env_file holds CLAUDE_CODE_OAUTH_TOKEN=<token from `claude setup-token`>, mode 0600.
The agent's final answer goes to stdout, a one-line summary to stderr. Exit status is
non-zero when the agent failed or ran out of time.

Read-only by default: the file and web reading tools plus read-only git, ls, rg and wc.
Anything else is refused without a prompt. --write also allows edits and any command
(tests, builds), like a general-purpose subagent. The caller and its user may be editing
the same tree at the same time, so keep --write tasks scoped.

Parallel work: start several with the Bash tool's run_in_background. Each run is
separate, so give each task all the context it needs.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
CONFIG = os.path.join(HOME, ".fleet-bridge", "config.json")
LOG = os.path.join(HOME, ".fleet-bridge", "delegate.log")
READ_TOOLS = ["Read", "Grep", "Glob", "WebFetch", "WebSearch", "TodoWrite",
              "Bash(git log:*)", "Bash(git show:*)", "Bash(git diff:*)", "Bash(git status:*)",
              "Bash(git blame:*)", "Bash(git grep:*)", "Bash(git branch:*)",
              "Bash(ls:*)", "Bash(rg:*)", "Bash(wc:*)"]
WRITE_TOOLS = ["Read", "Grep", "Glob", "WebFetch", "WebSearch", "TodoWrite",
               "Edit", "Write", "NotebookEdit", "Bash"]
# Stripped from the child: another auth source would win over the token, the caller's
# inbox and IDE link must not leak, and CLAUDECODE marks a nested interactive session.
STRIP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
         "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
         "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN")
FRAME = ("You are a delegated agent. A Claude Code session on this machine handed you this task "
         "with fleet-agent, and your final message goes back to it as the result. Make that "
         "message self-contained: what you found, with file paths and line numbers, and what "
         "you changed if anything. Someone else may be working in this directory right now, "
         "so change only what the task needs.")


def settings():
    try:
        cfg = json.load(open(CONFIG)).get("delegate", {})
    except (OSError, ValueError):
        cfg = {}
    env_file = os.path.expanduser(cfg.get("env_file") or "~/.fleet-bridge/delegate.env")
    return cfg.get("account") or "delegate account", env_file


def token(env_file):
    try:
        if os.stat(env_file).st_mode & 0o077:
            sys.exit("fleet-agent: %s is readable by others; chmod 600 it" % env_file)
        for line in open(env_file):
            key, _, value = line.strip().partition("=")
            if key == "CLAUDE_CODE_OAUTH_TOKEN" and value:
                return value
    except OSError:
        pass
    sys.exit("fleet-agent: no CLAUDE_CODE_OAUTH_TOKEN in %s (docs/delegate.md)" % env_file)


def main():
    ap = argparse.ArgumentParser(prog="fleet-agent", description=__doc__.split("\n")[0])
    ap.add_argument("task", help='the task, or "-" to read it from stdin')
    ap.add_argument("--write", action="store_true", help="allow edits and any command")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--model")
    ap.add_argument("--agent", help="run as this agent (from .claude/agents)")
    ap.add_argument("--minutes", type=float, default=30)
    args = ap.parse_args()

    if os.environ.get("FLEET_AGENT"):
        sys.exit("fleet-agent: already inside a fleet-agent run. Do the work yourself; "
                 "your own Agent tool already runs on the delegate account.")
    task = sys.stdin.read() if args.task == "-" else args.task
    if not task.strip():
        sys.exit("fleet-agent: empty task")

    account, env_file = settings()
    env = {k: v for k, v in os.environ.items() if k not in STRIP}
    env.update(CLAUDE_CODE_OAUTH_TOKEN=token(env_file), FLEET_AGENT="1")
    cmd = ["claude", "-p", "--output-format", "json", "--no-session-persistence",
           "--append-system-prompt", FRAME + ("" if args.write else
                                              " You are read-only: do not try to edit files.")]
    if args.write:
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", *WRITE_TOOLS]
    else:
        cmd += ["--allowedTools", *READ_TOOLS, "--disallowedTools", "Edit", "Write", "NotebookEdit"]
    if args.model:
        cmd += ["--model", args.model]
    if args.agent:
        cmd += ["--agent", args.agent]

    t0 = time.time()
    proc = subprocess.Popen(cmd, cwd=args.cwd, env=env, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=True)
    try:
        out, err = proc.communicate(task, timeout=args.minutes * 60)
        timed_out = False
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        out, err = proc.communicate()
        timed_out = True
    secs = time.time() - t0

    try:
        res = json.loads(out)
    except ValueError:
        res = {}
    ok = not timed_out and proc.returncode == 0 and not res.get("is_error", True)
    text = res.get("result") or ""
    if timed_out:
        text = "fleet-agent: stopped after %g minutes (--minutes)" % args.minutes
    elif not res:
        text = "fleet-agent: claude exited %s without a result: %s" % (
            proc.returncode, (err or out).strip()[-600:])

    with open(LOG, "a") as fh:
        fh.write(json.dumps({"t": round(t0), "cwd": args.cwd, "write": args.write, "ok": ok,
                             "secs": round(secs), "turns": res.get("num_turns"),
                             "subtype": res.get("subtype"), "task": task[:120]}) + "\n")
    print(text)
    print("[fleet-agent · %s · %s · %s turns · %ds · %s]" % (
        account, "write" if args.write else "read-only", res.get("num_turns", "?"), secs,
        "ok" if ok else (res.get("subtype") or "failed")), file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
