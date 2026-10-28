# Runbook

Step-by-step procedures for keeping this playlist working. For *why* it is built this way,
see [BUILD-DURABILITY.md](BUILD-DURABILITY.md).

| | |
|---|---|
| Playlist URL (what players use) | `https://raw.githubusercontent.com/oaatiq/tv-playlist/main/playlist.m3u` |
| Guide URL | `https://raw.githubusercontent.com/oaatiq/tv-playlist/guide/epg.xml.gz` |
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

Hard failures — duplicate channel numbers, numbers that do not run 1..N down the file, a
group split across the file, group order that contradicts the numbering, CRLF, a BOM — abort before `playlist.m3u` is touched.

**Expect one spurious diff every time:** CNN. The published playlist carries the
resolved CloudFront URL that `recheck.py` wrote on the box, while `sources/world_fr_en.m3u`
still has the `sis-global` one, so a rebuild "reverts" it. Both work (the sis-global URL is
a 307 to the same place). Either keep the published line or let it change - it is not a
fault. `-PullFromBox` makes it go away, at the cost of whatever else drifted on the box.

## 2. Change what is in the playlist

Everything that is not in the Kodi source lists lives in `overrides/overrides.json`.
Edit it, then run `.\rebuild.ps1`.

**Add a channel** — use `add_channel.py` rather than editing the JSON by hand. It puts the
channel at the end of its group, generates the `tvg-id`, tests the stream, writes the
override and then re-runs `apply_overrides.py` + `verify_playlist.py` to prove the result
is still sound. If anything fails, `overrides.json` is restored byte-for-byte and
`playlist.m3u` is never touched.

```powershell
python -I add_channel.py --groups                       # group names, sizes, current numbers
python -I add_channel.py --group Maroc --name "Chaine X" `
        --url https://host/x.m3u8 --logo https://host/logo.png --apply
python -I add_channel.py --from-m3u new.m3u --group "Other countries" --apply
```

It refuses to run while this clone is behind the remote, because the health check on the
router pushes a repaired playlist by itself and adding on top of a stale copy would put
back whatever it had just removed. `git pull` first; `--no-fetch` skips the check when you
are offline.

`--apply` updates `playlist.m3u` too, which is enough on its own — a full `rebuild.ps1` is
only needed when the Kodi source lists changed. Without it the override is written and the
playlist is left alone. Other flags: `--dry-run`,
`--no-probe` (a channel geo-locked to Morocco fails the probe from the laptop and is still
fine on the box), `--force` to add past a failed probe or a duplicate name,
`--keep-headers` to keep an `#EXTVLCOPT` line from an imported list (off by default,
because that line stops the channel playing in XCIPTV — see section 6).

Then publish as in section 4: `git add -A; git commit; git push`, then **Update Contents**
in XCIPTV.

By hand, if you prefer: append to `add`. There is no number to choose — an added channel
goes after the build's channels in its group, in the order the `add` list gives, so move
the record up or down the list to move the channel within its group:

```json
{ "name": "MBC Drama USA", "group": "Arabie Saoudite",
  "url": "https://example.com/stream.m3u8",
  "tvg-id": "MBCDramaUSA.sa@SD", "tvg-logo": "https://example.com/logo.png" }
```

**Remove a channel** — add its `tvg-id` to `remove.tvg_id`. To drop a whole country, add
its code to `remove.tvg_id_country_codes` (currently `ru`, `by`).

**Move channels between groups** — add `"<tvg-id>": "<group name>"` to
`regroup.by_tvg_id`.

**Channel numbers** — not configurable, on purpose. `apply_overrides.py` numbers the
finished list 1..N from top to bottom, so the number on the remote always matches the
order on screen. A removed channel closes its gap and every number below it drops by one;
when it comes back it takes its old place and they move back. `verify --compare` reports
that as one "renumbered: N, order unchanged" line and lists only channels whose place in
the order actually changed.

**Reorder the groups** — reorder `group_order`. The numbers follow.

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
or the route 404s. Proxied so far: `2m-monde`, `military-history`, `press-tv`, `music-box`, `tv3-ghana`.

**Not every channel can be proxied — check before promising one.** Two things make an
origin unproxyable, both met on 2026-10-12:

- **It sits behind Cloudflare.** A Worker subrequest to a Cloudflare-protected origin gets
  "Attention Required!" and a 403 however often it is asked. `AXN CEE Romania`
  (`cronos.mangora1.cfd`).
- **It issues an IP-bound session.** `Universal Comedy` and `Universal Crime`
  (`bantel-cdn1.iptvperu.tv`, Nimble) return a `?nimblesessionid=` on the master that only
  works from the IP that asked for it — minted on the LAN it serves 200 there and 403 from
  the Worker — and the variant and every segment need it. Only the master checks the
  User-Agent, so the box cannot fetch the master itself and the Worker cannot hand its
  session to the box. Nothing can sit in the middle.

Those three stay direct with their `#EXTVLCOPT`: fine in Kodi and OTT, knowingly broken in
XCIPTV. Both 502s name their own cause now, which is what the diagnostic text in
`headerProxy` and `snrt` is for — read the body before theorising.

Test before trusting a new `/h/` route: fetch master → variant → first segment, not just the
master. Both of these failed only at the variant.

**The SNRT fallback.** When the CDN block-pages this Worker for an event (arryadia-tnt), the
master is synthesised from the fixed 480/360/240 ladder and its variants point at
`/snrt/<slug>/v/<h>p.m3u8`, which answers with a **302** to the CDN carrying a token minted at
that moment. The redirect costs no subrequest — which is the whole point, since the CDN will
not answer this Worker — and because the player re-reads the media playlist every few seconds,
the token renews itself instead of dying an hour in. Your box follows the redirect; it is not
blocked. Nothing to configure: the normal proxied path is used whenever it works.

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
- **Is it the decoder, not the network?** 2M is MPEG-2; Android phones have no
  MPEG-2 decoder and play it audio-only. Switching XCIPTV's engine to VLC works around it.

### Known: 2M does not play on the U8K

Measured 2026-10-12. The box reaches `185.9.2.18` by ICMP (428 ms, 33% loss) but gets zero
bytes over TCP 80, while the laptop on the same LAN gets HTTP 200 in ~1 s at the same
moment, and the box reaches `1.1.1.1` and the Worker's Cloudflare IP (12.9 ms, 0% loss)
normally. No VPN is active on the box and the route is the normal default. That points at
per-device filtering on the router, not at the stream. Unresolved.

## 7. The health check that keeps this playlist alive

`watch_playlist.py` is the one that matters for the TVs, because they read **this** playlist
and nobody at the house can diagnose a dead channel. Register it once:

```powershell
cd C:\Users\aatiq\MINIX-U8K\tv-playlist
.\register-watch-task.ps1          # daily 05:30 and 13:30, plus at logon
python -I watch_playlist.py --dry-run   # see what it would do, change nothing
```

Each run tests every channel the way a player does, remembers what failed, rebuilds without
whatever has failed **three runs in a row**, and pushes. XCIPTV re-reads the list on launch
once its copy is more than about six hours old, so a repair reaches the TV without anyone
touching it.

It refuses to act when acting would be worse than waiting:

| Guard | Why |
|---|---|
| more than 40 % failing -> abort | that is this connection, not the channels |
| three consecutive failures before removal | one bad reading is weather, not death |
| more than 40 removals at once -> abort | wants a human; `--force` overrides |
| `verify_playlist.py` must pass | never commit a playlist that does not hold together |
| a channel returns on its first good reading | outages are usually temporary |

Its findings live in `overrides/auto_dead.json`, **not** `overrides.json` - that stays a
human document. It probes each channel with that channel's own `#EXTVLCOPT` headers, so a
channel that needs a Referer is not mistaken for a dead one and deleted.

Channels it removed come back by themselves. To force one back now, delete its entry from
`auto_dead.json` and rebuild.

**It only publishes when the dead set changes.** A run where nothing newly died and nothing
came back stops at "nothing to publish" — so a fix to the *build* (as opposed to a channel
dying) does not reach the playlist by itself. Apply it locally and push.

**A channel in the `add` list was immune to it until 2026-10-13.** `apply_overrides.py`
removes before it adds, so a dead channel that the overrides put there was dropped by the
removal pass and re-added by the addition pass in the same run. The health check reported
it gone, the dead set recorded it, and the channel stayed in the playlist — permanently,
because the set never changed again. It cost four channels (Abdulmajeed Abdullah 1210,
Mohammed Abdo 1235, Rashid AlMajed 1239, Al Ghad TV 1260: declared dead on 2026-10-13 and
still published after the run that said "15 gone"). The addition pass now holds back
anything in `auto_dead.json`, so `add` and the health check agree.

**Nothing ever came back until 2026-10-28.** The check tested only what was in
`playlist.m3u`, and a dead channel is by definition not in it, so no channel was ever
probed again once removed - 26 runs, 72 channels out, none back. Each run now also builds
the list with the dead kept in (`apply_overrides.py --ignore-dead`), tests those at the
URLs the sources give today, and restores any that answer. A returning channel takes its
old place in its group and everything below it moves down one number again. The first run
after this change re-tests all 72 at once.

### Where it runs, and why there

On the **router** (GL.iNet Flint 2, OpenWrt 25.12.5, `ssh root@192.168.2.1 -p 2222`), not
on the laptop and not in CI. Where you test from decides what looks alive: SNRT's CDN serves
this LAN a 200 and hands Cloudflare a 403 block page for the same channel, and 2M lives on a
Moroccan host. A GitHub Actions runner in a datacentre would call a chunk of the Maroc group
dead and, after three runs, delete channels that work perfectly at the TVs. The router is the
boxes' own network and public IP, and unlike the laptop it is always on.

```sh
ssh root@192.168.2.1 -p 2222          # note the port; 22 is refused
cat /root/tv-playlist-check.sh        # the cron wrapper (pull, then run)
tail -40 /root/tv-playlist/logs/watch-*.log
```

Set up there with `apk add python3 git-http ca-bundle` (OpenWrt 25.x uses **apk**, not opkg)
and a clone in `/root/tv-playlist`. Tunables come from the environment, so the checkout stays
byte-identical to this one and `git pull` never fights a local edit:
`TVWATCH_WORKERS=6` because the box is carrying the household's traffic - measured at load
average 0.45 on four cores during a full run.

**Do not schedule it at 04:00 or 05:00**: that router reboots itself at 04:00 and runs
`apk update` at 05:00, both already in root's crontab.

**Only one machine may hold the push.** If the router is doing it, disable the laptop task
(`Disable-ScheduledTask -TaskName 'tv-playlist - channel health check'`) or the two will race
and collide on push.  The router holds it as of 2026-10-13 and the laptop task is **disabled**.

The push credential is a fine-grained GitHub token (that repo's Contents: read and write, and
nothing else) in `/root/.git-credentials`, mode 600, with `credential.helper store`. To replace
it, run this from PowerShell - it prompts on the router, so the token never reaches a command
line, a shell history or a screenshot:

```sh
ssh -t root@192.168.2.1 -p 2222 'stty -echo; printf "Paste token then Enter: "; read T; stty echo; echo; echo "https://oaatiq:$T@github.com" > /root/.git-credentials; chmod 600 /root/.git-credentials; echo STORED'
```

**Use `echo`, not `printf ...
`.** A backslash-n does not survive the trip from PowerShell
through ssh to the router's shell: it arrives as a literal `n`, the file ends `@github.comn`,
and git then fails with `could not read Username for 'https://github.com'` - which looks like a
missing credential rather than a malformed one. Check with
`grep -c '@github\.com$' /root/.git-credentials`, which prints 1 when the line is right.

## 7a. The programme guide (`guide_world.py`, `guide_maroc.py`)

The guide is built **on the router**, every three hours, and published as `epg.xml.gz` on the
`guide` branch of this repo, which the playlist header's `url-tvg` points at:
`https://raw.githubusercontent.com/oaatiq/tv-playlist/guide/epg.xml.gz`. It is on a branch of
its own because the file is 1.6 MB and different on every run: on `main` it would add some
400 MB of history a month. The branch is a single commit with no parent, force-pushed each
time, so nothing piles up on GitHub or on the router. Nothing else is pushed from there, and
nothing is pushed to `main`.

`router/tv-guide-update.sh` runs, in order:

1. `git pull --ff-only` on the clone: the scripts, and `sources/world_fr_en.m3u`, the list the
   playlist is built from. The clone is only read.
2. `guide_world.py`: the world channels, from Samsung TV Plus and EPGShare, with the ids
   rewritten to the playlist's tvg-ids. It is the laptop's `playlists/autocheck/epg.py` (same
   sources, same matching; checked on the same inputs it gives the same 600 channels and
   32,660 programmes, identical down to each start, stop and title) but read as a stream:
   `epg.py` holds every source guide whole in memory, about 600 MB, which a router with 1 GB
   and no swap cannot spare. A channel it matched replaces what the guide had; every other
   channel is carried over (the Maroc channels, and any whose source failed that night), so a
   bad night costs nothing. Programmes that ended over 12 hours ago are dropped.
3. `guide_maroc.py`: the Maroc channels, below.
4. Publish, only if the file changed.

The last guide is kept in `/root/tv-guide/` between runs (`published` there holds the md5 of
the one on GitHub). A run takes about 100 s of one core at the lowest priority; the largest
process peaked at 200 MB (measured 2026-10-28, 767 MB free). The downloads, about 30 MB, go to
`/tmp`, which is RAM, and are removed afterwards.

The guide covers the 569 or so channels some source has a schedule for, out of 1,354 ids. A
channel added to the playlist gets a guide once `sources/world_fr_en.m3u` has it
(`.\rebuild.ps1 -PullFromBox`, then push). The laptop's `epg.py` still builds `world_epg.xml.gz`
for Kodi, but it no longer feeds this repo.

Every three hours because the Samsung TV Plus listings (about 340 channels) only reach some five
hours ahead; the EPGShare ones reach days. Set up once:

```sh
ssh root@192.168.2.1 -p 2222
cd /root/tv-playlist && git pull
sh router/tv-guide-update.sh && tail -30 logs/guide-*.log   # one run by hand
crontab -e                                                  # add the line below
echo root >> /etc/crontabs/cron.update                      # crond re-reads it
```

```
15 3 * * * /bin/sh /root/tv-playlist/router/tv-guide-update.sh
45 5,8,11,14,17,20,23 * * * /bin/sh /root/tv-playlist/router/tv-guide-update.sh
```

Nothing runs between 03:45 and 05:30 (the 04:00 reboot, the 05:00 `apk update`), and no run
overlaps the health check's, at 02:30 and 14:30 (6-8 minutes). A source that cannot be read leaves its channels as they were; if no source can,
or the push is refused, nothing is published and the last guide stays up. The log is
`logs/guide-YYYYMMDD.log` in the clone, a month of them. To see what is published:

```sh
curl -sL https://raw.githubusercontent.com/oaatiq/tv-playlist/guide/epg.xml.gz | gzip -dc | head -c 600
```

`epg.xml.gz` is still on `main`, no longer updated, for any player that has not re-read the
playlist since 2026-10-28. Delete it when none does.

### The Maroc channels (`guide_maroc.py`)

The world guide has no Moroccan channel in it, so on its own the Maroc group has no guide.
`guide_maroc.py` fetches the schedules that exist and merges them in, under the ids the
playlist already uses, so every player picks them up with nothing to set:

| Channel | tvg-id(s) in the guide | Source |
|---|---|---|
| Al Aoula, Laâyoune, Arryadia, Athaqafia, Assadissa, Tamazight | as in the playlist | snrt.ma (Arabic titles) |
| 2M Monde | `2MMonde.ma`, and `2MMonde.ma@Plus1` one hour later for the +1h feed | SFR, else Orange |
| Medi1 Afrique | `Medi1TVAfrique.ma`, `Medi1TVAfrique.ma@SD` | Orange, if it has a real schedule (SFR only has a placeholder); `--dry-run` says |

Not covered, because no source has them: 2M (the terrestrial feed), Arryadia HD1 and TNT, Télé
Maroc, Chada, StoryChannel, and Medi1 Maghreb and Arabic. Their tvg-ids are set, so a source
found later only needs a line in `CHANNELS`.

It replaces only the channels it fetched; one that no source answered for keeps what the file
had. A source whose schedule is one title repeated all day (SFR's "Chaîne nationale
marocaine") is skipped: that would show as *what is on now*, which is worse than nothing.
Morocco has been on plain UTC since 2026-09-20 (no +1, no Ramadan change), so the schedules'
local times are read as UTC and no tz database is needed. It runs here and not on the laptop
because snrt.ma answers this network. As of 2026-10-28 only 2M Monde comes back: snrt.ma
returns 503 (maintenance) and Orange 451.

## 8. The Kodi re-check (`recheck.py`)


**This one maintains Kodi's lists, which nothing reads any more** - the boxes run XCIPTV.
It is left running because it also keeps `master.m3u` honest, and that is where the world
channels come from on the next rebuild.

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

## 9. Recovery

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
