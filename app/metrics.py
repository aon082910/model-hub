"""Prometheus metrics (text format 0.0.4) for Grafana, and a ready-made Grafana dashboard.

Off until switched on (Settings, Metrics). Printer figures come from the printer poll that already runs: nothing extra is asked of a printer when Prometheus scrapes.
If a token is set, the scrape must send it as a bearer token."""
import json
from datetime import datetime
from typing import Iterable, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from app import maintenance, print_outcomes, printwatch, stock, version
from app.config import MODEL_EXTENSIONS
from app.models import Filament, Model3D, Order, Printer, PrintLog, QueueItem
from app.settings_store import get_setting

PRINTER_STATES = ("printing", "paused", "idle", "complete", "error", "offline", "unknown")


def enabled(session: Session) -> bool:
    return get_setting(session, "metrics_enabled", "") == "true"


def _label(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


class Registry:
    def __init__(self):
        self.lines, self._seen = [], set()

    def add(self, name: str, help_text: str, kind: str, samples: Iterable) -> None:
        """samples: [(labels dict, value)]"""
        if name not in self._seen:
            self._seen.add(name)
            self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
        for labels, value in samples:
            if value is None:
                continue
            tag = "{" + ",".join(f'{k}="{_label(v)}"' for k, v in labels.items()) + "}" if labels else ""
            self.lines.append(f"{name}{tag} {float(value):g}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def render(session: Session) -> str:
    r = Registry()
    r.add("modelhub_info", "Which Model Hub this is.", "gauge", [({"version": version.VERSION}, 1)])
    printers = session.exec(select(Printer).order_by(Printer.id)).all()
    ups, states, progress, nozzle, bed, chamber, parked = [], [], [], [], [], [], []
    for p in printers:
        st = printwatch.latest.get(p.id) or {}
        who = {"printer": p.name, "kind": p.kind}
        state = st.get("state") if st.get("online") else ("offline" if st else "unknown")
        ups.append((who, 1 if st.get("online") else 0))
        for name in PRINTER_STATES:
            states.append(({**who, "state": name}, 1 if state == name else 0))
        progress.append((who, st.get("progress")))
        nozzle.append((who, st.get("nozzle")))
        bed.append((who, st.get("bed")))
        chamber.append((who, st.get("chamber")))
        parked.append((who, 1 if p.out_of_service else 0))
    r.add("modelhub_printer_up", "1 when the printer answered its last poll.", "gauge", ups)
    r.add("modelhub_printer_state", "1 for the printer's current state (printing, paused, idle...).", "gauge", states)
    r.add("modelhub_printer_progress_percent", "Progress of the print that is running.", "gauge", progress)
    r.add("modelhub_printer_nozzle_celsius", "Nozzle temperature.", "gauge", nozzle)
    r.add("modelhub_printer_bed_celsius", "Bed temperature.", "gauge", bed)
    r.add("modelhub_printer_chamber_celsius", "Chamber temperature (printers that report it).", "gauge", chamber)
    r.add("modelhub_printer_out_of_service", "1 when the printer is marked out of service.", "gauge", parked)

    logs = session.exec(select(PrintLog)).all()
    names = {p.id: p.name for p in printers}
    jobs, grams, hours = {}, {}, {}
    for log in logs:
        who = names.get(log.printer_id) or "none"
        result = "failed" if print_outcomes.is_failed(log) else "ok"
        jobs[(who, result)] = jobs.get((who, result), 0) + 1
        grams[who] = grams.get(who, 0.0) + float(log.grams or 0)
        hours[who] = hours.get(who, 0.0) + float(log.minutes or 0) / 60
    r.add("modelhub_prints_total", "Prints logged, by printer and result.", "counter", [({"printer": w, "result": res}, n) for (w, res), n in sorted(jobs.items())])
    r.add("modelhub_filament_used_grams_total", "Grams of filament logged on prints.", "counter", [({"printer": w}, g) for w, g in sorted(grams.items())])
    r.add("modelhub_print_hours_total", "Hours of printing logged.", "counter", [({"printer": w}, h) for w, h in sorted(hours.items())])

    queue = {}
    for status in session.exec(select(QueueItem.status)).all():
        queue[status] = queue.get(status, 0) + 1
    r.add("modelhub_queue_entries", "Print queue entries by status.", "gauge", [({"status": s}, n) for s, n in sorted(queue.items())])
    orders = {}
    for status in session.exec(select(Order.status)).all():
        orders[status] = orders.get(status, 0) + 1
    r.add("modelhub_orders", "Orders by status.", "gauge", [({"status": s}, n) for s, n in sorted(orders.items())])

    spools = session.exec(select(Filament)).all()
    r.add("modelhub_spool_remaining_grams", "Filament left on each spool.", "gauge",
          [({"material": s.material or "", "brand": s.brand or "", "color": s.color or "", "spool": s.id}, s.remaining_g) for s in spools])
    r.add("modelhub_filament_remaining_grams", "Filament left on all spools.", "gauge", [({}, sum(s.remaining_g for s in spools))])
    shelf = stock.overview(session)
    r.add("modelhub_stock_on_hand", "Finished parts on the shelf.", "gauge", [({"part": s["filename"] or s["id"]}, s["on_hand"]) for s in shelf])
    r.add("modelhub_stock_low", "Finished parts at or below their minimum.", "gauge", [({}, sum(1 for s in shelf if s["low"]))])
    tasks = maintenance.overview(session)
    r.add("modelhub_maintenance_due", "Maintenance tasks that are due.", "gauge", [({}, sum(1 for t in tasks if t["status"] == "due"))])
    r.add("modelhub_maintenance_soon", "Maintenance tasks that will be due soon.", "gauge", [({}, sum(1 for t in tasks if t["status"] == "soon"))])
    total = session.exec(select(func.count()).select_from(Model3D).where(Model3D.extension.in_(MODEL_EXTENSIONS))).one()
    r.add("modelhub_models", "Models in the library.", "gauge", [({}, total)])
    r.add("modelhub_scrape_timestamp_seconds", "When this page was made.", "gauge", [({}, datetime.utcnow().timestamp())])
    return r.text()


def grafana_dashboard() -> dict:
    """An importable Grafana dashboard (Dashboards, New, Import) for the metrics above; it asks which Prometheus data source to use."""
    def panel(pid, title, expr, x, y, w=12, h=8, kind="timeseries", unit=None, legend="{{printer}}"):
        p = {"id": pid, "type": kind, "title": title, "datasource": {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}, "gridPos": {"x": x, "y": y, "w": w, "h": h},
             "targets": [{"expr": expr, "legendFormat": legend, "refId": "A"}], "fieldConfig": {"defaults": {"unit": unit} if unit else {}, "overrides": []}}
        return p
    panels = [
        panel(1, "Printers online", "modelhub_printer_up", 0, 0, 8, 6, "stat", legend="{{printer}}"),
        panel(2, "Printing now", 'sum(modelhub_printer_state{state="printing"})', 8, 0, 4, 6, "stat", legend="printing"),
        panel(3, "Queue", "modelhub_queue_entries", 12, 0, 6, 6, "bargauge", legend="{{status}}"),
        panel(4, "Maintenance due", "modelhub_maintenance_due", 18, 0, 3, 6, "stat", legend="due"),
        panel(5, "Parts below minimum", "modelhub_stock_low", 21, 0, 3, 6, "stat", legend="low"),
        panel(6, "Progress", "modelhub_printer_progress_percent", 0, 6, 12, 8, unit="percent"),
        panel(7, "Temperatures", 'modelhub_printer_nozzle_celsius or modelhub_printer_bed_celsius', 12, 6, 12, 8, unit="celsius", legend="{{printer}}"),
        panel(8, "Prints per day", "sum by (result) (increase(modelhub_prints_total[1d]))", 0, 14, 12, 8, legend="{{result}}"),
        panel(9, "Filament used per day (g)", "sum(increase(modelhub_filament_used_grams_total[1d]))", 12, 14, 12, 8, legend="grams"),
        panel(10, "Spools (g left)", "modelhub_spool_remaining_grams", 0, 22, 12, 8, "bargauge", unit="g", legend="{{material}} {{color}}"),
        panel(11, "Print hours per printer", "modelhub_print_hours_total", 12, 22, 12, 8, "bargauge", legend="{{printer}}"),
    ]
    return {"__inputs": [{"name": "DS_PROMETHEUS", "label": "Prometheus", "type": "datasource", "pluginId": "prometheus", "pluginName": "Prometheus"}],
            "title": "Model Hub", "uid": "modelhub-farm", "schemaVersion": 38, "version": 1, "editable": True, "refresh": "30s", "time": {"from": "now-24h", "to": "now"},
            "tags": ["modelhub", "3d-printing"], "panels": panels, "templating": {"list": []}}
