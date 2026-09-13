#!/usr/bin/env python3
"""Unit tests for the AnimeDekho addon. Run: python3 -m pytest test_animedekho.py -q
Everything network-touching is mocked — hermetic and deterministic."""
import json
import re
import sys
import os
import time
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import addon

PASS = 0


def run(fns):
    global PASS
    for fn in fns:
        fn()
        PASS += 1
        print("ok %d - %s" % (PASS, fn.__name__), flush=True)


# ---------------------------------------------------------------- fixtures

SEARCH_HTML = """
<article><figure> <img post-id="1" fifu="1" src="x.jpg" title="Jujutsu Kaisen" /> </figure>
<span class="season-episode">S3-EP12</span></div>
<a href="https://animedekho.app/series-hindi/jujutsu-kaisen-hindi/" class="lnk-blk"></a>
<article><figure> <img post-id="2" fifu="1" src="y.jpg" title="Jujutsu Kaisen 0" /> </figure>
<a href="https://animedekho.app/movie-hindi/jujutsu-kaisen-0/" class="lnk-blk"></a>
<article><figure> <img post-id="3" fifu="1" src="z.jpg" title="Naruto" /> </figure>
<a href="https://animedekho.app/series-hindi/naruto/" class="lnk-blk"></a>
"""

# the theme's SECOND card layout (series results): h2 entry-title + img alt=
SEARCH_HTML_B = """
<li> <article class="post dfx fcl movies"> <header class="entry-header">
<h2 class="entry-title">Demon Slayer: Kimetsu no Yaiba</h2>
<div class="entry-meta"> <span class="year">2019</span> </div> </header>
<div class="post-thumbnail or-1"> <figure>
<img src="https://image.tmdb.org/t/p/w500/ctR9Kv2MNhuIjgb96wARbT1BNts.jpg" loading="lazy" alt="Demon Slayer: Kimetsu no Yaiba" /> </figure>
<span class="play fa-play"></span> <span class="season-episode">S5-EP1</span></div>
<a href="https://animedekho.app/series-hindi/demon-slayer-kimetsu-no-yaiba/" class="lnk-blk"></a>
</article> </li>
"""

SERIES_HTML = """
<html><body>
<h1 class="entry-title">Jujutsu Kaisen</h1>
<a href="https://animedekho.app/series-hindi/batch/get-links.php?id=95479">Batch</a>
<a href="https://animedekho.app/epi/jujutsu-kaisen-1x1/">1</a>
<a href="https://animedekho.app/epi/jujutsu-kaisen-1x24/">24</a>
<a href="https://animedekho.app/epi/jujutsu-kaisen-2x3/">s2e3</a>
<a href="https://animedekho.app/epi/jujutsu-kaisen-3x12/">s3</a>
</body></html>
"""

MOVIE_HTML = """
<html><body><h1 class="entry-title">Jujutsu Kaisen 0</h1>
<iframe class="serversel" src="https://animedekho.app/embed/810693"></iframe>
</body></html>
"""

EMBED_HTML = ('<iframe loading="lazy" src="https://as-cdn26.top/video/'
              '0a09c8844ba8f0936c20bd791130d6b6" allowfullscreen></iframe>')

PLAYER_HTML = ('<script>var playerjsSubtitle = "[English]'
               'https://as-cdn26.top/p/abc.jpg"; var playerjsDefaultSubtitle = '
               '"English";</script>')

GETVIDEO_JSON = json.dumps({
    "hls": True,
    "videoSource": "https://as-cdn26.top/cdn/hls/abc/master.m3u8?md5=x&expires=99",
    "securedLink": "https://as-cdn26.top/cdn/hls/abc/master.m3u8?md5=x&expires=99"})

MASTER = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",LANGUAGE="jpn",NAME="Japanese",DEFAULT=NO,AUTOSELECT=YES,URI="/hls/aaa"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",LANGUAGE="eng",NAME="English",DEFAULT=NO,URI="/hls/bbb"
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",LANGUAGE="hin",NAME="Hindi",DEFAULT=YES,URI="/hls/ccc"
#EXT-X-STREAM-INF:BANDWIDTH=750000,RESOLUTION=842x480,AUDIO="audio"
/hls/v480
#EXT-X-STREAM-INF:BANDWIDTH=4096000,RESOLUTION=1920x1080,AUDIO="audio"
/hls/v1080
"""

CINEMETA_JJK = {"meta": {"name": "Jujutsu Kaisen", "year": "2020"}}


class R:
    def __init__(self, text="", status=200, js=None):
        self.text, self.status_code = text, status
        self._js = js
    def json(self):
        if self._js is None:
            raise ValueError("no json")
        return self._js


def _reset():
    for s in (addon._META_CACHE, addon._SEARCH_CACHE, addon._PAGE_CACHE,
              addon._MASTER_CACHE, addon._STREAM_CACHE, addon._STREAM_STALE,
              addon._REQLOG, addon._CARD_CACHE, addon._CARD_STALE):
        s.clear()
    addon._SWR_RUNNING.clear()


# ---------------------------------------------------------------- tests

def test_norm_and_clean_title():
    assert addon._norm("Jujutsu Kaisen!") == "jujutsukaisen"
    assert addon._clean_title("Naruto - Hindi Dub") == "Naruto"
    assert addon._clean_title("Your Name | English Sub") == "Your Name"

def test_site_search_parses_cards():
    _reset()
    with mock.patch.object(addon, "_get", return_value=R(SEARCH_HTML)):
        out = addon.site_search("jujutsu")
    assert len(out) == 3
    assert out[0]["title"] == "Jujutsu Kaisen"
    assert out[0]["family"] == "series-hindi"
    assert out[1]["family"] == "movie-hindi"
    assert out[2]["url"].endswith("/series-hindi/naruto/")

def test_site_search_layout_b():
    """second card layout: h2 entry-title + img alt= (series results)."""
    _reset()
    with mock.patch.object(addon, "_get", return_value=R(SEARCH_HTML_B)):
        out = addon.site_search("demon slayer")
    assert len(out) == 1
    assert out[0]["title"] == "Demon Slayer: Kimetsu no Yaiba"
    assert out[0]["family"] == "series-hindi"
    assert out[0]["year"] == "2019"

def test_search_candidates_progressive():
    """full title misses (0 cards) -> shorter prefix retries."""
    _reset()
    calls = []
    def fake_search(q):
        calls.append(q)
        return (addon._extract_cards(SEARCH_HTML_B)
                if q == "Demon Slayer" else [])
    with mock.patch.object(addon, "site_search", side_effect=fake_search):
        out = addon.search_candidates("Demon Slayer: Kimetsu no Yaiba")
    assert out and out[0]["title"].startswith("Demon Slayer")
    assert "Demon Slayer" in calls and "Demon Slayer: Kimetsu no Yaiba" in calls

def test_site_search_filters_family():
    _reset()
    with mock.patch.object(addon, "_get", return_value=R(SEARCH_HTML)):
        cands = addon.site_search("jujutsu")
    m = addon._match_candidates(cands, "Jujutsu Kaisen", "series-hindi")
    assert len(m) == 1 and m[0]["family"] == "series-hindi"
    m2 = addon._match_candidates(cands, "Jujutsu Kaisen", "movie-hindi")
    assert len(m2) == 1 and m2[0]["family"] == "movie-hindi"

def test_match_exact_beats_partial():
    _reset()
    cands = [{"title": "Jujutsu Kaisen", "url": "a", "family": "series-hindi"},
             {"title": "Jujutsu Kaisen English Dubbed Extra", "url": "b", "family": "series-hindi"}]
    m = addon._match_candidates(cands, "Jujutsu Kaisen", "series-hindi")
    assert len(m) == 1 and m[0]["title"] == "Jujutsu Kaisen"

def test_match_partial_requires_len():
    # a 2-char containment must not match ("it" inside everything)
    cands = [{"title": "It", "url": "a", "family": "series-hindi"},
             {"title": "Attack on Titan", "url": "b", "family": "series-hindi"}]
    assert addon._match_candidates(cands, "Titan", "series-hindi") == []

def test_parse_series_page():
    _reset()
    with mock.patch.object(addon, "_get", return_value=R(SERIES_HTML)):
        pg = addon._parse_series_page("https://animedekho.app/series-hindi/x/")
    assert pg["post_id"] == "95479"
    assert pg["title"] == "Jujutsu Kaisen"
    assert (1, 1) in pg["eps"] and (2, 3) in pg["eps"] and (3, 12) in pg["eps"]
    assert (9, 9) not in pg["eps"]

def test_parse_series_page_no_eps_is_none():
    _reset()
    with mock.patch.object(addon, "_get", return_value=R("<h1 class=\"entry-title\">X</h1>")):
        assert addon._parse_series_page("u1") is None

def test_parse_movie_page():
    _reset()
    with mock.patch.object(addon, "_get", return_value=R(MOVIE_HTML)):
        pg = addon._parse_movie_page("https://animedekho.app/movie-hindi/x/")
    assert pg["post_id"] == "810693"
    assert pg["embed"] == "https://animedekho.app/embed/810693"

def test_embed_iframe():
    with mock.patch.object(addon, "_get", return_value=R(EMBED_HTML)):
        url, vid = addon._embed_iframe("https://animedekho.app/embed/95479/1-1")
    assert url == "https://as-cdn26.top/video/0a09c8844ba8f0936c20bd791130d6b6"
    assert vid == "0a09c8844ba8f0936c20bd791130d6b6"

def test_embed_iframe_missing():
    with mock.patch.object(addon, "_get", return_value=R("<html>empty</html>")):
        assert addon._embed_iframe("x") is None

def test_player_subs():
    with mock.patch.object(addon, "_get", return_value=R(PLAYER_HTML)), \
         mock.patch.object(addon, "_sub_alive", return_value=True):
        subs = addon._player_subs("https://as-cdn26.top/video/x")
    assert len(subs) == 1
    assert subs[0]["lang"] == "en" and subs[0]["url"].endswith(".jpg")  # v1.8.1

def test_player_subs_none():
    with mock.patch.object(addon, "_get", return_value=R("<script>var x=1;</script>")):
        assert addon._player_subs("x") == []

def test_get_video_ok():
    with mock.patch.object(addon._S, "post", return_value=R(js=json.loads(GETVIDEO_JSON))):
        u = addon._get_video("https://as-cdn26.top/video/abc", "abc")
    assert u.startswith("https://as-cdn26.top/cdn/hls/") and "master.m3u8" in u

def test_get_video_bad_json():
    with mock.patch.object(addon._S, "post", return_value=R("<!DOCTYPE html>error")):
        assert addon._get_video("https://as-cdn26.top/video/abc", "abc") is None

def test_master_info():
    _reset()
    with mock.patch.object(addon, "_get", return_value=R(MASTER)):
        info = addon._master_info("https://as-cdn26.top/cdn/hls/abc/master.m3u8?x=1")
    assert info["langs"] == ["eng", "hin", "jpn"]
    assert info["res"] == [480, 1080]

def test_master_info_dead_link():
    _reset()
    with mock.patch.object(addon, "_get", return_value=R("<html>404</html>", status=404)):
        assert addon._master_info("https://x/master.m3u8") is None
    with mock.patch.object(addon, "_get", return_value=R("", status=200)):
        assert addon._master_info("https://y/master.m3u8") is None

def test_resolve_card_full():
    _reset()
    g = {"embed": 0}
    def fake_get(url, **kw):
        g["embed"] += 1
        if "/embed/" in url:
            return R(EMBED_HTML)
        if "/video/" in url:
            return R(PLAYER_HTML)
        return R(MASTER)
    with mock.patch.object(addon, "_get", side_effect=fake_get), \
         mock.patch.object(addon, "_sub_alive", return_value=True), \
         mock.patch.object(addon._S, "post", return_value=R(js=json.loads(GETVIDEO_JSON))):
        card = addon._resolve_card("Jujutsu Kaisen",
                                   "https://animedekho.app/embed/95479/2-3",
                                   "series", 2, 3, "2020")
    # v1.2.0: card points at OUR served route (cdn master is ip-bound)
    assert card and re.match(r"^/hls/[a-f0-9]{16}/master\.m3u8$", card["url"])
    # the served-route key maps back to the real (ip-bound) master url
    vs = json.loads(GETVIDEO_JSON)["videoSource"]
    key = card["url"].split("/")[2]
    assert addon._hls_key(vs) == key and addon._HLS_KEYS.get(key) == vs
    assert "1080p" in card["name"]                      # v1.7.1 ♧ highest-only
    assert "Hindi" in card["description"]               # v1.7.1 ◈ glass lang line
    assert card["subtitles"] and card["subtitles"][0]["lang"] == "en"  # v1.8.1
    assert card["behaviorHints"]["isBingeable"]
    # direct card: no proxyHeaders needed at all
    assert "proxyHeaders" not in (card.get("behaviorHints") or {})

def test_resolve_card_dead_master_no_card():
    _reset()
    def fake_get(url, **kw):
        if "/embed/" in url:
            return R(EMBED_HTML)
        return R("<html>gone</html>", status=404)
    with mock.patch.object(addon, "_get", side_effect=fake_get), \
         mock.patch.object(addon._S, "post", return_value=R(js=json.loads(GETVIDEO_JSON))):
        assert addon._resolve_card("X", "e", "series", 1, 1, "") is None

def test_build_series_happy_path():
    _reset()
    def fake_cinemeta(ctype, imdb):
        return ("Jujutsu Kaisen", "2020")
    def fake_get(url, **kw):
        if "s=jujutsu" in url or "/?s=" in url:
            return R(SEARCH_HTML)
        if "series-hindi/jujutsu" in url:
            return R(SERIES_HTML)
        if "/embed/" in url:
            return R(EMBED_HTML)
        if "/video/" in url:
            return R(PLAYER_HTML)
        return R(MASTER)
    with mock.patch.object(addon, "_cinemeta", side_effect=fake_cinemeta), \
         mock.patch.object(addon, "_get", side_effect=fake_get), \
         mock.patch.object(addon._S, "post", return_value=R(js=json.loads(GETVIDEO_JSON))):
        res = addon.build_streams("series", "tt1234", 2, 3)
    assert len(res["streams"]) == 1
    c = res["streams"][0]
    assert c["name"].endswith("Jujutsu Kaisen")   # v1.7.0 ♧/✹ format
    assert "S02 E03" in c["description"]   # v1.7.0 ◫ Sxx Exx format
    # cached now
    res2 = addon.build_streams("series", "tt1234", 2, 3)
    assert res2["streams"] == res["streams"]

def test_build_series_episode_missing_honest():
    _reset()
    with mock.patch.object(addon, "_cinemeta",
                           return_value=("Jujutsu Kaisen", "2020")), \
         mock.patch.object(addon, "site_search",
                           return_value=addon._match_candidates(
                               [{"title": "Jujutsu Kaisen", "url": "u",
                                 "family": "series-hindi"}], "Jujutsu Kaisen",
                               "series-hindi")), \
         mock.patch.object(addon, "_parse_series_page",
                           return_value={"post_id": "95479",
                                         "eps": {(1, 1), (1, 24)},
                                         "title": "Jujutsu Kaisen"}):
        res = addon.build_streams("series", "tt1234", 9, 9)
    assert res["streams"] == []
    assert "no video yet" in res["message"]

def test_build_no_metadata():
    _reset()
    with mock.patch.object(addon, "_cinemeta", return_value=None):
        res = addon.build_streams("series", "tt0", 1, 1)
    assert res["streams"] == [] and "metadata" in res["message"]

def test_build_not_on_site():
    _reset()
    with mock.patch.object(addon, "_cinemeta", return_value=("Random Show", "2020")), \
         mock.patch.object(addon, "site_search", return_value=[]):
        res = addon.build_streams("series", "tt1", 1, 1)
    assert res["streams"] == [] and "not on" in res["message"]

def test_build_movie_multiple_matches_alt_suffix():
    _reset()
    cands = [{"title": "Your Name", "url": "https://animedekho.app/movie-hindi/your-name-1/", "family": "movie-hindi"},
             {"title": "Your Name", "url": "https://animedekho.app/movie-hindi/your-name-2/", "family": "movie-hindi"}]
    def fake_get(url, **kw):
        if "/?s=" in url:
            return R(SEARCH_HTML)
        if "movie-hindi/" in url:
            return R(MOVIE_HTML)
        if "/embed/" in url:
            return R(EMBED_HTML)
        if "/video/" in url:
            return R(PLAYER_HTML)
        return R(MASTER)
    with mock.patch.object(addon, "_cinemeta", return_value=("Your Name", "2016")), \
         mock.patch.object(addon, "site_search", return_value=cands), \
         mock.patch.object(addon, "_get", side_effect=fake_get), \
         mock.patch.object(addon._S, "post",
                           return_value=R(js=json.loads(GETVIDEO_JSON))):
        res = addon.build_streams("movie", "tt5311514", 1, 1)
    assert len(res["streams"]) == 2
    names = [c["name"] for c in res["streams"]]
    assert names[0].endswith("Jujutsu Kaisen 0")    # v1.7.0 ♧/✹ format
    assert names[1].endswith("· alt")

def test_cache_put_sweeps_expired():
    store = {}
    with mock.patch.object(addon, "_CACHE_SWEEP_AT", 3):
        addon._cache_put(store, "a", 1, -10)
        addon._cache_put(store, "b", 2, -10)
        addon._cache_put(store, "c", 3, 600)
        assert len(store) == 3
        addon._cache_put(store, "d", 4, 600)
    assert set(store) == {"c", "d"}

def test_stale_put_sweeps_expired():
    addon._STREAM_STALE.clear()
    now = time.time()
    with mock.patch.object(addon, "_STALE_SWEEP_AT", 2):
        addon._STREAM_STALE["old"] = (now - 5, [{"x": 1}])
        addon._STREAM_STALE["live"] = (now + 3600, [{"x": 2}])
        addon._stale_put("new", [{"x": 3}])
    assert "old" not in addon._STREAM_STALE
    assert {"live", "new"} <= set(addon._STREAM_STALE)
    addon._STREAM_STALE.clear()

def test_swr_serves_stale_and_refreshes():
    _reset()
    key = ("series", "tt77", 1, 1)
    addon._STREAM_CACHE.pop(key, None)
    addon._STREAM_STALE[key] = (time.time() + 600, [{"name": "old"}])
    def fake_inner(*a):
        return {"streams": [{"name": "new"}]}
    with mock.patch.object(addon, "_build_inner", side_effect=fake_inner):
        res = addon.build_streams("series", "tt77", 1, 1)
        assert res["streams"] == [{"name": "old"}]     # instant stale serve
        for _ in range(50):
            if not addon._SWR_RUNNING:
                break
            time.sleep(0.02)
        time.sleep(0.05)
    assert addon._STREAM_CACHE[key][0] == [{"name": "new"}]

def test_manifest_shape():
    m = json.loads(json.dumps(addon.MANIFEST))
    assert m["id"] == "com.animedekho.stremio"
    assert m["resources"] == ["stream", "subtitles"]     # v1.9.0 Nuvio
    assert set(m["types"]) == {"movie", "series"}
    assert m["idPrefixes"] == ["tt", "kitsu", "anilist", "mal"]  # v1.4.0: +anilist/mal

def test_no_media_routes_in_source():
    src = open("addon.py").read()
    for gone in ("/hls", "/dash", "/sub/", "/seg", "video/mp4", "mpegurl"):
        assert 'path == "%s' % gone not in src
    # only JSON routes exist
    for route in ('"/health"', '"/manifest.json"', '"/"', '"/debug/reqlog"'):
        assert route in src

def test_stream_route_regex():
    rx = __import__("re").compile(
        r"^/stream/(movie|series)/(tt\d+)(?::(\d+):(\d+))?\.json$")
    assert rx.match("/stream/series/tt123:2:3.json").groups() == ("series", "tt123", "2", "3")
    assert rx.match("/stream/movie/tt456.json").groups() == ("movie", "tt456", None, None)
    assert not rx.match("/stream/series/tt123-2-3.json")




# --- v1.0.1: the Render-edge hang (Content-Length sent pre-gzip) ---------------

def _srv_sock_request(port, path, accept_gzip):
    """Raw-socket HTTP/1.1 GET: returns (code, headers, body-bytes-read).
    Fails the test on timeout — a wrong Content-Length would hang here,
    exactly like Render's edge in production."""
    import socket
    s = socket.create_connection(("127.0.0.1", port), timeout=6)
    s.settimeout(6)
    req = "GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nConnection: close\r\n" % (path, port)
    if accept_gzip:
        req += "Accept-Encoding: gzip\r\n"
    s.sendall((req + "\r\n").encode())
    buf = b""
    try:
        while True:
            d = s.recv(65536)
            if not d:
                break
            buf += d
    except socket.timeout:
        s.close()
        raise AssertionError("response hung (Content-Length/body mismatch?) for %s gz=%s"
                             % (path, accept_gzip))
    s.close()
    head, _, body = buf.partition(b"\r\n\r\n")
    code = int(head.split(b" ")[1])
    hdrs = dict((l.split(b":", 1)[0].strip().lower(),
                 l.split(b":", 1)[1].strip()) for l in head.split(b"\r\n")[1:])
    return code, hdrs, body

def test_v101_gzip_content_length_consistency():
    """The bug: Content-Length announced the PRE-gzip size, so every
    gzipped response (Render's edge always asks for gzip) delivered fewer
    bytes than promised and hung. CL must equal the actual body bytes."""
    import threading, subprocess, sys, time, gzip as gzmod, os
    port = 7831
    env = dict(os.environ, PORT=str(port))
    p = subprocess.Popen([sys.executable, "addon.py"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            time.sleep(0.25)
            try:
                import socket
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                pass
        # gzip ON: landing page is >512B -> gzipped; CL must match wire bytes
        code, hdrs, body = _srv_sock_request(port, "/", accept_gzip=True)
        assert code == 200
        assert hdrs[b"content-encoding"] == b"gzip"
        assert int(hdrs[b"content-length"]) == len(body), (
            "CL %s != body %d (the v1.0.0 bug)" % (hdrs[b"content-length"], len(body)))
        assert gzmod.decompress(body).decode().startswith("<!doctype html>")
        # gzip ON for json too (the manifest grew past the 512B threshold
        # in v1.9.x): the v1.0.1 invariant is CL == wire bytes + a valid
        # body, gzipped or not
        code, hdrs, body = _srv_sock_request(port, "/manifest.json", accept_gzip=True)
        assert code == 200
        assert int(hdrs[b"content-length"]) == len(body)
        if hdrs.get(b"content-encoding") == b"gzip":
            json.loads(gzmod.decompress(body).decode())
        else:
            json.loads(body.decode())
        # plain (no Accept-Encoding): never gzipped, consistent
        code, hdrs, body = _srv_sock_request(port, "/", accept_gzip=False)
        assert code == 200 and b"content-encoding" not in hdrs
        assert int(hdrs[b"content-length"]) == len(body)
        assert body.decode().startswith("<!doctype html>")
    finally:
        p.terminate(); p.wait(timeout=10)

def test_v101_debug_search_route_exists():
    src = open("addon.py").read()
    assert '"/debug/search"' in src
    assert "status_code" in src and "_extract_cards" in src


# --- v1.1.0: egress fallback pool (site 403s datacenter IPs) --------------------

class _Resp:
    def __init__(self, code=200, text=""):
        self.status_code = code
        self.text = text

def _reset_pool_state():
    addon._FREE_POOL[0] = []
    addon._POOL_BAD.clear()
    addon._POOL_STICKY[0] = None
    addon._POOL_STICKY[1] = 0.0
    addon._POOL_TS[0] = 0.0
    addon._DIRECT_OK_UNTIL[0] = 0.0
    addon._DIRECT_RETRY_AT[0] = float("inf")   # direct benched during tests

def test_v110_pool_used_when_direct_blocked():
    """Direct benched -> request rides a pool exit; a good exit becomes sticky."""
    _reset_pool_state()
    calls = []
    def fake_get(url, headers=None, timeout=None, proxies=None, **kw):
        calls.append((url, proxies))
        if proxies is None:
            return _Resp(403, "blocked")          # direct path (not reached)
        if "p1" in (proxies or {}).get("http", ""):
            return _Resp(200, "ok-via-p1")
        raise ConnectionError("dead exit")
    with mock.patch.object(addon._S, "get", side_effect=fake_get):
        addon._FREE_POOL[0] = [("http://dead:1", 0.9), ("http://p1:2", 0.4)]
        # sticky forces dead:1 to be tried FIRST (deterministic, not 50/50)
        addon._POOL_STICKY[0] = "http://dead:1"
        addon._POOL_STICKY[1] = time.time() + 60
        r = addon._get("https://animedekho.app/?s=demon")
    assert r.status_code == 200 and r.text == "ok-via-p1"
    assert all(c[1] for c in calls)               # every attempt went via proxy
    assert addon._POOL_STICKY[0] == "http://p1:2"
    assert addon._POOL_BAD.get("http://dead:1", 0) > time.time()  # benched
    _reset_pool_state()

def test_v110_sticky_exit_reused():
    _reset_pool_state()
    n = [0]
    def fake_get(url, headers=None, timeout=None, proxies=None, **kw):
        n[0] += 1
        return _Resp(200, "x")
    with mock.patch.object(addon._S, "get", side_effect=fake_get):
        addon._FREE_POOL[0] = [("http://a:1", 0.9), ("http://b:2", 0.4)]
        addon._POOL_STICKY[0] = "http://b:2"
        addon._POOL_STICKY[1] = time.time() + 60
        r1 = addon._get("https://animedekho.app/?s=one")
        r2 = addon._get("https://animedekho.app/series-hindi/x/")
    assert r1.status_code == r2.status_code == 200
    assert n[0] == 2
    # both calls rode the sticky exit
    with mock.patch.object(addon._S, "get", side_effect=lambda *a, **k: _Resp(200, "")) as g:
        addon._get("https://animedekho.app/?s=three")
        assert g.call_args.kwargs["proxies"]["http"] == "http://b:2"
    _reset_pool_state()

def test_v110_exempt_hosts_stay_direct():
    """cinemeta/TMDB never burn pool exits, even when direct is benched."""
    _reset_pool_state()
    with mock.patch.object(addon._S, "get", return_value=_Resp(200, "{}")) as g:
        r = addon._get("https://v3-cinemeta.strem.io/meta/series/tt1.json")
    assert r.status_code == 200
    assert "proxies" not in g.call_args.kwargs
    _reset_pool_state()

def test_v110_direct_403_benches_and_falls_to_pool():
    """First call probes direct, gets 403 -> benched, pool serves."""
    _reset_pool_state()
    addon._DIRECT_RETRY_AT[0] = 0.0
    addon._DIRECT_OK_UNTIL[0] = time.time()      # believe direct is fine
    seq = []
    def fake_get(url, headers=None, timeout=None, proxies=None, **kw):
        seq.append(proxies)
        if proxies is None:
            return _Resp(403, "dc-blocked")
        return _Resp(200, "via-pool")
    with mock.patch.object(addon._S, "get", side_effect=fake_get):
        addon._FREE_POOL[0] = [("http://p:1", 0.4)]
        r = addon._get("https://animedekho.app/?s=demon")
    assert r.text == "via-pool"
    assert seq[0] is None and seq[1] is not None    # direct first, then pool
    assert addon._DIRECT_OK_UNTIL[0] == 0.0         # direct benched
    assert addon._DIRECT_RETRY_AT[0] > time.time()  # re-probe later
    _reset_pool_state()

def test_v110_pool_exhausted_falls_back_direct():
    """No exits alive -> final attempt is plain direct (transient block?)."""
    _reset_pool_state()
    seen = []
    def fake_get(url, headers=None, timeout=None, proxies=None, **kw):
        seen.append(proxies)
        return _Resp(200, "direct-lucky")
    with mock.patch.object(addon._S, "get", side_effect=fake_get):
        r = addon._get("https://animedekho.app/?s=x")
    assert r.text == "direct-lucky"
    assert seen[-1] is None
    _reset_pool_state()

def test_v110_health_reports_egress():
    src = open("addon.py").read()
    assert '"egress"' in src and "_DIRECT_OK_UNTIL" in src.split('"egress"')[1][:200]


# --- v1.2.0: served playlists (cdn master is IP-bound to the minting exit) ------

MASTER_SAMPLE = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",LANGUAGE="hin",NAME="Hindi",DEFAULT=YES,AUTOSELECT=YES,URI="hin/audio.m3u8"
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2",AUDIO="aud"
/cdn/hls/abc/1080/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",AUDIO="aud"
/cdn/hls/abc/720/index.m3u8
"""

def test_v120_master_rewrite():
    text, variants = addon._rewrite_master(
        MASTER_SAMPLE, "https://as-cdn26.top/cdn/hls/abc/master.m3u8?md5=x&expires=1")
    assert "\nv0.m3u8\n" in text and "\nv1.m3u8\n" in text
    assert 'URI="a0.m3u8"' in text and "hin/audio.m3u8" not in text
    assert "RESOLUTION=1920x1080" in text and 'AUDIO="aud"' in text
    assert variants["v0.m3u8"] == "https://as-cdn26.top/cdn/hls/abc/1080/index.m3u8"
    # relative (no leading /) audio uri resolves against the master's dir
    assert variants["a0.m3u8"] == "https://as-cdn26.top/cdn/hls/abc/hin/audio.m3u8"
    assert text.startswith("#EXTM3U")

def test_v120_variant_text_absolutizes_relative_lines():
    vtext = "#EXTM3U\n#EXTINF:5.0,\n/p/tok1\n#EXTINF:5.0,\nhttps://as-cdn27.top/p/tok2\n"
    class R2:
        status_code = 200; text = vtext
    with mock.patch.object(addon, "_get", return_value=R2()):
        out = addon._variant_text("https://as-cdn26.top/hls/xyz")
    assert "https://as-cdn26.top/p/tok1" in out          # relative -> absolute
    assert "https://as-cdn27.top/p/tok2" in out          # already absolute kept

def test_v120_variant_text_negative_cache():
    class R404:
        status_code = 404; text = ""
    with mock.patch.object(addon, "_get", return_value=R404()):
        assert addon._variant_text("https://as-cdn26.top/hls/zzz") is None
    hit, val = addon._cache_get(addon._VARIANT_CACHE, "https://as-cdn26.top/hls/zzz")
    assert hit and val is None

def test_v120_master_info_registers_served_entry():
    class RM:
        status_code = 200; text = MASTER_SAMPLE
    addon._MASTER_CACHE.clear(); addon._HLS_KEYS.clear()
    try:
        with mock.patch.object(addon, "_get", return_value=RM()):
            info = addon._master_info("https://as-cdn26.top/cdn/hls/abc/master.m3u8?md5=q&expires=9")
        assert info and info["res"] == [720, 1080] and "hin" in info["langs"]
        key = addon._hls_key("https://as-cdn26.top/cdn/hls/abc/master.m3u8?md5=q&expires=9")
        assert addon._HLS_KEYS[key] == "https://as-cdn26.top/cdn/hls/abc/master.m3u8?md5=q&expires=9"
        hit, val = addon._cache_get(addon._MASTER_CACHE,
                                    "https://as-cdn26.top/cdn/hls/abc/master.m3u8?md5=q&expires=9")
        assert hit and "master_text" in val and "v0.m3u8" in val["variants"]
    finally:
        addon._MASTER_CACHE.clear(); addon._HLS_KEYS.clear()

def test_v120_cdn_never_benches_direct():
    """An IP-bound master 403s direct (expected) — the cdn family must NOT
    bench direct for the site family or vice versa."""
    from test_animedekho import _reset_pool_state
    _reset_pool_state()
    addon._DIRECT_OK_UNTIL[0] = time.time() + 600   # site direct believed ok
    class R403:
        status_code = 403; text = "forbidden"
    class R200:
        status_code = 200; text = "#EXTM3U"
    calls = []
    def fake_get(url, headers=None, timeout=None, proxies=None, **kw):
        calls.append(proxies)
        return R403() if proxies is None else R200()
    with mock.patch.object(addon._S, "get", side_effect=fake_get):
        addon._FREE_POOL[0] = [("http://p:1", 0.4)]
        r = addon._get("https://as-cdn26.top/cdn/hls/abc/master.m3u8?md5=x&expires=1")
    assert r.status_code == 200                        # served via pool exit
    assert addon._DIRECT_OK_UNTIL[0] > time.time()     # NOT benched (cdn family)
    _reset_pool_state()

def test_v120_unrelated_host_plain_direct():
    with mock.patch.object(addon._S, "get", return_value=_Resp(200, "{}")) as g:
        addon._get("https://example.com/x")
    assert "proxies" not in g.call_args.kwargs

def test_v120_hls_route_regex_and_404():
    import subprocess, sys, os, socket, time as _t
    port = 7833
    p = subprocess.Popen([sys.executable, "addon.py"],
                         env=dict(os.environ, PORT=str(port)),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            _t.sleep(0.25)
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                pass
        code, hdrs, body = _srv_sock_request(port, "/hls/0123456789abcdef/master.m3u8", False)
        assert code == 404                             # unknown key -> honest 404
        code, hdrs, body = _srv_sock_request(port, "/hls/0123456789abcdef/v0.m3u8", False)
        assert code == 404
    finally:
        p.terminate(); p.wait(timeout=10)

def test_v121_hls_routes_serve_seeded_entry():
    """Full in-process server test: seed a served entry, hit master + a
    variant route (this exact shape — regex group vs dict key mismatch —
    shipped broken in v1.2.0 and the 404-only test missed it)."""
    import threading
    from http.server import ThreadingHTTPServer
    murl = "https://as-cdn26.top/cdn/hls/zz/master.m3u8?md5=z&expires=7"
    key = addon._hls_key(murl)
    mtext, variants = addon._rewrite_master(MASTER_SAMPLE, murl)
    addon._MASTER_CACHE.clear(); addon._HLS_KEYS.clear(); addon._VARIANT_CACHE.clear()
    addon._cache_put(addon._MASTER_CACHE, murl,
                     {"info": {"langs": ["hin"], "res": [720, 1080], "audio_rends": []},
                      "master_text": mtext, "variants": variants}, 600)
    addon._HLS_KEYS[key] = murl
    VT = "#EXTM3U\n#EXTINF:5.0,\nhttps://as-cdn26.top/p/tok1\n"
    class RV:
        status_code = 200; text = VT
    port = 7835
    srv = ThreadingHTTPServer(("127.0.0.1", port), addon.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with mock.patch.object(addon, "_get", return_value=RV()):
            code, hdrs, body = _srv_sock_request(port, "/hls/%s/master.m3u8" % key, False)
            assert code == 200 and body.decode().startswith("#EXTM3U")
            assert "\nv0.m3u8\n" in body.decode() and 'URI="a0.m3u8"' in body.decode()
            assert hdrs[b"content-type"] == b"application/vnd.apple.mpegurl"
            # THE v1.2.0 bug: name group must include .m3u8 to hit the dict
            code, hdrs, body = _srv_sock_request(port, "/hls/%s/v0.m3u8" % key, False)
            assert code == 200, body[:80]
            assert "https://as-cdn26.top/p/tok1" in body.decode()
            code, hdrs, body = _srv_sock_request(port, "/hls/%s/a0.m3u8" % key, False)
            assert code == 200
            code, hdrs, body = _srv_sock_request(port, "/hls/%s/v9.m3u8" % key, False)
            assert code == 404
    finally:
        srv.shutdown(); srv.server_close()
        addon._MASTER_CACHE.clear(); addon._HLS_KEYS.clear(); addon._VARIANT_CACHE.clear()


# --- v1.3.0: KITSU ids (Stremio anime catalogs use kitsu: ids) -------------------

KITSU_DN = {"data": {"attributes": {
    "canonicalTitle": "Death Note", "titles": {"en": "Death Note"},
    "subtype": "TV", "startDate": "2006-10-04"}}}

def test_v130_kitsu_title_resolves():
    class RK:
        status_code = 200
        text = ""
        def json(self):
            return KITSU_DN
    addon._META_CACHE.clear()
    try:
        with mock.patch.object(addon, "_get", return_value=RK()) as g:
            t, y = addon._kitsu_title("1376")
        assert (t, y) == ("Death Note", "2006")
        assert "kitsu.io/api/edge/anime/1376" in g.call_args[0][0]
        # cached on second call (no extra fetch)
        with mock.patch.object(addon, "_get", side_effect=AssertionError("refetch")):
            assert addon._kitsu_title("1376") == ("Death Note", "2006")
    finally:
        addon._META_CACHE.clear()

def test_v130_kitsu_title_falls_back_to_titles_en():
    class RK:
        status_code = 200
        def json(self):
            return {"data": {"attributes": {
                "canonicalTitle": "", "titles": {"en": "Naruto"},
                "startDate": "2002-10-03"}}}
    addon._META_CACHE.clear()
    try:
        with mock.patch.object(addon, "_get", return_value=RK()):
            assert addon._kitsu_title("11") == ("Naruto", "2002")
    finally:
        addon._META_CACHE.clear()

def test_v130_build_uses_kitsu_title_not_cinemeta():
    addon._META_CACHE.clear(); addon._SEARCH_CACHE.clear()
    try:
        with mock.patch.object(addon, "_kitsu_title", return_value=("Death Note", "2006")), \
             mock.patch.object(addon, "_cinemeta", side_effect=AssertionError("cinemeta!")), \
             mock.patch.object(addon, "search_candidates",
                               return_value=[{"title": "Death Note",
                                              "url": "https://animedekho.app/series-hindi/death-note/",
                                              "family": "series-hindi", "year": "2006"}]) as sc, \
             mock.patch.object(addon, "_resolve_card", return_value=None):
            addon._build_inner("series", "kitsu:1376", 1, 1)
        assert sc.call_args[0][0] == "Death Note"
    finally:
        addon._META_CACHE.clear(); addon._SEARCH_CACHE.clear()

def test_v130_stream_route_accepts_kitsu_ids():
    import threading
    from http.server import ThreadingHTTPServer
    port = 7837
    srv = ThreadingHTTPServer(("127.0.0.1", port), addon.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with mock.patch.object(addon, "build_streams",
                               return_value={"streams": [
                                   {"name": "x", "url": "/hls/abc0123456789def/master.m3u8"}]}) as bs:
            code, hdrs, body = _srv_sock_request(
                port, "/stream/series/kitsu:1376:1:1.json", False)
        assert code == 200
        # v1.9.4: a successful series answer ALSO fires a background
        # build_streams(ep+1) binge-prefetch — judge the ROUTE by its
        # first call, not the last
        calls = [tuple(c[0]) for c in bs.call_args_list]
        assert ("series", "kitsu:1376", 1, 1) in calls, calls
        d = json.loads(body.decode())
        assert d["streams"][0]["url"].startswith("https://127.0.0.1:")  # absolutized
        assert "/hls/" in d["streams"][0]["url"]
        # plain tt still works
        with mock.patch.object(addon, "build_streams", return_value={"streams": []}):
            code, hdrs, body = _srv_sock_request(port, "/stream/series/tt0877057:1:1.json", False)
        assert code == 200
        # junk prefixes still get an empty-but-200 answer
        code, hdrs, body = _srv_sock_request(port, "/stream/series/xyz:1:1.json", False)
        assert code == 404 or code == 200   # regex miss -> 404 fallthrough
    finally:
        srv.shutdown(); srv.server_close()


# --- v1.4.0: movie Skip-AD gate + html entities + token-subset matching --------

def test_v140_movie_gate_unlock():
    """Movie pages hide the embed behind a Skip-AD form; ONE GET of the
    form's shortlink (verify.php) + page reload reveals it."""
    gated = ('<form id="landing" method="post" action="https://animedekho.app/skip/vshort.php">'
             '<input type="hidden" name="postlink" value="https://animedekho.app/movie-hindi/x-hin/">'
             '<input type="hidden" name="shortlink" value="https://animedekho.app/24hr/verify.php?expires=99&token=abc">')
    open_page = '<a href="https://animedekho.app/embed/129/x">play</a>'
    seq = []
    class R:
        def __init__(self, text):
            self.status_code = 200; self.text = text
    def fake_get(url, timeout=10, referer=None):
        seq.append(url)
        if "verify.php" in url:
            return R("ok")                     # sets the cookie
        return R(open_page if len(seq) > 2 else gated)
    addon._PAGE_CACHE.clear()
    try:
        with mock.patch.object(addon, "_get", side_effect=fake_get):
            pg = addon._parse_movie_page("https://animedekho.app/movie-hindi/x-hin/")
        assert pg and pg["post_id"] == "129"
        assert pg["embed"] == "https://animedekho.app/embed/129"
        assert seq[1].startswith("https://animedekho.app/24hr/verify.php")  # gate unlocked
        assert seq[2] == "https://animedekho.app/movie-hindi/x-hin/"        # page reloaded
    finally:
        addon._PAGE_CACHE.clear()

def test_v140_html_entities_in_titles():
    class R:
        status_code = 200
        text = ('<article><h2 class="entry-title">Howl&#8217;s Moving Castle</h2>'
                '<a href="https://animedekho.app/movie-hindi/howls-moving-castle/" '
                'class="lnk-blk">x</a></article>')
    with mock.patch.object(addon, "_get", return_value=R()):
        cards = addon.site_search("howl")
    assert cards and cards[0]["title"] == "Howl’s Moving Castle"

def test_v140_clean_title_parentheticals():
    assert addon._clean_title("Demon Slayer Infinity Castle (Official)") == \
        "Demon Slayer Infinity Castle"
    assert addon._clean_title("Your Name. (Official Dub)") == "Your Name."
    assert addon._clean_title("One Piece Film Red (Camrip)") == "One Piece Film Red"

def test_v140_token_subset_matching():
    cands = [{"title": "Demon Slayer Infinity Castle (Official)", "family": "movie-hindi"},
             {"title": "Naruto", "family": "series-hindi"},
             {"title": "Naruto Shippuden", "family": "series-hindi"}]
    # site shortens the official movie name -> subset tier matches it
    m = addon._match_candidates(cands, "Demon Slayer: Kimetsu no Yaiba - The Movie: Infinity Castle",
                                "movie-hindi")
    assert m and "Infinity Castle" in m[0]["title"]
    # single-token site title can NEVER match a longer different work
    # (xtream lesson: wrong-title cards are worse than no cards)
    m2 = addon._match_candidates([cands[1]], "Naruto Shippuden", "series-hindi")
    assert m2 == []
    # ...but subtitle containment of the SAME work still matches
    m4 = addon._match_candidates(
        [{"title": "A Silent Voice", "family": "movie-hindi"}],
        "A Silent Voice: The Movie", "movie-hindi")
    assert m4 and m4[0]["title"] == "A Silent Voice"
    # exact still wins
    m3 = addon._match_candidates(cands[1:], "Naruto Shippuden", "series-hindi")
    assert m3 and m3[0]["title"] == "Naruto Shippuden"


# --- v1.9.5: blakiteapi (trdekho slot) — Rumble-backed direct source ---------

BLAKITE_API_JSON = {
    "success": True,
    "data": {
        "animeTitle": "Demon Slayer: Kimetsu no Yaiba Infinity Castle (Hindi Dubbed)",
        "tmdbId": "1311031", "type": "Movie", "language": "ORG",
        "dataId": "fww1/fb/s8/2/K/R/B/K/KRBKA", "qid": 5,
        "quality": "480p", "format": "M3U8",
        "ranges": ("258746880-258839413 (240p)\n782270976-782364008 (360p)\n"
                   "1218316288-1218409766 (480p)\n2474653696-2474748032 (720p)\n"
                   "4763655168-4763749878 (1080p)"),
        "poster": "https://image.tmdb.org/t/p/w500/x.jpg",
    },
    "debug": {},
}

class _BKResp:
    def __init__(self, status=200, text="", jdict=None):
        self.status_code = status
        self.text = text
        self._j = jdict
    def json(self):
        if self._j is None:
            raise ValueError("no json")
        return self._j

def test_v195_blakite_best_quality_direct_card():
    """blakiteapi: API ranges -> BEST (1080p) chunklist card, url DIRECT
    (rumble CDN, no /hls relay), name carries FHD 1080p + blakite fam."""
    _reset()
    def fake_get(url, timeout=10, referer=None):
        if "api/get.php" in url:
            assert "tmdbId=1311031" in url
            return _BKResp(jdict=BLAKITE_API_JSON)
        if "chunklist.m3u8" in url:
            assert url.startswith("https://hugh.cdn.rumble.cloud/video/fww1/fb/s8/2/K/R/B/K/KRBKA.haa.tar?"), url
            assert "r_range=4763655168-4763749878" in url
            return _BKResp(text="#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:10,\nseg\n")
        raise AssertionError("unexpected fetch: " + url)
    with mock.patch.object(addon, "_get", side_effect=fake_get):
        card = addon._blakite_resolve("https://blakiteapi.xyz/embed/1311031",
                                      "Demon Slayer Infinity Castle", 2025)
    assert card, "card must build from a live-looking API answer"
    assert card["url"].startswith("https://hugh.cdn.rumble.cloud/video/")
    assert "/hls/" not in card["url"], "blakite is open — direct url, no relay"
    assert "FHD 1080p" in card["name"]
    assert "blakite" in card["description"]
    assert card["behaviorHints"]["notWebReady"] is False
    assert card["bingeGroup"] == "adk|Demon Slayer Infinity Castle|blakite"

def test_v195_blakite_dead_chunklist_is_honestly_skipped():
    """no phantom cards: a dead/unverified chunklist -> no card at all."""
    _reset()
    def fake_get(url, timeout=10, referer=None):
        if "api/get.php" in url:
            return _BKResp(jdict=BLAKITE_API_JSON)
        return _BKResp(status=403, text="Forbidden")
    with mock.patch.object(addon, "_get", side_effect=fake_get):
        assert addon._blakite_resolve("https://blakiteapi.xyz/embed/1311031",
                                      "T", 2025) is None

def test_v195_blakite_fallback_quality_when_1080_missing():
    """canonical quality order: when 1080p is absent the next best wins."""
    _reset()
    d = dict(BLAKITE_API_JSON)
    d = json.loads(json.dumps(d))
    d["data"]["ranges"] = ("782270976-782364008 (360p)\n2474653696-2474748032 (720p)")
    def fake_get(url, timeout=10, referer=None):
        if "api/get.php" in url:
            return _BKResp(jdict=d)
        assert ".gaa.tar?" in url and "r_range=2474653696-2474748032" in url, url
        return _BKResp(text="#EXTM3U\n#EXTINF:9,\nx\n")
    with mock.patch.object(addon, "_get", side_effect=fake_get):
        card = addon._blakite_resolve("https://blakiteapi.xyz/embed/1311031",
                                      "T", 2025)
    assert card and "HD 720p" in card["name"]

def test_v195_blakite_fam_tag_priority_and_dispatch():
    """the trdekho family map recognises blakite and orders it after
    emturbo, before the gated cdn; _player_master still honest-skips
    unknown hosts."""
    assert addon._tr_fam_tag("https://blakiteapi.xyz/embed/1311031") == "blakite"
    assert addon._TR_PRIO["emturbo"] < addon._TR_PRIO["blakite"] < addon._TR_PRIO["cdn"]
    # non-numeric id -> honest skip
    _reset()
    with mock.patch.object(addon, "_get") as g:
        assert addon._blakite_resolve("https://blakiteapi.xyz/embed/abc", "T", 2025) is None
        g.assert_not_called()


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    run(fns)
    print("\n%d/%d tests passed" % (PASS, len(fns)))
