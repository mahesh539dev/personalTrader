"""AI-powered analysis layer using Claude or OpenAI.

Provides news sentiment analysis, trade reasoning, market context,
earnings calendar checks, sector/market trend context, and enhanced
technical analysis reasoning.
Falls back gracefully if no AI API key is configured.
"""

import json
import logging
import os
import urllib.error
import urllib.request

from cloud import config

log = logging.getLogger(__name__)


def _get_client():
    """Return (client, provider) or (None, None) if no API key set."""
    anthropic_key = config.anthropic_key()
    if anthropic_key:
        try:
            import anthropic
            return anthropic.Anthropic(api_key=anthropic_key), "claude"
        except ImportError:
            log.warning("anthropic package not installed, skipping AI analysis")
            return None, None

    openai_key = config.openai_key()
    if openai_key:
        try:
            import openai
            return openai.OpenAI(api_key=openai_key), "openai"
        except ImportError:
            log.warning("openai package not installed, skipping AI analysis")
            return None, None

    return None, None


def _ask(prompt: str, max_tokens: int = 500) -> str | None:
    """Send a prompt to the AI and return the response text."""
    client, provider = _get_client()
    if not client:
        return None

    try:
        if provider == "claude":
            response = client.messages.create(
                model="claude-haiku-5-5",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    return block.text
            return None

        elif provider == "openai":
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content

    except Exception as e:
        log.warning("AI analysis failed: %s", e)
        return None


def analyze_news_sentiment(headlines: list[dict], symbol: str) -> str | None:
    """Summarize and score news headlines for a symbol.

    Returns a short sentiment summary or None if AI is unavailable.
    """
    if not headlines:
        return None

    headline_text = "\n".join(
        f"- {h.get('title', '')} ({h.get('source', '')}, {h.get('time', '')})"
        for h in headlines[:15]
    )

    return _ask(
        f"You are a day trading analyst. Analyze these headlines for {symbol} and give:\n"
        f"1. One-line sentiment (bullish/bearish/neutral) with confidence\n"
        f"2. Key catalyst if any\n"
        f"3. Risk to watch\n\n"
        f"Headlines:\n{headline_text}\n\n"
        f"Be concise. 3-4 lines max.",
        max_tokens=200,
    )


def generate_trade_reasoning(plan: dict, tm: dict | None, risk: dict | None) -> str | None:
    """Generate a 1-2 sentence explanation of why this trade makes sense (or doesn't)."""
    if not plan:
        return None

    context = json.dumps({
        "symbol": plan.get("symbol"),
        "setup": plan.get("setup"),
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "target": plan.get("target"),
        "reward_risk": plan.get("reward_risk"),
        "mc_target_pct": tm.get("simulation", {}).get("target_pct") if tm else None,
        "mc_stop_pct": tm.get("simulation", {}).get("stop_pct") if tm else None,
        "history_wins": tm.get("history", {}).get("wins") if tm else None,
        "history_total": tm.get("history", {}).get("total") if tm else None,
    }, indent=2)

    return _ask(
        f"You are a day trading analyst. In 1-2 sentences, explain the case for and against "
        f"this trade. Be honest about the odds.\n\n{context}",
        max_tokens=150,
    )


def daily_market_briefing(scan_summary: str, news_summary: str) -> str | None:
    """Generate a pre-market briefing combining scan results and news."""
    return _ask(
        f"You are a day trading analyst. Write a 3-4 sentence pre-market briefing based on:\n\n"
        f"Scan results:\n{scan_summary}\n\n"
        f"Key headlines:\n{news_summary}\n\n"
        f"Focus on what matters for intraday trading today. No fluff.",
        max_tokens=250,
    )


def generate_technical_reasoning(plan: dict, tm: dict | None, risk: dict | None,
                                 market_ctx: dict | None = None,
                                 earnings_warning: str | None = None,
                                 daily_setups: list[str] | None = None) -> str | None:
    """Enhanced trade reasoning that includes technical context and market trend."""
    if not plan:
        return None

    context = {
        "symbol": plan.get("symbol"),
        "setup": plan.get("setup"),
        "entry": plan.get("entry"),
        "stop": plan.get("stop"),
        "target": plan.get("target"),
        "reward_risk": plan.get("reward_risk"),
        "risk_per_share": plan.get("risk_per_share"),
    }

    or_data = plan.get("opening_range") or {}
    if or_data:
        entry = plan.get("entry") or 0
        or_high = or_data.get("high") or 0
        or_low = or_data.get("low") or 0
        context["or_high"] = or_high
        context["or_low"] = or_low
        context["or_range_pct"] = round((or_high - or_low) / or_low * 100, 2) if or_low else None
        context["entry_vs_or_high_pct"] = round((entry - or_high) / or_high * 100, 2) if or_high else None

    if tm:
        sim = tm.get("simulation", {})
        hist = tm.get("history", {})
        context["mc_target_pct"] = sim.get("target_pct")
        context["mc_stop_pct"] = sim.get("stop_pct")
        context["mc_neither_pct"] = sim.get("neither_pct")
        context["mc_avg_r"] = sim.get("avg_r")
        context["history_wins"] = hist.get("wins")
        context["history_total"] = hist.get("total")

    if market_ctx:
        context["spy_change_pct"] = market_ctx.get("spy_change_pct")
        context["qqq_change_pct"] = market_ctx.get("qqq_change_pct")
        context["market_trend"] = market_ctx.get("trend")

    if earnings_warning:
        context["earnings_warning"] = earnings_warning

    if daily_setups:
        context["daily_setups"] = daily_setups

    daily_line = ""
    if daily_setups:
        daily_line = (
            f"3. Daily setup alignment ({', '.join(daily_setups)} found in premarket scan "
            f"— does this strengthen or weaken the ORB case?)\n"
        )

    return _ask(
        f"You are a day trading analyst. Analyze this ORB trade and give:\n"
        f"1. Technical assessment (opening range size, entry quality, stop distance)\n"
        f"2. Market alignment (is the trade with or against the market trend?)\n"
        f"{daily_line}"
        f"{'4' if daily_setups else '3'}. One-line verdict: take it, skip it, or size down\n\n"
        f"{json.dumps(context, indent=2)}\n\n"
        f"Be specific about numbers. {'5-6' if daily_setups else '4-5'} lines max.",
        max_tokens=350,
    )


# ── Market context (no AI needed) ──

_YAHOO_UA = "Mozilla/5.0 (compatible; day-trader-skill/2.0)"
_YAHOO_HOSTS = ("https://query2.finance.yahoo.com", "https://query1.finance.yahoo.com")


def _yahoo_get(path: str, timeout: int = 15) -> str | None:
    for host in _YAHOO_HOSTS:
        try:
            req = urllib.request.Request(host + path, headers={"User-Agent": _YAHOO_UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            continue
    return None


def get_market_context() -> dict | None:
    """Fetch SPY and QQQ intraday change to gauge market direction.

    Returns {"spy_change_pct": float, "qqq_change_pct": float, "trend": str} or None.
    """
    try:
        text = _yahoo_get("/v7/finance/spark?symbols=SPY,QQQ&range=1d&interval=5m")
        if not text:
            return None
        data = json.loads(text)
        spark = data.get("spark", {})
        results = {}
        for item in spark.get("result", []):
            sym = item.get("symbol", "")
            resp = (item.get("response") or [{}])[0]
            meta = resp.get("meta", {})
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            price = meta.get("regularMarketPrice")
            if prev and price:
                results[sym] = round((price - prev) / prev * 100, 2)

        spy = results.get("SPY")
        qqq = results.get("QQQ")
        if spy is None and qqq is None:
            return None

        avg = ((spy or 0) + (qqq or 0)) / (bool(spy) + bool(qqq))
        if avg > 0.3:
            trend = "bullish"
        elif avg < -0.3:
            trend = "bearish"
        else:
            trend = "neutral"

        return {"spy_change_pct": spy, "qqq_change_pct": qqq, "trend": trend}
    except Exception as e:
        log.warning("Market context fetch failed: %s", e)
        return None


def check_earnings_calendar(symbol: str) -> str | None:
    """Check if a symbol has earnings today or tomorrow via Yahoo Finance.

    Returns a warning string if earnings are imminent, None otherwise.
    """
    try:
        text = _yahoo_get(
            f"/v10/finance/quoteSummary/{symbol.upper().replace('.', '-')}"
            f"?modules=calendarEvents"
        )
        if not text:
            return None
        data = json.loads(text)
        result = (data.get("quoteSummary", {}).get("result") or [{}])[0]
        cal = result.get("calendarEvents", {}).get("earnings", {})
        date_raw = cal.get("earningsDate", [])
        if not date_raw:
            return None

        import datetime
        today = datetime.date.today()
        for d in date_raw:
            fmt = d.get("fmt")
            if not fmt:
                continue
            try:
                earnings_date = datetime.date.fromisoformat(fmt)
            except ValueError:
                continue
            diff = (earnings_date - today).days
            if diff == 0:
                return f"EARNINGS TODAY ({fmt}) -- high risk of gap/halt, consider skipping"
            elif diff == 1:
                return f"EARNINGS TOMORROW ({fmt}) -- holding overnight is risky"
            elif diff == -1:
                return f"EARNINGS WERE YESTERDAY ({fmt}) -- price may still be volatile"
        return None
    except Exception as e:
        log.warning("Earnings check failed for %s: %s", symbol, e)
        return None


def post_trade_review(trades: list[dict]) -> str | None:
    """Review completed trades and identify patterns."""
    if not trades:
        return None

    trade_data = json.dumps([{
        "symbol": t.get("symbol"),
        "setup": t.get("setup"),
        "pnl": float(t.get("pnl") or 0),
        "pnl_r": float(t.get("pnl_r") or 0),
        "outcome": t.get("outcome"),
        "entry_price": float(t.get("entry_price") or 0),
        "fill_price": float(t.get("fill_price") or 0),
    } for t in trades], indent=2)

    return _ask(
        f"You are a trading coach. Review these completed day trades and give:\n"
        f"1. What went well\n"
        f"2. What to improve\n"
        f"3. One pattern to watch for tomorrow\n\n"
        f"Trades:\n{trade_data}\n\n"
        f"Be specific and actionable. 4-5 lines max.",
        max_tokens=300,
    )
