"""Portfolio and market-session helpers for BIST and US briefings."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

Portfolio = Literal["bist", "usa"]
MailType = Literal["morning", "evening", "intraday"]

_TURKEY = ZoneInfo("Europe/Istanbul")
_US_EASTERN = ZoneInfo("America/New_York")

_BIST_INDEX = "XU100.IS"
_US_INDEX = "SPY"


def now_turkey() -> datetime:
    return datetime.now(_TURKEY)


def now_us_eastern() -> datetime:
    return datetime.now(_US_EASTERN)


def today_turkey() -> date:
    return now_turkey().date()


def today_us_eastern() -> date:
    return now_us_eastern().date()


def today_for_portfolio(portfolio: Portfolio) -> date:
    if portfolio == "usa":
        return today_us_eastern()
    return today_turkey()


def _is_trading_day(d: date, index_ticker: str) -> bool:
    if d.weekday() >= 5:
        return False

    try:
        import yfinance as yf

        window_start = (d - timedelta(days=7)).isoformat()
        window_end = (d + timedelta(days=1)).isoformat()
        df = yf.Ticker(index_ticker).history(
            start=window_start,
            end=window_end,
            interval="1d",
        )
        if df.empty:
            return False
        return any(idx.date() == d for idx in df.index)
    except Exception as exc:
        logger.warning(
            "Trading-day check failed for %s (%s) — falling back to weekday only",
            d.isoformat(),
            exc,
        )
        return d.weekday() < 5


def is_bist_trading_day(d: date) -> bool:
    return _is_trading_day(d, _BIST_INDEX)


def is_us_trading_day(d: date) -> bool:
    return _is_trading_day(d, _US_INDEX)


def is_trading_day(d: date, portfolio: Portfolio) -> bool:
    if portfolio == "usa":
        return is_us_trading_day(d)
    return is_bist_trading_day(d)


def should_send_briefing(mail_type: MailType, portfolio: Portfolio = "bist") -> tuple[bool, str]:
    """Skip briefing when the relevant market is closed (weekend or holiday)."""
    today = today_for_portfolio(portfolio)
    market_label = "US" if portfolio == "usa" else "BIST"

    if not is_trading_day(today, portfolio):
        return False, (
            f"{market_label} market closed on {today.isoformat()} — "
            f"skipping {mail_type} briefing (no email sent)"
        )
    return True, ""
