# GitHub Story

Paste a GitHub profile, get the story of what that developer has been building —
written by **Qwen3-Coder-30B-A3B** (Q4_K_M) running on a single **RTX 4090**
rented from [Vast.ai](https://vast.ai). No frontier API.

**~25 seconds and ~$0.0026 per story.** Pick a narrator: Biography,
Children's story, or Grill me.

## Run it on Vast

**[Launch the template →](https://cloud.vast.ai/?ref_id=667524&creator_id=667524&name=github-story)**
(`github-story`, id `664556`)

Rent any RTX 4090 with 60 GB disk. Before renting, set your GitHub token in
**Docker Options**:

```
-p 8501:8501 -e GITHUB_TOKEN=ghp_your_token
```

A classic token with **no scopes** is enough — it only lifts GitHub's rate limit
from 60/hr to 5,000/hr, and one story makes ~60 API calls, so the placeholder
fails with a 401.

> Put the token on the **instance**, not the template. Clicking *Save* writes it
> back into a template, and if that template is public your token is public with
> it. Edit Docker Options and rent — don't save.

Weights take ~5 minutes, then open the mapped port **8501**.

From the CLI, same idea — the token goes in `--env`, never in the template:

```bash
vastai create instance <OFFER_ID> --template_hash <HASH> --disk 60 \
  --env '-p 8501:8501 -e GITHUB_TOKEN=ghp_your_token'
```

Fetch `<HASH>` fresh; Vast rotates it on every template edit. The id (`664556`)
is stable:

```bash
vastai search templates --raw | python3 -c \
  "import json,sys;print([t['hash_id'] for t in json.load(sys.stdin) if t.get('id')==664556])"
```

The template's only job is to clone this repo and run [`onstart.sh`](onstart.sh),
which starts llama.cpp on localhost and Streamlit on 8501. Keeping the boot
script here means it's reviewable, and editing it doesn't churn the template.

## Run it anywhere else

`MODEL_BASE_URL` is any OpenAI-compatible endpoint.

```bash
pip install -r requirements.txt
export GITHUB_TOKEN=... MODEL_BASE_URL=http://localhost:8000/v1 MODEL_NAME=...
streamlit run app.py
python pipeline.py kilo-loco "Grill me"   # same pipeline, no UI
```

## Why it's built this way

**The 16,384-token window drove the design.** The Q4_K_M quant is 18.56 GB of
the 4090's 24 GB, leaving ~5 GB of KV cache. A prolific developer's history does
not fit, so `pipeline.py` is a map/reduce: summarize each half-year period, then
weave the summaries into one story. Two passes, one endpoint — not because of
multiple GPUs, but because the input is bigger than the window.

**Commits come from one call per repo** —
`/repos/{owner}/{repo}/commits?author=` — fanned out with a thread pool.
Measured on a 91-repo profile: 3,066 commits in 3.2 seconds, using 92 of the
5,000 requests GitHub allows per hour.

## Measured on a 4090

| | |
|---|---|
| Context | 16,384 tokens (per request — `total_slots` does not divide it) |
| Generation | 180–228 tok/s |
| One story | 2,168 commits → 13 model calls, 27k in / 3k out, 24.5s |
| Cost | $0.00256 at $0.3767/hr · 390 stories per dollar |

Generation stays fast because A3B is mixture-of-experts: 30B total parameters,
**3B active** per token.

## Scope

Public repos only. No accounts, no database, no user tokens. One story generates
at a time behind a global lock, and fetches are cached for an hour — the site is
public and every story is GPU time.
