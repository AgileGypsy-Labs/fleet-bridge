#!/usr/bin/env bash
# git pre-commit hook. Refuses a commit that touches paths this node does not own.
# Install:  ln -sf /path/to/fleet-bridge/workspace/bin/ownership-guard.sh .git/hooks/pre-commit
# Bypass:   git commit --no-verify   (deliberate, visible, and your problem)
set -uo pipefail

NODE="${FLEET_NODE:-$(python3 -c 'import json,os
try: print(json.load(open(os.path.expanduser("~/.fleet-bridge/config.json")))["node"])
except Exception: print(os.uname().nodename)' 2>/dev/null)}"
ROOT="$(git rev-parse --show-toplevel)"
REG="$ROOT/OWNERSHIP.toml"
[ -f "$REG" ] || exit 0

mapfile -t STAGED < <(git diff --cached --name-only --diff-filter=ACMR)
[ "${#STAGED[@]}" -gt 0 ] || exit 0

exec python3 - "$REG" "$NODE" "${STAGED[@]}" <<'PY'
import fnmatch, re, sys

reg, node, paths = sys.argv[1], sys.argv[2], sys.argv[3:]
text = open(reg).read()

rules = []
for block in re.findall(r"\[\[rule\]\](.*?)(?=\[\[rule\]\]|\Z)", text, re.S):
    globs = re.search(r"paths\s*=\s*\[(.*?)\]", block, re.S)
    owner = re.search(r'owner\s*=\s*"([^"]+)"', block)
    if globs and owner:
        rules.append((re.findall(r'"([^"]+)"', globs.group(1)), owner.group(1)))


def owner_of(path):
    for globs, owner in rules:
        for g in globs:
            if fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", "")):
                return owner, g
    return "any", "(default)"


bad = [(p,) + owner_of(p) for p in paths if owner_of(p)[0] not in ("any", node)]
if bad:
    print("\ncommit refused: this node (%s) does not own these paths\n" % node)
    for p, owner, pattern in bad:
        print("  %-52s owned by %-10s via %s" % (p, owner, pattern))
    print("\nHand the work to that node with relay/send.py, or reassign the path in")
    print("OWNERSHIP.toml as a deliberate change. --no-verify bypasses this.\n")
    sys.exit(1)
PY
