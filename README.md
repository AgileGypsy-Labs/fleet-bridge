# fleet-bridge

Make Claude Code sessions on different machines, and under different accounts, work as one team.

You might run Claude Code on two machines, each signed in to its own account, working on the same
projects. Those sessions can't talk to each other, and when one account's plan is busy, the other's
sits idle. fleet-bridge adds the missing pieces:

| Piece | What it does |
|---|---|
| **Relay** (`relay/`) | A session on one machine sends a message into a **live** session on another. It arrives the way Claude Code shows messages between local sessions: as a note from a teammate. |
| **Delegation** (`delegate/`) | `fleet-agent` runs agent work (search, research, review, edits) for a session under a **second account** on the same machine. An optional hook sends the built-in Agent tool there for the repos you choose, and `delegate-session` can start whole sessions in those repos on that account. |
| **Ownership guard** (`workspace/`) | A pre-commit hook that assigns paths to nodes, so two machines working one repo can't both edit the same files. |
| **Ops** (`tools/`, `relay/live-session.py`) | `fleet-status` checks every part by what it actually does and flags services that keep restarting. Each relay's `GET /v1/status` gives a dashboard the node's sessions by repo, services, power, delegated runs and Claude plan usage, and never message text or tokens. `live-session.py` drives a real interactive session on a machine nobody is sitting at, so the relay can be tested end to end. |

**Status: experimental.** It runs daily on a small fleet: two Linux laptops, a Raspberry Pi 5 as the
hub, and Claude Code 2.1.280. The relay relies on Claude Code's per-session inbox socket, and that
socket's message format is not publicly documented. [docs/inbox-socket.md](docs/inbox-socket.md)
explains how the format was determined, and `relay/probe.py` re-checks it after an upgrade.

## How it fits together

```
   laptop-a                          hub                              laptop-b
 ┌───────────────────┐         ┌─────────────────────┐         ┌───────────────────┐
 │ session ◄─ inbox  │         │                     │         │ inbox ─► session  │
 │           socket  │         │  127.0.0.1:8801 ◄───┼── ssh ──┤ send.py           │
 │ agent.py :8787 ◄──┼── ssh ──┤                     │         │                   │
 │                   │         │  127.0.0.1:8802 ◄───┼── ssh ──┤ agent.py :8787    │
 │ send.py ──────────┼── ssh ──►                     │         │                   │
 └───────────────────┘         └─────────────────────┘         └───────────────────┘
      everything binds 127.0.0.1; ssh carries it; messages are HMAC-signed
```

- Each node runs a small relay (`agent.py`) on `127.0.0.1:8787`.
- An ssh tunnel publishes that relay on the hub's loopback, one port per node (`-R`), and brings
  each peer's port back to the node (`-L`). The hub can be any Linux machine running sshd.
- A `SessionStart` hook records each new interactive session's inbox socket and token.
- `send.py <peer> "text"` signs the message. The peer's relay checks it and writes it into the
  newest live session there, or into the one named with `--session`.

## Security model

- **Nothing listens on a network address.** Relays bind `127.0.0.1` and refuse `0.0.0.0`. The hub
  ports are loopback only (keep `GatewayPorts no`). ssh carries all traffic, and host keys must
  already be known, because the tunnel never accepts one on its own.
- **Signed, fresh, once.** Every message carries an HMAC-SHA256 signature over timestamp + body,
  made with a shared secret. The relay refuses a bad signature, a timestamp more than 120s off, or a
  signature it has already accepted, before it touches any socket.
- **A peer is not the user.** Claude Code tells the receiving session that the message came from
  another session. That session won't approve permission prompts, change its settings, or treat
  the message as your approval because a peer asked.
- **Headless runs never register.** `--print` sessions have no inbox, and the hook skips
  `FLEET_NO_REGISTER`, `FLEET_AGENT` and factory job runs, so a message can't land in the middle of
  an automated run.
- **Delegation fails closed.**
  - The token file must be `0600`.
  - Other credentials (`ANTHROPIC_API_KEY`, the caller's own token) are removed from the child's
    environment. That leaves the delegate token as the only credential.
  - Read-only mode is enforced by Claude Code's permission layer, not by asking nicely. A Bash
    write shows up in `permission_denials` and never happens.
  - Measured control: with a bogus token, `claude -p` fails with 401 instead of quietly falling
    back to the machine's own login.

Found a hole? See [SECURITY.md](SECURITY.md).

## Quick start

Requirements:
- Linux with systemd user units
- Python 3.10 or later
- OpenSSH, with key access from each node to the hub
- Claude Code 2.1.280 or later on each node, for the inbox socket. Delegation alone works with any
  recent version.

```bash
# on each node: relay (see docs/setup.md for the hub side and the shared secret)
relay/install.sh --node laptop-a --port 8801 --peer laptop-b=8802 \
                 --hub "hub.lan:22" --hub-user fleet
~/.fleet-bridge/bin/send.py --peers                 # peer up, sessions >= 1
~/.fleet-bridge/bin/send.py laptop-b "migration landed, rebase is safe"

# on a node that should hand agent work to a second account
delegate/install.sh --account ops@example.com ~/code/some-repo
~/.fleet-bridge/bin/fleet-agent "where is the retry policy configured?"
~/.fleet-bridge/bin/fleet-agent --write "add a test for the empty-input case in parse()"
```

Full walkthroughs:
- [docs/setup.md](docs/setup.md): hub, nodes, first message, testing the last hop on an unattended node
- [docs/delegate.md](docs/delegate.md): `fleet-agent`, the guard hook, permission modes, controls
- [docs/inbox-socket.md](docs/inbox-socket.md): how the relay writes into a live session, and caveats

## Tests

```bash
make test        # 32 tests: relay auth, replay and delivery against a fake inbox socket,
                 # guard scope, fleet-agent environment and flags, delegate-session token
                 # routing, ownership guard, the status endpoint (nothing secret leaks)
                 # and usage parsing
```

The tests need no Claude account, no internet access and no systemd.

## Accounts

`fleet-agent` runs Claude Code under a second account that you hold. Use each account within the
terms of its own plan. This project is about coordinating your own machines and accounts, not
sharing one account between people.

## Roadmap

- Task claim and lease across nodes. We use Owain Lewis's
  [factory](https://github.com/owainlewis/factory) (MIT) for that: a control plane with workers
  that run Claude Code jobs. fleet-bridge already works alongside it: the hook skips its job
  runs, and `fleet-status` checks its workers.
- A relay mode that picks the target session by working directory rather than recency.
- macOS support, via launchd units instead of systemd user units.

## License

MIT. See [LICENSE](LICENSE).
