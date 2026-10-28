# tv-playlist

Personal IPTV playlist for OTT Player / OTT Navigator on a MINIX U8K box.

| File | What |
|---|---|
| `playlist.m3u` | 1,431 free-to-air channels in 16 groups. **Generated** — see [Updating](#updating). The **Maroc** group comes first. |
| `guide_world.py` | Builds the programme guide (Samsung TV Plus, EPGShare) for `sources/world_fr_en.m3u` and the rest of `playlist.m3u`, read as a stream so it fits on the router. RUNBOOK §7a. |
| `guide_maroc.py` | Fetches the Maroc channels' schedules (snrt.ma, SFR, Orange) and merges them into the guide under the playlist's own tvg-ids. RUNBOOK §7a. |
| `router/tv-guide-update.sh` | Runs both on the router every three hours and publishes `epg.xml.gz` to the **`guide` branch**, which the playlist header's `url-tvg` points at: one force-pushed commit, so the history does not grow. |
| `worker/` | Cloudflare Worker at `https://ma-tv.oaatiq.workers.dev` that serves the SNRT channels and Télé Maroc, and proxies four channels that need an HTTP header. |
| `sources/` | Committed snapshots of the two Kodi lists the build reads, and of the build script. |
| `overrides/` | Everything the build cannot produce: added channels, removals, grouping, group order. |
| `rebuild.ps1` | Build → apply overrides → verify → replace. **The only safe way to rebuild.** |
| `add_channel.py` | Add one channel, or a whole `.m3u`, without touching the JSON or rebuilding. |

## Channel numbers

Channel numbers are the order on screen, 1 to N. They are not stored anywhere:
`apply_overrides.py` numbers the finished list top to bottom on every build. When the health
check removes a dead channel, everything below it moves up by one; when the channel comes
back, it returns to its old place and everything below moves down again. Nothing else
keys on the number — the guide matches on `tvg-id`, the health check remembers channels by
`tvg-id`, and the Worker by URL — so a shift costs nothing but the number on the remote.

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

**One new channel — no rebuild needed.** Adding does not disturb anything else, so
`add_channel.py` writes the override and re-applies it to `playlist.m3u` directly:

```powershell
python -I add_channel.py --groups                   # group names, sizes, current numbers
python -I add_channel.py --group Maroc --name "Chaine X" --url https://host/x.m3u8 --apply
python -I add_channel.py --from-m3u new.m3u --group "Other countries" --apply
```

It puts the channel at the end of its group, generates the `tvg-id`, probes the stream, and reverts
`overrides.json` and leaves `playlist.m3u` alone if the result does not verify. Drop
`--apply` to write the override only. See RUNBOOK §2 for the flags.

| Script | On its own |
|---|---|
| `add_channel.py --group G --name N --url U` | appends to `overrides/overrides.json`'s `add`, probes, verifies |
| `apply_overrides.py <playlist>` | re-applies `overrides/overrides.json`; idempotent |
| `verify_playlist.py <playlist> [--compare old]` | checks numbering/grouping/encoding, lists what drifted |

See [RUNBOOK.md](RUNBOOK.md) for step-by-step procedures — rebuilding, editing the overrides,
reaching the boxes, adding this playlist to another XCIPTV device, and diagnosing a channel that
will not play. See [BUILD-DURABILITY.md](BUILD-DURABILITY.md) for what lives in the overrides and why.

**Worker:** run `cd worker && npx wrangler deploy`.

2M and Al Maghribia HD (labelled "(backup)") are third-party re-streams from Net Sat AB, Sweden, and may disappear without notice. 2M sits just before 2M Monde because the Globecast 2M Monde feed stutters on the box while this one runs at a steady 25 fps, about 20 s behind live.
