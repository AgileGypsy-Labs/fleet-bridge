# Security

fleet-bridge writes into live Claude Code sessions and runs agents under a second account, so
holes matter. Please report one privately through GitHub's
[private vulnerability reporting](https://github.com/AgileGypsy-Labs/fleet-bridge/security/advisories/new)
rather than in a public issue.

In scope:
- anything that gets a message into a session without the shared secret;
- a replay that gets past the relay;
- a relay or hub port reachable from a network address;
- `fleet-agent` running with a credential other than the delegate token, or writing in read-only
  mode;
- the guard failing to fire inside a configured repo.

The threat model is in the README under "Security model". Anyone who already has your shell on a
node, or your secret, is out of scope: they can already do everything fleet-bridge can.
