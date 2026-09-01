"""
app.py — the entire UI. Everything with substance lives in pipeline.py;
this file only turns pipeline events into Streamlit widgets.
"""

import threading

import streamlit as st

import pipeline

st.set_page_config(page_title="GitHub Story", page_icon="📖")


# One story at a time. The site is public and every story is GPU time on a
# single 4090, so a global lock is the whole abuse story: a second visitor
# waits rather than queueing a parallel decode that halves everyone's speed.
@st.cache_resource
def _gpu_lock() -> threading.Lock:
    return threading.Lock()


# Same profile twice inside an hour = zero GitHub calls. Cheapest guard there is.
@st.cache_data(ttl=3600, show_spinner=False)
def _cached_fetch(url: str) -> dict:
    return pipeline.fetch_all(url)


st.title("📖 GitHub Story")
st.caption("Paste a GitHub profile or repo. Get the story of what they've been building.")

url = st.text_input("GitHub profile or repo URL", placeholder="https://github.com/torvalds")
voice = st.selectbox("Narrator", list(pipeline.STORY_VOICES))

# Gate on a button, not on the text input: Streamlit reruns the whole script on
# every widget interaction, and an ungated pipeline would refetch and regenerate
# each time.
if st.button("Tell me their story", type="primary", disabled=not url.strip()):
    if _gpu_lock().locked():
        st.warning("Someone else's story is generating right now — try again in a minute.")
    else:
        with _gpu_lock():
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

            except Exception as exc:  # rate limits, empty profiles, endpoint down
                st.error(f"{type(exc).__name__}: {exc}")

# Survive the rerun that any later widget interaction causes.
elif st.session_state.get("story"):
    st.markdown(st.session_state["story"])

st.divider()
st.caption(
    "Written by a self-hosted **Qwen3-Coder-30B** (Q4) on a single **RTX 4090** "
    "rented from [Vast.ai](https://vast.ai) — no frontier API involved. "
    "Public commit history only."
)
