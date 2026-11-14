"""AI-powered analysis layer using Claude or OpenAI.

Provides news sentiment analysis, trade reasoning, and market context.
Falls back gracefully if no AI API key is configured.
"""

import json
import logging
import os

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
                model="claude-sonnet-4-5-20250514",
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

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
