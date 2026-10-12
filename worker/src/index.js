// Moroccan live TV helper for OTT Player on the MINIX U8K.
//
// SNRT (EasyBroadcast) checks a 1-hour token on every .m3u8 request; the .ts segments are open.
// The master's variant paths are relative, so a token appended once is lost on the variant
// request. This Worker mints and caches the token, serves the master and variant playlists
// itself, and rewrites segment URIs to absolute CDN URLs, so video goes straight from the
// CDN to the box and only the small playlists pass through here.
//
// Télé Maroc's Cloudflare Stream URL is signed (JWT in the path, ~12 h). /telemaroc.m3u8
// resolves it from telemaroc.tv the same way the Catch-up TV & More add-on does and redirects.

const CDN = 'https://cdn.live.easybroadcast.io/abr_corp/';
const FEEDS_API = 'https://snrtlive.atlashoster.net/api/feeds-by-category?lang=fr&category=3';
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36';

// slug -> EasyBroadcast event. Re-read from the SNRT API if an event stops resolving.
const SNRT = {
  'aloula': '73_aloula_w1dqfwm',
  'laayoune': '73_laayoune_pgagr52',
  'arryadia': '73_arryadia_k2tgcj0',
  'arryadia-tnt': '73_arryadia-tnt_zcmwjdc',
  'arryadia-hd1': '133_arryadia-hd1_gmrvdlq',
  'athaqafia': '73_arrabia_hthcj4p',
  'almaghribia': '73_almaghribia_83tz85q',
  'assadissa': '73_assadissa_7b7u5n1',
  'tamazight': '73_tamazight_tccybxt',
};

// Channels whose CDN checks a Referer or User-Agent on *every* request, segments
// included. XCIPTV does worse than ignore #EXTVLCOPT: it hands the line to Cronet in
// place of the URL and the tune-in dies with net::ERR_INVALID_URL, so a playlist that
// carries the header breaks the channel outright. This Worker attaches it instead and
// the player sees plain URLs.
//
// Re-measured 2026-10-12, every header-carrying entry fetched with and without its
// header: the BFM channels, Rai Italia, Tele 7 and S Play Event answer identically bare
// and stay direct (their #EXTVLCOPT lines are now stripped in the playlist). AXN CEE
// does NOT - bare it serves an HTML 301 - so it joins the proxy here, with Universal
// Comedy, Universal Crime and TV3 Ghana, which all 403 without their header.
//
// Unlike the SNRT routes, segments can't be handed straight to the CDN here: they are
// protected too, so the video itself passes through the Worker. Checked 2026-10-11.
const PROXY = {
  '2m-monde': {
    url: 'https://cdn-globecast.akamaized.net/live/eds/2m_monde/hls_video_ts_tuhawxpiemz257adfc/2m_monde.m3u8',
    ref: 'http://www.radio2m.ma/',
    hosts: ['cdn-globecast.akamaized.net'],
  },
  'military-history': {
    url: 'https://frndly.keralive.workers.dev/v1/master/840a24ccd7c75076211d060179b3ad0c64fdc6ca/militaryhistory_live_nr/ab44a8245afe413db1169317ee5cda08/index.m3u8?type=ae2',
    ref: 'https://bhoomtv.org/',
    ua: 'Mozilla/5.0 (Windows NT 10.0 Win64 x64) AppleWebKit/537.36 (KHTML like Gecko) Chrome/154.0.0.0 Safari/537.36',
    hosts: ['frndly.keralive.workers.dev', 'sr-live-ae2.akamaized.net'],
  },
  'press-tv': {
    url: 'https://cloud.odysee.live/content/26bcbcc342a5b143578f27dbfaf000201f06e417/master.m3u8',
    ref: 'https://odysee.com/',
    hosts: ['cloud.odysee.live'],
  },
  'music-box': {
    url: 'http://777905.live.tvstitch.com/playlist.m3u8?source=aHR0cHM6Ly9obHMtY2RuMS5keXZ5YXBwLmNvbS9NdXNpYy1Cb3gvdHJhY2tzLXYxYTEvbW9uby50cy5tM3U4P3Rva2VuPWV5SmhiR2NpT2lKSVV6STFOaUlzSW5SNWNDSTZJa3BYVkNKOS5leUpqYUdGdWJtVnNYMmxrSWpvMU55d2lZMmhoYm01bGJGOXVZVzFsSWpvaVRYVnphV01nUW05NElpd2lhWEFpT2lJeE56Z3VNVE0yTGpReUxqSXlNQ0lzSW1OdmRXNTBjbmtpT2lKVmEzSmhhVzVsSWl3aWRYTmxjbDlwWkNJNk56UXdOeXdpZFhObGNsOXdhRzl1WlNJNklpc3pPREE1TXpBNU5qYzBNak1pTENKMWMyVnlYM0p2YkdWeklqcGJJbU5zYVdWdWRDSmRMQ0p3Y205bWFXeGxYMmxrSWpvMk5EY3hMQ0p6WlhOemFXOXVYMmxrSWpvaVpEQXdPVFppWmpndE1qTmxOUzAwWldVMkxXSmxNelV0Wmpnd09ETTVaV1k1WkRnMElpd2laVzUyYVhKdmJtMWxiblFpT2lKd2NtOWtkV04wYVc5dUlpd2ljR3hoZEdadmNtMGlPaUpFWlhOcmRHOXdJaXdpZFhObGNsOWhaMlZ1ZENJNklrMXZlbWxzYkdGY0x6VXVNQ0FvVjJsdVpHOTNjeUJPVkNBeE1DNHdPeUJYYVc0Mk5Ec2dlRFkwS1NCQmNIQnNaVmRsWWt0cGRGd3ZOVE0zTGpNMklDaExTRlJOVEN3Z2JHbHJaU0JIWldOcmJ5a2dRMmh5YjIxbFhDOHhOVEl1TUM0d0xqQWdVMkZtWVhKcFhDODFNemN1TXpZaUxDSmxlSEFpT2pFM09EZ3hPRFV4TmpkOS5nYUo1WGRxTjJ3djY2SnFxYldnX0dGUzY3V3lZVEVOc0JQa3VpcU1sNTVN&master=57',
    ref: 'http://live.tvstitch.com/',
    ua: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36',
    hosts: ['777905.live.tvstitch.com', 'hls-cdn1.dyvyapp.com'],
  },
  'universal-comedy': {
    url: 'http://bantel-cdn1.iptvperu.tv:1935/btnscrtn/universalcomedy/playlist.m3u8',
    ua: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    hosts: ['bantel-cdn1.iptvperu.tv'],
  },
  'universal-crime': {
    url: 'http://bantel-cdn1.iptvperu.tv:1935/btnscrtn/universalcrime/playlist.m3u8',
    ua: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    hosts: ['bantel-cdn1.iptvperu.tv'],
  },
  'tv3-ghana': {
    url: 'https://g2qd3exjy7an-hls-live.5centscdn.com/webtv3/ghanatv.stream/playlist.m3u8',
    ref: 'https://liveghanatv.com/',
    hosts: ['g2qd3exjy7an-hls-live.5centscdn.com'],
  },
  'axn-cee': {
    url: 'https://cronos.mangora1.cfd/axy/usergend3h5jkc9rnd.m3u8',
    ref: 'https://cool-tv.net/',
    ua: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 VLC/3.0.18 LibVLC/3.0.18',
    hosts: ['cronos.mangora1.cfd'],
  },
};

const tokens = new Map(); // event -> { q, until }
const events = new Map(); // slug -> event, overrides SNRT after an API refresh
let teleMaroc = null;     // { url, until }

const HLS = { 'content-type': 'application/vnd.apple.mpegurl', 'cache-control': 'no-store' };
const middle = (event) => event.split('_').slice(1, -1).join('_'); // 73_arryadia-tnt_zcmwjdc -> arryadia-tnt

async function token(event) {
  const cached = tokens.get(event);
  if (cached && cached.until > Date.now()) return cached.q;
  const r = await fetch('https://token.easybroadcast.io/all?url=' + encodeURIComponent(CDN + event + '/playlist.m3u8'),
    { headers: { 'User-Agent': UA } });
  if (!r.ok) throw new Error('token service ' + r.status);
  const q = (await r.text()).trim();
  if (!q.startsWith('token=')) throw new Error('token service said ' + JSON.stringify(q.slice(0, 120)));
  const expires = Number(new URLSearchParams(q).get('expires')) * 1000 || Date.now() + 3600e3;
  tokens.set(event, { q, until: expires - 5 * 60e3 });
  return q;
}

async function refreshEvents() {
  const text = await (await fetch(FEEDS_API, { headers: { 'User-Agent': UA } })).text();
  const found = [...text.replace(/\\\//g, '/').matchAll(/abr_corp\/(\d+_[a-z0-9-]+_[a-z0-9]+)\//g)].map((m) => m[1]);
  for (const [slug, event] of Object.entries(SNRT)) {
    const fresh = found.find((e) => middle(e) === middle(event));
    if (fresh) events.set(slug, fresh);
  }
}

// Every SNRT event serves the same three renditions - checked across aloula, arryadia-tnt,
// arrabia and arryadia-hd1 on 2026-10-12, identical resolutions, frame rate and codecs, only
// the bandwidths differ (and those are advisory). That makes a master playlist reproducible
// without fetching one, which is what fallbackMaster below relies on.
const LADDER = [
  { h: 480, resolution: '854x480', codecs: 'avc1.4d401f,mp4a.40.2', bandwidth: 2000000 },
  { h: 360, resolution: '640x360', codecs: 'avc1.4d401f,mp4a.40.2', bandwidth: 1100000 },
  { h: 240, resolution: '426x240', codecs: 'avc1.4d4016,mp4a.40.2', bandwidth: 600000 },
];

async function upstream(event, path) {
  return fetch(CDN + event + '/' + path + '?' + (await token(event)), { headers: { 'User-Agent': UA } });
}

async function snrt(slug, variant) {
  const path = variant || 'playlist.m3u8';
  let event = events.get(slug) || SNRT[slug];

  // Judge the answer by what is in it, not only by its status. The CDN has been seen
  // returning a perfectly good playlist under a 403 (athaqafia, 2026-10-12), and a 403
  // HTML block page for an event that serves fine from everywhere else (arryadia-tnt).
  const attempt = async () => {
    const r = await upstream(event, path);
    const text = await r.text();
    return { status: r.status, text, usable: r.ok || text.startsWith('#EXTM3U') };
  };

  let res = await attempt();
  for (let i = 0; i < 3 && !res.usable && res.status === 403; i++) {
    tokens.delete(event); // a fresh token, and a fresh roll of the egress dice
    res = await attempt();
  }
  if (!variant && !res.usable && (res.status === 404 || res.status === 410)) { // event renamed
    await refreshEvents();
    event = events.get(slug) || event;
    res = await attempt();
  }

  if (!res.usable) {
    // The CDN refuses some Cloudflare egress outright, so no number of retries from this
    // colo will get the master. The player's own network is not blocked, so hand it a
    // master it can follow by itself: the standard ladder, pointing straight at the CDN
    // with the token attached. Costs the live token refresh - the URLs stop working when
    // the token expires, about an hour - so it is a fallback, never the normal path.
    if (!variant) return new Response(await fallbackMaster(event), { headers: HLS });
    const why = res.text.trim().slice(0, 200);
    return new Response('upstream ' + res.status + (why ? ': ' + why : '') + '\n', { status: 502 });
  }

  const base = CDN + event + '/' + path;
  const lines = res.text.split('\n').map((line) => {
    if (!line || line.startsWith('#')) return line;
    // master: variant -> this Worker (relative to /snrt/<slug>.m3u8); media: segment -> CDN
    return variant ? new URL(line.trim(), base).href : slug + '/' + line.trim();
  });
  return new Response(lines.join('\n'), { headers: HLS });
}

async function fallbackMaster(event) {
  const q = await token(event);
  const out = ['#EXTM3U', '#EXT-X-VERSION:3'];
  for (const v of LADDER) {
    out.push('#EXT-X-STREAM-INF:BANDWIDTH=' + v.bandwidth + ',FRAME-RATE=50,RESOLUTION='
      + v.resolution + ',CODECS="' + v.codecs + '"');
    out.push(CDN + event + '/corp/' + event + '_' + v.h + 'p/chunks.m3u8?' + q);
  }
  return out.join('\n') + '\n';
}

async function teleMarocUrl() {
  if (teleMaroc && teleMaroc.until > Date.now()) return teleMaroc.url;
  const headers = { 'User-Agent': UA };
  const html = await (await fetch('https://www.telemaroc.tv/liveTV', { headers })).text();
  const m = html.match(/player\.restream\.io\/\?token=([0-9a-f]+)/);
  if (!m) throw new Error('telemaroc.tv: player not found');
  const video = await (await fetch('https://player-backend.restream.io/public/videos/' + m[1], { headers })).json();
  const url = video.videoUrlHls || video.videoUrl;
  if (!url) throw new Error('restream: no video URL');
  let until = Date.now() + 3600e3;
  try { // refresh an hour before the signed URL's own expiry
    const jwt = new URL(url).pathname.split('/')[1].split('.')[1];
    until = Math.max(until, JSON.parse(atob(jwt.replace(/-/g, '+').replace(/_/g, '/'))).exp * 1000 - 3600e3);
  } catch { /* keep the 1 h default */ }
  teleMaroc = { url, until };
  return url;
}

// Every URI in a proxied channel's playlists is routed back through here, because the
// CDN checks the header on segments too. `u` is restricted to the channel's own hosts
// so this can't be used as an open proxy.
const proxyUri = (slug, abs) => '/h/' + slug + '/r?u=' + encodeURIComponent(abs);

function rewritePlaylist(text, base, slug) {
  return text.split('\n').map((line) => {
    const t = line.trim();
    if (!t) return line;
    if (t.startsWith('#')) { // EXT-X-KEY / EXT-X-MAP / EXT-X-MEDIA carry URI="..."
      return line.replace(/URI="([^"]+)"/g, (_m, u) => 'URI="' + proxyUri(slug, new URL(u, base).href) + '"');
    }
    return proxyUri(slug, new URL(t, base).href);
  }).join('\n');
}

async function headerProxy(slug, target) {
  const cfg = PROXY[slug];
  if (!cfg) return new Response('not found\n', { status: 404 });
  let url;
  try { url = new URL(target); } catch { return new Response('bad url\n', { status: 400 }); }
  if (!cfg.hosts.includes(url.hostname)) return new Response('host not allowed\n', { status: 403 });

  const headers = { 'User-Agent': cfg.ua || UA };
  if (cfg.ref) headers.Referer = cfg.ref;
  let r = await fetch(target, { headers, redirect: 'follow' });
  for (let i = 0; i < 2 && r.status === 403; i++) r = await fetch(target, { headers, redirect: 'follow' });
  if (!r.ok) { // say what the host said, so a 502 here names its own cause
    const why = (await r.text().catch(() => '')).trim().replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').slice(0, 160);
    return new Response('upstream ' + r.status + (why ? ': ' + why : '') + '\n', { status: 502 });
  }

  const type = r.headers.get('content-type') || '';
  const base = r.url || target;
  if (/mpegurl|x-mpegurl/i.test(type) || /\.m3u8$/i.test(new URL(base).pathname)) {
    return new Response(rewritePlaylist(await r.text(), base, slug), { headers: HLS });
  }
  return new Response(r.body, { // segment: stream straight through
    headers: { 'content-type': type || 'video/mp2t', 'cache-control': 'public, max-age=10' },
  });
}

export default {
  async fetch(request) {
    const { pathname } = new URL(request.url);
    try {
      let m = pathname.match(/^\/snrt\/([a-z0-9-]+)\.m3u8$/);
      if (m && SNRT[m[1]]) return await snrt(m[1]);
      m = pathname.match(/^\/snrt\/([a-z0-9-]+)\/(corp\/[\w-]+\/chunks\.m3u8)$/);
      if (m && SNRT[m[1]]) return await snrt(m[1], m[2]);
      if (pathname === '/telemaroc.m3u8') return Response.redirect(await teleMarocUrl(), 302);
      m = pathname.match(/^\/h\/([a-z0-9-]+)\.m3u8$/);
      if (m && PROXY[m[1]]) return await headerProxy(m[1], PROXY[m[1]].url);
      m = pathname.match(/^\/h\/([a-z0-9-]+)\/r$/);
      if (m && PROXY[m[1]]) {
        const u = new URL(request.url).searchParams.get('u');
        if (!u) return new Response('missing u\n', { status: 400 });
        return await headerProxy(m[1], u);
      }
      if (pathname === '/') {
        const list = Object.keys(SNRT).map((s) => `/snrt/${s}.m3u8`).concat('/telemaroc.m3u8')
          .concat(Object.keys(PROXY).map((s) => `/h/${s}.m3u8`));
        return new Response(list.join('\n') + '\n', { headers: { 'content-type': 'text/plain' } });
      }
      return new Response('not found\n', { status: 404 });
    } catch (e) {
      return new Response(String(e && e.message || e) + '\n', { status: 502 });
    }
  },
};
