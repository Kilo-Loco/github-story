# GitHub Story — Qwen3-Coder-30B on one 4090

Paste a GitHub profile, get a narrated story of what that developer has been
building. Model and app run in **one container**. No API keys, no second instance.

**What launches:** llama.cpp serving `Qwen3-Coder-30B-A3B-Instruct` (Q4_K_M,
18.56 GB) on `localhost:8000`, a Streamlit app on port **8501**, and a Cloudflare
quick tunnel for an HTTPS link.

### Use it

1. Launch on any **RTX 4090** (24 GB). Give it **60 GB disk**.
2. Wait ~5 min for the weights. Then open the mapped port **8501**.
3. For an HTTPS link: `cat /opt/app/tunnel_url.txt`

### Optional

Set `GITHUB_TOKEN` (a classic token, **no scopes**) to lift GitHub's rate limit
from 60/hr to 5,000/hr. Without it you get roughly one story per hour.

### Numbers on a 4090

| | |
|---|---|
| Context | 16,384 tokens |
| Generation | 180–228 tok/s |
| One story | ~25 s, ~$0.0026 |

Generation stays fast because A3B is mixture-of-experts: 30B total parameters,
3B active per token.

### Notes

- The model API binds `127.0.0.1` and is **not** published. Only 8501 is exposed.
- Streamlit is PID 1 — don't `pkill` it, you'll stop the container.
- Source: https://github.com/Kilo-Loco/github-story
