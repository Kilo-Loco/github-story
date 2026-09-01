# Deploying the model on Vast.ai

Serving `unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF` (Q4_K_M, 18.56 GB) on a
single RTX 4090 with llama.cpp, exposing an OpenAI-compatible API.

Every number below was measured on this exact setup, not estimated.

---

## Daily driver: stop, don't destroy

A **stopped** instance keeps its disk, so the 18.56 GB of weights stay
downloaded. This is almost always what you want between sessions.

| State     | Cost/day | Time to serving |
|-----------|----------|-----------------|
| Running   | $9.04    | —               |
| Stopped   | **$0.40**| ~1-2 min        |
| Destroyed | $0       | ~25 min         |

```bash
vastai stop instance  <ID>     # end of session — weights survive
vastai start instance <ID>     # next session — no re-download
```

The one risk: a stopped instance does not reserve the GPU. If another renter
takes the machine while you sleep, `start` fails and you rebuild from scratch
with the template below. At $0.40/night that is a bet worth taking, but do not
leave it stopped the night before a demo — start it early enough to fail over.

---

## Rebuilding from scratch

### Option A — the saved template

Template `github-story-qwen3-coder-30b` (id `658939`) carries the image,
the `LLAMA_ARG_*` settings, 60 GB disk, and a 4090 search filter. Pick an
offer in the Vast UI and launch it.

### Option B — one box, both services (what's actually deployed)

The model API *and* the Streamlit app run in a single container. No second
instance, no cross-instance networking, and llama.cpp never touches the public
internet.

The trick is `--entrypoint`. The `llama.cpp:server-cuda` image has
`llama-server` as its ENTRYPOINT, so by default `--args` are arguments to
llama-server — which is why a stray `--raw` crash-loops the container. Override
the entrypoint to `/bin/bash` and `--args -c "..."` becomes a shell script:

```bash
GHT=$(grep '^GITHUB_TOKEN=' .env | cut -d= -f2)

BOOT='set -x; ls -la /app || true;
  export DEBIAN_FRONTEND=noninteractive;
  apt-get update -qq; apt-get install -y -qq python3-pip git;
  git clone --depth 1 https://github.com/Kilo-Loco/github-story /opt/app;
  pip3 install -q --break-system-packages -r /opt/app/requirements.txt
    || pip3 install -q -r /opt/app/requirements.txt;
  export MODEL_BASE_URL=http://127.0.0.1:8000/v1;
  ( cd /app && LD_LIBRARY_PATH=/app ./llama-server
      --host 127.0.0.1 --port 8000 -ngl 99 -c 16384
      -hf unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M 2>&1
      | sed "s/^/[llama] /" ) &
  cd /opt/app;
  exec streamlit run app.py --server.port 8501 --server.address 0.0.0.0
      --server.headless true --browser.gatherUsageStats false'

vastai create instance <OFFER_ID> \
  --image ghcr.io/ggml-org/llama.cpp:server-cuda \
  --disk 60 \
  --env "-p 8501:8501 -e GITHUB_TOKEN=$GHT \
         -e MODEL_NAME=unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M" \
  --entrypoint /bin/bash \
  --raw \
  --args -c "$BOOT"
```

Three things in there are load-bearing:

- **`LD_LIBRARY_PATH=/app`.** Putting `/app` on `PATH` finds the `llama-server`
  binary but not its shared objects, and it dies with
  `error while loading shared libraries: libllama-server-impl.so`. Run it from
  `/app` with `LD_LIBRARY_PATH` set.
- **`--break-system-packages`.** The image is Ubuntu 24.04, so pip refuses to
  install into the system environment (PEP 668) without it. The `||` fallback
  covers older bases that don't recognise the flag.
- **llama.cpp binds `127.0.0.1`, and only 8501 is published.** The model API is
  not reachable from the internet — Streamlit talks to it over localhost inside
  the container. llama.cpp ships with CORS open to `*` and no API key, so
  exposing port 8000 would hand anyone a free GPU.

Only the app port is mapped, so the public URL is
`http://<IP>:<HOST_PORT_FOR_8501>`. Vast assigns that host port at random on
every launch:

```bash
vastai show instances --raw | grep -A3 HostPort
```

The app comes up in about 90 seconds. The model takes ~20 minutes more, so the
UI is live and usable well before stories will generate.

### Option C — the CLI, model only

Serves just the API, with no app on the box. Useful when you're pointing a
local Streamlit (or anything else) at the endpoint during development.

```bash
# 1. Find an on-demand 4090. NOT interruptible: an interruptible instance can
#    be outbid and killed mid-demo.
vastai search offers 'gpu_name=RTX_4090 num_gpus=1 rentable=true \
  disk_space>80 inet_down>300 cuda_vers>=12.1' -o 'dph'

# 2. Launch. --args MUST be last: it swallows every remaining token on the
#    line, so a trailing --raw gets handed to llama-server, which rejects it
#    and crash-loops the container.
vastai create instance <OFFER_ID> \
  --image ghcr.io/ggml-org/llama.cpp:server-cuda \
  --disk 60 \
  --env '-p 8000:8000' \
  --raw \
  --args --port 8000 -ngl 99 -c 16384 \
         -hf unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M

# 3. If create returns "success": false, the instance exists but is STOPPED
#    (this happens when the offer was still held by a previous instance).
vastai show instances
vastai start instance <ID>

# 4. Get the external port. Vast maps container 8000 to a random host port.
vastai show instances --raw | grep -A3 HostPort

# 5. Wait for the model. 503 means "loading", 200 means ready.
curl -s -o /dev/null -w '%{http_code}\n' http://<IP>:<PORT>/health
```

### What to expect while waiting

- `vastai logs` **freezes** at the llama.cpp banner and never advances. The HF
  download writes a carriage-return progress bar that never flushes to Docker's
  log. The instance is fine. Track real progress with `disk_usage` instead:
  ```bash
  vastai show instances --raw | grep -E 'disk_usage|inet_down_billed'
  ```
- Download of 18.56 GB took **~20 minutes**, well under the machine's
  advertised 889 Mbps. That benchmark measures Vast's own CDN, not Hugging Face.
- Then `/health` returns **503** for several minutes while weights load into
  VRAM, and **200** when serving.
- There is no SSH on this image (runtype `args`, no sshd), and `vastai execute`
  only works on *stopped* instances. Debug through `/health`, `/props`, and the
  Vast stats fields.

---

## Verifying it works

```bash
curl -s http://<IP>:<PORT>/props | python3 -m json.tool | head -20
```

`total_slots: 4` is **not** a context divisor. The full 16,384 tokens are
available to a single request — confirmed by the rejection message on an
oversized prompt:

```
request (22907 tokens) exceeds the available context size (16384 tokens)
```

---

## Measured performance

RTX 4090, Q4_K_M, 16K context, at $0.3767/hr:

| Prompt size    | Prefill        | Generation |
|----------------|----------------|------------|
| 907 tokens     | 4,783 tok/s    | 228 tok/s  |
| 3,762 tokens   | 9,234 tok/s    | 194 tok/s  |
| 6,662 tokens   | 7,884 tok/s    | 181 tok/s  |

Generation stays near 200 tok/s because A3B is a mixture-of-experts model:
30B total parameters, only 3B active per token.

**One full story** (simonw, 2,168 commits, 12 chapters, 13 model calls):

| | |
|---|---|
| Input tokens  | 27,056 |
| Output tokens | 3,033 |
| Wall time     | 24.5s (7.0s GitHub + ~17s GPU) |
| Cost          | **$0.00256** — a quarter of a cent |
| Throughput    | 147 stories/hour, 390 stories per dollar |

---

## Pointing the app at it

```bash
# .env
MODEL_BASE_URL=http://<IP>:<PORT>/v1
MODEL_NAME=unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF:Q4_K_M
```

The host port changes on every rebuild, so this is the one line to update
after a respin.
