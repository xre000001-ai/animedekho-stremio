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

import gzip
import hashlib
import io
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, unquote, urljoin

import requests

# --------------------------------------------------------------------------
# 1. config
# --------------------------------------------------------------------------
VERSION = "1.2.1"
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
_WALL         = 20.0              # player-facing wall for one /stream build

_LANG_NAME = {"jpn": "Japanese", "hin": "Hindi", "eng": "English",
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
    "idPrefixes": ["tt"],
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
_STREAM_CACHE = {}   # (ctype, imdb, se, ep) -> (cards, expiry)
_STREAM_STALE = {}   # key -> (expiry, cards)   [SWR]
_STALE_SWEEP_AT = 256

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
    """Is this free exit usable for the site? (200 + real site content)"""
    try:
        r = _S.get(SITE + "/", timeout=timeout, headers={"User-Agent": UA},
                   proxies={"http": url, "https": url})
        return (r.status_code == 200
                and "animedekho" in r.text[:20000].lower())
    except Exception:
        return False

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
        cand = cand[:100]
        good = []
        with ThreadPoolExecutor(max_workers=20) as ex:
            for u, ok in zip(cand, ex.map(_site_ok_via, cand)):
                if ok:
                    good.append(u)
        with _POOL_LOCK:
            prev = [u for u in _FREE_POOL[0]
                    if _POOL_BAD.get(u, 0.0) <= time.time()]
            merged = list(dict.fromkeys(prev + good))[:20]
            _FREE_POOL[0] = merged or prev
    except Exception:
        pass

def _pick_exit(now):
    if _POOL_STICKY[0] and now < _POOL_STICKY[1]:
        u = _POOL_STICKY[0]
        if _POOL_BAD.get(u, 0.0) <= now:
            return u
    with _POOL_LOCK:
        live = [u for u in _FREE_POOL[0] if _POOL_BAD.get(u, 0.0) <= now]
    if not live:
        return None
    return random.choice(live[:8])

class _DeadResponse:
    status_code = 0
    text = ""
    def json(self):
        raise ValueError("dead response")

_CDN_RE = re.compile(r"https://as-cdn\d+\.top/")

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
    fam_cdn = bool(_CDN_RE.match(url))
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
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())

def _clean_title(t):
    """Strip trailing dub/sub qualifiers: 'Naruto - Hindi Dub' -> 'Naruto'."""
    t = (t or "").strip()
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
        out.append({"title": t.strip(), "url": m.group(1), "family": m.group(2),
                    "year": y.group(1) if y else ""})
    return out

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
    full title first, then progressively shorter prefixes (results merged;
    every step is cached)."""
    seen, out = set(), []
    words = re.sub(r"[^\w\s]", " ", title).split()
    queries = [title]
    if len(words) >= 3:
        queries.append(" ".join(words[:3]))
    if len(words) >= 2:
        queries.append(" ".join(words[:2]))
    if words and len(words[0]) >= 4:
        queries.append(words[0])
    for q in queries:
        for c in site_search(q):
            k = c["url"]
            if k not in seen:
                seen.add(k)
                out.append(c)
        if out and q == title:        # full title already answered
            break
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
                   "title": (t.group(1).strip() if t else "")}
        _cache_put(_PAGE_CACHE, url, val, _PAGE_TTL if val else _NEG_TTL)
        return val
    except Exception:
        return None

def _parse_movie_page(url):
    """movie-hindi page -> {post_id, embed} (6h cached)."""
    hit, val = _cache_get(_PAGE_CACHE, url)
    if hit:
        return val
    try:
        r = _get(url, timeout=10)
        if r.status_code != 200:
            return None
        h = r.text
        m = re.search(r'animedekho\.app/embed/(\d+)', h)
        t = re.search(r'<h1 class="entry-title">([^<]*)</h1>', h)
        if not m:
            val = None
        else:
            val = {"post_id": m.group(1),
                   "embed": SITE + "/embed/" + m.group(1),
                   "title": (t.group(1).strip() if t else "")}
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
            langs = sorted(set(re.findall(r'LANGUAGE="([a-z]{3})"', r.text)))
            res = sorted(set(int(x.split("x")[1])
                             for x in re.findall(r"RESOLUTION=(\d+x\d+)", r.text)))
            mtext, variants = _rewrite_master(r.text, master_url)
            val = {"info": {"langs": langs, "res": res, "audio_rends":
                            re.findall(r'TYPE=AUDIO[^>]*LANGUAGE="([a-z]{3})"[^>]*NAME="([^"]*)"',
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
def _resolve_card(site_title, embed_url, ctype, se, ep, year):
    """One candidate -> one stream card (direct master, direct subs)."""
    got = _embed_iframe(embed_url)
    if not got:
        return None
    player_url, vid = got
    master = _get_video(player_url, vid)
    if not master:
        return None
    info = _master_info(master)
    if not info:                       # dead/unverified link -> no card
        return None
    subs = _player_subs(player_url)
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
    return {
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

def _match_candidates(cands, want_title, family):
    """title-matched candidates for the requested type, best first."""
    want = _norm(want_title)
    fam = [c for c in cands if c.get("family") == family]
    exact = [c for c in fam if _norm(_clean_title(c["title"])) == want]
    if exact:
        return exact[:3]
    # partial only for substantial queries — 'It' must not live inside everything
    if len(want) < 6:
        return []
    partial = [c for c in fam
               if want in _norm(c["title"]) or _norm(c["title"]) in want]
    return partial[:3]

def _build_inner(ctype, imdb, se, ep):
    meta = _cinemeta(ctype, imdb)
    if not meta:
        return {"streams": [], "message": "no metadata for this id"}
    title, year = meta
    cands = search_candidates(title)
    if not cands:
        return {"streams": [], "message": "not on %s (search empty)" % BRAND}
    if ctype == "series":
        matched = _match_candidates(cands, title, "series-hindi")
        cards = []
        for c in matched:
            pg = _parse_series_page(c["url"])
            if not pg:
                continue
            if (se, ep) not in pg["eps"]:
                continue                     # this page simply lacks the episode
            name = pg["title"] or c["title"]
            if len(matched) > 1 and cards:
                name += " · alt"
            card = _resolve_card(name,
                                 SITE + "/embed/%s/%d-%d" % (pg["post_id"], se, ep),
                                 ctype, se, ep, year)
            if card:
                cards.append(card)
    else:
        matched = _match_candidates(cands, title, "movie-hindi")
        cards = []
        for c in matched:
            pg = _parse_movie_page(c["url"])
            if not pg:
                continue
            name = pg["title"] or c["title"]
            if len(matched) > 1 and cards:
                name += " · alt"
            card = _resolve_card(name, pg["embed"], ctype, 1, 1, year)
            if card:
                cards.append(card)
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
    fut = _BUILD_EX.submit(_build_inner, ctype, imdb, se, ep)
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
                    ("stale", _STREAM_STALE), ("masters", _MASTER_CACHE))},
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

        m = re.match(r"^/stream/(movie|series)/(tt\d+)(?::(\d+):(\d+))?\.json$", path)
        if m:
            ctype, imdb = m.group(1), m.group(2)
            if ctype not in ("movie", "series"):
                return self._send(400, json.dumps({"error": "bad type"}))
            se, ep = int(m.group(3) or 1), int(m.group(4) or 1)
            if not imdb.startswith("tt"):
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
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("%s %s listening on :%d (strict zero-bandwidth)" % (BRAND, VERSION, PORT),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
