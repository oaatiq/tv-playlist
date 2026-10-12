"""Check a playlist is structurally sound, and say what changed against a known-good one.

Run this on the output of apply_overrides.py before replacing the playlist the box
streams from. Hard failures are things that would confuse the player - duplicate channel
numbers, a group split across the file, group order that does not match the numbering.
Everything else is reported for a human to approve, because a channel appearing,
vanishing or changing URL is usually the upstream list drifting, not a bug.

usage: python -I verify_playlist.py <playlist.m3u> [--compare known-good.m3u] [--quiet]
exit status: 0 = sound, 1 = a hard failure
"""

import argparse
import io
import re
import sys
from collections import OrderedDict, Counter


def parse(path):
    raw = io.open(path, 'rb').read()
    problems = []
    if raw.startswith(b'\xef\xbb\xbf'):
        problems.append('file starts with a UTF-8 BOM')
    if b'\r\n' in raw:
        problems.append('file has CRLF line endings (want LF)')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError as exc:
        problems.append('file is not valid UTF-8: %s' % exc)
        text = raw.decode('utf-8', 'replace')

    entries, cur = [], None
    for lineno, line in enumerate(text.split('\n'), 1):
        s = line.rstrip('\r')
        if s.startswith('#EXTM3U') or not s.strip():
            continue
        if s.startswith('#EXTINF'):
            head, _, name = s.rpartition(',')
            cur = {'attr': dict(re.findall(r'([\w-]+)="([^"]*)"', head)),
                   'name': name, 'props': [], 'line': lineno}
        elif s.startswith('#'):
            if cur is not None:
                cur['props'].append(s)
        elif cur is not None:
            cur['url'] = s.strip()
            entries.append(cur)
            cur = None
        else:
            problems.append('line %d: URL with no #EXTINF before it' % lineno)
    if cur is not None:
        problems.append('line %d: #EXTINF with no URL after it' % cur['line'])
    return entries, problems


def ident(e):
    """A stable-ish identity for matching the same channel across two builds."""
    return e['attr'].get('tvg-id') or ('%s\t%s' % (e['attr'].get('group-title', ''), e['name']))


def check(entries):
    """-> (failures, group table). Group table is [(name, count, min, max)]."""
    fails = []

    for e in entries:
        if not e['attr'].get('tvg-chno'):
            fails.append('no tvg-chno: %s' % e['name'])
        elif not e['attr']['tvg-chno'].isdigit():
            fails.append('non-numeric tvg-chno %r: %s' % (e['attr']['tvg-chno'], e['name']))
        if not e['attr'].get('group-title'):
            fails.append('no group-title: %s' % e['name'])
        if not e.get('url'):
            fails.append('no URL: %s' % e['name'])
    if fails:
        return fails, []

    dups = [c for c, n in Counter(e['attr']['tvg-chno'] for e in entries).items() if n > 1]
    for c in sorted(dups, key=int):
        who = [e['name'] for e in entries if e['attr']['tvg-chno'] == c]
        fails.append('duplicate tvg-chno %s: %s' % (c, ', '.join(who)))

    # one contiguous run per group
    runs = []
    for e in entries:
        g = e['attr']['group-title']
        if not runs or runs[-1] != g:
            runs.append(g)
    for g, n in Counter(runs).items():
        if n > 1:
            fails.append('group %r is split across %d blocks in the file' % (g, n))

    table, prev_min, prev_name = [], None, None
    for g in OrderedDict.fromkeys(runs):
        chnos = [int(e['attr']['tvg-chno']) for e in entries if e['attr']['group-title'] == g]
        table.append((g, len(chnos), min(chnos), max(chnos)))
        if chnos != sorted(chnos):
            fails.append('group %r is not in ascending channel order' % g)
        if prev_min is not None and min(chnos) < prev_min:
            fails.append('group order breaks the numbering: %r (from %d) comes after %r (from %d)'
                         % (g, min(chnos), prev_name, prev_min))
        prev_min, prev_name = min(chnos), g
    return fails, table


def compare(old, new):
    """Report how `new` drifted from `old`, matched on channel identity."""
    o = OrderedDict((ident(e), e) for e in old)
    n = OrderedDict((ident(e), e) for e in new)
    notes = []
    for k, e in n.items():
        if k not in o:
            notes.append('+ added   %5s  %-22s %s'
                         % (e['attr']['tvg-chno'], e['attr']['group-title'], e['name']))
    for k, e in o.items():
        if k not in n:
            notes.append('- gone    %5s  %-22s %s'
                         % (e['attr']['tvg-chno'], e['attr']['group-title'], e['name']))
    moved, regrouped, rehomed = [], [], []
    for k, e in n.items():
        if k not in o:
            continue
        was = o[k]
        if was['attr']['tvg-chno'] != e['attr']['tvg-chno']:
            moved.append('  number  %s -> %-6s %s'
                         % (was['attr']['tvg-chno'], e['attr']['tvg-chno'], e['name']))
        if was['attr'].get('group-title') != e['attr'].get('group-title'):
            regrouped.append('  group   %-20s -> %-20s %s'
                             % (was['attr']['group-title'], e['attr']['group-title'], e['name']))
        if was.get('url') != e.get('url'):
            rehomed.append('  url     %5s  %s' % (e['attr']['tvg-chno'], e['name']))
        if was['props'] != e['props']:
            rehomed.append('  headers %5s  %s' % (e['attr']['tvg-chno'], e['name']))
    return notes, moved, regrouped, rehomed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('playlist')
    ap.add_argument('--compare', default=None, help='a known-good playlist to diff against')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    entries, problems = parse(args.playlist)
    fails, table = check(entries)
    fails = problems + fails

    if not args.quiet:
        for i, (g, n, lo, hi) in enumerate(table, 1):
            print('%3d. %-22s %4d  %d-%d' % (i, g, n, lo, hi))
        print('total: %d | groups: %d' % (len(entries), len(table)))

    if args.compare:
        old, _ = parse(args.compare)
        notes, moved, regrouped, rehomed = compare(old, entries)
        print('\nagainst %s (%d channels):' % (args.compare, len(old)))
        if not (notes or moved or regrouped or rehomed):
            print('  no differences')
        for group, label in ((notes, None), (moved, 'renumbered'),
                             (regrouped, 'regrouped'), (rehomed, 'stream changed')):
            if group and label:
                print('  %s: %d' % (label, len(group)))
            for line in group[:25]:
                print('  ' + line.strip() if label is None else '  ' + line)
            if len(group) > 25:
                print('    ... and %d more' % (len(group) - 25))

    if fails:
        print('\nFAILED (%d):' % len(fails))
        for f in fails[:40]:
            print('  ' + f)
        if len(fails) > 40:
            print('  ... and %d more' % (len(fails) - 40))
        return 1
    print('\nOK: numbering, grouping and encoding are sound')
    return 0


if __name__ == '__main__':
    sys.exit(main())
