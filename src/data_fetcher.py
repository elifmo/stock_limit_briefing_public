import logging

import pandas as pd
import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_REQUIRED_COLUMNS = {"Open", "High", "Low", "Close", "Volume"}


def _fetch_ohlcv(tickers: list[str], period: str, interval: str) -> dict[str, pd.DataFrame]:
    result: dict[str, pd.DataFrame] = {}
    for ticker in tickers:
        try:
            df = yf.Ticker(ticker).history(period=period, interval=interval)
            if df.empty or not _REQUIRED_COLUMNS.issubset(df.columns):
                logger.warning("No valid data for %s (%s) — skipping", ticker, interval)
                continue
            result[ticker] = df[list(_REQUIRED_COLUMNS)]
            logger.info("Fetched %d rows for %s (%s)", len(df), ticker, interval)
        except Exception as exc:
            logger.error("Failed to fetch %s (%s): %s", ticker, interval, exc)
    return result


def fetch_ticker_data(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Fetch 1-year daily OHLCV data for each ticker via yfinance."""
    return _fetch_ohlcv(tickers, period="1y", interval="1d")


def fetch_hourly_data(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Fetch 7-day hourly OHLCV data for each ticker via yfinance."""
    return _fetch_ohlcv(tickers, period="7d", interval="1h")


def fetch_4h_data(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """Fetch 60-day 4-hour OHLCV data for each ticker via yfinance."""
    return _fetch_ohlcv(tickers, period="60d", interval="4h")


def fetch_live_prices(tickers: list[str]) -> dict[str, float | None]:
    """Fetch current live price for each ticker via yfinance fast_info."""
    result: dict[str, float | None] = {}
    for ticker in tickers:
        try:
            result[ticker] = float(yf.Ticker(ticker).fast_info.last_price)
        except Exception as exc:
            logger.warning("Live price unavailable for %s: %s", ticker, exc)
            result[ticker] = None
    return result


def get_latest_price(df: pd.DataFrame) -> float:
    """Return the most recent closing price."""
    return float(df["Close"].iloc[-1])


def get_latest_volume(df: pd.DataFrame) -> float:
    """Return the most recent day's volume."""
    return float(df["Volume"].iloc[-1])


if __name__ == "__main__":
    test_tickers = ["NVDA", "AAPL", "BTC-USD"]
    data = fetch_ticker_data(test_tickers)
    for ticker, df in data.items():
        price = get_latest_price(df)
        logger.info("%s: %d rows, last price: $%.2f", ticker, len(df), price)
