"""Apply the post-build overrides in overrides/overrides.json to a built playlist.

build_tivimate.py regenerates playlist.m3u from the two Kodi lists on the MINIX box
(direct.m3u + world_fr_en.m3u). Anything edited straight into playlist.m3u is lost on
the next build. This script keeps those edits as data in this repo and re-applies them
after every build, so a rebuild is no longer destructive.

What it may change: tvg-chno, group-title, tvg-id, tvg-logo, which entries are present,
the order entries are written in, and the directive lines (#EXTVLCOPT and friends) of
entries named in `strip_props`. What it never touches: stream URLs.

Channel numbers are not data: they are the entry's position in the file, 1..N, assigned
last. So a channel that is removed closes its gap, one that comes back reopens it, and the
number on the remote always matches the order on screen.

Idempotent - running it twice on the same file is a no-op.

usage: python -I apply_overrides.py <playlist.m3u> [--overrides overrides/overrides.json]
                                    [--out FILE] [--quiet] [--ignore-dead]
       (default --out is the input path, i.e. rewrite in place)
"""

import argparse
import io
import json
import os
import re
import sys
from collections import OrderedDict, Counter

# the attribute order build_tivimate.py's render() emits; keep it so diffs stay small
ATTR_ORDER = ('tvg-chno', 'tvg-id', 'tvg-name', 'tvg-logo', 'group-title')


def parse(text):
    """-> (header line, [entry dicts]). Entry = attrs, name, prop lines, url."""
    header, entries, cur = '#EXTM3U', [], None
    for raw in text.split('\n'):
        s = raw.rstrip('\r')
        if s.startswith('#EXTM3U'):
            header = s
        elif s.startswith('#EXTINF'):
            head, _, name = s.rpartition(',')
            cur = {'attr': OrderedDict(re.findall(r'([\w-]+)="([^"]*)"', head)),
                   'name': name, 'props': [], 'url': None}
        elif s.startswith('#') and cur is not None:
            cur['props'].append(s)
        elif s.strip() and cur is not None:
            cur['url'] = s.strip()
            entries.append(cur)
            cur = None
    return header, entries


def extinf(e):
    keep = [(k, e['attr'][k]) for k in ATTR_ORDER if e['attr'].get(k)]
    extra = [(k, v) for k, v in e['attr'].items() if k not in ATTR_ORDER]
    return '#EXTINF:-1 %s,%s' % (
        ' '.join('%s="%s"' % kv for kv in keep + extra), e['name'])


def apply_overrides(entries, ov, warn):
    stats = Counter()

    # 1. attribute patches, keyed on the group the entry has in the build output.
    #    Only fills blanks, so it cannot clobber anything the build starts emitting.
    patches = ov.get('patch_attrs', {}).get('by_group_and_name', {})
    seen_patch = set()
    for e in entries:
        key = '%s\t%s' % (e['attr'].get('group-title', ''), e['name'])
        if key in patches:
            seen_patch.add(key)
            for k, v in patches[key].items():
                if not e['attr'].get(k):
                    e['attr'][k] = v
                    stats['patched'] += 1
    for key in patches:
        if key not in seen_patch:
            warn('patch_attrs: no entry matches %r' % key.replace('\t', ' / '))

    if 'renumber' in ov:
        warn('renumber is obsolete and ignored: channels are numbered 1..N in file order')

    # 2. regrouping (the International split), keyed on tvg-id.
    regroup = ov.get('regroup', {}).get('by_tvg_id', {})
    hit = set()
    for e in entries:
        tid = e['attr'].get('tvg-id')
        if tid in regroup:
            hit.add(tid)
            if e['attr'].get('group-title') != regroup[tid]:
                e['attr']['group-title'] = regroup[tid]
                stats['regrouped'] += 1
    for tid in regroup:
        if tid not in hit:
            warn('regroup: no entry with tvg-id %r' % tid)

    # 3. removals: by country code in the tvg-id, plus an explicit id list, plus whatever
    #    the health check has found dead (overrides/auto_dead.json - machine-written, so it
    #    is kept out of overrides.json where a human would have to read past it).
    codes = ov.get('remove', {}).get('tvg_id_country_codes', [])
    code_re = re.compile(r'\.(%s)(@|$)' % '|'.join(codes)) if codes else None
    drop_ids = set(ov.get('remove', {}).get('tvg_id', []))
    auto_dead = set(ov.get('_auto_dead', []))
    drop_ids |= auto_dead
    kept, dropped_ids = [], set()
    for e in entries:
        tid = e['attr'].get('tvg-id', '')
        if code_re and tid and code_re.search(tid):
            stats['removed_by_code'] += 1
            continue
        if tid and tid in drop_ids:
            dropped_ids.add(tid)
            stats['removed_dead' if tid in auto_dead else 'removed_by_id'] += 1
            continue
        kept.append(e)
    entries = kept
    stale_removals = sorted(drop_ids - dropped_ids - auto_dead)

    # 4. additions - skipped when the URL is already there, so a second run is a no-op.
    #    They are appended, so each one lands after the build's channels in its group, in
    #    the order the `add` list gives. Its number comes from step 6 like everyone else's.
    #    Also skipped when the health check has found the channel dead: step 3 removed it,
    #    and re-adding it here would undo that silently. These entries are the only ones
    #    the removal pass cannot win against, because nothing else in the playlist is put
    #    back after being taken out - and watch_playlist.py only publishes on a *change*
    #    in the dead set, so without this they would sit in the list dead for good.
    have = set(e['url'] for e in entries)
    for rec in ov.get('add', []):
        if 'chno' in rec:
            warn('add: %r carries a chno, which is ignored (its place in the list decides)'
                 % rec['name'])
        if rec.get('tvg-id') in auto_dead:
            stats['add_dead'] += 1
            continue
        if rec['url'] in have:
            stats['add_skipped'] += 1
            continue
        attr = OrderedDict()
        attr['tvg-chno'] = ''
        for k in ('tvg-id', 'tvg-name', 'tvg-logo'):
            if rec.get(k):
                attr[k] = rec[k]
        attr['group-title'] = rec['group']
        entries.append({'attr': attr, 'name': rec['name'],
                        'props': list(rec.get('props', [])), 'url': rec['url']})
        have.add(rec['url'])
        stats['added'] += 1

    # An entry on the removal list that was not there is worth knowing about after a
    # real build - the channel is gone upstream and the override has gone stale. After a
    # re-run on an already-overridden file it means nothing, so only report it when this
    # run actually had something to add.
    already_applied = ov.get('add') and not stats['added']
    if not already_applied:
        for tid in stale_removals:
            warn('remove: tvg-id %r was already absent' % tid)

    # 5. drop the directive lines of entries that do not need them. XCIPTV plays
    #    through Cronet and hands it the #EXTVLCOPT line as if it were the URL, which
    #    fails every time with net::ERR_INVALID_URL - so for a channel that serves fine
    #    without its header, carrying the line costs the channel. Entries that genuinely
    #    need the header keep it and are listed in the `why`.
    strip = set(ov.get('strip_props', {}).get('tvg_id', []))
    stripped = set()
    for e in entries:
        tid = e['attr'].get('tvg-id', '')
        if tid in strip and e['props']:
            stripped.add(tid)
            stats['props_stripped'] += len(e['props'])
            e['props'] = []
    for tid in sorted(strip - stripped):
        if not any(e['attr'].get('tvg-id') == tid for e in entries):
            warn('strip_props: no entry with tvg-id %r' % tid)

    # 6. write out one contiguous block per group, groups in the configured order and
    #    entries inside a group in the order they arrived (the build's, then additions),
    #    then number the result 1..N. A group the order does not mention keeps its
    #    channels: it goes last and is reported, never dropped.
    #
    #    Arrival order, not the incoming numbers, because this runs on two kinds of input:
    #    a fresh build, numbered by the source lists, and playlist.m3u itself (add_channel
    #    --apply), already numbered 1..N. The two orders agree; the two numberings do not.
    order = ov.get('group_order', [])
    by_group = OrderedDict((g, []) for g in order)
    for e in entries:
        by_group.setdefault(e['attr'].get('group-title', ''), []).append(e)
    for g in order:
        if not by_group[g]:
            warn('group_order: group %r has no entries' % g)
    for g in by_group:
        if g not in order:
            warn('group %r is not in group_order - appended at the end (%d channels)'
                 % (g, len(by_group[g])))

    out = []
    for es in by_group.values():
        out.extend(es)
    for n, e in enumerate(out, 1):
        if e['attr'].get('tvg-chno') != str(n):
            e['attr']['tvg-chno'] = str(n)
            stats['renumbered'] += 1
    return out, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('playlist')
    ap.add_argument('--overrides', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'overrides', 'overrides.json'))
    ap.add_argument('--out', default=None)
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('--ignore-dead', action='store_true',
                    help='keep channels the health check has found dead (it builds this '
                         'way to find their current URLs and test them again)')
    args = ap.parse_args()

    warnings = []

    text = io.open(args.playlist, encoding='utf-8').read()
    ov = json.load(io.open(args.overrides, encoding='utf-8'))
    # The health check's findings live in their own file so that overrides.json stays a
    # human document. Absent file = nothing has been found dead, which is the right
    # default for a fresh clone.
    auto = os.path.join(os.path.dirname(os.path.abspath(args.overrides)), 'auto_dead.json')
    if os.path.exists(auto) and not args.ignore_dead:
        ov['_auto_dead'] = sorted(json.load(io.open(auto, encoding='utf-8')).get('dead', {}))
    header, before = parse(text)
    entries, stats = apply_overrides(before, ov, warnings.append)

    lines = [header]
    for e in entries:
        lines.append(extinf(e))
        lines.extend(e['props'])
        lines.append(e['url'])

    out_path = args.out or args.playlist
    with io.open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        f.write('\n'.join(lines) + '\n')

    if not args.quiet:
        print('applied overrides: %s -> %s' % (args.playlist, out_path))
        print('  channels     : %d in, %d out' % (len(before), len(entries)))
        print('  removed      : %d by country code, %d by id, %d found dead'
              % (stats['removed_by_code'], stats['removed_by_id'], stats['removed_dead']))
        print('  added        : %d (%d already present, %d held back as dead)'
              % (stats['added'], stats['add_skipped'], stats['add_dead']))
        print('  numbered     : 1-%d (%d changed)' % (len(entries), stats['renumbered']))
        print('  regrouped    : %d' % stats['regrouped'])
        print('  attrs filled : %d' % stats['patched'])
    if warnings:
        print('  WARNINGS (%d):' % len(warnings))
        for w in warnings:
            print('      ' + w)
    return 0


if __name__ == '__main__':
    sys.exit(main())
