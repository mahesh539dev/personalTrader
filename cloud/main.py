"""Entry point: starts the orchestrator scheduler + Telegram bot in one process.

This is the single worker service that Railway runs.
"""

import asyncio
import logging
import signal
import sys

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from cloud import config, db
from cloud.orchestrator import (
    premarket_scan, orb_scan, flatten_check, forced_flatten,
    sync_fills, end_of_day, resume_pending_hits, resume_premarket_if_missed,
    close_stale_positions, is_early_close,
)
from cloud.telegram_bot import TradingBot


def _only_early(fn):
    """Wrap an async job so it runs only on early-close days."""
    async def wrapper(**kwargs):
        if not is_early_close():
            return
        await fn(**kwargs)
    wrapper.__name__ = fn.__name__ + "_early"
    return wrapper


def _skip_early(fn):
    """Wrap an async job so it skips early-close days."""
    async def wrapper(**kwargs):
        if is_early_close():
            return
        await fn(**kwargs)
    wrapper.__name__ = fn.__name__ + "_normal"
    return wrapper

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")


def _check_connections() -> list[str]:
    """Test all API connections at startup. Returns a summary list for Telegram."""
    results = []

    # Alpaca
    alpaca_key = config.alpaca_key()
    if not alpaca_key:
        log.warning("STARTUP CHECK: Alpaca API key not set -- order placement disabled")
        results.append("Alpaca: NOT CONFIGURED")
    else:
        try:
            from cloud.alpaca_client import AlpacaClient
            client = AlpacaClient()
            account = client.get_account()
            status = account.get("status", "unknown")
            equity = float(account.get("equity", 0))
            paper = "paper" if config.alpaca_paper() else "LIVE"
            log.info("STARTUP CHECK: Alpaca OK (%s, status=%s, equity=$%.2f)", paper, status, equity)
            results.append(f"Alpaca: OK ({paper}, ${equity:,.0f})")
        except Exception as e:
            log.error("STARTUP CHECK: Alpaca FAILED -- %s", e)
            results.append(f"Alpaca: FAILED ({e})")

    # Anthropic (Claude)
    anthropic_key = config.anthropic_key()
    if not anthropic_key:
        log.info("STARTUP CHECK: Anthropic API key not set -- Claude AI analysis disabled")
        results.append("Claude AI: not configured")
    else:
        try:
            import anthropic
            client = anthropic.Anthropic(api_key=anthropic_key)
            resp = client.messages.create(
                model="claude-haiku-5-5",
                max_tokens=10,
                messages=[{"role": "user", "content": "Reply with just the word OK"}],
            )
            log.info("STARTUP CHECK: Anthropic OK (claude-haiku-5-5)")
            results.append("Claude AI: OK")
        except ImportError:
            log.warning("STARTUP CHECK: anthropic package not installed")
            results.append("Claude AI: package not installed")
        except Exception as e:
            log.error("STARTUP CHECK: Anthropic FAILED -- %s", e)
            results.append(f"Claude AI: FAILED ({e})")

    # OpenAI
    openai_key = config.openai_key()
    if not openai_key:
        log.info("STARTUP CHECK: OpenAI API key not set -- OpenAI analysis disabled")
        results.append("OpenAI: not configured")
    else:
        try:
            import openai
            client = openai.OpenAI(api_key=openai_key)
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=10,
                messages=[{"role": "user", "content": "Reply with just the word OK"}],
            )
            log.info("STARTUP CHECK: OpenAI OK (gpt-4o-mini)")
            results.append("OpenAI: OK")
        except ImportError:
            log.warning("STARTUP CHECK: openai package not installed")
            results.append("OpenAI: package not installed")
        except Exception as e:
            log.error("STARTUP CHECK: OpenAI FAILED -- %s", e)
            results.append(f"OpenAI: FAILED ({e})")

    # Database (already initialized by this point, but log the confirmation)
    try:
        row = db.fetch_one("SELECT 1 as ok")
        if row and row.get("ok") == 1:
            log.info("STARTUP CHECK: Database OK")
            results.append("Database: OK")
        else:
            log.error("STARTUP CHECK: Database returned unexpected result")
            results.append("Database: ERROR")
    except Exception as e:
        log.error("STARTUP CHECK: Database FAILED -- %s", e)
        results.append(f"Database: FAILED ({e})")

    # Yahoo Finance (free, no key needed)
    try:
        from cloud.ai_analyst import get_market_context
        ctx = get_market_context()
        if ctx:
            log.info("STARTUP CHECK: Yahoo Finance OK (SPY %+.2f%%)", ctx.get("spy_change_pct", 0))
            results.append("Yahoo Finance: OK")
        else:
            log.info("STARTUP CHECK: Yahoo Finance reachable (market may be closed)")
            results.append("Yahoo Finance: OK (market closed)")
    except Exception as e:
        log.warning("STARTUP CHECK: Yahoo Finance FAILED -- %s", e)
        results.append(f"Yahoo Finance: FAILED ({e})")

    return results


async def main():
    # Validate env and generate trader/config.json
    config.validate()
    config.generate_trader_config()
    log.info("Config loaded. Paper=%s", config.alpaca_paper())

    # Initialize database
    db.init()
    log.info("Database ready")

    # Run startup health checks on all API connections
    log.info("Running startup connection checks...")
    health_results = _check_connections()
    log.info("Startup checks complete: %d services checked", len(health_results))

    # Start Telegram bot
    bot = TradingBot()
    await bot.start()
    log.info("Telegram bot ready")

    # Convenience wrappers that pass bot functions to orchestrator
    async def _send(text):
        await bot.send_message(text)

    async def _send_trade(trade_id, order, plan, tm, risk_result):
        await bot.send_trade_proposal(trade_id, order, plan, tm, risk_result)

    # Set up scheduler (all times Eastern)
    scheduler = AsyncIOScheduler(timezone="America/New_York")

    # Pre-market scan: weekdays at 6:00 AM ET
    scheduler.add_job(
        premarket_scan, CronTrigger(day_of_week="mon-fri", hour=6, minute=0),
        kwargs={"bot_send_fn": _send},
        id="premarket", name="Pre-market scan",
        misfire_grace_time=300,
    )

    # ORB scan loop: weekdays every 5 min from 9:50 AM to 12:00 PM ET
    for minute in range(50, 60, 5):
        scheduler.add_job(
            orb_scan, CronTrigger(day_of_week="mon-fri", hour=9, minute=minute),
            kwargs={"bot_send_fn": _send, "send_trade_fn": _send_trade},
            id=f"orb_9_{minute}", name=f"ORB scan 9:{minute:02d}",
            misfire_grace_time=120,
        )
    for hour in range(10, 12):
        for minute in range(0, 60, 5):
            scheduler.add_job(
                orb_scan, CronTrigger(day_of_week="mon-fri", hour=hour, minute=minute),
                kwargs={"bot_send_fn": _send, "send_trade_fn": _send_trade},
                id=f"orb_{hour}_{minute}", name=f"ORB scan {hour}:{minute:02d}",
                misfire_grace_time=120,
            )

    # Fill tracker: every 60 seconds during market hours (9:30-16:00 ET, weekdays)
    scheduler.add_job(
        sync_fills, IntervalTrigger(seconds=60),
        kwargs={"bot_send_fn": _send},
        id="fill_tracker", name="Fill tracker",
    )

    # Flatten check: 15:50 ET on normal days, 12:50 ET on early-close days
    scheduler.add_job(
        _skip_early(flatten_check), CronTrigger(day_of_week="mon-fri", hour=15, minute=50),
        kwargs={"bot_send_fn": _send, "send_trade_fn": _send_trade},
        id="flatten", name="Flatten check",
        misfire_grace_time=120,
    )
    scheduler.add_job(
        _only_early(flatten_check), CronTrigger(day_of_week="mon-fri", hour=12, minute=50),
        kwargs={"bot_send_fn": _send, "send_trade_fn": _send_trade},
        id="flatten_early", name="Flatten check (early close)",
        misfire_grace_time=120,
    )

    # Forced flatten: 15:55 ET on normal days, 12:55 ET on early-close days
    scheduler.add_job(
        _skip_early(forced_flatten), CronTrigger(day_of_week="mon-fri", hour=15, minute=55),
        kwargs={"bot_send_fn": _send},
        id="forced_flatten", name="Forced flatten",
        misfire_grace_time=60,
    )
    scheduler.add_job(
        _only_early(forced_flatten), CronTrigger(day_of_week="mon-fri", hour=12, minute=55),
        kwargs={"bot_send_fn": _send},
        id="forced_flatten_early", name="Forced flatten (early close)",
        misfire_grace_time=60,
    )

    # End of day: 16:05 ET on normal days, 13:05 ET on early-close days
    scheduler.add_job(
        _skip_early(end_of_day), CronTrigger(day_of_week="mon-fri", hour=16, minute=5),
        kwargs={"bot_send_fn": _send},
        id="eod", name="End of day",
        misfire_grace_time=300,
    )
    scheduler.add_job(
        _only_early(end_of_day), CronTrigger(day_of_week="mon-fri", hour=13, minute=5),
        kwargs={"bot_send_fn": _send},
        id="eod_early", name="End of day (early close)",
        misfire_grace_time=300,
    )

    # Safety net: close any remaining positions at 5 PM, 6 PM, and 9 AM ET
    for hour, job_id in [(17, "safety_5pm"), (18, "safety_6pm"), (9, "safety_9am")]:
        scheduler.add_job(
            close_stale_positions, CronTrigger(day_of_week="mon-fri", hour=hour, minute=0),
            kwargs={"bot_send_fn": _send},
            id=job_id, name=f"Safety close ({hour}:00)",
            misfire_grace_time=300,
        )

    scheduler.start()
    jobs = scheduler.get_jobs()
    log.info("Scheduler started with %d jobs", len(jobs))
    for j in jobs:
        log.info("  Job: %s next=%s", j.name, j.next_run_time)

    # Send startup notification immediately (before blocking scans)
    health_lines = "\n".join(f"  {r}" for r in health_results)
    job_names = [j.name for j in jobs if "ORB" not in j.name]
    orb_count = sum(1 for j in jobs if "ORB" in j.name)
    job_summary = ", ".join(job_names)
    if orb_count:
        job_summary += f", ORB scans x{orb_count}"
    await bot.send_message(
        f"Trading bot started.\n"
        f"Paper: {config.alpaca_paper()}\n"
        f"Budget: ${config.env_float('TRADING_BUDGET', 5000):,.0f}\n"
        f"Jobs ({len(jobs)}): {job_summary}\n\n"
        f"Connection checks:\n{health_lines}"
    )

    # Close any positions left over from a previous day (failed flatten)
    await close_stale_positions(bot_send_fn=_send)

    # If premarket scan was missed (deploy after 6 AM), run it now
    await resume_premarket_if_missed(bot_send_fn=_send)

    # Resume any pending scan hits from earlier today (survives redeploys)
    await resume_pending_hits(bot_send_fn=_send, send_trade_fn=_send_trade)

    # Keep running until shutdown signal
    stop_event = asyncio.Event()

    def _shutdown(sig, frame):
        log.info("Shutdown signal received")
        stop_event.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    try:
        await stop_event.wait()
    finally:
        log.info("Shutting down...")
        scheduler.shutdown(wait=False)
        await bot.stop()
        db.shutdown()
        log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
