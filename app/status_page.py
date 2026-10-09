"""A read-only page for a wall display: each printer's state and progress, and how much is waiting. Off until you switch it on; the link holds a secret.

It only shows what the background poll already knows (it never asks a printer itself), and never an address, a key or a serial number. File names are
shown only if you choose so."""
import hmac
import secrets
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from app.models import Printer, QueueItem
from app.settings_store import get_setting, set_setting


def token(session: Session) -> Optional[str]:
    return get_setting(session, "status_token", "") or None


def settings(session: Session) -> dict:
    t = token(session)
    return {"enabled": bool(t), "path": f"/status/{t}" if t else None, "show_files": get_setting(session, "status_show_files", "") == "true"}


def configure(session: Session, enabled: Optional[bool], show_files: Optional[bool], rotate: bool) -> dict:
    if show_files is not None:
        set_setting(session, "status_show_files", "true" if show_files else "")
    if enabled is False:
        set_setting(session, "status_token", "")
    elif enabled is True and not token(session):
        set_setting(session, "status_token", secrets.token_urlsafe(24))
    if rotate and token(session):
        set_setting(session, "status_token", secrets.token_urlsafe(24))
    return settings(session)


def valid(session: Session, given: str) -> bool:
    real = token(session)
    return bool(real) and hmac.compare_digest(real.encode(), (given or "").encode())


def snapshot(session: Session) -> dict:
    from app import printwatch
    show_files = get_setting(session, "status_show_files", "") == "true"
    printers = []
    for p in session.exec(select(Printer).order_by(Printer.name)).all():
        st = printwatch.latest.get(p.id) or {}
        printers.append({"name": p.name, "online": bool(st.get("online")), "state": st.get("state") or "unknown", "progress": st.get("progress"),
                         "nozzle": st.get("nozzle"), "bed": st.get("bed"), "file": (st.get("file") if show_files else None)})
    waiting = session.exec(select(QueueItem).where(QueueItem.status == "queued")).all()
    return {"updated": datetime.utcnow().isoformat() + "Z", "printers": printers,
            "queue": {"waiting": len(waiting), "held": sum(1 for q in waiting if q.held), "printing": len(session.exec(select(QueueItem.id).where(QueueItem.status == "printing")).all())}}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Print farm</title><style>
body{margin:0;background:#14161a;color:#e7e9ee;font-family:system-ui,sans-serif;padding:20px}h1{font-size:22px;margin:0 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}.card{background:#1d2026;border:1px solid #2a2e37;border-radius:12px;padding:16px}
.name{font-size:20px;font-weight:600}.state{margin:6px 0;font-size:15px;text-transform:capitalize}.bar{height:12px;background:#2a2e37;border-radius:6px;overflow:hidden;margin:8px 0}
.bar i{display:block;height:100%;background:#4f8ef7}.muted{color:#9aa1ad;font-size:14px}.printing .state{color:#6ee7a0}.offline .state{color:#ff8a8a}.foot{margin-top:18px}
</style></head><body><h1>Print farm</h1><div class="grid" id="grid"></div><p class="muted foot" id="foot">Loading...</p>
<script>
const url = location.pathname.replace(/[/]$/, '') + '/data';
async function tick() {
  try {
    const d = await (await fetch(url, { cache: 'no-store' })).json();
    const grid = document.getElementById('grid');
    grid.replaceChildren(...d.printers.map(p => {
      const card = document.createElement('div');
      card.className = 'card ' + (p.online ? (p.state === 'printing' ? 'printing' : '') : 'offline');
      const add = (cls, text) => { const e = document.createElement('div'); e.className = cls; e.textContent = text; card.appendChild(e); return e; };
      add('name', p.name);
      add('state', p.online ? p.state : 'offline');
      if (p.progress != null && p.state === 'printing') { const bar = document.createElement('div'); bar.className = 'bar'; const i = document.createElement('i'); i.style.width = Math.max(0, Math.min(100, p.progress)) + '%'; bar.appendChild(i); card.appendChild(bar); add('muted', Math.round(p.progress) + '%'); }
      if (p.file) add('muted', p.file);
      if (p.nozzle != null || p.bed != null) add('muted', [p.nozzle != null ? 'nozzle ' + Math.round(p.nozzle) + '\u00b0' : '', p.bed != null ? 'bed ' + Math.round(p.bed) + '\u00b0' : ''].filter(Boolean).join(' \u00b7 '));
      return card;
    }));
    document.getElementById('foot').textContent = d.queue.waiting + ' waiting' + (d.queue.held ? ' (' + d.queue.held + ' on hold)' : '') + ', ' + d.queue.printing + ' printing. Updated ' + new Date(d.updated).toLocaleTimeString() + '.';
  } catch (e) { document.getElementById('foot').textContent = 'Cannot reach Model Hub.'; }
}
tick(); setInterval(tick, 10000);
</script></body></html>"""
