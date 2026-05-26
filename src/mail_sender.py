import logging
import os
import smtplib
import ssl
import sys
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)

_SMTP_HOST = "smtp.gmail.com"
_SMTP_PORT = 465


# ── Helpers ───────────────────────────────────────────────────────────────────


def _extract_subject(body: str) -> str:
    first_line = body.splitlines()[0] if body else ""
    if first_line.startswith("Subject:"):
        return first_line[len("Subject:"):].strip()
    return "Stock Limit Briefing"


def _body_without_subject(body: str) -> str:
    lines = body.splitlines()
    if lines and lines[0].startswith("Subject:"):
        return "\n".join(lines[1:]).lstrip("\n")
    return body


# ── Sender ────────────────────────────────────────────────────────────────────


def send_briefing(report_path: Path) -> None:
    gmail_user = os.environ.get("GMAIL_USER")
    app_password = os.environ.get("GMAIL_APP_PASSWORD")
    to_email = os.environ.get("TO_EMAIL")

    missing = [k for k, v in {
        "GMAIL_USER": gmail_user,
        "GMAIL_APP_PASSWORD": app_password,
        "TO_EMAIL": to_email,
    }.items() if not v]

    if missing:
        logger.error("Missing env vars: %s. Add them to your .env file.", ", ".join(missing))
        sys.exit(1)

    body = report_path.read_text(encoding="utf-8")
    subject = _extract_subject(body)
    content = _body_without_subject(body)

    recipients = [addr.strip() for addr in to_email.split(",") if addr.strip()]

    msg = MIMEText(content, "plain", "utf-8")
    msg["From"] = gmail_user
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, context=context) as server:
            server.login(gmail_user, app_password)
            server.sendmail(gmail_user, recipients, msg.as_string())
        logger.info("Briefing sent to %s | Subject: %s", ", ".join(recipients), subject)
    except smtplib.SMTPException as exc:
        logger.error("Failed to send email: %s", exc)
        raise


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")

    sys.path.insert(0, str(Path(__file__).parent))
    from data_fetcher import fetch_hourly_data, fetch_ticker_data
    from state_classifier import classify_all_tickers
    from ta_engine import run_ta_engine
    from ai_synthesizer import run_synthesizer

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

    logger.info("Sending email...")
    send_briefing(path)
