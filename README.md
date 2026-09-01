# GitHub Story

Paste a GitHub profile. Get the story of what that developer has been building,
written by a 30B model running on a single RTX 4090 rented from
[Vast.ai](https://vast.ai) for about 34 cents an hour.

No frontier API is involved. Every token is generated on rented hardware, and a
complete story costs **$0.00256** — a quarter of a cent.

```
paste a URL  ->  fetch public commits  ->  filter noise  ->  group into chapters
             ->  summarize each chapter (one model call each)
             ->  weave the chapters into one story (streamed)
```

Pick a narrator while you're at it: warm biographer, nature documentary, noir
detective, sports commentator, or epic saga. The voice changes; the facts don't.

---

## Why this is built the way it is

### The constraint that shaped everything: 16,384 tokens

The model is `Qwen3-Coder-30B-A3B-Instruct`, Q4_K_M quantized to **18.56 GB**.
On a 24 GB 4090 that leaves roughly 5 GB for KV cache, which llama.cpp turns
into a **16,384 token** window.

A prolific developer has thousands of commits. They do not fit. So the pipeline
is a map/reduce: summarize each time period separately (the "map"), then weave
those summaries into one story (the "reduce"). Two passes, one endpoint, one
model — not because of multiple GPUs, but because the input is bigger than the
window.

`llama.cpp` reports `total_slots: 4`, which looks like it might divide the
context four ways. It doesn't. Verified by feeding it an oversized prompt:

```
request (22907 tokens) exceeds the available context size (16384 tokens)
```

Full 16K per request.

### The data source is not the obvious one

The obvious way to get someone's commits is GitHub's commit search API:
`/search/commits?q=author:USERNAME`. It's one endpoint and it returns message,
date, and repo in a single row.

It is also the wrong choice, for two measured reasons.

**It can't sustain the request volume.** GitHub enforces an undocumented
*secondary* rate limit on top of the published quota. Measured on one token:
commit search tripped a 403 after **3 calls**, while 24 of 30 primary requests
were still unspent. Pacing didn't help — 8 seconds between calls tripped at the
same 3. Recovery took 32 seconds. This pipeline needs ~10 such calls.

**It doesn't return what you think.** GitHub links commits to accounts by
*email*, and commit search spans every repo on GitHub. So:

```
author:torvalds  ->  429,964,072 results
```

The newest 100 are from a repository Linus has never touched. Anyone can put
any email in their git config.

The fix is the boring core endpoint, one call per repo:

```
/repos/{owner}/{repo}/commits?author={login}
```

Different rate-limit bucket, and it can only return commits from repos the user
actually owns, so spoofing is structurally impossible.

| | commit search | per-repo core API |
|---|---|---|
| Calls needed | 10 | 92 |
| Wall time | **fails** | **3.2s** (12 workers) |
| Commits returned | ≤1,000 | 3,066 |
| Quota left after | — | 4,953 / 5,000 |
| Spoofable | yes | no |

One best-effort commit-search call is kept as a garnish, to catch public
contributions to repos the user *doesn't* own. If it 403s, it's skipped and the
story is slightly thinner. It never fails the request.

### Small decisions that mattered

- **Noise filtering before any tokens are spent.** Merge commits, bot authors,
  and runs of identical messages are dropped. Garbage in, boring story out.
- **Chapters are time-based, not size-based.** Half-year buckets, sparse
  neighbours merged, capped at 12. "Early 2023: the iOS era" is a chapter;
  "commits 400-600" is not.
- **Even sampling, never truncation.** A period over 250 commits is sampled
  across its whole span, preserving its shape.
- **Repo selection samples across *creation date*.** Taking the N most recently
  pushed repos silently deletes someone's early years — which is the part worth
  reading. On one test profile this was the difference between 63 and 164
  visible repositories.

---

## Measured performance

RTX 4090, Q4_K_M, 16K context, llama.cpp `server-cuda`:

| Prompt | Prefill | Generation |
|---|---|---|
| 907 tokens | 4,783 tok/s | 228 tok/s |
| 3,762 tokens | 9,234 tok/s | 194 tok/s |
| 6,662 tokens | 7,884 tok/s | 181 tok/s |

Generation holds near 200 tok/s because A3B is a mixture-of-experts model:
30B total parameters, only **3B active** per token.

One complete story (2,168 commits, 12 chapters, 13 model calls):

| | |
|---|---|
| Input tokens | 27,056 |
| Output tokens | 3,033 |
| Wall time | **24.5s** (7.0s GitHub + ~17s GPU) |
| Cost | **$0.00256** |
| Throughput | 147 stories/hour · 390 stories per dollar |

---

## Running it

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill in GITHUB_TOKEN and MODEL_BASE_URL
.venv/bin/streamlit run app.py
```

`GITHUB_TOKEN` is a classic token with **no scopes** — it exists only to lift
the rate limit from 60/hr to 5,000/hr. Users never supply tokens; only public
data is ever read.

`MODEL_BASE_URL` is any OpenAI-compatible endpoint. See [DEPLOY.md](DEPLOY.md)
for standing up llama.cpp on a Vast.ai 4090, including the failure modes worth
knowing about.

There's a CLI harness too, which runs the whole pipeline with no UI:

```bash
.venv/bin/python pipeline.py kilo-loco "Noir detective"
```

---

## Layout

```
pipeline.py   all the logic — fetch, filter, chapter, summarize, weave
app.py        thin Streamlit shell over pipeline.run()
DEPLOY.md     standing up the model on Vast.ai, with measured numbers
```

`pipeline.py` is deliberately one file. Every non-obvious decision has a comment
next to it explaining what was measured and why the alternative was rejected.

---

## Scope

Public repositories only. No user tokens, no private data, no accounts, no
database. One story generates at a time (a global lock), and fetches are cached
for an hour per profile — the site is public and every story is GPU time.

## What I'd build next

The interesting version isn't this app — it's the thing underneath it. An agent
that takes a Hugging Face model link, works out what hardware it actually needs
(quant size, KV cache, context target), finds the cheapest Vast offer that fits,
and emits a one-click deploy config. The 16K-context math in this README is the
kind of arithmetic that agent would do automatically, and getting it wrong is
the difference between a model that serves and a model that OOMs.
