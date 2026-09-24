#!/usr/bin/env bash
# delegate/install.sh --account LABEL [--env-file PATH] REPO...
#
# Installs fleet-agent and routes agent work in each REPO (and anything below it,
# worktrees included) to the delegate account.
#
#   --account   a label for the delegate account, shown in every result line and refusal
#               (e.g. its email). Not a secret.
#   --env-file  file holding CLAUDE_CODE_OAUTH_TOKEN=<token>, mode 0600.
#               Default ~/.fleet-bridge/delegate.env. Make the token with `claude setup-token`
#               while logged in as the delegate account (docs/delegate.md).
#
# Sets "delegate" in ~/.fleet-bridge/config.json (repos = exactly the REPOs given) and adds
# the guard to ~/.claude/settings.json once (backup: settings.json.pre-fleet-agent).
# Running sessions keep the hooks they started with; new sessions get the guard.
set -euo pipefail
ACCOUNT="" ENV_FILE="$HOME/.fleet-bridge/delegate.env" REPOS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --account) ACCOUNT="$2"; shift 2 ;;
    --env-file) ENV_FILE="$2"; shift 2 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) REPOS+=("$(cd "$1" && pwd)"); shift ;;
  esac
done
[ -n "$ACCOUNT" ] || { echo "need --account LABEL (see --help)" >&2; exit 2; }

here=$(cd "$(dirname "$0")" && pwd)
bin="$HOME/.fleet-bridge/bin"
mkdir -p "$bin"
chmod 700 "$HOME/.fleet-bridge"
install -m 755 "$here/fleet-agent.py" "$bin/fleet-agent"
install -m 755 "$here/agent-guard.sh" "$bin/agent-guard.sh"

if [ ! -s "$ENV_FILE" ]; then
  echo "warning: $ENV_FILE is missing or empty; fleet-agent refuses to run until it holds the token" >&2
elif [ "$(stat -c %a "$ENV_FILE")" != 600 ]; then
  echo "warning: $ENV_FILE should be mode 600 (fleet-agent refuses otherwise)" >&2
fi

python3 - "$HOME/.fleet-bridge/config.json" "$ACCOUNT" "$ENV_FILE" "${REPOS[@]}" <<'PY'
import json, os, sys
path, account, env_file, repos = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
d = json.load(open(path)) if os.path.exists(path) else {}
d["delegate"] = {"account": account, "env_file": env_file, "repos": repos}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as fh:
    json.dump(d, fh, indent=2)
print("delegate account:", account)
print("repos routed to it:", *(repos or ["(none: fleet-agent works, nothing is guarded)"]), sep="\n  ")
PY

settings="$HOME/.claude/settings.json"
mkdir -p "$(dirname "$settings")"
[ -f "$settings" ] || echo '{}' > "$settings"
[ -f "$settings.pre-fleet-agent" ] || cp -p "$settings" "$settings.pre-fleet-agent"
python3 - "$settings" "$bin/agent-guard.sh" <<'PY'
import json, sys
path, guard = sys.argv[1], sys.argv[2]
d = json.load(open(path))
pre = d.setdefault("hooks", {}).setdefault("PreToolUse", [])
if not any(h.get("command") == guard for m in pre for h in m.get("hooks", [])):
    pre.append({"matcher": "Agent|Task", "hooks": [{"type": "command", "command": guard}]})
json.dump(d, open(path, "w"), indent=2)
print("guard hook in", path)
PY
