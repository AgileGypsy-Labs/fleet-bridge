#!/usr/bin/env bash
# Install the relay on this node. Idempotent: re-run after changing anything in relay/.
#
#   relay/install.sh --node laptop-a --port 8801 --peer laptop-b=8802 \
#                    --hub "192.0.2.10:22 100.64.0.10:2222" --hub-user fleet
#
#   --node      this node's fleet name. Messages are signed "from" it, so use a name that
#               says which machine/account it is, not necessarily its hostname.
#   --port      the hub loopback port this node's relay is published on. Unique per node.
#   --peer      NAME=PORT for each other node (repeatable).
#   --hub       hub ssh endpoints, preferred first (see hub-ssh.sh).
#   --hub-user  the account on the hub.
#   --usage     LABEL=claude-login  or  LABEL=token-headers:ENV_FILE  (repeatable, optional)
#               Claude accounts whose usage this node reports in GET /v1/status. Replaces
#               the node's list when given. See relay/nodestatus.py for the two sources.
#
# The first node to run this generates the shared secret. Copy it to every other node
# BEFORE running install there, or that node generates its own and every message 401s:
#   ssh <node> 'umask 077; mkdir -p ~/.fleet-bridge; cat > ~/.fleet-bridge/secret' < ~/.fleet-bridge/secret
# The secret is never printed; compare the fingerprint this script prints on each node.
set -euo pipefail
NODE="" PORT="" HUB="" HUB_USER_ARG="" PEERS=() USAGE=()
while [ $# -gt 0 ]; do
  case "$1" in
    --node) NODE="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --peer) PEERS+=("$2"); shift 2 ;;
    --hub) HUB="$2"; shift 2 ;;
    --hub-user) HUB_USER_ARG="$2"; shift 2 ;;
    --usage) USAGE+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done
[ -n "$NODE" ] && [ -n "$PORT" ] && [ -n "$HUB" ] && [ -n "$HUB_USER_ARG" ] && [ "${#PEERS[@]}" -gt 0 ] \
  || { echo "need --node, --port, --hub, --hub-user and at least one --peer (see --help)" >&2; exit 2; }
case "$PORT" in *[!0-9]*) echo "--port must be a number" >&2; exit 2 ;; esac

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="$HOME/.fleet-bridge"
BIN="$HOME_DIR/bin"
UNITS="$HOME/.config/systemd/user"

mkdir -p "$BIN" "$HOME_DIR/sessions" "$UNITS"
chmod 700 "$HOME_DIR" "$HOME_DIR/sessions"
install -m 0644 "$SRC/socketio.py" "$SRC/nodestatus.py" "$BIN/"
install -m 0755 "$SRC/agent.py" "$SRC/send.py" "$SRC/probe.py" "$SRC/live-session.py" "$BIN/"
install -m 0755 "$SRC/hooks/session-start.sh" "$BIN/session-start.sh"
install -m 0755 "$SRC/hub-ssh.sh" "$BIN/hub-ssh"

umask 077
if [ ! -f "$HOME_DIR/secret" ]; then
  openssl rand -hex 32 > "$HOME_DIR/secret"
  echo "generated $HOME_DIR/secret"
fi
chmod 600 "$HOME_DIR/secret"
echo "secret fingerprint: $(sha256sum "$HOME_DIR/secret" | cut -c1-12)   (must match on every node)"

args="-R 127.0.0.1:$PORT:127.0.0.1:8787"
peers_json="{"
for p in "${PEERS[@]}"; do
  name="${p%%=*}"; pport="${p#*=}"
  case "$pport" in ''|*[!0-9]*) echo "bad --peer $p (want NAME=PORT)" >&2; exit 2 ;; esac
  args="$args -L 127.0.0.1:$pport:127.0.0.1:$pport"
  peers_json="$peers_json\"$name\": \"http://127.0.0.1:$pport\", "
done
peers_json="${peers_json%, }}"
printf 'HUB_ENDPOINTS=%s\nHUB_USER=%s\nTUNNEL_ARGS=%s\n' "$HUB" "$HUB_USER_ARG" "$args" > "$HOME_DIR/tunnel.env"
printf '%s\n' "$peers_json" | python3 -m json.tool > "$HOME_DIR/peers.json"

python3 - "$HOME_DIR/config.json" "$NODE" "${USAGE[@]}" <<'PY'
import json, os, sys
path, node, usage = sys.argv[1], sys.argv[2], sys.argv[3:]
try:
    cfg = json.load(open(path))
except (OSError, ValueError):
    cfg = {}
cfg["node"] = node
cfg.setdefault("envelope", "sdk_user")   # docs/inbox-socket.md; re-check with probe.py
if usage:
    accounts = []
    for spec in usage:
        label, _, src = spec.partition("=")
        source, _, env_file = src.partition(":")
        if not label or source not in ("claude-login", "token-headers") or (source == "token-headers") != bool(env_file):
            sys.exit("bad --usage %r: want LABEL=claude-login or LABEL=token-headers:ENV_FILE" % spec)
        accounts.append({"label": label, "source": source, **({"env_file": env_file} if env_file else {})})
    cfg["accounts"] = accounts
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(cfg, fh, indent=2)
PY

# Register the SessionStart hook without disturbing any other hook. Backs up once.
python3 - "$HOME/.claude/settings.json" "$BIN/session-start.sh" <<'PY'
import json, os, shutil, sys
path, command = sys.argv[1], sys.argv[2]
try:
    settings = json.load(open(path))
except FileNotFoundError:
    settings = {}
entries = settings.setdefault("hooks", {}).setdefault("SessionStart", [])
if any(h.get("command") == command for e in entries for h in e.get("hooks", [])):
    print("SessionStart hook already registered")
else:
    if os.path.exists(path) and not os.path.exists(path + ".pre-fleet-bridge"):
        shutil.copy2(path, path + ".pre-fleet-bridge")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    entries.append({"hooks": [{"type": "command", "command": command}]})
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(settings, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    print("registered SessionStart hook in", path)
PY

install -m 0644 "$SRC/fleet-relay.service" "$SRC/fleet-relay-tunnel.service" "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable fleet-relay.service fleet-relay-tunnel.service >/dev/null 2>&1
systemctl --user restart fleet-relay.service fleet-relay-tunnel.service
sleep 3
for u in fleet-relay fleet-relay-tunnel; do printf '%-20s %s\n' "$u" "$(systemctl --user is-active $u)"; done
echo
echo "Only Claude Code sessions started from now on can receive (the hook registers them)."
echo "For a node that must keep running unattended: sudo loginctl enable-linger $USER"
echo "check:  $BIN/send.py --peers"
