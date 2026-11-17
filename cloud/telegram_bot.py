"""Telegram bot for trade approval notifications.

Sends formatted trade tickets with inline Yes/No buttons. Handles callbacks
to approve or decline trades, then places orders via Alpaca REST API.
"""

import asyncio
import logging
from datetime import datetime

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from cloud import config, db
from cloud.alpaca_client import AlpacaClient, AlpacaError

log = logging.getLogger(__name__)


class _Conflict409Filter(logging.Filter):
    """Drop the repetitive 409 Conflict tracebacks from the Telegram updater."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and record.exc_info[1]:
            msg = str(record.exc_info[1])
            if "terminated by other getUpdates request" in msg:
                return False
        return True


APPROVE_PREFIX = "approve:"
DECLINE_PREFIX = "decline:"

EXPIRY_SECONDS = 300  # 5 minutes


def _format_ticket(order: dict, plan: dict | None, tm: dict | None,
                   risk_result: dict | None, trade_id: int) -> str:
    """Format a trade ticket for Telegram (plain text, monospace sections)."""
    symbol = order.get("symbol", "?")
    side = order.get("side", "?").upper()
    qty = order.get("qty", "?")
    paper = "PAPER" if config.alpaca_paper() else "LIVE"

    lines = [f"ORDER — {paper} account, Alpaca", ""]

    # Main order line
    stop = order.get("stop_loss_stop_price") or order.get("plan", {}).get("stop")
    target = order.get("take_profit_limit_price") or order.get("plan", {}).get("target")
    tif = {"day": "good for today", "gtc": "good until canceled"}.get(
        order.get("time_in_force", "day"), order.get("time_in_force", "day"))

    lines.append(f"{side} {qty} {symbol}, {order.get('type', 'market')}, {tif}")
    if stop:
        lines.append(f"  stop-loss {stop}")
    if target:
        lines.append(f"  take-profit {target}")

    p = order.get("plan", {})
    max_loss = p.get("max_loss")
    if max_loss:
        lines.append(f"  max loss about ${max_loss:.2f}")

    lines.append("")

    # Plan details
    if plan:
        entry = plan.get("entry", "?")
        lines.append(f"Setup: {plan.get('setup', '?')}")
        lines.append(f"Entry: ${entry} | Stop: ${plan.get('stop', '?')} | Target: ${plan.get('target', '?')}")
        rr = plan.get("reward_risk")
        rps = plan.get("risk_per_share")
        if rr and rps:
            lines.append(f"R:R = {rr:.1f} | Risk/share: ${rps:.2f}")
        lines.append("")

    # Monte Carlo
    if tm:
        sim = tm.get("simulation", {})
        hist = tm.get("history", {})
        t_pct = sim.get("target_pct")
        s_pct = sim.get("stop_pct")
        n_pct = sim.get("neither_pct")
        if t_pct is not None:
            lines.append(f"Monte Carlo: {t_pct:.0f}% target, {s_pct:.0f}% stop, {n_pct:.0f}% open")
        h_wins = hist.get("wins")
        h_total = hist.get("total")
        if h_wins is not None and h_total:
            lines.append(f"History: {round(100 * h_wins / h_total)}% win rate over {h_total} past sessions")
        lines.append("")

    # Market context
    mkt = order.get("market_context")
    if mkt:
        spy = mkt.get("spy_change_pct")
        qqq = mkt.get("qqq_change_pct")
        trend = mkt.get("trend", "?")
        parts = []
        if spy is not None:
            parts.append(f"SPY {spy:+.2f}%")
        if qqq is not None:
            parts.append(f"QQQ {qqq:+.2f}%")
        lines.append(f"Market: {', '.join(parts)} ({trend})")

    # Daily setup alignment
    daily = order.get("daily_setups")
    if daily:
        lines.append(f"Daily setups: {', '.join(daily)} (premarket match)")

    # Earnings warning
    earn = order.get("earnings_warning")
    if earn:
        lines.append(f"!! {earn}")

    lines.append("")

    # Risk verdict
    if risk_result:
        verdict = risk_result.get("verdict", "?")
        lines.append(f"Risk check: {verdict}")
        for reason in risk_result.get("reasons", []):
            lines.append(f"  - {reason}")

    # AI technical analysis
    ai = order.get("ai_reasoning")
    if ai:
        lines.append("")
        lines.append(f"AI analysis:\n{ai}")

    lines.append(f"\n(Trade #{trade_id})")
    return "\n".join(lines)


def _format_flatten_ticket(symbol: str, qty: int, current_price: float | None,
                           trade_id: int) -> str:
    paper = "PAPER" if config.alpaca_paper() else "LIVE"
    lines = [
        f"FLATTEN — {paper} account",
        "",
        f"SELL {qty} {symbol} at market",
    ]
    if current_price:
        lines.append(f"  Current price: ${current_price:.2f}")
    lines.append(f"\nFlat-by time reached. Close this position?")
    lines.append(f"\n(Trade #{trade_id})")
    return "\n".join(lines)


class TradingBot:
    def __init__(self):
        self.app: Application | None = None
        self.bot: Bot | None = None
        self.chat_id = config.telegram_chat_id()
        self._expiry_tasks: dict[int, asyncio.Task] = {}

    async def start(self):
        """Initialize and start the Telegram bot (long-polling)."""
        self.app = (
            Application.builder()
            .token(config.telegram_token())
            .build()
        )
        self.bot = self.app.bot

        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("balance", self._cmd_balance))
        self.app.add_handler(CommandHandler("today", self._cmd_today))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

        await self.app.initialize()
        await self.app.start()

        # Clear any stale webhook/polling session before we start ours
        await self.bot.delete_webhook(drop_pending_updates=True)

        # Suppress the noisy 409 Conflict tracebacks — they auto-recover
        # and flood logs when a previous deploy's session hasn't timed out
        _updater_log = logging.getLogger("telegram.ext.Updater")
        _updater_log.addFilter(_Conflict409Filter())

        await self.app.updater.start_polling(drop_pending_updates=True)
        log.info("Telegram bot started (polling)")

    async def stop(self):
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()

    # ── Send messages ──

    async def send_message(self, text: str) -> int:
        """Send a plain text message. Returns message_id."""
        msg = await self.bot.send_message(
            chat_id=self.chat_id, text=text, parse_mode=None
        )
        return msg.message_id

    async def send_trade_proposal(self, trade_id: int, order: dict,
                                  plan: dict | None, tm: dict | None,
                                  risk_result: dict | None) -> int:
        """Send a trade ticket with Yes/No inline buttons."""
        text = _format_ticket(order, plan, tm, risk_result, trade_id)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, place order", callback_data=f"{APPROVE_PREFIX}{trade_id}"),
            InlineKeyboardButton("No, skip", callback_data=f"{DECLINE_PREFIX}{trade_id}"),
        ]])

        msg = await self.bot.send_message(
            chat_id=self.chat_id, text=text, reply_markup=keyboard, parse_mode=None
        )

        db.update_trade(trade_id, status="sent", sent_at=datetime.utcnow(),
                        telegram_msg_id=msg.message_id)

        # Schedule auto-expiry
        task = asyncio.create_task(self._expire_after(trade_id, msg.message_id, text))
        self._expiry_tasks[trade_id] = task

        return msg.message_id

    async def _expire_after(self, trade_id: int, msg_id: int, original_text: str):
        """Auto-expire a trade proposal after EXPIRY_SECONDS."""
        await asyncio.sleep(EXPIRY_SECONDS)
        trade = db.get_trade(trade_id)
        if trade and trade["status"] == "sent":
            db.update_trade(trade_id, status="expired", decision="expired",
                            decided_at=datetime.utcnow())
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id, message_id=msg_id,
                    text=f"EXPIRED (no response in {EXPIRY_SECONDS // 60}min)\n\n{original_text}",
                )
            except Exception:
                pass
            log.info("Trade %d expired", trade_id)

    # ── Callback handlers ──

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        data = query.data

        if data.startswith(APPROVE_PREFIX):
            trade_id = int(data[len(APPROVE_PREFIX):])
            await self._approve_trade(trade_id, query)
        elif data.startswith(DECLINE_PREFIX):
            trade_id = int(data[len(DECLINE_PREFIX):])
            await self._decline_trade(trade_id, query)

    async def _approve_trade(self, trade_id: int, query):
        trade = db.get_trade(trade_id)
        if not trade:
            await query.edit_message_text("Trade not found.")
            return
        if trade["status"] not in ("sent", "pending"):
            await query.edit_message_text(
                f"Already {trade['status']}.\n\n{query.message.text}"
            )
            return

        # Cancel expiry task
        task = self._expiry_tasks.pop(trade_id, None)
        if task:
            task.cancel()

        db.update_trade(trade_id, decision="approved", decided_at=datetime.utcnow())

        # Place order via Alpaca
        try:
            alpaca = AlpacaClient()

            # Reconstruct the order from the trade record
            order_body = {
                "symbol": trade["symbol"],
                "side": trade["side"],
                "qty": str(int(trade["qty"])),
                "type": "market",
                "time_in_force": "day",
            }
            if trade["stop_price"] and trade["target_price"]:
                order_body["order_class"] = "bracket"
                order_body["take_profit_limit_price"] = str(trade["target_price"])
                order_body["stop_loss_stop_price"] = str(trade["stop_price"])

            result = alpaca.place_order(order_body)
            broker_id = result.get("id", "")

            db.update_trade(trade_id, status="placed", broker_order_id=broker_id,
                            placed_at=datetime.utcnow())

            await query.edit_message_text(
                f"PLACED — Order {broker_id[:12]}...\n\n{query.message.text}"
            )
            log.info("Trade %d placed: %s", trade_id, broker_id)

        except AlpacaError as e:
            db.update_trade(trade_id, status="failed", decision="approved")
            await query.edit_message_text(
                f"FAILED — {e.body[:200]}\n\n{query.message.text}"
            )
            log.error("Trade %d failed: %s", trade_id, e)

        except Exception as e:
            db.update_trade(trade_id, status="failed", decision="approved")
            await query.edit_message_text(
                f"FAILED — {str(e)[:200]}\n\n{query.message.text}"
            )
            log.exception("Trade %d unexpected error", trade_id)

    async def _decline_trade(self, trade_id: int, query):
        trade = db.get_trade(trade_id)
        if not trade:
            await query.edit_message_text("Trade not found.")
            return

        task = self._expiry_tasks.pop(trade_id, None)
        if task:
            task.cancel()

        db.update_trade(trade_id, status="declined", decision="declined",
                        decided_at=datetime.utcnow())

        await query.edit_message_text(f"SKIPPED\n\n{query.message.text}")
        log.info("Trade %d declined", trade_id)

    # ── Command handlers ──

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "Day Trading Bot active.\n\n"
            "Commands:\n"
            "/status — Current positions & today's P&L\n"
            "/balance — Account balance\n"
            "/today — Today's trade summary"
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            alpaca = AlpacaClient()
            positions = alpaca.get_positions()

            if not positions:
                await update.message.reply_text("No open positions.")
                return

            lines = ["Open positions:"]
            for p in positions:
                sym = p["symbol"]
                qty = p["qty"]
                pnl = float(p.get("unrealized_pl", 0))
                sign = "+" if pnl >= 0 else ""
                lines.append(f"  {sym}: {qty} shares ({sign}${pnl:.2f})")

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            alpaca = AlpacaClient()
            account = alpaca.get_account()
            cash = float(account.get("cash", 0))
            equity = float(account.get("equity", 0))
            bp = float(account.get("buying_power", 0))
            paper = "PAPER" if config.alpaca_paper() else "LIVE"

            await update.message.reply_text(
                f"Account ({paper}):\n"
                f"  Equity: ${equity:,.2f}\n"
                f"  Cash: ${cash:,.2f}\n"
                f"  Buying power: ${bp:,.2f}"
            )
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def _cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        trades = db.get_todays_trades()
        if not trades:
            await update.message.reply_text("No trades today.")
            return

        lines = [f"Today's trades ({len(trades)}):"]
        for t in trades:
            status = t["status"]
            sym = t["symbol"]
            side = t["side"].upper()
            qty = int(t["qty"])
            pnl = t.get("pnl")
            pnl_str = f" P&L: ${pnl:.2f}" if pnl is not None else ""
            lines.append(f"  {side} {qty} {sym} — {status}{pnl_str}")

        stats = db.trade_stats(30)
        if stats.get("total"):
            win_pct = round(100 * stats["wins"] / max(stats["total"], 1), 1)
            lines.append(f"\n30-day: {stats['total']} trades, {win_pct}% win rate")

        await update.message.reply_text("\n".join(lines))
