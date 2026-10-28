"""Check a playlist is structurally sound, and say what changed against a known-good one.

Run this on the output of apply_overrides.py before replacing the playlist the box
streams from. Hard failures are things that would confuse the player - duplicate channel
numbers, numbers that do not run 1..N down the file, a group split across the file, group
order that does not match the numbering.
Everything else is reported for a human to approve, because a channel appearing,
vanishing or changing URL is usually the upstream list drifting, not a bug.

usage: python -I verify_playlist.py <playlist.m3u> [--compare known-good.m3u] [--quiet]
exit status: 0 = sound, 1 = a hard failure
"""

import argparse
import difflib
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

    # the number is the position: 1..N down the file, no gaps (apply_overrides.py step 6)
    for i, e in enumerate(entries, 1):
        if int(e['attr']['tvg-chno']) != i:
            fails.append('channel numbers do not run 1..N in file order: entry %d (%s) is '
                         'numbered %s' % (i, e['name'], e['attr']['tvg-chno']))
            break

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
    """Report how `new` drifted from `old`, matched on channel identity.

    A channel added or removed shifts the number of everything after it, which is
    expected and only counted (`shifted`). `moved` lists channels whose place in the order
    changed - those that fall outside the longest run of channels both lists keep in the
    same order."""
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
    common_old = [k for k in o if k in n]
    common_new = [k for k in n if k in o]
    sm = difflib.SequenceMatcher(None, common_old, common_new, autojunk=False)
    in_place = set()
    for b in sm.get_matching_blocks():
        in_place.update(common_new[b.b:b.b + b.size])
    moved, shifted, regrouped, rehomed = [], 0, [], []
    for k, e in n.items():
        if k not in o:
            continue
        was = o[k]
        if k not in in_place:
            moved.append('  moved   %s -> %-6s %s'
                         % (was['attr']['tvg-chno'], e['attr']['tvg-chno'], e['name']))
        elif was['attr']['tvg-chno'] != e['attr']['tvg-chno']:
            shifted += 1
        if was['attr'].get('group-title') != e['attr'].get('group-title'):
            regrouped.append('  group   %-20s -> %-20s %s'
                             % (was['attr']['group-title'], e['attr']['group-title'], e['name']))
        if was.get('url') != e.get('url'):
            rehomed.append('  url     %5s  %s' % (e['attr']['tvg-chno'], e['name']))
        if was['props'] != e['props']:
            rehomed.append('  headers %5s  %s' % (e['attr']['tvg-chno'], e['name']))
    return notes, moved, shifted, regrouped, rehomed


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
        notes, moved, shifted, regrouped, rehomed = compare(old, entries)
        print('\nagainst %s (%d channels):' % (args.compare, len(old)))
        if not (notes or moved or shifted or regrouped or rehomed):
            print('  no differences')
        if shifted:
            print('  renumbered: %d, order unchanged (channels added or removed ahead of them)'
                  % shifted)
        for group, label in ((notes, None), (moved, 'moved in the order'),
                             (regrouped, 'regrouped'), (rehomed, 'stream changed')):
            if group and label:
                print('  %s: %d' % (label, len(group)))
            for line in group[:25]:
                print('  ' + line.strip() if label is None else '  ' + line)
            if len(group) > 25:
                print('    ... and %d more' % (len(group) - 25))

    # A channel carrying a directive line does not play in XCIPTV at all - the line goes to
    # Cronet in place of the URL (see RUNBOOK section 6). Three are known to have no way
    # round it; anything else here is new, and is a channel that will silently not play.
    expected = {'UniversalComedy.us@SD', 'UniversalCrime.us@East', 'AXNCEE.es@Romania'}
    unexpected = [e for e in entries if e['props'] and e['attr'].get('tvg-id') not in expected]
    if unexpected:
        print('\nWARNING: %d channel(s) carry a directive line and will not play in XCIPTV.'
              % len(unexpected))
        print('  Test each with and without its header. If it serves the same bare, add its')
        print('  tvg-id to strip_props; if it needs one, see RUNBOOK section 6.')
        for e in unexpected[:10]:
            print('  %6s  %-28s %s' % (e['attr'].get('tvg-chno', '?'), e['name'][:28],
                                       e['props'][0][:60]))
        if len(unexpected) > 10:
            print('  ... and %d more' % (len(unexpected) - 10))

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
