import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))

from ai_synthesizer import run_synthesizer
from data_fetcher import fetch_4h_data, fetch_hourly_data, fetch_live_prices, fetch_ticker_data
from mail_sender import send_briefing
from market_calendar import Portfolio, should_send_briefing
from state_classifier import classify_all_tickers
from ta_engine import run_ta_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_CONFIG_PATHS: dict[Portfolio, Path] = {
    "bist": Path(__file__).parent.parent / "config" / "tickers.yaml",
    "usa": Path(__file__).parent.parent / "config" / "tickers_us.yaml",
}

MailType = Literal["morning", "evening", "intraday"]


def _load_tickers(portfolio: Portfolio) -> list[str]:
    config_path = _CONFIG_PATHS[portfolio]
    if not config_path.exists():
        logger.error("Ticker config not found: %s", config_path)
        sys.exit(1)
    with config_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    tickers = data.get("tickers", [])
    if not tickers:
        logger.error("No tickers found in %s", config_path)
        sys.exit(1)
    return tickers


def _detect_mail_type() -> MailType:
    """Infer morning/evening from current Turkey time (UTC+3)."""
    hour = datetime.utcnow().hour + 3
    hour %= 24
    return "morning" if hour < 15 else "evening"


def _parse_args(argv: list[str]) -> tuple[MailType, Portfolio]:
    portfolio: Portfolio = "bist"
    mail_type: MailType | None = None

    for arg in argv[1:]:
        if arg in ("morning", "evening", "intraday"):
            mail_type = arg  # type: ignore[assignment]
        elif arg in ("bist", "usa"):
            portfolio = arg  # type: ignore[assignment]
        elif arg == "--usa":
            portfolio = "usa"

    env_portfolio = os.environ.get("PORTFOLIO", "").lower()
    if env_portfolio in ("bist", "usa"):
        portfolio = env_portfolio  # type: ignore[assignment]

    if mail_type is None:
        mail_type = _detect_mail_type()
        logger.info("No mail type given — auto-detected: %s", mail_type)

    return mail_type, portfolio


def run(mail_type: MailType, portfolio: Portfolio = "bist") -> None:
    ok, skip_reason = should_send_briefing(mail_type, portfolio)
    if not ok:
        logger.warning(skip_reason)
        print(f"::notice title=Briefing skipped::{skip_reason}")
        return

    tickers = _load_tickers(portfolio)
    logger.info("Portfolio: %s", portfolio)
    logger.info("Tickers: %s", tickers)
    logger.info("Mail type: %s", mail_type)

    logger.info("Fetching data...")
    daily_data = fetch_ticker_data(tickers)
    hourly_data = fetch_hourly_data(tickers)
    four_hour_data = fetch_4h_data(tickers)
    live_prices = fetch_live_prices(tickers)
    logger.info("Live prices: %s", {k: f"{v:.2f}" if v else None for k, v in live_prices.items()})

    logger.info("Running TA engine...")
    ta_result = run_ta_engine(daily_data, hourly_data, four_hour_data)

    all_tickers = set(ta_result["daily"]) | set(ta_result["hourly"]) | set(ta_result["four_hour"])
    all_analysis = {
        ticker: {
            "daily": ta_result["daily"].get(ticker),
            "hourly": ta_result["hourly"].get(ticker),
            "four_hour": ta_result["four_hour"].get(ticker),
        }
        for ticker in all_tickers
    }

    logger.info("Classifying market states...")
    classifications = classify_all_tickers(all_analysis)

    logger.info("Synthesizing briefing...")
    report_path = run_synthesizer(ta_result, classifications, mail_type, live_prices, portfolio)

    logger.info("Sending email...")
    send_briefing(report_path, portfolio)

    logger.info("Done. Report: %s", report_path)


if __name__ == "__main__":
    mail_type, portfolio = _parse_args(sys.argv)
    run(mail_type, portfolio)
