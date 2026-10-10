"""A camera wall: every printer's latest camera picture on one page, for a tablet or TV, without signing in.

Off until you switch it on; the link holds a secret (anyone who has it can look, and can fetch each printer's still picture at `/wall/<secret>/cam/<id>.jpg`, which Home Assistant, Frigate
or a kiosk can use as a still-image camera). These are *still pictures that refresh every few seconds*, not live video. Each picture is fetched from the printer's camera at most once every
two seconds however many people are looking, so a wall of viewers does not load the camera. It shows only a printer's name, state and progress, never an address or a key."""
import hmac
import secrets
import threading
import time
from typing import Optional

from sqlmodel import Session, select

from app import printers as printing
from app.models import Printer
from app.settings_store import get_setting, set_setting

CACHE_SECONDS = 2.0
MAX_PICTURE = 3 * 1024 * 1024
_cache: dict = {}                 # printer id -> (fetched at, jpeg bytes or None)
_locks: dict = {}                 # printer id -> a lock, so a slow camera holds up only its own picture
_guard = threading.Lock()


def _lock_for(printer_id: int) -> threading.Lock:
    with _guard:
        return _locks.setdefault(printer_id, threading.Lock())


def token(session: Session) -> Optional[str]:
    return get_setting(session, "wall_token", "") or None


def settings(session: Session) -> dict:
    t = token(session)
    return {"enabled": bool(t), "path": f"/wall/{t}" if t else None}


def configure(session: Session, enabled: Optional[bool], rotate: bool) -> dict:
    if enabled is False:
        set_setting(session, "wall_token", "")
    elif enabled is True and not token(session):
        set_setting(session, "wall_token", secrets.token_urlsafe(24))
    if rotate and token(session):
        set_setting(session, "wall_token", secrets.token_urlsafe(24))
    return settings(session)


def valid(session: Session, given: str) -> bool:
    real = token(session)
    return bool(real) and hmac.compare_digest(real.encode(), (given or "").encode())


def snapshot(session: Session) -> dict:
    from app import printwatch
    out = []
    for p in session.exec(select(Printer).order_by(Printer.name)).all():
        st = printwatch.latest.get(p.id) or {}
        out.append({"id": p.id, "name": p.name, "camera": bool(p.snapshot_url), "online": bool(st.get("online")), "state": "out of service" if p.out_of_service else (st.get("state") or "unknown"),
                    "progress": st.get("progress")})
    return {"printers": out}


def picture(session: Session, printer: Printer, now: Optional[float] = None) -> Optional[bytes]:
    """The printer's latest picture (a few seconds old at most), or None when it has no camera or the camera does not answer."""
    if not printer.snapshot_url:
        return None
    now = time.time() if now is None else now
    with _lock_for(printer.id):
        cached = _cache.get(printer.id)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            data = printing.fetch_snapshot(printer.snapshot_url)
            data = data if data and len(data) <= MAX_PICTURE else None
        except Exception:
            data = None
        _cache[printer.id] = (now, data)
        return data


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex"><title>Cameras</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--card:#161b22;--text:#e6edf3;--muted:#8b949e;--ok:#3fb950;--warn:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.4 system-ui,sans-serif}
header{display:flex;justify-content:space-between;align-items:center;padding:10px 16px}h1{font-size:1.05rem;margin:0}.muted{color:var(--muted)}
#grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:10px;padding:0 16px 16px}
.cell{background:var(--card);border-radius:10px;overflow:hidden;position:relative}.cell img{display:block;width:100%;aspect-ratio:16/9;object-fit:cover;background:#000}
.cap{display:flex;justify-content:space-between;gap:8px;padding:6px 10px}.bar{height:4px;background:#30363d}.bar i{display:block;height:100%;background:var(--ok)}
.none{aspect-ratio:16/9;display:flex;align-items:center;justify-content:center;color:var(--muted)}
</style></head><body><header><h1>Cameras</h1><span class="muted" id="stamp"></span></header><div id="grid"></div>
<script>
const base = location.pathname.replace(/\\/$/, '');
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let tick = 0;
async function refresh() {
  tick++;
  try {
    const r = await fetch(base + '/data', {cache: 'no-store'});
    if (!r.ok) throw new Error();
    const d = await r.json(), grid = document.getElementById('grid');
    for (const p of d.printers) {
      let cell = document.getElementById('p' + p.id);
      if (!cell) { cell = document.createElement('div'); cell.className = 'cell'; cell.id = 'p' + p.id; grid.appendChild(cell); }
      const pct = typeof p.progress === 'number' ? Math.round(p.progress) : null;
      const shot = p.camera ? '<img alt="" src="' + base + '/cam/' + p.id + '.jpg?t=' + tick + '">' : '<div class="none">no camera</div>';
      cell.innerHTML = shot + '<div class="cap"><b>' + esc(p.name) + '</b><span class="muted">' + esc(p.state) + (pct != null && p.state === 'printing' ? ' ' + pct + '%' : '') + '</span></div>' + (pct != null && p.state === 'printing' ? '<div class="bar"><i style="width:' + pct + '%"></i></div>' : '');
    }
    document.getElementById('stamp').textContent = new Date().toLocaleTimeString();
  } catch (e) { document.getElementById('stamp').textContent = 'connection lost, retrying'; }
}
refresh(); setInterval(refresh, 4000);
</script></body></html>"""
