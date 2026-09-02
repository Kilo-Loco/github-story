"""
app.py — the entire UI. Everything with substance lives in pipeline.py;
this file only turns pipeline events into Streamlit widgets.
"""

import json
import pathlib
import re
import threading
import time
import urllib.parse

import streamlit as st
import streamlit.components.v1 as components

import pipeline

st.set_page_config(page_title="GitHub Story", page_icon="📖")

# The box tunnels itself out through cloudflared, which picks a RANDOM hostname
# every time it starts. A file written once at boot goes stale the moment the
# tunnel reconnects under a new name -- observed exactly that. So read the live
# log on each rerun and take the LAST hostname it announced, which is by
# definition the current one.
_CF_LOG = pathlib.Path("/tmp/cf.log")
_TUNNEL_FILE = pathlib.Path(__file__).parent / "tunnel_url.txt"
_HOSTNAME = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def public_url() -> str:
    """Current tunnel hostname, or "" if there isn't one."""
    try:
        # The log grows unbounded; only the tail can hold the newest hostname.
        size = _CF_LOG.stat().st_size
        with _CF_LOG.open("rb") as fh:
            fh.seek(max(0, size - 65536))
            found = _HOSTNAME.findall(fh.read().decode("utf-8", "replace"))
        if found:
            return found[-1]
    except OSError:
        pass
    try:
        return _TUNNEL_FILE.read_text().strip()   # fallback for odd setups
    except OSError:
        return ""

# Deliberately no template hash here: Vast rotates a template's hash_id on every
# edit, so any hash baked into this page goes stale the next time the template is
# touched. The repo carries the current one.
TEMPLATE_NAME = "github-story-qwen3-coder-4090"


# One story at a time. The site is public and every story is GPU time on a
# single 4090, so a global lock is the whole abuse story: a second visitor
# waits rather than queueing a parallel decode that halves everyone's speed.
#
# It carries a timestamp because a plain lock is not safe here. If a visitor
# closes the tab mid-story, Streamlit kills the script thread and the `with`
# block never unwinds, so the lock is held forever and the site is wedged for
# everyone until the process restarts. Observed exactly that. So we record when
# the lock was taken and reclaim it once no story could still plausibly be
# running.
STALE_AFTER = 180  # seconds; a full story takes ~25


@st.cache_resource
def _gpu_gate() -> dict:
    return {"lock": threading.Lock(), "taken_at": 0.0}


def _acquire_gpu() -> bool:
    gate = _gpu_gate()
    if gate["lock"].acquire(blocking=False):
        gate["taken_at"] = time.time()
        return True
    if time.time() - gate["taken_at"] > STALE_AFTER:
        gate["taken_at"] = time.time()   # steal it; the holder is gone
        return True
    return False


def _release_gpu() -> None:
    try:
        _gpu_gate()["lock"].release()
    except RuntimeError:
        pass   # already released, or we stole it from a dead holder


# Same profile twice inside an hour = zero GitHub calls. Cheapest guard there is.
@st.cache_data(ttl=3600, show_spinner=False)
def _cached_fetch(url: str) -> dict:
    return pipeline.fetch_all(url)


def share_controls(story: str, target: str) -> None:
    """Copy-to-clipboard and share-on-X.

    Streamlit has no native copy button for prose (st.code gives you one, but
    renders monospace, which reads badly for a story). So this is a small
    self-contained HTML block: json.dumps safely escapes the story into a JS
    string literal, including the quotes and newlines a story is full of.
    """
    teaser = (
        f"I ran {target}'s commit history through GitHub Story — the whole thing "
        f"is written by a 30B model self-hosted on one RTX 4090 rented from Vast.ai."
    )
    intent = "https://x.com/intent/post?" + urllib.parse.urlencode(
        {"text": teaser, "url": public_url() or "https://github.com/Kilo-Loco/github-story"}
    )

    components.html(
        f"""
        <style>
          /* components.html renders in an IFRAME, which does not inherit
             Streamlit's theme. "color: inherit" resolves against the iframe's
             own default styles, not the app's, so the labels came out nearly
             invisible. Every colour here is therefore explicit, and chosen to
             hold contrast against both the light and dark Streamlit themes. */
          .row {{ display:flex; gap:.5rem; font-family:-apple-system,system-ui,sans-serif; }}
          .btn {{
            flex:0 0 auto; padding:.5rem 1rem; border-radius:.5rem; cursor:pointer;
            font-size:.85rem; font-weight:500; text-decoration:none; line-height:1.4;
            border:1px solid #555; background:#262730; color:#FFFFFF !important;
            transition:background .15s ease, border-color .15s ease;
          }}
          .btn:hover {{ background:#3A3B47; border-color:#6E6E7B; }}
          .btn.x {{ background:#000; border-color:#000; color:#FFFFFF !important; }}
          .btn.x:hover {{ background:#1a1a1a; border-color:#333; }}
        </style>
        <div class="row">
          <button class="btn" id="c" onclick="copyStory()">Copy story</button>
          <a class="btn x" href="{intent}" target="_blank" rel="noopener">Share on 𝕏</a>
        </div>
        <script>
          const STORY = {json.dumps(story)};
          function copyStory() {{
            navigator.clipboard.writeText(STORY).then(() => {{
              const b = document.getElementById("c");
              b.textContent = "Copied";
              setTimeout(() => b.textContent = "Copy story", 1600);
            }});
          }}
        </script>
        """,
        height=56,
    )


st.title("📖 GitHub Story")
st.caption("Paste a GitHub profile or repo. Get the story of what they've been building.")

url = st.text_input("GitHub profile or repo URL", placeholder="https://github.com/torvalds")
voice = st.selectbox("Narrator", list(pipeline.STORY_VOICES))

# Gate on a button, not on the text input: Streamlit reruns the whole script on
# every widget interaction, and an ungated pipeline would refetch and regenerate
# each time.
if st.button("Tell me their story", type="primary", disabled=not url.strip()):
    if not _acquire_gpu():
        st.warning("Someone else's story is generating right now — try again in a minute.")
    else:
        try:
            try:
                with st.status("Reading public history...", expanded=True) as status:
                    events = pipeline.run(url, prefetched=_cached_fetch(url), voice=voice)
                    first_chunk = None
                    for event in events:
                        if event["type"] == "status":
                            status.update(label=event["text"])
                            st.write(event["text"])
                        else:
                            first_chunk = event      # story has started
                            break
                    status.update(label="Story written", state="complete", expanded=False)

                def story_stream():
                    """Resume the same generator: the status loop above stopped
                    at the first chunk, so hand that one over and keep going."""
                    if first_chunk:
                        yield first_chunk["text"]
                    for event in events:
                        yield event["text"]

                st.session_state["story"] = st.write_stream(story_stream())
                st.session_state["story_url"] = url
                share_controls(st.session_state["story"], url.rstrip("/").split("/")[-1])

            except Exception as exc:
                # The model takes ~25 minutes to download and load, while the app
                # is serving within 90 seconds. Anyone who arrives in that window
                # gets a connection error, so say what is actually happening
                # instead of showing them a Python exception name.
                name = type(exc).__name__
                if "Connection" in name or "APIError" in name or "Timeout" in name:
                    st.warning(
                        "The GPU is still waking up — the model takes a few "
                        "minutes to load after the site comes online. "
                        "Try again shortly."
                    )
                elif "GITHUB_TOKEN" in str(exc) or "rate limit" in str(exc).lower():
                    st.warning(str(exc))
                else:
                    st.error(str(exc) or name)
        finally:
            _release_gpu()

# Survive the rerun that any later widget interaction causes.
elif st.session_state.get("story"):
    st.markdown(st.session_state["story"])
    share_controls(
        st.session_state["story"],
        st.session_state.get("story_url", "").rstrip("/").split("/")[-1],
    )

st.divider()
st.caption(
    "Written by a self-hosted **Qwen3-Coder-30B-A3B** (Q4_K_M) on a single "
    "**RTX 4090** rented from [Vast.ai](https://vast.ai) — no frontier API "
    "involved. Public commit history only."
)
_url = public_url()
if _url:
    st.caption(f"This instance is reachable at {_url}")
st.caption(
    f"Run your own on a 4090: search Vast.ai templates for **{TEMPLATE_NAME}** · "
    "[source & setup](https://github.com/Kilo-Loco/github-story)"
)
