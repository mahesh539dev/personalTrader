# Next Steps — Deploy the Trading System

## 1. Create a Telegram Bot
- Open Telegram, message `@BotFather`
- Send `/newbot`, follow the prompts, save the **bot token**
- Message `@userinfobot` to get your **chat ID**

## 2. Create Alpaca Paper Trading Account
- Sign up at https://alpaca.markets
- Go to Paper Trading → API Keys
- Save the **API Key** and **Secret Key**

## 3. Set Up Railway
- Go to https://railway.app and create a new project
- Connect this GitHub repo
- Add the **PostgreSQL** plugin (free, click "New" → "Database" → "PostgreSQL")
- Railway auto-provides `DATABASE_URL`

## 4. Set Environment Variables in Railway
Go to your project → Variables tab and set:

```
TELEGRAM_BOT_TOKEN=<from step 1>
TELEGRAM_CHAT_ID=<from step 1>
ALPACA_API_KEY=<from step 2>
ALPACA_SECRET_KEY=<from step 2>
ALPACA_PAPER=true
SEC_USER_AGENT=Your Name you@example.com
DASHBOARD_TOKEN=<any random string for dashboard auth>
ANTHROPIC_API_KEY=<optional, for AI analysis>
OPENAI_API_KEY=<optional, for AI analysis>
TRADING_BUDGET=5000
MAX_LOSS_PER_TRADE=50
MAX_DAILY_LOSS=150
TZ=America/New_York
```

## 5. Create Two Railway Services

### Worker (orchestrator + Telegram bot)
- Start command: `python3 cloud/main.py`
- No port needed (worker process)

### Dashboard (web app)
- Start command: `gunicorn cloud.dashboard:app -b 0.0.0.0:$PORT -w 2`
- Railway assigns a public URL automatically

## 6. Verify It Works
- The Telegram bot will message you when it starts
- Open the dashboard URL in your browser, append `?token=YOUR_DASHBOARD_TOKEN`
- Save the dashboard to your phone's home screen (PWA)

## 7. First Trading Day
- **6:00 AM ET**: Pre-market scan results arrive on Telegram
- **9:50-10:30 AM ET**: ORB scan runs every 5 min, trade tickets with Yes/No buttons
- **Tap Yes**: Order placed on Alpaca paper account
- **Tap No**: Trade skipped
- **No response in 5 min**: Auto-expired
- **3:50 PM ET**: Flatten prompt for open positions
- **3:55 PM ET**: Auto-flatten anything still open
- **4:05 PM ET**: Daily summary with P&L

## 8. Go Live (when ready)
- Create Alpaca **live** API keys
- Change `ALPACA_PAPER=false` in Railway env vars
- Change `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` to live keys
- The system is identical — just real money instead of paper
