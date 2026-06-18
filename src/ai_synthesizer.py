import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import anthropic

logger = logging.getLogger(__name__)

_REPORTS_DIR = Path(__file__).parent.parent / "reports"

_SYSTEM_PROMPT = """\
You are a personal investment briefing writer. Explain technical signals in plain
language that any investor can understand — beginner or experienced.

Given technical analysis signals for a stock or crypto asset, write exactly four sections:

INVESTOR VIEW:
[2-3 sentences. Describe the overall picture clearly. Mention the trend, momentum, and
any key concern (e.g. low volume, breakout not confirmed, overbought RSI). Use the
signal count if helpful. If SRP detected a stop hunt, explain it simply. Be specific.]

FOR NEW BUYERS:
[2-3 sentences. Clear, specific guidance with exact price levels. Mention POC as
pullback entry if PAP is ACCEPTANCE READY or ACCEPTED UP. If SRP has a bullish setup,
mention the target. If risky, say so plainly.]

FOR CURRENT HOLDERS:
[2 sentences. What to watch and when to act. Mention the key stop/support level.
If SRP has an invalidation level, use it. Be direct.]

WATCH CLOSELY:
[1-2 sentences. The single most critical price level or condition. What happens if
it breaks or holds.]

Rules:
- Plain English. Short sentences. Like explaining to a friend, not a financial analyst.
- Never promise returns. Never say "will" — say "may" or "could".
- Always reference exact prices from the context (POC, VAL, VAH, SRP target,
  SRP invalidation, support, resistance).
- If PAP is REJECTION REVIEW: note that price is outside accepted range — higher risk.
- Output ONLY the four sections. No preamble. No trailing text.\
"""

_FALLBACK_VIEW    = "Technical signals are mixed. No strong directional signal right now."
_FALLBACK_BUYERS  = "✅ Wait for clearer signals before entering."
_FALLBACK_HOLDERS = "✅ Monitor key support levels. No immediate action needed."
_FALLBACK_WATCH   = "Watch key support and resistance levels closely."

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
        if ind.get("pap"):
            pap = ind["pap"]
            lines.append(f"PAP Action: {pap['action']} (Score: {pap['acceptance_score']:.0f}/100)")
            lines.append(f"PAP POC: {pap['poc']:.2f}  VAL: {pap['val']:.2f}  VAH: {pap['vah']:.2f}")
            lines.append(f"PAP Rotation: {pap['rotation']}")

    if four_hour_ta and four_hour_ta.get("indicators"):
        ind4 = four_hour_ta["indicators"]
        if ind4.get("stop_run"):
            lines.append(_format_stop_run_line(ind4["stop_run"], "4H"))
        if ind4.get("srp"):
            srp = ind4["srp"]
            lines.append(f"SRP Action: {srp['action']} (Score: {srp['reversal_readiness_score']:.0f}/100)")
            if srp["detected"]:
                lines.append(f"SRP Direction: {srp['direction'].upper()}")
                lines.append(f"SRP Target: {srp['target']:.2f}  Invalidation: {srp['invalidation']:.2f}")

    return "\n".join(lines)


# ── Claude API call ───────────────────────────────────────────────────────────


def _call_claude(context: str, client: anthropic.Anthropic) -> tuple[str, str, str, str]:
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
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
        return _FALLBACK_VIEW, _FALLBACK_BUYERS, _FALLBACK_HOLDERS, _FALLBACK_WATCH


def _parse_sections(text: str) -> tuple[str, str, str, str]:
    bufs: dict[str, list[str]] = {
        "view": [], "buyers": [], "holders": [], "watch": []
    }
    markers = {
        "INVESTOR VIEW:":      "view",
        "FOR NEW BUYERS:":     "buyers",
        "FOR CURRENT HOLDERS:":"holders",
        "WATCH CLOSELY:":      "watch",
    }
    current = None

    for line in text.splitlines():
        matched = False
        for marker, key in markers.items():
            if line.startswith(marker):
                current = key
                remainder = line[len(marker):].strip()
                if remainder:
                    bufs[key].append(remainder)
                matched = True
                break
        if not matched and current:
            bufs[current].append(line)

    view    = "\n".join(bufs["view"]).strip()    or _FALLBACK_VIEW
    buyers  = "\n".join(bufs["buyers"]).strip()  or _FALLBACK_BUYERS
    holders = "\n".join(bufs["holders"]).strip() or _FALLBACK_HOLDERS
    watch   = "\n".join(bufs["watch"]).strip()   or _FALLBACK_WATCH
    return view, buyers, holders, watch


# ── Market detection & grouping ───────────────────────────────────────────────

_MARKET_ORDER = ["TR", "NL", "USA", "CRYPTO"]

_MARKET_HEADERS: dict[str, dict[str, str]] = {
    "TR":     {"morning": "🇹🇷 TURKEY (Live — Open +1h)",       "evening": "🇹🇷 TURKEY (Close)"},
    "NL":     {"morning": "🇳🇱 NETHERLANDS (Live — Open +1h)",  "evening": "🇳🇱 NETHERLANDS (Close)"},
    "USA":    {"morning": "🇺🇸 USA (Previous Close)",            "evening": "🇺🇸 USA (Live — Open +1h)"},
    "CRYPTO": {"morning": "🪙 CRYPTO (Live)",                    "evening": "🪙 CRYPTO (Live)"},
}

# Markets showing live prices at each mail time
_LIVE_MARKETS: dict[str, set[str]] = {
    "morning": {"TR", "NL", "CRYPTO"},
    "evening": {"USA", "CRYPTO"},
}


def _detect_market(ticker: str) -> str:
    if ticker.endswith(".IS"):
        return "TR"
    if ticker.endswith(".AS"):
        return "NL"
    if "-" in ticker:
        return "CRYPTO"
    return "USA"


def _currency(ticker: str) -> str:
    if ticker.endswith(".IS"):
        return "₺"
    if ticker.endswith(".AS"):
        return "€"
    return "$"


def _group_by_market(classifications: dict) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {m: [] for m in _MARKET_ORDER}
    for ticker in classifications:
        market = _detect_market(ticker)
        groups.setdefault(market, []).append(ticker)
    for market in groups:
        groups[market].sort()
    return groups


# ── SRP / PAP block formatters ────────────────────────────────────────────────


def _format_srp_block(srp: dict, cur: str) -> str:
    lines = ["🔵 SRP — STOP RUN PLANNER (4H):"]
    if not srp["detected"]:
        lines.append("  Stand Aside — No stop run setup on 4H")
        return "\n".join(lines)
    dir_icon = "🟢 BULLISH" if srp["direction"] == "bullish" else "🔴 BEARISH"
    action_desc = {
        "Wait Confirm":    "Wait Confirm — Setup detected, waiting for confirmation",
        "Plan Pullback":   "Plan Pullback — Reversal setup forming",
        "Review Reversal": "Review Reversal — High-quality setup active",
    }.get(srp["action"], srp["action"])
    side = "below support" if srp["direction"] == "bullish" else "above resistance"
    closed = "above" if srp["direction"] == "bullish" else "below"
    meaning = ("Stop hunt reversed. Pullback = entry zone."
               if srp["direction"] == "bullish"
               else "Failed breakout. Watch for continuation down.")
    lines.append(f"  Action:    {action_desc} (Score: {srp['reversal_readiness_score']:.0f}/100)")
    lines.append(f"  Direction: {dir_icon} — swept {cur}{srp['sweep_amount']:.2f} {side}, closed {closed}")
    lines.append(f"  Target:    {cur}{srp['target']:.2f}  |  Invalidation: {cur}{srp['invalidation']:.2f}")
    lines.append(f"  Meaning:   {meaning}")
    return "\n".join(lines)


def _format_pap_block(pap: dict, cur: str) -> str:
    lines = ["🟠 PAP — PRICE ACCEPTANCE PROFILE (Daily):"]
    action_desc = {
        "WAIT PROFILE":     "WAIT PROFILE — Not enough acceptance yet",
        "ACCEPTANCE READY": "ACCEPTANCE READY — Price is accepted in balance zone",
        "ACCEPTED UP":      "ACCEPTED UP — Balance migrating upward",
        "ACCEPTED DOWN":    "ACCEPTED DOWN — Balance migrating downward",
        "EDGE REVIEW":      "EDGE REVIEW — Price testing balance boundary",
        "REJECTION REVIEW": "REJECTION REVIEW — Price outside balance zone",
    }.get(pap["action"], pap["action"])
    rotation_text = {
        "up":     "↑ Upward — buyers gaining control",
        "down":   "↓ Downward — sellers gaining control",
        "stable": "→ Stable — no clear migration",
    }.get(pap["rotation"], "")
    lines.append(f"  Action:    {action_desc}")
    lines.append(f"  Score:     {pap['acceptance_score']:.0f}/100  |  POC: {cur}{pap['poc']:.2f}")
    if pap["action"] != "WAIT PROFILE":
        lines.append(f"  Balance:   {cur}{pap['val']:.2f} (VAL) → {cur}{pap['vah']:.2f} (VAH)")
        lines.append(f"  Rotation:  {rotation_text}")
    return "\n".join(lines)


# ── Email block builder ───────────────────────────────────────────────────────


def _two_column_signals(left_title: str, left_sigs: list[str],
                        right_title: str, right_sigs: list[str],
                        col_width: int = 38) -> list[str]:
    """Render two signal lists side-by-side with a pipe separator."""
    sep = "─" * col_width
    header = f"{left_title:<{col_width}} | {right_title}"
    divider = f"{sep} | {sep}"
    n = max(len(left_sigs), len(right_sigs))
    rows = [header, divider]
    for i in range(n):
        left  = left_sigs[i]  if i < len(left_sigs)  else ""
        right = right_sigs[i] if i < len(right_sigs) else ""
        rows.append(f"{left:<{col_width}} | {right}")
    return rows


def build_ticker_section(
    ticker: str,
    classification: dict,
    ta_result: dict,
    client: anthropic.Anthropic,
    mail_type: MailType,
    live_prices: dict[str, float | None],
) -> str:
    state = classification["state"]
    confidence = classification["confidence"]
    icon = _STATE_ICONS.get(state, "➖")

    context = _format_ticker_context(ticker, classification, ta_result)
    investor_view, for_buyers, for_holders, watch_closely = _call_claude(context, client)

    daily_ta = ta_result["daily"].get(ticker)
    hourly_ta = ta_result["hourly"].get(ticker)
    four_hour_ta = ta_result.get("four_hour", {}).get(ticker)

    market = _detect_market(ticker)
    cur = _currency(ticker)
    is_live = market in _LIVE_MARKETS[mail_type]
    live_price = live_prices.get(ticker) if is_live else None

    if live_price is not None:
        price_line = f"💰 Live: {cur}{live_price:.2f}"
    elif daily_ta and daily_ta.get("indicators"):
        price_line = f"💰 Close: {cur}{daily_ta['indicators']['latest_close']:.2f}"
    else:
        price_line = None

    lines: list[str] = [f"{ticker} — {icon} {state}  ({confidence} confidence)"]

    if price_line:
        lines.append(price_line)

    # KEY LEVELS right after price
    if daily_ta and daily_ta.get("indicators"):
        ind = daily_ta["indicators"]
        sr = ind["support_resistance"]
        lines.append(f"🔑 Support: {cur}{sr['nearest_support']:.2f}  |  Resistance: {cur}{sr['nearest_resistance']:.2f}")

    lines.append("")

    # SHORT-TERM and LONG-TERM side by side
    short_sigs = hourly_ta.get("signals", []) if hourly_ta else []
    long_sigs  = daily_ta.get("signals", [])  if daily_ta  else []
    if short_sigs or long_sigs:
        lines.extend(_two_column_signals(
            "📊 SHORT-TERM (1H)", short_sigs,
            "📈 LONG-TERM (Daily)", long_sigs,
        ))
        lines.append("")

    # AGPro Stop Run
    has_4h_sr    = four_hour_ta and four_hour_ta.get("indicators") and four_hour_ta["indicators"].get("stop_run")
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

    # SRP block
    if four_hour_ta and four_hour_ta.get("indicators") and four_hour_ta["indicators"].get("srp") is not None:
        lines.append(_format_srp_block(four_hour_ta["indicators"]["srp"], cur))
        lines.append("")

    # PAP block
    if daily_ta and daily_ta.get("indicators") and daily_ta["indicators"].get("pap") is not None:
        lines.append(_format_pap_block(daily_ta["indicators"]["pap"], cur))
        lines.append("")

    # Investor View
    lines.append("💭 INVESTOR VIEW")
    lines.append(investor_view)
    lines.append("")
    lines.append("🟡 For New Buyers")
    lines.append(for_buyers)
    lines.append("")
    lines.append("🟢 For Current Holders")
    lines.append(for_holders)
    lines.append("")
    lines.append("⚠️ Watch Closely")
    lines.append(watch_closely)

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
    is_summer = bool(datetime.now(ZoneInfo("Europe/Amsterdam")).dst())
    if mail_type == "morning":
        return f"Morning ({'11:00' if is_summer else '12:00'} Turkey)"
    return f"Evening ({'18:45' if is_summer else '19:45'} Turkey)"


def build_email_body(
    classifications: dict[str, dict],
    ta_result: dict,
    mail_type: MailType,
    client: anthropic.Anthropic,
    live_prices: dict[str, float | None],
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
                lines.append(build_ticker_section(ticker, classification, ta_result, client, mail_type, live_prices))
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
    live_prices: dict[str, float | None] | None = None,
) -> Path:
    try:
        client = anthropic.Anthropic()
    except Exception:
        import os
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            logger.error("ANTHROPIC_API_KEY not set. Add it to your .env file.")
            sys.exit(1)
        client = anthropic.Anthropic(api_key=key)

    body = build_email_body(classifications, ta_result, mail_type, client, live_prices or {})
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
