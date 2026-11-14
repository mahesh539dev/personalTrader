# The Day Trader Skill

11 Claude skills for day trading: scan thousands of charts, track breaking news, time every entry and exit, and replay every outcome before you risk a dollar.

## How It Works

This is an automated day trading system that runs on Railway as two services. A **worker** process orchestrates the trading pipeline on a schedule and communicates via Telegram. A **dashboard** web app shows trade history, win rates, and an equity curve.

All times are US Eastern (ET). The system runs Monday through Friday on market days only.

## Daily Schedule

| Time (ET) | What happens |
|-----------|--------------|
| **6:00 AM** | **Pre-market scan.** Refreshes the stock universe (Mondays only), runs the daily scanner for setups, checks news on top hits. Results sent to Telegram. |
| **9:50 - 10:30 AM** | **ORB scan loop.** Every 5 minutes, scans for opening-range breakouts. Each hit runs through the full pipeline: entry check, exit plan, Monte Carlo simulation, position sizing, and risk check. Passing trades are sent to Telegram as tickets with Yes/No buttons. |
| **Every 60 seconds** | **Fill tracker.** Polls Alpaca for filled orders and updates the database with fill prices and P&L. |
| **3:50 PM** | **Flatten check.** Queries Alpaca for open positions and builds flatten orders. Sends close proposals to Telegram for approval. |
| **3:55 PM** | **Forced flatten.** Any positions still open are force-sold at market price. Cancels open bracket legs first. |
| **4:05 PM** | **End of day.** Final fill sync, computes daily snapshot (cash, equity, P&L), sends summary to Telegram with 30-day stats. |

## Trade Pipeline (per ORB hit)

Each stock that passes the ORB scanner goes through these steps in order:

1. **Entry check** (`trader-entry`) - confirms the setup is triggered
2. **Exit plan** (`trader-exit`) - sets stop-loss and take-profit targets (default: OR-mid stop, 1R target)
3. **Time Machine** (`trader-timemachine`) - Monte Carlo simulation using historical data to estimate win probability
4. **Position sizing** (`trader-budget`) - calculates share quantity based on budget, risk limits, and account state
5. **Risk check** (`trader-risk`) - validates against daily loss limits, max trades, and other guardrails

If all checks pass, the trade is sent to Telegram with an inline keyboard:
- **Tap Yes** - order is placed on Alpaca (bracket order with stop-loss and take-profit)
- **Tap No** - trade is declined and logged
- **No response in 5 min** - auto-expired

## Architecture

```
cloud/main.py          Entry point. Starts scheduler + Telegram bot.
cloud/orchestrator.py  Pipeline steps. Calls CLI tools via subprocess.
cloud/telegram_bot.py  Telegram bot. Trade tickets, Yes/No buttons, commands.
cloud/alpaca_client.py Alpaca REST client. Places and tracks orders.
cloud/db.py            PostgreSQL layer. Connection pool, migrations, queries.
cloud/dashboard.py     Flask web dashboard. Win rates, equity curve, trade log.
cloud/config.py        Reads env vars, generates trader/config.json.
cloud/ai_analyst.py    AI analysis (Claude Haiku 5.5 or GPT-4o-mini).
cloud/schema.sql       Database tables (auto-created on first run).
```

The 11 `trader-*` folders are standalone CLI tools. The orchestrator calls them as subprocesses and reads their JSON output files.

## AI Analysis (optional)

If an AI API key is configured, the system provides:
- **News sentiment** - bullish/bearish/neutral scoring of headlines per symbol
- **Trade reasoning** - 1-2 sentence case for/against each trade
- **Pre-market briefing** - daily summary combining scans and news
- **Post-trade review** - pattern analysis of completed trades

Set `ANTHROPIC_API_KEY` for Claude Haiku 5.5 or `OPENAI_API_KEY` for GPT-4o-mini. Claude is tried first if both are set. These are optional - the system works fully without them.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection string |
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `TELEGRAM_CHAT_ID` | Yes | From @userinfobot |
| `ALPACA_API_KEY` | Yes | Alpaca API key |
| `ALPACA_SECRET_KEY` | Yes | Alpaca secret key |
| `ALPACA_PAPER` | No | `true` (default) for paper trading |
| `SEC_USER_AGENT` | No | Required for SEC/EDGAR data |
| `DASHBOARD_TOKEN` | No | Auth token for the web dashboard |
| `ANTHROPIC_API_KEY` | No | For Claude AI analysis |
| `OPENAI_API_KEY` | No | For OpenAI AI analysis |
| `TRADING_BUDGET` | No | Total budget in USD (default: 5000) |
| `MAX_LOSS_PER_TRADE` | No | Max loss per trade in USD (default: 50) |
| `MAX_DAILY_LOSS` | No | Max daily loss in USD (default: 150) |
| `TZ` | No | Timezone (default: America/New_York) |

## Railway Deployment

Two services from the same repo:

**Worker** (orchestrator + Telegram bot):
```
python3 cloud/main.py
```

**Dashboard** (web app):
```
gunicorn cloud.dashboard:app -b 0.0.0.0:$PORT -w 2
```

## Database

The system uses PostgreSQL with 5 tables:
- `scans` - scanner results (pre-market, ORB)
- `trades` - trade proposals, decisions, fills, P&L
- `trade_pairs` - entry/exit pairs with outcomes
- `daily_snapshots` - daily equity curve data
- `pipeline_runs` - orchestrator run logs

All tables use `CREATE TABLE IF NOT EXISTS` and are auto-created on startup.

## Risk Guardrails

Built-in safety defaults (configurable via env vars):
- Paper trading enabled by default
- Max $50 loss per trade
- Max $150 daily loss
- Max 3 trades per day
- No entries before 9:45 AM or after 3:30 PM
- Mandatory stop-loss on every trade
- Auto-flatten all positions by 3:55 PM
- Options, margin, and short selling disabled by default

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## Fine print

This is a personal trading tool, not financial advice. Use at your own risk. Start with paper trading and verify the system works before risking real money.
