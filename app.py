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
        counts = scraper.run_and_save()
        _last_run.update(
            at=datetime.now(timezone.utc).isoformat(),
            count=counts,
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


def _enrich_viagogo(rows):
    for r in rows:
        avail = r.get("available") or 0
        sold = r.get("sold") or 0
        fv = r.get("face_value") or 0
        r["cost"] = round(avail * fv, 2)
        r["sold_cost"] = round(sold * fv, 2)
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


@app.route("/api/lysted-purchases")
def api_lysted_purchases():
    rows = db.all_lysted_purchases()
    def _live(r):
        s = (r.get("status") or "").strip().lower()
        return s != "sold"
    active = [r for r in rows if _live(r)]
    sold = [r for r in rows if not _live(r)]
    totals = {
        "tickets": sum(r.get("qty") or 0 for r in rows),
        "active_tickets": sum(r.get("qty") or 0 for r in active),
        "sold_tickets": sum(r.get("qty") or 0 for r in sold),
        "active_cost": round(sum(r.get("total_cost") or 0 for r in active), 2),
        "sold_cost": round(sum(r.get("total_cost") or 0 for r in sold), 2),
        "total_cost": round(sum(r.get("total_cost") or 0 for r in rows), 2),
    }
    return jsonify({"rows": rows, "totals": totals, "last_run": _last_run})


@app.route("/api/viagogo")
def api_viagogo():
    rows = _enrich_viagogo(db.all_viagogo())
    totals = {
        "listings": len(rows),
        "tickets_available": sum(r.get("available") or 0 for r in rows),
        "tickets_sold": sum(r.get("sold") or 0 for r in rows),
        "total_cost": round(sum(r.get("cost") or 0 for r in rows), 2),
        "sold_cost": round(sum(r.get("sold_cost") or 0 for r in rows), 2),
        "total_price": round(sum((r.get("price") or 0) * (r.get("available") or 0) for r in rows), 2),
        "total_proceeds": round(sum((r.get("proceeds") or 0) * (r.get("available") or 0) for r in rows), 2),
    }
    return jsonify({"rows": rows, "totals": totals, "last_run": _last_run})


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    threading.Thread(target=run_scraper, daemon=True).start()
    return jsonify({"started": True})


@app.route("/export.xlsx")
def export_xlsx():
    wb = Workbook()
    ws_l = wb.active
    ws_l.title = "Lysted"
    ws_l.append([
        "Event", "Date", "Time", "Venue",
        "Listings", "Tickets", "Total Cost", "Total List", "P/L",
    ])
    for r in _enrich(db.all_inventory()):
        ws_l.append([
            r.get("event_name"), r.get("event_date"), r.get("event_time"),
            r.get("venue"),
            r.get("listings_count"), r.get("tickets_count"),
            r.get("total_cost"), r.get("total_list"), r.get("profit_loss"),
        ])

    ws_p = wb.create_sheet("Lysted Tickets")
    ws_p.append([
        "Order", "Order Date", "Event", "Event Date", "Venue",
        "Section", "Row", "Qty", "Seats",
        "Delivery", "Account", "Transaction ID",
        "Total Cost", "Cost/Unit", "Status",
    ])
    for r in db.all_lysted_purchases():
        ws_p.append([
            r.get("order_id"), r.get("order_date"),
            r.get("event_name"), r.get("event_date"), r.get("venue"),
            r.get("section"), r.get("row_label"), r.get("qty"), r.get("seats"),
            r.get("delivery_type"), r.get("account_email"), r.get("transaction_id"),
            r.get("total_cost"), r.get("cost_per_unit"), r.get("status"),
        ])

    ws_v = wb.create_sheet("Viagogo")
    ws_v.append([
        "Event", "Date", "Venue", "Section", "Ticket Type",
        "Visibility", "Face Value", "Available", "Cost (Avail x Face)",
        "Price", "Proceeds", "Sold",
    ])
    for r in _enrich_viagogo(db.all_viagogo()):
        ws_v.append([
            r.get("event_name"), r.get("event_date"), r.get("venue"),
            r.get("section"), r.get("ticket_type"),
            r.get("visibility"),
            r.get("face_value"), r.get("available"), r.get("cost"),
            r.get("price"), r.get("proceeds"), r.get("sold"),
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
