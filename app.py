"""
app.py — the entire UI. Everything with substance lives in pipeline.py;
this file only turns pipeline events into Streamlit widgets.
"""

import threading
import time
import urllib.parse

import streamlit as st

import pipeline

st.set_page_config(page_title="GitHub Story", page_icon="📖")

# Vast's "referral link" addresses a template by name and creator, so it survives
# edits. Their other share link embeds hash_id, which Vast rotates on every edit.
TEMPLATE_URL = (
    "https://cloud.vast.ai/?ref_id=667524&creator_id=667524&name=github-story"
)

# One story at a time: the site is public and every story is GPU time on a
# single 4090. The timestamp matters because a visitor who closes the tab
# mid-story kills the script thread without releasing the lock, wedging the site
# for everyone. Reclaim it once no story could still be running.
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


def share_button(target: str) -> None:
    """Native link button. Streamlit has no copy-to-clipboard widget, and hand
    rolling one means shipping HTML, CSS and JS inside a Python f-string for a
    button people can replace by selecting the text."""
    teaser = (
        f"I ran {target}'s commit history through GitHub Story — written by a 30B "
        f"model self-hosted on one RTX 4090 rented from Vast.ai."
    )
    st.link_button("Share on X", "https://x.com/intent/post?" + urllib.parse.urlencode(
        {"text": teaser, "url": "https://github.com/Kilo-Loco/github-story"}))


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
                """Resume the same generator: the status loop above stopped at
                the first chunk, so hand that one over and keep going."""
                if first_chunk:
                    yield first_chunk["text"]
                for event in events:
                    yield event["text"]

            st.session_state["story"] = st.write_stream(story_stream())
            st.session_state["story_url"] = url
            share_button(url.rstrip("/").split("/")[-1])

        except Exception as exc:
            # The app serves in ~90 seconds; the model needs several minutes more
            # to load. Anyone arriving in that window gets a connection error, so
            # say what is happening instead of showing a Python exception name.
            name = type(exc).__name__
            if "Connection" in name or "APIError" in name or "Timeout" in name:
                st.warning(
                    "The GPU is still waking up — the model takes a few minutes "
                    "to load after the site comes online. Try again shortly."
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
    share_button(st.session_state.get("story_url", "").rstrip("/").split("/")[-1])

st.divider()
st.caption(
    "Written by a self-hosted **Qwen3-Coder-30B-A3B** (Q4_K_M) on a single "
    "**RTX 4090** rented from [Vast.ai](https://vast.ai) — no frontier API "
    "involved. Public commit history only."
)
st.caption(
    f"Run this yourself on a 4090: [one-click Vast.ai template]({TEMPLATE_URL}) · "
    "[source](https://github.com/Kilo-Loco/github-story)"
)
