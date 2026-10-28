#!/bin/sh
# Programme guide for the Maroc group: fetches the Moroccan channels' schedules, merges them
# into epg.xml.gz and pushes (see guide_maroc.py and section 7a of RUNBOOK.md).
#
# Runs on the router for the same reason the health check does: snrt.ma answers this
# network, and it is always on. It runs from the clone itself, so a `git pull` brings its
# updates too. It shares that clone with the health check, so the two must not overlap:
# a lock here, and crontab times well apart from each other (and from the 04:00 reboot and
# the 05:00 apk update).
#
#   20 6,18 * * * /bin/sh /root/tv-playlist/router/tv-guide-update.sh

DIR=${TVPLAYLIST_DIR:-/root/tv-playlist}
LOCK=/tmp/tv-playlist.lock
# A run takes minutes. A lock older than three hours was left by one that was killed (a power
# cut clears /tmp anyway): take it over rather than stop running for good.
find "$LOCK" -maxdepth 0 -mmin +180 -exec rmdir {} \; 2>/dev/null
mkdir "$LOCK" 2>/dev/null || { echo "another tv-playlist job is running" >&2; exit 0; }
trap 'rmdir "$LOCK"' EXIT

cd "$DIR" || exit 1
mkdir -p logs
LOG=logs/guide-$(date +%Y%m%d).log
{
    git pull -q --ff-only || { echo "git pull failed: the clone has diverged, fix by hand"; exit 1; }
    python3 -I guide_maroc.py
} >>"$LOG" 2>&1

# A month of logs, no more.
find "$DIR/logs" -name 'guide-*.log' -mtime +30 -delete 2>/dev/null
