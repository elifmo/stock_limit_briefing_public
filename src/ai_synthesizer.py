import logging
import sys
from datetime import date
from pathlib import Path
from typing import Literal

import anthropic

logger = logging.getLogger(__name__)

_REPORTS_DIR = Path(__file__).parent.parent / "reports"

_SYSTEM_PROMPT = """\
You are a concise financial signal interpreter for a personal investment briefing.

Given technical analysis signals for a stock or crypto asset, write exactly two sections:

WHAT THIS MEANS:
[2-3 short sentences. State what the signals show. No jargon. No guarantees.]

BEFORE YOU ACT:
[3-4 bullet points starting with ✅ or ❌. Key things to verify before taking action.]

Rules:
- Simple, clear English. Short sentences.
- Never promise future returns.
- Never say "buy" or "sell" directly — say "consider" or "watch".
- Use the exact signal values provided (RSI number, support price, etc).
- Output ONLY the two sections. No preamble. No trailing text.\
"""

_FALLBACK_WHAT = "Analysis based on technical signals. See signal details above."
_FALLBACK_ACT  = "✅ Review all signals carefully before taking any action."

_STATE_ICONS: dict[str, str] = {
    "Strong Uptrend":      "📈",
    "Bullish Momentum":    "📊",
    "Early Breakout":      "🚀",
    "Pullback Opportunity":"🔄",
    "Weakening Trend":     "📉",
    "Breakdown Risk":      "⚡",
    "No Clear Signal":     "➖",
}

MailType = Literal["morning", "evening"]


# ── Context formatter ─────────────────────────────────────────────────────────


def _format_stop_run_line(stop_run: dict, label: str) -> str:
    if stop_run["bullish_stop_run"]:
        return f"STOP RUN {label}: BULLISH — swept {stop_run['sweep_low']:.2f} below support, reversed up"
    if stop_run["bearish_stop_run"]:
        return f"STOP RUN {label}: BEARISH — swept {stop_run['sweep_high']:.2f} above resistance, reversed down"
    return f"STOP RUN {label}: none"


def _format_ticker_context(
    ticker: str,
    classification: dict,
    ta_result: dict,
) -> str:
    daily_ta = ta_result["daily"].get(ticker)
    hourly_ta = ta_result["hourly"].get(ticker)
    four_hour_ta = ta_result.get("four_hour", {}).get(ticker)

    state = classification["state"]
    confidence = classification["confidence"]
    positive = classification["positive_signals"]
    total = classification["total_signals"]

    lines: list[str] = [
        f"TICKER: {ticker}",
        f"STATE: {state}",
        f"CONFIDENCE: {confidence} ({positive}/{total} signals positive)",
        "",
    ]

    if daily_ta and daily_ta.get("signals"):
        lines.append("DAILY SIGNALS:")
        lines.extend(daily_ta["signals"])
        lines.append("")

    if four_hour_ta and four_hour_ta.get("signals"):
        lines.append("4H SIGNALS:")
        lines.extend(four_hour_ta["signals"])
        lines.append("")

    if hourly_ta and hourly_ta.get("signals"):
        lines.append("HOURLY SIGNALS:")
        lines.extend(hourly_ta["signals"])
        lines.append("")

    lines.append("KEY VALUES:")
    if daily_ta and daily_ta.get("indicators"):
        ind = daily_ta["indicators"]
        lines.append(f"Close: ${ind['latest_close']:.2f}")
        if ind.get("rsi"):
            lines.append(f"RSI: {ind['rsi']['value']:.1f}")
        if ind.get("atr"):
            lines.append(f"ATR: {ind['atr']['value']:.2f}")
        sr = ind["support_resistance"]
        lines.append(f"Support: ${sr['nearest_support']:.2f}")
        lines.append(f"Resistance: ${sr['nearest_resistance']:.2f}")
        vol = ind["volume"]
        lines.append(f"Volume ratio: {vol['volume_ratio']:.2f}x average")
        if ind.get("stop_run"):
            lines.append(_format_stop_run_line(ind["stop_run"], "Daily"))

    if four_hour_ta and four_hour_ta.get("indicators") and four_hour_ta["indicators"].get("stop_run"):
        lines.append(_format_stop_run_line(four_hour_ta["indicators"]["stop_run"], "4H"))

    return "\n".join(lines)


# ── Claude API call ───────────────────────────────────────────────────────────


def _call_claude(context: str, client: anthropic.Anthropic) -> tuple[str, str]:
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": context}],
        )
        text = response.content[0].text if response.content else ""
        return _parse_sections(text)
    except Exception as exc:
        logger.error("Claude API call failed: %s", exc)
        return _FALLBACK_WHAT, _FALLBACK_ACT


def _parse_sections(text: str) -> tuple[str, str]:
    what_buf: list[str] = []
    act_buf:  list[str] = []
    current = None

    for line in text.splitlines():
        if line.startswith("WHAT THIS MEANS:"):
            current = "what"
            remainder = line[len("WHAT THIS MEANS:"):].strip()
            if remainder:
                what_buf.append(remainder)
        elif line.startswith("BEFORE YOU ACT:"):
            current = "act"
            remainder = line[len("BEFORE YOU ACT:"):].strip()
            if remainder:
                act_buf.append(remainder)
        elif current == "what":
            what_buf.append(line)
        elif current == "act":
            act_buf.append(line)

    what = "\n".join(what_buf).strip() or _FALLBACK_WHAT
    act  = "\n".join(act_buf).strip()  or _FALLBACK_ACT
    return what, act


# ── Market detection & grouping ───────────────────────────────────────────────

_MARKET_ORDER = ["TR", "NL", "USA", "CRYPTO"]

_MARKET_HEADERS: dict[str, dict[str, str]] = {
    "TR":     {"morning": "🇹🇷 TÜRKİYE (Açılış + 1 saat)",       "evening": "🇹🇷 TÜRKİYE (Kapanış)"},
    "NL":     {"morning": "🇳🇱 HOLLANDA (Açılış + 1 saat)",       "evening": "🇳🇱 HOLLANDA (Kapanış)"},
    "USA":    {"morning": "🇺🇸 USA (Önceki günün kapanışı)",       "evening": "🇺🇸 USA (Açılış + 1 saat)"},
    "CRYPTO": {"morning": "🪙 KRİPTO (Günlük kapanış)",            "evening": "🪙 KRİPTO (Önceki günün kapanışı)"},
}


def _detect_market(ticker: str) -> str:
    if ticker.endswith(".IS"):
        return "TR"
    if ticker.endswith(".AS"):
        return "NL"
    if "-" in ticker:
        return "CRYPTO"
    return "USA"


def _group_by_market(classifications: dict) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {m: [] for m in _MARKET_ORDER}
    for ticker in classifications:
        market = _detect_market(ticker)
        groups.setdefault(market, []).append(ticker)
    return groups


# ── Email block builder ───────────────────────────────────────────────────────


def build_ticker_section(
    ticker: str,
    classification: dict,
    ta_result: dict,
    client: anthropic.Anthropic,
) -> str:
    state = classification["state"]
    confidence = classification["confidence"]
    icon = _STATE_ICONS.get(state, "➖")

    context = _format_ticker_context(ticker, classification, ta_result)
    what, act = _call_claude(context, client)

    daily_ta = ta_result["daily"].get(ticker)
    hourly_ta = ta_result["hourly"].get(ticker)
    four_hour_ta = ta_result.get("four_hour", {}).get(ticker)

    lines: list[str] = [
        f"{ticker} — {icon} {state}  ({confidence} confidence)",
        "",
    ]

    if hourly_ta and hourly_ta.get("signals"):
        lines.append("📊 SHORT-TERM (1H):")
        lines.extend(hourly_ta["signals"])
        lines.append("")

    if daily_ta and daily_ta.get("signals"):
        lines.append("📈 LONG-TERM (Daily):")
        lines.extend(daily_ta["signals"])
        lines.append("")

    # KEY LEVELS from daily indicators
    if daily_ta and daily_ta.get("indicators"):
        ind = daily_ta["indicators"]
        sr = ind["support_resistance"]
        lines.append("🔑 KEY LEVELS:")
        lines.append(f"  🟢 Support:    {sr['nearest_support']:.2f}")
        lines.append(f"  🔴 Resistance: {sr['nearest_resistance']:.2f}")
        lines.append("")

    # STOP RUN REVERSAL ANALYSIS (4H + Daily)
    has_4h_sr = four_hour_ta and four_hour_ta.get("indicators") and four_hour_ta["indicators"].get("stop_run")
    has_daily_sr = daily_ta and daily_ta.get("indicators") and daily_ta["indicators"].get("stop_run")
    if has_4h_sr or has_daily_sr:
        lines.append("🎯 STOP RUN REVERSAL (AGPro):")
        if has_4h_sr:
            sr4 = four_hour_ta["indicators"]["stop_run"]
            if sr4["bullish_stop_run"]:
                lines.append(f"  4H:    ✅ BULLISH — swept {sr4['sweep_low']:.2f} below support, closed above → Reversal signal")
            elif sr4["bearish_stop_run"]:
                lines.append(f"  4H:    ❌ BEARISH — swept {sr4['sweep_high']:.2f} above resistance, closed below → Rejection signal")
            else:
                lines.append("  4H:    ➖ No stop run detected")
        if has_daily_sr:
            srd = daily_ta["indicators"]["stop_run"]
            if srd["bullish_stop_run"]:
                lines.append(f"  Daily: ✅ BULLISH — swept {srd['sweep_low']:.2f} below support, closed above → Strong reversal")
            elif srd["bearish_stop_run"]:
                lines.append(f"  Daily: ❌ BEARISH — swept {srd['sweep_high']:.2f} above resistance, closed below → Strong rejection")
            else:
                lines.append("  Daily: ➖ No stop run detected")
        lines.append("")

    lines.append("💭 WHAT THIS MEANS:")
    lines.append(what)
    lines.append("")
    lines.append("📋 BEFORE YOU ACT:")
    lines.append(act)

    return "\n".join(lines)


# ── Email assembler ───────────────────────────────────────────────────────────

_SEP_THICK = "═" * 41
_SEP_THIN  = "─" * 41


def _mail_label(mail_type: MailType) -> str:
    return "Morning" if mail_type == "morning" else "Evening"


def _next_briefing(mail_type: MailType) -> str:
    if mail_type == "morning":
        return "Next briefing: Evening (Today)"
    return "Next briefing: Morning (Tomorrow)"


def _time_label(mail_type: MailType) -> str:
    if mail_type == "morning":
        return "Morning (12:00 Turkey)"
    return "Evening (19:35 Turkey)"


def build_email_body(
    classifications: dict[str, dict],
    ta_result: dict,
    mail_type: MailType,
    client: anthropic.Anthropic,
) -> str:
    today = date.today().strftime("%B %d, %Y")
    label = _mail_label(mail_type)

    lines: list[str] = [
        f"Subject: Stock Limit Briefing — {date.today().strftime('%B %d')}, {label}",
        "",
        _SEP_THICK,
        "📊 STOCK LIMIT BRIEFING",
        f"{today} — {_time_label(mail_type)}",
        _SEP_THICK,
    ]

    groups = _group_by_market(classifications)

    for market in _MARKET_ORDER:
        tickers = groups.get(market, [])
        if not tickers:
            continue

        lines.append("")
        lines.append(_MARKET_HEADERS[market][mail_type])
        lines.append(_SEP_THIN)

        for ticker in tickers:
            classification = classifications.get(ticker)
            if not classification:
                continue
            try:
                lines.append("")
                lines.append(build_ticker_section(ticker, classification, ta_result, client))
                lines.append("")
                lines.append(_SEP_THIN)
            except Exception as exc:
                logger.error("Failed to build section for %s: %s", ticker, exc)

        lines.append(_SEP_THICK)

    lines.append("")
    lines.append(_next_briefing(mail_type))

    return "\n".join(lines)


# ── File saver ────────────────────────────────────────────────────────────────


def save_briefing(body: str, mail_type: MailType) -> Path:
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{date.today().isoformat()}-{mail_type}.txt"
    path = _REPORTS_DIR / filename
    path.write_text(body, encoding="utf-8")
    return path


# ── Main entry point ──────────────────────────────────────────────────────────


def run_synthesizer(
    ta_result: dict,
    classifications: dict[str, dict],
    mail_type: MailType,
) -> Path:
    api_key_missing = False
    try:
        client = anthropic.Anthropic()
    except Exception:
        import os
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            logger.error("ANTHROPIC_API_KEY not set. Add it to your .env file.")
            sys.exit(1)
        client = anthropic.Anthropic(api_key=key)

    body = build_email_body(classifications, ta_result, mail_type, client)
    path = save_briefing(body, mail_type)
    logger.info("Briefing saved: %s", path)
    return path


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")

    sys.path.insert(0, str(Path(__file__).parent))
    from data_fetcher import fetch_hourly_data, fetch_ticker_data
    from state_classifier import classify_all_tickers
    from ta_engine import run_ta_engine

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    test_tickers = ["NVDA", "AAPL", "BTC-USD"]

    logger.info("Fetching data...")
    daily_data = fetch_ticker_data(test_tickers)
    hourly_data = fetch_hourly_data(test_tickers)

    logger.info("Running TA engine...")
    ta_result = run_ta_engine(daily_data, hourly_data)

    all_tickers = set(ta_result["daily"]) | set(ta_result["hourly"])
    all_analysis = {
        ticker: {
            "daily": ta_result["daily"].get(ticker),
            "hourly": ta_result["hourly"].get(ticker),
        }
        for ticker in all_tickers
    }

    logger.info("Classifying market states...")
    classifications = classify_all_tickers(all_analysis)

    logger.info("Synthesizing briefing...")
    path = run_synthesizer(ta_result, classifications, "morning")

    logger.info("─" * 50)
    logger.info("Briefing written to: %s", path)
    print(path.read_text(encoding="utf-8"))
