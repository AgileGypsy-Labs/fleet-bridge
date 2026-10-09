# The quota proxy: moving a running session between your accounts

Claude Code picks its credential when it starts and never re-reads it, so a session that
began on one account stays there, however the other account's week looks. The quota proxy
(`proxy/quota-proxy.py`) takes that decision per request instead. It listens on loopback,
Claude Code is pointed at it with `ANTHROPIC_BASE_URL`, and each inference request goes
upstream with the setup-token of whichever of your accounts has the most headroom for the
request's model. Everything else a session sends (profile, usage, Remote Control) passes
through on the session's own login, untouched.

This is a gray area. Anthropic documents neither the rate-limit headers nor rotation between
plans, and its consumer terms forbid sharing an account with anyone else. The proxy is for
one person's own accounts on that person's own machines: it binds to loopback only, every
client presents a device key, and `quota-proxy --off` makes it a plain pass-through in one
command. Decide for yourself whether to run it.

## How it decides

- **The ledger.** From the Caravan hub (`route` in the config), every `HUB_EVERY` seconds: each
  account's windows as the relays sampled them, per-model windows included. Overlaid with the
  `anthropic-ratelimit-unified-*` headers the proxy sees itself on every answer, which are
  fresher but carry only the 5-hour and 7-day windows. Without a hub, the headers alone drive it.
- **Sticky, then the line.** A session (keyed by the credential it sends) stays on the account it
  is on until that account's worst applicable window reaches `switch_at` (90%). It then moves to
  the best other account that is under `switch_at` minus `hysteresis` (85%), and moves again only
  by the same rule. A window that resets within 15 minutes counts as empty, as in the hub's
  policy. Nothing known about an account counts as 50%.
- **A quota 429** (unified status `rejected`, as opposed to a burst 429 with a `retry-after`)
  marks that window in force until its reset and the request is retried once on the best other
  account. Any other 429 or error is returned as it came.
- **A 401 on a swapped-in token** (revoked, expired) is never shown to the session: the request is
  sent again as it came, on the session's own login, and that token is left alone for 10 minutes.
- **Pass-through** when the proxy is off, when the request carries no device key, for anything
  that is not an inference request, and when every account is over the line.

The session's own login is the fallback at every step: the proxy cannot refuse a request the
session could have made directly.

## Setup

```bash
proxy/install.sh                       # unit, device key, accounts dir, "proxy" config block
```

Then give each account a setup-token file and name it in the config:

```bash
# logged in as that account, on any machine:
claude setup-token                     # prints a token good for a year
# on the node:
(umask 077; printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' '<token>' > ~/.fleet-bridge/accounts/second.env)
```

```json
"delegate": {"accounts": [
  {"label": "you@example.com", "config_dir": "~/.claude", "own": true,
   "token_file": "~/.fleet-bridge/accounts/you.env"},
  {"label": "second@example.com", "config_dir": "~/.claude-second",
   "token_file": "~/.fleet-bridge/accounts/second.env"}
]}
```

Sessions started through `delegate-session` and `fleet-agent` runs get `ANTHROPIC_BASE_URL`
and the device key (`route.proxy_env`) whenever the proxy is configured, switched on and
listening; a proxy that is down costs nothing, the session goes direct. Caravan and Factory
workers get the same two variables from their unit files.

```bash
~/.fleet-bridge/bin/quota-proxy --off      # pass-through, no restart; --on to resume; --status
tail -f ~/.fleet-bridge/proxy.log          # one line per request: labels and numbers, never a token
```

## What the hub sees

Every header the proxy reads goes to the hub's `POST /v1/proxy-events` (node, account, window,
utilization, reset, status, HTTP status), so the hub's `/v1/accounts` and the dashboard show the
proxy's view beside the relays' samples. With rotation on, a job's `account` field is the
account it started on; the proxy's audit log is the usage truth.

## Limits

- Streaming is passed through chunk by chunk (`HTTPResponse.read1`), so a stream reaches the
  session event by event; a session that disconnects mid-answer drops the upstream connection
  too, which ends the generation.
- Loopback only: `proxy.listen` must be a loopback address, and the proxy refuses to start on
  anything else.
- Tokens at rest are plain files (0600). Rotate them yearly; revoke at claude.ai on suspicion.
