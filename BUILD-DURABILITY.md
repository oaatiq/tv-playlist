# Build durability: why the playlist used to be overwritten, and what fixes it

**Status:** fixed, 2026-10-12. Option C from the original plan.
**Read this when:** you are about to change how `playlist.m3u` is produced, or a rebuild
surprises you.

`playlist.m3u` is generated, but about 150 channels' worth of content, grouping and
numbering never existed in the build's inputs — it was edited straight into the file. A
rebuild used to discard all of it, silently and without a conflict. It no longer can:
those edits now live in `overrides/overrides.json`, and `rebuild.ps1` re-applies them
after every build.

---

## 1. The pipeline

```
          hand-curated on the box                generated on the laptop
        ┌──────────────────────────┐      ┌──────────────────────────────────┐
        │ box: direct.m3u  (1–48)  │      │ autocheck/master.m3u             │
        └────────────┬─────────────┘      │   │ recheck.py, scheduled task   │
                     │                    │   │ every 2 h, full re-check     │
                     │                    │   │ weekly                       │
                     │                    │   ▼                              │
                     │                    │ autocheck/live.m3u ──adb push──┐ │
                     │                    └────────────────────────────────┼─┘
                     │                                                     ▼
                     │                              box: world_fr_en.m3u (1001–3744)
                     │                                                     │
                     └──────────────────┬──────────────────────────────────┘
                                        ▼
                              sources/*.m3u   (committed snapshots)
                                        │
                                        ▼  build_tivimate.py          ← rebuild.ps1
                              .rebuild/playlist.m3u   (1,386 ch, 9 groups)
                                        │
                                        ▼  apply_overrides.py         ← rebuild.ps1
                                        │  + overrides/overrides.json
                              .rebuild/playlist.m3u   (1,431 ch, 16 groups)
                                        │
                                        ▼  verify_playlist.py         ← rebuild.ps1
                                        ▼
                                   playlist.m3u ──git push──► raw.githubusercontent
                                                                      │
                                                                      ▼
                                                            XCIPTV on the box
```

Two things worth knowing:

- **The source lists drift on their own.** `recheck.py` runs from a Windows scheduled task
  (`playlists/autocheck/register-task.ps1`) every two hours, re-checks every channel weekly,
  drops dead ones, re-resolves Samsung TV Plus URLs and pushes the result to the box. So a
  rebuild months from now will not see the same world list.
- **Nothing automated touches this repo.** `recheck.py` never runs the build and never
  touches `playlist.m3u`, so the overwrite only ever happened by hand. That is still the
  only way it can happen — and `rebuild.ps1` is now the thing to run by hand.

The Worker (`worker/`) is independent of all of this.

---

## 2. What the overrides hold

`overrides/overrides.json`, applied in this order by `apply_overrides.py`:

| Key | Effect | Size |
|---|---|---|
| `patch_attrs` | fills in `tvg-id` / `tvg-logo` the build leaves blank — the 7 Al Jazeera entries, which were the only ones in the playlist with neither, so XCIPTV showed no logo | 7 entries, 13 attributes |
| `regroup` | splits the build's one `International` group into Infos Monde / Sport / Musique & Jeunesse / Art de vivre, keyed on `tvg-id` | 54 channels |
| `remove` | drops any entry whose `tvg-id` carries a `.ru` or `.by` country code (6), plus 19 named `tvg-id`s — 16 religious broadcasters, NBT World, Rai Italia, TV BRICS Africa | 25 channels |
| `add` | the 70 Arab-world channels, as full entries: Arabie Saoudite 33, Égypte 9, Émirats 19, Qatar 9. Sourced from iptv-org's Arabic list, each probed live | 70 channels |
| `group_order` | the order the 16 group blocks are written in | 16 groups |

Net effect: **1,386 → 1,431 channels, 9 → 16 groups.**

**Numbering is not an override.** After everything above, the list is written one group
at a time in `group_order` — inside a group, the build's channels in the order the build
emitted them, then that group's `add` records in list order — and numbered 1..N from the
top. Until 2026-10-28 it kept the source lists' numbers plus a per-group base (`renumber`),
which left Maroc running 1–16, 42, 1252, 1421 and Al Jazeera restarting at 17. The order
on screen did not change when that was replaced; only the numbers did.

### Things it is careful about

- **It never touches a stream URL or an `#EXTVLCOPT` line.** Only `tvg-chno`,
  `group-title`, `tvg-id`, `tvg-logo`, which entries exist, and the order they are written
  in. Nothing that decides how a channel plays.
- **It is idempotent.** Running it twice is a no-op: additions are skipped when the URL is
  already present, and the order it writes is the order it reads, so the numbers come out
  the same. That is also why the order comes from arrival, not from the incoming numbers:
  it runs on fresh builds (source numbers) and on `playlist.m3u` itself (already 1..N).
- **It never silently drops a channel.** If the world list grows a group that
  `group_order` does not mention, those channels are written at the end and reported, not
  discarded.
- **It reports staleness.** An override that matched nothing — a removal for a channel that
  is already gone, a `tvg-id` that no longer exists — is printed as a warning.

No number is pinned. A channel removed — by an override, by upstream or by the health
check — shifts everything below it up by one, and one that returns shifts them back. Nothing
keys on the number (the guide and the health check use `tvg-id`), so that is safe.
`verify_playlist.py --compare` counts those shifts in one line and lists only channels
whose place in the order changed.

---

## 3. How the build behaves (the questions the original handoff left open)

Answered by reading `build_tivimate.py` (snapshot in `sources/`) and `playlists/autocheck/recheck.py`.

**Q1 — where does `group-title` come from?** Both, depending on the list.
*Curated* (`direct.m3u`): copied verbatim, except that anything already in `Maroc` or
carrying a `.ma` `tvg-id` is forced into `Maroc`. *World* (`world_fr_en.m3u`): **computed**
by `world_group()` from IPTV Simple's `Lang;Country;Genre` syntax — with `--grouping
country` it takes the first field that is not a language, falling back to `Other countries`
when there is none. So a literal `group-title="Arabie Saoudite"` in the world list would
have survived, but writing the four new groups into the sources was never the plan anyway.

**Q2 — where does `tvg-chno` come from?** In the build output, read from the source lists
(`apply_overrides.py` then replaces it with the position in the final list). The world list is
authoritative and the attribute is required there (hence the README's note that the
`playlists/` copies will not build). In the curated list it is read when present, and
otherwise continues from the last entry that had one.

**Q3 — what decides group order in the output?** The `Maroc` block is written first,
sorted by channel number; everything else follows in source-file order, unsorted. That is
why the build emits `Other countries` in three separate blocks. `apply_overrides.py`
replaces this with one contiguous block per group in `group_order`.

**Q4 — what decides `Other countries`?** Nothing in `build_tivimate.py` — no hardcoded
list, no threshold. It is whatever `recheck.py` wrote into the world list's `group-title`;
it collapsed every country except France, International, United Kingdom and United States
into `Other countries` on 2026-10-11, deleting 266 channels in the process.

**Q5 — does the build also regenerate `epg.xml.gz`?** No. It writes the `.m3u` and nothing
else. The guide comes from `playlists/autocheck/epg.py`, which `recheck.py` runs
separately.

**Q6 — where does `build_tivimate.py` live, and is it version-controlled?**
`C:\Users\aatiq\MINIX-U8K\playlists\tivimate\build_tivimate.py`. `MINIX-U8K` is not a git
repository, so it was version-controlled nowhere. There is now a snapshot at
`sources/build_tivimate.py` — a copy for recovery, not the authoritative one.
`rebuild.ps1` runs the laptop copy.

**Q7 — how is the box reached?** adb over LAN, `192.168.2.131:5555`. Note that `adb pull`
**fails** on the Kodi data directory, which is not world-readable on Android 11;
`adb exec-out cat <path>` works and is what `rebuild.ps1 -PullFromBox` uses.

---

## 4. Why this is trustworthy

`apply_overrides.py` applied to the last pure build output (`44b4a44:playlist.m3u`, 1,386
channels) reproduces the hand-edited playlist (`0a798e6:playlist.m3u`, 1,431 channels)
**byte for byte** — sha256 `8a1da688…`, 438,014 bytes. Running it a second time changes
nothing.

Rebuilt from the box's live lists on 2026-10-12, the result was structurally identical —
same 16 groups, same counts, same channel-number ranges, no duplicate numbers, no
warnings — and differed from the committed playlist in exactly **one** entry: CNN (UK,
1475), whose Samsung TV Plus URL `recheck.py` had re-resolved that afternoon. Upstream
drift, correctly picked up.

Re-run that check any time:

```powershell
.\rebuild.ps1 -WhatIf
```

Hard failures (duplicate channel numbers, a group split across the file, group order that
contradicts the numbering, CRLF, a BOM) abort before `playlist.m3u` is touched.

### Two corrections to the original handoff

- It said **369** channels were renumbered. The real figure is **294** surviving channels
  (299 renumbering operations, 5 of which land on entries that are then removed).
- It listed **KTV ×6** among the added Arab channels. No Kuwaiti channels were added —
  there is no Kuwait group. `KTV Sport Plus` is in the "available to add" list below.

---

## 5. If you would rather not keep two steps

Option D from the original plan — teaching `build_tivimate.py` to read
`overrides/overrides.json` itself — is still the cleaner end state, and is now a much
smaller job: the override data is settled and proven, so it is a matter of moving where it
is applied. The reason not to is unchanged: that script is shared with other outputs and
lives outside this repo, so a bug there breaks more than this playlist.

---

## 6. Open items, unrelated to the build

- **2M** is MPEG-2 video, confirmed by parsing its transport stream (PMT
  reports stream type 0x02 video + 0x04 audio). Android phones have no MPEG-2 decoder, so
  it plays audio-only there; the Amlogic box probably does have one. Untested on the box.
  If it fails there too, switch XCIPTV's player engine to VLC, which bundles a software
  MPEG-2 decoder.
- **`epg.xml.gz` has no Arab-world coverage.** 769 channel ids, led by `us` (179), `uk`
  (34), `ca` (28), `fr` (26). `AlAoula.ma` and `2M.ma` are both absent, so the Maroc group
  and all four new Arab groups have no guide data at all.
- **13 entries still have no `tvg-logo`** (12 United States, 1 Other countries), and four
  Maroc entries have no `tvg-id` (Arryadia TNT, Medi1 ×3).
- **About 45 religious channels** remain scattered across United States, Other countries,
  United Kingdom and France.
- **65 further channels** were probed live and are available to add from iptv-org: Series
  23, Sports 17, Kids 11, Movies 11, Animation 2. The ones that fit the existing Arab
  groups are MBC Drama USA, Al Iraqia Sport, KTV Sport Plus and Taha TV. Add them to
  `overrides/overrides.json` under `add` (or with `add_channel.py`).
