#!/usr/bin/env bash
# ssh to the fleet hub carrying the given forwards, trying each endpoint in order.
# Installed as ~/.fleet-bridge/bin/hub-ssh; fleet-relay-tunnel.service runs it.
#
#   hub-ssh -R 127.0.0.1:8801:127.0.0.1:8787 -L 127.0.0.1:8802:127.0.0.1:8802
#
# HUB_ENDPOINTS: space-separated host:port list, preferred first, e.g.
#   "192.0.2.10:22 100.64.0.10:2222"   (hub LAN sshd, then hub/fleet-sshd on the tailnet)
# HUB_USER: the account on the hub. Both come from ~/.fleet-bridge/tunnel.env.
#
# An endpoint that fails fast (unreachable, refused, wrong host key, forward refused)
# moves on to the next. One that ran for a while was connected and then dropped: exit and
# let systemd restart from the preferred endpoint.
set -u
: "${HUB_ENDPOINTS:?set HUB_ENDPOINTS (host:port ...) in ~/.fleet-bridge/tunnel.env}"
: "${HUB_USER:?set HUB_USER in ~/.fleet-bridge/tunnel.env}"
rc=1
for ep in $HUB_ENDPOINTS; do
  host="${ep%:*}"; port="${ep##*:}"
  start=$(date +%s)
  /usr/bin/ssh -NT -p "$port" \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    -o ExitOnForwardFailure=yes \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=3 \
    "$@" "$HUB_USER@$host"
  rc=$?
  ran=$(( $(date +%s) - start ))
  [ "$ran" -ge 30 ] && exit "$rc"
  echo "hub-ssh: $ep failed after ${ran}s (rc=$rc)" >&2
done
exit "$rc"
