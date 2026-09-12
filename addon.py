#!/usr/bin/env python3
"""
AnimeDekho — a Stremio addon for animedekho.app (Hindi-dub anime/cartoons).

STRICT ZERO-BANDWIDTH RULE (user directive, same as moviebox/netmirror)
    NOTHING but tiny JSON is served from here. Cards point DIRECTLY at the
    host's signed HLS master (as-cdn{N}.top/cdn/hls/…/master.m3u8?md5=…)
    which needs NO headers at all, and subtitles point DIRECTLY at the
    host's caption CDN (as-cdn{N}.top/p/….jpg — actually plain SRT).
    The media chain (master -> variant playlists -> .js/.css-disguised
    segments) flows host <-> player with zero involvement of this server.

RESOLVE RECIPE (all fetches are tiny HTML/JSON text)
    1. cinemeta           -> title/year from the IMDb id
    2. animedekho.app/?s= -> result cards (series-hindi / movie-hindi)
    3. series page        -> WP postId (batch/get-links.php?id=N) + the
                             /epi/{slug}-{SE}x{EP}/ list   [movie: the
                             page carries /embed/{id} directly]
    4. /embed/{postId}/{se}-{ep}   (NO cookie — the site's ad-gate only
                             guards the website UX, not the embed)  ->
                             iframe  as-cdn{N}.top/video/{vid}
    5. POST as-cdn{N}.top/player/index.php?data={vid}&do=getVideo
       with header  X-Requested-With: XMLHttpRequest  (the ONLY gate)
                             -> {"hls":true,"videoSource": master.m3u8}
    6. master.m3u8        -> parse audio langs + resolutions (and verify
                             it is alive — no phantom cards, ever)

    The signed master lives ~2h, so card lists are cached 45min and
    stale-served max 90min while a refresh runs behind the curtain.
"""

import base64
import gzip
import hashlib
import html as _html
import io
import json
import os
import random
import re
import threading
import time
import unicodedata
from concurrent.futures import (ThreadPoolExecutor, as_completed,
                                TimeoutError as FuturesTimeoutError)
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote, urljoin

import requests

# --------------------------------------------------------------------------
# 1. config
# --------------------------------------------------------------------------
VERSION = "1.6.4"
BRAND   = "AnimeDekho"
PORT    = int(os.environ.get("PORT", "7000"))
PUBLIC_URL = os.environ.get("ADK_PUBLIC_URL", "").rstrip("/")
SITE    = "https://animedekho.app"
CINEMETA = "https://v3-cinemeta.strem.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_STREAM_CACHE_TTL = 45 * 60        # signed master lives ~2h — keep cards fresher
_STREAM_STALE_TTL = 90 * 60       # SWR ceiling (must stay < signature life)
_SEARCH_TTL   = 3600
_PAGE_TTL     = 6 * 3600
_META_TTL     = 12 * 3600
_MASTER_TTL   = 45 * 60
_NEG_TTL      = 300               # honest "not there" answers may be re-tried soon
_CARD_TTL     = 40 * 60          # resolved-card cache (must stay < _MASTER_TTL
                                 # so a cached card never outlives its relay)
_CARD_STALE_TTL = 110 * 60       # SWR ceiling for cards
_PREWARM_EVERY   = 10 * 60       # background warm cycle for the newest posts
_WALL         = 20.0              # player-facing wall for one /stream build

# v1.6.0: trdekho players (vidmoly) use ISO 639-1 two-letter codes
_LANG_NAME = {"hi": "Hindi", "en": "English", "ja": "Japanese",
              "te": "Telugu", "ta": "Tamil", "kn": "Kannada",
              "ml": "Malayalam", "mr": "Marathi", "bn": "Bangla",
              "jpn": "Japanese", "hin": "Hindi", "eng": "English",
              "tel": "Telugu", "tam": "Tamil", "kan": "Kannada",
              "mal": "Malayalam", "mar": "Marathi", "ben": "Bangla",
              "und": "", "": ""}

MANIFEST = {
    "id": "com.animedekho.stremio",
    "version": VERSION,
    "name": BRAND,
    "description": ("Hindi-dub anime & cartoons from AnimeDekho — direct "
                    "multi-audio HLS (Japanese / Hindi / English / Telugu / "
                    "Tamil), 240p-1080p, English subs. Zero-bandwidth addon: "
                    "only tiny JSON is served, media flows directly from the "
                    "CDN to your player."),
    "types": ["movie", "series"],
    "resources": ["stream"],
    "idPrefixes": ["tt", "kitsu", "anilist", "mal"],
    "catalogs": [],
}

# --------------------------------------------------------------------------
# 2. utilities — TTL caches (with v1.9.3 sweep lesson applied)
# --------------------------------------------------------------------------
_CACHE_SWEEP_AT = 512

def _cache_put(store, key, val, ttl):
    if len(store) >= _CACHE_SWEEP_AT:
        now = time.time()
        for k in [k for k, ent in store.items() if ent[1] < now]:
            store.pop(k, None)
    store[key] = (val, time.time() + ttl)

def _cache_get(store, key):
    ent = store.get(key)
    if not ent:
        return False, None
    val, exp = ent
    if time.time() > exp:
        store.pop(key, None)
        return False, None
    return True, val

_META_CACHE   = {}   # (ctype, imdb) -> (name, year)
_SEARCH_CACHE = {}   # kw -> [(title, url, family)]
_PAGE_CACHE   = {}   # url -> page-info dict
_MASTER_CACHE = {}   # master url -> {"info", "master_text", "variants"}
_HLS_KEYS      = {}   # 16-hex route key -> master url (served-playlist registry)
_VARIANT_CACHE = {}   # variant url -> (absolutized text, expiry)
_CARD_CACHE   = {}   # (post_id, se, ep) -> card dict (movies: ep=0)
_CARD_STALE   = {}   # key -> (expiry, card, refresh-args)      [SWR]
_STREAM_CACHE = {}   # (ctype, imdb, se, ep) -> (cards, expiry)
_STREAM_STALE = {}   # key -> (expiry, cards)   [SWR]
_STALE_SWEEP_AT = 256

# v1.5.0: one shared IO pool for the parallel innards (search queries run
# together, subs race the master chain, candidates resolve concurrently)
_IO_EX = ThreadPoolExecutor(max_workers=10, thread_name_prefix="io")
_SUBS_EX = ThreadPoolExecutor(max_workers=4, thread_name_prefix="subs")
_PREWARM = {"cycles": 0, "cards": 0, "posts": 0, "last": 0.0, "epis": []}
# v1.5.0: in-memory index of the newest site posts (title/url/family).
# A request whose title matches an indexed post skips the site search
# entirely — with the page+card caches warm that makes a fresh-episode
# stream request answer in milliseconds.
_LATEST = {"posts": [], "ts": 0.0}
_LATEST_TTL = 2 * 3600

def _latest_candidates(title, family):
    """index hit? -> candidate dicts for _match_candidates, else None."""
    if time.time() - _LATEST["ts"] > _LATEST_TTL or not _LATEST["posts"]:
        return None
    out = [{"title": p["title"], "url": p["url"], "family": p["family"]}
           for p in _LATEST["posts"]
           if p.get("family") == family and p.get("title")]
    return out or None

def _stale_put(key, cards):
    if len(_STREAM_STALE) >= _STALE_SWEEP_AT:
        now = time.time()
        for k in [k for k, ent in _STREAM_STALE.items() if ent[0] < now]:
            _STREAM_STALE.pop(k, None)
    _STREAM_STALE[key] = (time.time() + _STREAM_STALE_TTL, cards)

_S = requests.Session()
_S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})

# ---- v1.1.0: egress fallback pool ------------------------------------------
# animedekho.app 403s datacenter IPs (verified 2026-09-11: /debug/search from
# prod = 403/0 cards, same UA from other IPs = 200 + cards). Same problem
# moviebox solved with its free-proxy pool — simplified here (no tokens,
# plain HTML site):
#   * direct egress is tried first and re-probed every 10 min
#   * on 403/406 direct is benched 10 min and a free-proxy pool takes over
#   * pool: proxyscrape public text list (http:// entries only), exits
#     probed against the site itself, 20 usable kept, dead exits benched
#     10 min, site-blocked exits 15 min, one good exit sticky for 90s so
#     a resolve chain rides the SAME exit
#   * cinemeta / TMDB are exempt (they never block us; no need to burn
#     proxy bandwidth)
_POOL_SRC = os.environ.get(
    "ANIMEDEKHO_PROXY_SOURCE",
    "https://api.proxyscrape.com/v4/free-proxy-list/get"
    "?request=display_proxies&proxy_format=protocolipport&format=text",
).strip()
_POOL_EXEMPT = ("v3-cinemeta.strem.io", "api.themoviedb.org")
_FREE_POOL = [[]]                 # alive free exits (http://ip:port)
_POOL_BAD = {}                    # url -> benched-until ts
_POOL_STICKY = [None, 0.0]        # last good exit, sticky-until ts
_POOL_TS = [0.0]                  # last refresh start (throttle 4 min)
_POOL_LOCK = threading.Lock()
_DIRECT_OK_UNTIL = [time.time()]  # direct egress believed healthy until
_DIRECT_RETRY_AT = [0.0]          # earliest re-probe after a block

def _site_ok_via(url, timeout=5):
    """Usable free exit? -> (url, latency_s) or None. v1.5.0: measure the
    latency so the pool can be ranked — _pick_exit then rides the
    fastest exits first (a 0.6s exit vs a 3s one is the difference
    between a 2s and an 8s cold build)."""
    t0 = time.time()
    try:
        r = _S.get(SITE + "/", timeout=timeout, headers={"User-Agent": UA},
                   proxies={"http": url, "https": url})
        if (r.status_code == 200
                and "animedekho" in r.text[:20000].lower()):
            return (url, round(time.time() - t0, 3))
        return None
    except Exception:
        return None

def _pool_refresh(force=False):
    now = time.time()
    if not force and now - _POOL_TS[0] < 240:
        return
    with _POOL_LOCK:
        if now - _POOL_TS[0] < 240:            # someone refreshed meanwhile
            return
        _POOL_TS[0] = now
    try:
        r = _S.get(_POOL_SRC, timeout=20, headers={"User-Agent": UA})
        cand = [u.strip() for u in r.text.replace("\r", "").splitlines()
                if u.strip().startswith("http://")]
        random.shuffle(cand)
        cand = cand[:120]
        good = []
        with ThreadPoolExecutor(max_workers=24) as ex:
            for res in ex.map(_site_ok_via, cand):
                if res:
                    good.append(res)          # [(url, latency)]
        good.sort(key=lambda x: x[1])         # fastest first
        with _POOL_LOCK:
            prev = [u for u in _FREE_POOL[0]
                    if _POOL_BAD.get(u, 0.0) <= time.time()]
            pmap = {u: l for u, l in prev}
            for u, l in good:
                pmap[u] = min(pmap.get(u, 99.0), l)   # keep best latency
            merged = sorted(pmap.items(), key=lambda x: x[1])[:25]
            _FREE_POOL[0] = merged or prev
    except Exception:
        pass

def _pick_exit(now):
    if _POOL_STICKY[0] and now < _POOL_STICKY[1]:
        u = _POOL_STICKY[0]
        if _POOL_BAD.get(u, 0.0) <= now:
            return u
    with _POOL_LOCK:
        live = [(u, l) for u, l in _FREE_POOL[0]
                if _POOL_BAD.get(u, 0.0) <= now]
    if not live:
        return None
    # v1.5.0: random among the 3 FASTEST exits (pure best-of-one would
    # overload a single free proxy; top-3 keeps latency without the
    # thundering herd)
    top = [u for u, _l in live[:3]]
    return random.choice(top) if top else live[0][0]

class _DeadResponse:
    status_code = 0
    text = ""
    def json(self):
        raise ValueError("dead response")

_CDN_RE = re.compile(r"https://as-cdn\d+\.top/")
# v1.6.1: trdekho OPEN player hosts (vidmoly/emturbo embeds, masters,
# VTT subs) — verified open cross-IP, so they get the as-cdn treatment:
# direct tried, NEVER benched, pool only as a 403 fallback. This keeps
# the player chain fast on prod, where the site family must crawl
# through pool proxies (v1.6.0 gave them fam_site routing and the
# 20s wall was missed — 20.5s prod timing, zero cards).
_PLAYER_OPEN = ("vidmoly.", "emturbovid.com", "turboviplay.com",
                "vmnow.online", "srt.vidmoly.me")

def _get(url, timeout=8, referer=None):
    # plain requests (no shared-session locking): the site needs NO cookies,
    # so per-call headers on a pooled session are safe and parallel-friendly.
    # v1.1.0: site-family URLs carry the egress fallback (direct-first,
    # pool when the datacenter IP is 403-blocked).
    # v1.2.0: three families — SITE (animedekho.app: direct-first, direct
    # benched 10 min on 403), CDN (as-cdnN.top: direct tried but NEVER
    # benched — ip-bound masters 403 by design while variants/segments are
    # open, so benching would only slow the open ones down), and everything
    # else (cinemeta/tmdb/debug urls: plain direct, no pool ever).
    hd = {"User-Agent": UA}
    if referer:
        hd["Referer"] = referer
    fam_cdn = bool(_CDN_RE.match(url)) or any(h in url for h in _PLAYER_OPEN)
    fam_site = "animedekho.app" in url
    if not fam_cdn and not fam_site:
        return _S.get(url, headers=hd, timeout=timeout)
    now = time.time()
    if fam_cdn or now < _DIRECT_OK_UNTIL[0] or now >= _DIRECT_RETRY_AT[0]:
        try:
            r = _S.get(url, headers=hd, timeout=timeout)
        except Exception:
            r = None
        if r is not None and r.status_code not in (403, 406):
            if fam_site:
                _DIRECT_OK_UNTIL[0] = time.time() + 600
                _DIRECT_RETRY_AT[0] = 0.0
            return r
        if fam_site:                               # real block signal
            _DIRECT_OK_UNTIL[0] = 0.0
            _DIRECT_RETRY_AT[0] = time.time() + 600   # re-probe in 10 min
    for _ in range(3):
        u = _pick_exit(time.time())
        if u is None:
            _pool_refresh(force=True)
            u = _pick_exit(time.time())
            if u is None:
                break
        try:
            r = _S.get(url, headers=hd, timeout=timeout,
                       proxies={"http": u, "https": u})
            if r.status_code in (403, 406):
                _POOL_BAD[u] = time.time() + 900   # exit blocked by site
            else:
                _POOL_STICKY[0] = u                # [url, expiry] pair
                _POOL_STICKY[1] = time.time() + 90
                return r
        except Exception:
            _POOL_BAD[u] = time.time() + 600       # dead exit
        _POOL_STICKY[0] = None
        _POOL_STICKY[1] = 0.0
    try:                                            # last resort: direct
        return _S.get(url, headers=hd, timeout=timeout)
    except Exception:
        return _DeadResponse()

def _norm(t):
    # v1.4.1: NFKD + ascii fold FIRST — 'Ranma ½' vs the site's 'Ranma 1/2'
    # both become 'ranma12' (½ -> '1⁄2' -> '12'); full-width '！' -> '!'…
    t = unicodedata.normalize("NFKD", (t or "").lower())
    t = t.encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", t)

def _clean_title(t):
    """Strip trailing dub/sub qualifiers: 'Naruto - Hindi Dub' -> 'Naruto'.
    v1.4.0: also strips trailing parentheticals the site loves —
    'Demon Slayer Infinity Castle (Official)', 'Your Name. (Official Dub)',
    'One Piece Film Red (Camrip)'."""
    t = (t or "").strip()
    t = re.sub(r"\s*\((official|camrip|fandub|[a-z ]*dub[a-z ]*|"
               r"[a-z ]*sub[a-z ]*)\)\s*$", "", t, flags=re.I).strip()
    pat = re.compile(r"\s*[-|:–—]?\s*(hindi|english|japanese|tamil|telugu|dub(bed)?"
                     r"|sub(titl(ed|es))?)(\s+(dub(bed)?|sub(titl(ed|es))?))*$",
                     re.I)
    for _ in range(3):                     # 'Hindi Dub', 'English Sub', combos
        t2 = pat.sub("", t).strip()
        if t2 == t:
            break
        t = t2
    return re.sub(r"\s+", " ", t)

# --------------------------------------------------------------------------
# 3. metadata (cinemeta)
# --------------------------------------------------------------------------
_TMDB_KEY = os.environ.get("TMDB_API_KEY", "adc48d20c0956934fb224de5c40bb85d")

def _cinemeta(ctype, imdb):
    hit, val = _cache_get(_META_CACHE, (ctype, imdb))
    if hit:
        return val
    try:
        r = _get("%s/meta/%s/%s.json" % (CINEMETA, ctype, imdb), timeout=5)
        m = ((r.json() or {}).get("meta") or {})
        name, year = m.get("name") or "", str(m.get("year") or "")
        if name:
            val = (name, year)
            _cache_put(_META_CACHE, (ctype, imdb), val, _META_TTL)
            return val
    except Exception:
        pass
    # fallback: TMDB find by imdb id (cinemeta occasionally answers {})
    try:
        r = _get("https://api.themoviedb.org/3/find/%s?api_key=%s"
                 "&external_source=imdb_id" % (imdb, _TMDB_KEY), timeout=5)
        d = r.json().get("movie_result" if ctype == "movie" else "tv_result") or []
        if d:
            name = d[0].get("title") or d[0].get("name") or ""
            year = str((d[0].get("release_date") or d[0].get("first_air_date")
                        or "")[:4])
            if name:
                val = (name, year)
                _cache_put(_META_CACHE, (ctype, imdb), val, _META_TTL)
                return val
    except Exception:
        pass
    return None

# --------------------------------------------------------------------------
# 4. site search + page parsing
# --------------------------------------------------------------------------
def _extract_cards(html):
    """Result cards, robust to BOTH layouts the theme uses:
       a) <img post-id="N" ... title="Name" .../> ... <a href=… lnk-blk>
       b) <h2 class="entry-title">Name</h2> ... <img alt="Name"/> ... <a href=… lnk-blk>
       Parses per-<article> chunk so titles can never bleed across cards."""
    out = []
    for chunk in re.split(r"<article", html)[1:]:
        m = re.search(r'href="(https://animedekho\.app/'
                      r'((?:series|movie)-hindi)/[a-z0-9-]+/)"'
                      r'[^>]*class="lnk-blk"', chunk)
        if not m:
            continue
        # title sources sit BEFORE the href — scan only that window (the
        # last <article> chunk swallows the rest of the page: sidebar,
        # partner-site cards, footer … whose titles must never bleed in)
        win = chunk[max(0, m.start() - 2000):m.start()]
        t = None
        for a, b, c in re.findall(
                r'title="([^"]{2,120})"|<h2 class="entry-title">([^<]{2,120})'
                r"</h2>|alt=\"([^\"]{2,120})\"", win):
            t = a or b or c or t          # nearest-to-href wins (last found)
        if not t:
            continue
        y = re.search(r'class="year">(\d{4})<', win)
        out.append({"title": _html.unescape(t.strip()), "url": m.group(1),
                    "family": m.group(2), "year": y.group(1) if y else ""})
    return out

def _kitsu_title(kid):
    """kitsu:<id> -> (title, year) via the public kitsu.io API (v1.3.0 —
    Stremio's anime catalogs use KITSU ids; without this the addon never
    showed a stream in them). anime-kitsu.strem.io is dead (DNS).
    v1.4.1: prefer titles.EN over canonicalTitle — canonical is usually
    the romaji ('Ore dake Level Up na Ken: Season 2...') while the site
    lists English names ('Solo Leveling'); romaji searches always miss."""
    hit, val = _cache_get(_META_CACHE, ("kitsu", kid))
    if hit:
        return val
    val = None
    try:
        r = _get("https://kitsu.io/api/edge/anime/%s" % kid, timeout=6)
        a = ((r.json() or {}).get("data") or {}).get("attributes") or {}
        t = (a.get("titles") or {}).get("en") or a.get("canonicalTitle") \
            or (a.get("titles") or {}).get("en_jp") or ""
        if t:
            val = (t, str(a.get("startDate") or "")[:4])
            _cache_put(_META_CACHE, ("kitsu", kid), val, _META_TTL)
    except Exception:
        pass
    return val

# v1.4.1: kitsu catalogs split every season into its OWN single-season
# entry ('DAN DA DAN Season 2' = kitsu 49425, eps 1x1..1x12) while the
# site packs the whole series into one post numbered 2x1..2x12 — the
# direct (se, ep) lookup misses and the user sees no stream. Derive
# WHICH site season the entry is by counting same-franchise TV entries
# (kitsu filter[text], sorted by startDate) up to and including ours.
_SEASON_MARK_RE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)\s+season|season\s+\d{1,2}"
    r"|part\s+\d{1,2}|cour\s+\d{1,2}|final\s+season)\b", re.I)


def _season_stripped(t):
    """remove 'Season 2'/'2nd Season'/'Part 2' markers anywhere in the
    title — 'Solo Leveling Season 2 -Arise from the Shadow-' ->
    'Solo Leveling -Arise from the Shadow-'."""
    return _SEASON_MARK_RE.sub(" ", t or "").strip(" \t-\u2013\u2014:|")


def _kitsu_season_index(kid):
    """kitsu entry -> 1-based season index within its franchise (cached;
    None when undeterminable — then the strict (se, ep) lookup stands)."""
    hit, val = _cache_get(_META_CACHE, ("kseas", kid))
    if hit:
        return val
    idx = None
    try:
        meta = _kitsu_title(kid)
        if meta:
            base = _season_stripped(meta[0]).split()
            q = " ".join(base[:2]) if len(base) >= 2 else (base[0] if base else "")
            if q:
                r = _get("https://kitsu.io/api/edge/anime?filter%5Btext%5D="
                         + quote(q)
                         + "&page%5Blimit%5D=20&sort=startDate", timeout=8)
                n = 0
                for a in ((r.json() or {}).get("data") or []):
                    at = a.get("attributes") or {}
                    if at.get("subtype") != "TV":
                        continue        # movies/specials/OVAs skew the count
                    n += 1
                    if str(a.get("id")) == str(kid):
                        idx = n
                        break           # startDate-sorted: ours == the index
    except Exception:
        idx = None
    _cache_put(_META_CACHE, ("kseas", kid), idx, _META_TTL)
    return idx


def _season_for_ep(eps, se, ep, season_index):
    """the site season that actually serves (ep): the direct hit first,
    then the kitsu-derived index, then the unique season containing ep
    (One Piece pattern: kitsu 1x1100 lives at the site's 22x1100)."""
    if (se, ep) in eps:
        return se
    if season_index and (season_index, ep) in eps:
        return season_index
    alts = sorted({s for (s, e) in eps if e == ep})
    if len(alts) == 1:
        return alts[0]
    return None


# ---- v1.5.0: AniList / MAL ids -------------------------------------------
# Stremio anime catalogs also exist with anilist:/mal: ids — one cached
# GraphQL call resolves either to (title, year), titles.english first
# (same romaji-vs-English lesson as kitsu). Season entries get the same
# franchise-count index via AniList search (TV format, startDate-sorted).
_ANILIST_GQL = "https://graphql.anilist.co"

def _anilist_title(qid, idmal=False):
    key = ("al", ("mal" if idmal else "an"), str(qid))
    hit, val = _cache_get(_META_CACHE, key)
    if hit:
        return val
    val = None
    try:
        gql = ("query($id:Int,$mal:Int){Media(id:$id,idMal:$mal,type:ANIME)"
               "{id format title{english romaji} startDate{year}}}")
        var = {"mal": int(qid)} if idmal else {"id": int(qid)}
        r = _S.post(_ANILIST_GQL, json={"query": gql, "variables": var},
                    headers={"User-Agent": UA}, timeout=8)
        m = ((r.json() or {}).get("data") or {}).get("Media") or {}
        t = (m.get("title") or {}).get("english") or (m.get("title") or {}).get("romaji") or ""
        if t:
            val = (t, str((m.get("startDate") or {}).get("year") or ""))
            _cache_put(_META_CACHE, key, val, _META_TTL)
            if m.get("id"):        # mal -> anilist id bridge for season index
                _cache_put(_META_CACHE, ("alid", str(qid)),
                           str(m["id"]), _META_TTL)
    except Exception:
        pass
    return val

def _anilist_season_index(qid, idmal=False):
    """1-based season index via same-franchise TV entries, startDate-sorted
    (kitsu _kitsu_season_index pattern, AniList flavour)."""
    key = ("alseas", ("mal" if idmal else "an"), str(qid))
    hit, val = _cache_get(_META_CACHE, key)
    if hit:
        return val
    idx = None
    try:
        meta = _anilist_title(qid, idmal)
        mine = None
        if meta:
            gql = ("query($s:String){Page(perPage:20){media(search:$s,"
                   "type:ANIME,format:TV,sort:START_DATE_ASC)"
                   "{id startDate{year month day}}}}")
            base = _season_stripped(meta[0]).split()
            q = " ".join(base[:2]) if len(base) >= 2 else (base[0] if base else "")
            if q:
                r = _S.post(_ANILIST_GQL,
                            json={"query": gql, "variables": {"s": q}},
                            headers={"User-Agent": UA}, timeout=8)
                rows = ((((r.json() or {}).get("data") or {})
                         .get("Page") or {}).get("media")) or []
                n = 0
                mine = str(qid)
                if idmal:
                    _hit, _alid = _cache_get(_META_CACHE, ("alid", str(qid)))
                    mine = _alid if (_hit and _alid) else None
                if mine:
                    for a in rows:
                        n += 1
                        if str(a.get("id")) == mine:
                            idx = n
                            break
    except Exception:
        idx = None
    _cache_put(_META_CACHE, key, idx, _META_TTL if idx else _NEG_TTL)
    return idx


def site_search(kw):
    """/?s= result cards -> [(title, url, family)] (series-hindi|movie-hindi)."""
    key = kw.strip().lower()
    hit, val = _cache_get(_SEARCH_CACHE, key)
    if hit:
        return val or []
    out = []
    try:
        r = _get(SITE + "/?s=" + quote(kw), timeout=9)
        if r.status_code == 200:
            out = _extract_cards(r.text)
    except Exception:
        return out          # empty: caller treats as "not found" (short TTL)
    _cache_put(_SEARCH_CACHE, key, out or None, _SEARCH_TTL if out else _NEG_TTL)
    return out

def search_candidates(title):
    """The site search word-ANDs, so long official titles can miss. Try the
    full title plus progressively shorter prefixes (results merged; every
    step is cached).
    v1.5.0: all queries fire CONCURRENTLY — sequential prefixes cost
    3-4 proxied roundtrips (~1.5s each) on the cold path; now the wall
    time is one roundtrip. Merge order keeps the full-title answer first
    so matching tiers still see the best candidates first."""
    seen, out = set(), []
    words = re.sub(r"[^\w\s]", " ", title).split()
    queries = [title]
    if len(words) >= 3:
        queries.append(" ".join(words[:3]))
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))
    if words and len(words[0]) >= 4:
        queries.append(words[0])
    uniq = list(dict.fromkeys(queries))[:4]
    if len(uniq) == 1:
        for c in site_search(uniq[0]):
            if c["url"] not in seen:
                seen.add(c["url"])
                out.append(c)
        return out
    results = list(_IO_EX.map(site_search, uniq))
    for q, cands in zip(uniq, results):
        for c in cands or []:
            if c["url"] not in seen:
                seen.add(c["url"])
                out.append(c)
    return out

def _parse_series_page(url):
    """series-hindi page -> {post_id, eps:set[(se,ep)], title} (6h cached)."""
    hit, val = _cache_get(_PAGE_CACHE, url)
    if hit:
        return val
    try:
        r = _get(url, timeout=10)
        if r.status_code != 200:
            return None
        h = r.text
        ids = re.findall(r"get-links\.php\?id=(\d+)", h)
        eps = set((int(se), int(ep)) for _, se, ep in
                  re.findall(r'href="https://animedekho\.app/epi/([a-z0-9-]+)-(\d+)x(\d+)/"', h))
        t = re.search(r'<h1 class="entry-title">([^<]*)</h1>', h)
        # fallback: an /embed/{id}/{se}-{ep} link also carries the post id
        if not ids:
            ids = re.findall(r"animedekho\.app/embed/(\d+)/\d+-\d+", h)
        if not ids or not eps:
            val = None
        else:
            val = {"post_id": ids[0], "eps": eps,
                   "title": _html.unescape(t.group(1).strip()) if t else ""}
        _cache_put(_PAGE_CACHE, url, val, _PAGE_TTL if val else _NEG_TTL)
        return val
    except Exception:
        return None

def _parse_movie_page(url):
    """movie-hindi page -> {post_id, embed} (6h cached).
    v1.4.0: movie pages hide the embed behind a 'Skip AD' gate — the page
    carries a form whose shortlink (24hr/verify.php?expires&token) sets the
    toronites_server cookie; ONE direct GET of it (no shortener maze) and
    a page reload reveals animedekho.app/embed/<id> (verified 2026-09-11)."""
    hit, val = _cache_get(_PAGE_CACHE, url)
    if hit:
        return val
    try:
        r = _get(url, timeout=10)
        if r.status_code != 200:
            return None
        h = r.text
        m = re.search(r'animedekho\.app/embed/(\d+)', h)
        if not m:
            sl = re.search(r'name="shortlink"\s+value="([^"]+)"', h)
            if sl and "verify.php" in sl.group(1):
                try:
                    _get(sl.group(1), timeout=10)      # sets the gate cookie
                    r = _get(url, timeout=10)          # reload: embed appears
                    h = r.text
                    m = re.search(r'animedekho\.app/embed/(\d+)', h)
                except Exception:
                    pass
        t = re.search(r'<h1 class="entry-title">([^<]*)</h1>', h)
        # v1.6.0: NEW movie posts (e.g. 'The Ribbon Hero') hide their
        # players in base64 data-(src|url|link) attributes, each decoding
        # to /?trdekho={0-8}&trid={postid}&trtype=1 — a multi-server
        # grid. Collect every one (dedup, order-preserving).
        tr_servers = []
        for b64 in re.findall(r'data-(?:src|url|link)="([A-Za-z0-9+/=]{16,})"', h):
            try:
                d = base64.b64decode(b64).decode("utf-8", "ignore")
            except Exception:
                continue
            if "trdekho=" in d:
                tr_servers.append(d if d.startswith("http")
                                  else urljoin(SITE + "/", d))
        tr_servers = list(dict.fromkeys(tr_servers))
        if not m and not tr_servers:
            val = None
        else:
            trid = ""
            if tr_servers:
                mt = re.search(r"[?&]trid=(\d+)", tr_servers[0])
                if mt:
                    trid = mt.group(1)
            # v1.6.0: BOTH patterns can coexist (death-note relight has
            # embed + tr_servers); no embed at all -> post_id = trid
            val = {"post_id": m.group(1) if m else trid,
                   "embed": (SITE + "/embed/" + m.group(1)) if m else None,
                   "tr_servers": tr_servers,
                   "title": _html.unescape(t.group(1).strip()) if t else ""}
        _cache_put(_PAGE_CACHE, url, val, _PAGE_TTL if val else _NEG_TTL)
        return val
    except Exception:
        return None

# --------------------------------------------------------------------------
# 5. host chain: embed -> player page -> getVideo -> master
# --------------------------------------------------------------------------
_IFRAME_RE = re.compile(r'<iframe[^>]*src="(https://as-cdn\d+\.top/video/[a-f0-9]+)"')

def _embed_iframe(embed_url):
    """embed page (NO cookie needed) -> (player_url, vid) or None."""
    try:
        r = _get(embed_url, timeout=8, referer=SITE + "/")
        m = _IFRAME_RE.search(r.text)
        return (m.group(1), m.group(1).split("/video/")[-1]) if m else None
    except Exception:
        return None

def _player_subs(player_url):
    """player page -> [{'lang','url'}] — 'playerjsSubtitle = "[Label]url'"."""
    try:
        r = _get(player_url, timeout=8, referer=SITE + "/")
        out = []
        for label, url in re.findall(
                r'playerjs\w*[Ss]ubtitle\w*\s*=\s*"\[([^\]]+)\](https?://[^"]+)"',
                r.text):
            lang = "eng" if "eng" in label.lower() else _norm(label)[:3]
            out.append({"url": url, "lang": lang or "eng", "id": "adk-" + (lang or "eng")})
        return out
    except Exception:
        return []

def _get_video(player_url, vid):
    """POST getVideo (AJAX header is the only gate) -> master url or None."""
    host = player_url.split("/video/")[0]
    try:
        r = _S.post(host + "/player/index.php?data=" + vid + "&do=getVideo",
                    headers={"X-Requested-With": "XMLHttpRequest",
                             "Referer": player_url, "User-Agent": UA},
                    data={"hash": vid, "r": SITE + "/"},
                    timeout=8)
        d = r.json()
        u = str(d.get("videoSource") or d.get("securedLink") or "")
        return u if u.startswith("http") else None
    except Exception:
        return None

def _hls_key(master_url):
    """stable 16-hex route key for a (long, ip-bound) master url"""
    return hashlib.md5(master_url.encode()).hexdigest()[:16]

def _rewrite_master(mtext, base):
    """v1.2.0: the CDN master is IP-BOUND to the pool exit that minted it
    (md5+expires secure_link; verified 2026-09-11: fresh master 403s from
    every other IP, even with Referer). Variants (as-cdn/hls/<token>, no
    signature) and segments (/p/<token>, absolute) are OPEN cross-IP.

    So we serve the master OURSELVES: every variant/audio URI is rewritten
    to OUR /hls/{key}/vN|aN.m3u8 route. Variant playlists are relayed as
    text (segments already absolute / absolutized) — media bytes still
    NEVER touch this server. Same pattern as moviebox v1.9.4."""
    lines_out, variants = [], {}
    pending_stream = False
    vi = ai = 0
    for ln in mtext.splitlines():
        s = ln.strip()
        if s.startswith("#EXT-X-STREAM-INF:"):
            lines_out.append(ln); pending_stream = True
            continue
        if pending_stream and s and not s.startswith("#"):
            name = "v%d.m3u8" % vi; vi += 1
            variants[name] = urljoin(base, s)
            lines_out.append(name)
            pending_stream = False
            continue
        if s.startswith("#EXT-X-MEDIA:") and "TYPE=AUDIO" in ln:
            m = re.search(r'URI="([^"]+)"', ln)
            if m:
                name = "a%d.m3u8" % ai; ai += 1
                variants[name] = urljoin(base, m.group(1))
                ln = ln.replace(m.group(1), name)
            lines_out.append(ln)
            continue
        lines_out.append(ln)
    return "\n".join(lines_out) + "\n", variants

def _variant_text(orig_url):
    """relay one variant playlist: fetch (open cross-IP), absolutize any
    relative media line, cache. -> text|None"""
    hit, val = _cache_get(_VARIANT_CACHE, orig_url)
    if hit:
        return val
    try:
        r = _get(orig_url, timeout=12)
    except Exception:
        return None
    if r.status_code != 200 or "#EXTM3U" not in r.text[:64]:
        _cache_put(_VARIANT_CACHE, orig_url, None, _NEG_TTL)
        return None
    out = []
    for ln in r.text.splitlines():
        s = ln.strip()
        if s and not s.startswith("#") and not s.startswith("http"):
            ln = urljoin(orig_url, s)
        out.append(ln)
    text = "\n".join(out) + "\n"
    _cache_put(_VARIANT_CACHE, orig_url, text, 45 * 60)
    return text

def _master_info(master_url):
    """verify + parse the master (no phantom cards) AND register the
    served-playlist entry. -> info dict | None."""
    hit, val = _cache_get(_MASTER_CACHE, master_url)
    if hit:
        if val:
            _HLS_KEYS[_hls_key(master_url)] = master_url
        return val["info"] if val else None
    val = None
    try:
        r = _get(master_url, timeout=10)
        if r.status_code == 200 and "#EXTM3U" in r.text[:64]:
            langs = sorted(set(re.findall(r'LANGUAGE="([a-z]{2,3})"', r.text)))
            res = sorted(set(int(x.split("x")[1])
                             for x in re.findall(r"RESOLUTION=(\d+x\d+)", r.text)))
            mtext, variants = _rewrite_master(r.text, master_url)
            val = {"info": {"langs": langs, "res": res, "audio_rends":
                            re.findall(r'TYPE=AUDIO[^>]*LANGUAGE="([a-z]{2,3})"[^>]*NAME="([^"]*)"',
                                       r.text)},
                   "master_text": mtext, "variants": variants}
            _HLS_KEYS[_hls_key(master_url)] = master_url
    except Exception:
        pass
    _cache_put(_MASTER_CACHE, master_url, val, _MASTER_TTL)
    return val["info"] if val else None

# --------------------------------------------------------------------------
# 6. card building
# --------------------------------------------------------------------------
def _card_refresh(site_title, embed_url, ctype, se, ep, year):
    """SWR for the card cache: re-resolve in the background, in-place."""
    try:
        _resolve_card(site_title, embed_url, ctype, se, ep, year,
                      force=True)
    except Exception:
        pass

def _resolve_card(site_title, embed_url, ctype, se, ep, year,
                  force=False, deadline=None):
    """One candidate -> one stream card (direct master, direct subs).
    v1.5.0: (a) resolved cards are cached by (post_id, se, ep) with SWR —
    an imdb request, a kitsu request and a prewarm cycle for the same
    episode share ONE resolution; (b) subtitles race the master chain
    instead of running after it; (c) deadline-aware: subs are skipped
    when the player-facing wall is about to hit."""
    # v1.6.0 FIX (regressed in v1.5.0): series embed URLs end in
    # '/embed/{post}/{se}-{ep}' — the old rsplit key was just '{se}-{ep}',
    # colliding across ALL series (a cached Solo Leveling 2x1 answered
    # Dandadan 2x1 requests!). Key on the numeric POST id instead.
    _mk = re.search(r"/embed/(\d+)", embed_url or "")
    ckey = (_mk.group(1) if _mk else (embed_url or "?").rsplit("/", 1)[-1],
            se, ep)
    if not force:
        hit, card = _cache_get(_CARD_CACHE, ckey)
        if hit:
            return card
        ent = _CARD_STALE.get(ckey)
        if ent and ent[0] > time.time() and ent[1]:
            threading.Thread(target=_card_refresh,
                             args=(site_title, embed_url, ctype, se, ep,
                                   year), daemon=True).start()
            return ent[1]
    got = _embed_iframe(embed_url)
    if not got:
        if not force:                  # cache honest misses briefly too
            _cache_put(_CARD_CACHE, ckey, None, _NEG_TTL)
        return None
    player_url, vid = got
    if deadline is None:
        deadline = time.time() + 12
    f_subs = _SUBS_EX.submit(_player_subs, player_url)
    master = _get_video(player_url, vid)
    if not master:
        if not force:
            _cache_put(_CARD_CACHE, ckey, None, _NEG_TTL)
        return None
    info = _master_info(master)
    if not info:                       # dead/unverified link -> no card
        if not force:
            _cache_put(_CARD_CACHE, ckey, None, _NEG_TTL)
        return None
    try:
        subs = f_subs.result(timeout=max(0.5, deadline - time.time()))
    except Exception:
        subs = []
    langs = [l for l in info["langs"] if _LANG_NAME.get(l, l)]
    l1 = "▣ %dp" % max(info["res"]) if info["res"] else "▣ MULTI"
    if info["res"] and len(info["res"]) > 1:
        l1 += " ▣ %d–%dp multi-quality" % (min(info["res"]), max(info["res"]))
    if langs:
        l1 += " ▣ %s audio" % "/".join(_LANG_NAME.get(l, l) for l in langs[:5])
    if ctype == "series":
        l2 = "▣ S%02dE%02d" % (se, ep)
    else:
        l2 = ("▣ %s" % year) if year else "▣ movie"
    l3 = "▣ %s ▣ multi-quality HLS ▣ zero-bandwidth addon" % BRAND
    desc = l1 + "\n" + l2 + "\n" + l3
    if subs:
        desc += "\n▣ %d subtitle track" % len(subs) + ("s" if len(subs) > 1 else "")
    card = {
        "name": "𖤍 %s" % site_title,
        "description": desc,
        # v1.2.0: the CDN master is ip-bound to the pool exit that minted
        # it — a direct card url would be a phantom for every user. We
        # serve the (rewritten) master ourselves; /stream absolutizes it.
        "url": "/hls/%s/master.m3u8" % _hls_key(master),
        "subtitles": subs,
        "behaviorHints": {"notWebReady": False, "isBingeable": True},
        "bingeGroup": "adk|%s|%s:%s:%s" % (site_title, ctype, se, ep),
    }
    if card:                           # fresh success -> cache + SWR entry
        _cache_put(_CARD_CACHE, ckey, card, _CARD_TTL)
        if len(_CARD_STALE) >= _STALE_SWEEP_AT:
            now = time.time()
            for k in [k for k, e in _CARD_STALE.items() if e[0] < now]:
                _CARD_STALE.pop(k, None)
        _CARD_STALE[ckey] = (time.time() + _CARD_STALE_TTL, card)
    return card

# --------------------------------------------------------------------------
# 6b. v1.6.0 trdekho multi-server engine (new movie posts)
# --------------------------------------------------------------------------
def _player_master(player_url):
    """player iframe url -> (master_url, subs) | (None, None).
    Dispatch: as-cdnN.top keeps the v1.2.0 getVideo POST chain; vidmoly
    embeds carry the signed master LITERALLY in the page (12h token,
    absolute variant/audio urls — open cross-IP) + srt.vidmoly.me subs;
    emturbovid carries a tokenless turboviplay master literally. The
    other trdekho hosts (abyssplayer, xerver, rubystm, upns,
    filesforever) assemble sources at runtime behind anti-debug checks —
    honest skip until cracked."""
    try:
        if _CDN_RE.match(player_url):
            vid = player_url.split("/video/")[-1]
            return _get_video(player_url, vid), []
        if "vidmoly" in player_url:
            r = _get(player_url, timeout=8, referer=SITE + "/")
            m = re.search(r'(https://[^\s"\'\\]+\.m3u8\?[^\s"\'\\]+)',
                          r.text or "")
            if not m:
                return None, None
            subs = []
            for label, su in re.findall(
                    r'playerjs\w*[Ss]ubtitle\w*\s*=\s*"\[([^\]]+)\](https?://[^"]+)"',
                    r.text or ""):
                lang = "eng" if "eng" in label.lower() else _norm(label)[:3]
                subs.append({"url": su, "lang": lang or "eng",
                             "id": "vm-" + (lang or "eng")})
            if not subs:
                su = re.search(r'(https?://srt\.vidmoly\.me/[^\s"\'\\]+\.vtt)',
                               r.text or "")
                if su:
                    subs.append({"url": su.group(1), "lang": "eng",
                                 "id": "vm-eng"})
            return m.group(1), subs
        if "emturbovid" in player_url or "turboviplay" in player_url:
            r = _get(player_url, timeout=8, referer=SITE + "/")
            m = re.search(r'(https://cdn\d+\.turboviplay\.com/[^\s"\'\\]+\.m3u8)',
                          r.text or "")
            return (m.group(1), []) if m else (None, None)
        return None, None
    except Exception:
        return None, None

_TR_FAM_TAG = (("vidmoly", "vidmoly"), ("emturbovid", "emturbo"),
               ("turboviplay", "emturbo"), ("as-cdn", "cdn"))
_TR_PRIO = {"vidmoly": 0, "emturbo": 1, "cdn": 2}

def _tr_fam_tag(u):
    for k, v in _TR_FAM_TAG:
        if k in u:
            return v
    return None

def _card_from_master(site_title, fam, master, subs, year):
    """verified master -> one stream card ('NAME · fam' style)."""
    info = _master_info(master)
    if not info:
        return None
    langs = [l for l in info["langs"] if _LANG_NAME.get(l, l)]
    l1 = "\u25a3 %dp" % max(info["res"]) if info["res"] else "\u25a3 MULTI"
    if info["res"] and len(info["res"]) > 1:
        l1 += " \u25a3 %d\u2013%dp multi-quality" % (min(info["res"]), max(info["res"]))
    if langs:
        l1 += " \u25a3 %s audio" % "/".join(_LANG_NAME.get(l, l) for l in langs[:5])
    l2 = ("\u25a3 %s" % year) if year else "\u25a3 movie"
    l3 = "\u25a3 %s \u25a3 multi-quality HLS \u25a3 zero-bandwidth addon" % BRAND
    desc = l1 + "\n" + l2 + "\n" + l3
    if subs:
        desc += "\n\u25a3 %d subtitle track" % len(subs) + ("s" if len(subs) > 1 else "")
    return {
        "name": "𖤍 %s \u00b7 %s" % (site_title, fam),
        "description": desc,
        "url": "/hls/%s/master.m3u8" % _hls_key(master),
        "subtitles": subs,
        "behaviorHints": {"notWebReady": False, "isBingeable": True},
        "bingeGroup": "adk|%s|%s" % (site_title, fam),
    }

def _tr_refresh(site_title, tr_servers, post_id, year):
    try:
        _resolve_trservers(site_title, tr_servers, post_id, year,
                           force=True)
    except Exception:
        pass

def _resolve_trservers(site_title, tr_servers, post_id, year,
                       force=False, deadline=None):
    """trdekho server pages -> up to 2 extra cards.
    All player pages fetch in parallel; resolvable players run in
    priority order (vidmoly > emturbo > as-cdn), deduped by master
    (query-stripped). Cached + SWR under ('tr'+post_id, 1, 1)."""
    ckey = ("tr" + str(post_id or "?"), 1, 1)
    if not force:
        hit, cards = _cache_get(_CARD_CACHE, ckey)
        if hit:
            return cards
        ent = _CARD_STALE.get(ckey)
        if ent and ent[0] > time.time() and ent[1]:
            threading.Thread(target=_tr_refresh,
                             args=(site_title, tr_servers, post_id,
                                   year), daemon=True).start()
            return ent[1]
    # v1.6.1: own minimum budget — the trdekho pages are site-family
    # (pool-proxied on prod, several seconds each), so a caller passing
    # a nearly-spent build wall must not starve this chain.
    if deadline is None or deadline < time.time() + 18:
        deadline = time.time() + 18

    def _iframe(u):
        try:
            # v1.6.3: 12s — with pool-proxied trdekho pages, the old 8s
            # cut the OTHER resolvable slots (emturbo) off at exactly the
            # moment the first (vidmoly) landed, caching just 1 card
            r = _get(u, timeout=12, referer=SITE + "/")
            m = re.search(r'<iframe[^>]*\ssrc="([^"]+)"', r.text or "")
            return m.group(1) if m else None
        except Exception:
            return None

    # v1.6.1: consume player pages AS THEY LAND (map's ordered iterator
    # stalled behind the slowest pool fetch) and bail out as soon as two
    # cards are built — the wall clock is dominated by the 9 pool
    # fetches, not the (direct, fast) player chains.
    futs = {_IO_EX.submit(_iframe, u): u for u in tr_servers}
    out, seen = [], set()
    timed_out = False
    try:
        for f in as_completed(futs, timeout=max(1.0, deadline - time.time())):
            if len(out) >= 2 or time.time() >= deadline:
                break
            try:
                p = f.result()
            except Exception:
                p = None
            if not p:
                continue
            fam = _tr_fam_tag(p)
            if not fam:
                continue               # uncracked player host
            master, subs = _player_master(p)
            if not master:
                continue
            noq = master.split("?", 1)[0]
            if noq in seen:
                continue
            seen.add(noq)
            card = _card_from_master(site_title, fam, master, subs or [],
                                     year)
            if card:
                out.append((fam, card))
    except FuturesTimeoutError:
        timed_out = True
    out.sort(key=lambda fc: _TR_PRIO.get(fc[0], 9))
    out = [c for _fam, c in out]
    if not force:
        # a timeout-induced empty result must NOT be negative-cached —
        # the very next tap deserves a fresh try on a warm pool
        if out or not timed_out:
            _cache_put(_CARD_CACHE, ckey, out or None,
                       _CARD_TTL if out else _NEG_TTL)
        if out:
            if len(_CARD_STALE) >= _STALE_SWEEP_AT:
                now = time.time()
                for k in [k for k, e in _CARD_STALE.items() if e[0] < now]:
                    _CARD_STALE.pop(k, None)
            _CARD_STALE[ckey] = (time.time() + _CARD_STALE_TTL, out)
    return out or None

def _movie_cards(pg, ctitle, year, deadline=None):
    """movie page-info -> list of cards (embed card + trdekho cards).
    Old posts: embed only; new posts: trdekho only; dual-pattern posts:
    both — embed first, then the multi-server extras."""
    out = []
    if pg.get("embed"):
        card = _resolve_card(pg.get("title") or ctitle, pg["embed"],
                             "movie", 1, 1, year, deadline=deadline)
        if card:
            out.append(card)
    if pg.get("tr_servers"):
        trs = _resolve_trservers(pg.get("title") or ctitle,
                                 pg["tr_servers"], pg.get("post_id"),
                                 year, deadline=deadline)
        if trs:
            out.extend(trs[:2])
    return out

_GENERIC_TOK = {"the", "movie", "film", "official", "camrip", "dub", "dubbed",
                "sub", "subbed", "hindi", "english", "japanese", "season",
                "part", "and", "no", "yaiba", "tv", "ova", "ona", "special"}

def _tokens(t):
    """meaningful title tokens (>=4 chars, non-generic). NOTE: lower+split
    directly — _norm() removes spaces too and would fuse the whole title
    into one token."""
    return {w for w in re.sub(r"[^a-z0-9\s]", " ", (t or "").lower()).split()
            if len(w) >= 4 and w not in _GENERIC_TOK}

def _match_candidates(cands, want_title, family):
    """title-matched candidates for the requested type, best first.
    v1.4.0 tier 2.5 (token-subset): the site often SHORTENS official names —
    'Demon Slayer: Kimetsu no Yaiba - The Movie: Infinity Castle' is listed
    as 'Demon Slayer Infinity Castle'. If every meaningful token of the SITE
    title appears in the requested title (>=2 site tokens so 'Naruto' can
    never match a 'Naruto Shippuden' request), it's the same work."""
    want = _norm(want_title)
    fam = [c for c in cands if c.get("family") == family]
    exact = [c for c in fam if _norm(_clean_title(c["title"])) == want]
    if exact:
        return exact[:3]
    if len(want) >= 6:
        # v1.4.0 xtream-lesson tightening: substring containment only counts
        # when the CONTAINED side carries >=2 meaningful tokens (or is a
        # long single norm) — 'Naruto' must not answer a 'Naruto Shippuden'
        # request, but 'A Silent Voice' still answers 'A Silent Voice:
        # The Movie'.
        partial = [c for c in fam
                   if (want in _norm(c["title"])
                       and (len(_tokens(want_title)) >= 2 or len(want) >= 12))
                   or (_norm(c["title"]) in want
                       and (len(_tokens(c["title"])) >= 2
                            or len(_norm(c["title"])) >= 12))]
        if partial:
            return partial[:3]
    # token-subset tier: site name = shortened/parenthetical-stripped form
    wtok = _tokens(want_title)
    if len(wtok) >= 2:
        subs = [c for c in fam
                if len(_tokens(_clean_title(c["title"]))) >= 2
                and _tokens(_clean_title(c["title"])) <= wtok]
        if subs:
            return subs[:3]
    # v1.5.0 fuzzy last resort: the site occasionally renames a little
    # ('Kaiju No 8' vs 'Kaiju No. 8 — The Third Wave'). Only when every
    # other tier failed, same family, ratio >= 0.90 on the folded norms —
    # conservative enough that a wrong franchise answer stays unlikely.
    if len(want) >= 8:
        import difflib
        best, best_r = [], 0.0
        for c in fam:
            cn = _norm(_clean_title(c["title"]))
            if abs(len(cn) - len(want)) > max(6, len(want) // 2):
                continue
            r = difflib.SequenceMatcher(None, want, cn).ratio()
            if r > best_r:
                best, best_r = [c], r
            elif r == best_r and best:
                best.append(c)
        if best_r >= 0.90:
            return best[:3]
    return []

def _build_inner(ctype, imdb, se, ep, deadline=None):
    if deadline is None:
        deadline = time.time() + _WALL
    season_index = None
    if (imdb or "").startswith("kitsu:"):
        kid = imdb.split(":", 1)[1]
        meta = _kitsu_title(kid)
        if meta:
            # v1.4.1: a season entry ('DAN DA DAN Season 2') must search
            # and match the BASE name, and its 1xN episodes must be
            # remapped to the site's real season number
            base = _season_stripped(meta[0])
            if base and base != meta[0]:
                season_index = _kitsu_season_index(kid)
                meta = (base, meta[1])
    elif (imdb or "").startswith("anilist:") or (imdb or "").startswith("mal:"):
        kid = imdb.split(":", 1)[1]
        meta = _anilist_title(kid, idmal=imdb.startswith("mal:"))
        if meta:
            base = _season_stripped(meta[0])
            if base and base != meta[0]:
                season_index = _anilist_season_index(
                    kid, idmal=imdb.startswith("mal:"))
                meta = (base, meta[1])
    else:
        meta = _cinemeta(ctype, imdb)
    if not meta:
        return {"streams": [], "message": "no metadata for this id"}
    title, year = meta
    # v1.4.1: disambiguation parens ('Ranma ½ (2024)', '(TV)') never
    # appear on the site — strip them or exact matching breaks
    title = re.sub(r"\s*\((?:19|20)\d{2}\)\s*$|\s*\(TV\)\s*$", "", title).strip()
    fam_want = ("series-hindi" if ctype == "series" else "movie-hindi")
    # v1.5.0 index-first: the newest posts answer from RAM (no site
    # search at all). The index ONLY holds ~10 fresh posts though, so a
    # title that doesn't match any of them falls through to the real
    # site search — old titles keep working exactly as before.
    cands = _latest_candidates(title, fam_want)
    matched = _match_candidates(cands, title, fam_want) if cands else []
    if not matched:
        cands = search_candidates(title)
        matched = _match_candidates(cands, title, fam_want)
    if not cands:
        return {"streams": [], "message": "not on %s (search empty)" % BRAND}
    # v1.5.0: candidates resolve CONCURRENTLY (pages + embed chains in
    # parallel) and the whole build is deadline-aware — when the wall is
    # about to hit we return whatever cards already landed instead of a
    # "slow, tap again" message.
    if ctype == "series":

        def _one_series(c):
            pg = _parse_series_page(c["url"])
            if not pg:
                return None
            ss = _season_for_ep(pg["eps"], se, ep, season_index)
            if ss is None:
                return None                 # this page simply lacks the episode
            card = _resolve_card(
                pg["title"] or c["title"],
                SITE + "/embed/%s/%d-%d" % (pg["post_id"], ss, ep),
                ctype, ss, ep, year, deadline=deadline)
            return [card] if card else None

        worker = _one_series
    else:

        def _one_movie(c):
            pg = _parse_movie_page(c["url"])
            if not pg:
                return None
            return _movie_cards(pg, c["title"], year,
                                deadline=deadline) or None

        worker = _one_movie
    cards = []
    if matched:
        futs = [_IO_EX.submit(worker, c) for c in matched[:3]]
        for f in futs:
            try:
                card = f.result(timeout=max(0.2, deadline - time.time()))
            except Exception:
                card = None
            if card:
                cards.extend(card)      # v1.6.0: workers return lists
            if time.time() >= deadline:
                for f2 in futs:
                    f2.cancel()
                break
    for i in range(1, len(cards)):          # name the extras as alternates
        cards[i]["name"] += " · alt"
    if not cards:
        if matched:
            return {"streams": [], "message":
                    "matched on %s but this episode/movie has no video yet" % BRAND}
        return {"streams": [], "message": "not on %s" % BRAND}
    return {"streams": cards}

_BUILD_EX = ThreadPoolExecutor(max_workers=2, thread_name_prefix="build")

def build_streams(ctype, imdb, se, ep):
    key = (ctype, imdb, se, ep)
    hit, val = _cache_get(_STREAM_CACHE, key)
    if hit:
        return {"streams": val}
    stale = _STREAM_STALE.get(key)
    if stale and stale[0] > time.time() and stale[1]:
        # SWR: serve the old list instantly, refresh behind the curtain
        if key not in _SWR_RUNNING:
            with _SWR_LOCK:
                if key not in _SWR_RUNNING:
                    _SWR_RUNNING.add(key)
                    threading.Thread(target=_swr_refresh,
                                     args=(ctype, imdb, se, ep, key),
                                     daemon=True).start()
        return {"streams": stale[1]}
    fut = _BUILD_EX.submit(_build_inner, ctype, imdb, se, ep,
                         time.time() + _WALL)
    try:
        res = fut.result(timeout=_WALL)
    except FuturesTimeoutError:
        return {"streams": [], "message":
                "%s is slow — tap streams again in a minute" % BRAND}
    streams = res.get("streams") or []
    if streams:
        _cache_put(_STREAM_CACHE, key, streams, _STREAM_CACHE_TTL)
        _stale_put(key, streams)
    elif "message" in res and "slow" in res["message"]:
        return res                        # transient: never cache
    return res

_SWR_RUNNING, _SWR_LOCK = set(), threading.Lock()

def _swr_refresh(ctype, imdb, se, ep, key):
    try:
        res = _build_inner(ctype, imdb, se, ep)
        if res.get("streams"):
            _cache_put(_STREAM_CACHE, key, res["streams"], _STREAM_CACHE_TTL)
            _stale_put(key, res["streams"])
    except Exception:
        pass
    finally:
        with _SWR_LOCK:
            _SWR_RUNNING.discard(key)

# --------------------------------------------------------------------------
# 7. keep-alive (anti-sleep; auto-armed from the first public Host header)
# --------------------------------------------------------------------------
_KEEPALIVE_URL = None
_KEEPALIVE_LOCK = threading.Lock()

def _note_public_base(base):
    global _KEEPALIVE_URL
    if PUBLIC_URL or not base:
        return
    host = base.split("//", 1)[-1].split(":")[0].lower()
    if (not host or host in ("localhost", "0.0.0.0") or host.startswith("127.")
            or host.startswith("10.") or host.startswith("192.168.")
            or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host)):
        return
    with _KEEPALIVE_LOCK:
        if _KEEPALIVE_URL:
            return
        _KEEPALIVE_URL = base
        threading.Thread(target=_keepalive_loop, daemon=True).start()
        print("keepalive auto-armed: %s" % base, flush=True)

def _keepalive_loop():
    while True:
        url = PUBLIC_URL or _KEEPALIVE_URL
        if url:
            try:
                requests.get(url + "/health", timeout=20)
            except Exception:
                pass
        time.sleep(240)

# --------------------------------------------------------------------------
# 7.5 prewarm (v1.5.0) — the newest episodes are hot before anyone asks
# --------------------------------------------------------------------------
# Cold-path anatomy (prod): every site-family fetch rides a free proxy
# (~1-2.5s each) — search + page + embed + subs made first-touch builds
# 5-15s. The fixes: parallel innards (above) plus THIS background cycle —
# every ~10 min it scrapes /category/hindi-dub/ (WordPress date-ordered,
# server-rendered), parses the newest series/movie posts and resolves the
# newest episodes. All the shared caches (search, page, CARD, master)
# fill from the same keys a real request uses, so when a user opens a
# fresh episode the answer is a chain of cache hits (<300ms).
_LATEST_RE = re.compile(
    r'href="(https://animedekho\.app/(series-hindi|movie-hindi)/([a-z0-9-]+))/?[^a-z0-9-]')

def _prewarm_cycle():
    try:
        r = _get(SITE + "/category/hindi-dub/", timeout=15)
        if r.status_code != 200:
            return
        posts, seen = [], set()
        for _u, fam, slug in _LATEST_RE.findall(r.text):
            if slug in seen:
                continue
            seen.add(slug)
            posts.append((fam, slug))
        _PREWARM["posts"] = len(posts)
        n_cards = 0
        epis = []
        index = []
        # newest series first (the list is date-ordered): parse every
        # post (index + page cache), resolve cards for the newest 6
        # series + 2 movies; older posts in the list stay index-only
        n_series = 0
        for fam, slug in posts:
            url = "%s/%s/%s/" % (SITE, fam, slug)
            try:
                if fam == "series-hindi":
                    pg = _parse_series_page(url)
                    if not pg or not pg["eps"]:
                        continue
                    if pg["title"]:
                        index.append({"title": pg["title"], "url": url,
                                      "family": "series-hindi"})
                    if n_series >= 6 or n_cards >= 8:
                        continue                # indexed, not card-warmed
                    n_series += 1
                    ss, ee = max(pg["eps"])   # newest episode of this post
                    # a post can hold several seasons: warm the newest ep
                    # of the newest TWO seasons (returning viewers + new)
                    targets = {(ss, ee)}
                    if ss > 1:
                        targets.add((ss - 1, max(e2 for (s2, e2) in pg["eps"]
                                                 if s2 == ss - 1)))
                    for (s2, e2) in sorted(targets, reverse=True)[:2]:
                        if _resolve_card(pg["title"] or slug,
                                         SITE + "/embed/%s/%d-%d"
                                         % (pg["post_id"], s2, e2),
                                         "series", s2, e2, ""):
                            n_cards += 1
                            epis.append("%s %dx%d" % (slug[:22], s2, e2))
                        if pg["title"]:
                            site_search(_season_stripped(pg["title"]))
                else:
                    pg = _parse_movie_page(url)
                    if not pg:
                        continue
                    if pg["title"]:
                        index.append({"title": pg["title"], "url": url,
                                      "family": "movie-hindi"})
                    if n_cards >= 8 or (n_series >= 6 and n_cards >= 4):
                        continue                # indexed, not card-warmed
                    if _movie_cards(pg, slug, ""):
                        n_cards += 1
                        epis.append(slug[:24])
            except Exception:
                continue
        if index:
            _LATEST["posts"] = index
            _LATEST["ts"] = time.time()
        _PREWARM["cycles"] += 1
        _PREWARM["cards"] += n_cards
        _PREWARM["last"] = time.time()
        _PREWARM["epis"] = epis[:8]
    except Exception:
        pass

def _prewarm_loop():
    time.sleep(90)                     # let the pool warm first
    while True:
        _prewarm_cycle()
        time.sleep(_PREWARM_EVERY + random.randint(0, 90))


# --------------------------------------------------------------------------
# 8. http server — JSON only, gzip, CORS
# --------------------------------------------------------------------------
_REQLOG = []
_T0 = time.time()

LANDING = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%s — Stremio addon</title>
<style>
body{font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0e1116;color:#e6e9ef;
display:flex;min-height:100vh;align-items:center;justify-content:center;margin:0}
.card{max-width:640px;padding:40px;background:#161b22;border:1px solid #232a33;
border-radius:16px;margin:20px}
h1{margin:0 0 6px;font-size:28px}p{color:#9aa4b2;line-height:1.6}
a.install{display:inline-block;background:#e50914;color:#fff;text-decoration:none;
font-weight:600;padding:14px 28px;border-radius:10px;margin-top:10px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:18px}
.feat{background:#0e1116;border:1px solid #232a33;border-radius:10px;padding:10px 12px;
font-size:13px;color:#c9d1d9}
b{color:#e6e9ef}
</style></head><body><div class="card">
<h1>𖤍 %s</h1>
<p>Hindi-dub anime &amp; cartoons, direct from AnimeDekho.</p>
<div class="grid">
<div class="feat"><b>🔊 Multi-audio</b> — Japanese / Hindi / English / Telugu / Tamil in one stream</div>
<div class="feat"><b>🎚 Multi-quality</b> — 240p to 1080p, your player picks</div>
<div class="feat"><b>💬 Subtitles</b> — English, straight from the CDN</div>
<div class="feat"><b>⚡ Zero-bandwidth</b> — media goes CDN&nbsp;→ your player directly</div>
</div>
<p>Install, then open any anime in Stremio — cards appear automatically.</p>
<a class="install" href="stremio://">Install in Stremio</a>
<p style="font-size:12px">v%s · stream-only addon · no catalogs · idPrefixes tt</p>
</div></body></html>""" % (BRAND, BRAND, VERSION)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = BRAND + "/" + VERSION

    def log_message(self, fmt, *args):
        print("[%s] %s" % (time.strftime("%H:%M:%S"), fmt % args), flush=True)

    def _send(self, code, body, ctype="application/json"):
        # v1.0.1 CRITICAL FIX: gzip the body FIRST, then announce
        # Content-Length. The old order sent the PRE-gzip length with a
        # shorter gzipped body — Render's edge (which forwards
        # Accept-Encoding: gzip) then waited forever for bytes that never
        # came: every response >512B (landing page, reqlog, real stream
        # cards) hung with zero bytes delivered. Responses <=512B skipped
        # gzip and worked, which is why /health & /manifest looked fine.
        self._c = code
        if isinstance(body, str):
            body = body.encode()
        gz = b""
        if len(body) > 512 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as z:
                z.write(body)
            if len(buf.getvalue()) < len(body):
                gz = buf.getvalue()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(gz or body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control",
                             "public, max-age=300" if ctype != "application/json"
                             else "no-store")
            if gz:
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.end_headers()
            self.wfile.write(gz or body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _log_req(self, path, code, ms):
        if path.startswith(("/health", "/debug")):
            return
        _REQLOG.append({"t": time.strftime("%H:%M:%S"), "path": path[:120],
                        "code": code, "ms": int(ms)})
        if len(_REQLOG) > 400:
            del _REQLOG[:200]

    def do_GET(self):
        t0 = time.time()
        try:
            self._route()
        except Exception as e:
            try:
                self._send(500, json.dumps({"error": str(e)}))
            except Exception:
                pass
        finally:
            self._log_req(getattr(self, "_p", ""), getattr(self, "_c", 0),
                          (time.time() - t0) * 1000)

    def _route(self):
        u = urlparse(self.path)
        path, q = unquote(u.path), parse_qs(u.query)
        self._p = path[:120]
        _note_public_base("https://" + (self.headers.get("Host") or ""))

        if path == "/health":
            return self._send(200, json.dumps({
                "version": VERSION, "brand": BRAND, "uptime_s": int(time.time() - _T0),
                "keepalive": bool(PUBLIC_URL or _KEEPALIVE_URL),
                "egress": {"direct": time.time() < _DIRECT_OK_UNTIL[0],
                           "pool": len(_FREE_POOL[0]),
                           "pool_bad": len(_POOL_BAD)},
                "caches": {k: len(v) for k, v in (
                    ("meta", _META_CACHE), ("search", _SEARCH_CACHE),
                    ("pages", _PAGE_CACHE), ("streams", _STREAM_CACHE),
                    ("stale", _STREAM_STALE), ("masters", _MASTER_CACHE),
                    ("cards", _CARD_CACHE))},
                "prewarm": {"cycles": _PREWARM["cycles"],
                            "cards": _PREWARM["cards"],
                            "posts": _PREWARM["posts"],
                            "last_ago_s": int(time.time() - _PREWARM["last"])
                            if _PREWARM["last"] else None,
                            "episodes": _PREWARM["epis"]},
                "reqlog_len": len(_REQLOG)}))

        if path == "/" or path == "/install":
            return self._send(200, LANDING, "text/html; charset=utf-8")

        if path == "/manifest.json":
            return self._send(200, json.dumps(MANIFEST))

        if path == "/debug/reqlog":
            k = (q.get("k") or [""])[0]
            if k != "adk-dbg-9c2f":
                return self._send(404, json.dumps({"error": "not found"}))
            return self._send(200, json.dumps({"version": VERSION,
                                               "entries": _REQLOG[-120:]}))

        if path == "/debug/master":
            # fetch a master (via pool if needed) + its first variant; return
            # the segment URL pattern so we can test cross-IP segment access
            k = (q.get("k") or [""])[0]
            u = (q.get("u") or [""])[0]
            if k != "adk-dbg-9c2f" or not u.startswith("http"):
                return self._send(404, json.dumps({"error": "not found"}))
            try:
                r = _get(u, timeout=14)
                if r.status_code != 200:
                    return self._send(200, json.dumps({"master_status": r.status_code}))
                import urllib.parse as _up
                lines = r.text.splitlines()
                var = next((l for l in lines if l and not l.startswith("#")), "")
                vurl = _up.urljoin(u, var)
                r2 = _get(vurl, timeout=14)
                segs = [l for l in r2.text.splitlines()
                        if l and not l.startswith("#")][:2]
                return self._send(200, json.dumps({
                    "master": r.status_code, "variant": var,
                    "variant_status": r2.status_code,
                    "segs": [_up.urljoin(vurl, s) for s in segs]}))
            except Exception as e:
                return self._send(200, json.dumps(
                    {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}))

        if path == "/debug/tr":
            # v1.6.4: trdekho grid forensics from THIS host's egress —
            # per-slot fetch status/bytes/latency, iframe src, family tag
            # and master-resolution outcome
            k = (q.get("k") or [""])[0]
            trid = (q.get("trid") or [""])[0]
            trtype = (q.get("trtype") or ["1"])[0]
            if k != "adk-dbg-9c2f" or not trid.isdigit():
                return self._send(404, json.dumps({"error": "not found"}))
            slots = []
            for n in range(9):
                u = "%s/?trdekho=%d&trid=%s&trtype=%s" % (SITE, n, trid, trtype)
                t0 = time.time()
                try:
                    r = _get(u, timeout=12, referer=SITE + "/")
                    dt = round(time.time() - t0, 2)
                    m = re.search(r'<iframe[^>]*\ssrc="([^"]+)"', r.text or "")
                    purl = m.group(1) if m else None
                except Exception as e:
                    r, dt, purl = None, round(time.time() - t0, 2), None
                ent = {"n": n, "st": getattr(r, "status_code", None),
                       "bytes": len(getattr(r, "text", "") or ""),
                       "s": dt, "iframe": (purl or "")[:60],
                       "fam": (_tr_fam_tag(purl) if purl else None)}
                if purl and ent["fam"]:
                    master, subs = _player_master(purl)
                    ent["master"] = (master or "NONE")[:70]
                    ent["subs"] = len(subs or [])
                slots.append(ent)
            return self._send(200, json.dumps({"trid": trid, "slots": slots}))

        if path == "/debug/search":
            # ground truth for site egress from THIS host (is the site
            # blocking datacenter IPs? status/bytes/cards tell us)
            k = (q.get("k") or [""])[0]
            kw = (q.get("q") or [""])[0]
            if k != "adk-dbg-9c2f" or not kw:
                return self._send(404, json.dumps({"error": "not found"}))
            try:
                r = _get(SITE + "/?s=" + quote(kw), timeout=12)
                cards = _extract_cards(r.text) if r.status_code == 200 else []
                return self._send(200, json.dumps({
                    "status": r.status_code, "bytes": len(r.text),
                    "cards": len(cards),
                    "first": [{"t": c["title"][:60], "url": c["url"][:70]}
                              for c in cards[:3]]}))
            except Exception as e:
                return self._send(200, json.dumps(
                    {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}))

        m = re.match(r"^/stream/(movie|series)/"
                    r"((?:tt\d+|kitsu:\d+|anilist:\d+|mal:\d+))"
                    r"(?::(\d+):(\d+))?\.json$", path)
        if m:
            ctype, imdb = m.group(1), m.group(2)
            if ctype not in ("movie", "series"):
                return self._send(400, json.dumps({"error": "bad type"}))
            se, ep = int(m.group(3) or 1), int(m.group(4) or 1)
            if not (imdb.startswith("tt") or imdb.startswith("kitsu:")
                    or imdb.startswith("anilist:")
                    or imdb.startswith("mal:")):
                return self._send(200, json.dumps({"streams": []}))
            res = build_streams(ctype, imdb, se, ep)
            # v1.2.0: card urls are relative /hls/… routes — absolutize
            # against the request host so players get a full https url
            host = (self.headers.get("Host") or "").strip()
            if host:
                for c in (res.get("streams") or []):
                    if (c.get("url") or "").startswith("/hls/"):
                        c["url"] = "https://" + host + c["url"]
            return self._send(200, json.dumps(res))

        m = re.match(r"^/hls/([a-f0-9]{16})/((?:master|v\d+|a\d+)\.m3u8)$", path)
        if m:
            # v1.2.0 served-playlist routes: TEXT ONLY (master ~1-2KB,
            # variant ~100KB gzipped to ~10KB); segments stay absolute
            # open-CDN urls the player fetches directly — zero media bytes.
            key, name = m.group(1), m.group(2)   # name INCLUDES .m3u8
            if name == "master.m3u8":
                name = "master"
            murl = _HLS_KEYS.get(key)
            hit, val = _cache_get(_MASTER_CACHE, murl or "")
            if not (hit and val):
                return self._send(404, json.dumps({"error": "expired"}))
            if name == "master":
                return self._send(200, val["master_text"],
                                  "application/vnd.apple.mpegurl")
            orig = (val["variants"] or {}).get(name)
            if not orig:
                return self._send(404, json.dumps({"error": "no such variant"}))
            text = _variant_text(orig)
            if not text:
                return self._send(404, json.dumps({"error": "variant unavailable"}))
            return self._send(200, text, "application/vnd.apple.mpegurl")

        # strict zero: nothing else is served from here — ever
        return self._send(404, json.dumps({"error": "not found"}))


def main():
    threading.Thread(target=_keepalive_loop, daemon=True).start()
    # v1.1.0: warm the egress pool at boot — prod egress is site-blocked
    # (403), so the first user request would otherwise pay the full
    # list-fetch + probe cost (~10-30s)
    threading.Thread(target=_pool_refresh, kwargs={"force": True},
                     daemon=True).start()
    threading.Thread(target=_prewarm_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("%s %s listening on :%d (strict zero-bandwidth)" % (BRAND, VERSION, PORT),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
