#!/bin/sh
# Health check for the published playlist that the TV boxes read.
#
# Runs here, on the router, because this is the boxes' own network and public IP: a channel
# geo-locked to Morocco answers here exactly as it answers them, and would look dead from a
# datacentre - which would make this job delete working channels. See section 7 of
# /root/tv-playlist/RUNBOOK.md.
#
# Not at 04:00 or 05:00: this router reboots at 04:00 and runs apk update at 05:00, both
# already in root's crontab. Fewer threads than the laptop default, because this box is
# carrying the household's traffic (measured: load average 0.45 on four cores).
#
# MODE is --dry-run until a push credential exists. With a token in /root/.git-credentials,
# drop the flag and it repairs the playlist by itself; XCIPTV re-reads the list on launch
# once its copy is ~6 h old, so the fix reaches the TVs with nobody touching anything.
MODE="--dry-run"

cd /root/tv-playlist || exit 1
git pull -q --ff-only 2>/dev/null

TVWATCH_WORKERS=6 \
TVWATCH_RETRY_PAUSE=90 \
TVWATCH_TIMEOUT=20 \
python3 -I watch_playlist.py $MODE

# Keep a month of logs, no more: this is a router, not a log server.
find /root/tv-playlist/logs -name 'watch-*.log' -mtime +30 -delete 2>/dev/null
