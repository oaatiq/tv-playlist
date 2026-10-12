# sources

The two Kodi IPTV Simple playlists `build_tivimate.py` reads, plus a copy of the build
script. Snapshots, committed so they are not one failed SD card away from gone.

| File | What it is | Authoritative copy |
|---|---|---|
| `direct.m3u` | the curated channels, 1–48 | the box, `.../addon_data/pvr.iptvsimple/direct.m3u` |
| `world_fr_en.m3u` | the numbered World FR/EN list | the box, same directory — written by `playlists/autocheck/recheck.py` |
| `build_tivimate.py` | the generator | `MINIX-U8K/playlists/tivimate/build_tivimate.py` on the laptop |

These are **snapshots, not the originals**. Editing them here changes nothing on the box.

- `direct.m3u` is hand-curated. Edit it on the box.
- `world_fr_en.m3u` is generated. `recheck.py` runs from a Windows scheduled task every two
  hours, re-checks the whole list weekly, drops dead channels, re-resolves Samsung TV Plus
  URLs, and pushes the result to the box. Edits here and on the box are both temporary.
- `build_tivimate.py` is a copy for reference and recovery. `rebuild.ps1` runs the laptop
  copy, not this one.

Refresh the two lists from the box with:

    .\rebuild.ps1 -PullFromBox

Pulled with `adb exec-out cat` rather than `adb pull`, because the Kodi data directory is
not world-readable on Android 11 and `pull` fails on it.
