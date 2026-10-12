# Build durability: why the playlist will be overwritten, and how to fix it

**Status:** open problem. Nothing here has been done yet.
**Written:** 2026-10-12, against `main` @ `da7a8cf`.
**Read this when:** you are at home, the MINIX U8K is reachable over LAN, and you can see both `build_tivimate.py` and the two source lists on the box.

This file is a handoff to a fresh Claude session. It assumes no prior context.

---

## 1. The setup

| Thing | Where |
|---|---|
| This repo | `github.com/oaatiq/tv-playlist`, branch `main` |
| Playlist consumed by the player | `https://raw.githubusercontent.com/oaatiq/tv-playlist/main/playlist.m3u` |
| Guide | `epg.xml.gz` in this repo, referenced from the playlist header |
| Cloudflare Worker | `https://ma-tv.oaatiq.workers.dev` — source in `worker/src/index.js`, deploy with `cd worker && npx wrangler deploy` |
| Player | XCIPTV on a MINIX U8K Android TV box (also tested on an Android phone) |

The Worker exists because SNRT's EasyBroadcast streams need an hourly token and Télé Maroc's signed link expires after about 12 hours, so static URLs die. It also hosts `/h/` routes that attach a `Referer`/`User-Agent` for four channels, because XCIPTV ignores `#EXTVLCOPT`. **None of this is affected by the problem below** — the Worker is independent of the build.

### The build (per this repo's README)

`playlist.m3u` is **generated**, not hand-written:

```
playlists/tivimate/build_tivimate.py \
  --out tv-playlist --out-name playlist.m3u \
  --grouping country \
  --maroc-worker https://ma-tv.oaatiq.workers.dev \
  --epg-url https://raw.githubusercontent.com/oaatiq/tv-playlist/main/epg.xml.gz
```

Its two inputs live **on the box**, not in this repo, under `/storage/emulated/0/Android/data/org.xbmc.kodi/files/.kodi/userdata/addon_data/pvr.iptvsimple/`:

- `direct.m3u` — passed as `--direct`, the curated channels 1–48
- `world_fr_en.m3u` — passed as `--world`, the numbered world list

The README notes that the copies under `playlists/` have no `tvg-chno` and will not build.

---

## 2. The constraint

On 2026-10-12 a large set of changes was committed **directly to `playlist.m3u`** and merged to `main`. The two source lists on the box were **not** touched.

So the next time `build_tivimate.py` runs, it regenerates `playlist.m3u` from `direct.m3u` + `world_fr_en.m3u` and **silently discards every change below.** There is no warning and no conflict — the file is simply rewritten.

This is not hypothetical any more: the changes are live on `main` and the box is serving them.

---

## 3. What exactly gets lost

Nine commits, `56db85c..da7a8cf`. Net effect **1,386 → 1,431 channels**.

1. **70 Arab-world channels added** in four new groups, sourced from iptv-org's Arabic list and each probed live: Arabie Saoudite (33), Égypte (9), Émirats (19), Qatar (9). Includes MBC 1/4/5/Drama/Mood, MBC Masr 1/2, Al Ekhbariya, Al Saudiya, SBC, Asharq x3, Qatar TV 1/2, Al Rayyan, Al Araby, KTV x6.
2. **369 channels renumbered** so that every group's lowest `tvg-chno` ascends in the same order the groups appear in the file. This makes the category order hold whether the player sorts by file position or by channel number. Infos FR moved 24–41 to 1180–1197, Infos Arabe 43–48 to 1200–1205, Other countries to 3750–4024.
3. **`International` (73) split into four groups** — Infos Monde (22), Sport (10), Musique & Jeunesse (16), Art de vivre (6) — and **19 removed** (16 religious broadcasters, plus NBT World, Rai Italia, TV BRICS Africa).
4. **6 Russian/Belarusian channels removed** — every entry whose `tvg-id` carried a `.ru` or `.by` country code.
5. **7 Al Jazeera entries given `tvg-logo` and `tvg-id`** — they were the only entries in the playlist with neither, so XCIPTV showed no logo.
6. **Group order set** to the 16 groups in section 5's table.

---

## 4. What is NOT known yet — investigate first

These could not be answered without the box. **Answer them before choosing a solution; the right fix depends entirely on Q1.**

- **Q1. Where does `group-title` come from?** Does the script compute it from a country code or internal mapping, or copy it from the source entry? If it computes it, writing `group-title="Arabie Saoudite"` into `direct.m3u` will be ignored and those channels will land in `Other countries`.
- **Q2. Where does `tvg-chno` come from?** Read verbatim from the source lists, or assigned by the script? The README implies the former (the `playlists/` copies "will not build" without `tvg-chno`), but confirm.
- **Q3. What decides group *order* in the output file?**
- **Q4. What decides which countries get their own group vs the `Other countries` catch-all** — a hardcoded list, or a channel-count threshold?
- **Q5. Does the same script also regenerate `epg.xml.gz`,** or is that a separate process?
- **Q6. Where does `build_tivimate.py` actually live** (the README path is relative to an unknown root), and is it version-controlled anywhere?
- **Q7. How is the box reached** — adb over LAN, SMB, FTP, a file manager?

---

## 5. Verification baseline

Current, correct state of `main` @ `da7a8cf` (`playlist.m3u`, 438,014 bytes, sha256 starts `8a1da688`):

| # | Group | Count | chno range |
|---|---|---|---|
| 1 | Maroc | 21 | 1–1422 |
| 2 | Al Jazeera | 7 | 17–23 |
| 3 | France | 175 | 1001–1176 |
| 4 | Infos FR | 18 | 1180–1197 |
| 5 | Infos Arabe | 6 | 1200–1205 |
| 6 | Arabie Saoudite | 33 | 1210–1242 |
| 7 | Égypte | 9 | 1260–1268 |
| 8 | Émirats | 19 | 1270–1288 |
| 9 | Qatar | 9 | 1300–1308 |
| 10 | United Kingdom | 232 | 1471–1702 |
| 11 | United States | 578 | 1811–2392 |
| 12 | Infos Monde | 22 | 3671–3692 |
| 13 | Sport | 10 | 3693–3702 |
| 14 | Musique & Jeunesse | 16 | 3703–3718 |
| 15 | Art de vivre | 6 | 3719–3725 |
| 16 | Other countries | 270 | 3750–4024 |

**Total: 1,431.** Group order must ascend by minimum `tvg-chno`. No duplicate `tvg-chno`. File is UTF-8, no BOM, LF line endings (see `.gitattributes`).

Run this after any rebuild. It must print the table above, `order consistent: True` and `dup chno: none`:

```python
import re
from collections import OrderedDict, Counter

txt = open('playlist.m3u', encoding='utf-8').read()
groups = OrderedDict()
for line in txt.split('\n'):
    if line.startswith('#EXTINF'):
        g = re.search(r'group-title="([^"]*)"', line).group(1)
        c = int(re.search(r'tvg-chno="(\d+)"', line).group(1))
        groups.setdefault(g, []).append(c)

prev, ok = -1, True
for i, (name, chnos) in enumerate(groups.items(), 1):
    ok &= min(chnos) > prev
    prev = min(chnos)
    print(f'{i:3}. {name:20} {len(chnos):4}  {min(chnos)}-{max(chnos)}')

dups = [c for c, n in Counter(re.findall(r'tvg-chno="(\d+)"', txt)).items() if n > 1]
print('total:', txt.count('#EXTINF'), '| groups:', len(groups), '| order consistent:', ok)
print('dup chno:', dups or 'none')
```

---

## 6. Solution options

### A — Port the changes back into the source lists on the box

Add the 70 Arabic channels to `direct.m3u` / `world_fr_en.m3u` with the right `tvg-chno`, apply the renumbering and removals there, then rebuild.

*Keeps the existing workflow.* But it only works if the answers to Q1–Q4 are "read from source". If the script computes grouping and ordering, the group split, the group order and the four new groups will not survive. The source lists also stay off-repo, so this is still fragile and unbacked-up.

### B — Stop building; make this repo the source of truth

Retire `build_tivimate.py` and edit `playlist.m3u` here directly.

*Simplest, and makes the playlist versioned and reviewable.* Cost: you lose whatever else the script does — Worker URL injection, EPG URL rewriting, possibly EPG regeneration (Q5). Only safe once Q5 is answered.

### C — Keep the build, add a post-build override step (recommended)

Leave `build_tivimate.py` untouched. Add to this repo:

- `overrides/` — the 70 added channels as data, the removal list (`.ru`/`.by` ids plus the 19 International entries), the group map and group order, the renumbering plan, the Al Jazeera logo/id patches
- `apply_overrides.py` — reads the freshly built `playlist.m3u`, applies all of the above deterministically, writes it back
- a one-line wrapper that runs the build and then the overrides

*Survives rebuilds, keeps the script's behaviour, and puts every change under version control in this repo where it is diffable.* Idempotent, so it is safe to run twice. **This works regardless of how Q1–Q4 turn out**, which is why it is the recommendation — it does not depend on the unknowns.

### D — Teach `build_tivimate.py` to read config from this repo

Cleanest end state, one pass instead of two. But it means editing a script that is not version-controlled here and is shared with other outputs (the README mentions a TiviMate target), so a bug breaks more than this playlist.

**Suggested path:** C now, D later if the override file grows unwieldy.

---

## 7. Plan for the session at home

1. **Do not rebuild yet.** A rebuild before the fix destroys the current state. If it already happened, `git checkout da7a8cf -- playlist.m3u` restores it.
2. Locate `build_tivimate.py` and read it. Answer Q1–Q7 in section 4.
3. Pull `direct.m3u` and `world_fr_en.m3u` off the box and **commit copies to this repo** (for example under `sources/`). Right now they exist in exactly one place, on a TV box, with no backup. Do this even if nothing else gets done.
4. Implement option C (or A, if Q1–Q4 came back "read from source").
5. Rebuild into a scratch file, run the section 5 check, and diff against the current `playlist.m3u`. Expect **zero** differences.
6. Only then overwrite, commit, push, and run *Update Contents* on the box.

---

## 8. Open items unrelated to this problem

- **Channel 14 (2M)** is MPEG-2 video, confirmed by parsing its transport stream (PMT reports stream type 0x02 video + 0x04 audio). Android phones have no MPEG-2 decoder, so it plays audio-only there; the Amlogic box probably does have one. Untested on the box. If it fails there too, switch XCIPTV's player engine to VLC, which bundles a software MPEG-2 decoder.
- **`epg.xml.gz` has no Arab-world coverage** — 769 channel ids, led by `us` (179), `uk` (34), `ca` (28), `fr` (26). `AlAoula.ma` and `2M.ma` are both absent, so the Maroc group and all four new Arab groups have no guide data at all.
- **13 entries still have no `tvg-logo`** (12 United States, 1 Other countries), and four Maroc entries have no `tvg-id` (Arryadia TNT, Medi1 x3).
- **About 45 religious channels** remain scattered across United States, Other countries, United Kingdom and France.
- **65 further channels** were probed live and are available to add from iptv-org: Series 23, Sports 17, Kids 11, Movies 11, Animation 2. The ones that fit the existing Arab groups are MBC Drama USA, Al Iraqia Sport, KTV Sport Plus and Taha TV.
