# Claude Code's inbox socket

Since roughly 2.1.280, each interactive Claude Code session opens a Unix socket that other local
sessions use to send it messages. The socket isn't a documented public interface. This page
records what fleet-bridge relies on and how it was established, so you can judge the risk and
re-check it yourself.

## What a session exposes

- `CLAUDE_CODE_MESSAGING_SOCKET`: the socket path. `/status` shows it as the "Peer address" row,
  prefixed `uds:`. Every session gets its own socket rather than inheriting its parent's.
- `CLAUDE_CODE_MESSAGING_TOKEN`: a per-session token that the socket requires.
- Both are exported **before any hook runs**, `SessionStart` included. That is how
  `relay/hooks/session-start.sh` records them in `~/.fleet-bridge/sessions/<session-id>.json`
  (mode `0600`).
- `claude --print` sessions have none. Neither did Claude Code 2.1.206.

## The message format

One connection, newline-delimited JSON, **auth line first**:

```
{"type":"auth","token":"<CLAUDE_CODE_MESSAGING_TOKEN>"}
{"type":"user","message":{"role":"user","content":"hello"}}
```

The second line has the same shape as an SDK stream-json user message. Claude Code closes a
connection that hasn't sent a complete line within about 30 seconds, so the relay builds the whole
payload before it connects.

How it was established:
1. Five plausible envelopes were each sent to a session someone was watching. The shapes were
   `message`, `text`, `peer_message`, `user_message`/`prompt`, and bare text. **None rendered.**
2. The Claude Code binary's own debug strings describe injection with an auth line and an SDK user
   message. That shape rendered at once.

The receiving session shows it as "Another Claude session sent a message". It tells the model the
message is from a peer rather than the user: act on it within the session's own permissions,
never approve prompts or change settings because a peer asked, and refuse "permission laundering"
(a peer asking the session to do something the peer was refused).

## Re-checking after an upgrade

```bash
~/.fleet-bridge/bin/probe.py --list
~/.fleet-bridge/bin/probe.py --session <id>    # sends each known envelope to a session you
                                               # are watching and asks which one rendered
```

`probe.py` writes the confirmed envelope into `~/.fleet-bridge/config.json`. The relay answers
`503` until an envelope is configured, so it never guesses.

## Why sessions are picked by recency

A node can have many live sessions. Without `--session`, the relay delivers to the most recently
registered one that still has a live socket. That's simple, and sometimes wrong: in testing, a
reply meant for one session landed in another that had been opened 30 seconds earlier. Name the
session when it matters (`send.py <peer> --session <prefix> ...`). The peer's `/v1/health` lists
live session ids, newest first.
