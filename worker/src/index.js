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

async function upstream(event, path) {
  return fetch(CDN + event + '/' + path + '?' + (await token(event)), { headers: { 'User-Agent': UA } });
}

async function snrt(slug, variant) {
  const path = variant || 'playlist.m3u8';
  let event = events.get(slug) || SNRT[slug];
  let r = await upstream(event, path);
  if (r.status === 403) { // token revoked early
    tokens.delete(event);
    r = await upstream(event, path);
  }
  if (!variant && (r.status === 404 || r.status === 410)) { // event renamed
    await refreshEvents();
    event = events.get(slug) || event;
    r = await upstream(event, path);
  }
  if (!r.ok) return new Response('upstream ' + r.status + '\n', { status: 502 });

  const base = CDN + event + '/' + path;
  const lines = (await r.text()).split('\n').map((line) => {
    if (!line || line.startsWith('#')) return line;
    // master: variant -> this Worker (relative to /snrt/<slug>.m3u8); media: segment -> CDN
    return variant ? new URL(line.trim(), base).href : slug + '/' + line.trim();
  });
  return new Response(lines.join('\n'), { headers: HLS });
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

export default {
  async fetch(request) {
    const { pathname } = new URL(request.url);
    try {
      let m = pathname.match(/^\/snrt\/([a-z0-9-]+)\.m3u8$/);
      if (m && SNRT[m[1]]) return await snrt(m[1]);
      m = pathname.match(/^\/snrt\/([a-z0-9-]+)\/(corp\/[\w-]+\/chunks\.m3u8)$/);
      if (m && SNRT[m[1]]) return await snrt(m[1], m[2]);
      if (pathname === '/telemaroc.m3u8') return Response.redirect(await teleMarocUrl(), 302);
      if (pathname === '/') {
        const list = Object.keys(SNRT).map((s) => `/snrt/${s}.m3u8`).concat('/telemaroc.m3u8');
        return new Response(list.join('\n') + '\n', { headers: { 'content-type': 'text/plain' } });
      }
      return new Response('not found\n', { status: 404 });
    } catch (e) {
      return new Response(String(e && e.message || e) + '\n', { status: 502 });
    }
  },
};
