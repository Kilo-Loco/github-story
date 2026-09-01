#!/usr/bin/env bash
# What's my instance doing?
#
# There is no SSH on this box on purpose: launching with --entrypoint puts us in
# Vast's "args" mode, and Vast only injects sshd for ssh/jupyter mode. So we read
# the instance telemetry instead. `vastai logs` is NOT a substitute -- it serves a
# cached snapshot that can sit frozen for the entire model download.
#
# usage: ./status.sh [--watch]

set -euo pipefail

read_stats() {
  vastai show instances --raw 2>/dev/null | python3 -c '
import json, sys
rows = json.load(sys.stdin)
if not rows:
    print("no instances"); raise SystemExit
d = rows[0]
disk = d.get("disk_usage") or 0
vram = d.get("vmem_usage") or 0
net  = (d.get("inet_down_billed") or 0) / 1e6
port = next((x["HostPort"] for v in (d.get("ports") or {}).values() for x in v), None)

# vmem is the honest readiness signal: ~0.5GB while downloading, ~19.6GB once
# the weights are resident on the GPU.
if vram > 15:   state = "READY - model resident on GPU"
elif disk > 19: state = "loading weights into VRAM"
else:           state = "downloading weights"

iid, st, rate = d.get("id"), d.get("actual_status"), d.get("dph_total", 0)
ip = d.get("public_ipaddr")
print("instance  {}  [{}]  ${:.4f}/hr".format(iid, st, rate))
print("app       http://{}:{}".format(ip, port) if port else "app       (port not mapped yet)")
print("disk      {:.1f} GB / ~21 GB".format(disk))
print("vram      {:.2f} GB / ~19.6 GB when loaded".format(vram))
print("network   {:.2f} GB pulled".format(net))
print("state     " + state)
'
}

if [ "${1:-}" = "--watch" ]; then
  while true; do clear; date +%H:%M:%S; read_stats; sleep 20; done
else
  read_stats
fi
