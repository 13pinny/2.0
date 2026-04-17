import io
import threading
import traceback
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, send_file
from openpyxl import Workbook

import db
import scraper

load_dotenv()

app = Flask(__name__)
db.init()

_last_run = {"at": None, "count": 0, "error": None, "running": False}
_run_lock = threading.Lock()


def run_scraper():
    if not _run_lock.acquire(blocking=False):
        return
    _last_run["running"] = True
    try:
        count = scraper.run_and_save()
        _last_run.update(
            at=datetime.now(timezone.utc).isoformat(),
            count=count,
            error=None,
        )
    except Exception as e:
        _last_run.update(
            at=datetime.now(timezone.utc).isoformat(),
            error=f"{type(e).__name__}: {e}",
        )
        traceback.print_exc()
    finally:
        _last_run["running"] = False
        _run_lock.release()


def _enrich(rows):
    for r in rows:
        cost = r.get("total_cost")
        lst = r.get("total_list")
        r["profit_loss"] = round(lst - cost, 2) if cost is not None and lst is not None else None
    return rows


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/inventory")
def api_inventory():
    rows = _enrich(db.all_inventory())
    totals = {
        "events": len(rows),
        "listings": sum(r.get("listings_count") or 0 for r in rows),
        "tickets": sum(r.get("tickets_count") or 0 for r in rows),
        "total_cost": round(sum(r.get("total_cost") or 0 for r in rows), 2),
        "total_list": round(sum(r.get("total_list") or 0 for r in rows), 2),
    }
    totals["total_pl"] = round(totals["total_list"] - totals["total_cost"], 2)
    return jsonify({"rows": rows, "totals": totals, "last_run": _last_run})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=run_scraper, daemon=True).start()
    return jsonify({"started": True})


@app.route("/export.xlsx")
def export_xlsx():
    rows = _enrich(db.all_inventory())
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append([
        "Event", "Date", "Time", "Venue",
        "Listings", "Tickets", "Total Cost", "Total List", "P/L",
    ])
    for r in rows:
        ws.append([
            r.get("event_name"), r.get("event_date"), r.get("event_time"),
            r.get("venue"),
            r.get("listings_count"), r.get("tickets_count"),
            r.get("total_cost"), r.get("total_list"), r.get("profit_loss"),
        ])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"kartis-{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(run_scraper, "interval", hours=1, id="scrape")
scheduler.start()


if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=False)
