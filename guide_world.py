"""The programme guide for the world channels, built on the router.

The same guide as playlists/autocheck/epg.py on the laptop: the same sources (Samsung TV Plus
and EPGShare), the same matching of a playlist channel to a guide channel, the same ids
(rewritten to the playlist's own tvg-ids). It differs in how it reads them. epg.py holds every
source guide whole in memory, about 600 MB all told, which a router with 1 GB and no swap
cannot spare; this reads each one as a stream and keeps only the programmes of channels it
matched. The biggest source (US2, 80 MB of XML) takes about 18 s and 27 MB on the Flint2.

What it does with the guide that is already there (--keep):
  * a channel it matched this run replaces what the guide had for it;
  * every other channel is carried over as it was: the Maroc channels (guide_maroc.py owns
    them) and any channel whose source failed tonight, so a bad night costs nothing;
  * programmes that ended more than KEEP_PAST_HOURS ago are left out, everyone's.

The list is the one the playlist is generated from (sources/world_fr_en.m3u), whose group-title
is "language;country;category": the country picks the EPGShare files to search. The playlist
itself has more channels than that list (added by overrides, or from direct.m3u: Infos FR, the
Arab groups), and its group-titles are not countries ("Infos FR"), so --also takes it too and
reads each extra channel's country from the code that ends its tvg-id ("BFMBusiness.fr@SD").

usage: python3 -I guide_world.py LIST OUT [--also PLAYLIST] [--keep PREVIOUS]
                                 [--cache DIR [--max-age HOURS]]
       LIST      sources/world_fr_en.m3u
       OUT       the guide to write (gzipped XMLTV); may be the same file as PREVIOUS
       --also    playlist.m3u: its channels that LIST does not have are matched too
       --cache   keep the downloads in DIR and reuse those younger than --max-age (default 20);
                 without it they go to a temporary directory that is removed afterwards
"""

import argparse
import collections
import datetime
import gzip
import html
import os
import re
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET

SAMSUNG = ('samsung', 'https://i.mjh.nz/SamsungTVPlus/all.xml.gz')
EPGSHARE = 'https://epgshare01.online/epgshare01/epg_ripper_{}.xml.gz'
# playlist country -> EPGShare files to search (FAST-service guides are searched for every country)
COUNTRY_FILES = {
    'United States': ['US2'], 'United Kingdom': ['UK1'], 'France': ['FR1'], 'Canada': ['CA2'], 'Ireland': ['IE1'],
    'Australia': ['AU1'], 'New Zealand': ['NZ1'], 'India': ['IN1'], 'Nigeria': ['NG1'], 'Belgium': ['BE2'],
    'Switzerland': ['CH1'], 'South Africa': ['ZA1'],
}
FAST_FILES = ['RAKUTEN1', 'PLEX1', 'DISTROTV1']
# playlist channel name or tvg-id -> (guide file, guide channel id), where the names differ too much to
# match automatically or the country has no file of its own (.qa, .sa, .ma)
ALIASES = {'LCP': ('FR1', 'LCP-AN-PS.fr'),
           'Euronews FR': ('FR1', 'Euronews.fr'), 'BFM Var': ('FR1', 'BFM.TOULON.VAR.fr'),
           'BFM Marseille': ('FR1', 'BFM.MARSEILLE.PROVENCE.fr'),
           "BFM Cote d'Azur": ('FR1', "BFM.NICE.COTE.D'AZUR.fr"),
           # ids ending .qa (Qatar) have no country file to search; guide_maroc.py takes Al Jazeera
           # English from aljazeera.com, and this keeps the last one if that fails
           'Al Jazeera English': ('FR1', 'Al.Jazeera.English.fr'),
           'Al Jazeera Documentary': ('BE2', 'Al.Jazeera.Documentary.be'),
           # EPGShare's UAE file lists channels from all over the Arab world, Morocco included. Only
           # these, checked one by one against their programmes: it also lists channels that look right
           # and are not ("Chada TV" and "BBC Arabic" carry BBC News; Watan TV, Al Qamar TV, Al Wousta TV,
           # Al Maaref TV and Sharjah Radio all carry the same schedule), so no automatic matching.
           'AlAoula.ma': ('AE1', 'Al.Aoula.ae'),
           'SkyNewsArabia.ae@HD': ('AE1', 'Sky.News.Arabia.HD.ae'),
           'CNBCArabiya.ae@SD': ('AE1', 'CNBC.Arabiya.ae'),
           'KoogiTV.eg@SD': ('AE1', 'Koogi.TV.ae'),
           'AlMasriyah.eg@SD': ('AE1', 'Al.Masriyah.ae'),
           'SharjahTV.ae@SD': ('AE1', 'Sharjah.HD.ae'),
           'AlArabyTV.qa@SD': ('AE1', 'Al.Araby.HD.ae'),
           'AlArabyTV2.qa@SD': ('AE1', 'Al.Araby.2.HD.ae'),
           'AlRayyanTV.qa@SD': ('AE1', 'Al.Rayyan.HD.ae'),
           'AlRayyanOldTV.qa@SD': ('AE1', 'Al.Rayyan.Al.Qadeem.HD.ae'),
           'QatarTelevision.qa@SD': ('AE1', 'Qatar.TV.HD.ae'),
           'QatarTelevision2.qa@SD': ('AE1', 'Qatar.2.HD.ae')}


def alias_of(e):
    return ALIASES.get(e['id']) or ALIASES.get(e['name'])

# the country code that ends a tvg-id ("BFMBusiness.fr@SD") -> the country, for channels whose
# group-title says nothing about it
CC_COUNTRY = {'us': 'United States', 'uk': 'United Kingdom', 'fr': 'France', 'ca': 'Canada', 'ie': 'Ireland',
              'au': 'Australia', 'nz': 'New Zealand', 'in': 'India', 'ng': 'Nigeria', 'be': 'Belgium',
              'ch': 'Switzerland', 'za': 'South Africa'}

KEEP_PAST_HOURS = 12
ATTEMPTS = 3
RETRY_PAUSE = 20


def log(msg):
    print(msg, flush=True)


# ---------- reading ----------

def xmltv_seconds(s):
    """'20261028200000 +0100' -> seconds since 1970, or None."""
    m = re.match(r'\s*(\d{14})\s*([+-]\d{4})?', s or '')
    if not m:
        return None
    t = datetime.datetime.strptime(m.group(1), '%Y%m%d%H%M%S').replace(tzinfo=datetime.timezone.utc)
    off = m.group(2) or '+0000'
    delta = datetime.timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
    return (t - delta if off[0] == '+' else t + delta).timestamp()


def norm(s):
    s = unicodedata.normalize('NFKD', html.unescape(s)).encode('ascii', 'ignore').decode().lower()
    s = re.sub(r'\((united states|united kingdom|france|canada|[a-z ]+)\)', ' ', s)
    s = re.sub(r'\b(hd|sd|fhd|uhd|4k|tv|channel|television|live)\b', ' ', s)
    return re.sub(r'[^a-z0-9]', '', s)


def download(name, url, cache, max_age):
    path = os.path.join(cache, name + '.xml.gz')
    if max_age and os.path.exists(path) and time.time() - os.path.getmtime(path) < max_age * 3600:
        return path
    for attempt in range(ATTEMPTS):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            size = 0
            with urllib.request.urlopen(req, timeout=180) as r, open(path + '.tmp', 'wb') as f:
                want = r.headers.get('Content-Length')
                for block in iter(lambda: r.read(65536), b''):
                    f.write(block)
                    size += len(block)
            if want and int(want) != size:
                raise OSError('short read: %d of %s bytes' % (size, want))
            os.replace(path + '.tmp', path)
            return path
        except Exception:
            if attempt == ATTEMPTS - 1:
                raise
            time.sleep(RETRY_PAUSE)


CHANNEL = re.compile(rb'<channel id="([^"]+)"[^>]*>(.*?)</channel>', re.S)
DISPLAY_NAME = re.compile(rb'<display-name[^>]*>([^<]+)</display-name>')


def read_channels(path):
    """-> {id: [display names]} for every <channel> of a guide. The whole file is read, in
    blocks and without parsing it: Samsung's and PLEX's list some of their channels after
    their programmes, so stopping at the first programme would miss them."""
    channels, tail = {}, b''
    with gzip.open(path, 'rb') as f:
        while True:
            block = f.read(1 << 20)
            data = tail + block
            end = 0
            for m in CHANNEL.finditer(data):
                channels[html.unescape(m.group(1).decode('utf-8', 'replace'))] = [
                    html.unescape(n.decode('utf-8', 'replace')) for n in DISPLAY_NAME.findall(m.group(2))]
                end = m.end()
            if not block:
                return channels
            cut = data.rfind(b'<channel id=', end)      # a channel cut off by the end of the block
            tail = data[cut:] if cut != -1 else data[-12:]


class Guide:
    def __init__(self, name, path):
        self.name, self.path = name, path
        self.channels = read_channels(path)   # id -> [display names]
        self.by_key = collections.defaultdict(list)
        for cid, names in self.channels.items():
            base = re.sub(r'\.[a-z]{2}$', '', cid)
            for k in {norm(base)} | {norm(n) for n in names}:
                if k:
                    self.by_key[k].append(cid)

    def stream_programmes(self, want, cutoff):
        """The programmes of the guide channels in `want` ({guide id: [playlist ids]}), as XML
        strings with the channel rewritten to the playlist id. Raises if the file is cut short."""
        out = []
        with gzip.open(self.path, 'rb') as f:
            for _, el in ET.iterparse(f, events=('end',)):
                if el.tag == 'programme':
                    tids = want.get(el.get('channel'))
                    if tids:
                        stop = xmltv_seconds(el.get('stop')) or xmltv_seconds(el.get('start'))
                        if stop is None or stop >= cutoff:
                            el.tail = None
                            for tid in tids:
                                el.set('channel', tid)
                                out.append(ET.tostring(el, encoding='unicode'))
                    el.clear()
        return out


# ---------- the previous guide ----------

def carried_over(path, matched, cutoff):
    """-> (channel xml, programme xml) of the guide at `path` for every channel not in `matched`."""
    chans, progs = [], []
    with gzip.open(path, 'rb') as f:
        for _, el in ET.iterparse(f, events=('end',)):
            if el.tag == 'channel':
                if el.get('id') not in matched:
                    el.tail = None
                    chans.append(ET.tostring(el, encoding='unicode'))
                el.clear()
            elif el.tag == 'programme':
                if el.get('channel') not in matched:
                    stop = xmltv_seconds(el.get('stop')) or xmltv_seconds(el.get('start'))
                    if stop is None or stop >= cutoff:
                        el.tail = None
                        progs.append(ET.tostring(el, encoding='unicode'))
                el.clear()
    return chans, progs


# ---------- matching ----------

def read_list(path, country_from_id=False):
    """The channels of an m3u. The country is the middle part of the group-title, or with
    `country_from_id` the code that ends the tvg-id: the playlist's own group-titles are
    countries only in some groups ("France") and not in others ("Infos FR")."""
    ents = []
    for l in open(path, encoding='utf-8').read().split('\n'):
        if l.startswith('#EXTINF'):
            a = dict(re.findall(r'([\w-]+)="([^"]*)"', l))
            groups = a.get('group-title', '').split(';')
            country = groups[-2] if len(groups) >= 2 else ''
            if country_from_id:
                m = re.search(r'\.([a-z]{2})(@.*)?$', a.get('tvg-id', ''))
                country = CC_COUNTRY.get(m.group(1), '') if m else ''
            ents.append({'id': a.get('tvg-id', ''), 'name': l.rsplit(',', 1)[1], 'country': country})
    return ents


def match(ents, guides):
    """-> ({playlist id: (guide, guide channel id)}, how many by which rule)"""
    matches, how = {}, collections.Counter()
    samsung = guides.get('samsung')
    for e in ents:
        if not e['id']:
            continue
        if samsung and e['id'] in samsung.channels:
            matches[e['id']] = ('samsung', e['id']); how['samsung id'] += 1; continue
        alias = alias_of(e)
        if alias and alias[0] in guides and alias[1] in guides[alias[0]].channels:
            matches[e['id']] = alias; how['alias'] += 1; continue
        files = COUNTRY_FILES.get(e['country'], ['UK1', 'US2', 'FR1'] if e['country'] == 'International' else [])
        files = [f for f in files + FAST_FILES if f in guides]
        idbase = re.sub(r'\.[a-z]{2}(@.*)?$', '', e['id'])
        keys = [norm(idbase), norm(e['name'])]
        hit = None
        for f in files:
            for k in keys:
                if k and guides[f].by_key.get(k):
                    hit = (f, guides[f].by_key[k][0]); break
            if hit:
                break
        if hit:
            matches[e['id']] = hit; how['epgshare ' + ('fast' if hit[0] in FAST_FILES else 'country')] += 1
        else:
            how['no guide'] += 1
    return matches, how


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('list')
    ap.add_argument('out')
    ap.add_argument('--also', action='append', default=[], metavar='PLAYLIST',
                    help='a playlist whose other channels (those LIST does not have) get a guide too')
    ap.add_argument('--keep', help='the previous guide: its other channels are carried over')
    ap.add_argument('--cache')
    ap.add_argument('--max-age', type=float, default=20, help='hours a cached download is reused')
    ap.add_argument('--past-hours', type=float, default=KEEP_PAST_HOURS,
                    help='leave out programmes that ended longer ago than this')
    args = ap.parse_args()

    now = time.time()
    cutoff = now - args.past_hours * 3600
    log('guide_world %s' % datetime.datetime.now().isoformat(timespec='seconds'))
    ents = read_list(args.list)
    seen = {e['id'] for e in ents}
    for path in args.also:
        extra = [e for e in read_list(path, country_from_id=True) if e['id'] and e['id'] not in seen]
        seen |= {e['id'] for e in extra}
        ents += extra
        log('%s: %d more channels' % (os.path.basename(path), len(extra)))
    needed = set(FAST_FILES) | {f for e in ents for f in COUNTRY_FILES.get(e['country'], [])}
    needed |= {alias_of(e)[0] for e in ents if alias_of(e) and alias_of(e)[0] != 'samsung'}
    if any(e['country'] == 'International' for e in ents):
        needed |= {'UK1', 'US2', 'FR1'}
    sources = [SAMSUNG] + [('epgshare_' + f, EPGSHARE.format(f)) for f in sorted(needed)]

    cache = args.cache or tempfile.mkdtemp(prefix='tv-guide-world.')
    os.makedirs(cache, exist_ok=True)
    try:
        guides = {}
        for name, url in sources:
            key = name[len('epgshare_'):] if name.startswith('epgshare_') else name
            try:
                guides[key] = Guide(key, download(name, url, cache, args.max_age if args.cache else 0))
            except Exception as ex:
                log('warning: guide %s unavailable (%s: %s)' % (key, type(ex).__name__, str(ex)[:100]))
        if not guides:
            log('no source guide could be read: nothing written')
            return 1

        matches, how = match(ents, guides)
        by_guide = collections.defaultdict(dict)
        for tid, (g, cid) in matches.items():
            by_guide[g].setdefault(cid, []).append(tid)
        programmes = []
        for g, want in by_guide.items():
            try:
                got = guides[g].stream_programmes(want, cutoff)
            except Exception as ex:
                # Its channels count as unmatched, so they keep what the previous guide had.
                log('warning: guide %s unreadable (%s: %s)' % (g, type(ex).__name__, str(ex)[:100]))
                for tids in want.values():
                    for tid in tids:
                        del matches[tid]
                continue
            programmes += got
        if not programmes:
            log('no programmes: nothing written')
            return 1

        kept_chans, kept_progs = [], []
        if args.keep and os.path.exists(args.keep):
            try:
                kept_chans, kept_progs = carried_over(args.keep, set(matches), cutoff)
            except Exception as ex:
                log('warning: the previous guide is unreadable (%s: %s), not carried over'
                    % (type(ex).__name__, str(ex)[:100]))
        chans = []
        for tid, (g, cid) in matches.items():
            names = guides[g].channels[cid]
            el = ET.Element('channel', id=tid)
            ET.SubElement(el, 'display-name').text = names[0] if names else tid
            chans.append(ET.tostring(el, encoding='unicode'))

        tmp = args.out + '.tmp'
        with gzip.GzipFile(filename='', mode='wb', fileobj=open(tmp, 'wb'), mtime=0, compresslevel=9) as f:
            f.write('<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="tv-playlist guide_world.py">\n'
                    .encode('utf-8'))
            for line in chans + kept_chans + programmes + kept_progs:
                f.write(line.encode('utf-8') + b'\n')
            f.write(b'</tv>\n')
        os.replace(tmp, args.out)
        total = sum(1 for e in ents if e['id'])
        log('guide: %d/%d channels matched (%d%%), %d programmes | %s'
            % (len(matches), total, 100 * len(matches) // max(total, 1), len(programmes), dict(how)))
        log('carried over: %d channels, %d programmes' % (len(kept_chans), len(kept_progs)))
        return 0
    finally:
        if not args.cache:
            shutil.rmtree(cache, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
