#!/usr/bin/env bash
# Claude Code SessionStart hook. Registers this session so the relay can deliver into it.
#
# Claude Code exports CLAUDE_CODE_MESSAGING_SOCKET and CLAUDE_CODE_MESSAGING_TOKEN
# before any hook runs, including SessionStart, and each session exports its own socket
# rather than inheriting a parent's. If the variables are empty this session has no
# inbox (see `/status` -> Peer address) and there is nothing to register.
set -euo pipefail

REG="$HOME/.fleet-bridge/sessions"
NODE="${FLEET_NODE:-$(python3 -c 'import json,os
try: print(json.load(open(os.path.expanduser("~/.fleet-bridge/config.json")))["node"])
except Exception: print(os.uname().nodename)' 2>/dev/null)}"

[ -n "${CLAUDE_CODE_MESSAGING_SOCKET:-}" ] || exit 0

# Never register a headless job. A relay message landing in one becomes an extra user turn
# in the middle of someone's automated run. `claude --print` sessions on 2.1.280 have no
# inbox anyway; this keeps it that way if a later version gives them one.
#   FLEET_NO_REGISTER=1   set it in any runner's environment to opt out
#   FLEET_AGENT=1         delegate/fleet-agent runs
#   FACTORY_JOB_ID        job runs of github.com/owainlewis/factory
[ -z "${FLEET_NO_REGISTER:-}${FLEET_AGENT:-}${FACTORY_JOB_ID:-}" ] || exit 0

payload=$(cat || true)
sid=$(printf '%s' "$payload" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("session_id",""))
except Exception: print("")' 2>/dev/null || true)
[ -n "$sid" ] || sid="pid-$PPID"

mkdir -p "$REG"
python3 - "$REG" "$sid" "$NODE" <<'PY'
import json, os, sys, time
reg, sid, node = sys.argv[1], sys.argv[2], sys.argv[3]
entry = {
    "session_id": sid,
    "node": node,
    "socket": os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET", ""),
    "token": os.environ.get("CLAUDE_CODE_MESSAGING_TOKEN", ""),
    "cwd": os.getcwd(),
    "pid": os.getppid(),
    "registered_at": time.time(),
}
path = os.path.join(reg, sid + ".json")
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(entry, fh)
cur = os.path.join(reg, "current.json")
tmp = cur + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(entry, fh)
os.replace(tmp, cur)
PY
exit 0
