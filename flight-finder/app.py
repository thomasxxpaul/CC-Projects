import os
import json
import sqlite3
import threading
import smtplib
from email.mime.text import MIMEText
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from flask import Flask, jsonify, render_template, request, Response, stream_with_context

app = Flask(__name__)

SERPAPI_URL        = "https://serpapi.com/search"
KEYS_FILE          = Path(".serpapi_keys.json")

def _load_keys_data():
    if KEYS_FILE.exists():
        try: return json.loads(KEYS_FILE.read_text())
        except: pass
    return {"active": None, "keys": []}

def _save_keys_data(data):
    KEYS_FILE.write_text(json.dumps(data, indent=2))

def _active_key():
    if os.environ.get("SERPAPI_KEY"):
        return os.environ["SERPAPI_KEY"]
    d = _load_keys_data()
    return d.get("active") or ""

def _alert_key():
    d = _load_keys_data()
    ak = d.get("alert_key")
    if ak:
        return ak
    return _active_key()  # fall back to search key if no alert key set

SERPAPI_KEY = _active_key()

def _load_email_config():
    d = _load_keys_data()
    return (
        d.get("email_from", os.environ.get("ALERT_EMAIL_FROM", "")),
        d.get("email_pass", os.environ.get("ALERT_EMAIL_PASS", ""))
    )

ALERT_EMAIL_FROM, ALERT_EMAIL_PASS = _load_email_config()
ALERTS_FILE        = Path("alerts.json")
DB_PATH            = Path("prices.db")
CACHE_DAYS         = 3   # reuse a price for up to 3 days before re-fetching

# US federal holidays (Mon–Thu that are still flyable)
HOLIDAYS = {
    "2025-11-27", "2025-11-28", "2025-12-24", "2025-12-25", "2025-12-31",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25",
    "2026-07-03",  # Independence Day observed (Jul 4 is Saturday)
    "2026-09-07", "2026-10-12", "2026-11-11", "2026-11-26", "2026-11-27",
    "2026-12-24", "2026-12-25", "2026-12-31",
    "2027-01-01",
}

def is_flyable(date_str):
    d = date.fromisoformat(date_str)
    return d.weekday() >= 4 or date_str in HOLIDAYS  # Fri/Sat/Sun or holiday

ORIGINS = ["SJC", "SFO"]

ALL_DESTINATIONS = {
    # West Coast
    "LAX": "Los Angeles",
    "LAS": "Las Vegas",
    "SEA": "Seattle",
    "PDX": "Portland",
    "PHX": "Phoenix",
    "SAN": "San Diego",
    "SLC": "Salt Lake City",
    # Mountain / Central
    "DEN": "Denver",
    "ORD": "Chicago",
    "AUS": "Austin",
    "MSY": "New Orleans",
    # East Coast
    "JFK": "New York",
    "BOS": "Boston",
    "MIA": "Miami",
    "ATL": "Atlanta",
    "MCO": "Orlando",
    "BNA": "Nashville",
    # Hawaii
    "HNL": "Honolulu",
    "OGG": "Maui",
}

# Destinations used for API fetching (quota-friendly 8)
ALERT_DESTINATIONS = ["LAX","LAS","SEA","DEN","JFK","MIA","PHX","SAN"]

DEFAULT_DESTINATIONS = list(ALL_DESTINATIONS.keys())
SEARCH_DESTINATIONS  = ["LAX","LAS","SEA","DEN","JFK","MIA","PHX","SAN"]


# ---------------------------------------------------------------------------
# Database — caching + price history
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            origin       TEXT NOT NULL,
            destination  TEXT NOT NULL,
            depart_date  TEXT NOT NULL,
            return_date  TEXT NOT NULL DEFAULT '',
            price        REAL NOT NULL,
            stops        INTEGER,
            duration     TEXT,
            fetched_date TEXT NOT NULL,
            PRIMARY KEY (origin, destination, depart_date, return_date, fetched_date)
        )
    """)
    conn.commit()
    conn.close()

init_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def date_range(start_str, end_str):
    d = date.fromisoformat(start_str)
    e = date.fromisoformat(end_str)
    while d <= e:
        yield str(d)
        d += timedelta(days=1)


def _mins_to_str(mins):
    if not mins:
        return ""
    h, m = divmod(int(mins), 60)
    return f"{h}h {m}m" if m else f"{h}h"


def fetch_offer(origin, destination, depart_date, return_date=None):
    import requests as req
    ret   = return_date or ""
    today = str(date.today())

    # ── Cache check (valid for CACHE_DAYS) ───────────────────────────────────
    cutoff = str(date.today() - timedelta(days=CACHE_DAYS - 1))
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM prices WHERE origin=? AND destination=? AND depart_date=? AND return_date=? AND fetched_date>=? ORDER BY fetched_date DESC LIMIT 1",
            (origin, destination, depart_date, ret, cutoff)
        ).fetchone()
        conn.close()
        if row:
            return {
                "origin":      row["origin"],
                "destination": row["destination"],
                "departDate":  row["depart_date"],
                "returnDate":  row["return_date"],
                "price":       row["price"],
                "currency":    "USD",
                "stops":       row["stops"],
                "duration":    row["duration"],
                "fetchedAt":   row["fetched_date"],
                "cached":      True,
            }
    except Exception:
        pass

    # ── SerpAPI call ─────────────────────────────────────────────────────────
    if not SERPAPI_KEY:
        return None  # no key and no cache hit — skip silently
    try:
        params = {
            "engine":        "google_flights",
            "departure_id":  origin,
            "arrival_id":    destination,
            "outbound_date": depart_date,
            "currency":      "USD",
            "hl":            "en",
            "type":          "1" if return_date else "2",
            "api_key":       SERPAPI_KEY,
        }
        if return_date:
            params["return_date"] = return_date

        r = req.get(SERPAPI_URL, params=params, timeout=15)
        if not r.ok:
            return None

        body = r.json()
        all_options = body.get("best_flights", []) + body.get("other_flights", [])
        if not all_options:
            return None

        cheapest = min(all_options, key=lambda x: x.get("price", 99999))
        price = cheapest.get("price")
        if not price:
            return None

        legs = cheapest.get("flights", [])
        result = {
            "origin":      origin,
            "destination": destination,
            "departDate":  depart_date,
            "returnDate":  ret,
            "price":       float(price),
            "currency":    "USD",
            "stops":       len(legs) - 1,
            "duration":    _mins_to_str(cheapest.get("total_duration", 0)),
            "fetchedAt":   today,
            "cached":      False,
        }

        # ── Save to DB ───────────────────────────────────────────────────────
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT OR REPLACE INTO prices "
                "(origin, destination, depart_date, return_date, price, stops, duration, fetched_date) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (origin, destination, depart_date, ret,
                 result["price"], result["stops"], result["duration"], today)
            )
            conn.commit()
            conn.close()
        except Exception:
            pass

        return result
    except Exception:
        return None


def _parse_destinations(param):
    if not param:
        return SEARCH_DESTINATIONS
    dests = [d.strip() for d in param.split(",") if d.strip() in ALL_DESTINATIONS]
    return dests or SEARCH_DESTINATIONS


def run_search(date_from, date_to, return_date=None, destinations=None):
    if destinations is None:
        destinations = DEFAULT_DESTINATIONS
    dates = [d for d in date_range(date_from, date_to) if is_flyable(d)]
    tasks = [
        (origin, dest, d, return_date)
        for origin in ORIGINS
        for dest in destinations
        for d in dates
    ]
    results = []
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {pool.submit(fetch_offer, *t): t for t in tasks}
        for future in as_completed(futures):
            result = future.result()
            if result:
                results.append(result)

    seen = {}
    for r in results:
        key = (r["origin"], r["destination"], r["departDate"])
        if key not in seen or r["price"] < seen[key]["price"]:
            seen[key] = r

    return sorted(seen.values(), key=lambda x: x["price"])


# ---------------------------------------------------------------------------
# Price alerts
# ---------------------------------------------------------------------------

def load_alerts():
    if ALERTS_FILE.exists():
        return json.loads(ALERTS_FILE.read_text())
    return []


def save_alert(alert):
    alerts = [a for a in load_alerts() if a["email"] != alert["email"]]
    alerts.append(alert)
    ALERTS_FILE.write_text(json.dumps(alerts, indent=2))


def send_email(to, subject, body_html):
    if not ALERT_EMAIL_FROM or not ALERT_EMAIL_PASS:
        print(f"[ALERT] No email config — deal found for {to}: {subject}")
        return
    msg = MIMEText(body_html, "html")
    msg["Subject"] = subject
    msg["From"]    = ALERT_EMAIL_FROM
    msg["To"]      = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(ALERT_EMAIL_FROM, ALERT_EMAIL_PASS)
        smtp.send_message(msg)
    print(f"[ALERT] Email sent to {to}", flush=True)


def check_alerts():
    # Temporarily swap to the alert key for all fetch_offer calls
    global SERPAPI_KEY
    saved_key  = SERPAPI_KEY
    SERPAPI_KEY = _alert_key()

    # Check 3-8 weeks out, Fridays and Saturdays only
    # Round trip: Fri→Sun (+2), Sat→Mon (+2)
    start = date.today() + timedelta(weeks=3)
    end   = date.today() + timedelta(weeks=8)
    depart_return_pairs = []
    for d in date_range(str(start), str(end)):
        dd = date.fromisoformat(d)
        if dd.weekday() == 4:    # Friday → return Sunday
            depart_return_pairs.append((d, str(dd + timedelta(days=2))))
        elif dd.weekday() == 5:  # Saturday → return Monday
            depart_return_pairs.append((d, str(dd + timedelta(days=2))))

    alerts = load_alerts()
    print(f"[ALERT] Running check: {len(alerts)} alert(s), {len(depart_return_pairs)} date pairs", flush=True)

    for alert in alerts:
        try:
            top_routes = alert.get("topRoutes", [])
            if not top_routes:
                continue
            threshold = float(alert["threshold"])

            # Fetch all results
            all_results = []
            for route in top_routes:
                for depart_date, return_date in depart_return_pairs:
                    result = fetch_offer(
                        route["origin"], route["destination"],
                        depart_date, return_date,
                    )
                    if result:
                        all_results.append(result)

            # Keep cheapest per route, sort, take top 10
            best = {}
            for d in all_results:
                k = (d["origin"], d["destination"])
                if k not in best or d["price"] < best[k]["price"]:
                    best[k] = d
            top10 = sorted(best.values(), key=lambda x: x["price"])[:10]

            print(f"[ALERT] Found {len(top10)} deals for {alert['email']}", flush=True)
            if top10:
                rows = "".join(
                    f"<tr style=\"background:{'#FEF3C7' if d['price'] <= threshold else 'white'}\">"
                    f"<td>{d['origin']} → {d['destination']}</td>"
                    f"<td>{d['departDate']} → {d['returnDate']}</td>"
                    f"<td><b>${d['price']:.0f}</b>"
                    f"{'&nbsp;🔥' if d['price'] <= threshold else ''}</td></tr>"
                    for d in top10
                )
                hot_count = sum(1 for d in top10 if d["price"] <= threshold)
                subject = (
                    f"🔥 {hot_count} deal{'s' if hot_count!=1 else ''} under ${threshold:.0f} — Top 10 this week"
                    if hot_count else
                    "Your top 10 flight deals this week"
                )
                body = f"""
                <h2>Your Top 10 Deals This Week</h2>
                <p>Round trip from SJC &nbsp;·&nbsp; 3–8 weeks out &nbsp;·&nbsp; Fri/Sat departures</p>
                {'<p style="color:#D97706;font-weight:bold">🔥 Highlighted rows are under your $' + f'{threshold:.0f} threshold!</p>' if hot_count else ''}
                <table border="1" cellpadding="8" style="border-collapse:collapse;font-family:sans-serif">
                  <tr style="background:#F1F5F9"><th>Route</th><th>Depart → Return</th><th>Price (RT)</th></tr>
                  {rows}
                </table>
                <p style="color:#94A3B8;font-size:12px">Sent every Tuesday at 6pm &nbsp;·&nbsp; Open the app to book</p>
                """
                send_email(alert["email"], subject, body)
        except Exception as e:
            print(f"[ALERT] Error checking alert for {alert['email']}: {e}", flush=True)

    SERPAPI_KEY = saved_key  # restore search key
    _schedule_next_alert()


def _schedule_next_alert():
    """Schedule next run for Tuesday at 6pm local time."""
    import datetime as dt
    now      = dt.datetime.now()
    # Find next Tuesday (weekday=1)
    days_ahead = (1 - now.weekday()) % 7
    if days_ahead == 0 and now.hour >= 18:
        days_ahead = 7  # already past 6pm Tuesday, wait for next week
    next_run = now.replace(hour=18, minute=0, second=0, microsecond=0) + dt.timedelta(days=days_ahead)
    delay = (next_run - now).total_seconds()
    print(f"[ALERT] Next check scheduled for {next_run.strftime('%A %b %d at %I:%M %p')} ({delay/3600:.1f}h from now)")
    threading.Timer(delay, check_alerts).start()


_schedule_next_alert()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", all_destinations=ALL_DESTINATIONS)


@app.route("/api/search")
def search():
    if not SERPAPI_KEY:
        return jsonify({"error": "Missing SERPAPI_KEY environment variable."}), 500
    date_from   = request.args.get("dateFrom",   str(date.today()))
    date_to     = request.args.get("dateTo",     str(date.today() + timedelta(days=6)))
    return_date = request.args.get("returnDate") or None
    destinations = _parse_destinations(request.args.get("destinations", ""))
    return jsonify(run_search(date_from, date_to, return_date, destinations))


@app.route("/api/search/stream")
def search_stream():

    date_from    = request.args.get("dateFrom",   str(date.today()))
    date_to      = request.args.get("dateTo",     str(date.today() + timedelta(days=6)))
    return_date  = request.args.get("returnDate") or None
    destinations = _parse_destinations(request.args.get("destinations", ""))

    def generate():
        dates = [d for d in date_range(date_from, date_to) if is_flyable(d)]
        all_tasks = [
            (origin, dest, d, return_date)
            for origin in ORIGINS
            for dest in destinations
            for d in dates
        ]

        # ── Pre-check DB: split into cached vs needs-API ──────────────────────
        cutoff = str(date.today() - timedelta(days=CACHE_DAYS - 1))
        ret_val = return_date or ""
        cached_results, uncached_tasks = [], []

        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            for (org, dst, dep, ret) in all_tasks:
                row = conn.execute(
                    "SELECT * FROM prices WHERE origin=? AND destination=? AND depart_date=? AND return_date=? AND fetched_date>=? ORDER BY fetched_date DESC LIMIT 1",
                    (org, dst, dep, ret_val, cutoff)
                ).fetchone()
                if row:
                    cached_results.append({
                        "origin":      row["origin"],
                        "destination": row["destination"],
                        "departDate":  row["depart_date"],
                        "returnDate":  row["return_date"],
                        "price":       row["price"],
                        "currency":    "USD",
                        "stops":       row["stops"],
                        "duration":    row["duration"],
                        "fetchedAt":   row["fetched_date"],
                        "cached":      True,
                    })
                else:
                    uncached_tasks.append((org, dst, dep, ret or None))
            conn.close()
        except Exception:
            uncached_tasks = all_tasks

        # Tell the client how many API calls we'll make
        yield f"event: apicalls\ndata: {json.dumps({'cached': len(cached_results), 'live': len(uncached_tasks)})}\n\n"

        # ── Stream cached hits immediately ────────────────────────────────────
        seen = {}
        for result in cached_results:
            key = (result["origin"], result["destination"], result["departDate"])
            if key not in seen or result["price"] < seen[key]["price"]:
                seen[key] = result
                yield f"data: {json.dumps(result)}\n\n"

        # ── Fetch only uncached routes from API ───────────────────────────────
        if uncached_tasks and SERPAPI_KEY:
            with ThreadPoolExecutor(max_workers=20) as pool:
                futures = {pool.submit(fetch_offer, *t): t for t in uncached_tasks}
                for future in as_completed(futures):
                    result = future.result()
                    if result:
                        key = (result["origin"], result["destination"], result["departDate"])
                        if key not in seen or result["price"] < seen[key]["price"]:
                            seen[key] = result
                            yield f"data: {json.dumps(result)}\n\n"

        yield "event: done\ndata: {}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/price-history")
def price_history():
    origin      = request.args.get("origin", "")
    destination = request.args.get("destination", "")
    depart_date = request.args.get("departDate", "")
    return_date = request.args.get("returnDate", "")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT price, fetched_date FROM prices "
        "WHERE origin=? AND destination=? AND depart_date=? AND return_date=? "
        "ORDER BY fetched_date ASC",
        (origin, destination, depart_date, return_date)
    ).fetchall()
    conn.close()

    return jsonify([{"price": r["price"], "date": r["fetched_date"]} for r in rows])


def _mask(key):
    return ("••••" + key[-4:]) if len(key) > 4 else "••••"

@app.route("/api/keys")
def list_keys():
    d        = _load_keys_data()
    alert_k  = d.get("alert_key")
    return jsonify({
        "keys": [{
            "name":     k["name"],
            "masked":   _mask(k["key"]),
            "active":   k["key"] == SERPAPI_KEY,
            "is_alert": k["key"] == alert_k,
        } for k in d["keys"]],
        "active": bool(SERPAPI_KEY),
    })

@app.route("/api/keys/add", methods=["POST"])
def add_key():
    data = request.json or {}
    key  = data.get("key", "").strip()
    name = data.get("name", "").strip() or "Key"
    if not key:
        return jsonify({"error": "No key provided."}), 400
    d = _load_keys_data()
    # avoid duplicates
    if not any(k["key"] == key for k in d["keys"]):
        d["keys"].append({"name": name, "key": key})
    _save_keys_data(d)
    return jsonify({"ok": True})

@app.route("/api/keys/select", methods=["POST"])
def select_key():
    global SERPAPI_KEY
    data = request.json or {}
    name = data.get("name", "")
    d    = _load_keys_data()
    match = next((k for k in d["keys"] if k["name"] == name), None)
    if not match:
        return jsonify({"error": "Key not found."}), 404
    d["active"] = match["key"]
    _save_keys_data(d)
    SERPAPI_KEY = match["key"]
    return jsonify({"ok": True, "masked": _mask(SERPAPI_KEY)})

@app.route("/api/email-config", methods=["GET"])
def get_email_config():
    return jsonify({"from": ALERT_EMAIL_FROM, "set": bool(ALERT_EMAIL_FROM and ALERT_EMAIL_PASS)})

@app.route("/api/email-config", methods=["POST"])
def save_email_config():
    global ALERT_EMAIL_FROM, ALERT_EMAIL_PASS
    data = request.json or {}
    frm  = data.get("from", "").strip()
    pwd  = data.get("pass", "").strip()
    d    = _load_keys_data()
    d["email_from"] = frm
    d["email_pass"] = pwd
    _save_keys_data(d)
    ALERT_EMAIL_FROM = frm
    ALERT_EMAIL_PASS = pwd
    return jsonify({"ok": True})

@app.route("/api/keys/set-alert-key", methods=["POST"])
def set_alert_key():
    data = request.json or {}
    name = data.get("name", "")
    d    = _load_keys_data()
    if name == "":
        d["alert_key"] = None
    else:
        match = next((k for k in d["keys"] if k["name"] == name), None)
        if not match:
            return jsonify({"error": "Key not found."}), 404
        d["alert_key"] = match["key"]
    _save_keys_data(d)
    return jsonify({"ok": True})

@app.route("/api/keys/delete", methods=["POST"])
def delete_key():
    global SERPAPI_KEY
    data = request.json or {}
    name = data.get("name", "")
    d    = _load_keys_data()
    removed = next((k for k in d["keys"] if k["name"] == name), None)
    d["keys"] = [k for k in d["keys"] if k["name"] != name]
    if removed and removed["key"] == SERPAPI_KEY:
        d["active"] = d["keys"][0]["key"] if d["keys"] else None
        SERPAPI_KEY = d["active"] or ""
    _save_keys_data(d)
    return jsonify({"ok": True})


@app.route("/api/trigger-alert", methods=["POST"])
def trigger_alert():
    def _run():
        try:
            check_alerts()
        except Exception as e:
            import traceback
            print(f"[ALERT] Uncaught error in check_alerts: {e}")
            traceback.print_exc()
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "msg": "Alert check started in background."})


@app.route("/api/set-alert", methods=["POST"])
def set_alert():
    data = request.json
    required = ["email", "threshold", "dateFrom", "dateTo"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "Missing fields."}), 400
    save_alert(data)
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5050, use_reloader=False)
