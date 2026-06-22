import logging
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_TURKEY = ZoneInfo("Europe/Istanbul")
_BIST_INDEX = "XU100.IS"

MailType = Literal["morning", "evening", "intraday"]


def now_turkey() -> datetime:
    return datetime.now(_TURKEY)


def today_turkey() -> date:
    return now_turkey().date()


def is_bist_trading_day(d: date) -> bool:
    """Return True when BIST traded on the given calendar date."""
    if d.weekday() >= 5:
        return False

    try:
        import yfinance as yf

        window_start = (d - timedelta(days=7)).isoformat()
        window_end = (d + timedelta(days=1)).isoformat()
        df = yf.Ticker(_BIST_INDEX).history(
            start=window_start,
            end=window_end,
            interval="1d",
        )
        if df.empty:
            return False
        return any(idx.date() == d for idx in df.index)
    except Exception as exc:
        logger.warning(
            "BIST calendar check failed for %s (%s) — falling back to weekday only",
            d.isoformat(),
            exc,
        )
        return d.weekday() < 5


def should_send_briefing(mail_type: MailType) -> tuple[bool, str]:
    """Gate briefing runs when BIST is closed (weekend or holiday)."""
    today = today_turkey()
    if not is_bist_trading_day(today):
        return False, (
            f"BIST market closed on {today.isoformat()} — "
            f"skipping {mail_type} briefing (no email sent)"
        )
    return True, ""
