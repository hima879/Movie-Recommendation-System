import re
import pickle
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Movie Recommender", page_icon="🎬", layout="wide")
st.title("🎬 Movie Recommender System")

# ----------------------------------------------------------------
# 1. Load pickle files (works no matter where you launch from)
# ----------------------------------------------------------------
BASE = Path(__file__).resolve().parent


def find_file(name):
    for d in (BASE, BASE.parent):
        p = d / name
        if p.exists():
            return p
    return None


@st.cache_data(show_spinner=False)
def load_data():
    movies_path = find_file("movie_dict.pkl")
    top30_path = find_file("similarity_top30.pkl")
    sim_path = find_file("similarity.pkl")

    if not movies_path:
        st.error(f"❌ movie_dict.pkl not found in {BASE} or {BASE.parent}")
        st.stop()

    movies = pd.DataFrame(pickle.load(open(movies_path, "rb")))

    # Prefer the small top-30 indices file; fall back to the full matrix
    if top30_path:
        similarity = pickle.load(open(top30_path, "rb"))
        use_topk = True
    elif sim_path:
        similarity = pickle.load(open(sim_path, "rb"))
        use_topk = False
    else:
        st.error("❌ Neither similarity_top30.pkl nor similarity.pkl found.")
        st.stop()

    return movies, similarity, use_topk


movies, similarity, USE_TOPK = load_data()


# ----------------------------------------------------------------
# 2. Poster sources (no API key needed for any of them)
# ----------------------------------------------------------------
HEADERS = {"User-Agent": "Mozilla/5.0 (MovieRecommender/1.0; educational project)"}
TIMEOUT = 8


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _split_title(title):
    title = str(title)
    year = re.search(r"\((\d{4})\)", title)
    clean = title.split(" (")[0].strip()
    return clean, (int(year.group(1)) if year else None)


# --- Source 1: iTunes Search API (official, keyless) ---------------
def src_itunes(title):
    clean, year = _split_title(title)
    try:
        r = requests.get(
            "https://itunes.apple.com/search",
            params={"term": clean, "media": "movie", "entity": "movie",
                    "limit": 10, "country": "US"},
            headers=HEADERS, timeout=TIMEOUT,
        )
        results = r.json().get("results", [])
    except Exception:
        return None

    best = None
    for it in results:
        if _norm(it.get("trackName")) != _norm(clean):
            continue
        if year and not str(it.get("releaseDate", "")).startswith(str(year)):
            best = best or it
            continue
        best = it
        break
    if best and best.get("artworkUrl100"):
        return best["artworkUrl100"].replace("100x100bb", "600x600bb")
    return None


# --- Source 2: IMDb suggestion endpoint (unofficial, keyless) ------
def src_imdb(title):
    clean, year = _split_title(title)
    q = _norm(clean)
    if not q:
        return None
    url = f"https://v2.sg.media-imdb.com/suggestion/{q[0]}/{quote(clean.lower())}.json"
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        data = r.json()
    except Exception:
        return None

    best = None
    for it in data.get("d", []):
        img = (it.get("i") or {}).get("imageUrl")
        if not img or it.get("qid") not in ("movie", "tvMovie"):
            continue
        if _norm(it.get("l")) != q:
            continue
        if year and it.get("y") == year:
            return img
        best = best or img
    return best


# --- Source 3: Wikipedia page summary ------------------------------
def src_wiki_rest(title):
    clean, year = _split_title(title)
    names = ([f"{clean} ({year} film)"] if year else []) + [f"{clean} (film)", clean]
    for name in names:
        try:
            r = requests.get(
                "https://en.wikipedia.org/api/rest_v1/page/summary/"
                + quote(name.replace(" ", "_"), safe=""),
                headers=HEADERS, timeout=TIMEOUT,
            )
        except Exception:
            continue
        if r.status_code != 200:
            continue
        j = r.json()
        if j.get("type") == "disambiguation":
            continue
        img = (j.get("originalimage") or {}).get("source") \
            or (j.get("thumbnail") or {}).get("source")
        if img:
            return img
    return None


# --- Source 4: Wikipedia search API --------------------------------
def src_wiki_search(title):
    clean, _ = _split_title(title)
    try:
        r = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "format": "json", "generator": "search",
                    "gsrsearch": f"{clean} film", "gsrlimit": 3,
                    "prop": "pageimages", "piprop": "original", "redirects": 1},
            headers=HEADERS, timeout=TIMEOUT,
        )
        pages = r.json().get("query", {}).get("pages", {})
    except Exception:
        return None

    for p in sorted(pages.values(), key=lambda x: x.get("index", 99)):
        src = (p.get("original") or {}).get("source")
        if src:
            return src
    return None


SOURCES = [("iTunes", src_itunes), ("IMDb", src_imdb),
           ("Wikipedia", src_wiki_rest), ("Wikipedia search", src_wiki_search)]


@st.cache_resource(show_spinner=False)
def _poster_store():
    return {}


def fetch_poster(movie_title: str):
    store = _poster_store()
    if movie_title in store:
        return store[movie_title]

    for _, fn in SOURCES:
        try:
            img = fn(movie_title)
        except Exception:
            img = None
        if img:
            store[movie_title] = img
            return img
    return None


# ----------------------------------------------------------------
# 3. Recommend function (supports top-30 indices OR full matrix)
# ----------------------------------------------------------------
def recommend(movie_name):
    idx = movies[movies["title"] == movie_name].index[0]

    if USE_TOPK:
        # similarity[idx] is already a 1-D array of top-N movie indices,
        # sorted best-first (excluding self)
        neighbor_indices = similarity[idx][:5]
        top = [(int(i), 0.0) for i in neighbor_indices]
    else:
        distances = similarity[idx]
        top = sorted(enumerate(distances), reverse=True, key=lambda x: x[1])[1:6]

    names = [movies.iloc[i[0]].title for i in top]

    with st.spinner("Fetching posters..."):
        with ThreadPoolExecutor(max_workers=5) as ex:
            posters = list(ex.map(fetch_poster, names))

    st.write("### Recommended Movies")
    cols = st.columns(5)
    for col, name, url in zip(cols, names, posters):
        with col:
            if url:
                st.image(url, use_container_width=True)
            else:
                st.write("🎞️")
                st.caption("No poster")
            st.caption(name)


# ----------------------------------------------------------------
# 4. UI
# ----------------------------------------------------------------
with st.sidebar:
    st.subheader("Debug")
    st.caption(f"Using {'top-30 indices' if USE_TOPK else 'full matrix'}")
    if st.button("Test poster sources"):
        for label, fn in SOURCES:
            try:
                res = fn("Aliens (1986)")
                st.write(f"**{label}:**", "✅ " + res if res else "⚠️ reachable, no image")
            except Exception as e:
                st.write(f"**{label}:** ❌ {type(e).__name__}")

option = st.selectbox("Select a movie", movies["title"].values)

if st.button("Recommend"):
    recommend(option)