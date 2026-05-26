import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))

from ai_synthesizer import run_synthesizer
from data_fetcher import fetch_4h_data, fetch_hourly_data, fetch_ticker_data
from mail_sender import send_briefing
from state_classifier import classify_all_tickers
from ta_engine import run_ta_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "tickers.yaml"

MailType = Literal["morning", "evening"]


def _load_tickers() -> list[str]:
    with _CONFIG_PATH.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    tickers = data.get("tickers", [])
    if not tickers:
        logger.error("No tickers found in %s", _CONFIG_PATH)
        sys.exit(1)
    return tickers


def _detect_mail_type() -> MailType:
    """Infer morning/evening from current Turkey time (UTC+3)."""
    hour = datetime.utcnow().hour + 3  # UTC → Turkey (approximate, no DST logic here)
    hour %= 24
    return "morning" if hour < 15 else "evening"


def run(mail_type: MailType) -> None:
    tickers = _load_tickers()
    logger.info("Tickers: %s", tickers)
    logger.info("Mail type: %s", mail_type)

    logger.info("Fetching data...")
    daily_data = fetch_ticker_data(tickers)
    hourly_data = fetch_hourly_data(tickers)
    four_hour_data = fetch_4h_data(tickers)

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
    report_path = run_synthesizer(ta_result, classifications, mail_type)

    logger.info("Sending email...")
    send_briefing(report_path)

    logger.info("Done. Report: %s", report_path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("morning", "evening"):
        mail_type: MailType = sys.argv[1]  # type: ignore[assignment]
    else:
        mail_type = _detect_mail_type()
        logger.info("No argument given — auto-detected: %s", mail_type)

    run(mail_type)
