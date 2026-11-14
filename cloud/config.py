"""Configuration from environment variables.

Reads all secrets and trading parameters from env vars (set in Railway UI).
Generates trader/config.json on startup so the existing CLI tools can read it.
"""

import json
import os

REQUIRED_VARS = ("DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
ALPACA_VARS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")

TRADER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trader")
STATE_DIR = os.path.join(TRADER_DIR, "state")
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env(name: str, default: str | None = None) -> str:
    val = os.environ.get(name, default)
    if val is None:
        raise EnvironmentError(f"Missing required environment variable: {name}")
    return val


def env_bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("true", "1", "yes")


def env_float(name: str, default: float | None = None) -> float | None:
    val = os.environ.get(name)
    if val is None:
        return default
    return float(val)


def env_int(name: str, default: int | None = None) -> int | None:
    val = os.environ.get(name)
    if val is None:
        return default
    return int(val)


def validate():
    """Check that all required env vars are set. Raises on missing."""
    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        raise EnvironmentError(f"Missing required environment variables: {', '.join(missing)}")
    if not os.environ.get("ALPACA_API_KEY"):
        import logging
        logging.warning("ALPACA_API_KEY not set — order placement will be disabled")


def generate_trader_config():
    """Write trader/config.json from env vars so CLI tools can read it."""
    os.makedirs(STATE_DIR, exist_ok=True)

    config = {
        "budget": env_float("TRADING_BUDGET", 5000),
        "cash_buffer_pct": env_float("CASH_BUFFER_PCT", 10),
        "max_position_pct": env_float("MAX_POSITION_PCT", 75),
        "max_order_usd": env_float("MAX_ORDER_USD", 4000),
        "min_order_usd": env_float("MIN_ORDER_USD", 5),
        "max_loss_per_trade_usd": env_float("MAX_LOSS_PER_TRADE", 50),
        "max_daily_loss_usd": env_float("MAX_DAILY_LOSS", 150),
        "max_trades_per_day": env_int("MAX_TRADES_PER_DAY", 3),
        "max_orders_per_day": env_int("MAX_ORDERS_PER_DAY", 10),
        "min_reward_risk": env_float("MIN_REWARD_RISK", 1.0),
        "require_stop": env_bool("REQUIRE_STOP", True),
        "no_entries_before": env("NO_ENTRIES_BEFORE", "09:45"),
        "no_entries_after": env("NO_ENTRIES_AFTER", "15:30"),
        "flat_by": env("FLAT_BY", "15:55"),
        "fractional": env_bool("FRACTIONAL", False),
        "broker": env("BROKER", "alpaca"),
        "paper": env_bool("ALPACA_PAPER", True),
        "blocked": [s.strip() for s in env("BLOCKED_SYMBOLS", "").split(",") if s.strip()],
        "allow_options": env_bool("ALLOW_OPTIONS", False),
        "allow_margin": env_bool("ALLOW_MARGIN", False),
        "allow_short": env_bool("ALLOW_SHORT", False),
        "pdt_guard": env_bool("PDT_GUARD", False),
        "sec_user_agent": env("SEC_USER_AGENT", ""),
    }

    path = os.path.join(TRADER_DIR, "config.json")
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    return config


# Convenience accessors for commonly used values
def database_url() -> str:
    return env("DATABASE_URL")


def alpaca_key() -> str:
    return env("ALPACA_API_KEY", "")


def alpaca_secret() -> str:
    return env("ALPACA_SECRET_KEY", "")


def alpaca_paper() -> bool:
    return env_bool("ALPACA_PAPER", True)


def telegram_token() -> str:
    return env("TELEGRAM_BOT_TOKEN")


def telegram_chat_id() -> str:
    return env("TELEGRAM_CHAT_ID")


def dashboard_token() -> str:
    return env("DASHBOARD_TOKEN", "")


def anthropic_key() -> str:
    return env("ANTHROPIC_API_KEY", "")


def openai_key() -> str:
    return env("OPENAI_API_KEY", "")
