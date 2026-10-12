# tv-playlist

Personal IPTV playlist for OTT Player / OTT Navigator on a MINIX U8K box.

| File | What |
|---|---|
| `playlist.m3u` | 1,431 free-to-air channels in 16 groups. **Generated** — see [Updating](#updating). The **Maroc** group comes first. |
| `epg.xml.gz` | XMLTV guide, referenced from the playlist header (`url-tvg`). Built separately, by `playlists/autocheck/epg.py`. |
| `worker/` | Cloudflare Worker at `https://ma-tv.oaatiq.workers.dev` that serves SNRT (1–9) and Télé Maroc (10), and proxies four channels that need an HTTP header. |
| `sources/` | Committed snapshots of the two Kodi lists the build reads, and of the build script. |
| `overrides/` | Everything the build cannot produce: added channels, removals, grouping, numbering. |
| `rebuild.ps1` | Build → apply overrides → verify → replace. **The only safe way to rebuild.** |

## Why the Worker exists

SNRT's EasyBroadcast streams need a token on every playlist request, and that token expires after one hour, so a static link stops working. The Worker fetches and renews the token. It serves only the small `.m3u8` playlists; the video segments come straight from the CDN. Télé Maroc's signed Cloudflare Stream link expires after about 12 hours, so the Worker resolves a fresh one from telemaroc.tv.

## The `/h/` header routes

XCIPTV ignores `#EXTVLCOPT`, so channels whose CDN checks a `Referer` or `User-Agent` can't carry the header in the playlist. The Worker attaches it instead, and the player sees a plain URL.

Of the 24 entries that carried headers, only these four actually need one: **2M Monde**, **Military History**, **Press TV** and **Music Box Polska Ukraine**. Every other one — all the BFM channels, Rai Italia, Tele 7, S Play Event, AXN — returns 200 with no header, so they stay direct.

These four are protected on their segments as well as their playlists, so unlike the SNRT routes the video itself passes through the Worker. The `u` parameter is restricted to each channel's own hosts so `/h/` can't be used as an open proxy.

## Updating

**Playlist — run `.\rebuild.ps1`, never `build_tivimate.py` on its own.**

`playlist.m3u` is generated from the two Kodi lists in `sources/`, but roughly 150 channels' worth of
grouping, numbering and content exists only in `overrides/overrides.json`. The build knows nothing
about it, so building straight into this repo silently throws it away. `rebuild.ps1` runs the build,
re-applies the overrides, checks the result and shows you what moved before anything is replaced.

```powershell
.\rebuild.ps1                 # build from the committed sources\ lists
.\rebuild.ps1 -PullFromBox    # refresh sources\ off the box first
.\rebuild.ps1 -WhatIf         # build and verify only, leave playlist.m3u alone
```

It writes nothing outside the working tree. Commit, push and reload the player yourself:

```powershell
git add -A; git commit -m "Rebuild playlist"; git push
```

then **Update Contents** in XCIPTV on the box.

| Script | On its own |
|---|---|
| `apply_overrides.py <playlist>` | re-applies `overrides/overrides.json`; idempotent |
| `verify_playlist.py <playlist> [--compare old]` | checks numbering/grouping/encoding, lists what drifted |

See [BUILD-DURABILITY.md](BUILD-DURABILITY.md) for what lives in the overrides and why.

**Worker:** run `cd worker && npx wrangler deploy`.

Channel 14 (2M) and channel 107 (Al Maghribia HD, labelled "(backup)") are third-party re-streams from Net Sat AB, Sweden, and may disappear without notice. 2M is at 14, next to 2M Monde, because the Globecast 2M Monde feed (15) stutters on the box while this one runs at a steady 25 fps, about 20 s behind live.
