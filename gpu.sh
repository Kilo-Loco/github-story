#!/usr/bin/env bash
# Talk to the running instance directly over SSH.
#
# Vast's telemetry is unreliable -- some hosts report disk_usage -1 and no
# network counters at all -- and `vastai logs` serves a cached snapshot that can
# sit frozen through an entire 18.56GB download. SSH is the only channel that
# tells the truth, which is why the boot script installs sshd.
#
# usage: ./gpu.sh [status|watch|logs|shell|forward]

set -uo pipefail
cd "$(dirname "$0")"

read -r IP SSHP APPP <<<"$(vastai show instances --raw 2>/dev/null | python3 -c '
import json, sys
rows = json.load(sys.stdin)
if not rows:
    print("  "); raise SystemExit
d = rows[0]
ports = d.get("ports") or {}
ssh = next((x["HostPort"] for x in ports.get("22/tcp", [])), "")
app = next((x["HostPort"] for x in ports.get("8501/tcp", [])), "")
print(d.get("public_ipaddr",""), ssh, app)
')"

[ -z "${IP:-}" ] && { echo "no instance"; exit 1; }

SSH="ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=10 -i $HOME/.ssh/id_ed25519 -p $SSHP root@$IP"

status() {
  echo "app   http://$IP:$APPP"
  [ -f tunnel_url.txt ] && echo "https $(cat tunnel_url.txt)"
  $SSH '
    # awk, not bc -- bc is not in this image and the failure is silent.
    MB=$(du -sm /root/.cache/huggingface 2>/dev/null | cut -f1); MB=${MB:-0}
    awk -v m="$MB" "BEGIN{printf \"model %.2f GB / 18.56 GB (%d%%)\n\", m/1024, m*100/19005}"
    echo "vram  $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)"
    echo "up    $(ps -eo comm,etime | grep -E "llama-server|streamlit|cloudflared" | tr -s " " | tr "\n" " ")"
    curl -s -o /dev/null -w "llama %{http_code}\n" -m 3 http://127.0.0.1:8000/health 2>/dev/null || echo "llama down"
    grep -oh "https://[a-z0-9-]*\.trycloudflare\.com" /tmp/cf.log 2>/dev/null | head -1
  ' 2>/dev/null
}

case "${1:-status}" in
  status)  status ;;
  watch)   while true; do clear; date +%H:%M:%S; status; sleep 20; done ;;
  logs)    $SSH 'tail -40 /tmp/cf.log; echo; ps -eo comm,etime | head' ;;
  shell)   exec $SSH ;;
  # Forward the model API to localhost so prompts can be tuned from the laptop
  # without a rebuild. llama.cpp stays bound to 127.0.0.1 on the box.
  forward) echo "model API -> http://127.0.0.1:8000/v1 (ctrl-c to stop)"; exec $SSH -N -L 8000:127.0.0.1:8000 ;;
  *)       echo "usage: $0 [status|watch|logs|shell|forward]"; exit 1 ;;
esac
