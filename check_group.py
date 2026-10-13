#!/usr/bin/env python
"""Play-test every channel in a playlist group, from this PC.

    python -I check_group.py France                 # one group of playlist.m3u
    python -I check_group.py --list                 # what the groups are called
    python -I check_group.py --all --quiet          # whole playlist, failures only
    python -I check_group.py France --url https://raw.githubusercontent.com/oaatiq/tv-playlist/main/playlist.m3u

What it does per channel: fetch the URL, confirm it is really an m3u8, and for a master
playlist fetch one variant too and confirm it has segments. That is the same sequence a
player does on tune-in, so an OK here means the stream was serving seconds ago.

What it cannot tell you: whether the *box* can reach that host (the router filters per
device - see RUNBOOK section 6) or whether the box can decode it. A channel that is OK
here but dead on the TV is a path or decoder problem, not a playlist problem.

Exit code is 1 if anything failed, so it can gate a rebuild.
"""
import argparse, gzip, re, ssl, sys, urllib.parse, urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

UA = "VLC/3.0.20 LibVLC/3.0.20"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url, timeout, maxbytes=8192, extra=None):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    headers.update(extra or {})   # a channel with #EXTVLCOPT only answers if asked its way
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        body = r.read(maxbytes)
        if r.headers.get("Content-Encoding") == "gzip":
            try:
                body = gzip.decompress(body)
            except Exception:
                pass
        return r.status, r.geturl(), body.decode("utf-8", "replace")


def parse(text):
    """-> [{chno, name, group, url}] in file order."""
    lines = text.splitlines()
    out = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXTINF"):
            continue
        url = next((x for x in lines[i + 1:i + 8] if x and not x.startswith("#")), "")
        headers = {}
        for extra in lines[i + 1:i + 8]:
            if not extra.startswith("#"):
                break
            if "http-user-agent=" in extra:
                headers["User-Agent"] = extra.split("http-user-agent=", 1)[1].strip()
            elif "http-referrer=" in extra:
                headers["Referer"] = extra.split("http-referrer=", 1)[1].strip()
        m = re.search(r'group-title="([^"]*)"', line)
        c = re.search(r'tvg-chno="([^"]*)"', line)
        out.append(dict(chno=c.group(1) if c else "",
                        name=line.split(",", 1)[-1].strip(),
                        group=m.group(1) if m else "",
                        url=url, headers=headers))
    return out


def check(chan, timeout):
    def done(status, detail):
        return dict(chan, status=status, detail=detail)

    if not chan["url"]:
        return done("NOURL", "no stream line follows the #EXTINF")
    try:
        status, final, text = fetch(chan["url"], timeout, extra=chan["headers"])
    except Exception as e:
        return done("DEAD", f"{type(e).__name__}: {str(e)[:90]}")
    if status != 200:
        return done("DEAD", f"HTTP {status}")
    if "<MPD" in text[:400]:
        return done("OK", "dash mpd")          # ExoPlayer plays DASH; nothing more to probe
    if "#EXTM3U" not in text[:200]:
        return done("NOTM3U", repr(text[:60]))
    if "#EXT-X-STREAM-INF" in text:
        variant = next((l for l in text.splitlines() if l and not l.startswith("#")), None)
        if not variant:
            return done("EMPTY", "master playlist lists no variant")
        try:
            vstatus, _, vtext = fetch(urllib.parse.urljoin(final, variant), timeout,
                                      extra=chan["headers"])
        except Exception as e:
            return done("VARIANT", f"{type(e).__name__}: {str(e)[:80]}")
        if vstatus != 200 or "#EXTM3U" not in vtext[:200]:
            return done("VARIANT", f"variant HTTP {vstatus}")
        if "#EXTINF" not in vtext:
            return done("EMPTY", "variant carries no segments")
        return done("OK", "master + variant")
    if "#EXTINF" not in text:
        return done("EMPTY", "media playlist, no segments")
    return done("OK", "media")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("group", nargs="*", help="group-title to test, e.g. France")
    ap.add_argument("--playlist", default="playlist.m3u")
    ap.add_argument("--url", help="test a published playlist instead of the local file")
    ap.add_argument("--all", action="store_true", help="every group")
    ap.add_argument("--list", action="store_true", help="print group names and counts")
    ap.add_argument("--quiet", action="store_true", help="print failures only")
    ap.add_argument("--timeout", type=float, default=12)
    ap.add_argument("--workers", type=int, default=20)
    args = ap.parse_args()

    if args.url:
        text = fetch(args.url, 30, maxbytes=20_000_000)[2]
    else:
        text = open(args.playlist, encoding="utf-8").read()
    chans = parse(text)

    if args.list:
        for g, n in Counter(c["group"] for c in chans).most_common():
            print(f"{n:5}  {g}")
        return 0

    if not args.all:
        if not args.group:
            ap.error("name a group, or pass --all or --list")
        wanted = {g.casefold() for g in args.group}
        known = {c["group"].casefold() for c in chans}
        for g in wanted - known:
            print(f"no such group: {g!r} (try --list)", file=sys.stderr)
            return 2
        chans = [c for c in chans if c["group"].casefold() in wanted]

    with ThreadPoolExecutor(args.workers) as ex:
        res = list(ex.map(lambda c: check(c, args.timeout), chans))

    bad = [r for r in res if r["status"] != "OK"]
    for r in sorted(bad if args.quiet else res,
                    key=lambda r: (r["status"] == "OK", r["group"], int(r["chno"] or 0))):
        print(f'{r["status"]:8}{r["chno"]:>6}  {r["group"][:18]:<18} '
              f'{r["name"][:34]:<34} {r["detail"][:64]}')

    print(f'\n{len(res)} tested, {len(res) - len(bad)} OK, {len(bad)} failing  '
          f'({", ".join(f"{k} {v}" for k, v in Counter(r["status"] for r in res).most_common())})')
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
