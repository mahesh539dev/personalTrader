"""PostgreSQL database layer.

Connection pooling, auto-migration, and query helpers for the trading system.
"""

import logging
import os
from contextlib import contextmanager
from datetime import date, datetime

import psycopg2
import psycopg2.extras
import psycopg2.pool

from cloud import config

log = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def init():
    """Initialize the connection pool and run migrations."""
    global _pool
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=1,
        maxconn=5,
        dsn=config.database_url(),
    )
    _migrate()
    log.info("Database initialized")


def _migrate():
    """Run schema.sql (all CREATE IF NOT EXISTS, safe to re-run)."""
    with open(SCHEMA_PATH) as f:
        sql = f.read()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    log.info("Schema migration complete")


@contextmanager
def get_conn():
    """Get a connection from the pool. Auto-returns on exit."""
    conn = _pool.getconn()
    try:
        yield conn
    finally:
        _pool.putconn(conn)


def execute(query: str, params: tuple = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
        conn.commit()


def fetch_one(query: str, params: tuple = None) -> dict | None:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
    return dict(row) if row else None


def fetch_all(query: str, params: tuple = None) -> list[dict]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# ── Trade helpers ──

def insert_trade(
    trade_date: date,
    symbol: str,
    side: str,
    qty: float,
    setup: str | None,
    entry_price: float | None,
    stop_price: float | None,
    target_price: float | None,
    risk_per_share: float | None,
    max_loss: float | None,
    reward_risk: float | None,
    mc_target_pct: float | None,
    mc_stop_pct: float | None,
    mc_neither_pct: float | None,
    mc_history_wins: int | None,
    mc_history_total: int | None,
    risk_verdict: str,
    risk_reasons: list[str],
    fingerprint: str,
    paper: bool = True,
) -> int:
    """Insert a trade proposal and return its id."""
    row = fetch_one(
        """INSERT INTO trades (
            date, symbol, side, qty, setup,
            entry_price, stop_price, target_price, risk_per_share, max_loss, reward_risk,
            mc_target_pct, mc_stop_pct, mc_neither_pct, mc_history_wins, mc_history_total,
            risk_verdict, risk_reasons, fingerprint, paper
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        ) RETURNING id""",
        (
            trade_date, symbol, side, qty, setup,
            entry_price, stop_price, target_price, risk_per_share, max_loss, reward_risk,
            mc_target_pct, mc_stop_pct, mc_neither_pct, mc_history_wins, mc_history_total,
            risk_verdict, risk_reasons, fingerprint, paper,
        ),
    )
    return row["id"]


def update_trade(trade_id: int, **fields) -> None:
    """Update specific fields on a trade row."""
    if not fields:
        return
    sets = ", ".join(f"{k} = %s" for k in fields)
    vals = list(fields.values()) + [trade_id]
    execute(f"UPDATE trades SET {sets}, updated_at = NOW() WHERE id = %s", tuple(vals))


def get_trade(trade_id: int) -> dict | None:
    return fetch_one("SELECT * FROM trades WHERE id = %s", (trade_id,))


def get_pending_trades() -> list[dict]:
    return fetch_all(
        "SELECT * FROM trades WHERE status IN ('pending', 'sent') ORDER BY created_at"
    )


def get_todays_trades(today: date | None = None) -> list[dict]:
    today = today or date.today()
    return fetch_all("SELECT * FROM trades WHERE date = %s ORDER BY created_at", (today,))


# ── Scan helpers ──

def insert_scan(scan_type: str, scan_date: date, hits: int, symbols: list[str],
                duration_ms: int | None = None, raw_output: str | None = None) -> int:
    row = fetch_one(
        """INSERT INTO scans (scan_type, date, hits, symbols, duration_ms, raw_output)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
        (scan_type, scan_date, hits, symbols, duration_ms, raw_output),
    )
    return row["id"]


# ── Trade pairs ──

def insert_trade_pair(entry_id: int, exit_id: int, symbol: str, pair_date: date,
                      entry_price: float, exit_price: float, qty: float,
                      pnl: float, pnl_r: float, outcome: str,
                      hold_minutes: int | None = None) -> int:
    row = fetch_one(
        """INSERT INTO trade_pairs (
            entry_id, exit_id, symbol, date, entry_price, exit_price, qty,
            pnl, pnl_r, outcome, hold_minutes
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        (entry_id, exit_id, symbol, pair_date, entry_price, exit_price, qty,
         pnl, pnl_r, outcome, hold_minutes),
    )
    return row["id"]


# ── Daily snapshots ──

def upsert_daily_snapshot(snap_date: date, cash: float, positions_value: float,
                          total_equity: float, trades_today: int = 0,
                          wins_today: int = 0, losses_today: int = 0,
                          pnl_today: float = 0) -> None:
    execute(
        """INSERT INTO daily_snapshots (date, cash, positions_value, total_equity,
            trades_today, wins_today, losses_today, pnl_today)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (date) DO UPDATE SET
            cash = EXCLUDED.cash,
            positions_value = EXCLUDED.positions_value,
            total_equity = EXCLUDED.total_equity,
            trades_today = EXCLUDED.trades_today,
            wins_today = EXCLUDED.wins_today,
            losses_today = EXCLUDED.losses_today,
            pnl_today = EXCLUDED.pnl_today""",
        (snap_date, cash, positions_value, total_equity,
         trades_today, wins_today, losses_today, pnl_today),
    )


# ── Pipeline runs ──

def start_pipeline_run(run_type: str) -> int:
    row = fetch_one(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES (%s, NOW()) RETURNING id",
        (run_type,),
    )
    return row["id"]


def finish_pipeline_run(run_id: int, status: str = "completed",
                        steps: dict | None = None, error: str | None = None) -> None:
    import json
    execute(
        """UPDATE pipeline_runs SET finished_at = NOW(), status = %s,
           steps = %s, error = %s WHERE id = %s""",
        (status, json.dumps(steps) if steps else None, error, run_id),
    )


# ── Scan hits (persistent ORB hits) ──

def upsert_scan_hit(scan_date: date, symbol: str, scan_type: str = "orb",
                    scan_time: str | None = None, hit_data: dict | None = None) -> int:
    import json
    row = fetch_one(
        """INSERT INTO scan_hits (date, symbol, scan_type, scan_time, hit_data)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (date, symbol, scan_type) DO UPDATE SET
            scan_time = EXCLUDED.scan_time, hit_data = EXCLUDED.hit_data,
            status = 'pending'
        RETURNING id""",
        (scan_date, symbol, scan_type, scan_time,
         json.dumps(hit_data) if hit_data else None),
    )
    return row["id"]


def get_pending_scan_hits(scan_date: date | None = None,
                          scan_type: str = "orb") -> list[dict]:
    scan_date = scan_date or date.today()
    return fetch_all(
        """SELECT * FROM scan_hits
        WHERE date = %s AND scan_type = %s AND status = 'pending'
        ORDER BY created_at""",
        (scan_date, scan_type),
    )


def get_daily_setups_for_symbol(symbol: str, scan_date: date | None = None) -> list[str]:
    """Return the daily setup names for a symbol from today's premarket scan."""
    scan_date = scan_date or date.today()
    row = fetch_one(
        """SELECT hit_data FROM scan_hits
        WHERE date = %s AND symbol = %s AND scan_type = 'daily'""",
        (scan_date, symbol),
    )
    if not row or not row.get("hit_data"):
        return []
    import json
    data = row["hit_data"]
    if isinstance(data, str):
        data = json.loads(data)
    return data.get("setups", [])


def has_daily_scan_today(scan_date: date | None = None) -> bool:
    """Return True if any daily scan hits exist for the given date."""
    scan_date = scan_date or date.today()
    row = fetch_one(
        "SELECT 1 FROM scan_hits WHERE date = %s AND scan_type = 'daily' LIMIT 1",
        (scan_date,),
    )
    return row is not None


def count_scan_hits_today(scan_type: str = "orb", scan_date: date | None = None) -> int:
    scan_date = scan_date or date.today()
    row = fetch_one(
        "SELECT COUNT(*) as cnt FROM scan_hits WHERE date = %s AND scan_type = %s",
        (scan_date, scan_type),
    )
    return row["cnt"] if row else 0


def update_scan_hit(hit_id: int, status: str, skip_reason: str | None = None) -> None:
    execute(
        """UPDATE scan_hits SET status = %s, skip_reason = %s, processed_at = NOW()
        WHERE id = %s""",
        (status, skip_reason, hit_id),
    )


# ── Dashboard queries ──

def win_rate_by_setup(days: int = 30) -> list[dict]:
    return fetch_all(
        """SELECT COALESCE(t.setup, 'unknown') as setup, COUNT(*) as total,
            COUNT(*) FILTER (WHERE tp.pnl > 0) as wins,
            ROUND(100.0 * COUNT(*) FILTER (WHERE tp.pnl > 0) / NULLIF(COUNT(*), 0), 1) as win_pct,
            ROUND(AVG(tp.pnl_r)::numeric, 2) as avg_r,
            ROUND(SUM(tp.pnl)::numeric, 2) as total_pnl
        FROM trade_pairs tp
        LEFT JOIN trades t ON t.id = tp.entry_id
        WHERE tp.date >= CURRENT_DATE - %s
        GROUP BY t.setup ORDER BY total DESC""",
        (days,),
    )


def equity_curve(days: int = 90) -> list[dict]:
    return fetch_all(
        """SELECT date, total_equity, pnl_today, trades_today, wins_today
        FROM daily_snapshots WHERE date >= CURRENT_DATE - %s
        ORDER BY date""",
        (days,),
    )


def recent_trades(limit: int = 50) -> list[dict]:
    return fetch_all(
        "SELECT * FROM trades ORDER BY created_at DESC LIMIT %s",
        (limit,),
    )


def trade_stats(days: int = 30) -> dict:
    row = fetch_one(
        """SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE pnl > 0) as wins,
            COUNT(*) FILTER (WHERE pnl < 0) as losses,
            COUNT(*) FILTER (WHERE pnl = 0 OR pnl IS NULL) as scratch,
            ROUND(COALESCE(SUM(pnl), 0)::numeric, 2) as total_pnl,
            ROUND(COALESCE(AVG(pnl_r), 0)::numeric, 2) as avg_r
        FROM trade_pairs WHERE date >= CURRENT_DATE - %s""",
        (days,),
    )
    return row or {}


def shutdown():
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
