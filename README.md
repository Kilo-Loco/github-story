# GitHub Story

Paste a GitHub profile, get the story of what that developer has been building —
written by **Qwen3-Coder-30B-A3B** (Q4_K_M) running on a single **RTX 4090**
rented from [Vast.ai](https://vast.ai). No frontier API.

**~25 seconds and ~$0.0026 per story.** Pick a narrator: Biography,
Children's story, or Grill me.

## Run it on Vast

**[Launch the template →](https://cloud.vast.ai/?ref_id=667524&creator_id=667524&name=github-story-qwen3-coder-4090)**

1. Click the pencil on the template card and replace `GITHUB_TOKEN` in Docker
   Options with a classic token (**no scopes** — it only lifts GitHub's rate
   limit from 60/hr to 5,000/hr). One story makes ~60 API calls, so the
   placeholder fails with a 401. Saving makes your own copy of the template;
   that's expected.
2. Rent any RTX 4090 with 60 GB disk.
3. Weights take ~5 minutes. Open the mapped port **8501** — the footer shows
   that instance's HTTPS link.

Or from the CLI:

```bash
vastai create instance <OFFER_ID> \
  --template_hash 6e0f5a02c94c3b3dc54808041828ea08 --disk 60
```

> Vast rotates a template's `hash_id` on every edit, so that hash may be stale.
> The link above addresses the template by name and doesn't rot.

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

**Commit search is the obvious data source and the wrong one.** Measured:
GitHub's secondary rate limit allows ~3 commit-search calls per 30s no matter
how you pace them, and this needs ~10. It also matches on commit *email* across
all of GitHub, so `author:torvalds` returns 429,964,072 results whose newest
100 are from a repo he has never touched. The per-repo core endpoint
(`/repos/{owner}/{repo}/commits?author=`) returned 3,066 commits in 3.2s using
92 calls, left 4,953 of 5,000 unspent, and cannot be spoofed.

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
