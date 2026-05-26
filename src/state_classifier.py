import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Market state constants ────────────────────────────────────────────────────

STRONG_UPTREND = "Strong Uptrend"
BULLISH_MOMENTUM = "Bullish Momentum"
EARLY_BREAKOUT = "Early Breakout"
PULLBACK_OPPORTUNITY = "Pullback Opportunity"
WEAKENING_TREND = "Weakening Trend"
BREAKDOWN_RISK = "Breakdown Risk"
NO_CLEAR_SIGNAL = "No Clear Signal"


# ── Core helpers ──────────────────────────────────────────────────────────────


def count_positive_signals(signals: list[str]) -> int:
    """Count ✅ signals in the signal list."""
    return sum(1 for s in signals if s.startswith("✅"))


def get_confidence_level(positive_count: int, total_signals: int) -> str:
    """Return High / Medium / Low based on positive signal ratio."""
    if total_signals == 0:
        return "Low"
    ratio = positive_count / total_signals
    if ratio > 0.70:
        return "High"
    if ratio >= 0.50:
        return "Medium"
    return "Low"


# ── State classification ──────────────────────────────────────────────────────


def classify_market_state(analysis: dict) -> str:
    """Classify one TickerAnalysis (single timeframe) into a market state.

    Priority order: Early Breakout → Breakdown Risk → Strong Uptrend →
    Pullback Opportunity → Bullish Momentum → Weakening Trend → No Clear Signal.
    """
    if analysis.get("error") or analysis.get("indicators") is None:
        return NO_CLEAR_SIGNAL

    indicators = analysis["indicators"]
    ema = indicators["ema"]
    rsi = indicators.get("rsi")
    breakout = indicators["breakout"]
    signals = analysis["signals"]

    positive = count_positive_signals(signals)
    total = len(signals)
    ratio = positive / total if total > 0 else 0.0

    ema5_up = ema["ema5"] > ema["ema20"]
    ema20_up = ema["ema20"] > ema["ema50"]

    rsi_value = rsi["value"] if rsi else None
    rsi_healthy = rsi_value is not None and 40 <= rsi_value <= 75
    rsi_overheated = rsi_value is not None and rsi_value > 80

    # 1. Early Breakout — price just crossed above resistance with majority positive signals
    if (breakout["is_breakout"]
            and breakout["breakout_type"] == "above_resistance"
            and ratio >= 0.5):
        return EARLY_BREAKOUT

    # 2. Breakdown Risk — price broke below support
    if breakout["is_breakout"] and breakout["breakout_type"] == "below_support":
        return BREAKDOWN_RISK

    # 3. Strong Uptrend — EMA5>EMA20>EMA50 aligned + healthy RSI + high confidence
    if ema5_up and ema20_up and rsi_healthy and ratio >= 0.60:
        return STRONG_UPTREND

    # 4. Pullback Opportunity — EMA20>EMA50 (trend intact) but EMA5<EMA20 (short-term pullback)
    long_term_up = ema20_up
    short_term_pullback = not ema5_up
    rsi_not_crashed = rsi_value is None or rsi_value > 30
    if long_term_up and short_term_pullback and rsi_not_crashed:
        return PULLBACK_OPPORTUNITY

    # 5. Bullish Momentum — partial uptrend, positive majority, not overheated
    if (ema5_up or ema20_up) and not rsi_overheated and ratio >= 0.5:
        return BULLISH_MOMENTUM

    # 6. Weakening Trend — momentum fading, negative majority
    if not ema5_up and ratio < 0.5:
        # Escalate to Breakdown Risk when both EMAs are bearish and signals very negative
        if not ema20_up and ratio < 0.30:
            return BREAKDOWN_RISK
        return WEAKENING_TREND

    return NO_CLEAR_SIGNAL


def _combine_states(daily_state: str, hourly_state: str | None) -> str:
    """Merge daily and hourly states into a single representative state."""
    if hourly_state is None:
        return daily_state

    # Confirmed breakout: hourly breakout on top of bullish daily
    if hourly_state == EARLY_BREAKOUT and daily_state in (STRONG_UPTREND, BULLISH_MOMENTUM):
        return EARLY_BREAKOUT

    # Strong daily uptrend + intraday weakness = pullback within uptrend (re-entry opportunity)
    if daily_state == STRONG_UPTREND and hourly_state in (WEAKENING_TREND, PULLBACK_OPPORTUNITY, BREAKDOWN_RISK):
        return PULLBACK_OPPORTUNITY

    # Merely bullish daily + hourly breakdown/weakness = caution, trend may be turning
    if daily_state == BULLISH_MOMENTUM and hourly_state in (WEAKENING_TREND, BREAKDOWN_RISK):
        return WEAKENING_TREND

    # Bullish daily + hourly pullback = pullback opportunity
    if daily_state == BULLISH_MOMENTUM and hourly_state == PULLBACK_OPPORTUNITY:
        return PULLBACK_OPPORTUNITY

    return daily_state


# ── Per-ticker classifier ─────────────────────────────────────────────────────


def classify_single_ticker(ticker: str, analysis: dict) -> dict:
    """Classify a single ticker.

    analysis = {"daily": TickerAnalysis, "hourly": TickerAnalysis | None, "four_hour": TickerAnalysis | None}
    """
    daily_ta = analysis.get("daily")
    hourly_ta = analysis.get("hourly")
    four_hour_ta = analysis.get("four_hour")

    daily_state = classify_market_state(daily_ta) if daily_ta else NO_CLEAR_SIGNAL
    hourly_state = classify_market_state(hourly_ta) if hourly_ta else None

    state = _combine_states(daily_state, hourly_state)

    daily_signals = daily_ta["signals"] if daily_ta else []
    hourly_signals = hourly_ta["signals"] if hourly_ta else []
    four_hour_signals = four_hour_ta["signals"] if four_hour_ta else []
    all_signals = daily_signals + hourly_signals + four_hour_signals

    positive = count_positive_signals(all_signals)
    total = len(all_signals)
    confidence = get_confidence_level(positive, total)

    result: dict = {
        "ticker": ticker,
        "state": state,
        "confidence": confidence,
        "positive_signals": positive,
        "total_signals": total,
        "daily_state": daily_state,
    }
    if hourly_state is not None:
        result["hourly_state"] = hourly_state
    if four_hour_ta is not None:
        result["four_hour_state"] = classify_market_state(four_hour_ta)

    return result


# ── Batch classifier ──────────────────────────────────────────────────────────


def classify_all_tickers(all_analysis: dict[str, dict]) -> dict[str, dict]:
    """Classify all tickers.

    all_analysis = {ticker: {"daily": TickerAnalysis, "hourly": TickerAnalysis | None}}
    """
    results: dict[str, dict] = {}

    for ticker, analysis in all_analysis.items():
        try:
            classification = classify_single_ticker(ticker, analysis)
            results[ticker] = classification
            logger.info(
                "%s: %s (Confidence: %s)",
                ticker,
                classification["state"],
                classification["confidence"],
            )
        except Exception as exc:
            logger.error("Classification failed for %s: %s", ticker, exc)

    return results


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent))
    from data_fetcher import fetch_hourly_data, fetch_ticker_data
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

    # Restructure TAEngineResult into per-ticker format
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

    logger.info("─" * 50)
    for ticker, c in classifications.items():
        hourly_part = f" | 1H: {c.get('hourly_state', '—')}" if "hourly_state" in c else ""
        logger.info(
            "%s: %s (%s confidence, %d/%d signals) | Daily: %s%s",
            ticker,
            c["state"],
            c["confidence"],
            c["positive_signals"],
            c["total_signals"],
            c["daily_state"],
            hourly_part,
        )
