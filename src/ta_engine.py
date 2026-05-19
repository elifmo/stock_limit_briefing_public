import logging
from typing import Literal, TypedDict

import pandas as pd

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

Timeframe = Literal["daily", "1h"]

MIN_ROWS_DAILY = 60
MIN_ROWS_HOURLY = 40  # US stocks: 7d × ~6.5h/day ≈ 46 bars; crypto: 7d × 24h = 168
VOLUME_HIGH_RATIO = 1.5
VOLUME_LOW_RATIO = 0.7
SR_LOOKBACK_DAILY = 20
SR_LOOKBACK_HOURLY = 20
BB_STD_DEVS = 2.0
BB_PERIOD = 20
RSI_PERIOD = 14
ATR_PERIOD = 14

# ── Type definitions ──────────────────────────────────────────────────────────


class EMAValues(TypedDict):
    ema5: float
    ema20: float
    ema50: float
    ema200: float | None  # None on 1H (only 7 days of data available)


class RSIValues(TypedDict):
    value: float
    prev_value: float


class ATRValues(TypedDict):
    value: float
    prev_value: float


class BollingerValues(TypedDict):
    upper: float
    middle: float
    lower: float
    bandwidth: float
    prev_bandwidth: float


class SRLevels(TypedDict):
    nearest_support: float
    nearest_resistance: float
    price_vs_support_pct: float
    price_vs_resistance_pct: float


class VolumeAnalysis(TypedDict):
    latest_volume: float
    avg_volume_20: float
    volume_ratio: float
    up_day: bool


class BreakoutDetection(TypedDict):
    is_breakout: bool
    breakout_type: Literal["above_resistance", "below_support", "none"]
    volume_confirmed: bool


class IndicatorValues(TypedDict):
    ema: EMAValues
    rsi: RSIValues | None       # None on 1H
    atr: ATRValues | None       # None on 1H
    bollinger: BollingerValues
    support_resistance: SRLevels
    volume: VolumeAnalysis
    breakout: BreakoutDetection
    latest_close: float


class TickerAnalysis(TypedDict):
    ticker: str
    timeframe: Timeframe
    indicators: IndicatorValues | None
    signals: list[str]
    is_breakout: bool
    error: str | None


class TAEngineResult(TypedDict):
    daily: dict[str, TickerAnalysis]
    hourly: dict[str, TickerAnalysis]


# ── Indicator calculators ─────────────────────────────────────────────────────


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def calculate_rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    alpha = 1.0 / period
    avg_gain = gain.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    # Guard against division by zero: no losing bars → RSI = 100
    rs = avg_gain / avg_loss.replace(0, float("inf"))
    return 100 - (100 / (1 + rs))


def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    prev_close = df["Close"].shift(1)
    tr = pd.DataFrame({
        "hl":  df["High"] - df["Low"],
        "hpc": (df["High"] - prev_close).abs(),
        "lpc": (df["Low"] - prev_close).abs(),
    }).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def calculate_bollinger(
    series: pd.Series,
    period: int = BB_PERIOD,
    std_dev: float = BB_STD_DEVS,
) -> pd.DataFrame:
    middle = series.rolling(period).mean()
    std = series.rolling(period).std(ddof=0)   # population std — matches TradingView
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    bandwidth = (upper - lower) / middle
    return pd.DataFrame({
        "upper": upper,
        "middle": middle,
        "lower": lower,
        "bandwidth": bandwidth,
    })


def calculate_support_resistance(
    df: pd.DataFrame,
    lookback: int,
) -> tuple[float, float]:
    window = df.iloc[-lookback - 1:-1]   # exclude the current (latest) bar
    support = float(window["Low"].min())
    resistance = float(window["High"].max())
    return support, resistance


def calculate_volume_analysis(
    df: pd.DataFrame,
    avg_period: int = 20,
) -> VolumeAnalysis:
    latest_vol = float(df["Volume"].iloc[-1])
    avg_vol = float(df["Volume"].rolling(avg_period).mean().iloc[-1])
    ratio = latest_vol / avg_vol if avg_vol > 0 else 1.0
    up_day = float(df["Close"].iloc[-1]) > float(df["Open"].iloc[-1])
    return VolumeAnalysis(
        latest_volume=latest_vol,
        avg_volume_20=avg_vol,
        volume_ratio=ratio,
        up_day=up_day,
    )


def detect_breakout(
    df: pd.DataFrame,
    resistance: float,
    support: float,
    volume_ratio: float,
    volume_threshold: float = VOLUME_HIGH_RATIO,
) -> BreakoutDetection:
    close = float(df["Close"].iloc[-1])
    if close > resistance:
        breakout_type: Literal["above_resistance", "below_support", "none"] = "above_resistance"
        is_breakout = True
    elif close < support:
        breakout_type = "below_support"
        is_breakout = True
    else:
        breakout_type = "none"
        is_breakout = False
    return BreakoutDetection(
        is_breakout=is_breakout,
        breakout_type=breakout_type,
        volume_confirmed=is_breakout and volume_ratio >= volume_threshold,
    )


# ── Signal evaluators ─────────────────────────────────────────────────────────


def evaluate_ema_signals(
    ema: EMAValues,
    latest_close: float,
    timeframe: Timeframe,
) -> list[str]:
    signals: list[str] = []

    if ema["ema5"] > ema["ema20"]:
        signals.append("✅ EMA5 > EMA20 = Momentum building")
    else:
        signals.append("❌ EMA5 < EMA20 = Momentum fading")

    if ema["ema20"] > ema["ema50"]:
        signals.append("✅ EMA20 > EMA50 = Uptrend starting")
    else:
        signals.append("❌ EMA20 < EMA50 = Downtrend forming")

    if timeframe == "daily" and ema["ema200"] is not None:
        if ema["ema50"] > ema["ema200"]:
            signals.append("✅ EMA50 > EMA200 = Strong uptrend")
        else:
            signals.append("❌ EMA50 < EMA200 = Strong downtrend")

    if latest_close > ema["ema20"]:
        signals.append("✅ Price above EMA20 = Buyers in control")
    else:
        signals.append("❌ Price below EMA20 = Sellers in control")

    return signals


def evaluate_rsi_signals(rsi: RSIValues) -> list[str]:
    signals: list[str] = []
    v = rsi["value"]
    pv = rsi["prev_value"]
    rv = round(v)

    if v > 80:
        signals.append(f"❌ RSI {rv} = Overheated, pullback risk")
    elif v < 20:
        signals.append(f"❌ RSI {rv} = Oversold, panic selling")
    elif pv >= 70 and v < 70:
        signals.append(f"❌ RSI {rv} = Momentum fading")
    elif pv <= 35 and v > pv:
        signals.append(f"✅ RSI {rv} = Bounce starting")
    elif 50 <= v <= 70:
        signals.append(f"✅ RSI {rv} = Strong uptrend (room to go)")
    elif 40 <= v < 50:
        signals.append(f"✅ RSI {rv} = Healthy momentum")

    return signals


def evaluate_volume_signals(volume: VolumeAnalysis) -> list[str]:
    signals: list[str] = []
    ratio = volume["volume_ratio"]
    up = volume["up_day"]

    if ratio >= VOLUME_HIGH_RATIO and up:
        signals.append("✅ Volume high on up day = Real buying")
    elif ratio >= VOLUME_HIGH_RATIO and not up:
        signals.append("❌ Volume high on down day = Real selling pressure")
    elif ratio <= VOLUME_LOW_RATIO and up:
        signals.append("❌ Volume low on up day = Weak buying")
    elif ratio <= VOLUME_LOW_RATIO:
        signals.append("❌ Volume dropping = Trend losing power")

    return signals


def evaluate_bollinger_signals(
    bb: BollingerValues,
    latest_close: float,
) -> list[str]:
    signals: list[str] = []

    if bb["bandwidth"] > bb["prev_bandwidth"]:
        signals.append("✅ BB widening = Trend accelerating")
    else:
        signals.append("❌ BB squeezing = Low volatility (flat market)")

    if latest_close >= bb["upper"]:
        signals.append("✅ Price at upper BB = Strong momentum")
    elif latest_close <= bb["lower"]:
        signals.append("❌ Price at lower BB = Weak, oversold")

    return signals


def evaluate_atr_signals(atr: ATRValues) -> list[str]:
    if atr["value"] > atr["prev_value"]:
        return ["✅ ATR rising = Power increasing"]
    return ["❌ ATR falling = Energy declining"]


def evaluate_sr_signals(
    sr: SRLevels,
    breakout: BreakoutDetection,
) -> list[str]:
    signals: list[str] = []

    if breakout["is_breakout"] and breakout["breakout_type"] == "above_resistance":
        if breakout["volume_confirmed"]:
            signals.append("✅ Price breaks resistance with volume = Real breakout")
        else:
            signals.append("⚠️ Price above resistance but low volume = Fake breakout risk")
    elif breakout["is_breakout"] and breakout["breakout_type"] == "below_support":
        signals.append("❌ Price breaking below support = Downtrend risk")
    elif sr["price_vs_support_pct"] <= 1.0:
        signals.append("✅ Price bouncing off support = Buyers defending")
    elif sr["price_vs_resistance_pct"] >= -2.0 and sr["price_vs_resistance_pct"] < 0:
        signals.append("⚠️ Price approaching resistance = Watch for rejection")

    return signals


# ── Per-ticker orchestrator ───────────────────────────────────────────────────


def analyze_ticker(
    ticker: str,
    df: pd.DataFrame,
    timeframe: Timeframe,
) -> TickerAnalysis:
    min_rows = MIN_ROWS_DAILY if timeframe == "daily" else MIN_ROWS_HOURLY
    if len(df) < min_rows:
        return TickerAnalysis(
            ticker=ticker,
            timeframe=timeframe,
            indicators=None,
            signals=[],
            is_breakout=False,
            error=f"Insufficient data: {len(df)} rows (need {min_rows})",
        )

    signals: list[str] = []
    latest_close = float(df["Close"].iloc[-1])
    sr_lookback = SR_LOOKBACK_DAILY if timeframe == "daily" else SR_LOOKBACK_HOURLY

    # EMA
    ema_values: EMAValues | None = None
    try:
        e5 = calculate_ema(df["Close"], 5)
        e20 = calculate_ema(df["Close"], 20)
        e50 = calculate_ema(df["Close"], 50)
        ema200_val: float | None = None
        if timeframe == "daily":
            e200 = calculate_ema(df["Close"], 200)
            last200 = e200.dropna()
            if not last200.empty:
                ema200_val = float(last200.iloc[-1])
        ema_values = EMAValues(
            ema5=float(e5.iloc[-1]),
            ema20=float(e20.iloc[-1]),
            ema50=float(e50.iloc[-1]),
            ema200=ema200_val,
        )
        signals.extend(evaluate_ema_signals(ema_values, latest_close, timeframe))
    except Exception as exc:
        logger.warning("EMA calculation failed for %s: %s", ticker, exc)

    # RSI (daily only)
    rsi_values: RSIValues | None = None
    if timeframe == "daily":
        try:
            rsi_series = calculate_rsi(df["Close"])
            rsi_values = RSIValues(
                value=float(rsi_series.iloc[-1]),
                prev_value=float(rsi_series.iloc[-2]),
            )
            signals.extend(evaluate_rsi_signals(rsi_values))
        except Exception as exc:
            logger.warning("RSI calculation failed for %s: %s", ticker, exc)

    # ATR (daily only)
    atr_values: ATRValues | None = None
    if timeframe == "daily":
        try:
            atr_series = calculate_atr(df)
            atr_values = ATRValues(
                value=float(atr_series.iloc[-1]),
                prev_value=float(atr_series.iloc[-2]),
            )
            signals.extend(evaluate_atr_signals(atr_values))
        except Exception as exc:
            logger.warning("ATR calculation failed for %s: %s", ticker, exc)

    # Bollinger Bands
    bb_values: BollingerValues | None = None
    try:
        bb_df = calculate_bollinger(df["Close"])
        bb_values = BollingerValues(
            upper=float(bb_df["upper"].iloc[-1]),
            middle=float(bb_df["middle"].iloc[-1]),
            lower=float(bb_df["lower"].iloc[-1]),
            bandwidth=float(bb_df["bandwidth"].iloc[-1]),
            prev_bandwidth=float(bb_df["bandwidth"].iloc[-2]),
        )
        signals.extend(evaluate_bollinger_signals(bb_values, latest_close))
    except Exception as exc:
        logger.warning("Bollinger calculation failed for %s: %s", ticker, exc)

    # Volume
    vol_values: VolumeAnalysis | None = None
    try:
        vol_values = calculate_volume_analysis(df)
        signals.extend(evaluate_volume_signals(vol_values))
    except Exception as exc:
        logger.warning("Volume calculation failed for %s: %s", ticker, exc)

    # Support / Resistance + Breakout
    sr_values: SRLevels | None = None
    breakout_values: BreakoutDetection | None = None
    try:
        support, resistance = calculate_support_resistance(df, sr_lookback)
        price_vs_support_pct = (latest_close - support) / support * 100 if support > 0 else 0.0
        price_vs_resistance_pct = (latest_close - resistance) / resistance * 100 if resistance > 0 else 0.0
        sr_values = SRLevels(
            nearest_support=support,
            nearest_resistance=resistance,
            price_vs_support_pct=price_vs_support_pct,
            price_vs_resistance_pct=price_vs_resistance_pct,
        )
        vol_ratio = vol_values["volume_ratio"] if vol_values else 1.0
        breakout_values = detect_breakout(df, resistance, support, vol_ratio)
        signals.extend(evaluate_sr_signals(sr_values, breakout_values))
    except Exception as exc:
        logger.warning("S/R calculation failed for %s: %s", ticker, exc)

    # Fallback defaults for failed indicators
    if ema_values is None:
        ema_values = EMAValues(ema5=0.0, ema20=0.0, ema50=0.0, ema200=None)
    if bb_values is None:
        bb_values = BollingerValues(upper=0.0, middle=0.0, lower=0.0, bandwidth=0.0, prev_bandwidth=0.0)
    if vol_values is None:
        vol_values = VolumeAnalysis(latest_volume=0.0, avg_volume_20=0.0, volume_ratio=1.0, up_day=False)
    if sr_values is None:
        sr_values = SRLevels(nearest_support=0.0, nearest_resistance=0.0, price_vs_support_pct=0.0, price_vs_resistance_pct=0.0)
    if breakout_values is None:
        breakout_values = BreakoutDetection(is_breakout=False, breakout_type="none", volume_confirmed=False)

    indicators = IndicatorValues(
        ema=ema_values,
        rsi=rsi_values,
        atr=atr_values,
        bollinger=bb_values,
        support_resistance=sr_values,
        volume=vol_values,
        breakout=breakout_values,
        latest_close=latest_close,
    )

    return TickerAnalysis(
        ticker=ticker,
        timeframe=timeframe,
        indicators=indicators,
        signals=signals,
        is_breakout=breakout_values["is_breakout"],
        error=None,
    )


# ── Main entry point ──────────────────────────────────────────────────────────


def run_ta_engine(
    daily_data: dict[str, pd.DataFrame],
    hourly_data: dict[str, pd.DataFrame],
) -> TAEngineResult:
    """Run full TA pipeline for all tickers on both timeframes.

    hourly_data may be an empty dict for daily-only runs.
    Each ticker failure is logged and excluded; remaining tickers proceed.
    """
    result: TAEngineResult = {"daily": {}, "hourly": {}}

    for ticker, df in daily_data.items():
        try:
            result["daily"][ticker] = analyze_ticker(ticker, df, "daily")
        except Exception as exc:
            logger.error("TA analysis failed for %s (daily): %s", ticker, exc)

    for ticker, df in hourly_data.items():
        try:
            result["hourly"][ticker] = analyze_ticker(ticker, df, "1h")
        except Exception as exc:
            logger.error("TA analysis failed for %s (1h): %s", ticker, exc)

    return result


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    sys.path.insert(0, "/Users/Elifcik/Desktop/apps/Stock-limit-briefing/src")
    from data_fetcher import fetch_hourly_data, fetch_ticker_data

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    test_tickers = ["NVDA", "AAPL", "BTC-USD"]

    logger.info("Fetching daily data...")
    daily_data = fetch_ticker_data(test_tickers)

    logger.info("Fetching hourly data...")
    hourly_data = fetch_hourly_data(test_tickers)

    logger.info("Running TA engine...")
    result = run_ta_engine(daily_data, hourly_data)

    for ticker, analysis in result["daily"].items():
        logger.info("=== %s DAILY ===", ticker)
        if analysis["error"]:
            logger.warning("  Error: %s", analysis["error"])
            continue
        ind = analysis["indicators"]
        assert ind is not None
        logger.info("  Close: $%.2f", ind["latest_close"])
        if ind["rsi"]:
            logger.info("  RSI:   %.1f", ind["rsi"]["value"])
        if ind["atr"]:
            logger.info("  ATR:   %.2f", ind["atr"]["value"])
        logger.info("  Breakout: %s", analysis["is_breakout"])
        logger.info("  Signals (%d):", len(analysis["signals"]))
        for sig in analysis["signals"]:
            logger.info("    %s", sig)

    for ticker, analysis in result["hourly"].items():
        logger.info("=== %s 1H ===", ticker)
        if analysis["error"]:
            logger.warning("  Error: %s", analysis["error"])
            continue
        logger.info("  Signals (%d):", len(analysis["signals"]))
        for sig in analysis["signals"]:
            logger.info("    %s", sig)
