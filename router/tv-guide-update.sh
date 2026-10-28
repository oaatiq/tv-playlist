#!/bin/sh
# Programme guide for the whole playlist: builds the world channels' guide (guide_world.py),
# merges the Maroc channels' schedules into it (guide_maroc.py) and publishes the result as
# epg.xml.gz on the `guide` branch of the repo (see section 7a of RUNBOOK.md).
#
# Runs on the router for the same reason the health check does: snrt.ma answers this
# network, and it is always on. The guide goes to a branch of its own, one commit that is
# force-pushed every time, because the file is 1.6 MB and different on every run: on main it
# would add some 400 MB a month of history. Nothing else is ever pushed from here, and nothing
# is ever pushed to main.
#
# The last guide is kept in $STATE between runs (guide_maroc.py carries the Maroc channels
# over from it). The main clone is only read, and updated with a fast-forward pull so that a
# `git pull` brings these scripts' updates too. A lock keeps two runs from overlapping.
#
# Every three hours or so, because the Samsung TV Plus listings reach only about five hours
# ahead. Nothing between 03:45 and 05:30: this router reboots at 04:00 and runs apk update at
# 05:00. The health check runs at 02:30 and 14:30 and takes 6-8 minutes.
#
#   15 3 * * * /bin/sh /root/tv-playlist/router/tv-guide-update.sh
#   45 5,8,11,14,17,20,23 * * * /bin/sh /root/tv-playlist/router/tv-guide-update.sh

DIR=${TVPLAYLIST_DIR:-/root/tv-playlist}
STATE=${TVGUIDE_STATE:-/root/tv-guide}
LOCK=/tmp/tv-playlist.lock
# A run takes minutes. A lock older than three hours was left by one that was killed (a power
# cut clears /tmp anyway): take it over rather than stop running for good.
find "$LOCK" -maxdepth 0 -mmin +180 -exec rmdir {} \; 2>/dev/null
mkdir "$LOCK" 2>/dev/null || { echo "another tv-playlist job is running" >&2; exit 0; }
trap 'rmdir "$LOCK"' EXIT

cd "$DIR" || exit 1
mkdir -p logs "$STATE"
LOG=logs/guide-$(date +%Y%m%d).log

# One commit with no parent, force-pushed over the branch. A fresh repository each time, so
# nothing piles up here either.
publish() {
    sum=$(md5sum "$STATE/epg.xml.gz" | cut -d' ' -f1)
    if [ "$sum" = "$(cat "$STATE/published" 2>/dev/null)" ]; then
        echo "nothing new to publish"
        return 0
    fi
    url=$(git -C "$DIR" remote get-url origin) || return 1
    work=$(mktemp -d /tmp/tv-guide-push.XXXXXX) || return 1
    cp "$STATE/epg.xml.gz" "$work/" &&
        git -C "$work" init -q -b guide &&
        git -C "$work" add epg.xml.gz &&
        git -C "$work" commit -q -m "Guide $(date -u +%Y-%m-%dT%H:%MZ)" &&
        git -C "$work" push -q --force "$url" guide:guide
    rc=$?
    rm -rf "$work"
    if [ $rc -eq 0 ]; then
        echo "$sum" >"$STATE/published"
        echo "published ($(wc -c <"$STATE/epg.xml.gz") bytes)"
    else
        echo "push failed: the last published guide stays as it was"
    fi
    return $rc
}

{
    git pull -q --ff-only || { echo "git pull failed: the clone has diverged, fix by hand"; exit 1; }
    # A source that fails leaves the world part as it was; one that is unreadable stops here.
    nice -n 19 python3 -I guide_world.py sources/world_fr_en.m3u "$STATE/epg.xml.gz" \
        --keep "$STATE/epg.xml.gz" || { echo "guide_world.py failed: nothing published"; exit 1; }
    nice -n 19 python3 -I guide_maroc.py "$STATE/epg.xml.gz" || echo "guide_maroc.py failed: Maroc as it was"
    publish
} >>"$LOG" 2>&1

# A month of logs, no more.
find "$DIR/logs" -name 'guide-*.log' -mtime +30 -exec rm -f {} \; 2>/dev/null
