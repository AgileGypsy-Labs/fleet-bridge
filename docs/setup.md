# Setting up a fleet

The examples use two nodes, `laptop-a` and `laptop-b`, and a hub reachable as `hub.lan`, with an
account `fleet` on it. Substitute your own names. Node names appear in every message ("from
laptop-a"), so name nodes after the machine or account, not after the hostname.

## 1. The hub

Any always-on Linux machine with OpenSSH will do. Nothing runs on it except sshd.

1. Make sure each node can log in to it with a key, as the same account:
   `ssh fleet@hub.lan true`. **Check the host key fingerprint against the hub itself before you
   accept it.** The tunnels run with `BatchMode=yes` and will never accept one on their own.
2. Keep `GatewayPorts no` (the default). Forwarded ports then bind the hub's loopback only.
3. Install `hub/sshd-client-alive.conf` as `/etc/ssh/sshd_config.d/10-fleet-client-alive.conf`
   and reload sshd. Without it, a node that disappears without disconnecting (sleep, pulled
   cable) keeps its port held on the hub for about two hours. Until then its reconnect fails
   with `remote port forwarding failed`.

### Optional: a second path over a tailnet

The first reachable endpoint in `--hub` wins, so a node that leaves the hub's LAN can fall back to
a tailnet address:

```
--hub "hub.lan:22 100.64.0.10:2222"
```

If you use Tailscale SSH in **check** mode on the hub, don't point the fallback at port 22. Check
mode asks for a browser re-auth on new connections, and an unattended tunnel just hangs.
`hub/fleet-sshd.conf.example` and `hub/fleet-sshd.service` run a second, narrow sshd on
`:2222`. It admits one user from one tailnet device, keys only, tunnels only, and only the ports
you list. Fill in the `<...>` placeholders, then:

```bash
sudo install -d /etc/ssh/fleet
sudo install -m 644 hub/fleet-sshd.conf.example /etc/ssh/fleet/sshd_config   # after editing
sudo sshd -t -f /etc/ssh/fleet/sshd_config
sudo install -m 644 hub/fleet-sshd.service /etc/systemd/system/
sudo systemctl enable --now fleet-sshd
```

Controls worth running once:
- from another tailnet device, `ssh -p 2222 fleet@100.64.0.10` must be refused;
- from the allowed device, `ssh -p 2222 ... true` must be refused (no commands);
- from the allowed device, a `-L` to any port not listed in `PermitOpen` must be refused.

## 2. The relay on each node

On the first node:

```bash
relay/install.sh --node laptop-a --port 8801 --peer laptop-b=8802 \
                 --hub "hub.lan:22" --hub-user fleet
```

That generates the shared secret at `~/.fleet-bridge/secret`. Copy it to every other node
**before** installing there:

```bash
ssh laptop-b 'umask 077; mkdir -p ~/.fleet-bridge; cat > ~/.fleet-bridge/secret' < ~/.fleet-bridge/secret
```

On each other node, run the same install with its own name and port, and every peer:

```bash
relay/install.sh --node laptop-b --port 8802 --peer laptop-a=8801 \
                 --hub "hub.lan:22" --hub-user fleet
```

Each install prints a `secret fingerprint`, and it must be the same on every node. The script also:
- registers a `SessionStart` hook in `~/.claude/settings.json`, backing up the file first to
  `settings.json.pre-fleet-bridge`;
- starts `fleet-relay` and `fleet-relay-tunnel` as systemd user units.

On a machine that should keep running with nobody logged in, also run
`sudo loginctl enable-linger $USER`.

Only sessions **started after** the install can receive messages, because the hook registers them.

```bash
~/.fleet-bridge/bin/send.py --peers
#   laptop-b   http://127.0.0.1:8802   up    envelope=sdk_user sessions=1
~/.fleet-bridge/bin/send.py laptop-b "hello from laptop-a"
```

The message appears in laptop-b's newest session as "Another Claude session sent a message", with
a line saying how to reply. `--session <id-prefix>` targets a particular session, and
`/v1/health` on the peer lists the live ones.

Controls:
- from another machine on the LAN, `curl http://<node-ip>:8787/v1/health` must fail;
- a message signed with the wrong secret must get `401`.

## 3. Testing the last hop on a node nobody is sitting at

`relay/live-session.py` opens a real interactive session in a pseudo-terminal, prints
`READY <session-id>` once the session has registered, and exits it after `--seconds`.
`--allow-send` pre-approves `send.py` for that one session, so it can answer.

```bash
ssh laptop-b 'setsid nohup python3 ~/.fleet-bridge/bin/live-session.py \
    --cwd ~/some-repo --seconds 300 --allow-send > ~/.fleet-bridge/live-session.out 2>&1 &'
# wait for READY in live-session.out, then from laptop-a:
~/.fleet-bridge/bin/send.py laptop-b 'reply by running: ~/.fleet-bridge/bin/send.py laptop-a "pong"'
```

Its screen goes to `~/.fleet-bridge/live-session.log`. Things that stop it, found by running it:

| It stops with | Why | Fix |
|---|---|---|
| `NEEDS ONBOARDING` | a token-only account never ran interactive onboarding, so claude shows the login picker (Enter there starts a browser login) | set `"hasCompletedOnboarding": true` in that node's `~/.claude.json` |
| `NEEDS TRUST` | the folder-trust dialog's **default is "No, exit"** | set `projects["<abs path>"].hasTrustDialogAccepted: true` in `~/.claude.json` |
| `EXITED` straight away | `claude` not found, or the token was refused | the harness adds `~/.local/bin` to PATH; check the token |

The harness also answers the terminal's device-attributes query (`ESC[c`) as a VT220 would.
Claude Code holds back **all** keyboard input until the terminal identifies itself, so a bare pty
looks hung.

## 4. Watching it

```bash
tools/fleet-status.py            # one pass, one line per check, exit 1 on any failure
tools/fleet-status.py --probe    # also runs a tiny real fleet-agent task
```

A unit only counts as healthy if it is active **and** its restart counter hasn't moved since the
last pass. A tunnel that restarts every ten seconds still reads `active`. Run it from a timer or
cron, or in a loop while you test something.

### A status endpoint for dashboards

Each relay also answers `GET /v1/status` on its loopback port. The hub reaches every node's relay
at the node's published port, so a dashboard on the hub can read the whole fleet:

```bash
curl -s http://127.0.0.1:8801/v1/status     # on the hub: laptop-a
```

It returns counts, states and percentages only:

| Key | What |
|---|---|
| `sessions` | live registered sessions, `total` and `by_repo`. Git worktrees count under their repo. |
| `units` | `ActiveState` and `NRestarts` for each unit in `config.json` `status.units` (default: the two relay units) |
| `power` | `ac`, `battery` percent, `status`, and `on_battery`. `null` on a machine without a battery or mains supply entry. |
| `load` | `load1`, `cpus`, `per_core`, `mem_used_pct` |
| `delegate` | `fleet-agent` runs in the last hour and day, failures, runs in flight, and the last run's repo, duration and result. Present only when delegation is installed. |
| `accounts` | Claude plan usage per configured account, see below |

It never returns message text, delegated prompts, session tokens, account tokens or the shared
secret, so it needs no signature. A test checks that. Like `/v1/health`, it's reachable only on
loopback and through the ssh tunnel.

#### Account usage

Name the accounts a node should report when you install it:

```bash
relay/install.sh ... --usage you@example.com=claude-login \
                     --usage ops@example.com=token-headers:$HOME/.fleet-bridge/delegate.env
```

- **`claude-login`** reads this machine's Claude Code login (`~/.claude/.credentials.json`) and asks
  the usage API every 5 minutes. It returns every limit the plan has: the 5-hour session, the week,
  and any per-model weekly limit, each with its reset time. It uses the access token only and never
  the refresh token, so it can't disturb Claude Code's login. When the access token has expired, the
  account shows `login-expired` until Claude Code is used on that machine again.
- **`token-headers`** is for a `claude setup-token` token, which the usage API refuses. Every 10
  minutes it sends a one-token request (Haiku by default, `FLEET_PROBE_MODEL` to change it) and reads
  the 5-hour and weekly utilization from the response's rate-limit headers. That's about 150 tokens
  an hour. On an account measured both ways, the two sources agreed.

The usage API and the `anthropic-ratelimit-unified-*` headers are what Claude Code itself reads.
Neither is documented. If either changes, the account shows `error` with a reason, and the last
good numbers stay, with `as_of` giving their age.

## Troubleshooting

| Symptom | Look at |
|---|---|
| peer `DOWN` in `send.py --peers` | `systemctl --user status fleet-relay-tunnel` on both nodes; `journalctl --user -u fleet-relay-tunnel` |
| tunnel restarts, `remote port forwarding failed` | the hub still holds the port for a dead session; the client-alive drop-in clears it within about 60s |
| `401 bad signature` | the secret differs between nodes; compare fingerprints |
| `404 no live registered session` | no session started since the install, Claude Code is older than 2.1.280, or the hook isn't registered |
| message lands in the wrong session | without `--session`, the newest session gets it |
| `503` no envelope | re-run `relay/install.sh` |
