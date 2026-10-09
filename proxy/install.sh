#!/usr/bin/env bash
# proxy/install.sh [--listen 127.0.0.1:8899] [--upstream https://api.anthropic.com]
#
# Installs the quota proxy on this node as a user unit (quota-proxy.service), writes the
# "proxy" block of ~/.fleet-bridge/config.json (kept when it already exists), creates the
# device key the node's sessions present (~/.fleet-bridge/proxy.key, 0600) and the directory
# for the accounts' setup-token files (~/.fleet-bridge/accounts, 0700).
#
# Then, for each account the proxy may swap in, add "token_file" to its entry under
# "delegate" -> "accounts" in the config and put CLAUDE_CODE_OAUTH_TOKEN=<setup-token> in that
# file (0600). New sessions started through delegate-session, and fleet-agent runs, use the
# proxy from then on (route.proxy_env). `quota-proxy --off` turns rotation off without a restart.
set -euo pipefail
LISTEN="" UPSTREAM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --listen) LISTEN="$2"; shift 2 ;;
    --upstream) UPSTREAM="$2"; shift 2 ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
done
here=$(cd "$(dirname "$0")" && pwd)
home="$HOME/.fleet-bridge"
bin="$home/bin"
units="$HOME/.config/systemd/user"
mkdir -p "$bin" "$home/accounts" "$units"
chmod 700 "$home" "$home/accounts"
install -m 755 "$here/quota-proxy.py" "$bin/quota-proxy.py"
install -m 644 "$here/../delegate/route.py" "$bin/route.py"
ln -sf "$bin/quota-proxy.py" "$bin/quota-proxy"
umask 077
if [ ! -s "$home/proxy.key" ]; then
  openssl rand -hex 24 > "$home/proxy.key"
  echo "generated $home/proxy.key"
fi
chmod 600 "$home/proxy.key"

python3 - "$home/config.json" "$LISTEN" "$UPSTREAM" <<'PY'
import json, os, sys
path, listen, upstream = sys.argv[1:4]
try:
    cfg = json.load(open(path))
except (OSError, ValueError):
    cfg = {}
p = cfg.setdefault("proxy", {})
p.setdefault("listen", "127.0.0.1:8899")
p.setdefault("upstream", "https://api.anthropic.com")
p.setdefault("switch_at", 90)
p.setdefault("hysteresis", 5)
if listen:
    p["listen"] = listen
if upstream:
    p["upstream"] = upstream
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(cfg, fh, indent=2)
    fh.write("\n")
have = [a.get("label") for a in (cfg.get("delegate") or {}).get("accounts") or [] if a.get("token_file")]
print("proxy listens on", p["listen"], "->", p["upstream"])
print("accounts with a token_file:", ", ".join(have) if have else "none yet (the proxy passes everything through)")
PY

install -m 0644 "$here/quota-proxy.service" "$units/"
systemctl --user daemon-reload
systemctl --user enable quota-proxy.service >/dev/null 2>&1
systemctl --user restart quota-proxy.service
sleep 1
printf '%-20s %s\n' quota-proxy "$(systemctl --user is-active quota-proxy)"
echo "check:  curl -s -o /dev/null -w '%{http_code}\n' http://${LISTEN:-127.0.0.1:8899}/v1/models   (any answer from upstream)"
