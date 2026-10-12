"""Build a TiviMate-ready playlist from the Kodi IPTV Simple playlists.

Reads the two live m3u files used by IPTV Simple (direct.m3u = curated 11-48,
world_fr_en.m3u = World FR/EN 1001-3744) and emits one merged playlist that an
ExoPlayer-based client can use:

  * #KODIPROP lines dropped (Kodi/InputStream Adaptive only). Losing
    stream_selection_type=fixed-res is the point: ExoPlayer does real HLS ABR.
  * Kodi pipe headers (url|User-Agent=...&Referer=...) moved off the URL into
    #EXTVLCOPT:http-user-agent / http-referrer lines, percent-decoded.
  * plugin:// entries dropped - no Python resolver outside Kodi.
  * resource:// logos rewritten to --logo-base, or dropped if not given.
  * tvg-chno written on every entry so the existing numbering survives
    regardless of playlist order.
  * World group-title (IPTV Simple's "Lang;Country;Genre" multi-group syntax)
    collapsed to a single label, since TiviMate takes one group per channel.
  * Every Moroccan channel (curated "Maroc" group + world entries with a .ma
    tvg-id) is emitted first, in one "Maroc" group, ordered by tvg-chno.
  * --maroc-worker adds SNRT 1-9 + Télé Maroc 10 through the Cloudflare Worker
    in tv-playlist/worker (it renews SNRT's 1-hour token), and the labelled
    third-party re-streams in MAROC_BACKUP.

usage: python -I build_tivimate.py --direct <direct.m3u> --world <world.m3u>
                                   --out <dir> [--out-name NAME]
                                   [--grouping lang-country-genre|country-genre|country]
                                   [--logo-base URL] [--epg-url URL]
                                   [--maroc-worker URL]
"""

import argparse
import io
import os
import re
import sys
from collections import Counter, OrderedDict
from urllib.parse import unquote

# Kodi header name -> EXTVLCOPT option name
VLCOPT = {
    'user-agent': 'http-user-agent',
    'referer': 'http-referrer',
    'referrer': 'http-referrer',
    'origin': 'http-origin',
}

LANG_SHORT = {'Français': 'FR', 'English': 'EN'}

MAROC = 'Maroc'

# chno, name, path on the Worker, tvg-id, logo (ids/logos from iptv-org)
MAROC_WORKER = [
    (1, 'Al Aoula', 'snrt/aloula.m3u8', 'AlAoula.ma', 'https://i.imgur.com/nq53d2N.png'),
    (2, 'Al Aoula Laâyoune', 'snrt/laayoune.m3u8', 'LaayouneTV.ma', 'https://i.ibb.co/Q3r5D5d0/image-removebg-preview.png'),
    (3, 'Arryadia', 'snrt/arryadia.m3u8', 'Arryadia.ma', 'https://i.imgur.com/XjzK3gZ.png'),
    (4, 'Arryadia TNT', 'snrt/arryadia-tnt.m3u8', '', 'https://i.imgur.com/XjzK3gZ.png'),
    (5, 'Arryadia HD1', 'snrt/arryadia-hd1.m3u8', 'ArryadiaHD1.ma', 'https://i.ibb.co/fYNx6Fk1/image-removebg-preview-1.png'),
    (6, 'Athaqafia', 'snrt/athaqafia.m3u8', 'Athaqafia.ma', 'https://freebox.cdn.scw.iliad.fr/medium_Athaqafia_fdc90e055e.png'),
    (7, 'Al Maghribia', 'snrt/almaghribia.m3u8', 'AlMaghribia.ma', 'https://i.imgur.com/7GaahYh.png'),
    (8, 'Assadissa', 'snrt/assadissa.m3u8', 'Assadissa.ma', 'https://freebox.cdn.scw.iliad.fr/medium_Assadissa_f8b25de821.png'),
    (9, 'Tamazight', 'snrt/tamazight.m3u8', 'TamazightTV.ma', 'https://i.imgur.com/fm6S7we.png'),
    (10, 'Télé Maroc', 'telemaroc.m3u8', 'TeleMaroc.ma', 'https://freebox.cdn.scw.iliad.fr/medium_Tele_Maroc_900x800_ffdee4d2e4.png'),
]

# Third-party re-streams (Net Sat AB, Sweden), not the broadcasters' own. Tested
# 2026-10-11; may disappear without notice. (Their Arryadia re-stream was too slow.)
# 2M sits at 14, next to 2M Monde: on the box it's live (~20 s) and a steady
# 25 fps, while the Globecast 2M Monde (15) stutters.
MAROC_BACKUP = [
    (14, '2M', 'http://185.9.2.18/chid_218/index.m3u8', '2M.ma', 'https://i.imgur.com/PJYTfHi.png'),
    (107, 'Al Maghribia HD (backup)', 'http://185.9.2.18/chid_205/index.m3u8', 'AlMaghribia.ma', 'https://i.imgur.com/7GaahYh.png'),
]

# Channels whose CDN checks a Referer/User-Agent on every request, segments included.
# XCIPTV ignores #EXTVLCOPT, so these go through the Worker's /h/ routes instead and the
# headers are dropped from the playlist. Measured 2026-10-11: every other header-carrying
# entry (the BFM channels, Rai Italia, Tele 7, S Play Event, AXN) plays with no header at
# all, so those stay direct. Needs --maroc-worker.
PROXY_HEADERS = {
    '2M Monde': 'h/2m-monde.m3u8',
    'Military History': 'h/military-history.m3u8',
    'Press TV': 'h/press-tv.m3u8',
    'Music Box Polska Ukraine': 'h/music-box.m3u8',
}

# Curated Maroc entries shown under a different name than in Kodi's direct.m3u.
# 2M_ES (iptv-org "2M Monde +1") was confirmed one hour behind 2M Monde on the TV
# on 2026-10-11.
MAROC_NAMES = {
    '2M Monde HD': '2M Monde HD (+1h)',
}

# Curated Maroc entries whose Kodi logo is a resource:// path or missing.
MAROC_LOGOS = {
    'Medi1 Maghreb': 'https://i.imgur.com/F5OFQsp.jpeg',
    'Medi1 Arabic': 'https://i.imgur.com/3YsZPY6.jpeg',
    'Medi1 Afrique': 'https://i.imgur.com/a20DHnj.png',
}


def parse(path):
    """Yield (extinf, [prop lines], url) for each entry in an m3u."""
    lines = io.open(path, encoding='utf-8').read().splitlines()
    extinf, props = None, []
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if s.startswith('#EXTINF'):
            extinf, props = s, []
        elif s.startswith('#EXTM3U') or s.startswith('##'):
            continue
        elif s.startswith('#'):
            props.append(s)
        elif extinf is not None:
            yield extinf, props, s
            extinf, props = None, []


def attrs(extinf):
    """Split an #EXTINF line into (attr dict, display name)."""
    head, _, name = extinf.rpartition(',')
    return dict(re.findall(r'([\w-]+)="([^"]*)"', head)), name


def split_headers(url):
    """url|K=v&K2=v2 -> (url, {k: decoded v}). Kodi/IPTV Simple syntax."""
    if '|' not in url:
        return url, {}
    base, _, tail = url.partition('|')
    headers = {}
    for part in tail.split('&'):
        k, _, v = part.partition('=')
        if k:
            headers[k.strip().lower()] = unquote(v)
    return base, headers


def world_group(raw, mode):
    """Collapse "Lang;Country;Genre" (langs may be multiple) to one label."""
    parts = [p for p in raw.split(';') if p]
    langs = [p for p in parts if p in LANG_SHORT]
    rest = [p for p in parts if p not in LANG_SHORT]
    country = rest[0] if rest else 'Other countries'
    genre = rest[1] if len(rest) > 1 else 'General'
    if mode == 'country':
        return country
    if mode == 'country-genre':
        return '%s - %s' % (country, genre)
    # lang-country-genre (default): French first so FR groups sort together.
    lang = 'FR' if 'Français' in langs else (LANG_SHORT.get(langs[0], 'EN') if langs else 'EN')
    return '%s - %s - %s' % (lang, country, genre)


def render(num, name, attr, url, headers, logo_base, stats):
    """Emit the lines for one TiviMate entry."""
    out = []
    keep = OrderedDict()
    keep['tvg-chno'] = str(num)
    for k in ('tvg-id', 'tvg-name'):
        if attr.get(k):
            keep[k] = attr[k]
    logo = attr.get('tvg-logo', '')
    if logo.startswith('resource://'):
        if logo_base:
            keep['tvg-logo'] = '%s/%s' % (logo_base.rstrip('/'), logo.rsplit('/', 1)[1])
            stats['logo_rewritten'] += 1
        else:
            stats['logo_dropped'] += 1
    elif logo:
        keep['tvg-logo'] = logo
    keep['group-title'] = attr['group-title']

    out.append('#EXTINF:-1 %s,%s' % (
        ' '.join('%s="%s"' % (k, v) for k, v in keep.items()), name))
    for hk, hv in headers.items():
        opt = VLCOPT.get(hk)
        if opt:
            out.append('#EXTVLCOPT:%s=%s' % (opt, hv))
            stats['headers_moved'] += 1
        else:
            stats['headers_unknown'].append('%s (%s)' % (name, hk))
    out.append(url)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--direct', required=True)
    ap.add_argument('--world', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--grouping', default='lang-country-genre',
                    choices=['lang-country-genre', 'country-genre', 'country'])
    ap.add_argument('--logo-base', default='')
    ap.add_argument('--epg-url', default='')
    ap.add_argument('--out-name', default='tivimate.m3u')
    ap.add_argument('--maroc-worker', default='',
                    help='base URL of the tv-playlist Worker; adds SNRT 1-9 + Télé Maroc 10')
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    stats = Counter()
    stats['headers_unknown'] = []
    dropped, groups = [], Counter()
    maroc, body = [], []  # maroc: (chno, lines), emitted first in chno order

    def add(num, name, attr, url, headers):
        if args.maroc_worker and name in PROXY_HEADERS:
            url = '%s/%s' % (args.maroc_worker.rstrip('/'), PROXY_HEADERS[name])
            headers = {}  # the Worker attaches them upstream
            stats['header_proxied'] += 1
        is_maroc = attr['group-title'] == MAROC or re.search(r'\.ma(@|$)', attr.get('tvg-id', ''))
        if is_maroc:
            attr['group-title'] = MAROC
            name = MAROC_NAMES.get(name, name)
            if not attr.get('tvg-logo') or attr['tvg-logo'].startswith('resource://'):
                attr['tvg-logo'] = MAROC_LOGOS.get(name, attr.get('tvg-logo', ''))
        lines = render(num, name, attr, url, headers, args.logo_base, stats)
        (maroc.append((num, lines)) if is_maroc else body.extend(lines))
        groups[attr['group-title']] += 1

    if args.maroc_worker:
        for num, name, path, tvg_id, logo in MAROC_WORKER:
            add(num, name, {'tvg-id': tvg_id, 'tvg-logo': logo, 'group-title': MAROC},
                '%s/%s' % (args.maroc_worker.rstrip('/'), path), {})
            stats['maroc_worker'] += 1
        for num, name, url, tvg_id, logo in MAROC_BACKUP:
            add(num, name, {'tvg-id': tvg_id, 'tvg-logo': logo, 'group-title': MAROC}, url, {})
            stats['maroc_backup'] += 1

    # --- curated list: chno 11-14 are explicit, the rest continue from the
    # last explicit one exactly as IPTV Simple numbers them (15..48).
    next_num = None
    for extinf, _props, url in parse(args.direct):
        attr, name = attrs(extinf)
        if attr.get('tvg-chno'):
            num = int(attr['tvg-chno'])
            next_num = num + 1
        else:
            num = next_num
            next_num += 1
        if url.startswith('plugin://'):
            dropped.append((num, name, 'Kodi plugin:// - needs a Python resolver'))
            continue
        clean, headers = split_headers(url)
        add(num, name, attr, clean, headers)
        stats['curated'] += 1

    # --- world list: tvg-chno is authoritative, group-title gets collapsed.
    for extinf, _props, url in parse(args.world):
        attr, name = attrs(extinf)
        num = int(attr['tvg-chno'])
        if url.startswith('plugin://'):
            dropped.append((num, name, 'Kodi plugin:// - needs a Python resolver'))
            continue
        attr['group-title'] = world_group(attr.get('group-title', ''), args.grouping)
        if name.startswith('Medi1TV '):  # same channels as curated 11/13, ABR variant
            name += ' (alt)'
        clean, headers = split_headers(url)
        add(num, name, attr, clean, headers)
        stats['world'] += 1

    header = '#EXTM3U'
    if args.epg_url:
        header += ' url-tvg="%s" x-tvg-url="%s"' % (args.epg_url, args.epg_url)

    maroc_lines = [line for _num, lines in sorted(maroc, key=lambda e: e[0]) for line in lines]

    out_m3u = os.path.join(args.out, args.out_name)
    with io.open(out_m3u, 'w', encoding='utf-8', newline='\n') as f:
        f.write(header + '\n' + '\n'.join(maroc_lines + body) + '\n')

    total = stats['curated'] + stats['world'] + stats['maroc_worker'] + stats['maroc_backup']
    print('wrote %s' % out_m3u)
    print('  channels      : %d  (curated %d + world %d + Worker %d + backup %d)' % (
        total, stats['curated'], stats['world'], stats['maroc_worker'], stats['maroc_backup']))
    print('  Maroc group   : %d  (%s)' % (len(maroc), ', '.join(str(n) for n, _ in sorted(maroc))))
    print('  groups        : %d' % len(groups))
    print('  headers moved : %d' % stats['headers_moved'])
    print('  logos         : %d rewritten, %d dropped' % (stats['logo_rewritten'], stats['logo_dropped']))
    print('  dropped       : %d' % len(dropped))
    for num, name, why in dropped:
        print('      %s  %s  - %s' % (num, name, why))
    if stats['headers_unknown']:
        print('  UNMAPPED HEADERS (check these):')
        for h in stats['headers_unknown']:
            print('      ' + h)
    return 0


if __name__ == '__main__':
    sys.exit(main())
