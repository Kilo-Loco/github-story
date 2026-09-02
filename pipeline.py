"""
pipeline.py — everything except the UI.

Flow: GitHub URL -> commits -> filter -> group into periods
      -> summarize each period (one model call each)
      -> weave period summaries into one story (one streamed model call).

The model endpoint is any OpenAI-compatible server. During development,
point MODEL_BASE_URL at a cheap hosted API; in production it's the
llama.cpp / vLLM server on the Vast.ai 4090. Nothing else changes.

Test from the terminal (no Streamlit, no GPU required):
    python pipeline.py https://github.com/kiloloco
"""

from __future__ import annotations

import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Generator

import requests
from dotenv import load_dotenv
from openai import OpenAI

# ---------------------------------------------------------------------------
# Configuration — all via env vars so nothing sensitive lives in code.
# load_dotenv() reads a gitignored .env for local dev; on the Vast box the
# same names are set as real environment variables and .env simply isn't there.
# Real environment variables always win over .env.
# ---------------------------------------------------------------------------

load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")          # read-only; raises rate limit 60/hr -> 5000/hr
MODEL_BASE_URL = os.environ.get("MODEL_BASE_URL", "http://localhost:8000/v1")
MODEL_NAME = os.environ.get("MODEL_NAME", "qwen3-coder-30b-a3b-instruct")

# Context budget math (the reason this file has two passes at all):
# The Q4_K_M quant is 18.56 GB of the 4090's 24 GB, leaving ~5 GB for KV cache.
# MEASURED on the live server: n_ctx = 16,384 tokens, and that is the ceiling
# for a SINGLE request -- llama.cpp reports total_slots=4, but slots do not
# subdivide the window (a 22,907-token prompt is rejected with
# "exceeds the available context size (16384 tokens)", not 4,096).
# Every request below stays comfortably inside 16K.
COMMITS_PER_PAGE = 100           # GitHub max page size
MAX_REPO_PAGES = 5               # up to 500 repos; 2 extra calls for most people
MAX_PERIODS = 12                 # each period costs one model call; see timing note below
MAX_COMMITS_PER_PERIOD = 250     # evenly sampled beyond this; measured at 3,762 prompt tokens
# Measured end-to-end on the 4090 (simonw, 2,168 commits, 12 chapters): 25.4s
# total -- 5.3s of GitHub fetch, ~20s of GPU. Generation runs at 180-228 tok/s
# because A3B is a mixture-of-experts: 30B total parameters, only 3B active per
# token. That speed is why 12 chapters is comfortable rather than expensive.
SUMMARY_MAX_TOKENS = 300         # per-period summary length
STORY_MAX_TOKENS = 1200          # final story length

GITHUB_API = "https://api.github.com"


def _gh_headers() -> dict:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


# ---------------------------------------------------------------------------
# Step 0: parse whatever the user pasted.
# ---------------------------------------------------------------------------

@dataclass
class Target:
    kind: str        # "user" or "repo"
    owner: str
    repo: str | None = None

    @property
    def label(self) -> str:
        return f"{self.owner}/{self.repo}" if self.repo else self.owner


def parse_github_url(url: str) -> Target:
    """Accept a profile URL, a repo URL, or a bare username."""
    path = re.sub(r"^(https?://)?(www\.)?github\.com/", "", url.strip()).strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        raise ValueError("That doesn't look like a GitHub profile or repo URL.")
    if len(parts) == 1:
        return Target(kind="user", owner=parts[0])
    return Target(kind="repo", owner=parts[0], repo=parts[1])


# ---------------------------------------------------------------------------
# Step 1: fetch. Deliberately small surface:
#   profile (1 call) + repo list (1-5 calls) + one call per repo.
# We never fetch diffs or per-commit stats: that's 1 extra call per commit
# and would blow both the rate limit and the context budget. Commit messages
# are the narrative humans already wrote; we use those.
# ---------------------------------------------------------------------------

@dataclass
class Commit:
    sha: str
    message: str      # first line only
    date: datetime
    repo: str


def _get(url: str, params: dict | None = None) -> dict | list:
    resp = requests.get(url, headers=_gh_headers(), params=params, timeout=15)

    if resp.status_code == 401:
        raise RuntimeError(
            "GitHub rejected GITHUB_TOKEN (401). If you launched from the Vast "
            "template, replace the placeholder with a real token — a classic "
            "token with no scopes is enough — or remove the variable entirely "
            "to run unauthenticated at 60 requests/hour."
        )
    if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
        reset = resp.headers.get("x-ratelimit-reset", "")
        when = (datetime.fromtimestamp(int(reset)).strftime("%H:%M:%S")
                if reset.isdigit() else "shortly")
        raise RuntimeError(
            f"GitHub rate limit exhausted; it resets at {when}."
            + ("" if GITHUB_TOKEN else " Set GITHUB_TOKEN to raise the limit.")
        )

    resp.raise_for_status()
    return resp.json()


def fetch_profile(target: Target) -> dict:
    """One call. Name, bio, and account age give the story its opening."""
    if target.kind == "user":
        data = _get(f"{GITHUB_API}/users/{target.owner}")
        return {
            "name": data.get("name") or target.owner,
            "bio": data.get("bio") or "",
            "created_at": (data.get("created_at") or "")[:10],
            "public_repos": data.get("public_repos", 0),
        }
    data = _get(f"{GITHUB_API}/repos/{target.owner}/{target.repo}")
    return {
        "name": data.get("full_name"),
        "bio": data.get("description") or "",
        "created_at": (data.get("created_at") or "")[:10],
        "language": data.get("language") or "",
        "stars": data.get("stargazers_count", 0),
    }


def fetch_repo_context(target: Target) -> list[dict]:
    """One call. Descriptions + languages are the cheapest, highest-value
    tokens in the whole prompt: they let the model notice 'the Swift repos
    went quiet and Python appeared'. Forks are dropped — they aren't the
    user's story."""
    if target.kind != "user":
        return []

    # Paginate. One page is 100 repos, and plenty of people have more than
    # that -- kilo-loco has 209. Worse, a single page sorted by "pushed" drops
    # the OLDEST repos specifically, which is exactly the early-career material
    # a story is about. Two extra calls buy back half someone's history.
    repos: list[dict] = []
    for page in range(1, MAX_REPO_PAGES + 1):
        batch = _get(
            f"{GITHUB_API}/users/{target.owner}/repos",
            params={"sort": "pushed", "per_page": 100, "page": page},
        )
        repos.extend(batch)
        if len(batch) < 100:
            break

    return [
        {
            "name": r["name"],
            "full_name": r["full_name"],
            "description": (r.get("description") or "")[:120],
            "language": r.get("language") or "",
            "created": (r.get("created_at") or "")[:7],
        }
        for r in repos
        if not r.get("fork")
    ]


# Two ways to get someone's commits. The difference is not obvious until you
# measure it, so I did — against simonw (91 public repos), on one token:
#
#   /search/commits?q=author:X       10 calls needed, FAILS. GitHub's secondary
#       rate limit allows ~3 commit-search calls per 30 seconds no matter how
#       you pace them (measured: 8s spacing tripped at the 3rd call, recovery
#       took 32s) while 24 of 30 primary requests were still unspent. Worse,
#       it matches on the commit's AUTHOR EMAIL across every repo on GitHub:
#       `author:torvalds` returns 429,964,072 results and the newest 100 are
#       from a repo he has never touched, because anyone can set that email
#       in their own git config.
#
#   /repos/{owner}/{repo}/commits?author=X    92 calls, 3.2s wall with 12
#       workers, 3,066 commits, and 4,953 of 5,000 requests still unspent.
#       A different rate-limit bucket entirely, and it can only ever return
#       commits from repos the user actually owns — spoofing is structurally
#       impossible.
#
# So this uses the core endpoint only. The tradeoff is that it cannot see
# contributions to repos the user does not own; their own repos are the story.
MAX_REPOS_SCANNED = 60      # ~60 calls stays far inside the 5,000/hr core budget
REPO_FETCH_WORKERS = 12     # measured: 91 repos in 3.2s, zero throttling


def _parse_commit(item: dict, repo_name: str) -> Commit | None:
    """Both endpoints return the same commit shape, so one parser serves both."""
    commit = item.get("commit", {})
    date_str = commit.get("author", {}).get("date", "")
    if not date_str:
        return None
    return Commit(
        sha=item.get("sha", ""),
        message=(commit.get("message") or "").split("\n", 1)[0][:100],
        date=datetime.fromisoformat(date_str.replace("Z", "+00:00")),
        repo=repo_name,
    )


def _commits_from_repo(full_name: str, author: str | None) -> list[Commit]:
    """One core-API call. Returns [] for the boring failures (empty repo gives
    409, deleted/renamed gives 404) — a single repo missing is not worth
    failing someone's whole story over. Rate-limit exhaustion still raises."""
    params = {"per_page": COMMITS_PER_PAGE}
    if author:
        params["author"] = author
    try:
        data = _get(f"{GITHUB_API}/repos/{full_name}/commits", params=params)
    except requests.HTTPError:
        return []
    repo_name = full_name.split("/")[-1]
    return [c for c in (_parse_commit(i, repo_name) for i in data) if c]


def _select_repos(repos: list[dict], limit: int) -> list[str]:
    """Evenly sample across CREATION DATE, not recency. Taking the N
    most-recently-pushed repos would quietly delete the user's early years,
    which is the part of a story worth reading."""
    ordered = sorted(repos, key=lambda r: r.get("created") or "")
    if len(ordered) > limit:
        step = len(ordered) / limit
        ordered = [ordered[int(i * step)] for i in range(limit)]
    return [r["full_name"] for r in ordered]


def fetch_commits(target: Target, repos: list[dict]) -> list[Commit]:
    """Fan out across the user's repos in parallel, then dedupe by SHA."""
    if target.kind == "repo":
        return sorted(_commits_from_repo(target.label, None), key=lambda c: c.date)

    names = _select_repos(repos, MAX_REPOS_SCANNED)
    with ThreadPoolExecutor(max_workers=REPO_FETCH_WORKERS) as pool:
        batches = pool.map(lambda n: _commits_from_repo(n, target.owner), names)

    collected = [c for batch in batches for c in batch]

    seen: set[str] = set()
    merged: list[Commit] = []
    for c in collected:
        if c.sha and c.sha not in seen:
            seen.add(c.sha)
            merged.append(c)
    merged.sort(key=lambda c: c.date)
    return merged


# ---------------------------------------------------------------------------
# Step 2: filter. Ten lines where story quality is actually won —
# garbage in, boring story out.
# ---------------------------------------------------------------------------

BOT_PATTERN = re.compile(r"(dependabot|renovate|github-actions|\[bot\])", re.I)
NOISE_PATTERN = re.compile(r"^(merge (branch|pull request)|update readme)", re.I)


def filter_commits(commits: list[Commit]) -> list[Commit]:
    kept: list[Commit] = []
    prev_msg = ""
    for c in commits:
        if BOT_PATTERN.search(c.message) or NOISE_PATTERN.match(c.message):
            continue
        if c.message.strip() and c.message == prev_msg:
            continue  # collapse runs of identical messages ("wip", "fix", ...)
        prev_msg = c.message
        kept.append(c)
    return kept


# ---------------------------------------------------------------------------
# Step 3: group into periods (the "chapters").
# Half-year buckets, then sparse neighbours merged, then capped at
# MAX_PERIODS by merging the smallest neighbours. Time-based boundaries make
# each chunk a coherent chapter instead of an arbitrary slice of 200 commits.
# ---------------------------------------------------------------------------

def _bucket_key(dt: datetime) -> str:
    half = "H1" if dt.month <= 6 else "H2"
    return f"{dt.year} {half}"


def group_into_periods(commits: list[Commit]) -> list[tuple[str, list[Commit]]]:
    buckets: dict[str, list[Commit]] = defaultdict(list)
    for c in commits:
        buckets[_bucket_key(c.date)].append(c)

    periods = sorted(buckets.items())  # "2019 H1" sorts correctly as a string

    # Merge tiny periods into their neighbour: a half-year with 4 commits
    # isn't a chapter, it's a footnote.
    merged: list[tuple[str, list[Commit]]] = []
    for label, chunk in periods:
        if merged and (len(chunk) < 10 or len(merged[-1][1]) < 10):
            prev_label, prev_chunk = merged[-1]
            merged[-1] = (f"{prev_label.split(' ')[0]}–{label}", prev_chunk + chunk)
        else:
            merged.append((label, chunk))

    # Cap the number of chapters: each one is a model call, and 12 calls
    # keeps a full story under ~2 minutes on the 4090.
    while len(merged) > MAX_PERIODS:
        idx = min(range(len(merged) - 1), key=lambda i: len(merged[i][1]) + len(merged[i + 1][1]))
        a_label, a_chunk = merged[idx]
        b_label, b_chunk = merged[idx + 1]
        merged[idx] = (f"{a_label.split('–')[0]}–{b_label}", a_chunk + b_chunk)
        del merged[idx + 1]

    return merged


def _sample_evenly(commits: list[Commit], limit: int) -> list[Commit]:
    """Keep each map call inside the context budget. Even sampling preserves
    the shape of a period better than truncating its end."""
    if len(commits) <= limit:
        return commits
    step = len(commits) / limit
    return [commits[int(i * step)] for i in range(limit)]


# ---------------------------------------------------------------------------
# Step 4 + 5: the two passes.
# Pass 1 (map): one summary per period. Pass 2 (reduce): weave the story.
# Both are calls to the same single endpoint — no parallelism, no extra GPUs;
# the input is just bigger than a 13-16K window, so we go through it twice.
# ---------------------------------------------------------------------------

_client = OpenAI(base_url=MODEL_BASE_URL, api_key=os.environ.get("MODEL_API_KEY", "not-needed"))


def summarize_period(label: str, commits: list[Commit]) -> str:
    commits = _sample_evenly(commits, MAX_COMMITS_PER_PERIOD)
    lines = "\n".join(f"[{c.repo}] {c.message}" for c in commits)
    prompt = (
        f"Below are git commit messages from the period {label}, "
        f"prefixed with the repository they belong to.\n\n{lines}\n\n"
        "In 3-5 sentences, describe what this developer was building and "
        "learning during this period. Note the technologies involved and any "
        "shift in focus. Write in the third person, past tense, plain prose."
    )
    resp = _client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=SUMMARY_MAX_TOKENS,
        temperature=0.7,
    )
    return resp.choices[0].message.content.strip()


# The voices. Each one only swaps the NARRATOR -- the facts come from the
# chapter notes either way, and the rules below the voice block are what keep a
# silly narrator from inventing a silly fact. This is the cheapest interesting
# thing in the whole project: same model, same 27K tokens, same quarter of a
# cent, wildly different output.
STORY_VOICES = {
    "Biography": (
        "A thoughtful biographer writing for people who know this developer. "
        "Plain, generous prose. Quietly observant about what changed and when, "
        "and willing to name the turning points plainly."
    ),
    "Children's story": (
        "A bedtime storyteller reading to a curious seven-year-old. Short "
        "sentences. Warm and a little silly. Explain each technology in terms a "
        "child would picture -- a database is a big box of drawers, a bug is a "
        "sock that went missing -- but keep using the real project names, "
        "because they are the characters. Never condescending, never babyish."
    ),
    "Grill me": (
        "A blunt, very senior engineer reviewing this person's public history in "
        "a hiring loop, saying the things politeness usually hides. Point out "
        "the abandoned repos, the tutorial projects that never became products, "
        "the years spent re-learning the same lesson, the framework-chasing. "
        "Ask the uncomfortable questions their commit log raises. Be specific "
        "and fair -- cite the actual evidence, credit what genuinely improved, "
        "and land on the one thing they should do differently. Critical, not "
        "cruel; this should sting because it is accurate, not because it is mean."
    ),
}

DEFAULT_VOICE = "Biography"


def write_story(profile: dict, repo_context: list[dict],
                period_summaries: list[tuple[str, str]],
                voice: str = DEFAULT_VOICE) -> Generator[str, None, None]:
    """Final pass, streamed — yields text chunks for st.write_stream."""
    chapters = "\n\n".join(f"{label}:\n{summary}" for label, summary in period_summaries)
    repo_lines = "\n".join(
        f"- {r['name']} ({r['language']}, created {r['created']}): {r['description']}"
        for r in repo_context[:25]
    )
    style = STORY_VOICES.get(voice, STORY_VOICES[DEFAULT_VOICE])
    prompt = (
        f"You are telling the story of a developer's journey, built entirely "
        f"from their public GitHub history.\n\n"
        f"Profile: {profile.get('name')} — {profile.get('bio')}. "
        f"On GitHub since {profile.get('created_at')}.\n\n"
        f"Their repositories:\n{repo_lines}\n\n"
        f"Chapter notes, in chronological order:\n{chapters}\n\n"
        f"NARRATOR: {style}\n\n"
        "Write 4-7 paragraphs, chronological, committing fully to that narrator's "
        "voice from the first sentence.\n"
        "Rules that outrank the voice:\n"
        "- Every project name, language, and date must come from the notes above. "
        "The voice is a costume on real facts; invent nothing.\n"
        "- Name specific repositories and technologies. A paragraph that could "
        "describe any developer is a failed paragraph.\n"
        "- Show how their focus shifted over time, and end on where they seem "
        "to be heading now.\n"
        "- No headings, no bullet points, no flattery, no summarising preamble "
        "like 'here is the story'. Start in the voice immediately."
    )
    stream = _client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=STORY_MAX_TOKENS,
        temperature=0.8,
        stream=True,
    )
    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            yield delta


# ---------------------------------------------------------------------------
# Orchestrator — what app.py calls. Yields progress events so the UI can
# show chapters assembling, then streams the story.
# ---------------------------------------------------------------------------

def fetch_all(url: str) -> dict:
    """Every network call to GitHub, in one function that returns plain data.

    Split out from run() for one reason: the UI wraps this in
    st.cache_data(ttl=3600), so pasting the same profile twice costs zero API
    calls. Keeping it free of Streamlit imports keeps the CLI harness working
    with no UI installed."""
    target = parse_github_url(url)
    repo_context = fetch_repo_context(target)
    commits = filter_commits(fetch_commits(target, repo_context))
    if not commits:
        raise RuntimeError("No usable commits found — is this profile/repo active and public?")
    return {
        "label": target.label,
        "profile": fetch_profile(target),
        "repo_context": repo_context,
        "commits": commits,
    }


def run(url: str, prefetched: dict | None = None,
        voice: str = DEFAULT_VOICE) -> Generator[dict, None, None]:
    """Yields: {"type": "status", ...} events, then {"type": "story_chunk", ...}.

    `prefetched` lets the caller supply a cached fetch_all() result; when it's
    None we fetch inline, which is what the CLI harness does."""
    if prefetched is None:
        yield {"type": "status", "text": "Reading public history..."}
        prefetched = fetch_all(url)

    profile = prefetched["profile"]
    repo_context = prefetched["repo_context"]
    commits = prefetched["commits"]

    periods = group_into_periods(commits)
    yield {"type": "status",
           "text": f"{len(commits)} commits across {len(periods)} chapters."}

    summaries: list[tuple[str, str]] = []
    for label, chunk in periods:
        yield {"type": "status", "text": f"Summarizing {label} ({len(chunk)} commits)..."}
        summaries.append((label, summarize_period(label, chunk)))

    yield {"type": "status", "text": "Writing the story..."}
    for text in write_story(profile, repo_context, summaries, voice):
        yield {"type": "story_chunk", "text": text}


# ---------------------------------------------------------------------------
# CLI harness: develop and debug the whole pipeline with no UI and no GPU.
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print("usage: python pipeline.py <github profile or repo url> [voice]")
        print("voices:", ", ".join(STORY_VOICES))
        sys.exit(1)
    for event in run(sys.argv[1], voice=sys.argv[2] if len(sys.argv) == 3 else DEFAULT_VOICE):
        if event["type"] == "status":
            print(f"\n--- {event['text']}")
        else:
            print(event["text"], end="", flush=True)
    print()
