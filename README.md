# tv-playlist

Personal IPTV playlist for OTT Player / OTT Navigator on a MINIX U8K box.

| File | What |
|---|---|
| `playlist.m3u` | 1,386 free-to-air channels in 9 groups. The **Maroc** group comes first. |
| `epg.xml.gz` | XMLTV guide, referenced from the playlist header (`url-tvg`). |
| `worker/` | Cloudflare Worker at `https://ma-tv.oaatiq.workers.dev` that serves SNRT (1–9) and Télé Maroc (10), and proxies four channels that need an HTTP header. |

## Why the Worker exists

SNRT's EasyBroadcast streams need a token on every playlist request, and that token expires after one hour, so a static link stops working. The Worker fetches and renews the token. It serves only the small `.m3u8` playlists; the video segments come straight from the CDN. Télé Maroc's signed Cloudflare Stream link expires after about 12 hours, so the Worker resolves a fresh one from telemaroc.tv.

## The `/h/` header routes

XCIPTV ignores `#EXTVLCOPT`, so channels whose CDN checks a `Referer` or `User-Agent` can't carry the header in the playlist. The Worker attaches it instead, and the player sees a plain URL.

Of the 24 entries that carried headers, only these four actually need one: **2M Monde**, **Military History**, **Press TV** and **Music Box Polska Ukraine**. Every other one — all the BFM channels, Rai Italia, Tele 7, S Play Event, AXN — returns 200 with no header, so they stay direct.

These four are protected on their segments as well as their playlists, so unlike the SNRT routes the video itself passes through the Worker. The `u` parameter is restricted to each channel's own hosts so `/h/` can't be used as an open proxy.

## Updating

- **Playlist:** run `playlists/tivimate/build_tivimate.py` with `--out tv-playlist --out-name playlist.m3u --grouping country --maroc-worker https://ma-tv.oaatiq.workers.dev --epg-url https://raw.githubusercontent.com/oaatiq/tv-playlist/main/epg.xml.gz`, then commit and push.

  The two source lists live on the box, not in this repo, under `/storage/emulated/0/Android/data/org.xbmc.kodi/files/.kodi/userdata/addon_data/pvr.iptvsimple/`: `direct.m3u` (`--direct`, the curated 1–48) and `world_fr_en.m3u` (`--world`, the numbered world list). The copies under `playlists/` have no `tvg-chno` and will not build.
- **Worker:** run `cd worker && npx wrangler deploy`.

Channel 14 (2M) and channel 107 (Al Maghribia HD, labelled "(backup)") are third-party re-streams from Net Sat AB, Sweden, and may disappear without notice. 2M is at 14, next to 2M Monde, because the Globecast 2M Monde feed (15) stutters on the box while this one runs at a steady 25 fps, about 20 s behind live.
