#!/usr/bin/env python
"""Keep the published playlist honest without anyone watching it.

The boxes run XCIPTV, which reads playlist.m3u from GitHub and re-reads it on launch
whenever its copy is more than about six hours old. Nothing else maintains that file:
`playlists/autocheck/recheck.py` maintains the *Kodi* lists on the box and never touches
this repo. So a channel that dies upstream stays in the published list until a person
notices and says so - and the people watching these boxes are not in a position to
diagnose anything.

This closes that loop. It tests every channel, remembers what has been failing, rebuilds
the playlist without the ones that are reliably gone, and pushes. The TV picks the new
list up by itself the next time it is switched on.

    python -I watch_playlist.py                 # test, update, rebuild, commit, push
    python -I watch_playlist.py --dry-run       # test and report; change nothing
    python -I watch_playlist.py --no-push       # rebuild and commit, but stay local
    python -I watch_playlist.py --force         # ignore the "too many removals" guard

What it will not do, because an unattended job that is wrong is worse than no job:

  * remove a channel on one bad reading - it must fail DROP_AFTER runs in a row
  * do anything at all if more than MAX_FAIL_RATIO of the list fails at once, which
    means the internet here is down, not the channels
  * remove more than MAX_REMOVALS channels in a single run without --force
  * commit a playlist that verify_playlist.py rejects
  * touch overrides.json, which stays a human document - its findings go to
    overrides/auto_dead.json, and a channel that comes back is restored on the first
    good reading. A dead channel is no longer in playlist.m3u, so each run also builds
    the list with the dead kept in and tests those again, at their current URLs

Borrowed from recheck.py, which has been doing this for the Kodi lists for a while:
the fail-ratio abort, the second chance after a pause, and the frozen-stream check -
a live stream whose media sequence never advances is dead however well it answers.
"""

import argparse
import datetime
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLIST = os.path.join(HERE, 'playlist.m3u')
PLAYLIST8 = os.path.join(HERE, 'playlist.m3u8')
SOURCES = os.path.join(HERE, 'sources')
BUILD = os.path.join(SOURCES, 'build_tivimate.py')
OVERRIDES_DIR = os.path.join(HERE, 'overrides')
AUTO_DEAD = os.path.join(OVERRIDES_DIR, 'auto_dead.json')
STAGE = os.path.join(HERE, '.watch')
LOGDIR = os.path.join(HERE, 'logs')

WORKER = 'https://ma-tv.oaatiq.workers.dev'
EPG_URL = 'https://raw.githubusercontent.com/oaatiq/tv-playlist/guide/epg.xml.gz'

# Tunables. Environment overrides exist because this also runs on the household router,
# which is the honest place to test from - it is the boxes' own network and public IP, and
# a channel that is geo-locked to Morocco looks alive there and dead from anywhere else.
# It is also a router carrying everyone's traffic, so it runs with fewer threads.
def _env(name, default, cast=int):
    try:
        return cast(os.environ[name])
    except (KeyError, ValueError):
        return default


DROP_AFTER = _env('TVWATCH_DROP_AFTER', 3)       # failing runs in a row before removal
MAX_FAIL_RATIO = _env('TVWATCH_MAX_FAIL', 0.40, float)   # above this, do nothing at all
MAX_REMOVALS = _env('TVWATCH_MAX_REMOVALS', 40)  # a bigger cull than this wants a human
RETRY_PAUSE = _env('TVWATCH_RETRY_PAUSE', 120)   # before giving failures a second chance
FROZEN_PAUSE = _env('TVWATCH_FROZEN_PAUSE', 25)  # before re-reading a live playlist
TIMEOUT = _env('TVWATCH_TIMEOUT', 15)
WORKERS = _env('TVWATCH_WORKERS', 24)
UA = 'VLC/3.0.20 LibVLC/3.0.20'

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

os.makedirs(LOGDIR, exist_ok=True)
LOGFILE = os.path.join(LOGDIR, 'watch-' + datetime.date.today().isoformat() + '.log')


def log(msg):
    line = '%s %s' % (datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    with io.open(LOGFILE, 'a', encoding='utf-8') as f:
        f.write(line + '\n')
    if sys.stdout:
        try:
            print(line, flush=True)
        except Exception:
            pass


# ---------- the playlist ----------

def parse(path):
    lines = io.open(path, encoding='utf-8').read().splitlines()
    out = []
    for i, line in enumerate(lines):
        if not line.startswith('#EXTINF'):
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
        url = next((x for x in lines[i + 1:i + 8] if x and not x.startswith('#')), '')
        # A channel carrying #EXTVLCOPT needs those headers to answer at all. Probing
        # without them reports it dead, and after three runs this job would quietly delete
        # a channel that works perfectly in Kodi and OTT. Ask the way its own entry says to.
        headers = {}
        for extra in lines[i + 1:i + 8]:
            if not extra.startswith('#'):
                break
            if 'http-user-agent=' in extra:
                headers['User-Agent'] = extra.split('http-user-agent=', 1)[1].strip()
            elif 'http-referrer=' in extra:
                headers['Referer'] = extra.split('http-referrer=', 1)[1].strip()
        out.append({'chno': attrs.get('tvg-chno', ''), 'id': attrs.get('tvg-id', ''),
                    'group': attrs.get('group-title', ''), 'name': line.split(',', 1)[-1],
                    'url': url, 'headers': headers})
    return out


def key(c):
    """What the state file remembers a channel by. tvg-id where there is one, because a
    URL changes when a provider rotates it and the channel is still the same channel."""
    return c['id'] or (c['group'] + '\t' + c['name'])


# ---------- probing ----------

def get(url, limit=16384, extra=None):
    hdr = {'User-Agent': UA, 'Accept': '*/*'}
    hdr.update(extra or {})
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
        return r.status, r.geturl(), r.read(limit).decode('utf-8', 'replace')


def media_sequence(text):
    m = re.search(r'#EXT-X-MEDIA-SEQUENCE:(\d+)', text)
    return int(m.group(1)) if m else None


def probe(c):
    """-> (ok, why, media_url, sequence). media_url/sequence feed the frozen check."""
    if not c['url']:
        return False, 'no url', None, None
    try:
        status, final, text = get(c['url'], extra=c['headers'])
    except Exception as exc:
        return False, '%s: %s' % (type(exc).__name__, str(exc)[:60]), None, None
    if status != 200 and '#EXTM3U' not in text[:200]:
        return False, 'HTTP %d' % status, None, None
    if '<MPD' in text[:400]:
        return True, 'dash', None, None          # DASH: no media sequence to watch
    if '#EXTM3U' not in text[:200]:
        return False, 'not a playlist', None, None
    if '#EXT-X-STREAM-INF' in text:
        variant = next((l for l in text.splitlines() if l.strip() and not l.startswith('#')), None)
        if not variant:
            return False, 'master lists no variant', None, None
        vurl = urllib.parse.urljoin(final, variant)
        try:
            vstatus, vfinal, vtext = get(vurl, extra=c['headers'])
        except Exception as exc:
            return False, 'variant %s' % type(exc).__name__, None, None
        if vstatus != 200 or '#EXTINF' not in vtext:
            return False, 'variant HTTP %d' % vstatus, None, None
        return True, 'master', vfinal, media_sequence(vtext)
    if '#EXTINF' not in text:
        return False, 'no segments', None, None
    return True, 'media', final, media_sequence(text)


def still_moving(media_url, seq, extra=None):
    """A live playlist whose media sequence never advances is a frozen stream: it answers
    perfectly and shows nothing. recheck.py learned this the hard way."""
    try:
        _, _, text = get(media_url, extra=extra)
    except Exception:
        return True                               # unreadable now: let the next run judge
    now = media_sequence(text)
    return now is None or seq is None or now != seq


def run_checks(channels):
    results = {}
    with ThreadPoolExecutor(WORKERS) as ex:
        for c, r in zip(channels, ex.map(probe, channels)):
            results[key(c)] = r

    failed = [c for c in channels if not results[key(c)][0]]
    if failed:
        log('  %d failed on the first pass; second chance in %ds' % (len(failed), RETRY_PAUSE))
        time.sleep(RETRY_PAUSE)
        with ThreadPoolExecutor(WORKERS) as ex:
            for c, r in zip(failed, ex.map(probe, failed)):
                if r[0]:
                    results[key(c)] = r

    live = [c for c in channels if results[key(c)][0] and results[key(c)][2]
            and results[key(c)][3] is not None]
    if live:
        log('  watching %d live playlists for movement' % len(live))
        time.sleep(FROZEN_PAUSE)
        with ThreadPoolExecutor(WORKERS) as ex:
            moving = ex.map(lambda c: still_moving(results[key(c)][2], results[key(c)][3],
                                                   c['headers']), live)
            for c, ok in zip(live, moving):
                if not ok:
                    results[key(c)] = (False, 'frozen', None, None)
    return results


# ---------- state ----------

def load_state():
    try:
        return json.load(io.open(AUTO_DEAD, encoding='utf-8'))
    except Exception:
        return {'dead': {}, 'failing': {}, 'last_run': None}


def save_state(st):
    st['last_run'] = datetime.datetime.now().isoformat(timespec='seconds')
    os.makedirs(OVERRIDES_DIR, exist_ok=True)
    tmp = AUTO_DEAD + '.tmp'
    with io.open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(st, indent=1, ensure_ascii=False, sort_keys=True) + '\n')
    os.replace(tmp, AUTO_DEAD)


# ---------- rebuild ----------

def run(cmd, **kw):
    return subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, **kw)


def build(name, *apply_flags):
    """build -> overrides, into STAGE/<name>. -> (staged path or None, error)."""
    os.makedirs(STAGE, exist_ok=True)
    staged = os.path.join(STAGE, name)
    if os.path.exists(staged):
        os.remove(staged)
    r = run([sys.executable, '-I', BUILD,
             '--direct', os.path.join(SOURCES, 'direct.m3u'),
             '--world', os.path.join(SOURCES, 'world_fr_en.m3u'),
             '--out', STAGE, '--out-name', name,
             '--grouping', 'country', '--maroc-worker', WORKER, '--epg-url', EPG_URL])
    if r.returncode:
        return None, 'build failed:\n' + r.stdout + r.stderr
    r = run([sys.executable, '-I', os.path.join(HERE, 'apply_overrides.py'), staged, '--quiet']
            + list(apply_flags))
    if r.returncode:
        return None, 'overrides failed:\n' + r.stdout + r.stderr
    return staged, None


def dead_candidates(dead):
    """The dead channels as the sources describe them today, so they can be tested again.

    They are not in playlist.m3u - that is what being dead means - so without this nothing
    would ever probe them and none could come back. Built rather than remembered, so a
    channel whose URL upstream has since replaced is tested at the new one."""
    if not dead:
        return []
    staged, err = build('with-dead.m3u', '--ignore-dead')
    if not staged:
        log('  could not build the dead channels for a re-test, skipping it:\n' + err)
        return []
    found = [c for c in parse(staged) if key(c) in dead]
    missing = len(set(dead) - set(key(c) for c in found))
    if missing:
        log('  %d dead channel(s) are no longer in the sources at all' % missing)
    return found


def rebuild():
    """build -> overrides -> verify, into a staging dir. -> (ok, staged path, report)."""
    staged, err = build('playlist.m3u')
    if not staged:
        return False, None, err
    r = run([sys.executable, '-I', os.path.join(HERE, 'verify_playlist.py'), staged,
             '--compare', PLAYLIST])
    if r.returncode:
        return False, None, 'verify rejected the result:\n' + r.stdout + r.stderr
    return True, staged, r.stdout


def keep_resolved_urls(staged):
    """The committed source lists lag behind the live ones for a handful of channels whose
    URLs were resolved after the fact (CNN is the standing example). Rebuilding would undo
    that, so a URL already published for a channel this build did not change wins."""
    published = {}
    for c in parse(PLAYLIST):
        published[key(c)] = c['url']
    lines = io.open(staged, encoding='utf-8').read().splitlines()
    kept = 0
    for i, line in enumerate(lines):
        if not line.startswith('#EXTINF'):
            continue
        attrs = dict(re.findall(r'([\w-]+)="([^"]*)"', line))
        k = attrs.get('tvg-id') or (attrs.get('group-title', '') + '\t' + line.split(',', 1)[-1])
        for j in range(i + 1, min(i + 8, len(lines))):
            if lines[j] and not lines[j].startswith('#'):
                old = published.get(k)
                if old and old != lines[j] and 'jmp2.uk' not in old:
                    lines[j] = old
                    kept += 1
                break
    if kept:
        with io.open(staged, 'w', encoding='utf-8', newline='\n') as f:
            f.write('\n'.join(lines) + '\n')
    return kept


def publish(message, push):
    r = run(['git', 'add', '-A'])
    if r.returncode:
        return False, r.stderr
    r = run(['git', 'commit', '-m', message])
    if r.returncode:
        return False, (r.stdout + r.stderr).strip()
    if not push:
        return True, 'committed locally (--no-push)'
    r = run(['git', 'push'])
    return (r.returncode == 0), (r.stdout + r.stderr).strip()


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='test and report, change nothing')
    ap.add_argument('--no-push', action='store_true', help='rebuild and commit, stay local')
    ap.add_argument('--force', action='store_true', help='ignore the MAX_REMOVALS guard')
    args = ap.parse_args()

    channels = parse(PLAYLIST)
    st = load_state()
    failing, dead = dict(st.get('failing', {})), dict(st.get('dead', {}))
    buried = dead_candidates(dead)
    log('checking %d channels in %s, and %d found dead before'
        % (len(channels), os.path.basename(PLAYLIST), len(buried)))
    results = run_checks(channels + buried)

    bad = [c for c in channels if not results[key(c)][0]]
    ratio = len(bad) / max(len(channels), 1)
    log('  %d failing (%.1f%%)' % (len(bad), ratio * 100))
    if ratio > MAX_FAIL_RATIO:
        log('ABORT: %.0f%% of the list failed. That is this connection, not the channels.'
            % (ratio * 100))
        return 2

    # A dead channel that answers is back. One that still fails changes nothing: it is
    # already out, and its failures are not counted against the connection above.
    #    Two entries can share a key (Al Maghribia and its backup share a tvg-id), and
    #    removal goes by tvg-id, so they leave together and come back together.
    revived = list(OrderedDict((key(c), c) for c in buried if results[key(c)][0]).values())
    for c in revived:
        dead.pop(key(c), None)
        failing.pop(key(c), None)        # back with a clean slate: three strikes again
    for c in channels:
        k, (ok, why, _, _) = key(c), results[key(c)]
        if ok:
            failing.pop(k, None)
            if k in dead:
                dead.pop(k)
                revived.append(c)
        else:
            rec = failing.get(k, {'runs': 0})
            rec['runs'] += 1
            rec['why'] = why
            rec['name'] = c['name']
            rec['chno'] = c['chno']
            failing[k] = rec

    newly_dead = [c for c in channels
                  if key(c) in failing and failing[key(c)]['runs'] >= DROP_AFTER
                  and key(c) not in dead]
    for c in newly_dead:
        dead[key(c)] = {'name': c['name'], 'chno': c['chno'],
                        'why': failing[key(c)]['why'],
                        'since': datetime.date.today().isoformat()}

    for c in bad:
        rec = failing[key(c)]
        log('    failing x%d  %5s  %-28s %s' % (rec['runs'], c['chno'], c['name'][:28], rec['why']))
    for c in revived:
        log('    back             %s' % c['name'])

    if len(newly_dead) > MAX_REMOVALS and not args.force:
        log('ABORT: %d channels would be removed at once (limit %d). Run with --force if that '
            'is really right.' % (len(newly_dead), MAX_REMOVALS))
        return 2

    if args.dry_run:
        log('dry run: %d would be removed, %d restored' % (len(newly_dead), len(revived)))
        return 0

    if not newly_dead and not revived:
        save_state({'dead': dead, 'failing': failing})
        log('nothing to publish')
        return 0

    save_state({'dead': dead, 'failing': failing})
    ok, staged, report = rebuild()
    if not ok:
        log('rebuild failed, playlist left alone:\n' + report)
        return 1
    kept = keep_resolved_urls(staged)
    log(report.strip())
    if kept:
        log('  kept %d published URL(s) the source lists have not caught up with' % kept)

    os.replace(staged, PLAYLIST)
    shutil.copyfile(PLAYLIST, PLAYLIST8)
    # a returning channel's number is the one it has now, not the one it was tested under
    now = dict((key(c), c['chno']) for c in parse(PLAYLIST))
    gone = ', '.join('%s (%s)' % (c['name'], c['chno']) for c in newly_dead[:6])
    back = ', '.join('%s (%s)' % (c['name'], now.get(key(c), '?')) for c in revived[:6])
    summary = []
    if newly_dead:
        summary.append('%d gone: %s%s' % (len(newly_dead), gone,
                                          ' ...' if len(newly_dead) > 6 else ''))
    if revived:
        summary.append('%d back: %s%s' % (len(revived), back, ' ...' if len(revived) > 6 else ''))
    message = ('Health check: ' + '; '.join(summary) + '\n\n'
               + 'Checked %d channels, %d failing. A channel leaves the playlist after %d\n'
                 'consecutive failing runs and returns on the first good one. Written by\n'
                 'watch_playlist.py, which nobody had to be watching.\n'
               % (len(channels), len(bad), DROP_AFTER))
    ok, out = publish(message, push=not args.no_push)
    log(('published: ' if ok else 'publish FAILED: ') + out)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
