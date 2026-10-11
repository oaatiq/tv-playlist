# tv-playlist

Personal IPTV playlist for OTT Player / OTT Navigator on a MINIX U8K box.

| File | What |
|---|---|
| `playlist.m3u` | 1,386 free-to-air channels. The **Maroc** group comes first. |
| `epg.xml.gz` | XMLTV guide, referenced from the playlist header (`url-tvg`). |
| `worker/` | Cloudflare Worker at `https://ma-tv.oaatiq.workers.dev` that serves SNRT (1–9) and Télé Maroc (10). |

## Why the Worker exists

SNRT's EasyBroadcast streams need a token on every playlist request, and that token expires after one hour, so a static link stops working. The Worker fetches and renews the token. It serves only the small `.m3u8` playlists; the video segments come straight from the CDN. Télé Maroc's signed Cloudflare Stream link expires after about 12 hours, so the Worker resolves a fresh one from telemaroc.tv.

## Updating

- **Playlist:** run `playlists/tivimate/build_tivimate.py` with `--out tv-playlist --out-name playlist.m3u --maroc-worker https://ma-tv.oaatiq.workers.dev --epg-url https://raw.githubusercontent.com/oaatiq/tv-playlist/main/epg.xml.gz`, then commit and push.
- **Worker:** run `cd worker && npx wrangler deploy`.

Channel 14 (2M) and channel 107 (Al Maghribia HD, labelled "(backup)") are third-party re-streams from Net Sat AB, Sweden, and may disappear without notice. 2M is at 14, next to 2M Monde, because the Globecast 2M Monde feed (15) stutters on the box while this one runs at a steady 25 fps, about 20 s behind live.
