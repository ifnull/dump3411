#!/usr/bin/env python3
"""
dump3411 / feed_server.py

Tiny stdlib HTTP server. Endpoints:

  GET /                       — single-page status dashboard (HTML)
  GET /data/remoteid.json     — current tracker snapshot per FEED.md
  GET /status                 — operational health: uptime, per-source
                                counters, CPU temp, drones_active

Constraints from FEED.md "Producer obligations":
  * **Snapshot-only handlers.** No decoding, conversion, or computation under
    request; everything is already done inside ``Tracker.snapshot()`` and
    ``Tracker.health()``. Each handler grabs a dict, serialises, returns.
  * **Stdlib only** — ``http.server`` + ``json``. No async, no extra deps.

The server is threaded so multiple LAN consumers can poll concurrently
without serialising on one request. The handler also responds to ``HEAD``
and emits no per-request access log (otherwise the journal would gain one
line per consumer poll).
"""

import http.server
import json
import logging
import urllib.parse
from typing import Tuple

from tracker import Tracker

log = logging.getLogger("dump3411.feed")


# -- Status dashboard (GET /) --------------------------------------------------
# Single self-contained HTML page. No CDN, no build step, no external assets.
# Polls /status and /data/remoteid.json every 1.5s and renders the live state.

_DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dump3411</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#21262d; --rule:#30363d;
          --fg:#c9d1d9; --dim:#8b949e; --muted:#6e7681; --hi:#e6edf3; }
  * { box-sizing: border-box; }
  body { font: 14px/1.45 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
         background: var(--bg); color: var(--fg); margin: 0; padding: 1.5rem;
         max-width: 1200px; }
  h1 { font-size: 1rem; font-weight: 600; margin: 0 0 1.25rem 0;
       display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
  h1 .host { color: var(--muted); font-weight: 400; font-size: 0.85rem; }
  .pill { display: inline-block; padding: 0.15rem 0.55rem; border-radius: 3px;
          font-size: 0.7rem; font-weight: 700; letter-spacing: 0.05em; }
  .pill.ok    { background: #1f6f3f; color: #fff; }
  .pill.idle  { background: #5c4400; color: #f0c674; }
  .pill.down  { background: #6b1f1f; color: #fff; }
  .row { display: grid; gap: 0.75rem;
         grid-template-columns: repeat(auto-fit, minmax(155px, 1fr));
         margin: 0.5rem 0 1.25rem 0; }
  .stat { background: var(--card); padding: 0.6rem 0.8rem;
          border-radius: 4px; border-left: 3px solid var(--rule); }
  .stat .label { color: var(--dim); font-size: 0.7rem;
                 text-transform: uppercase; letter-spacing: 0.06em; }
  .stat .val   { color: var(--hi); font-size: 1.4rem; font-weight: 600;
                 line-height: 1.2; margin-top: 0.15rem; }
  h2 { font-size: 0.75rem; font-weight: 700; color: var(--dim);
       text-transform: uppercase; letter-spacing: 0.06em;
       margin: 1.75rem 0 0.4rem 0; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; font-weight: 600; color: var(--dim);
       font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em;
       padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--rule); }
  td { padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--line); }
  tr:hover td { background: var(--card); }
  .num   { font-variant-numeric: tabular-nums; }
  .id    { color: var(--hi); }
  .empty { color: var(--muted); font-style: italic; padding: 1rem 0;
           text-align: center; }
  .maplink { color: inherit; text-decoration: none;
             border-bottom: 1px dotted var(--rule); }
  .maplink:hover { color: var(--hi); border-bottom-color: var(--dim); }
  .unit-toggle { display: inline-flex; gap: 2px; background: var(--rule);
                 padding: 2px; border-radius: 4px; margin-left: auto; }
  .unit-pill   { background: transparent; border: 0; color: var(--dim);
                 cursor: pointer; font: inherit;
                 padding: 0.15rem 0.55rem; border-radius: 3px;
                 font-size: 0.7rem; letter-spacing: 0.04em; }
  .unit-pill.active        { background: var(--card); color: var(--hi); }
  .unit-pill:hover         { color: var(--fg); }
  .unit-pill.active:hover  { color: var(--hi); }
  footer { margin-top: 2rem; color: var(--muted); font-size: 0.7rem; }
</style>
</head>
<body>

<h1>dump3411
  <span id="pill" class="pill idle">…</span>
  <span id="hostname" class="host"></span>
  <span class="unit-toggle">
    <button id="u-imperial" class="unit-pill" onclick="setUnits('imperial')">ft·kt·°F</button>
    <button id="u-metric"   class="unit-pill" onclick="setUnits('metric')">m·m/s·°C</button>
  </span>
</h1>

<div class="row">
  <div class="stat"><div class="label">Uptime</div><div class="val num" id="uptime">–</div></div>
  <div class="stat"><div class="label">Last beacon</div><div class="val num" id="last_seen">–</div></div>
  <div class="stat"><div class="label">Drones active</div><div class="val num" id="drones_active">0</div></div>
  <div class="stat"><div class="label">Messages</div><div class="val num" id="messages_total">0</div></div>
  <div class="stat"><div class="label">CPU temp</div><div class="val num" id="cpu_temp">–</div></div>
</div>

<h2>By transport</h2>
<table>
  <thead><tr><th>Source</th><th>Messages</th><th>Last seen</th></tr></thead>
  <tbody id="by_source"></tbody>
</table>

<h2>Drones</h2>
<table>
  <thead><tr>
    <th>UAS-ID</th><th>Type</th><th>Description</th><th>Drone</th><th>Operator</th><th>Alt</th><th>AGL</th>
    <th>GS</th><th>Track</th><th>RSSI</th><th>Source</th><th>Age</th>
  </tr></thead>
  <tbody id="drones"><tr><td class="empty" colspan="12">no drones currently in range</td></tr></tbody>
</table>

<footer>Polls /status and /data/remoteid.json every 1.5 s &middot; FEED.md is the wire contract.</footer>

<script>
// Sources we actively decode into the tracker. We always show these so a
// dead radio is visible at a glance. Any other source the tracker reports
// gets appended automatically.
const KNOWN_SOURCES = ['ble', 'wifi_beacon', 'wifi_nan'];

// Unit system — display only. The feed (/data/remoteid.json) and /status are
// always imperial / °C respectively; this just controls what the HTML shows.
// Per-browser preference, persisted in localStorage. Default: imperial, to
// match the feed contract that this page is just a window onto.
let units = localStorage.getItem('units') || 'imperial';

function setUnits(u) {
  units = u;
  localStorage.setItem('units', u);
  syncUnitButtons();
  tick();                  // re-render right away rather than wait for the next poll
}

function syncUnitButtons() {
  document.getElementById('u-imperial').className =
    'unit-pill' + (units === 'imperial' ? ' active' : '');
  document.getElementById('u-metric').className =
    'unit-pill' + (units === 'metric'   ? ' active' : '');
}

// All converters guard null so a missing value stays null (and renders as '–'),
// not 0 °F or 0 m.
const conv = {
  alt:  v => v == null ? null : (units === 'metric'   ? v * 0.3048    : v),
  spd:  v => v == null ? null : (units === 'metric'   ? v * 0.5144444 : v),
  temp: v => v == null ? null : (units === 'imperial' ? v * 9/5 + 32  : v),
};
const lbl = {
  alt:  () => units === 'metric'   ? 'm'   : 'ft',
  spd:  () => units === 'metric'   ? 'm/s' : 'kt',
  temp: () => units === 'imperial' ? '°F'  : '°C',
};

const fmt = {
  age(s) {
    if (s == null) return 'never';
    if (s < 60)    return s.toFixed(1) + 's';
    if (s < 3600)  return Math.floor(s/60) + 'm ' + Math.floor(s%60) + 's';
    if (s < 86400) return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
    return Math.floor(s/86400) + 'd ' + Math.floor((s%86400)/3600) + 'h';
  },
  num(v, d) {
    return (v == null || Number.isNaN(v)) ? '–' : Number(v).toFixed(d);
  },
  coord(v) {
    return (v == null || Number.isNaN(v)) ? '–' : Number(v).toFixed(5);
  },
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

// Render a lat/lon pair as a Google Maps link (opens new tab). Returns '–' if
// either coord is missing — used for both drone position and operator location.
function coordCell(lat, lon) {
  if (lat == null || lon == null) return '–';
  const txt = fmt.coord(lat) + ', ' + fmt.coord(lon);
  const url = 'https://www.google.com/maps?q=' + encodeURIComponent(lat + ',' + lon);
  return '<a class="maplink" href="' + url
       + '" target="_blank" rel="noopener noreferrer">' + txt + '</a>';
}

async function fetchJSON(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(r.status);
  return r.json();
}

function setPill(text, klass) {
  const p = document.getElementById('pill');
  p.textContent = text;
  p.className = 'pill ' + klass;
}

async function tick() {
  try {
    const [s, f] = await Promise.all([
      fetchJSON('/status'),
      fetchJSON('/data/remoteid.json'),
    ]);

    if (s.last_seen_s != null && s.last_seen_s < 300) {
      setPill('ACTIVE', 'ok');
    } else {
      setPill('IDLE', 'idle');
    }

    document.getElementById('uptime').textContent        = fmt.age(s.uptime_s);
    document.getElementById('last_seen').textContent     = s.last_seen_s == null ? 'never' : fmt.age(s.last_seen_s);
    document.getElementById('drones_active').textContent = s.drones_active;
    document.getElementById('messages_total').textContent = s.messages_total.toLocaleString();
    document.getElementById('cpu_temp').textContent      = s.cpu_temp_c == null ? '–' : fmt.num(conv.temp(s.cpu_temp_c), 1) + ' ' + lbl.temp();

    const sources = [...KNOWN_SOURCES,
                     ...Object.keys(s.by_source).filter(k => !KNOWN_SOURCES.includes(k))];
    document.getElementById('by_source').innerHTML = sources.map(src => {
      const x = s.by_source[src] || { messages: 0, last_seen_s: null };
      return '<tr><td>' + src + '</td>'
           + '<td class="num">' + x.messages.toLocaleString() + '</td>'
           + '<td class="num">' + (x.last_seen_s == null ? 'never' : fmt.age(x.last_seen_s)) + '</td></tr>';
    }).join('');

    const drones = document.getElementById('drones');
    if (!f.drones || f.drones.length === 0) {
      drones.innerHTML = '<tr><td class="empty" colspan="12">no drones currently in range</td></tr>';
    } else {
      const historyEnabled = !!s.history_enabled;
      drones.innerHTML = f.drones.map(d => '<tr>'
        + '<td class="id">'
        + (historyEnabled
            ? '<a class="maplink" href="/map?uas_id=' + encodeURIComponent(d.id)
              + '" target="_blank" rel="noopener noreferrer">' + escapeHtml(d.id) + '</a>'
            : escapeHtml(d.id))
        + '</td>'
        + '<td>' + (d.ua_type || '–') + '</td>'
        + '<td>' + (d.self_id ? escapeHtml(d.self_id) : '–') + '</td>'
        + '<td class="num">' + coordCell(d.lat, d.lon) + '</td>'
        + '<td class="num">' + coordCell(d.operator?.lat, d.operator?.lon) + '</td>'
        + '<td class="num">' + fmt.num(conv.alt(d.alt_geom_ft), 0) + ' ' + lbl.alt() + '</td>'
        + '<td class="num">' + fmt.num(conv.alt(d.agl_ft), 0) + ' ' + lbl.alt() + '</td>'
        + '<td class="num">' + fmt.num(conv.spd(d.gs), 1) + ' ' + lbl.spd() + '</td>'
        + '<td class="num">' + fmt.num(d.track, 0) + '°</td>'
        + '<td class="num">' + fmt.num(d.rssi, 0) + ' dBm</td>'
        + '<td>' + (d.rid_source || '–') + '</td>'
        + '<td class="num">' + fmt.age(d.seen) + '</td>'
        + '</tr>'
      ).join('');
    }
  } catch (e) {
    setPill('OFFLINE', 'down');
  }
}

document.getElementById('hostname').textContent = location.host;
syncUnitButtons();
tick();
setInterval(tick, 1500);
</script>
</body>
</html>
""".encode("utf-8")


# -- Map view (GET /map?uas_id=X) ----------------------------------------------
# Self-contained HTML for one-drone track replay. Loads Leaflet from a CDN
# and OSM tiles from OSM; tiles already require internet to view a map at
# all, so the extra CDN dependency is a non-issue. The page polls
# /history.json once on load and renders the track polyline + operator
# marker. Map only available when history is enabled; the route returns 404
# otherwise so this HTML stays unreachable.

_MAP_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>dump3411 — track</title>
<link rel="stylesheet"
      href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css"
      integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY="
      crossorigin="anonymous">
<style>
  html, body { margin: 0; padding: 0; height: 100%;
               font: 13px/1.4 ui-monospace, "SF Mono", Menlo, Consolas, monospace;
               background: #0d1117; color: #c9d1d9; }
  #map { position: absolute; inset: 0; }
  #panel { position: absolute; top: 12px; right: 12px; z-index: 1000;
           background: rgba(13,17,23,0.92); border: 1px solid #30363d;
           border-radius: 4px; padding: 10px 12px; max-width: 320px; }
  #panel h1 { margin: 0 0 0.4rem 0; font-size: 0.95rem; font-weight: 600; }
  #panel .meta { color: #8b949e; font-size: 0.75rem; line-height: 1.6; }
  #panel .meta b { color: #e6edf3; font-weight: 600; }
  #panel a { color: #58a6ff; }
  #empty { position: absolute; left: 50%; top: 50%;
           transform: translate(-50%, -50%); padding: 1rem 1.5rem;
           background: rgba(13,17,23,0.92); border: 1px solid #6b1f1f;
           border-radius: 4px; color: #ffb4b4; z-index: 1000;
           display: none; }
  .leaflet-popup-content-wrapper { background: #161b22; color: #c9d1d9; }
  .leaflet-popup-tip { background: #161b22; }
</style>
</head>
<body>
<div id="map"></div>
<div id="panel">
  <h1 id="uas-id">…</h1>
  <div class="meta" id="meta"></div>
  <div class="meta"><a href="/">← dashboard</a></div>
</div>
<div id="empty">No track points stored for this UAS-ID yet.</div>

<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"
        integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo="
        crossorigin="anonymous"></script>
<script>
const params = new URLSearchParams(location.search);
const uasId = params.get('uas_id') || '';
document.getElementById('uas-id').textContent = uasId || '(no uas_id)';

const map = L.map('map', { preferCanvas: true });
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '© OpenStreetMap contributors',
  maxZoom: 19,
}).addTo(map);
map.setView([0, 0], 2);

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}
function fmt(v, d) { return (v == null || Number.isNaN(v)) ? '–' : Number(v).toFixed(d); }

async function loadTrack() {
  if (!uasId) { document.getElementById('empty').style.display = 'block'; return; }
  let data;
  try {
    const r = await fetch('/history.json?uas_id=' + encodeURIComponent(uasId),
                          { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    data = await r.json();
  } catch (e) {
    document.getElementById('empty').textContent = 'history.json failed: ' + e.message;
    document.getElementById('empty').style.display = 'block';
    return;
  }

  // Drone track — red polyline + per-point circle markers.
  const track = (data.track || []).filter(p => p.lat != null && p.lon != null);
  if (track.length > 0) {
    const coords = track.map(p => [p.lat, p.lon]);
    L.polyline(coords, { color: '#ff5252', weight: 2, opacity: 0.85 }).addTo(map);
    track.forEach((p, i) => {
      const isEnd = (i === 0 || i === track.length - 1);
      const m = L.circleMarker([p.lat, p.lon], {
        radius: isEnd ? 5 : 3,
        color: '#ff5252', fillColor: isEnd ? '#ff5252' : '#ff8a8a',
        fillOpacity: 0.9, weight: isEnd ? 2 : 1,
      }).addTo(map);
      const when = new Date(p.ts * 1000).toLocaleString();
      m.bindPopup(
        '<b>' + when + '</b><br>'
        + 'Alt: '   + fmt(p.alt_geom_ft, 0) + ' ft<br>'
        + 'AGL: '   + fmt(p.agl_ft, 0)      + ' ft<br>'
        + 'Speed: ' + fmt(p.gs, 1)          + ' kt<br>'
        + 'Track: ' + fmt(p.track, 0)       + '°<br>'
        + 'RSSI: '  + fmt(p.rssi, 0)        + ' dBm<br>'
        + 'Src: '   + (p.rid_source || '–')
      );
    });
  }

  // Operator marker — blue.
  const op = data.operator;
  if (op && op.lat != null && op.lon != null) {
    L.circleMarker([op.lat, op.lon], {
      radius: 7, color: '#1f6feb', fillColor: '#58a6ff',
      fillOpacity: 0.9, weight: 2,
    }).addTo(map).bindPopup(
      '<b>Operator</b><br>'
      + (op.id ? 'ID: ' + escapeHtml(op.id) + '<br>' : '')
      + 'Last seen: ' + new Date(op.ts * 1000).toLocaleString()
    );
  }

  // Side panel meta.
  const since = track.length ? new Date(track[0].ts * 1000).toLocaleString() : '–';
  const until = track.length ? new Date(track[track.length-1].ts * 1000).toLocaleString() : '–';
  document.getElementById('meta').innerHTML =
      '<b>Type:</b> '   + (data.ua_type  || '–') + '<br>'
    + '<b>Id type:</b> '+ (data.id_type  || '–') + '<br>'
    + (data.self_id ? '<b>Description:</b> ' + escapeHtml(data.self_id) + '<br>' : '')
    + '<b>Points:</b> ' + track.length + '<br>'
    + '<b>From:</b> '   + since + '<br>'
    + '<b>To:</b> '     + until;

  // Fit map to whatever we have.
  const all = [...track.map(p => [p.lat, p.lon])];
  if (op && op.lat != null) all.push([op.lat, op.lon]);
  if (all.length > 0) {
    map.fitBounds(L.latLngBounds(all), { padding: [40, 40], maxZoom: 17 });
  } else {
    document.getElementById('empty').style.display = 'block';
  }
}

loadTrack();
</script>
</body>
</html>
""".encode("utf-8")


# -- Request handler -----------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    """Per-request handler.  ``tracker`` and ``history`` are bound at
    subclass-creation time in :func:`make_server` so this class can be a
    plain BaseHTTPRequestHandler."""

    tracker: Tracker        # filled in by make_server()
    history  = None         # HistoryWriter | None; None disables /history and /map
    server_version = "dump3411/1"
    sys_version    = ""     # suppress the default "Python/3.x" Server suffix

    def do_GET(self) -> None:
        try:
            body, ctype = self._render(self.path)
        except KeyError:
            self.send_error(404, "Not Found")
            return
        except Exception:
            log.exception("handler failed for %s", self.path)
            self.send_error(500, "Internal Server Error")
            return
        self._send_headers(len(body), ctype)
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        try:
            body, ctype = self._render(self.path)
        except KeyError:
            self.send_error(404, "Not Found")
            return
        except Exception:
            log.exception("handler failed for %s", self.path)
            self.send_error(500, "Internal Server Error")
            return
        self._send_headers(len(body), ctype)

    def _render(self, path: str) -> Tuple[bytes, str]:
        """Dispatch by path. Raises KeyError on unknown paths."""
        # Strip a query string for path matching; keep self.path intact for
        # endpoints that consume it.
        bare = path.split("?", 1)[0]
        if bare == "/data/remoteid.json":
            body = json.dumps(self.tracker.snapshot(), separators=(",", ":")).encode("utf-8")
            return body, "application/json"
        if bare == "/status":
            doc = self.tracker.health()
            doc["history_enabled"] = self.history is not None
            if self.history is not None:
                try:
                    doc["history"] = self.history.stats()
                except Exception:
                    log.exception("history stats failed")
            body = json.dumps(doc, separators=(",", ":")).encode("utf-8")
            return body, "application/json"
        if bare in ("/", "/index.html"):
            return _DASHBOARD_HTML, "text/html; charset=utf-8"
        if bare == "/history.json":
            if self.history is None:
                raise KeyError(path)
            return self._history_json(path), "application/json"
        if bare == "/map":
            if self.history is None:
                raise KeyError(path)
            return _MAP_HTML, "text/html; charset=utf-8"
        raise KeyError(path)

    def _history_json(self, path: str) -> bytes:
        """Build the /history.json response for a query."""
        # Minimal query parsing — only the params we use.
        q = path.split("?", 1)[1] if "?" in path else ""
        params: dict = {}
        for part in q.split("&"):
            if "=" in part:
                k, v = part.split("=", 1)
                params[k] = urllib.parse.unquote_plus(v)
        uas_id = params.get("uas_id", "").strip()
        if not uas_id:
            return json.dumps({"error": "uas_id is required"}).encode("utf-8")
        since = float(params["since"]) if params.get("since") else None
        until = float(params["until"]) if params.get("until") else None
        track = self.history.query_track(uas_id, since=since, until=until)
        operator = self.history.query_operator(uas_id)
        meta = self.history.query_drone_meta(uas_id) or {}
        doc = {
            "uas_id":   uas_id,
            "id_type":  meta.get("id_type"),
            "ua_type":  meta.get("ua_type"),
            "self_id":  meta.get("self_id"),
            "since":    since,
            "until":    until,
            "operator": operator,
            "track":    track,
        }
        return json.dumps(doc, separators=(",", ":")).encode("utf-8")

    def _send_headers(self, body_len: int, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type",                content_type)
        self.send_header("Content-Length",              str(body_len))
        self.send_header("Cache-Control",               "no-store")
        # LAN tool, public-airspace payload — let browser consumers poll too.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def log_message(self, *_args) -> None:
        """Silence the default per-request stderr access log."""
        pass


# -- Server constructors -------------------------------------------------------

def make_server(addr: Tuple[str, int], tracker: Tracker,
                history=None) -> http.server.ThreadingHTTPServer:
    """Build a ThreadingHTTPServer bound to ``addr`` serving ``tracker``.

    When ``history`` is provided, ``/history.json`` and ``/map`` light up
    and ``/status`` exposes ``history_enabled: true`` (used by the
    dashboard to decide whether to render UAS-IDs as clickable map links).

    Returns the server instance so the caller can ``serve_forever()`` it on
    any thread and ``shutdown()`` it cleanly (used by the standalone test
    below).
    """
    handler_cls = type("Handler", (_Handler,),
                       {"tracker": tracker, "history": history})
    return http.server.ThreadingHTTPServer(addr, handler_cls)


def serve(addr: Tuple[str, int], tracker: Tracker) -> None:
    """Build a server and block in ``serve_forever``.

    This is the entry point ``dump3411.py`` uses on its main thread.
    Returns when ``KeyboardInterrupt`` is raised (SIGINT) or when something
    else calls ``server.shutdown()``.
    """
    server = make_server(addr, tracker)
    host, port = addr
    log.info(f"feed listening on http://{host}:{port}/data/remoteid.json")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# -- Standalone smoke test -----------------------------------------------------

if __name__ == "__main__":
    import threading
    import urllib.error
    import urllib.request

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    t = Tracker(ttl_seconds=60.0)
    t.update_basic_id(
        mac="aa:bb:cc:dd:ee:01", uas_id="158190SK3X2YB7",
        id_type_raw=1, ua_type_raw=2,
        rssi=-62.0, rid_source="ble",
    )
    t.update_location(
        mac="aa:bb:cc:dd:ee:01",
        lat=40.7128, lon=-74.0060,
        alt_geo_m=125.5, height_agl_m=115.0,
        gs_mps=8.2, heading_deg=271.0, vspeed_mps=-3.25,
        rssi=-60.0, rid_source="ble",
    )

    server = make_server(("127.0.0.1", 0), t)        # ephemeral port
    host, port = server.server_address
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        # 1) Good path.
        url = f"http://{host}:{port}/data/remoteid.json"
        with urllib.request.urlopen(url, timeout=2) as r:
            body    = r.read()
            doc     = json.loads(body)
            headers = {k: r.headers[k] for k in
                       ("Content-Type", "Cache-Control",
                        "Access-Control-Allow-Origin", "Server")}
        print(f"GET /data/remoteid.json -> {r.status}")
        for k, v in headers.items():
            print(f"  {k}: {v}")
        print(f"  body: {len(body)} bytes, drones={len(doc['drones'])}, "
              f"schema_v={doc['schema_version']}, messages={doc['messages']}")
        assert doc["schema_version"] == 1
        assert doc["drones"][0]["id"]          == "158190SK3X2YB7"
        assert doc["drones"][0]["lat"]         == 40.7128
        assert doc["drones"][0]["alt_geom_ft"] == round(125.5 * 3.28084, 1)
        assert headers["Content-Type"]                == "application/json"
        assert headers["Cache-Control"]               == "no-store"
        assert headers["Access-Control-Allow-Origin"] == "*"

        # 2) HEAD same path — headers only, empty body.
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
            assert r.read() == b""
            print(f"HEAD /data/remoteid.json -> {r.status} (no body, ok)")

        # 3) Wrong path -> 404.
        try:
            urllib.request.urlopen(
                f"http://{host}:{port}/data/aircraft.json", timeout=2
            )
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
            print(f"GET /data/aircraft.json -> {e.code} (correct)")

        print("OK")
    finally:
        server.shutdown()
        server.server_close()
