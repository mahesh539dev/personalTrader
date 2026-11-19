"""Pipeline orchestrator. Runs the trading tools on a schedule.

Calls the existing Python CLI tools via subprocess and chains their JSON outputs.
Each tool runs in isolation exactly as it does on the command line.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from datetime import date, datetime, timedelta

from cloud import config, db
from cloud.ai_analyst import (
    check_earnings_calendar,
    daily_market_briefing,
    generate_technical_reasoning,
    get_market_context,
)
from cloud.alpaca_client import AlpacaClient, AlpacaError

log = logging.getLogger(__name__)

APP_DIR = config.APP_DIR
TRADER_DIR = config.TRADER_DIR


def run_tool(script: str, args: list[str], timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a trader tool CLI script and return the result."""
    cmd = ["python3", script] + args
    log.info("Running: %s", " ".join(cmd))
    start = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=APP_DIR)
    elapsed = int((time.monotonic() - start) * 1000)
    if result.returncode != 0:
        log.warning("Tool %s exited %d in %dms: %s", script, result.returncode, elapsed,
                     result.stderr[:500] if result.stderr else result.stdout[:500])
    else:
        log.info("Tool %s completed in %dms", script, elapsed)
    return result


def read_trader_json(filename: str) -> dict | list | None:
    path = os.path.join(TRADER_DIR, filename)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def _today_str() -> str:
    return date.today().isoformat()


# ── Pipeline steps ──

async def premarket_scan(bot_send_fn=None):
    """06:00 ET — Pre-market scan: universe refresh (Mon), daily scan, news."""
    if db.has_daily_scan_today():
        log.info("Premarket scan already ran today, skipping")
        return

    run_id = db.start_pipeline_run("premarket")
    steps = {}
    try:
        # Refresh universe on Mondays or if universe.txt is missing
        universe_path = os.path.join(TRADER_DIR, "universe.txt")
        if date.today().weekday() == 0 or not os.path.exists(universe_path):
            r = run_tool("trader-scanner/scan.py", ["universe"])
            steps["universe"] = {"exit_code": r.returncode, "duration_ms": 0}

        # Daily scan
        start = time.monotonic()
        r = run_tool("trader-scanner/scan.py", ["daily"], timeout=600)
        steps["daily"] = {"exit_code": r.returncode, "duration_ms": int((time.monotonic() - start) * 1000)}

        watchlist = read_trader_json("watchlist.json")
        if not watchlist or not watchlist.get("hits"):
            log.info("No pre-market hits found")
            db.finish_pipeline_run(run_id, "completed", steps)
            if bot_send_fn:
                await bot_send_fn("Pre-market scan: no setups found today.")
            return

        symbols = [h["symbol"] for h in watchlist["hits"]]
        hit_count = len(symbols)

        db.insert_scan("daily", date.today(), hit_count, symbols[:50],
                       steps["daily"].get("duration_ms"), r.stdout[:2000])

        # Persist each daily hit to scan_hits for ORB cross-referencing
        for h in watchlist["hits"]:
            hit_id = db.upsert_scan_hit(
                scan_date=date.today(),
                symbol=h["symbol"],
                scan_type="daily",
                hit_data={"setups": h.get("setups", [])},
            )
            db.update_scan_hit(hit_id, status="processed")

        # News check on top hits
        top_tickers = ",".join(symbols[:10])
        r = run_tool("trader-news/news.py", ["--tickers", top_tickers, "--hours", "12"])
        steps["news"] = {"exit_code": r.returncode}

        # Build summary for Telegram
        setups_summary = {}
        for h in watchlist["hits"][:20]:
            for s in h.get("setups", []):
                setups_summary.setdefault(s, []).append(h["symbol"])

        lines = [f"Pre-market scan: {hit_count} setups found"]
        for setup, tickers in setups_summary.items():
            lines.append(f"  {setup}: {', '.join(tickers[:5])}")

        # Check earnings for top hits and warn
        for sym in symbols[:5]:
            warning = check_earnings_calendar(sym)
            if warning:
                lines.append(f"  {sym}: {warning}")

        # AI briefing (optional)
        scan_text = "\n".join(f"{s}: {', '.join(t)}" for s, t in setups_summary.items())
        news_text = r.stdout[:500] if r.stdout else "No news checked"
        briefing = daily_market_briefing(scan_text, news_text)
        if briefing:
            lines.append(f"\n{briefing}")

        if bot_send_fn:
            await bot_send_fn("\n".join(lines))

        db.finish_pipeline_run(run_id, "completed", steps)

    except Exception as e:
        log.exception("Pre-market scan failed")
        db.finish_pipeline_run(run_id, "failed", steps, str(e))
        if bot_send_fn:
            await bot_send_fn(f"Pre-market scan failed: {e}")


async def orb_scan(bot_send_fn=None, send_trade_fn=None):
    """09:50-10:30 ET — Opening-range breakout scan + full pipeline for hits."""
    run_id = db.start_pipeline_run("orb_scan")
    steps = {}
    try:
        now_et = datetime.now()
        at_time = now_et.strftime("%H:%M")

        r = run_tool("trader-scanner/scan.py", ["orb", "--at", at_time], timeout=600)
        steps["orb"] = {"exit_code": r.returncode}

        # exit code 1 = no hits found (normal), anything else = real error
        if r.returncode not in (0, 1):
            log.warning("ORB scan at %s failed (exit %d)", at_time, r.returncode)
            db.finish_pipeline_run(run_id, "failed", steps)
            return

        watchlist = read_trader_json("watchlist.json")
        hits = watchlist.get("hits", []) if watchlist else []
        if not hits:
            log.info("ORB scan at %s: no breakouts found", at_time)
            db.finish_pipeline_run(run_id, "completed", steps)
            return

        log.info("ORB scan at %s: %d hits — %s", at_time, len(hits),
                 ", ".join(h["symbol"] for h in hits[:10]))

        symbols = [h["symbol"] for h in watchlist["hits"]]
        db.insert_scan("orb", date.today(), len(symbols), symbols[:50],
                       raw_output=r.stdout[:2000])

        # Persist each hit to the database so they survive redeploys
        for h in watchlist["hits"]:
            db.upsert_scan_hit(
                scan_date=date.today(),
                symbol=h["symbol"],
                scan_type="orb",
                scan_time=at_time,
                hit_data=h,
            )

        # Process pending hits (includes ones from earlier scans today)
        await _process_pending_orb_hits(steps, bot_send_fn, send_trade_fn)

        db.finish_pipeline_run(run_id, "completed", steps)

    except Exception as e:
        log.exception("ORB scan failed")
        db.finish_pipeline_run(run_id, "failed", steps, str(e))


async def _process_pending_orb_hits(steps: dict, bot_send_fn=None, send_trade_fn=None):
    """Process all pending ORB hits from today, including from earlier scans."""
    market_ctx = get_market_context()
    if market_ctx:
        trend = market_ctx.get("trend", "?")
        spy = market_ctx.get("spy_change_pct", 0)
        qqq = market_ctx.get("qqq_change_pct", 0)
        log.info("Market context: SPY %+.2f%%, QQQ %+.2f%% (%s)", spy, qqq, trend)

    already_proposed = {t["symbol"] for t in db.get_todays_trades()}
    pending = db.get_pending_scan_hits(date.today(), "orb")
    new_hits = [h for h in pending if h["symbol"] not in already_proposed]

    if not new_hits:
        return

    log.info("Processing %d pending ORB hits", len(new_hits))
    for hit_row in new_hits[:3]:
        hit_data = hit_row.get("hit_data") or {"symbol": hit_row["symbol"]}
        if isinstance(hit_data, str):
            import json as _json
            hit_data = _json.loads(hit_data)
        await _process_hit(
            hit_data, steps, send_trade_fn,
            market_ctx=market_ctx, hit_id=hit_row["id"],
        )


async def resume_pending_hits(bot_send_fn=None, send_trade_fn=None):
    """Called on startup to pick up unprocessed ORB hits from today."""
    pending = db.get_pending_scan_hits(date.today(), "orb")
    if not pending:
        log.info("No pending scan hits to resume")
        return

    log.info("Found %d pending scan hits from today, resuming pipeline", len(pending))
    if bot_send_fn:
        symbols = [h["symbol"] for h in pending]
        await bot_send_fn(
            f"Resuming {len(pending)} pending ORB hits from earlier: {', '.join(symbols[:10])}"
        )

    steps = {}
    await _process_pending_orb_hits(steps, bot_send_fn, send_trade_fn)


async def resume_premarket_if_missed(bot_send_fn=None):
    """Run the premarket scan on startup if it hasn't run today yet.

    Handles mid-day redeploys between 6:00 AM and market open — the
    scheduler's 6 AM job will have been missed, so we run it now to
    populate the daily scan hits that ORB cross-references later.
    """
    if db.has_daily_scan_today():
        log.info("Premarket scan already ran today, skipping")
        return

    # Only run if we're in the window where it makes sense (after 6 AM ET on a weekday)
    now_et = datetime.now()
    if now_et.weekday() >= 5:
        log.info("Weekend — skipping premarket scan")
        return

    log.info("No premarket data for today — running premarket scan now")
    await premarket_scan(bot_send_fn=bot_send_fn)


async def _process_hit(hit: dict, steps: dict, send_trade_fn=None,
                       market_ctx: dict | None = None, hit_id: int | None = None):
    """Run the full pipeline for a single ORB hit: earnings check -> entry -> exit -> timemachine -> budget -> risk."""
    symbol = hit["symbol"]
    log.info("Processing hit: %s", symbol)

    # Earnings calendar check -- skip if earnings today (high gap/halt risk)
    earnings_warning = check_earnings_calendar(symbol)
    if earnings_warning and "EARNINGS TODAY" in earnings_warning:
        log.info("%s: skipping -- %s", symbol, earnings_warning)
        steps[f"earnings_{symbol}"] = {"skipped": True, "reason": earnings_warning}
        if hit_id:
            db.update_scan_hit(hit_id, "skipped", earnings_warning)
        return

    # Entry check with volume confirmation (trigger bar must have >= 1x range avg volume)
    r = run_tool("trader-entry/entry.py", [
        "check", symbol, "--setup", "orb", "--min-volume-x", "1.0", "--write",
    ])
    steps[f"entry_{symbol}"] = {"exit_code": r.returncode}

    entry = read_trader_json("entry.json")
    if not entry or entry.get("status") != "triggered":
        status = entry.get("status", "unknown") if entry else "no data"
        reason = entry.get("reason", "") if entry else ""
        log.info("%s: entry status=%s (%s), skipping", symbol, status, reason)
        # Don't mark as skipped — it may trigger on a later scan cycle
        return

    # Exit plan
    r = run_tool("trader-exit/exit.py", ["plan", "--stop", "or-mid", "--target", "2R", "--write"])
    steps[f"exit_{symbol}"] = {"exit_code": r.returncode}

    plan = read_trader_json("plan.json")
    if not plan:
        log.warning("%s: exit plan failed (no plan.json)", symbol)
        if hit_id:
            db.update_scan_hit(hit_id, "skipped", "no exit plan")
        return
    log.info("%s: plan entry=%.2f stop=%.2f target=%.2f R:R=%.2f",
             symbol, plan.get("entry", 0), plan.get("stop", 0),
             plan.get("target", 0), plan.get("reward_risk", 0))

    # Time Machine simulation
    r = run_tool("trader-timemachine/timemachine.py", ["run", "--write"])
    steps[f"timemachine_{symbol}"] = {"exit_code": r.returncode}

    tm = read_trader_json("timemachine.json")

    # Check if this symbol also appeared in today's premarket daily scan
    daily_setups = db.get_daily_setups_for_symbol(symbol)
    if daily_setups:
        log.info("%s has daily setups: %s", symbol, daily_setups)

    # AI-enhanced technical reasoning (runs in parallel with sizing conceptually)
    ai_reasoning = generate_technical_reasoning(
        plan, tm, None, market_ctx=market_ctx, earnings_warning=earnings_warning,
        daily_setups=daily_setups,
    )
    if ai_reasoning:
        log.info("%s AI reasoning: %s", symbol, ai_reasoning[:200])

    # Fresh quote for sizing
    run_tool("trader-alerts/alerts.py", ["quote", symbol, "--write"])

    # Position sizing
    r = run_tool("trader-budget/size.py", ["--write"])
    steps[f"budget_{symbol}"] = {"exit_code": r.returncode}

    orders = read_trader_json("orders.json")
    if not orders or not orders.get("orders"):
        log.info("%s: sizer produced no orders (exit %d): %s",
                 symbol, r.returncode, r.stderr[:300] if r.stderr else r.stdout[:300])
        if hit_id:
            db.update_scan_hit(hit_id, "skipped", "budget sizer rejected")
        return

    # Risk check
    r = run_tool("trader-risk/check.py", [])
    steps[f"risk_{symbol}"] = {"exit_code": r.returncode}

    risk = read_trader_json("state/risk.json")
    if not risk or not risk.get("results"):
        if hit_id:
            db.update_scan_hit(hit_id, "skipped", "risk check failed")
        return

    # Process each order
    for result in risk["results"]:
        order_data = orders["orders"][result["index"] - 1] if result["index"] <= len(orders["orders"]) else None
        if not order_data:
            continue

        # Attach extra context to the order for the Telegram ticket
        if ai_reasoning:
            order_data["ai_reasoning"] = ai_reasoning
        if market_ctx:
            order_data["market_context"] = market_ctx
        if earnings_warning:
            order_data["earnings_warning"] = earnings_warning
        if daily_setups:
            order_data["daily_setups"] = daily_setups

        # Extract Monte Carlo results
        mc_target = mc_stop = mc_neither = None
        mc_wins = mc_total = None
        if tm:
            sim = tm.get("simulation", {})
            mc_target = sim.get("target_pct")
            mc_stop = sim.get("stop_pct")
            mc_neither = sim.get("neither_pct")
            hist = tm.get("history", {})
            mc_wins = hist.get("wins")
            mc_total = hist.get("total")

        trade_id = db.insert_trade(
            trade_date=date.today(),
            symbol=symbol,
            side=order_data.get("side", "buy"),
            qty=float(order_data.get("qty", 0)),
            setup=plan.get("setup"),
            entry_price=plan.get("entry"),
            stop_price=plan.get("stop"),
            target_price=plan.get("target"),
            risk_per_share=plan.get("risk_per_share"),
            max_loss=order_data.get("plan", {}).get("max_loss"),
            reward_risk=plan.get("reward_risk"),
            mc_target_pct=mc_target,
            mc_stop_pct=mc_stop,
            mc_neither_pct=mc_neither,
            mc_history_wins=mc_wins,
            mc_history_total=mc_total,
            risk_verdict=result["verdict"],
            risk_reasons=result.get("reasons", []),
            fingerprint=result.get("fingerprint", ""),
            paper=config.alpaca_paper(),
        )

        if result["verdict"] == "PASS" and send_trade_fn:
            await send_trade_fn(trade_id, order_data, plan, tm, result)
            if hit_id:
                db.update_scan_hit(hit_id, "processed")
        elif result["verdict"] == "BLOCK":
            db.update_trade(trade_id, status="blocked", decision="blocked")
            log.info("%s: BLOCKED — %s", symbol, result.get("reasons"))
            if hit_id:
                db.update_scan_hit(hit_id, "skipped", f"BLOCKED: {result.get('reasons')}")


async def flatten_check(bot_send_fn=None, send_trade_fn=None):
    """15:50 ET — Check for open positions and build flatten orders."""
    run_id = db.start_pipeline_run("flatten")
    try:
        alpaca = AlpacaClient()
        positions = alpaca.get_positions()

        if not positions:
            db.finish_pipeline_run(run_id, "completed")
            return

        for pos in positions:
            symbol = pos["symbol"]
            qty = abs(int(float(pos["qty"])))
            if qty == 0:
                continue

            r = run_tool("trader-broker/ticket.py", [
                "flatten", "--symbol", symbol, "--qty", str(qty)
            ])

            r = run_tool("trader-risk/check.py", [])
            risk = read_trader_json("state/risk.json")

            if risk and risk.get("results"):
                for result in risk["results"]:
                    if result["verdict"] == "PASS" and send_trade_fn:
                        orders = read_trader_json("orders.json")
                        if orders and orders.get("orders"):
                            order_data = orders["orders"][0]
                            trade_id = db.insert_trade(
                                trade_date=date.today(),
                                symbol=symbol,
                                side="sell",
                                qty=qty,
                                setup="flatten",
                                entry_price=float(pos.get("avg_entry_price", 0)),
                                stop_price=None,
                                target_price=None,
                                risk_per_share=None,
                                max_loss=None,
                                reward_risk=None,
                                mc_target_pct=None, mc_stop_pct=None, mc_neither_pct=None,
                                mc_history_wins=None, mc_history_total=None,
                                risk_verdict="PASS",
                                risk_reasons=[],
                                fingerprint=result.get("fingerprint", ""),
                                paper=config.alpaca_paper(),
                            )
                            await send_trade_fn(trade_id, order_data, None, None, result)

        db.finish_pipeline_run(run_id, "completed")

    except Exception as e:
        log.exception("Flatten check failed")
        db.finish_pipeline_run(run_id, "failed", error=str(e))


async def forced_flatten(bot_send_fn=None):
    """15:55 ET — Force-sell any remaining positions."""
    try:
        alpaca = AlpacaClient()
        positions = alpaca.get_positions()

        for pos in positions:
            symbol = pos["symbol"]
            qty = abs(int(float(pos["qty"])))
            if qty == 0:
                continue

            side = "sell" if float(pos["qty"]) > 0 else "buy"
            order = {
                "symbol": symbol,
                "side": side,
                "qty": str(qty),
                "type": "market",
                "time_in_force": "day",
            }

            try:
                # Cancel any open bracket legs first
                open_orders = alpaca.get_orders(status="open")
                for oo in open_orders:
                    if oo.get("symbol") == symbol:
                        alpaca.cancel_order(oo["id"])

                result = alpaca.place_order(order)
                log.info("Force-flattened %s: %s", symbol, result.get("id"))
                if bot_send_fn:
                    await bot_send_fn(f"Auto-flattened {qty} {symbol} at market (forced by flat-by time)")
            except AlpacaError as e:
                log.error("Failed to flatten %s: %s", symbol, e)
                if bot_send_fn:
                    await bot_send_fn(f"FAILED to flatten {symbol}: {e}")

    except Exception as e:
        log.exception("Forced flatten failed")


async def sync_fills(bot_send_fn=None):
    """Poll Alpaca for filled orders and update the database."""
    try:
        alpaca = AlpacaClient()
        today_str = date.today().isoformat()
        filled = alpaca.get_orders(status="filled", after=today_str)

        for order in filled:
            broker_id = order["id"]
            existing = db.fetch_one(
                "SELECT id FROM trades WHERE broker_order_id = %s", (broker_id,)
            )
            if existing:
                fill_price = float(order.get("filled_avg_price") or 0)
                fill_qty = float(order.get("filled_qty") or 0)
                filled_at = order.get("filled_at")
                db.update_trade(
                    existing["id"],
                    status="filled",
                    fill_price=fill_price,
                    fill_qty=fill_qty,
                    filled_at=filled_at,
                )

                # Compute P&L for sell fills
                trade = db.get_trade(existing["id"])
                if trade and trade["side"] == "sell":
                    entry_trade = db.fetch_one(
                        """SELECT * FROM trades WHERE symbol = %s AND side = 'buy'
                           AND status = 'filled' AND date = %s
                           ORDER BY created_at DESC LIMIT 1""",
                        (trade["symbol"], today_str),
                    )
                    if entry_trade and entry_trade["fill_price"]:
                        pnl = (fill_price - entry_trade["fill_price"]) * fill_qty
                        risk = entry_trade.get("risk_per_share") or 1
                        pnl_r = round(pnl / (risk * fill_qty), 2) if risk else 0
                        outcome = "target" if pnl > 0 else "stop"

                        db.update_trade(existing["id"], pnl=pnl, pnl_r=pnl_r, outcome=outcome)
                        db.insert_trade_pair(
                            entry_id=entry_trade["id"],
                            exit_id=existing["id"],
                            symbol=trade["symbol"],
                            pair_date=date.today(),
                            entry_price=entry_trade["fill_price"],
                            exit_price=fill_price,
                            qty=fill_qty,
                            pnl=pnl,
                            pnl_r=pnl_r,
                            outcome=outcome,
                        )

                        if bot_send_fn:
                            sign = "+" if pnl >= 0 else ""
                            await bot_send_fn(
                                f"FILLED: {trade['side'].upper()} {int(fill_qty)} {trade['symbol']} "
                                f"at ${fill_price:.2f} — P&L: {sign}${pnl:.2f} ({sign}{pnl_r}R)"
                            )

    except Exception as e:
        log.exception("Fill sync failed")


async def end_of_day(bot_send_fn=None):
    """16:05 ET — Sync fills, compute daily snapshot, send summary."""
    await sync_fills(bot_send_fn)

    try:
        alpaca = AlpacaClient()
        account = alpaca.get_account()
        cash = float(account.get("cash", 0))
        equity = float(account.get("equity", 0))
        positions_value = equity - cash

        today = date.today()
        pairs = db.fetch_all(
            "SELECT pnl FROM trade_pairs WHERE date = %s", (today,)
        )
        trades_today = len(pairs)
        wins = sum(1 for p in pairs if (p.get("pnl") or 0) > 0)
        losses = sum(1 for p in pairs if (p.get("pnl") or 0) < 0)
        pnl_today = sum(p.get("pnl") or 0 for p in pairs)

        db.upsert_daily_snapshot(
            today, cash, positions_value, equity,
            trades_today, wins, losses, round(pnl_today, 2),
        )

        if bot_send_fn:
            stats = db.trade_stats(30)
            win_pct = round(100 * stats["wins"] / max(stats["total"], 1), 1) if stats.get("total") else 0
            sign = "+" if pnl_today >= 0 else ""
            lines = [
                f"End of day summary:",
                f"  Today: {trades_today} trades, {wins}W/{losses}L, {sign}${pnl_today:.2f}",
                f"  Account: ${equity:,.2f}",
                f"  30-day: {stats.get('total', 0)} trades, {win_pct}% win rate, ${stats.get('total_pnl', 0):.2f}",
            ]
            await bot_send_fn("\n".join(lines))

    except Exception as e:
        log.exception("End of day failed")
