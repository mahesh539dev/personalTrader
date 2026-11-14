"""Flask dashboard for win-rate analysis and trade history.

Served by Gunicorn as the second Railway service.
Save as home screen icon on mobile (PWA manifest included).
"""

import functools
import logging
import os
from datetime import date

from flask import Flask, jsonify, redirect, render_template, request

from cloud import config, db

log = logging.getLogger(__name__)

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)


def _init_app():
    """Initialize database on first request."""
    try:
        db.init()
    except Exception:
        log.warning("DB already initialized or failed")


_init_app()


def require_auth(f):
    """Simple token-based auth via cookie or query param."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        token = config.dashboard_token()
        if not token:
            return f(*args, **kwargs)

        # Check cookie
        if request.cookies.get("auth_token") == token:
            return f(*args, **kwargs)

        # Check query param and set cookie
        if request.args.get("token") == token:
            resp = redirect(request.path)
            resp.set_cookie("auth_token", token, max_age=86400 * 30, httponly=True, samesite="Lax")
            return resp

        return "Unauthorized. Append ?token=YOUR_TOKEN to the URL.", 401

    return decorated


@app.route("/")
@require_auth
def index():
    stats = db.trade_stats(30)
    win_pct = round(100 * stats["wins"] / max(stats["total"], 1), 1) if stats.get("total") else 0
    recent = db.recent_trades(10)
    setups = db.win_rate_by_setup(30)
    return render_template("index.html",
                           stats=stats, win_pct=win_pct, recent=recent, setups=setups,
                           paper=config.alpaca_paper())


@app.route("/trades")
@require_auth
def trades():
    page = int(request.args.get("page", 1))
    limit = 50
    offset = (page - 1) * limit
    all_trades = db.fetch_all(
        "SELECT * FROM trades ORDER BY created_at DESC LIMIT %s OFFSET %s",
        (limit, offset),
    )
    total = db.fetch_one("SELECT COUNT(*) as count FROM trades")
    total_count = total["count"] if total else 0
    return render_template("trades.html", trades=all_trades, page=page,
                           total=total_count, limit=limit,
                           paper=config.alpaca_paper())


@app.route("/analysis")
@require_auth
def analysis():
    days = int(request.args.get("days", 30))
    setups = db.win_rate_by_setup(days)
    stats = db.trade_stats(days)

    # Win rate by day of week
    dow_stats = db.fetch_all(
        """SELECT EXTRACT(DOW FROM date) as dow, COUNT(*) as total,
            COUNT(*) FILTER (WHERE pnl > 0) as wins,
            ROUND(SUM(pnl)::numeric, 2) as total_pnl
        FROM trade_pairs WHERE date >= CURRENT_DATE - %s
        GROUP BY dow ORDER BY dow""",
        (days,),
    )
    dow_names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    for d in dow_stats:
        d["day_name"] = dow_names[int(d["dow"])]
        d["win_pct"] = round(100 * d["wins"] / max(d["total"], 1), 1)

    return render_template("analysis.html", setups=setups, stats=stats,
                           dow_stats=dow_stats, days=days,
                           paper=config.alpaca_paper())


# ── API endpoints for Chart.js ──

@app.route("/api/equity-curve")
@require_auth
def equity_curve_api():
    days = int(request.args.get("days", 90))
    data = db.equity_curve(days)
    return jsonify({
        "labels": [str(d["date"]) for d in data],
        "equity": [float(d["total_equity"]) for d in data],
        "pnl": [float(d["pnl_today"]) for d in data],
    })


@app.route("/api/pnl-by-setup")
@require_auth
def pnl_by_setup_api():
    days = int(request.args.get("days", 30))
    data = db.win_rate_by_setup(days)
    return jsonify({
        "labels": [d["setup"] or "unknown" for d in data],
        "total_pnl": [float(d["total_pnl"] or 0) for d in data],
        "win_pct": [float(d["win_pct"] or 0) for d in data],
        "avg_r": [float(d["avg_r"] or 0) for d in data],
        "total": [int(d["total"] or 0) for d in data],
    })


@app.route("/manifest.json")
def manifest():
    return jsonify({
        "name": "Day Trader Dashboard",
        "short_name": "Trader",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#0f172a",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
        ],
    })


@app.route("/sw.js")
def service_worker():
    return app.send_static_file("sw.js"), 200, {"Content-Type": "application/javascript"}
