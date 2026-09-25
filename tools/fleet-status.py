#!/usr/bin/env python3
"""One pass over every part of the fleet, each checked by what it actually does.

    fleet-status.py [--probe] [--quiet]

Prints one line per check (OK / FAIL) and exits 1 if any failed. --probe also runs a
real fleet-agent task on the delegate account (a few seconds, a few tokens).
--quiet prints only failures and the summary line. Run it from cron or a timer.

Optional settings in ~/.fleet-bridge/config.json, all with working defaults:
    "status": {
      "units":         ["fleet-relay", "fleet-relay-tunnel"],   # user units on this node
      "hub_ssh":       "fleet@hub",           # check the hub's system units over ssh
      "hub_units":     ["ssh"],
      "peer_ssh":      {"laptop-b": "me@laptop-b"},   # check a peer's user units over ssh
      "peer_units":    ["fleet-relay", "fleet-relay-tunnel"],
      "control_plane": "http://127.0.0.1:7337/api/v1" # an owainlewis/factory control plane:
    }                                                 #   every worker online and healthy
The relay checks go through the tunnels, so they cover the hub and peers even without
any ssh settings.

A unit only counts as healthy if it is active AND its restart counter has not moved
since the previous pass (~/.fleet-bridge/status.state). A unit that is "active" but
flapping is exactly what an is-active check would miss.
"""
import json
import os
import stat
import subprocess
import sys
import time
import urllib.request

HOME = os.path.expanduser("~/.fleet-bridge")
CFG = os.path.join(HOME, "config.json")
STATE = os.path.join(HOME, "status.state")
DEFAULT_UNITS = ["fleet-relay", "fleet-relay-tunnel"]
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))


def http_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.load(r)


def unit_show(units, user=True, ssh=None):
    """{unit: (ActiveState, NRestarts)} locally or over ssh."""
    cmd = ["systemctl"] + (["--user"] if user else []) + ["show", "-p", "Id,ActiveState,NRestarts"] + units
    if ssh:
        cmd = SSH + [ssh, " ".join(cmd)]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    info = {}
    for block in out.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if "Id" in kv:
            info[kv["Id"].removesuffix(".service")] = (kv.get("ActiveState"), int(kv.get("NRestarts") or 0))
    return info


def units(label, names, prev, now, user=True, ssh=None):
    try:
        info = unit_show(names, user, ssh)
    except (subprocess.SubprocessError, OSError) as exc:
        check(label + " units", False, "unreachable: %s" % exc)
        return
    if not info:
        check(label + " units", False, "no answer" + (" from " + ssh if ssh else ""))
        return
    for name in names:
        state, restarts = info.get(name, ("missing", 0))
        key = "%s/%s" % (label, name)
        before = prev.get(key)
        now[key] = restarts
        moved = before is not None and restarts > before
        check("%s %s" % (label, name), state == "active" and not moved,
              "%s, restarts %d%s" % (state, restarts, " (+%d since last pass)" % (restarts - before) if moved else ""))


def main():
    probe, quiet = "--probe" in sys.argv, "--quiet" in sys.argv
    cfg = json.load(open(CFG)) if os.path.exists(CFG) else {}
    node = cfg.get("node", "?")
    st = cfg.get("status", {})
    try:
        prev = json.load(open(STATE))
    except (OSError, ValueError):
        prev = {}
    now = {}

    local_units = st.get("units", DEFAULT_UNITS)
    units(node, local_units, prev, now)

    control = st.get("control_plane")
    try:
        if not control:
            raise LookupError
        _, data = http_json(control.rstrip("/") + "/workers")
        workers = data.get("workers", data) if isinstance(data, dict) else data
        for w in workers:
            check("worker " + w.get("name", w.get("id", "?")[:8]),
                  w.get("online") and w.get("health") == "healthy",
                  "online=%s health=%s" % (w.get("online"), w.get("health")))
        if not workers:
            check("workers", False, "control plane lists none")
    except LookupError:
        pass
    except Exception as exc:  # noqa: BLE001
        check("control plane", False, str(exc)[:120])

    try:
        _, h = http_json("http://127.0.0.1:8787/v1/health")
        check("relay " + node, h.get("ok"), "%d live session(s)" % len(h.get("sessions", [])))
    except Exception as exc:  # noqa: BLE001
        check("relay " + node, False, str(exc)[:120])
    peers = json.load(open(os.path.join(HOME, "peers.json"))) if os.path.exists(os.path.join(HOME, "peers.json")) else {}
    for peer, url in peers.items():
        try:
            _, h = http_json(url.rstrip("/") + "/v1/health")
            check("relay %s (through the hub)" % peer, h.get("ok"),
                  "%d live session(s)" % len(h.get("sessions", [])))
        except Exception as exc:  # noqa: BLE001
            check("relay %s (through the hub)" % peer, False, str(exc)[:120])

    if st.get("hub_ssh") and st.get("hub_units"):
        units("hub", st["hub_units"], prev, now, user=False, ssh=st["hub_ssh"])
    for peer, target in st.get("peer_ssh", {}).items():
        units(peer, st.get("peer_units", local_units), prev, now, ssh=target)

    token = os.path.expanduser(cfg.get("delegate", {}).get("env_file") or "~/.fleet-bridge/delegate.env")
    if os.path.exists(token):
        mode = stat.S_IMODE(os.stat(token).st_mode)
        check("delegate token", mode == 0o600, "mode %o" % mode)
        log = os.path.join(HOME, "delegate.log")
        recent = []
        if os.path.exists(log):
            for line in open(log).readlines()[-200:]:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("t", 0) > time.time() - 3600:
                    recent.append(e)
        failed = [e for e in recent if not e.get("ok")]
        check("delegate runs (last hour)", not failed,
              "%d run(s), %d failed%s" % (len(recent), len(failed),
                                          (": " + failed[-1].get("subtype", "?")) if failed else ""))
        if probe:
            r = subprocess.run([os.path.join(HOME, "bin", "fleet-agent"), "--model", "haiku", "--cwd", "/tmp",
                                "--minutes", "3", "Reply with exactly: FLEET-OK"],
                               capture_output=True, text=True)
            check("delegate probe", r.returncode == 0 and "FLEET-OK" in r.stdout,
                  (r.stderr.strip().splitlines() or ["no output"])[-1])

    json.dump(now, open(STATE, "w"))
    bad = [r for r in results if not r[1]]
    for name, ok, detail in results:
        if ok and quiet:
            continue
        print("%-4s %-34s %s" % ("OK" if ok else "FAIL", name, detail))
    print("%s %d/%d checks ok" % (time.strftime("%H:%M:%S"), len(results) - len(bad), len(results)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
