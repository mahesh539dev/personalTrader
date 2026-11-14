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
    sync_fills, end_of_day,
)
from cloud.telegram_bot import TradingBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("main")


async def main():
    # Validate env and generate trader/config.json
    config.validate()
    config.generate_trader_config()
    log.info("Config loaded. Paper=%s", config.alpaca_paper())

    # Initialize database
    db.init()
    log.info("Database ready")

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

    # ORB scan loop: weekdays every 5 min from 9:50 to 10:30 AM ET
    for minute in range(50, 60, 5):
        scheduler.add_job(
            orb_scan, CronTrigger(day_of_week="mon-fri", hour=9, minute=minute),
            kwargs={"bot_send_fn": _send, "send_trade_fn": _send_trade},
            id=f"orb_9_{minute}", name=f"ORB scan 9:{minute:02d}",
            misfire_grace_time=120,
        )
    for minute in range(0, 35, 5):
        scheduler.add_job(
            orb_scan, CronTrigger(day_of_week="mon-fri", hour=10, minute=minute),
            kwargs={"bot_send_fn": _send, "send_trade_fn": _send_trade},
            id=f"orb_10_{minute}", name=f"ORB scan 10:{minute:02d}",
            misfire_grace_time=120,
        )

    # Fill tracker: every 60 seconds during market hours (9:30-16:00 ET, weekdays)
    scheduler.add_job(
        sync_fills, IntervalTrigger(seconds=60),
        kwargs={"bot_send_fn": _send},
        id="fill_tracker", name="Fill tracker",
    )

    # Flatten check: weekdays at 15:50 ET
    scheduler.add_job(
        flatten_check, CronTrigger(day_of_week="mon-fri", hour=15, minute=50),
        kwargs={"bot_send_fn": _send, "send_trade_fn": _send_trade},
        id="flatten", name="Flatten check",
        misfire_grace_time=120,
    )

    # Forced flatten: weekdays at 15:55 ET
    scheduler.add_job(
        forced_flatten, CronTrigger(day_of_week="mon-fri", hour=15, minute=55),
        kwargs={"bot_send_fn": _send},
        id="forced_flatten", name="Forced flatten",
        misfire_grace_time=60,
    )

    # End of day: weekdays at 16:05 ET
    scheduler.add_job(
        end_of_day, CronTrigger(day_of_week="mon-fri", hour=16, minute=5),
        kwargs={"bot_send_fn": _send},
        id="eod", name="End of day",
        misfire_grace_time=300,
    )

    scheduler.start()
    log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    # Send startup notification
    await bot.send_message(
        f"Trading bot started.\n"
        f"Paper: {config.alpaca_paper()}\n"
        f"Budget: ${config.env_float('TRADING_BUDGET', 5000):,.0f}\n"
        f"Jobs: {len(scheduler.get_jobs())}"
    )

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
