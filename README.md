# AnimeDekho Stremio Addon

Hindi-dub anime & cartoons from **animedekho.app** as direct Stremio streams.

- 🔊 **Multi-audio** — Japanese / Hindi / English / Telugu / Tamil in one HLS master (pick in your player)
- 🎚 **Multi-quality** — 240p → 1080p adaptive
- 💬 **English subtitles** — straight from the CDN
- ⚡ **Strict zero-bandwidth** — the addon serves only tiny JSON; the media chain (master → variant playlists → segments) flows **CDN ↔ player directly**
- 🎯 **No phantom cards** — every master.m3u8 is fetched and verified before a card is served

## How it resolves (text-only fetches)

1. `cinemeta` → title/year from the IMDb id
2. `animedekho.app/?s=` → result cards (progressive keyword search — the site's search word-ANDs)
3. series page → WP post id + episode list; movie page → embed link
4. `/embed/{postId}/{se}-{ep}` (**no cookie needed** — the ad-gate only guards the website) → `as-cdn{N}.top/video/{vid}`
5. `POST …/player/index.php?data={vid}&do=getVideo` (only gate: `X-Requested-With: XMLHttpRequest`) → signed `master.m3u8` (~2h)
6. master fetched once to verify + read langs/resolutions → card

## Run locally

```bash
pip install requests
python3 addon.py            # listens on :7000 (PORT env to change)
```

## Deploy (Render)

Any Python service works — no build step. Use `render.yaml` (blueprint) or:
- **Build:** `pip install requests`
- **Start:** `python3 addon.py`

## Tests

```bash
python3 -m pytest test_animedekho.py -q
```
