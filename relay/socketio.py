"""Write a message into a Claude Code session's inbox socket.

Facts this relies on, all from Claude Code 2.1.280:
  - the socket path is exported as CLAUDE_CODE_MESSAGING_SOCKET (also shown in /status
    as the `Peer address` row, prefixed `uds:`) and its token as
    CLAUDE_CODE_MESSAGING_TOKEN;
  - the binary's own debug output documents injection, auth line REQUIRED:
        {"type":"auth","token":"<TOKEN>"}
        {"type":"user","message":{"role":"user","content":"hello"}}
    i.e. an SDK stream-json user message. Verified live 2026-09-23: that line rendered in
    the receiving session as "Another Claude session sent a message"; the five envelopes
    this module used to guess (message/text/peer_message/user_message/prompt/bare) were
    each sent to the same session and none rendered;
  - Claude Code closes a connection that has not sent a complete line within 30s,
    so the payload is built before the socket is opened.

Versions without the inbox (2.1.206 has no CLAUDE_CODE_MESSAGING_SOCKET at all) never
export the variable, so the SessionStart hook registers nothing and the agent answers
404 rather than writing into a socket that is not there.
"""
import json
import os
import socket

CONFIG = os.path.expanduser("~/.fleet-bridge/config.json")


def _sdk_user(text, sender):
    # The receiving session sees a peer message but cannot answer it with SendMessage:
    # the sender is on another machine under another account. Say how to reply.
    content = (
        "[fleet-bridge message from %s]\n%s\n\n"
        "(reply with: ~/.fleet-bridge/bin/send.py %s \"<your reply>\")" % (sender, text, sender)
    )
    return {"type": "user", "message": {"role": "user", "content": content}}


def _notice(text, sender):
    # one-way: set-up notices from this node to its own sessions, nothing to reply to
    content = "[fleet-bridge notice from %s]\n%s" % (sender, text)
    return {"type": "user", "message": {"role": "user", "content": content}}


ENVELOPES = {
    "sdk_user": _sdk_user,
    "notice": _notice,
}


def load_config():
    try:
        with open(CONFIG) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}


def build(text, sender, envelope):
    if envelope not in ENVELOPES:
        raise ValueError(
            "unknown envelope %r; known: %s" % (envelope, ", ".join(ENVELOPES))
        )
    built = ENVELOPES[envelope](text, sender)
    return built if isinstance(built, str) else json.dumps(built)


def deliver(sock_path, token, text, sender, envelope, timeout=10.0):
    """Open, auth, send, close. Returns anything the socket wrote back."""
    payload = build(text, sender, envelope)
    lines = []
    if token:
        lines.append(json.dumps({"type": "auth", "token": token}))
    lines.append(payload)
    blob = ("\n".join(lines) + "\n").encode("utf-8")

    if sock_path.startswith("uds:"):
        sock_path = sock_path[4:]

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(sock_path)
        s.sendall(blob)
        try:
            s.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        chunks = []
        while True:
            try:
                b = s.recv(4096)
            except (socket.timeout, OSError):
                break
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks).decode("utf-8", "replace")
    finally:
        s.close()
