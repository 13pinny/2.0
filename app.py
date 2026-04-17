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
        pp = r.get("purchase_price") or 0
        sp = r.get("sale_price")
        sold = sp is not None
        r["sold"] = sold
        r["profit_loss"] = round(sp - pp, 2) if sold else None
    return rows


@app.route("/")
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/tickets")
def api_tickets():
    rows = _enrich(db.all_tickets())
    active = [r for r in rows if not r["sold"]]
    sold = [r for r in rows if r["sold"]]
    total_profit = sum(r["profit_loss"] for r in sold if r["profit_loss"] is not None)
    return jsonify({
        "tickets": rows,
        "summary": {
            "active_count": len(active),
            "sold_count": len(sold),
            "total_profit": round(total_profit, 2),
        },
        "last_run": _last_run,
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=run_scraper, daemon=True).start()
    return jsonify({"started": True})


@app.route("/export.xlsx")
def export_xlsx():
    rows = _enrich(db.all_tickets())
    wb = Workbook()
    ws = wb.active
    ws.title = "Inventory"
    ws.append([
        "Event", "Date", "Section", "Row", "Seat",
        "Purchase Price", "Sale Price", "Profit/Loss", "Status",
    ])
    for r in rows:
        ws.append([
            r.get("event_name"), r.get("event_date"), r.get("section"),
            r.get("row"), r.get("seat"),
            r.get("purchase_price"), r.get("sale_price"),
            r.get("profit_loss"), r.get("status"),
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
