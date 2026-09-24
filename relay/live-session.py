#!/usr/bin/env python3
"""Open a real interactive Claude Code session in a pseudo-terminal, so the relay's last hop
(agent -> inbox socket -> a live session) can be tested on a node nobody is sitting at.

    live-session.py [--cwd DIR] [--seconds 180] [--allow-send]

Prints `READY <session-id>` once the SessionStart hook has registered the session, keeps it
alive for --seconds while writing its screen to ~/.fleet-bridge/live-session.log, then
exits it. First-run prompts (theme, security notes) get their default answer. Two are
never answered, because Enter would do the wrong thing: the login picker (Enter starts a
browser login) and folder trust (its default is "No, exit"). The run stops with a message
naming the ~/.claude.json flag to set instead — see docs/setup.md.
--allow-send pre-approves ~/.fleet-bridge/bin/send.py (and nothing else) for this one
session, so it can answer a peer over the relay without a permission prompt.

--env-file loads CLAUDE_CODE_OAUTH_TOKEN from a KEY=VALUE file when the environment has
none, for a node whose account exists only as a token (no interactive login).
"""
import argparse
import fcntl
import glob
import json
import os
import pty
import re
import select
import shutil
import struct
import sys
import termios
import time

HOME = os.path.expanduser("~")
REG = os.path.join(HOME, ".fleet-bridge", "sessions")
SEND = os.path.join(HOME, ".fleet-bridge", "bin", "send.py")
LOG = os.path.join(HOME, ".fleet-bridge", "live-session.log")
ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\r")
PROMPTS = ("trust the files", "Yes, proceed", "text style", "Security notes",
           "Press Enter", "Enter to confirm", "Enter to continue")


def registered_since(t0):
    for path in glob.glob(os.path.join(REG, "*.json")):
        if os.path.basename(path) == "current.json" or os.path.getmtime(path) < t0:
            continue
        try:
            entry = json.load(open(path))
        except (OSError, ValueError):
            continue
        if os.path.exists(entry.get("socket", "").removeprefix("uds:")):
            return entry["session_id"]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cwd", default=HOME)
    ap.add_argument("--seconds", type=int, default=180)
    ap.add_argument("--allow-send", action="store_true")
    ap.add_argument("--env-file", help="KEY=VALUE file holding CLAUDE_CODE_OAUTH_TOKEN")
    args = ap.parse_args()

    env = dict(os.environ, TERM="xterm-256color")
    # the native installer puts claude in ~/.local/bin, which a non-interactive ssh has no PATH to
    env["PATH"] = os.path.join(HOME, ".local", "bin") + os.pathsep + env.get("PATH", "")
    claude = shutil.which("claude", path=env["PATH"])
    if not claude:
        sys.exit("claude not found on %s" % env["PATH"])
    if "CLAUDE_CODE_OAUTH_TOKEN" not in env and args.env_file:
        for line in open(args.env_file):
            key, _, value = line.strip().partition("=")
            if key == "CLAUDE_CODE_OAUTH_TOKEN":
                env[key] = value
    for key in ("CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN"):
        env.pop(key, None)   # a parent session's inbox must not leak into this one
    argv = [claude]
    if args.allow_send:
        argv += ["--allowedTools", "Bash(%s:*)" % SEND]

    t0 = time.time()
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(args.cwd)
        os.execvpe(argv[0], argv, env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 45, 140, 0, 0))

    log = open(LOG, "wb")
    enters, last_enter, ready, recent, exited = 0, 0.0, None, b"", False
    unanswered = False                     # Enter pressed on a prompt, nothing redrawn since
    deadline = t0 + args.seconds
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], 0.5)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:                  # claude exited
                exited = True
                break
            log.write(chunk)
            log.flush()
            # answer "what terminal are you?" (DA1) as a VT220 would: claude holds keyboard
            # input until it hears back, and a bare pty never replies
            if b"\x1b[c" in chunk or b"\x1b[0c" in chunk:
                os.write(fd, b"\x1b[?62;22c")
            # escapes -> spaces: the TUI draws most spaces as cursor moves, so deleting them
            # glues words together ("Darkmode") and no prompt would ever match
            recent = (recent + ANSI.sub(b" ", chunk))[-4000:]
            unanswered = False
        if not ready and enters < 8:
            text = " ".join(recent.decode("utf-8", "replace").split())
            if "Select login method" in text:   # Enter here would start a browser login
                print("NEEDS ONBOARDING: set hasCompletedOnboarding=true in ~/.claude.json "
                      "(the token is the login)", flush=True)
                break
            if "trust this folder" in text:      # the default answer is "No, exit"
                print("NEEDS TRUST: set projects[\"%s\"].hasTrustDialogAccepted=true in "
                      "~/.claude.json" % os.path.realpath(args.cwd), flush=True)
                break
            since = time.time() - last_enter
            # a key pressed before the TUI reads input is lost and nothing is redrawn,
            # so an unchanged screen gets Enter again
            if (since > 1.5 and any(p in text for p in PROMPTS)) or (unanswered and since > 3):
                os.write(fd, b"\r")
                enters, last_enter, recent, unanswered = enters + 1, time.time(), b"", True
        if not ready:
            ready = registered_since(t0)
            if ready:
                print("READY %s" % ready, flush=True)
    if exited:
        print("EXITED after %ds%s (see %s)" % (time.time() - t0, "" if ready else ", never READY", LOG),
              flush=True)
    elif not ready:
        print("NOT READY after %ds (see %s)" % (args.seconds, LOG), flush=True)
    try:
        os.write(fd, b"/exit\r")
        time.sleep(3)
        os.kill(pid, 15)
    except OSError:
        pass
    os.waitpid(pid, 0)
    return 0 if ready else 1


if __name__ == "__main__":
    sys.exit(main())
