#!/usr/bin/env bash
# What the Vast.ai template runs at boot.
#
# The template itself only clones this repo and execs this file, so the launch
# logic stays here where it can be read and diffed -- and so editing it does not
# churn the template (Vast rotates a template's hash_id on every edit).
#
# Everything runs in ONE container: llama.cpp on localhost, the app on 8501.
set -x
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get install -y -qq python3-pip curl
pip3 install -q --break-system-packages -r /opt/app/requirements.txt \
  || pip3 install -q -r /opt/app/requirements.txt

curl -sSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

# llama-server needs LD_LIBRARY_PATH: /app holds the binary AND its shared
# objects, and it dies on libllama-server-impl.so without this.
#
# It binds 127.0.0.1 deliberately. llama.cpp ships CORS open to * with no API
# key, so publishing this port would hand anyone a free GPU. Only 8501 is
# exposed; Streamlit reaches the model over localhost.
( cd /app && LD_LIBRARY_PATH=/app ./llama-server \
    --host 127.0.0.1 --port 8000 -ngl 99 -c 16384 \
    -hf unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M > /var/log/llama.log 2>&1 ) &

# Quick tunnel for an HTTPS link. The hostname is random on every start, so
# nothing caches it -- app.py reads the tail of this log at render time.
( cloudflared tunnel --url http://127.0.0.1:8501 --no-autoupdate \
    --logfile /tmp/cf.log > /var/log/cf.log 2>&1 ) &

( cd /opt/app && MODEL_BASE_URL=http://127.0.0.1:8000/v1 streamlit run app.py \
    --server.port 8501 --server.address 0.0.0.0 --server.headless true \
    --browser.gatherUsageStats false --server.enableCORS false \
    --server.enableXsrfProtection false > /var/log/streamlit.log 2>&1 ) &
