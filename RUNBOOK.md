# Runbook

Step-by-step procedures for keeping this playlist working. For *why* it is built this way,
see [BUILD-DURABILITY.md](BUILD-DURABILITY.md).

| | |
|---|---|
| Playlist URL (what players use) | `https://raw.githubusercontent.com/oaatiq/tv-playlist/main/playlist.m3u` |
| Guide URL | `https://raw.githubusercontent.com/oaatiq/tv-playlist/main/epg.xml.gz` |
| Worker | `https://ma-tv.oaatiq.workers.dev` |
| MINIX U8K (test box) | `192.168.2.131:5555` |
| MiTV (paid Xtream + this playlist) | `192.168.2.135:5555` |

---

## 1. Rebuild the playlist

**Never run `build_tivimate.py` straight into this repo** — it regenerates `playlist.m3u`
from the Kodi lists and knows nothing about `overrides/`, so it would silently drop ~150
channels' worth of content, grouping and numbering.

```powershell
cd C:\Users\aatiq\MINIX-U8K\tv-playlist
.\rebuild.ps1 -WhatIf        # build + verify, change nothing
.\rebuild.ps1                # same, then prompt to replace playlist.m3u
.\rebuild.ps1 -PullFromBox   # refresh sources\ off the box first
.\rebuild.ps1 -Force         # no confirmation prompt
```

It runs build → `apply_overrides.py` → `verify_playlist.py`, and only replaces
`playlist.m3u` once the check passes. Read the "against playlist.m3u" section it prints:
that is every channel that moved, changed URL or disappeared. Then publish (section 4).

Hard failures — duplicate channel numbers, a group split across the file, group order that
contradicts the numbering, CRLF, a BOM — abort before `playlist.m3u` is touched.

**Expect one spurious diff every time:** CNN (1475). The published playlist carries the
resolved CloudFront URL that `recheck.py` wrote on the box, while `sources/world_fr_en.m3u`
still has the `sis-global` one, so a rebuild "reverts" it. Both work (the sis-global URL is
a 307 to the same place). Either keep the published line or let it change - it is not a
fault. `-PullFromBox` makes it go away, at the cost of whatever else drifted on the box.

## 2. Change what is in the playlist

Everything that is not in the Kodi source lists lives in `overrides/overrides.json`.
Edit it, then run `.\rebuild.ps1`.

**Add a channel** — append to `add`. `chno` must fall inside the group's range so the
ordering check passes (see the table in BUILD-DURABILITY.md §2):

```json
{ "chno": 1243, "name": "MBC Drama USA", "group": "Arabie Saoudite",
  "url": "https://example.com/stream.m3u8",
  "tvg-id": "MBCDramaUSA.sa@SD", "tvg-logo": "https://example.com/logo.png" }
```

**Remove a channel** — add its `tvg-id` to `remove.tvg_id`. To drop a whole country, add
its code to `remove.tvg_id_country_codes` (currently `ru`, `by`).

**Move channels between groups** — add `"<tvg-id>": "<group name>"` to
`regroup.by_tvg_id`.

**Renumber a group** — set its base in `renumber`. Members are numbered from that base in
the order the build emits them.

**Reorder the groups** — reorder `group_order`. It must stay consistent with the numbering
(each group's lowest number ascending), or verification fails.

**Fill in a missing logo or id** — add to `patch_attrs.by_group_and_name`, keyed
`"<group>\t<name>"`. It only fills blanks, never overwrites.

**Drop a channel's `#EXTVLCOPT` line** - add its `tvg-id` to `strip_props.tvg_id`.
XCIPTV hands that line to Cronet in place of the stream URL
(`net::ERR_INVALID_URL, ErrorCode=11`, raised before anything leaves the box), so a
channel that does not need its header plays only once the line is gone. Test first -
fetch the URL with the header and without it - and strip only when both come back as a
real playlist. The four that genuinely need theirs (Universal Comedy, Universal Crime,
TV3 Ghana, AXN CEE Romania) keep the line and stay broken in XCIPTV until they are
routed through the Worker's `/h/` route.

**Give a channel the Worker's header proxy** - add it to `PROXY` in `worker/src/index.js`
(`url`, `ref` and/or `ua`, and a `hosts` allowlist covering its master, variant and segment
hosts), `npx wrangler deploy`, then in `overrides.json` put the channel's `tvg-id` in
`remove.tvg_id` and re-add it under `add` pointing at
`https://ma-tv.oaatiq.workers.dev/h/<slug>.m3u8`. Deploy **before** publishing the playlist,
or the route 404s. Proxied so far: `2m-monde`, `military-history`, `press-tv`, `music-box`,
`universal-comedy`, `universal-crime`, `tv3-ghana`.

**Not every channel can be proxied.** If the origin sits behind Cloudflare's own bot
protection, a Worker subrequest gets "Attention Required!" and a 403 no matter what, because
the request arrives from Cloudflare. AXN CEE Romania (`cronos.mangora1.cfd`) is the known
case: it needs a Referer, cannot be proxied, and so stays direct carrying its `#EXTVLCOPT` -
the one channel knowingly left broken in XCIPTV, working in Kodi and OTT. The 502 body names
this itself, which is what the diagnostic text in `headerProxy` is for.

Test a route without deploying - the Worker is plain ESM, so node can run it:

```powershell
copy worker\src\index.js $env:TEMP\worker.mjs
node -e "const {pathToFileURL}=require('url'); import(pathToFileURL(process.env.TEMP+'/worker.mjs')).then(async m => { const r = await m.default.fetch(new Request('https://x/h/tv3-ghana.m3u8')); console.log(r.status, (await r.text()).slice(0,120)); })"
```

## 3. Reach the boxes

adb over LAN. Both are already authorised; neither is rooted.

```bash
adb connect 192.168.2.131:5555     # U8K
adb connect 192.168.2.135:5555     # MiTV
```

Quirks worth knowing:

- **`adb pull` fails on the Kodi data directory** (not world-readable on Android 11). Use
  `adb exec-out "cat <path>" > local` instead. That is what `-PullFromBox` does.
- **The MiTV prepends junk to `screencap`** (`Init wrapper sys mutex successful...`), so
  the PNG is invalid until you strip everything before the `\x89PNG` magic bytes.
- **The U8K's `screencap` cannot see video**, which renders on a separate hardware plane —
  a playing channel looks like a black screen. The MiTV's does capture video.
- **Neither box has `curl` or `wget`.** For HTTP tests use `nc` (see section 6).
- **No root on either box**, so XCIPTV's config in `/data/data` cannot be backed up.

## 4. Publish

```powershell
git add -A; git commit -m "Rebuild playlist"; git push
```

Then reload the players:

- **XCIPTV:** main menu → **UPDATE** (top right), or it refreshes on next launch.
- **Kodi:** the IPTV Simple add-on reloads on restart, or via its context menu.

GitHub's raw CDN can cache for a minute or two; if a player shows the old list, wait and
update again.

## 5. Add this playlist to XCIPTV on another device

Verified on the MiTV (XCIPTV 7.0) on 2026-10-12, alongside a paid Xtream account.

1. Main menu → **ACCOUNT** (bottom left).
2. **+M3U** (top row).
3. **Name:** anything, e.g. `tv-playlist`. **M3U URL:** the playlist URL at the top of this
   file. Tap each field directly — pressing DOWN moves focus inside the on-screen keyboard,
   not to the next field, so the URL ends up appended to the name.
4. Close the keyboard before pressing **+ADD** — while it is open the dialog shifts up and
   the buttons are not where they appear to be.
5. To switch accounts: ACCOUNT → tap the one you want → **LOGIN**.

> **Careful:** tapping an M3U account opens **CANCEL / REMOVE / LOGIN**, and **REMOVE is
> the highlighted default**. Press LOGIN deliberately. The `Default (XC)` account offers
> only CANCEL/LOGIN, so the paid account cannot be removed by accident.

Accounts are independent records. Adding one does not touch the others, and the M3U
account pulls only from GitHub and the Worker, so it consumes none of the paid provider's
connections. Switching reloads channels and takes 30–60 s for the Xtream account.

Do **not** use SETTINGS → panel type to add a playlist — that reconfigures the *current*
account instead of adding one.

## 6. When a channel will not play

### Start with the whole group

`check_group.py` play-tests every channel in a group from this PC: it fetches the URL,
confirms it really is an m3u8 (or a DASH manifest), and for a master playlist fetches one
variant and confirms it has segments. That is what a player does on tune-in, so OK means
the stream was serving seconds ago.

```powershell
python -I check_group.py --list                 # group names and counts
python -I check_group.py France                 # one group
python -I check_group.py Maroc "Infos FR"       # several
python -I check_group.py --all --quiet          # everything, failures only (~3 min)
python -I check_group.py France --url https://raw.githubusercontent.com/oaatiq/tv-playlist/main/playlist.m3u
```

It exits 1 if anything failed. Statuses: `DEAD` (no answer or an HTTP error), `VARIANT`
(master loads, the variant does not), `EMPTY` (a playlist with no segments), `NOTM3U`
(answered with something else), `NOURL` (playlist bug - no stream line).

A channel that is OK here but dead on the TV is **not** a playlist problem - it is the path
from that device or the decoder. Work outwards from the box with the steps below.

### Then work outwards from the box

```bash
# 1. does the box reach the host at all?
adb -s 192.168.2.131:5555 shell "ping -c 3 -W 3 <host-or-ip>"

# 2. does HTTP work? (no curl on the box; nc + a raw GET)
adb -s 192.168.2.131:5555 shell "echo -e 'GET /path HTTP/1.0\r\nHost: <host>\r\n\r\n' \
  | timeout 20 nc <host> 80 | head -5"

# 3. control: a host that definitely works
adb -s 192.168.2.131:5555 shell "echo -e 'GET / HTTP/1.0\r\nHost: 1.1.1.1\r\n\r\n' \
  | timeout 10 nc 1.1.1.1 80 | head -3"

# 4. compare against the laptop, at the same moment
curl -s -o /dev/null -w '%{http_code} %{size_download}B %{time_total}s\n' http://<host>/path
```

If the laptop gets a 200 and the box gets nothing, it is not the stream — it is the path to
that host from that device. Check the router's filtering before touching the playlist.

Other things to check:

- **Kodi's log:** `adb exec-out "tail -40 /storage/emulated/0/Android/data/org.xbmc.kodi/files/.kodi/temp/kodi.log"`
- **What the player is actually connected to:** `adb shell "ss -tn state established"`
- **Is it the decoder, not the network?** Channel 14 (2M) is MPEG-2; Android phones have no
  MPEG-2 decoder and play it audio-only. Switching XCIPTV's engine to VLC works around it.

### Known: channel 14 (2M) does not play on the U8K

Measured 2026-10-12. The box reaches `185.9.2.18` by ICMP (428 ms, 33% loss) but gets zero
bytes over TCP 80, while the laptop on the same LAN gets HTTP 200 in ~1 s at the same
moment, and the box reaches `1.1.1.1` and the Worker's Cloudflare IP (12.9 ms, 0% loss)
normally. No VPN is active on the box and the route is the normal default. That points at
per-device filtering on the router, not at the stream. Unresolved.

## 7. The automated re-check

`playlists/autocheck/recheck.py` runs from a Windows scheduled task
(`MINIX U8K - weekly channel re-check`) every two hours from 07:00 to 23:00, and at logon.
It re-checks every channel weekly, drops dead ones, re-resolves Samsung TV Plus URLs,
rebuilds the guide and pushes both to the box.

It maintains the **world** list only, and never touches this repo — so the only thing that
can overwrite `playlist.m3u` is a hand-run build. But it does mean the source list drifts
on its own, so a rebuild months from now will not produce the same file.

```powershell
Get-ScheduledTask -TaskName 'MINIX U8K - weekly channel re-check'
python -I C:\Users\aatiq\MINIX-U8K\playlists\autocheck\recheck.py --force --no-push
Get-Content C:\Users\aatiq\MINIX-U8K\playlists\autocheck\logs\*.log -Tail 20
```

## 8. Recovery

- **A rebuild produced something wrong:** `git checkout -- playlist.m3u`, or
  `git checkout <commit> -- playlist.m3u` for an older one. The playlist is versioned;
  every state it has ever been in is recoverable.
- **The source lists were lost on the box:** they are committed in `sources/`. Push them
  back with `adb push`, then restart Kodi.
- **`build_tivimate.py` was lost:** a copy is in `sources/build_tivimate.py`. The live one
  is at `MINIX-U8K\playlists\tivimate\build_tivimate.py` and is tracked nowhere else.
- **An override stopped matching:** `apply_overrides.py` prints a warning naming it — an
  id that no longer exists upstream, or a group that vanished. Channels in a group the
  override does not know about are kept and reported, never dropped.
