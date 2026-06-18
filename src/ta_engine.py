import logging
from typing import Literal, TypedDict

import pandas as pd

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

Timeframe = Literal["daily", "1h", "4h"]

MIN_ROWS_DAILY = 60
MIN_ROWS_HOURLY = 40  # US stocks: 7d × ~6.5h/day ≈ 46 bars; crypto: 7d × 24h = 168
MIN_ROWS_4H = 20      # US stocks: 60d × ~1.6 bars/day ≈ 96 bars; crypto: 60d × 6 = 360
VOLUME_HIGH_RATIO = 1.5
VOLUME_LOW_RATIO = 0.7
SR_LOOKBACK_DAILY = 20
SR_LOOKBACK_HOURLY = 20
BB_STD_DEVS = 2.0
BB_PERIOD = 20
RSI_PERIOD = 14
ATR_PERIOD = 14

# SRP constants
SRP_SR_LOOKBACK = 34
SRP_TARGET_ATR_MULT = 1.35
SRP_INVALIDATION_ATR_MULT = 0.18
SRP_READINESS_THRESHOLD_LOW = 66
SRP_READINESS_THRESHOLD_HIGH = 78

# PAP constants
PAP_LOOKBACK = 320
PAP_ROWS = 26
PAP_VALUE_AREA_PCT = 0.70
PAP_SCORE_THRESHOLD = 66
PAP_RECENT_BARS = 20
PAP_REVISIT_ATR_MULT = 0.5
PAP_EDGE_ATR_MULT = 0.5

# ── Type definitions ──────────────────────────────────────────────────────────


class EMAValues(TypedDict):
    ema5: float
    ema20: float
    ema50: float


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


class StopRunDetection(TypedDict):
    bullish_stop_run: bool   # Low < support, Close > support
    bearish_stop_run: bool   # High > resistance, Close < resistance
    sweep_low: float         # distance swept below support (0.0 if no bullish stop run)
    sweep_high: float        # distance swept above resistance (0.0 if no bearish stop run)


class SRPResult(TypedDict):
    detected: bool
    direction: Literal["bullish", "bearish", "none"]
    sweep_amount: float
    support: float
    resistance: float
    sweep_score: float
    confirmation_score: float
    volume_score: float
    reversal_readiness_score: float
    action: Literal["Stand Aside", "Wait Confirm", "Plan Pullback", "Review Reversal"]
    target: float
    invalidation: float


class PAPResult(TypedDict):
    poc: float
    vah: float
    val: float
    poc1: float
    poc2: float
    rotation: Literal["up", "down", "stable"]
    acceptance_score: float
    price_location: Literal["inside_balance", "edge_review", "above_balance", "below_balance"]
    action: Literal["WAIT PROFILE", "ACCEPTANCE READY", "ACCEPTED UP", "ACCEPTED DOWN", "EDGE REVIEW", "REJECTION REVIEW"]


class IndicatorValues(TypedDict):
    ema: EMAValues
    rsi: RSIValues | None       # None on 1H
    atr: ATRValues | None       # None on 1H
    bollinger: BollingerValues
    support_resistance: SRLevels
    volume: VolumeAnalysis
    breakout: BreakoutDetection
    stop_run: StopRunDetection
    latest_close: float
    srp: SRPResult | None       # 4H only
    pap: PAPResult | None       # daily only


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
    four_hour: dict[str, TickerAnalysis]


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


def detect_stop_run(
    df: pd.DataFrame,
    support: float,
    resistance: float,
) -> StopRunDetection:
    """Detect stop run reversals: price sweeps a level then closes back through it."""
    latest = df.iloc[-1]
    low = float(latest["Low"])
    high = float(latest["High"])
    close = float(latest["Close"])

    bullish = low < support and close > support   # swept lows, reversed up
    bearish = high > resistance and close < resistance  # swept highs, reversed down

    return StopRunDetection(
        bullish_stop_run=bullish,
        bearish_stop_run=bearish,
        sweep_low=support - low if bullish else 0.0,
        sweep_high=high - resistance if bearish else 0.0,
    )


# ── SRP & PAP calculators ─────────────────────────────────────────────────────


def _compute_poc(
    window: pd.DataFrame,
    min_price: float,
    max_price: float,
    n_rows: int,
) -> float:
    if min_price >= max_price or len(window) == 0:
        return (min_price + max_price) / 2
    row_size = (max_price - min_price) / n_rows
    rows = [min_price + i * row_size for i in range(n_rows)]
    touch_counts = [
        int(((window["Low"] <= level + row_size) & (window["High"] >= level)).sum())
        for level in rows
    ]
    return rows[touch_counts.index(max(touch_counts))]


def calculate_srp(
    df: pd.DataFrame,
    atr: float,
    ema5: float,
    ema20: float,
    volume_ratio: float,
) -> SRPResult:
    support, resistance = calculate_support_resistance(df, SRP_SR_LOOKBACK)
    latest = df.iloc[-1]
    low = float(latest["Low"])
    high = float(latest["High"])
    close = float(latest["Close"])
    open_ = float(latest["Open"])
    bar_range = high - low or atr  # guard against doji

    bullish = low < support and close > support
    bearish = high > resistance and close < resistance

    _no_signal = SRPResult(
        detected=False, direction="none", sweep_amount=0.0,
        support=support, resistance=resistance,
        sweep_score=0.0, confirmation_score=0.0, volume_score=0.0,
        reversal_readiness_score=0.0, action="Stand Aside",
        target=0.0, invalidation=0.0,
    )

    if not bullish and not bearish:
        return _no_signal

    direction: Literal["bullish", "bearish"] = "bullish" if bullish else "bearish"
    volume_score = min(100.0, volume_ratio * 100)

    if bullish:
        lower_wick = close - low
        wick_score = min(100.0, lower_wick / bar_range * 100)
        penetration_score = min(100.0, (support - low) / atr * 100)
        sweep_score = 0.56 * wick_score + 0.34 * penetration_score + 0.10 * volume_score

        dir_body = 100.0 if close > open_ else 0.0
        body_share = abs(close - open_) / bar_range * 100
        close_location = (close - low) / bar_range * 100
        close_following = min(100.0, (close - support) / atr * 100)

        target = min(resistance, close + SRP_TARGET_ATR_MULT * atr) if resistance > close else close + SRP_TARGET_ATR_MULT * atr
        invalidation = low - SRP_INVALIDATION_ATR_MULT * atr
        sweep_amount = support - low
    else:
        upper_wick = high - close
        wick_score = min(100.0, upper_wick / bar_range * 100)
        penetration_score = min(100.0, (high - resistance) / atr * 100)
        sweep_score = 0.56 * wick_score + 0.34 * penetration_score + 0.10 * volume_score

        dir_body = 100.0 if close < open_ else 0.0
        body_share = abs(close - open_) / bar_range * 100
        close_location = (high - close) / bar_range * 100
        close_following = min(100.0, (resistance - close) / atr * 100)

        target = max(support, close - SRP_TARGET_ATR_MULT * atr) if support < close else close - SRP_TARGET_ATR_MULT * atr
        invalidation = high + SRP_INVALIDATION_ATR_MULT * atr
        sweep_amount = high - resistance

    confirmation_score = (0.22 * dir_body + 0.38 * body_share
                          + 0.12 * close_location + 0.28 * close_following)

    if bullish:
        target_room = min(100.0, (target - close) / (SRP_TARGET_ATR_MULT * atr) * 100) if atr > 0 else 0.0
        trend_turn = 100.0 if ema5 > ema20 else 0.0
    else:
        target_room = min(100.0, (close - target) / (SRP_TARGET_ATR_MULT * atr) * 100) if atr > 0 else 0.0
        trend_turn = 100.0 if ema5 < ema20 else 0.0

    readiness = (0.22 * sweep_score + 0.44 * confirmation_score
                 + 0.12 * volume_score + 0.14 * target_room + 0.08 * trend_turn)

    if readiness < SRP_READINESS_THRESHOLD_LOW:
        action: Literal["Stand Aside", "Wait Confirm", "Plan Pullback", "Review Reversal"] = "Wait Confirm"
    elif readiness < SRP_READINESS_THRESHOLD_HIGH:
        action = "Plan Pullback"
    else:
        action = "Review Reversal"

    return SRPResult(
        detected=True,
        direction=direction,
        sweep_amount=sweep_amount,
        support=support,
        resistance=resistance,
        sweep_score=sweep_score,
        confirmation_score=confirmation_score,
        volume_score=volume_score,
        reversal_readiness_score=readiness,
        action=action,
        target=target,
        invalidation=invalidation,
    )


def calculate_pap(
    df: pd.DataFrame,
    atr: float,
) -> PAPResult:
    lookback = min(PAP_LOOKBACK, len(df))
    window = df.iloc[-lookback:]
    min_price = float(window["Low"].min())
    max_price = float(window["High"].max())
    close = float(df["Close"].iloc[-1])

    if min_price >= max_price:
        mid = (min_price + max_price) / 2
        return PAPResult(
            poc=mid, vah=max_price, val=min_price, poc1=mid, poc2=mid,
            rotation="stable", acceptance_score=0.0,
            price_location="inside_balance", action="WAIT PROFILE",
        )

    row_size = (max_price - min_price) / PAP_ROWS
    rows = [min_price + i * row_size for i in range(PAP_ROWS)]
    touch_counts = [
        int(((window["Low"] <= level + row_size) & (window["High"] >= level)).sum())
        for level in rows
    ]
    total_touches = sum(touch_counts)
    poc_idx = touch_counts.index(max(touch_counts))
    poc = rows[poc_idx]

    # Value area: symmetric expansion from POC until 70% covered
    vah_idx = poc_idx
    val_idx = poc_idx
    accumulated = touch_counts[poc_idx]
    target_touches = PAP_VALUE_AREA_PCT * total_touches

    while accumulated < target_touches:
        above = touch_counts[vah_idx + 1] if vah_idx + 1 < PAP_ROWS else 0
        below = touch_counts[val_idx - 1] if val_idx - 1 >= 0 else 0
        if above == 0 and below == 0:
            break
        if above >= below and vah_idx + 1 < PAP_ROWS:
            vah_idx += 1
            accumulated += above
        elif val_idx - 1 >= 0:
            val_idx -= 1
            accumulated += below
        else:
            vah_idx += 1
            accumulated += above

    vah = rows[vah_idx] + row_size
    val = rows[val_idx]

    # Rotation: compare first-half vs second-half POC
    half = lookback // 2
    poc1 = _compute_poc(df.iloc[-lookback:-half], min_price, max_price, PAP_ROWS)
    poc2 = _compute_poc(df.iloc[-half:], min_price, max_price, PAP_ROWS)
    if poc2 > poc1:
        rotation: Literal["up", "down", "stable"] = "up"
    elif poc2 < poc1:
        rotation = "down"
    else:
        rotation = "stable"

    # Acceptance score
    recent = df.iloc[-PAP_RECENT_BARS:]
    acceptance_strength = float(((recent["Close"] >= val) & (recent["Close"] <= vah)).sum()) / PAP_RECENT_BARS * 100
    revisit_mask = window["Close"].between(poc - PAP_REVISIT_ATR_MULT * atr, poc + PAP_REVISIT_ATR_MULT * atr)
    revisit_density = float(revisit_mask.sum()) / lookback * 100
    total_range = max_price - min_price
    zone_range = vah - val
    zone_separation = min(100.0, zone_range / total_range * 100) if total_range > 0 else 0.0
    width_health = min(100.0, zone_range / (2 * atr) * 100) if atr > 0 else 0.0
    acceptance_score = (0.50 * acceptance_strength + 0.20 * revisit_density
                        + 0.20 * zone_separation + 0.10 * width_health)

    # Price location
    at_edge = (abs(close - vah) < PAP_EDGE_ATR_MULT * atr or
               abs(close - val) < PAP_EDGE_ATR_MULT * atr)
    if val <= close <= vah:
        price_location: Literal["inside_balance", "edge_review", "above_balance", "below_balance"] = (
            "edge_review" if at_edge else "inside_balance"
        )
    elif close > vah:
        price_location = "above_balance"
    else:
        price_location = "below_balance"

    # Action state
    if acceptance_score < PAP_SCORE_THRESHOLD:
        action_pap: Literal["WAIT PROFILE", "ACCEPTANCE READY", "ACCEPTED UP", "ACCEPTED DOWN", "EDGE REVIEW", "REJECTION REVIEW"] = "WAIT PROFILE"
    elif price_location == "inside_balance" and rotation == "up":
        action_pap = "ACCEPTED UP"
    elif price_location == "inside_balance" and rotation == "down":
        action_pap = "ACCEPTED DOWN"
    elif price_location == "inside_balance":
        action_pap = "ACCEPTANCE READY"
    elif price_location == "edge_review":
        action_pap = "EDGE REVIEW"
    else:
        action_pap = "REJECTION REVIEW"

    return PAPResult(
        poc=poc, vah=vah, val=val, poc1=poc1, poc2=poc2,
        rotation=rotation, acceptance_score=acceptance_score,
        price_location=price_location, action=action_pap,
    )


# ── Signal evaluators ─────────────────────────────────────────────────────────


def evaluate_stop_run_signals(stop_run: StopRunDetection) -> list[str]:
    """Return a signal string only when a stop run is actually detected."""
    if stop_run["bullish_stop_run"]:
        return [
            f"✅ Bullish Stop Run: Swept {stop_run['sweep_low']:.2f} below support, "
            f"closed above → Reversal signal"
        ]
    if stop_run["bearish_stop_run"]:
        return [
            f"❌ Bearish Stop Run: Swept {stop_run['sweep_high']:.2f} above resistance, "
            f"closed below → Rejection signal"
        ]
    return []


def evaluate_ema_signals(
    ema: EMAValues,
    latest_close: float,
) -> list[str]:
    signals: list[str] = []

    if ema["ema5"] > ema["ema20"]:
        signals.append("✅ EMA5 > EMA20 = Momentum building")
    else:
        signals.append("❌ EMA5 < EMA20 = Momentum fading")

    if ema["ema20"] > ema["ema50"]:
        signals.append("✅ EMA20 > EMA50 = Strong uptrend")
    else:
        signals.append("❌ EMA20 < EMA50 = Strong downtrend")

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
    if timeframe == "daily":
        min_rows = MIN_ROWS_DAILY
    elif timeframe == "4h":
        min_rows = MIN_ROWS_4H
    else:
        min_rows = MIN_ROWS_HOURLY
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

    ema_values: EMAValues | None = None
    try:
        e5 = calculate_ema(df["Close"], 5)
        e20 = calculate_ema(df["Close"], 20)
        e50 = calculate_ema(df["Close"], 50)
        ema_values = EMAValues(
            ema5=float(e5.iloc[-1]),
            ema20=float(e20.iloc[-1]),
            ema50=float(e50.iloc[-1]),
        )
        signals.extend(evaluate_ema_signals(ema_values, latest_close))
    except Exception as exc:
        logger.warning("EMA calculation failed for %s: %s", ticker, exc)

    # RSI (daily and 4h)
    rsi_values: RSIValues | None = None
    if timeframe in ("daily", "4h"):
        try:
            rsi_series = calculate_rsi(df["Close"])
            rsi_values = RSIValues(
                value=float(rsi_series.iloc[-1]),
                prev_value=float(rsi_series.iloc[-2]),
            )
            signals.extend(evaluate_rsi_signals(rsi_values))
        except Exception as exc:
            logger.warning("RSI calculation failed for %s: %s", ticker, exc)

    # ATR (daily and 4h)
    atr_values: ATRValues | None = None
    if timeframe in ("daily", "4h"):
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

    # Support / Resistance + Breakout + Stop Run
    sr_values: SRLevels | None = None
    breakout_values: BreakoutDetection | None = None
    stop_run_values: StopRunDetection | None = None
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
        stop_run_values = detect_stop_run(df, support, resistance)
        signals.extend(evaluate_stop_run_signals(stop_run_values))
    except Exception as exc:
        logger.warning("S/R calculation failed for %s: %s", ticker, exc)

    # SRP (4H only)
    srp_values: SRPResult | None = None
    if timeframe == "4h" and atr_values is not None and ema_values is not None and vol_values is not None:
        try:
            srp_values = calculate_srp(
                df,
                atr=atr_values["value"],
                ema5=ema_values["ema5"],
                ema20=ema_values["ema20"],
                volume_ratio=vol_values["volume_ratio"],
            )
        except Exception as exc:
            logger.warning("SRP calculation failed for %s: %s", ticker, exc)

    # PAP (daily only)
    pap_values: PAPResult | None = None
    if timeframe == "daily" and atr_values is not None:
        try:
            pap_values = calculate_pap(df, atr=atr_values["value"])
        except Exception as exc:
            logger.warning("PAP calculation failed for %s: %s", ticker, exc)

    # Fallback defaults for failed indicators
    if ema_values is None:
        ema_values = EMAValues(ema5=0.0, ema20=0.0, ema50=0.0)
    if bb_values is None:
        bb_values = BollingerValues(upper=0.0, middle=0.0, lower=0.0, bandwidth=0.0, prev_bandwidth=0.0)
    if vol_values is None:
        vol_values = VolumeAnalysis(latest_volume=0.0, avg_volume_20=0.0, volume_ratio=1.0, up_day=False)
    if sr_values is None:
        sr_values = SRLevels(nearest_support=0.0, nearest_resistance=0.0, price_vs_support_pct=0.0, price_vs_resistance_pct=0.0)
    if breakout_values is None:
        breakout_values = BreakoutDetection(is_breakout=False, breakout_type="none", volume_confirmed=False)
    if stop_run_values is None:
        stop_run_values = StopRunDetection(bullish_stop_run=False, bearish_stop_run=False, sweep_low=0.0, sweep_high=0.0)

    indicators = IndicatorValues(
        ema=ema_values,
        rsi=rsi_values,
        atr=atr_values,
        bollinger=bb_values,
        support_resistance=sr_values,
        volume=vol_values,
        breakout=breakout_values,
        stop_run=stop_run_values,
        latest_close=latest_close,
        srp=srp_values,
        pap=pap_values,
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
    four_hour_data: dict[str, pd.DataFrame] | None = None,
) -> TAEngineResult:
    """Run full TA pipeline for all tickers on daily, 1H, and 4H timeframes.

    hourly_data and four_hour_data may be empty dicts or None for partial runs.
    Each ticker failure is logged and excluded; remaining tickers proceed.
    """
    result: TAEngineResult = {"daily": {}, "hourly": {}, "four_hour": {}}

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

    for ticker, df in (four_hour_data or {}).items():
        try:
            result["four_hour"][ticker] = analyze_ticker(ticker, df, "4h")
        except Exception as exc:
            logger.error("TA analysis failed for %s (4h): %s", ticker, exc)

    return result


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).parent))
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
