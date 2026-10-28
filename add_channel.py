"""Add a channel - or a whole .m3u of them - to the playlist, safely.

A channel typed straight into playlist.m3u is gone on the next build. The durable place
is the `add` list in overrides/overrides.json, and this is that edit done for you: it
puts the channel at the end of its group, fills in a tvg-id, checks the stream
actually serves, writes the override, and then *proves* the result still passes
verify_playlist.py before keeping it. If the check fails, overrides.json is put back the
way it was and playlist.m3u is never touched.

    python -I add_channel.py --groups
        the group table: how many channels and their current numbers. Read this first
        when you are not sure which group it goes in.

    python -I add_channel.py --group Maroc --name "Al Aoula 4K" --url https://host/x.m3u8
        stage one channel: probe it, write the override, verify. playlist.m3u unchanged.

    python -I add_channel.py --group Maroc --name "..." --url ... --apply
        the same, and update playlist.m3u too - no full rebuild needed, because
        apply_overrides.py is idempotent and an addition does not disturb anything else.

    python -I add_channel.py --from-m3u new.m3u --group "Other countries" --apply
        every channel in new.m3u, in file order. Already-present URLs are skipped, so
        re-running the same file adds nothing.

Channel numbers are not chosen here: the playlist is numbered 1..N down the file every
time it is built, so a new channel takes the number after its group's last one and every
channel below it moves up by one.

Useful flags:
    --logo URL --id Foo.ma@HD   set them yourself instead of letting it guess
    --no-probe                  skip the stream test (a geo-locked channel that only
                                answers from the box's network will fail from here)
    --keep-headers              keep #EXTVLCOPT lines from --from-m3u. Off by default:
                                XCIPTV feeds that line to Cronet in place of the URL and
                                the channel does not play at all (see RUNBOOK section 6).
    --dry-run                   print what it would add and stop
    --force                     add anyway when the probe fails or the name is a duplicate

Nothing is committed or pushed, and nothing is sent to the box. That is still section 4
of the RUNBOOK.
"""

import argparse
import io
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections import OrderedDict

HERE = os.path.dirname(os.path.abspath(__file__))
PLAYLIST = os.path.join(HERE, 'playlist.m3u')
OVERRIDES = os.path.join(HERE, 'overrides', 'overrides.json')
APPLY = os.path.join(HERE, 'apply_overrides.py')
VERIFY = os.path.join(HERE, 'verify_playlist.py')

UA = 'VLC/3.0.20 LibVLC/3.0.20'          # what the box sends; some origins check
TIMEOUT = 15
CTX = ssl.create_default_context()
CTX.check_hostname = False               # a few of these origins have a bad chain and
CTX.verify_mode = ssl.CERT_NONE          # still play fine on the box

# A country code for the tvg-id, so a generated id looks like the ones already there.
# Only the groups where it is unambiguous; anywhere else the id is left without one.
GROUP_CC = {
    'Maroc': 'ma', 'France': 'fr', 'Infos FR': 'fr', 'Al Jazeera': 'qa',
    'Arabie Saoudite': 'sa', 'Égypte': 'eg', 'Émirats': 'ae', 'Qatar': 'qa',
    'United Kingdom': 'uk', 'United States': 'us',
}


# ---------- reading what is already there ----------

def parse_m3u(path):
    """-> [{attr, name, props, url}]. Same shape apply_overrides.py uses."""
    entries, cur = [], None
    for raw in io.open(path, encoding='utf-8').read().split('\n'):
        s = raw.rstrip('\r')
        if s.startswith('#EXTM3U'):
            continue
        if s.startswith('#EXTINF'):
            head, _, name = s.rpartition(',')
            cur = {'attr': dict(re.findall(r'([\w-]+)="([^"]*)"', head)),
                   'name': name.strip(), 'props': [], 'url': None}
        elif s.startswith('#') and cur is not None:
            cur['props'].append(s)
        elif s.strip() and cur is not None:
            cur['url'] = s.strip()
            entries.append(cur)
            cur = None
    return entries


def group_table(entries, order):
    """-> OrderedDict group -> sorted channel numbers, in group_order."""
    by_group = OrderedDict((g, []) for g in order)
    for e in entries:
        g = e['attr'].get('group-title', '')
        by_group.setdefault(g, []).append(int(e['attr']['tvg-chno']))
    for g in by_group:
        by_group[g].sort()
    return by_group


def make_id(name, group, taken):
    """A tvg-id in the house style: CamelCaseName.cc@SD, never a duplicate."""
    base = re.sub(r'[^0-9A-Za-z]', '', name.title()) or 'Channel'
    cc = GROUP_CC.get(group)
    tid = '%s.%s@SD' % (base, cc) if cc else '%s@SD' % base
    n = 2
    while tid in taken:
        tid = ('%s%d.%s@SD' % (base, n, cc)) if cc else ('%s%d@SD' % (base, n))
        n += 1
    return tid


# ---------- does it actually serve? ----------

def get(url):
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=CTX) as r:
        return r.status, r.geturl(), r.read(16384).decode('utf-8', 'replace')


def probe(url):
    """-> (ok, why). Deliberately the same shape of check watch_playlist.py runs: a
    master whose variant is unreadable is not a working channel."""
    try:
        status, final, text = get(url)
    except Exception as exc:
        return False, '%s: %s' % (type(exc).__name__, str(exc)[:70])
    if '<MPD' in text[:400]:
        return True, 'DASH'
    if '#EXTM3U' not in text[:200]:
        return False, 'HTTP %d, not a playlist' % status
    if '#EXT-X-STREAM-INF' in text:
        variant = next((l for l in text.splitlines() if l.strip() and not l.startswith('#')), None)
        if not variant:
            return False, 'master lists no variant'
        try:
            _, _, vtext = get(urllib.parse.urljoin(final, variant))
        except Exception as exc:
            return False, 'variant %s' % type(exc).__name__
        if '#EXTINF' not in vtext:
            return False, 'variant carries no segments'
        return True, 'HLS master, %d variants' % text.count('#EXT-X-STREAM-INF')
    if '#EXTINF' not in text:
        return False, 'playlist carries no segments'
    return True, 'HLS media playlist'


# ---------- proving the result before keeping it ----------

def behind_upstream():
    """-> (commits behind, note). The health check on the router rebuilds and pushes by
    itself, so this clone goes stale on its own. Adding on a stale base is not harmless:
    --apply writes the new playlist on top of the local one, so it would resurrect every
    channel the router has removed since, and overrides/auto_dead.json - which says which
    those are - may not even be here yet. Hence a hard stop rather than a warning."""
    try:
        r = subprocess.run(['git', 'fetch', '-q'], cwd=HERE, capture_output=True,
                           text=True, timeout=25)
        if r.returncode:
            return 0, 'could not reach the remote; carrying on with the local copy'
        r = subprocess.run(['git', 'rev-list', '--count', 'HEAD..@{u}'], cwd=HERE,
                           capture_output=True, text=True, timeout=10)
        return (int(r.stdout.strip() or 0), None) if r.returncode == 0 else (0, None)
    except Exception as exc:
        return 0, 'could not check the remote (%s); carrying on' % type(exc).__name__


def stage_and_verify():
    """Apply the overrides as they now stand to a copy of playlist.m3u and verify it.
    -> (ok, staged path, output). The copy is what --apply then promotes."""
    staged = os.path.join(tempfile.mkdtemp(prefix='addchan-'), 'playlist.m3u')
    shutil.copyfile(PLAYLIST, staged)
    out = []
    for cmd in ([sys.executable, '-I', APPLY, staged],
                [sys.executable, '-I', VERIFY, staged, '--compare', PLAYLIST]):
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
        out.append((r.stdout or '') + (r.stderr or ''))
        if r.returncode != 0:
            return False, staged, '\n'.join(out)
    return True, staged, '\n'.join(out)


# ---------- the records to add ----------

def records_from_args(args, playlist, ov, ids):
    """-> ([record], [note]). Records come out in the order given, which is the order
    they will have at the end of the group."""
    # pending adds the live playlist has not picked up yet still own their ids
    live_urls = set(e['url'] for e in playlist)
    for rec in ov.get('add', []):
        if rec['url'] not in live_urls:
            ids.add(rec.get('tvg-id', ''))

    if args.from_m3u:
        incoming = [{'name': e['name'], 'url': e['url'],
                     'logo': e['attr'].get('tvg-logo', ''),
                     'id': e['attr'].get('tvg-id', ''), 'props': e['props']}
                    for e in parse_m3u(args.from_m3u)]
        if not incoming:
            return [], ['%s holds no channels' % args.from_m3u]
    else:
        incoming = [{'name': args.name, 'url': args.url, 'logo': args.logo or '',
                     'id': args.id or '', 'props': []}]

    names_in_group = {}
    for e in playlist:
        names_in_group.setdefault(e['attr'].get('group-title', ''), set()).add(e['name'])

    recs, notes = [], []
    for c in incoming:
        if c['url'] in live_urls or any(r['url'] == c['url'] for r in ov.get('add', [])):
            notes.append('skipped (already in the playlist): %s' % c['name'])
            continue
        if c['name'] in names_in_group.get(args.group, set()):
            msg = '%r is already a channel in %s' % (c['name'], args.group)
            if not args.force:
                notes.append('skipped (%s) - pass --force to add it anyway' % msg)
                continue
            notes.append(msg + ' - added anyway (--force)')

        tid = c['id'] or make_id(c['name'], args.group, ids)
        if tid in ids:
            notes.append('tvg-id %r is already used; generating another' % tid)
            tid = make_id(c['name'], args.group, ids)

        rec = OrderedDict()
        rec['name'] = c['name']
        rec['group'] = args.group
        rec['url'] = c['url']
        rec['tvg-id'] = tid
        if c['logo']:
            rec['tvg-logo'] = c['logo']
        if c['props']:
            if args.keep_headers:
                rec['props'] = c['props']
                notes.append('%s keeps its header line - it will NOT play in XCIPTV '
                             '(RUNBOOK section 6)' % c['name'])
            else:
                notes.append('%s: dropped %d header line(s); test it, and if it needs them '
                             'see RUNBOOK section 6' % (c['name'], len(c['props'])))
        recs.append(rec)
        ids.add(tid)
        names_in_group.setdefault(args.group, set()).add(c['name'])
    return recs, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--groups', action='store_true', help='show the group table and exit')
    ap.add_argument('--group', help='the group to add to (must already exist)')
    ap.add_argument('--name')
    ap.add_argument('--url')
    ap.add_argument('--logo', default='')
    ap.add_argument('--id', default='', help='tvg-id; generated when left out')
    ap.add_argument('--from-m3u', help='add every channel in this .m3u instead')
    ap.add_argument('--apply', action='store_true', help='also update playlist.m3u')
    ap.add_argument('--no-probe', action='store_true')
    ap.add_argument('--no-fetch', action='store_true',
                    help='skip the "is this clone stale" check against the remote')
    ap.add_argument('--keep-headers', action='store_true')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true')
    args = ap.parse_args()

    original = io.open(OVERRIDES, encoding='utf-8', newline='').read()
    ov = json.loads(original)
    playlist = parse_m3u(PLAYLIST)
    order = ov['group_order']

    if args.groups:
        table = group_table(playlist, order)
        print('%-22s %5s  %s' % ('group', 'chans', 'numbers'))
        for g in order:
            n = table.get(g) or []
            print('%-22s %5d  %s' % (g, len(n), '%d-%d' % (n[0], n[-1]) if n else '-'))
        print('\n%d channels. Group names are exact - copy one as --group.' % len(playlist))
        return 0

    if not args.group:
        ap.error('--group is required (see --groups)')
    if args.group not in order:
        print('FAILED: %r is not one of the groups. Run --groups for the list.' % args.group)
        print('        (a group outside group_order gets dumped at the end of the file)')
        return 1
    if not args.from_m3u and not (args.name and args.url):
        ap.error('give --name and --url, or --from-m3u')
    if args.from_m3u and not os.path.exists(args.from_m3u):
        print('FAILED: no such file: %s' % args.from_m3u)
        return 1

    if not args.no_fetch:
        behind, note = behind_upstream()
        if note:
            print('  note: %s' % note)
        if behind and not args.force:
            print('FAILED: this clone is %d commit(s) behind the remote.' % behind)
            print('        The health check on the router rebuilds and pushes by itself, so')
            print('        adding on top of this playlist would undo whatever it has removed')
            print('        since. Run `git pull` first (or --force if you know better).')
            return 1

    ids = set(e['attr'].get('tvg-id', '') for e in playlist)
    recs, notes = records_from_args(args, playlist, ov, ids)

    for n in notes:
        print('  note: %s' % n)
    if not recs:
        print('nothing to add.')
        return 0

    # 1. does it serve? A dead URL in the playlist is a channel number that does nothing.
    if not args.no_probe:
        print('\nprobing %d stream(s):' % len(recs))
        bad = []
        for rec in recs:
            ok, why = probe(rec['url'])
            print('  %-28s %s' % (rec['name'][:28],
                                       ('ok, ' + why) if ok else ('FAILED: ' + why)))
            if not ok:
                bad.append(rec)
        if bad and not args.force:
            print('\n%d stream(s) did not serve from here. Nothing was written.' % len(bad))
            print('A channel geo-locked to Morocco can still be fine on the box - check it')
            print('there, then re-run with --force (or --no-probe).')
            return 1

    print('\nadding %d channel(s) to %s:' % (len(recs), args.group))
    for rec in recs:
        print('  %s' % json.dumps(rec, ensure_ascii=False))

    if args.dry_run:
        print('\n--dry-run: nothing written.')
        return 0

    # 2. write the override. Appended, not sorted: an addition's place in the list is its
    #    place in its group, so the new one goes last.
    ov.setdefault('add', []).extend(recs)
    io.open(OVERRIDES, 'w', encoding='utf-8', newline='\n').write(
        json.dumps(ov, indent=2, ensure_ascii=False) + '\n')

    # 3. prove the playlist still verifies with it. If not, put overrides.json back.
    print('\nverifying:')
    ok, staged, out = stage_and_verify()
    shown = [l for l in out.split('\n') if l.strip()]
    print('\n'.join('  ' + l for l in shown[-20:]))
    if not ok:
        io.open(OVERRIDES, 'w', encoding='utf-8', newline='\n').write(original)
        print('\nFAILED: the result does not verify. overrides.json has been put back and')
        print('        playlist.m3u was never touched.')
        return 1

    if args.apply:
        shutil.copyfile(staged, PLAYLIST)
        print('\noverrides.json and playlist.m3u updated. Next:')
        print('    git add -A; git commit -m "Add %s"; git push'
              % (recs[0]['name'] if len(recs) == 1 else '%d channels' % len(recs)))
        print('    then XCIPTV -> Update Contents on the box')
    else:
        print('\noverrides.json updated; playlist.m3u left alone.')
        print('Re-run with --apply to update it, or .\\rebuild.ps1 for a full rebuild.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
