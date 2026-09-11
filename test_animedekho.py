#!/usr/bin/env python3
"""Unit tests for the AnimeDekho addon. Run: python3 -m pytest test_animedekho.py -q
Everything network-touching is mocked — hermetic and deterministic."""
import json
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
              addon._REQLOG):
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
    with mock.patch.object(addon, "_get", return_value=R(PLAYER_HTML)):
        subs = addon._player_subs("https://as-cdn26.top/video/x")
    assert len(subs) == 1
    assert subs[0]["lang"] == "eng" and subs[0]["url"].endswith(".jpg")

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
         mock.patch.object(addon._S, "post", return_value=R(js=json.loads(GETVIDEO_JSON))):
        card = addon._resolve_card("Jujutsu Kaisen",
                                   "https://animedekho.app/embed/95479/2-3",
                                   "series", 2, 3, "2020")
    assert card and card["url"].startswith("https://as-cdn26.top/cdn/hls/")
    assert card["url"] == json.loads(GETVIDEO_JSON)["videoSource"]
    assert "1080p" in card["description"] and "Hindi" in card["description"]
    assert card["subtitles"][0]["lang"] == "eng"
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
    assert c["name"] == "𖤍 Jujutsu Kaisen"
    assert "S02E03" in c["description"]
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
    assert names[0] == "𖤍 Jujutsu Kaisen 0"          # from MOVIE_HTML h1
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
    assert m["resources"] == ["stream"]                  # stream-only
    assert set(m["types"]) == {"movie", "series"}
    assert m["idPrefixes"] == ["tt"]

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


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    run(fns)
    print("\n%d/%d tests passed" % (PASS, len(fns)))
