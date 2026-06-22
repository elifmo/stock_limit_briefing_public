import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from market_calendar import Portfolio, today_for_portfolio

logger = logging.getLogger(__name__)

_REPORTS_DIR = Path(__file__).parent.parent / "reports"
_LEGACY_PATH = _REPORTS_DIR / "last_snapshot.json"
_MAX_SNAPSHOTS = 50

MailType = Literal["morning", "evening", "intraday"]

_MAIL_LABELS: dict[str, str] = {
    "morning": "Morning",
    "intraday": "Intraday",
    "evening": "Evening",
}

_STATE_RANK: dict[str, int] = {
    "Strong Uptrend": 6,
    "Early Breakout": 5,
    "Bullish Momentum": 4,
    "Pullback Opportunity": 3,
    "No Clear Signal": 2,
    "Weakening Trend": 1,
    "Breakdown Risk": 0,
}


def _history_path(portfolio: Portfolio) -> Path:
    if portfolio == "usa":
        return _REPORTS_DIR / "snapshot_history_usa.json"
    return _REPORTS_DIR / "snapshot_history.json"


def _timezone_for_portfolio(portfolio: Portfolio) -> ZoneInfo:
    if portfolio == "usa":
        return ZoneInfo("America/New_York")
    return ZoneInfo("Europe/Istanbul")


def _snap_date(snapshot: dict) -> date:
    if trading_date := snapshot.get("trading_date"):
        return date.fromisoformat(trading_date)
    return datetime.fromisoformat(snapshot["saved_at"]).date()


def _load_history(portfolio: Portfolio) -> list[dict]:
    history_path = _history_path(portfolio)
    snapshots: list[dict] = []

    if history_path.exists():
        try:
            data = json.loads(history_path.read_text(encoding="utf-8"))
            snapshots = data.get("snapshots", [])
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load snapshot history: %s", exc)

    if not snapshots and portfolio == "bist" and _LEGACY_PATH.exists():
        try:
            snapshots = [json.loads(_LEGACY_PATH.read_text(encoding="utf-8"))]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load legacy snapshot: %s", exc)

    return sorted(snapshots, key=lambda s: s["saved_at"], reverse=True)


def _find_latest_evening_before(day: date, history: list[dict]) -> dict | None:
    for snapshot in history:
        if snapshot["mail_type"] == "evening" and _snap_date(snapshot) < day:
            return snapshot
    return None


def load_previous_snapshot(mail_type: MailType, portfolio: Portfolio = "bist") -> dict | None:
    history = _load_history(portfolio)
    if not history:
        return None

    today = today_for_portfolio(portfolio)

    if mail_type == "morning":
        return _find_latest_evening_before(today, history)

    if mail_type == "intraday":
        for snapshot in history:
            if snapshot["mail_type"] == "morning" and _snap_date(snapshot) == today:
                return snapshot
        return _find_latest_evening_before(today, history)

    if mail_type == "evening":
        for snapshot in history:
            if snapshot["mail_type"] == "intraday" and _snap_date(snapshot) == today:
                return snapshot
        for snapshot in history:
            if snapshot["mail_type"] == "morning" and _snap_date(snapshot) == today:
                return snapshot
        return _find_latest_evening_before(today, history)

    return None


def save_snapshot(
    classifications: dict[str, dict],
    mail_type: MailType,
    portfolio: Portfolio = "bist",
) -> None:
    tz = _timezone_for_portfolio(portfolio)
    snapshot = {
        "saved_at": datetime.now(tz).isoformat(),
        "trading_date": today_for_portfolio(portfolio).isoformat(),
        "mail_type": mail_type,
        "portfolio": portfolio,
        "tickers": {
            ticker: {
                "state": c["state"],
                "confidence": c["confidence"],
                "daily_state": c.get("daily_state"),
                "hourly_state": c.get("hourly_state"),
                "four_hour_state": c.get("four_hour_state"),
            }
            for ticker, c in classifications.items()
        },
    }

    history = _load_history(portfolio)
    history.insert(0, snapshot)
    history = history[:_MAX_SNAPSHOTS]

    history_path = _history_path(portfolio)
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps({"snapshots": history}, indent=2),
        encoding="utf-8",
    )
    logger.info("State snapshot saved (%s %s, %s)", portfolio, mail_type, history_path)


def _change_direction(previous_state: str, current_state: str) -> str:
    prev_rank = _STATE_RANK.get(previous_state, 2)
    curr_rank = _STATE_RANK.get(current_state, 2)
    if curr_rank > prev_rank:
        return "improved"
    if curr_rank < prev_rank:
        return "worsened"
    return "shifted"


def _direction_icon(direction: str) -> str:
    return {"improved": "🟢", "worsened": "⚠️", "shifted": "🔄"}[direction]


def _direction_suffix(direction: str) -> str:
    return {"improved": " — improved", "worsened": " — caution", "shifted": ""}[direction]


def _comparison_label(previous_snapshot: dict, portfolio: Portfolio) -> str:
    prev_mail = _MAIL_LABELS.get(previous_snapshot.get("mail_type", ""), "last briefing")
    snap_date = _snap_date(previous_snapshot)
    today = today_for_portfolio(portfolio)
    gap_days = (today - snap_date).days

    if gap_days > 1 or (gap_days == 1 and today.weekday() == 0):
        day_label = snap_date.strftime("%a %d %b")
        return f"last close ({prev_mail}, {day_label})"

    return prev_mail


def format_status_change(
    ticker: str,
    classification: dict,
    previous_snapshot: dict | None,
    portfolio: Portfolio = "bist",
) -> str:
    if previous_snapshot is None:
        return "📌 Status: First briefing — nothing to compare yet"

    previous = previous_snapshot.get("tickers", {}).get(ticker)
    if previous is None:
        return "📌 Status: New in watchlist — no previous briefing"

    compare_label = _comparison_label(previous_snapshot, portfolio)
    prev_state = previous["state"]
    curr_state = classification["state"]

    if prev_state == curr_state:
        prev_conf = previous.get("confidence", "")
        curr_conf = classification["confidence"]
        if prev_conf != curr_conf:
            return (
                f"📌 Since {compare_label}: Status unchanged ({curr_state}), "
                f"confidence {prev_conf} → {curr_conf}"
            )
        return f"📌 Since {compare_label}: No status change ({curr_state})"

    direction = _change_direction(prev_state, curr_state)
    icon = _direction_icon(direction)
    suffix = _direction_suffix(direction)
    return f"{icon} Since {compare_label}: {prev_state} → {curr_state}{suffix}"


def build_change_summary(
    classifications: dict[str, dict],
    previous_snapshot: dict | None,
    portfolio: Portfolio = "bist",
) -> list[str]:
    if previous_snapshot is None:
        return []

    compare_label = _comparison_label(previous_snapshot, portfolio)
    changed: list[tuple[str, str, str, str]] = []

    for ticker, current in classifications.items():
        previous = previous_snapshot.get("tickers", {}).get(ticker)
        if previous and previous["state"] != current["state"]:
            direction = _change_direction(previous["state"], current["state"])
            changed.append((ticker, previous["state"], current["state"], direction))

    if not changed:
        return [f"📋 Since {compare_label}: No ticker changed status."]

    lines = [f"📋 Status changes since {compare_label}:"]
    for ticker, old_state, new_state, direction in sorted(changed):
        icon = _direction_icon(direction)
        lines.append(f"  {icon} {ticker}: {old_state} → {new_state}")
    return lines
