"""Apply the post-build overrides in overrides/overrides.json to a built playlist.

build_tivimate.py regenerates playlist.m3u from the two Kodi lists on the MINIX box
(direct.m3u + world_fr_en.m3u). Anything edited straight into playlist.m3u is lost on
the next build. This script keeps those edits as data in this repo and re-applies them
after every build, so a rebuild is no longer destructive.

What it may change: tvg-chno, group-title, tvg-id, tvg-logo, which entries are present,
the order entries are written in, and - only through an explicit `rewrite_url` rule -
literal substrings of the stream URLs. What it never touches: #EXTVLCOPT lines, and any
URL no rewrite rule names.

Idempotent - running it twice on the same file is a no-op.

usage: python -I apply_overrides.py <playlist.m3u> [--overrides overrides/overrides.json]
                                    [--out FILE] [--quiet]
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

    # 2. renumbering, by position inside the group as the build emitted it.
    #    Done before removals, so removed entries leave their numbers as gaps - that is
    #    what the committed playlist has. Skipped when the group is already numbered at
    #    or above its base, which is what makes a second run a no-op.
    for group, base in ov.get('renumber', {}).items():
        members = [e for e in entries if e['attr'].get('group-title') == group]
        if not members:
            warn('renumber: group %r not present' % group)
            continue
        if min(int(e['attr']['tvg-chno']) for e in members) >= base:
            stats['renumber_skipped'] += 1
            continue
        for i, e in enumerate(members):
            e['attr']['tvg-chno'] = str(base + i)
            stats['renumbered'] += 1

    # 3. regrouping (the International split), keyed on tvg-id.
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

    # 4. removals: by country code in the tvg-id, plus an explicit id list.
    codes = ov.get('remove', {}).get('tvg_id_country_codes', [])
    code_re = re.compile(r'\.(%s)(@|$)' % '|'.join(codes)) if codes else None
    drop_ids = set(ov.get('remove', {}).get('tvg_id', []))
    kept, dropped_ids = [], set()
    for e in entries:
        tid = e['attr'].get('tvg-id', '')
        if code_re and tid and code_re.search(tid):
            stats['removed_by_code'] += 1
            continue
        if tid and tid in drop_ids:
            dropped_ids.add(tid)
            stats['removed_by_id'] += 1
            continue
        kept.append(e)
    entries = kept
    stale_removals = sorted(drop_ids - dropped_ids)

    # 5. additions - skipped when the URL is already there, so a second run is a no-op.
    have = set(e['url'] for e in entries)
    for rec in ov.get('add', []):
        if rec['url'] in have:
            stats['add_skipped'] += 1
            continue
        attr = OrderedDict()
        attr['tvg-chno'] = str(rec['chno'])
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

    # 6. URL rewrites - literal substring swaps, applied to added entries too. These
    #    exist for players that choke on a URL the upstream list is happy with; a swap
    #    that is already in place simply finds nothing, which is what keeps this
    #    idempotent.
    for rule in ov.get('rewrite_url', []):
        find, repl = rule['find'], rule['replace']
        hits = 0
        for e in entries:
            if find in e['url']:
                e['url'] = e['url'].replace(find, repl)
                hits += 1
        stats['urls_rewritten'] += hits
        if not hits and not any(repl in e['url'] for e in entries):
            warn('rewrite_url: nothing contains %r (nor %r) - rule is stale'
                 % (find, repl))

    # 7. write out one contiguous block per group, groups in the configured order and
    #    entries inside a group by channel number. A group the order does not mention
    #    keeps its channels: it goes last and is reported, never dropped.
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
        out.extend(sorted(es, key=lambda e: int(e['attr']['tvg-chno'])))
    return out, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('playlist')
    ap.add_argument('--overrides', default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'overrides', 'overrides.json'))
    ap.add_argument('--out', default=None)
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    warnings = []

    text = io.open(args.playlist, encoding='utf-8').read()
    ov = json.load(io.open(args.overrides, encoding='utf-8'))
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
        print('  removed      : %d by country code, %d by id'
              % (stats['removed_by_code'], stats['removed_by_id']))
        print('  added        : %d (%d already present)' % (stats['added'], stats['add_skipped']))
        print('  renumbered   : %d (%d groups already numbered)'
              % (stats['renumbered'], stats['renumber_skipped']))
        print('  regrouped    : %d' % stats['regrouped'])
        print('  attrs filled : %d' % stats['patched'])
    if warnings:
        print('  WARNINGS (%d):' % len(warnings))
        for w in warnings:
            print('      ' + w)
    return 0


if __name__ == '__main__':
    sys.exit(main())
