"""Keep the programme guide current for the Maroc group and the Arab channels.

The world channels' guide (guide_world.py) has no Arab-world channels at all, so nothing in the
Maroc group, and no Arab channel, ever had a guide. This fetches the schedules of the channels
that have one somewhere, and merges them into the guide under the playlist's own tvg-ids, so
every player matches them without anything to configure: the playlist header already points at
that file. The wrapper (router/tv-guide-update.sh) runs it after guide_world.py and publishes
the result.

Sources, per channel, in order; the first that answers wins:
  * snrt.ma        the SNRT channels' own schedule pages (Arabic titles)
  * shahid.net     MBC's streaming service: one request a day for all its channels (Arabic titles)
  * aljazeera.com  Al Jazeera's own schedule page: a week or more in one request per channel
  * tv.sfr.fr      SFR's web guide, one file a day for all its channels (French titles)
  * orange.fr      Orange's guide API, one channel a day at a time (French titles)
  * prayer         no request at all: the five daily prayers at Makkah or Madinah, calculated,
                   for the two mosque channels, which no source has a schedule of (below)
They are the ones iptv-org/epg uses for these channels (sites/<site>/<site>.config.js there),
redone here with the standard library alone, because this runs on the router, which has
python3 and nothing else.

What the merge does to the rest of the file:
  * a channel this run fetched replaces what the file had for it;
  * a channel no source answered for keeps what the file had, so a bad night costs nothing;
  * programmes that ended more than KEEP_PAST_HOURS ago are dropped, everyone's: a guide
    file is for what is on now and next.
The output is gzipped without a timestamp, so a run that changes nothing writes the same
bytes and there is nothing to publish.

usage: python3 -I guide_maroc.py GUIDE             # fetch and merge into GUIDE (epg.xml.gz)
       python3 -I guide_maroc.py GUIDE --dry-run   # fetch and report, write nothing
"""

import argparse
import datetime
import gzip
import html
import io
import json
import math
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

# The guide ids are the playlist's tvg-ids, so players match them exactly. `also` are further
# ids the same schedule is published under: other entries of the playlist that show the same
# channel, with the minutes their feed runs late (2M Monde HD is the +1 hour feed).
CHANNELS = [
    {'id': 'AlAoula.ma', 'name': 'Al Aoula',
     'sources': [('snrt', '1208'), ('sfr', '769'), ('orange', '90504')]},
    {'id': 'LaayouneTV.ma', 'name': 'Al Aoula Laâyoune', 'sources': [('snrt', '4069')]},
    {'id': 'Arryadia.ma', 'name': 'Arryadia', 'sources': [('snrt', '4070')]},
    {'id': 'Athaqafia.ma', 'name': 'Athaqafia', 'sources': [('snrt', '4071')]},
    {'id': 'Assadissa.ma', 'name': 'Assadissa', 'sources': [('snrt', '4073')]},
    {'id': 'TamazightTV.ma', 'name': 'Tamazight', 'sources': [('snrt', '4075')]},
    {'id': '2MMonde.ma', 'name': '2M Monde',
     'sources': [('sfr', '340'), ('orange', '340')],
     'also': [('2MMonde.ma@Plus1', 60)]},
    {'id': 'Medi1TVAfrique.ma', 'name': 'Medi1 Afrique',
     'sources': [('sfr', '665'), ('orange', '90242')],
     'also': [('Medi1TVAfrique.ma@SD', 0)]},
    {'id': 'ChadaTV.ma@SD', 'name': 'Chada TV', 'sources': [('sfr', '3774')]},
    {'id': 'AsharqNews.sa@SD', 'name': 'Asharq News', 'sources': [('shahid', '862837'), ('sfr', '3726')]},
    {'id': 'AlJazeera.qa@Arabic', 'name': 'Al Jazeera', 'sources': [('aljazeera', 'aja#')]},
    {'id': 'AlJazeera.qa@English', 'name': 'Al Jazeera English', 'sources': [('aljazeera', 'aje#')]},
    {'id': 'AlJazeera2.qa@HD', 'name': 'Al Jazeera 2', 'sources': [('aljazeera', 'aja#aj2')]},
]

# The Arab channels Shahid has the schedule of: (tvg-id, name, Shahid channel id), from
# iptv-org's guides.json. A channel Shahid lists but has nothing for (a slot that reads
# "schedule not available") is skipped like any other source that does not answer, so those stay
# until Shahid has something for them.
SHAHID = [
    ('Aflam.sa@SD', 'Aflam', '989622'),
    ('AlArabiyaBusiness.ae@SD', 'Al Arabiya Business', '1003218'),
    ('AlArabiyaPrograms.ae@SD', 'Al Arabiya Programs', '49923733029294'),
    ('AlEkhbariya.sa@SD', 'Al Ekhbariya', '946948'),
    ('AlHadath.sa@SD', 'Al Hadath', '387288'),
    ('AlSaudiya.sa@SD', 'Al Saudiya', '946938'),
    ('AlSaudiyaAlaan.sa@SD', 'Al Saudiya Alaan', '999927'),
    ('Alarabiya.ae@SD', 'Al Arabiya', '387286'),
    ('AlkhuzamaRadio.sa@SD', 'Alkhuzama Radio', '1029746'),
    ('AsharqDiscovery.sa@SD', 'Asharq Discovery', '1001845'),
    ('AsharqDocumentary.sa@SD', 'Asharq Documentary', '997605'),
    ('BabAlHara.sa@SD', 'Bab Al Hara', '975435'),
    ('BigTimePlus.sa@SD', 'Big Time Plus', '49922904934759'),
    ('FIFAPlus.uk@French', 'FIFA+ French', '49923446898171'),
    ('Fairuz.sa@HD', 'Fairuz', '49923172215352'),
    ('MBC1.ae@SD', 'MBC 1', '387238'),
    ('MBC1Egypt.eg@HD', 'MBC 1 Egypt', '387248'),
    ('MBC1USA.us@SD', 'MBC 1 USA', '816764'),
    ('MBC3USA.us@SD', 'MBC 3 USA', '816771'),
    ('MBC4.ae@SD', 'MBC 4', '400919'),
    ('MBC5.ae@SD', 'MBC 5', '387937'),
    ('MBCBollywood.ae@SD', 'MBC Bollywood', '409387'),
    ('MBCDrama.ae@SD', 'MBC Drama (United Arab Emirates)', '387251'),
    ('MBCFM.ae@SD', 'MBC FM', '388567'),
    ('MBCMasr.eg@SD', 'MBC Masr', '387290'),
    ('MBCMasr2.eg@SD', 'MBC Masr 2', '387293'),
    ('MBCMasrDrama.sa@SD', 'MBC Masr Drama', '49923122575716'),
    ('MBCPlusDrama.sa@SD', 'MBC+ Drama', '387296'),
    ('MajidAlMohandis.sa@SD', 'Majid Al Mohandis', '49922763891977'),
    ('Maraya.sa@HD', 'Maraya', '988045'),
    ('MoviesAction.sa@SD', 'Movies Action', '986064'),
    ('MoviesThriller.sa@SD', 'Movies Thriller', '986069'),
    ('PanoramaFM.sa@SD', 'Panorama FM', '388566'),
    ('RabehSaqer.sa@HD', 'Rabeh Saqer', '49923172117967'),
    ('SBC.sa@SD', 'SBC', '946940'),
    ('SaudiThaqafiyaTV.sa@SD', 'Saudi Thaqafiya TV', '995495'),
    ('Tarab.sa@SD', 'Tarab', '49922763510387'),
]
CHANNELS += [{'id': i, 'name': n, 'sources': [('shahid', s)]} for i, n, s in SHAHID]

# The two mosque channels: Masjid al-Haram and Masjid an-Nabawi, live round the clock. Nobody
# publishes a schedule of them. Checked 2026-11-12: Shahid lists both and answers "schedule not
# available" for every day; EPGShare's AE1 has "Saudi Quran TV HD" and "Saudi Sunna TV HD"
# carrying Cartoon Network's programmes (the same schedule as five other channels), and an
# "en: Saudi Quran" that is one title in four-hour blocks. What is actually on is known all the
# same - the five prayers - so the guide is calculated from the sun (the `prayer` source below).
# Shahid is asked first: should it ever have the real schedule, that wins by itself.
CHANNELS += [
    {'id': 'AlQuranAlKareemTV.sa@SD', 'name': 'Makkah Live',
     'sources': [('shahid', '946946'), ('prayer', 'makkah')]},
    {'id': 'AlSunnahAlNabawiyahTV.sa@SD', 'name': 'Madinah Live',
     'sources': [('shahid', '946942'), ('prayer', 'madinah')]},
]

KEEP_PAST_HOURS = 12
DAYS = 2                    # today and tomorrow
TIMEOUT = 60
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36')


def log(msg):
    print(msg, flush=True)


# ---------- time ----------

def casablanca():
    """Morocco's time zone: plain UTC, all year. It left +1 (and the Ramadan change) for UTC on
    2026-09-20, so the schedules' local times are UTC and no tz database is needed."""
    return datetime.timezone.utc


def xmltv_time(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime('%Y%m%d%H%M%S +0000')


def parse_xmltv_time(s):
    """'20261028200000 +0100' -> seconds since 1970, or None."""
    m = re.match(r'\s*(\d{14})\s*([+-]\d{4})?', s or '')
    if not m:
        return None
    t = datetime.datetime.strptime(m.group(1), '%Y%m%d%H%M%S')
    off = m.group(2) or '+0000'
    delta = datetime.timedelta(hours=int(off[1:3]), minutes=int(off[3:5]))
    t = t.replace(tzinfo=datetime.timezone.utc) - (delta if off[0] == '+' else -delta)
    return t.timestamp()


# ---------- fetching ----------

def get(url, headers=None):
    hdr = {'User-Agent': UA, 'Accept': '*/*'}
    hdr.update(headers or {})
    req = urllib.request.Request(url, headers=hdr)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = r.read()
        if r.headers.get('Content-Encoding') == 'gzip':
            data = gzip.decompress(data)
        return data


def clean(text):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', ' ', text or ''))).strip()


# ---------- snrt.ma ----------

SNRT_LINE = re.compile(r'class="[^"]*grille-line\s+(\d{8})[^"]*"')


def snrt_parse(page, tz):
    """The schedule page of one SNRT channel -> programmes. The page holds a few days, one
    block per programme, each with its date in its class and its time as "20H45"; a
    programme runs until the next one starts."""
    starts = [m for m in SNRT_LINE.finditer(page)]
    items = []
    for i, m in enumerate(starts):
        block = page[m.end():starts[i + 1].start() if i + 1 < len(starts) else len(page)]
        tm = re.search(r'grille-time[^>]*>(.*?)</div>', block, re.S)
        hm = re.search(r'(\d{1,2})\s*H\s*(\d{2})', clean(tm.group(1)) if tm else '')
        title = re.search(r'program-title-sm[^>]*>(.*?)</', block, re.S)
        if not hm or not title or not clean(title.group(1)):
            continue
        day = datetime.datetime.strptime(m.group(1), '%Y%m%d')
        local = day.replace(hour=int(hm.group(1)) % 24, minute=int(hm.group(2)), tzinfo=tz)
        desc = re.search(r'program-description-sm[^>]*>(.*?)</', block, re.S)
        genre = re.search(r'genre-first[^>]*>(.*?)</', block, re.S)
        items.append({'start': local.timestamp(), 'title': clean(title.group(1)), 'lang': 'ar',
                      'desc': clean(desc.group(1)) if desc else '',
                      'category': clean(genre.group(1)) if genre else ''})
    items.sort(key=lambda p: p['start'])
    out = []
    for i, p in enumerate(items):
        if out and out[-1]['start'] == p['start']:
            continue
        if i + 1 < len(items):
            p['stop'] = items[i + 1]['start']
        else:
            day_end = datetime.datetime.fromtimestamp(p['start'], tz).replace(
                hour=0, minute=0, second=0) + datetime.timedelta(days=1)
            p['stop'] = day_end.timestamp()
        if p['stop'] > p['start']:
            out.append(p)
    return out


def snrt(site_id, ctx):
    page = get('https://www.snrt.ma/ar/node/%s' % site_id).decode('utf-8', 'replace')
    return snrt_parse(page, ctx['tz'])


# ---------- tv.sfr.fr ----------

def sfr_parse(day_json, site_id):
    out = []
    for item in day_json.get('epg', {}).get(site_id) or []:
        start, stop = item.get('startDate'), item.get('endDate')
        title = clean(item.get('title'))
        if not start or not stop or not title or stop <= start:
            continue
        out.append({'start': start / 1000.0, 'stop': stop / 1000.0, 'title': title, 'lang': 'fr',
                    'desc': clean(item.get('longSynopsis')), 'category': clean(item.get('genre'))})
    return out


def sfr(site_id, ctx):
    """One file a day holds every SFR channel (about 10 MB): fetched once a run and shared."""
    out = []
    for day in ctx['days']:
        key = 'sfr:' + day
        if key not in ctx['cache']:
            try:
                url = 'https://static-cdn.tv.sfr.net/data/epg/gen8/guide_web_%s.json' % day
                ctx['cache'][key] = json.loads(get(url).decode('utf-8'))
            except Exception as e:
                ctx['cache'][key] = e
        data = ctx['cache'][key]
        if isinstance(data, Exception):
            raise data
        out.extend(sfr_parse(data, site_id))
    return out


# ---------- chaines-tv.orange.fr ----------

def orange_parse(data, site_id):
    out = []
    for item in data.get(site_id) or []:
        start, length = item.get('diffusionDate'), item.get('duration')
        title = clean(item.get('title'))
        if not start or not length or not title:
            continue
        out.append({'start': float(start), 'stop': float(start + length), 'title': title,
                    'lang': 'fr', 'desc': clean(item.get('synopsis')),
                    'category': clean(item.get('genreDetailed') or item.get('genre'))})
    return out


def orange(site_id, ctx):
    out = []
    for day in ctx['days']:
        start = datetime.datetime.strptime(day, '%Y%m%d').replace(tzinfo=ctx['tz'])
        a = int(start.timestamp() * 1000)
        b = a + 86400 * 1000
        url = ('https://rp-ott-mediation-tv.woopic.com/api-gw/live/v3/applications/STB4PC/'
               'programs?groupBy=channel&includeEmptyChannels=false&period=%d,%d&after=%s&limit=1'
               % (a, b, site_id))
        out.extend(orange_parse(json.loads(get(url, {'Accept': 'application/json'})
                                           .decode('utf-8')), site_id))
    return out


# ---------- api2.shahid.net ----------

def shahid_parse(day_json, site_id):
    out = []
    for ch in day_json.get('items') or []:
        if str(ch.get('channelId')) != site_id:
            continue
        for item in ch.get('items') or []:
            if item.get('emptySlot'):           # "schedule not available", not a programme
                continue
            try:
                start = datetime.datetime.fromisoformat(item['actualFrom']).timestamp()
                stop = datetime.datetime.fromisoformat(item['actualTo']).timestamp()
            except (KeyError, TypeError, ValueError):
                continue
            title = clean(item.get('title'))
            if not title or stop <= start:
                continue
            genres = [g for g in item.get('genres') or [] if g]
            out.append({'start': start, 'stop': stop, 'title': title, 'lang': 'ar',
                        'desc': clean(item.get('description')),
                        'category': clean(genres[0]) if genres else ''})
    return out


def shahid(site_id, ctx):
    """One request a day holds every Shahid channel the guide wants (about 1 MB): fetched once
    a run and shared. The times are UTC."""
    ids = ','.join(sid for ch in CHANNELS for site, sid in ch['sources'] if site == 'shahid')
    seen, out = set(), []
    for day in ctx['days']:
        key = 'shahid:' + day
        if key not in ctx['cache']:
            try:
                d = datetime.datetime.strptime(day, '%Y%m%d').strftime('%Y-%m-%d')
                url = ('https://api2.shahid.net/proxy/v2.1/shahid-epg-api/?csvChannelIds=%s'
                       '&from=%sT00:00:00.000Z&to=%sT23:59:59.999Z&country=SA&language=ar'
                       '&Accept-Language=ar' % (ids, d, d))
                ctx['cache'][key] = json.loads(get(url, {'Accept': 'application/json'}).decode('utf-8'))
            except Exception as e:
                ctx['cache'][key] = e
        data = ctx['cache'][key]
        if isinstance(data, Exception):
            raise data
        for p in shahid_parse(data, site_id):
            if (p['start'], p['stop']) not in seen:     # a programme over midnight is in both days
                seen.add((p['start'], p['stop']))
                out.append(p)
    return out


# ---------- aljazeera.com ----------

def aljazeera_parse(data, lang):
    items = []
    for it in ((data.get('data') or {}).get('post') or {}).get('schedule') or []:
        try:
            day = int(it['startDate'])                  # midnight UTC of the day, in seconds
            hour, minute = it['showTimeslot'].split(':')
            start = day + int(hour) * 3600 + int(minute) * 60
            d = re.match(r'(\d+):(\d+)(?::(\d+))?', it['duration'])
            # the last slot of a Friday says 23:59:59: no slot runs past its own day
            stop = min(start + int(d.group(1)) * 3600 + int(d.group(2)) * 60 + int(d.group(3) or 0),
                       day + 86400)
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        title = clean(it.get('showName'))
        if title:
            items.append({'start': start, 'stop': stop, 'title': title, 'lang': lang,
                          'desc': clean(it.get('showDescription')), 'category': ''})
    items.sort(key=lambda p: p['start'])
    items = [p for i, p in enumerate(items) if i == 0 or p['start'] != items[i - 1]['start']]
    for i, p in enumerate(items[:-1]):
        p['stop'] = min(p['stop'], items[i + 1]['start'])
    return [p for p in items if p['stop'] > p['start']]


def aljazeera(site_id, ctx):
    """The query aljazeera.com's own schedule page makes: GraphQL over GET, a week or more in
    one request per channel. `site_id` is "<site>#<suffix>" as in iptv-org's grabber (aja is
    Arabic, aje English, aja#aj2 the second channel). The slot times are UTC."""
    site, suffix = site_id.split('#')
    variables = json.dumps({'postName': 'schedule-' + suffix if suffix else 'schedule', 'preview': ''},
                           separators=(',', ':'))
    url = ('https://www.aljazeera.com/graphql?wp-site=%s&operationName=ArchipelagoSchedulePageQuery'
           '&variables=%s&extensions=%%7B%%7D' % (site, urllib.parse.quote(variables)))
    data = json.loads(get(url, {'wp-site': site, 'Accept': 'application/json'}).decode('utf-8'))
    return aljazeera_parse(data, 'en' if site == 'aje' else 'ar')


# ---------- prayer times, calculated ----------

# (latitude, longitude, the mosque, the city) - Saudi Arabia is on UTC+3 all year.
MOSQUES = {'makkah': (21.4225, 39.8262, 'Masjid al-Haram', 'Makkah'),
           'madinah': (24.4672, 39.6111, 'Masjid an-Nabawi', 'Madinah')}
SAUDI_UTC = 3
PRAYERS = [('fajr', 'Fajr', 45), ('dhuhr', 'Dhuhr', 40), ('asr', 'Asr', 40),
           ('maghrib', 'Maghrib', 35), ('isha', 'Isha', 45)]     # key, name, minutes shown


def hijri_month(date):
    """The month of the Islamic year, by the arithmetic (tabular) calendar: a day or two from
    the sighted one at worst. Only used to know whether it is Ramadan (month 9)."""
    days = date.toordinal() - 227015                    # days since 1 Muharram AH 1
    cycle, rest = divmod(days, 10631)                    # 30 years
    year = 0
    for year in range(30):
        length = 355 if (11 * (year + 1) + 14) % 30 < 11 else 354
        if rest < length:
            break
        rest -= length
    month = 0
    for month in range(12):
        length = 30 if month % 2 == 0 or (month == 11 and (11 * (year + 1) + 14) % 30 < 11) else 29
        if rest < length:
            break
        rest -= length
    return month + 1


def prayer_times(date, lat, lng):
    """-> {'fajr': seconds since 1970, ...} for that local day, the way the Umm al-Qura calendar
    does it: Fajr with the sun 18.5 degrees under the horizon, Dhuhr when it crosses the
    meridian, Asr when a shadow is its object's length longer than at noon, Maghrib at sunset,
    Isha 90 minutes after Maghrib (120 in Ramadan). The sun's position is the low-precision
    formula of the Astronomical Almanac, good to a fraction of a minute until 2050."""
    rad, deg = math.radians, math.degrees
    # days since 2000-01-01 12:00 UTC, at local noon
    d = date.toordinal() - datetime.date(2000, 1, 1).toordinal() - lng / 360.0
    g = rad((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    L = rad((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    e = rad(23.439 - 0.00000036 * d)
    ra = deg(math.atan2(math.cos(e) * math.sin(L), math.cos(L))) % 360 / 15.0
    decl = math.asin(math.sin(e) * math.sin(L))
    eqt = q / 15.0 - ra
    eqt -= 24 * round(eqt / 24.0)                        # hours, a quarter of an hour at most
    noon = 12 + SAUDI_UTC - lng / 15.0 - eqt             # local clock time of the meridian

    def arc(altitude):                                   # hours from noon to that altitude
        x = (math.sin(rad(altitude)) - math.sin(rad(lat)) * math.sin(decl)) / (
            math.cos(rad(lat)) * math.cos(decl))
        return deg(math.acos(x)) / 15.0
    asr_altitude = deg(math.atan(1.0 / (1.0 + math.tan(abs(rad(lat) - decl)))))
    maghrib = noon + arc(-0.833)
    hours = {'fajr': noon - arc(-18.5), 'dhuhr': noon, 'asr': noon + arc(asr_altitude),
             'maghrib': maghrib, 'isha': maghrib + (2.0 if hijri_month(date) == 9 else 1.5)}
    midnight = (datetime.datetime(date.year, date.month, date.day, tzinfo=datetime.timezone.utc).timestamp()
                - SAUDI_UTC * 3600)
    return {k: midnight + round(h * 60) * 60 for k, h in hours.items()}


def prayer(site_id, ctx):
    """A schedule for a channel that shows a mosque all day: each prayer from its adhan, and
    the live picture in between, saying which prayer is next and when. Nothing is fetched."""
    lat, lng, mosque, city = MOSQUES[site_id]
    local = datetime.timezone(datetime.timedelta(hours=SAUDI_UTC))
    clock = lambda t: datetime.datetime.fromtimestamp(t, local).strftime('%H:%M')
    note = ('Prayer times are calculated for %s (Umm al-Qura method), not taken from the '
            'broadcaster; they can be a minute or two out.' % city)
    first = datetime.datetime.strptime(ctx['days'][0], '%Y%m%d').date()
    events = []                                          # (start, stop, name), in order
    for n in range(-1, len(ctx['days']) + 1):            # a day either side: the nights join up
        date = first + datetime.timedelta(days=n)
        times = prayer_times(date, lat, lng)
        for key, name, minutes in PRAYERS:
            if key == 'dhuhr' and date.weekday() == 4:   # Friday: the sermon, then the prayer
                name, minutes = "Jumu'ah", 60
            events.append((times[key], times[key] + minutes * 60, name))
    out = []
    for (start, stop, name), (next_start, _, next_name) in zip(events, events[1:]):
        out.append({'start': start, 'stop': stop, 'title': '%s prayer - %s' % (name, mosque), 'lang': 'en',
                    'desc': '%s adhan at %s %s time. %s' % (name, clock(start), city, note),
                    'category': 'Religion'})
        out.append({'start': stop, 'stop': next_start, 'title': 'Live from %s' % mosque, 'lang': 'en',
                    'desc': 'Next: %s at %s %s time. %s' % (next_name, clock(next_start), city, note),
                    'category': 'Religion'})
    return out


SOURCES = {'snrt': snrt, 'shahid': shahid, 'aljazeera': aljazeera, 'sfr': sfr, 'orange': orange,
           'prayer': prayer}


def placeholder(programmes):
    """Whether a schedule is only a stand-in: SFR lists Medi1 TV and Al Aoula as the channel's
    own description in three-hour blocks ("Chaîne nationale marocaine" all day). That would
    show as what is on now, which is worse than showing nothing."""
    titles = [p['title'] for p in programmes]
    return len(titles) >= 4 and len(set(titles)) <= len(titles) // 4


def fetch_all(now):
    """-> {guide id: programmes} for every channel some source answered for."""
    tz = casablanca()
    today = datetime.datetime.fromtimestamp(now, tz)
    ctx = {'tz': tz, 'cache': {},
           'days': [(today + datetime.timedelta(days=d)).strftime('%Y%m%d') for d in range(DAYS)]}
    fetched = {}
    for ch in CHANNELS:
        programmes = None
        for site, site_id in ch['sources']:
            try:
                got = SOURCES[site](site_id, ctx)
            except Exception as e:
                log('  %-22s %-6s failed: %s' % (ch['id'], site, str(e)[:120]))
                continue
            # A source that answers with nothing current is no better than one that fails.
            got = [p for p in got if p['stop'] > now - KEEP_PAST_HOURS * 3600]
            # SFR's blocks are one title all day; Shahid's and Al Jazeera's real channels repeat theirs
            # (and `prayer` shows the same live picture between prayers, which is what is on)
            if site not in ('shahid', 'aljazeera', 'prayer') and placeholder(got):
                log('  %-22s %-6s only a placeholder ("%s")' % (ch['id'], site, got[0]['title'][:40]))
                continue
            if any(p['stop'] > now for p in got):
                programmes = sorted(got, key=lambda p: p['start'])
                log('  %-22s %-6s %d programmes, to %s' % (
                    ch['id'], site, len(programmes),
                    datetime.datetime.fromtimestamp(programmes[-1]['stop'], tz).strftime('%a %H:%M')))
                break
            log('  %-22s %-6s nothing current' % (ch['id'], site))
        if programmes is None:
            log('  %-22s no source: keeping what the guide has' % ch['id'])
            continue
        fetched[ch['id']] = (ch['name'], programmes, 0)
        for alias, late in ch.get('also', []):
            fetched[alias] = (ch['name'], programmes, late * 60)
    return fetched


# ---------- merging ----------

def merge(old_bytes, fetched, now):
    """The old guide with every channel in `fetched` replaced, and everything that ended
    more than KEEP_PAST_HOURS ago left out. -> (xml bytes, stats)."""
    cutoff = now - KEEP_PAST_HOURS * 3600
    out = io.BytesIO()
    out.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
    root_attrs = {}
    channels, programmes, kept, dropped = [], [], 0, 0
    if old_bytes:
        for event, el in ET.iterparse(io.BytesIO(old_bytes), events=('start', 'end')):
            if event == 'start':
                if el.tag == 'tv' and not root_attrs:
                    root_attrs = dict(el.attrib)
                continue
            if el.tag == 'channel':
                if el.get('id') not in fetched:
                    channels.append(ET.tostring(el, encoding='utf-8'))
                el.clear()
            elif el.tag == 'programme':
                stop = parse_xmltv_time(el.get('stop')) or parse_xmltv_time(el.get('start'))
                if el.get('channel') in fetched or (stop is not None and stop < cutoff):
                    dropped += 1
                else:
                    programmes.append(ET.tostring(el, encoding='utf-8'))
                    kept += 1
                el.clear()
    attrs = root_attrs or {'generator-info-name': 'tv-playlist guide_maroc.py'}
    out.write(b'<tv' + b''.join(b' %s="%s"' % (k.encode('utf-8'), html.escape(v).encode('utf-8'))
                                for k, v in attrs.items()) + b'>\n')
    for c in channels:
        out.write(c.strip() + b'\n')
    added = 0
    for gid, (name, _, _) in sorted(fetched.items()):
        el = ET.Element('channel', id=gid)
        ET.SubElement(el, 'display-name').text = name
        out.write(ET.tostring(el, encoding='utf-8') + b'\n')
    for p in programmes:
        out.write(p.strip() + b'\n')
    for gid, (name, progs, late) in sorted(fetched.items()):
        for p in progs:
            el = ET.Element('programme', start=xmltv_time(p['start'] + late),
                            stop=xmltv_time(p['stop'] + late), channel=gid)
            ET.SubElement(el, 'title', lang=p['lang']).text = p['title']
            if p.get('desc'):
                ET.SubElement(el, 'desc', lang=p['lang']).text = p['desc']
            if p.get('category'):
                ET.SubElement(el, 'category', lang=p['lang']).text = p['category']
            out.write(ET.tostring(el, encoding='utf-8') + b'\n')
            added += 1
    out.write(b'</tv>\n')
    return out.getvalue(), {'kept': kept, 'dropped': dropped, 'added': added,
                            'channels': len(channels) + len(fetched)}


def gz(data):
    """Gzipped with no name and no time in the header: the same guide, the same bytes."""
    buf = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=buf, mtime=0, compresslevel=9) as f:
        f.write(data)
    return buf.getvalue()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('guide', help='the guide to merge into (epg.xml.gz)')
    ap.add_argument('--dry-run', action='store_true', help='fetch and report, write nothing')
    args = ap.parse_args()
    guide = args.guide

    now = time.time()
    log('guide_maroc %s' % datetime.datetime.now().isoformat(timespec='seconds'))
    fetched = fetch_all(now)
    main_ids = [c['id'] for c in CHANNELS]
    got = [i for i in main_ids if i in fetched]
    log('fetched %d of %d channels' % (len(got), len(main_ids)))

    old = b''
    if os.path.exists(guide):
        with open(guide, 'rb') as f:
            old = f.read()
        try:
            old = gzip.decompress(old)
        except OSError:
            pass
    try:
        xml, st = merge(old, fetched, now)
    except ET.ParseError as e:
        # The guide there is unreadable (a broken upload, say): start again from the Maroc
        # channels alone rather than refuse every run from now on.
        log('the guide there does not parse (%s): starting from an empty one' % e)
        xml, st = merge(b'', fetched, now)
    try:
        ET.fromstring(xml)          # never publish a guide that does not parse
    except ET.ParseError as e:
        log('the merged guide does not parse, nothing written: %s' % e)
        return 1
    log('guide: %d channels; %d programmes kept, %d dropped (old or replaced), %d added'
        % (st['channels'], st['kept'], st['dropped'], st['added']))

    data = gz(xml)
    if data == (open(guide, 'rb').read() if os.path.exists(guide) else None):
        log('nothing changed')
        return 0
    if args.dry_run:
        log('dry run: %d bytes not written' % len(data))
        return 0
    tmp = guide + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, guide)
    log('written: Maroc %d of %d channels, %d programmes' % (len(got), len(main_ids), st['added']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
