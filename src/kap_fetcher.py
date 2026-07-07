"""Fetch same-day KAP (Public Disclosure Platform) news for BIST tickers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

_KAP_API = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
_KAP_HEADERS = {
    "Referer": "https://www.kap.org.tr/tr/bildirim-sorgu",
    "User-Agent": "Stock-limit-briefing/1.0",
}
_MAX_PER_TICKER = 3
_TR_TZ = ZoneInfo("Europe/Istanbul")


@dataclass(frozen=True)
class KapNewsItem:
    time: str
    subject: str
    summary: str
    url: str


def _bist_symbol(ticker: str) -> str | None:
    if not ticker.endswith(".IS"):
        return None
    return ticker.removesuffix(".IS").upper()


def _today_tr() -> date:
    return datetime.now(_TR_TZ).date()


def _parse_time(publish_date: str) -> str:
    """07.07.2026 13:47:23 -> 13:47"""
    parts = publish_date.strip().split()
    if len(parts) < 2 or ":" not in parts[1]:
        return publish_date
    clock = parts[1].split(":")
    return f"{clock[0]}:{clock[1]}"


def _item_codes(item: dict) -> set[str]:
    codes: set[str] = set()
    if item.get("stockCodes"):
        codes.add(str(item["stockCodes"]).strip().upper())
    related = item.get("relatedStocks")
    if related:
        for part in str(related).replace(" ", "").split(","):
            if part:
                codes.add(part.upper())
    return codes


def _fetch_disclosures(target_date: date) -> list[dict]:
    iso = target_date.isoformat()
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                _KAP_API,
                json={
                    "fromDate": iso,
                    "toDate": iso,
                    "mkkMemberOidList": [],
                    "subjectList": [],
                },
                headers=_KAP_HEADERS,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list):
                logger.warning("Unexpected KAP response type: %s", type(data))
                return []
            return data
    except Exception as exc:
        logger.error("KAP fetch failed for %s: %s", iso, exc)
        return []


def _to_news_item(item: dict) -> KapNewsItem:
    index = item.get("disclosureIndex")
    url = f"https://www.kap.org.tr/tr/Bildirim/{index}" if index else "https://www.kap.org.tr"
    subject = (item.get("subject") or item.get("kapTitle") or "Bildirim").strip()
    summary = (item.get("summary") or "").strip()
    return KapNewsItem(
        time=_parse_time(str(item.get("publishDate", ""))),
        subject=subject,
        summary=summary,
        url=url,
    )


def fetch_kap_for_tickers(
    tickers: list[str],
    target_date: date | None = None,
) -> dict[str, list[KapNewsItem]]:
    """Return same-day KAP items keyed by full ticker (e.g. THYAO.IS)."""
    symbols: dict[str, str] = {}
    for ticker in tickers:
        sym = _bist_symbol(ticker)
        if sym:
            symbols[ticker] = sym

    if not symbols:
        return {}

    day = target_date or _today_tr()
    disclosures = _fetch_disclosures(day)
    if not disclosures:
        return {ticker: [] for ticker in symbols}

    by_symbol: dict[str, list[KapNewsItem]] = {sym: [] for sym in symbols.values()}
    for item in disclosures:
        codes = _item_codes(item)
        if not codes:
            continue
        news = _to_news_item(item)
        for sym in codes:
            if sym in by_symbol:
                by_symbol[sym].append(news)

    result: dict[str, list[KapNewsItem]] = {}
    for ticker, sym in symbols.items():
        items = by_symbol.get(sym, [])
        items.sort(key=lambda x: x.time, reverse=True)
        result[ticker] = items[:_MAX_PER_TICKER]
        if items:
            logger.info("KAP %s: %d item(s) on %s", sym, len(items), day.isoformat())

    return result
