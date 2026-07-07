import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import anthropic

from market_calendar import Portfolio
from kap_fetcher import KapNewsItem
from state_history import load_previous_snapshot, save_snapshot

logger = logging.getLogger(__name__)

_REPORTS_DIR = Path(__file__).parent.parent / "reports"

MailType = Literal["morning", "evening", "intraday"]
Lang = Literal["tr", "en"]


def _is_turkish(portfolio: Portfolio, lang: Lang = "tr") -> bool:
    return portfolio == "bist" and lang == "tr"


# BIST mails are English-only for now. Change to "tr" to re-enable Turkish.
_BIST_MAIL_LANG: Lang = "en"


def _mail_lang(portfolio: Portfolio) -> Lang:
    return _BIST_MAIL_LANG if portfolio == "bist" else "en"

# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT_USA = """\
Write 4 short lines for a stock email. Use very easy English (like texting a friend).

SUMMARY:
[2 short sentences max. Say if price is going up, down, or stuck. One risk.]

NEW BUYERS:
[Word 1: BUY or WAIT or DO NOT BUY.
Then 1 short sentence with a price. Example: "WAIT. Buy at $138 if it drops."]

HOLDERS:
[Word 1: HOLD or TRIM or SELL.
Then 1 short sentence with stop price. Max 12 words.]

WATCH:
[1 sentence. One price. Say what happens if it breaks that level.]

Rules:
- Use small words only (up, down, wait, buy, stop, support).
- No EMA, RSI, momentum, conviction, timeframe, reclaim.
- Use prices from the data.
- No promises of profit.
- Output ONLY the four sections.\
"""

_SYSTEM_PROMPT_BIST = """\
Kişisel portföy maili için aksiyon satırları yazıyorsun. Basit Türkçe.

Tam dört bölüm:

ÖZET:
[En fazla 2 kısa cümle. Sade Türkçe. EMA, timeframe, sinyal oranı deme.]

YENİ ALICI:
[Önce tek kelime: AL / BEKLE / ALMA.
Sonra 1 cümle — limit emir fiyatı veya neden bekle.]

ELINDE OLAN:
[Önce: TUT / AZALT / SAT.
Sonra 1 cümle — stop fiyatı. Toplam max 15 kelime.]

İZLE:
[1 cümle. Tek fiyat seviyesi. Kırılırsa / tutarsa ne olur.]

Kurallar:
- "conviction", "timeframe", "signal count" gibi İngilizce jargon kullanma.
- Verideki para birimini kullan (₺, €, $).
- "Kesin kazanç" deme.
- Sadece dört bölüm.\
"""

_SECTION_MARKERS: dict[Portfolio, dict[str, str]] = {
    "usa": {
        "SUMMARY:": "summary",
        "NEW BUYERS:": "buyers",
        "HOLDERS:": "holders",
        "WATCH:": "watch",
    },
    "bist": {
        "ÖZET:": "summary",
        "YENİ ALICI:": "buyers",
        "ELINDE OLAN:": "holders",
        "İZLE:": "watch",
    },
}

_FALLBACKS: dict[Portfolio, dict[str, str]] = {
    "usa": {
        "summary": "Price is mixed. No clear move yet.",
        "buyers": "WAIT. No good buy signal yet.",
        "holders": "HOLD. Watch support. Nothing urgent.",
        "watch": "Watch support and resistance.",
    },
    "bist": {
        "summary": "Sinyaller karışık. Henüz net bir yön yok.",
        "buyers": "Daha net sinyal gelene kadar bekle.",
        "holders": "Tut ve desteği izle. Acil işlem yok.",
        "watch": "Destek ve direnç seviyelerini izle.",
    },
}

_STATE_ICONS: dict[str, str] = {
    "Strong Uptrend": "📈",
    "Bullish Momentum": "📊",
    "Early Breakout": "🚀",
    "Pullback Opportunity": "🔄",
    "Weakening Trend": "📉",
    "Breakdown Risk": "⚡",
    "No Clear Signal": "➖",
}

_STATE_LABEL: dict[Portfolio, dict[str, str]] = {
    "usa": {
        "Strong Uptrend": "Going up strong",
        "Bullish Momentum": "Moving up",
        "Early Breakout": "Just broke out",
        "Pullback Opportunity": "Dip — maybe buy",
        "Weakening Trend": "Losing strength",
        "Breakdown Risk": "May fall",
        "No Clear Signal": "Unclear",
    },
    "bist": {
        "Strong Uptrend": "Güçlü Yükseliş",
        "Bullish Momentum": "Yükseliş Momentumu",
        "Early Breakout": "Erken Kırılım",
        "Pullback Opportunity": "Geri Çekilme Fırsatı",
        "Weakening Trend": "Zayıflayan Trend",
        "Breakdown Risk": "Düşüş Riski",
        "No Clear Signal": "Net Sinyal Yok",
    },
}

_CONF_LABEL: dict[Portfolio, dict[str, str]] = {
    "usa": {"High": "High", "Medium": "Medium", "Low": "Low"},
    "bist": {"High": "Yüksek", "Medium": "Orta", "Low": "Düşük"},
}

_TR_MONTHS = (
    "", "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
)

_MAIL_LABEL: dict[Portfolio, dict[str, str]] = {
    "usa": {"morning": "Morning", "evening": "Evening", "intraday": "Mid-day"},
    "bist": {"morning": "Sabah", "evening": "Akşam", "intraday": "Gün İçi"},
}

# ── Market headers ────────────────────────────────────────────────────────────

_MARKET_HEADERS: dict[str, dict[str, dict[str, str]]] = {
    "bist": {
        "TR": {
            "morning": "🇹🇷 BIST",
            "intraday": "🇹🇷 BIST",
            "evening": "🇹🇷 BIST — Kapanış",
        },
        "NL": {
            "morning": "🇳🇱 Amsterdam",
            "evening": "🇳🇱 Amsterdam — Kapanış",
        },
        "USA": {
            "morning": "🇺🇸 ABD — Dünkü Kapanış",
            "intraday": "🇺🇸 ABD — Dünkü Kapanış",
            "evening": "🇺🇸 ABD — Kapanış",
        },
        "CRYPTO": {
            "morning": "🪙 Kripto",
            "evening": "🪙 Kripto",
        },
    },
    "usa": {
        "USA": {
            "morning": "🇺🇸 US Stocks (live now)",
            "intraday": "🇺🇸 US Stocks (live now)",
            "evening": "🇺🇸 US Stocks (today's close)",
        },
    },
}

_MARKET_HEADERS_BIST_EN: dict[str, dict[str, str]] = {
    "TR": {
        "morning": "🇹🇷 BIST",
        "intraday": "🇹🇷 BIST",
        "evening": "🇹🇷 BIST — Close",
    },
    "NL": {
        "morning": "🇳🇱 Amsterdam",
        "evening": "🇳🇱 Amsterdam — Close",
    },
    "USA": {
        "morning": "🇺🇸 US — Yesterday's close",
        "intraday": "🇺🇸 US — Yesterday's close",
        "evening": "🇺🇸 US — Close",
    },
    "CRYPTO": {
        "morning": "🪙 Crypto",
        "evening": "🪙 Crypto",
    },
}

_LIVE_MARKETS_BIST: dict[str, set[str]] = {
    "morning": {"TR", "NL", "CRYPTO"},
    "intraday": {"TR"},
    "evening": {"USA", "CRYPTO"},
}

_LIVE_MARKETS_USA: dict[str, set[str]] = {
    "morning": {"USA"},
    "intraday": {"USA"},
    "evening": set(),
}

_MARKET_ORDER_BIST = ["TR", "NL", "USA", "CRYPTO"]
_MARKET_ORDER_USA = ["USA"]

_SIGNAL_TR: dict[str, str] = {
    "Momentum building": "Momentum artıyor",
    "Momentum fading": "Momentum zayıflıyor",
    "Strong uptrend": "Güçlü yükseliş trendi",
    "Strong downtrend": "Güçlü düşüş trendi",
    "Buyers in control": "Alıcılar baskın",
    "Sellers in control": "Satıcılar baskın",
    "Overheated, pullback risk": "Aşırı alım — geri çekilme riski",
    "Oversold, panic selling": "Aşırı satım — panik satışı",
    "Bounce starting": "Toparlanma başlıyor",
    "Strong uptrend (room to go)": "Güçlü trend — hâlâ hareket alanı var",
    "Healthy momentum": "Sağlıklı momentum",
    "Real buying": "Gerçek alım (hacimli yükseliş)",
    "Real selling pressure": "Gerçek satış baskısı (hacimli düşüş)",
    "Weak buying": "Zayıf alım (düşük hacim)",
    "Trend losing power": "Trend gücünü kaybediyor",
    "Trend accelerating": "Trend hızlanıyor",
    "Low volatility (flat market)": "Düşük volatilite — yatay piyasa",
    "Strong momentum": "Güçlü momentum",
    "Weak, oversold": "Zayıf — aşırı satım bölgesi",
    "Power increasing": "Hareket gücü artıyor",
    "Energy declining": "Hareket enerjisi azalıyor",
    "Real breakout": "Gerçek kırılım (hacimli)",
    "Fake breakout risk": "Sahte kırılım riski (düşük hacim)",
    "Downtrend risk": "Düşüş trendi riski",
    "Buyers defending": "Alıcılar desteği koruyor",
    "Watch for rejection": "Dirençte red riski — izle",
}

_TF_LABEL: dict[Portfolio, dict[str, str]] = {
    "usa": {"daily": "Daily", "hourly": "Hourly", "four_hour": "4H"},
    "bist": {"daily": "Günlük", "hourly": "Saatlik", "four_hour": "4 Saat"},
}


_SUBJECT_EN: dict[Portfolio, str] = {
    "bist": "Portfolio Update (BIST)",
    "usa": "Portfolio Update (US)",
}


def _english_labels(portfolio: Portfolio) -> dict[str, str]:
    """Shared English mail labels for BIST and USA."""
    return {
        "title": "PORTFOLIO UPDATE",
        "subject": _SUBJECT_EN[portfolio],
        "status": "Signal",
        "price": "Price",
        "live": "Now",
        "close": "Last close",
        "support": "Support",
        "resistance": "Resistance",
        "what_to_do": "★ YOUR MOVE",
        "new_buyer": "Want to buy?",
        "holder": "Already own it?",
        "watch": "Watch this price",
        "summary": "BRIEF ANALYSIS",
        "short_term": "SHORT-TERM (1H)",
        "mid_term": "MID-TERM (4H)",
        "long_term": "LONG-TERM (Daily)",
        "stop_run": "STOP RUN REVERSAL (AGPro)",
        "srp_title": "SRP — Stop Run Planner (4H)",
        "pap_title": "PAP — Price Acceptance Profile (Daily)",
        "kap_title": "KAP (today)",
        "kap_alert_title": "⚠️ KAP today — new disclosures on your watchlist:",
        "kap_none": "No disclosure today.",
        "kap_more": "more items",
        "no_signals": "Nothing big to report.",
        "note": "Note",
        "changes": "What changed since last mail",
        "no_changes": "Nothing changed.",
        "first_briefing": "First mail — nothing to compare.",
        "new_ticker": "New stock on list — no old mail.",
        "no_status_change": "Same as before",
        "confidence": "trust level",
        "disclaimer": "Not advice. Your call.",
    }


def _labels(portfolio: Portfolio, lang: Lang = "tr") -> dict[str, str]:
    if _is_turkish(portfolio, lang):
        return {
            "title": "PORTFÖY ÖZETİ",
            "subject": "Portföy Özeti",
            "status": "Durum",
            "price": "Fiyat",
            "live": "Canlı",
            "close": "Kapanış",
            "support": "Destek",
            "resistance": "Direnç",
            "what_to_do": "★ NE YAPMALI?",
            "new_buyer": "Yeni alıcı",
            "holder": "Elinde Olan",
            "watch": "İzle",
            "summary": "KISA ÖZET",
            "short_term": "KISA VADE (1 Saat)",
            "mid_term": "ORTA VADE (4 Saat)",
            "long_term": "UZUN VADE (Günlük)",
            "stop_run": "STOP AVI (AGPro)",
            "srp_title": "SRP — Stop Run Planner (4 Saat)",
            "pap_title": "PAP — Fiyat Kabul Profili (Günlük)",
            "kap_title": "KAP (bugün)",
            "kap_alert_title": "⚠️ KAP bugün — takip listende yeni bildirim:",
            "kap_none": "Bugün bildirim yok.",
            "kap_more": "bildirim daha",
            "no_signals": "Belirgin sinyal yok.",
            "changes": "Son özete göre değişenler",
            "no_changes": "Hiçbir hisse durum değiştirmedi.",
            "first_briefing": "İlk özet — karşılaştırma yok.",
            "new_ticker": "Yeni takip listesinde — önceki özet yok.",
            "no_status_change": "Durum değişmedi",
            "confidence": "güven",
            "disclaimer": "Yatırım tavsiyesi değildir.",
        }
    return _english_labels(portfolio)


def _format_date(d: date, portfolio: Portfolio, lang: Lang = "tr") -> str:
    if _is_turkish(portfolio, lang):
        return f"{d.day} {_TR_MONTHS[d.month]} {d.year}"
    return d.strftime("%B %d, %Y")


def _state_label(state: str, portfolio: Portfolio, lang: Lang = "tr") -> str:
    key = "bist" if _is_turkish(portfolio, lang) else "usa"
    return _STATE_LABEL[key].get(state, state)


def _conf_label(confidence: str, portfolio: Portfolio, lang: Lang = "tr") -> str:
    key = "bist" if _is_turkish(portfolio, lang) else "usa"
    return _CONF_LABEL[key].get(confidence, confidence)


def _live_markets(mail_type: MailType, portfolio: Portfolio) -> set[str]:
    table = _LIVE_MARKETS_USA if portfolio == "usa" else _LIVE_MARKETS_BIST
    return table.get(mail_type, set())


def _detect_market(ticker: str) -> str:
    if ticker.endswith(".IS"):
        return "TR"
    if ticker.endswith(".AS"):
        return "NL"
    if "-" in ticker:
        return "CRYPTO"
    return "USA"


def _currency(ticker: str) -> str:
    if ticker.endswith(".IS"):
        return "₺"
    if ticker.endswith(".AS"):
        return "€"
    return "$"


def _market_header(market: str, mail_type: MailType, portfolio: Portfolio, lang: Lang = "tr") -> str:
    if portfolio == "bist" and not _is_turkish(portfolio, lang):
        headers = _MARKET_HEADERS_BIST_EN.get(market, {})
    else:
        headers = _MARKET_HEADERS.get(portfolio, {}).get(market, {})
    return headers.get(mail_type, headers.get("morning", market))


def _market_order(portfolio: Portfolio) -> list[str]:
    return _MARKET_ORDER_USA if portfolio == "usa" else _MARKET_ORDER_BIST


def _group_by_market(classifications: dict, portfolio: Portfolio = "bist") -> dict[str, list[str]]:
    order = _market_order(portfolio)
    groups: dict[str, list[str]] = {m: [] for m in order}
    for ticker in classifications:
        market = _detect_market(ticker)
        groups.setdefault(market, []).append(ticker)
    for market in groups:
        groups[market].sort()
    return groups


# ── Context formatter ─────────────────────────────────────────────────────────


def _format_stop_run_line(stop_run: dict, label: str) -> str:
    if stop_run["bullish_stop_run"]:
        return f"STOP RUN {label}: BULLISH — swept {stop_run['sweep_low']:.2f} below support, reversed up"
    if stop_run["bearish_stop_run"]:
        return f"STOP RUN {label}: BEARISH — swept {stop_run['sweep_high']:.2f} above resistance, reversed down"
    return f"STOP RUN {label}: none"


def _format_ticker_context(
    ticker: str,
    classification: dict,
    ta_result: dict,
    portfolio: Portfolio = "bist",
    lang: Lang = "tr",
) -> str:
    daily_ta = ta_result["daily"].get(ticker)
    hourly_ta = ta_result["hourly"].get(ticker)
    four_hour_ta = ta_result.get("four_hour", {}).get(ticker)

    state = classification["state"]
    confidence = classification["confidence"]
    positive = classification["positive_signals"]
    total = classification["total_signals"]
    cur = _currency(ticker)

    if _is_turkish(portfolio, lang):
        lines = [
            f"TICKER: {ticker}",
            f"STATE: {state}",
            f"CONFIDENCE: {confidence} ({positive}/{total} signals positive)",
            "",
        ]
        daily_hdr, h4_hdr, hour_hdr = "DAILY SIGNALS:", "4H SIGNALS:", "HOURLY SIGNALS:"
        floor_l, ceil_l = "Support", "Resistance"
    else:
        lines = [
            f"STOCK: {ticker}",
            f"SIGNAL: {_state_label(state, portfolio, lang)}",
            f"TRUST: {_conf_label(confidence, portfolio, lang)} ({positive} good / {total} total checks)",
            "",
        ]
        daily_hdr, h4_hdr, hour_hdr = "DAILY SIGNALS:", "4H SIGNALS:", "HOURLY SIGNALS:"
        floor_l, ceil_l = "Support", "Resistance"

    if daily_ta and daily_ta.get("signals"):
        lines.append(daily_hdr)
        for sig in daily_ta["signals"]:
            lines.append(_translate_signal_line(sig, portfolio, lang))
        lines.append("")

    if four_hour_ta and four_hour_ta.get("signals"):
        lines.append(h4_hdr)
        for sig in four_hour_ta["signals"]:
            lines.append(_translate_signal_line(sig, portfolio, lang))
        lines.append("")

    if hourly_ta and hourly_ta.get("signals"):
        lines.append(hour_hdr)
        for sig in hourly_ta["signals"]:
            lines.append(_translate_signal_line(sig, portfolio, lang))
        lines.append("")

    lines.append("KEY VALUES:")
    if daily_ta and daily_ta.get("indicators"):
        ind = daily_ta["indicators"]
        lines.append(f"Close: {cur}{ind['latest_close']:.2f}")
        if ind.get("rsi"):
            lines.append(f"RSI: {ind['rsi']['value']:.1f}")
        sr = ind["support_resistance"]
        lines.append(f"{floor_l}: {cur}{sr['nearest_support']:.2f}")
        lines.append(f"{ceil_l}: {cur}{sr['nearest_resistance']:.2f}")
        vol = ind["volume"]
        vol_label = "Volume ratio"
        lines.append(f"{vol_label}: {vol['volume_ratio']:.2f}x average")
        if ind.get("stop_run"):
            lines.append(_format_stop_run_line(ind["stop_run"], "Daily"))
        if ind.get("pap"):
            pap = ind["pap"]
            if _is_turkish(portfolio, lang):
                lines.append(f"PAP Action: {pap['action']} (Score: {pap['acceptance_score']:.0f}/100)")
                lines.append(f"PAP POC: {pap['poc']:.2f}  VAL: {pap['val']:.2f}  VAH: {pap['vah']:.2f}")
                lines.append(f"PAP Rotation: {pap['rotation']}")
            else:
                lines.append(f"PAP Action: {pap['action']} (Score: {pap['acceptance_score']:.0f}/100)")
                lines.append(f"PAP POC: {pap['poc']:.2f}  VAL: {pap['val']:.2f}  VAH: {pap['vah']:.2f}")
                lines.append(f"PAP Rotation: {pap['rotation']}")

    if four_hour_ta and four_hour_ta.get("indicators"):
        ind4 = four_hour_ta["indicators"]
        if ind4.get("stop_run"):
            lines.append(_format_stop_run_line(ind4["stop_run"], "4H"))
        if ind4.get("srp"):
            srp = ind4["srp"]
            if _is_turkish(portfolio, lang):
                lines.append(f"SRP Action: {srp['action']} (Score: {srp['reversal_readiness_score']:.0f}/100)")
                if srp["detected"]:
                    lines.append(f"SRP Direction: {srp['direction'].upper()}")
                    lines.append(f"SRP Target: {srp['target']:.2f}  Invalidation: {srp['invalidation']:.2f}")
            else:
                lines.append(f"SRP Action: {srp['action']} (Score: {srp['reversal_readiness_score']:.0f}/100)")
                if srp["detected"]:
                    lines.append(f"SRP Direction: {srp['direction'].upper()}")
                    lines.append(f"SRP Target: {srp['target']:.2f}  Invalidation: {srp['invalidation']:.2f}")

    return "\n".join(lines)


# ── Claude API ────────────────────────────────────────────────────────────────


def _call_claude(
    context: str,
    client: anthropic.Anthropic,
    portfolio: Portfolio,
    lang: Lang = "tr",
) -> tuple[str, str, str, str]:
    prompt_key = "bist" if _is_turkish(portfolio, lang) else "usa"
    prompt = _SYSTEM_PROMPT_BIST if prompt_key == "bist" else _SYSTEM_PROMPT_USA
    fallbacks = _FALLBACKS[prompt_key]
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            system=[
                {
                    "type": "text",
                    "text": prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": "user", "content": context}],
        )
        text = response.content[0].text if response.content else ""
        return _parse_sections(text, prompt_key)
    except Exception as exc:
        logger.error("Claude API call failed: %s", exc)
        return (
            fallbacks["summary"],
            fallbacks["buyers"],
            fallbacks["holders"],
            fallbacks["watch"],
        )


def _parse_sections(text: str, prompt_key: Portfolio | Literal["usa"]) -> tuple[str, str, str, str]:
    markers = _SECTION_MARKERS["bist" if prompt_key == "bist" else "usa"]
    fallbacks = _FALLBACKS["bist" if prompt_key == "bist" else "usa"]
    bufs: dict[str, list[str]] = {k: [] for k in fallbacks}
    current = None

    for line in text.splitlines():
        matched = False
        for marker, key in markers.items():
            if line.startswith(marker):
                current = key
                remainder = line[len(marker):].strip()
                if remainder:
                    bufs[key].append(remainder)
                matched = True
                break
        if not matched and current:
            bufs[current].append(line)

    return (
        "\n".join(bufs["summary"]).strip() or fallbacks["summary"],
        "\n".join(bufs["buyers"]).strip() or fallbacks["buyers"],
        "\n".join(bufs["holders"]).strip() or fallbacks["holders"],
        "\n".join(bufs["watch"]).strip() or fallbacks["watch"],
    )


# ── Signal helpers ────────────────────────────────────────────────────────────


_SIGNAL_LINE_TR: dict[str, str] = {
    "EMA5 > EMA20 = Momentum building": "EMA5 > EMA20 = Momentum artıyor",
    "EMA5 < EMA20 = Momentum fading": "EMA5 < EMA20 = Momentum zayıflıyor",
    "EMA20 > EMA50 = Strong uptrend": "EMA20 > EMA50 = Güçlü yükseliş trendi",
    "EMA20 < EMA50 = Strong downtrend": "EMA20 < EMA50 = Güçlü düşüş trendi",
    "Price above EMA20 = Buyers in control": "Fiyat EMA20 üstünde = Alıcılar baskın",
    "Price below EMA20 = Sellers in control": "Fiyat EMA20 altında = Satıcılar baskın",
    "Volume high on up day = Real buying": "Yükselişte yüksek hacim = Gerçek alım",
    "Volume high on down day = Real selling pressure": "Düşüşte yüksek hacim = Gerçek satış",
    "Volume low on up day = Weak buying": "Yükselişte düşük hacim = Zayıf alım",
    "Volume dropping = Trend losing power": "Hacim düşüyor = Trend zayıflıyor",
    "BB widening = Trend accelerating": "BB genişliyor = Trend hızlanıyor",
    "BB squeezing = Low volatility (flat market)": "BB daralıyor = Düşük volatilite, yatay",
    "Price at upper BB = Strong momentum": "Fiyat üst BB = Güçlü momentum",
    "Price at lower BB = Weak, oversold": "Fiyat alt BB = Zayıf, aşırı satım",
    "ATR rising = Power increasing": "ATR artıyor = Hareket gücü artıyor",
    "ATR falling = Energy declining": "ATR düşüyor = Hareket enerjisi azalıyor",
    "Price breaks resistance with volume = Real breakout": "Direnç kırılımı + hacim = Gerçek kırılım",
    "Price above resistance but low volume = Fake breakout risk": "Direnç üstü ama düşük hacim = Sahte kırılım riski",
    "Price breaking below support = Downtrend risk": "Destek kırılımı = Düşüş riski",
    "Price bouncing off support = Buyers defending": "Destekten sekme = Alıcılar koruyor",
    "Price approaching resistance = Watch for rejection": "Dirence yaklaşıyor = Red riski",
}

_SIGNAL_LINE_USA: dict[str, str] = {
    "EMA5 > EMA20 = Momentum building": "Last hour: price going up",
    "EMA5 < EMA20 = Momentum fading": "Last hour: move slowing down",
    "EMA20 > EMA50 = Strong uptrend": "Trend: going up",
    "EMA20 > EMA50 = Uptrend starting": "Trend: starting to go up",
    "EMA20 < EMA50 = Strong downtrend": "Trend: going down",
    "EMA20 < EMA50 = Downtrend forming": "Trend: starting to fall",
    "EMA50 > EMA200 = Strong uptrend": "Big trend: still up",
    "EMA50 < EMA200 = Strong downtrend": "Big trend: still down",
    "Price above EMA20 = Buyers in control": "Price above key line — buyers winning",
    "Price below EMA20 = Sellers in control": "Price below key line — sellers winning",
    "Volume high on up day = Real buying": "Up day with big volume — real buying",
    "Volume high on down day = Real selling pressure": "Down day with big volume — real selling",
    "Volume low on up day = Weak buying": "Up day but low volume — weak move",
    "Volume dropping = Trend losing power": "Volume falling — move may stop",
    "BB widening = Trend accelerating": "Price swings getting bigger — move speeding up",
    "BB squeezing = Low volatility (flat market)": "Price stuck in a tight range",
    "Price at upper BB = Strong momentum": "Price near top of range — strong up move",
    "Price at lower BB = Weak, oversold": "Price near bottom — very weak",
    "ATR rising = Power increasing": "Bigger daily moves — more action",
    "ATR falling = Energy declining": "Smaller daily moves — less action",
    "Price breaks resistance with volume = Real breakout": "Broke resistance with volume — may keep going up",
    "Price above resistance but low volume = Fake breakout risk": "Broke resistance but low volume — may fail",
    "Price breaking below support = Downtrend risk": "Broke support — may keep falling",
    "Price bouncing off support = Buyers defending": "Bounced off support — buyers stepped in",
    "Price approaching resistance = Watch for rejection": "Near resistance — may get pushed back",
}

_SIGNAL_HINTS: dict[str, dict[str, str]] = {
    "Momentum building": {
        "usa": "Short-term: price pushing up",
        "bist": "Kısa vadede alıcılar fiyatı yukarı itiyor",
    },
    "Momentum fading": {
        "usa": "Up move is slowing — be careful buying",
        "bist": "Yükseliş yavaşlıyor — yeni alımda dikkat",
    },
    "Strong uptrend": {
        "usa": "Main trend is up — good sign",
        "bist": "Büyük trend yukarı — olumlu zemin",
    },
    "Strong downtrend": {
        "usa": "Main trend is down — risky",
        "bist": "Büyük trend aşağı — risk yüksek",
    },
    "Buyers in control": {
        "usa": "Buyers are in charge right now",
        "bist": "Fiyat önemli ortalamanın üstünde — alıcılar önde",
    },
    "Sellers in control": {
        "usa": "Sellers are in charge right now",
        "bist": "Fiyat ortalamanın altında — satıcılar önde",
    },
    "Real buying": {
        "usa": "Volume backs the up move",
        "bist": "Hacim yükselişi doğruluyor",
    },
    "Weak buying": {
        "usa": "Price up but volume low — may not last",
        "bist": "Yükseliş var ama hacim zayıf — hareket sürmeyebilir",
    },
    "Real selling pressure": {
        "usa": "Volume backs the down move",
        "bist": "Hacim satışı doğruluyor",
    },
    "Trend losing power": {
        "usa": "Fewer people trading — move may stall",
        "bist": "Katılım azalıyor — trend durabilir",
    },
    "Healthy momentum": {
        "usa": "Not too hot, not too cold — OK zone",
        "bist": "RSI normal bölgede — aşırı alım değil",
    },
    "Overheated, pullback risk": {
        "usa": "Ran up a lot — may pull back soon",
        "bist": "RSI çok yüksek — geri çekilme olabilir",
    },
    "Oversold, panic selling": {
        "usa": "Sold off hard — bounce possible but risky",
        "bist": "RSI çok düşük — tepki olabilir ama riskli",
    },
    "Downtrend risk": {
        "usa": "Support broke — don't buy yet",
        "bist": "Destek kırıldı — yeni alım riskli",
    },
    "Buyers defending": {
        "usa": "Support held — buyers showed up",
        "bist": "Destek tutuyor — olumlu işaret",
    },
    "Real breakout": {
        "usa": "Broke out with volume — may run higher",
        "bist": "Hacimli kırılım — yukarı sürebilir",
    },
    "Fake breakout risk": {
        "usa": "Broke out but weak volume — may reverse",
        "bist": "Hacimsiz kırılım — geri dönebilir",
    },
}


def _translate_signal_line(signal: str, portfolio: Portfolio, lang: Lang = "tr") -> str:
    """Translate signals for Turkish mail only — English keeps original metrics."""
    if not _is_turkish(portfolio, lang):
        return signal

    prefix = signal[:2] if signal[:1] in "✅❌⚠️➖" else ""
    body = signal[2:].strip() if prefix else signal

    for en, tr in _SIGNAL_LINE_TR.items():
        if en in body:
            body = body.replace(en, tr)
            break
    # RSI lines: translate template
    if "RSI" in body and "=" in body:
        body = body.replace(" = Bounce starting", " = Toparlanma başlıyor")
        body = body.replace(" = Strong uptrend (room to go)", " = Güçlü trend, hâlâ alan var")
        body = body.replace(" = Healthy momentum", " = Sağlıklı momentum")
        body = body.replace(" = Overheated, pullback risk", " = Aşırı alım, geri çekilme riski")
        body = body.replace(" = Oversold, panic selling", " = Aşırı satım, panik satışı")
        body = body.replace(" = Momentum fading", " = Momentum zayıflıyor")
    return f"{prefix} {body}".strip() if prefix else body


def _signal_hint(signal: str, portfolio: Portfolio, lang: Lang = "tr") -> str | None:
    msg = _signal_message(signal)
    hint_key = "bist" if _is_turkish(portfolio, lang) else "usa"
    for key, hints in _SIGNAL_HINTS.items():
        if key.lower() in msg.lower():
            return hints[hint_key]
    return None


def _format_signal_sections(
    sections: list[tuple[str, list[str]]],
    portfolio: Portfolio,
    lang: Lang = "tr",
) -> list[str]:
    lines: list[str] = []
    bar_width = 41
    L = _labels(portfolio, lang)

    for i, (title, signals) in enumerate(sections):
        if i > 0:
            lines.append("")
        underline = "─" * max(8, bar_width - len(title) - 3)
        lines.append(f"── {title} {underline}")
        if not signals:
            lines.append(f"  ➖ {L['no_signals']}")
            continue
        for signal in signals:
            line = _translate_signal_line(signal, portfolio, lang)
            lines.append(f"  {line}")
            hint = _signal_hint(signal, portfolio, lang)
            if hint:
                lines.append(f"     ↳ {hint}")
    return lines


def _timeframe_sections(
    mail_type: MailType,
    hourly_ta: dict | None,
    four_hour_ta: dict | None,
    daily_ta: dict | None,
    portfolio: Portfolio,
    lang: Lang = "tr",
) -> list[tuple[str, list[str]]]:
    L = _labels(portfolio, lang)
    sections: list[tuple[str, list[str]]] = [
        (L["short_term"], hourly_ta.get("signals", []) if hourly_ta else []),
    ]
    if mail_type == "intraday":
        sections.append(
            (L["mid_term"], four_hour_ta.get("signals", []) if four_hour_ta else [])
        )
    sections.append(
        (L["long_term"], daily_ta.get("signals", []) if daily_ta else [])
    )
    return sections


def _format_srp_block(srp: dict, cur: str, portfolio: Portfolio, lang: Lang = "tr") -> str:
    L = _labels(portfolio, lang)
    lines = [f"🔵 {L['srp_title']}:"]
    if not srp["detected"]:
        aside = (
            "Bekle — 4 saatte stop avı setup yok"
            if _is_turkish(portfolio, lang)
            else "Stand Aside — No stop run setup on 4H"
        )
        lines.append(f"  {aside}")
        return "\n".join(lines)

    if _is_turkish(portfolio, lang):
        dir_icon = "🟢 YUKARI" if srp["direction"] == "bullish" else "🔴 AŞAĞI"
        action_desc = {
            "Wait Confirm": "Onay bekle — setup var, teyit lazım",
            "Plan Pullback": "Geri çekilme planla — dönüş setup'ı",
            "Review Reversal": "Dönüş incele — kaliteli setup",
        }.get(srp["action"], srp["action"])
        side = "destek altı" if srp["direction"] == "bullish" else "direnç üstü"
        meaning = (
            "Stop avı sonrası geri döndü — geri çekilme alım fırsatı olabilir"
            if srp["direction"] == "bullish"
            else "Kırılım tutmadı — aşağı devam riski"
        )
        lines.append(f"  Aksiyon:   {action_desc} (Skor: {srp['reversal_readiness_score']:.0f}/100)")
        lines.append(f"  Yön:       {dir_icon} — {cur}{srp['sweep_amount']:.2f} {side} iğne")
        lines.append(f"  Hedef:     {cur}{srp['target']:.2f}  |  Geçersiz: {cur}{srp['invalidation']:.2f}")
        lines.append(f"  Anlam:     {meaning}")
        return "\n".join(lines)

    dir_icon = "🟢 BULLISH" if srp["direction"] == "bullish" else "🔴 BEARISH"
    action_desc = {
        "Wait Confirm": "Wait Confirm — setup detected, need confirmation",
        "Plan Pullback": "Plan Pullback — reversal setup forming",
        "Review Reversal": "Review Reversal — high-quality setup",
    }.get(srp["action"], srp["action"])
    side = "below support" if srp["direction"] == "bullish" else "above resistance"
    meaning = (
        "Stop hunt reversed — pullback may be a buy zone"
        if srp["direction"] == "bullish"
        else "Breakout failed — watch for more downside"
    )
    lines.append(f"  Action:    {action_desc} (Score: {srp['reversal_readiness_score']:.0f}/100)")
    lines.append(f"  Direction: {dir_icon} — swept {cur}{srp['sweep_amount']:.2f} {side}")
    lines.append(f"  Target:    {cur}{srp['target']:.2f}  |  Invalid: {cur}{srp['invalidation']:.2f}")
    lines.append(f"  Means:     {meaning}")
    return "\n".join(lines)


def _pap_plain_explain(pap: dict, cur: str, portfolio: Portfolio, lang: Lang = "tr") -> list[str]:
    """Plain-language footnotes for PAP levels."""
    poc = f"{cur}{pap['poc']:.2f}"
    val = f"{cur}{pap['val']:.2f}"
    vah = f"{cur}{pap['vah']:.2f}"

    if _is_turkish(portfolio, lang):
        if pap["action"] == "WAIT PROFILE":
            return [
                f"  ↳ POC ({poc}) nedir?",
                f"     Son dönemde en çok alım-satım bu fiyatta oldu.",
                f"     Fiyat düşerse buraya yaklaşabilir — ama skor düşük, önce BEKLE.",
            ]
        return [
            f"  ↳ POC ({poc}): En yoğun işlem fiyatı. Geri çekilmede limit almayı buna yakın düşün.",
            f"  ↳ VAL ({val}): Denge bölgesinin alt sınırı — destek gibi düşün.",
            f"  ↳ VAH ({vah}): Denge bölgesinin üst sınırı — direnç gibi düşün.",
        ]

    if pap["action"] == "WAIT PROFILE":
        return [
            f"  ↳ What is POC ({poc})?",
            f"     Most trades happened near this price lately.",
            f"     Price may come back here — but score is low, WAIT first.",
        ]
    return [
        f"  ↳ POC ({poc}): Busiest price. On a dip, think about buying near here.",
        f"  ↳ VAL ({val}): Bottom of fair zone — like support.",
        f"  ↳ VAH ({vah}): Top of fair zone — like resistance.",
    ]


def _format_pap_block(pap: dict, cur: str, portfolio: Portfolio, lang: Lang = "tr") -> str:
    L = _labels(portfolio, lang)
    lines = [f"🟠 {L['pap_title']}:"]

    if _is_turkish(portfolio, lang):
        action_desc = {
            "WAIT PROFILE": "BEKLE — henüz yeterli fiyat kabulü yok",
            "ACCEPTANCE READY": "KABUL HAZIR — fiyat denge bölgesinde kabul gördü",
            "ACCEPTED UP": "YUKARI KABUL — denge yukarı kayıyor",
            "ACCEPTED DOWN": "AŞAĞI KABUL — denge aşağı kayıyor",
            "EDGE REVIEW": "SINIR İNCELE — denge sınırı test ediliyor",
            "REJECTION REVIEW": "RED İNCELE — fiyat denge dışında, risk yüksek",
        }.get(pap["action"], pap["action"])
        rotation_text = {
            "up": "↑ Yukarı — alıcılar güçleniyor",
            "down": "↓ Aşağı — satıcılar güçleniyor",
            "stable": "→ Sabit — net kayma yok",
        }.get(pap["rotation"], "")
        lines.append(f"  Durum:     {action_desc}")
        lines.append(f"  Skor:      {pap['acceptance_score']:.0f}/100  |  POC: {cur}{pap['poc']:.2f}")
        if pap["action"] != "WAIT PROFILE":
            lines.append(f"  Denge:     {cur}{pap['val']:.2f} (VAL) → {cur}{pap['vah']:.2f} (VAH)")
            lines.append(f"  Kayma:     {rotation_text}")
        lines.extend(_pap_plain_explain(pap, cur, portfolio, lang))
        return "\n".join(lines)

    action_desc = {
        "WAIT PROFILE": "WAIT — not enough price acceptance yet",
        "ACCEPTANCE READY": "ACCEPTANCE READY — price accepted in balance zone",
        "ACCEPTED UP": "ACCEPTED UP — balance shifting up",
        "ACCEPTED DOWN": "ACCEPTED DOWN — balance shifting down",
        "EDGE REVIEW": "EDGE REVIEW — testing balance edge",
        "REJECTION REVIEW": "REJECTION REVIEW — price outside balance, higher risk",
    }.get(pap["action"], pap["action"])
    rotation_text = {
        "up": "↑ Up — buyers gaining control",
        "down": "↓ Down — sellers gaining control",
        "stable": "→ Stable — no clear shift",
    }.get(pap["rotation"], "")
    lines.append(f"  Action:    {action_desc}")
    lines.append(f"  Score:     {pap['acceptance_score']:.0f}/100  |  POC: {cur}{pap['poc']:.2f}")
    if pap["action"] != "WAIT PROFILE":
        lines.append(f"  Balance:   {cur}{pap['val']:.2f} (VAL) → {cur}{pap['vah']:.2f} (VAH)")
        lines.append(f"  Rotation:  {rotation_text}")
    lines.extend(_pap_plain_explain(pap, cur, portfolio, lang))
    return "\n".join(lines)


def _format_stop_run_block(
    four_hour_ta: dict | None,
    daily_ta: dict | None,
    portfolio: Portfolio,
    lang: Lang = "tr",
) -> list[str]:
    L = _labels(portfolio, lang)
    has_4h = four_hour_ta and four_hour_ta.get("indicators") and four_hour_ta["indicators"].get("stop_run")
    has_daily = daily_ta and daily_ta.get("indicators") and daily_ta["indicators"].get("stop_run")
    if not has_4h and not has_daily:
        return []

    lines = [f"🎯 {L['stop_run']}:"]
    none_txt = "Stop avı yok" if _is_turkish(portfolio, lang) else "No stop run detected"

    if has_4h:
        sr4 = four_hour_ta["indicators"]["stop_run"]
        if sr4["bullish_stop_run"]:
            if _is_turkish(portfolio, lang):
                lines.append(
                    f"  4 Saat: ✅ YUKARI — {sr4['sweep_low']:.2f} destek altına iğne, geri döndü"
                )
                lines.append("     ↳ Satışlar tuzağa düştü — kısa vadede olumlu")
            else:
                lines.append(
                    f"  4H:    ✅ BULLISH — swept {sr4['sweep_low']:.2f} below support, reversed up"
                )
                lines.append("     ↳ Sellers trapped — short-term positive")
        elif sr4["bearish_stop_run"]:
            if _is_turkish(portfolio, lang):
                lines.append(
                    f"  4 Saat: ❌ AŞAĞI — {sr4['sweep_high']:.2f} direnç üstü iğne, geri döndü"
                )
                lines.append("     ↳ Alıcılar tuzağa düştü — kısa vadede olumsuz")
            else:
                lines.append(
                    f"  4H:    ❌ BEARISH — swept {sr4['sweep_high']:.2f} above resistance, reversed down"
                )
                lines.append("     ↳ Buyers trapped — short-term negative")
        else:
            lines.append(f"  4H:    ➖ {none_txt}")

    if has_daily:
        srd = daily_ta["indicators"]["stop_run"]
        if srd["bullish_stop_run"]:
            if _is_turkish(portfolio, lang):
                lines.append(
                    f"  Günlük: ✅ YUKARI — {srd['sweep_low']:.2f} destek altına iğne, güçlü dönüş"
                )
            else:
                lines.append(
                    f"  Daily: ✅ BULLISH — swept {srd['sweep_low']:.2f} below support, strong reversal"
                )
        elif srd["bearish_stop_run"]:
            if _is_turkish(portfolio, lang):
                lines.append(
                    f"  Günlük: ❌ AŞAĞI — {srd['sweep_high']:.2f} direnç üstü iğne, güçlü red"
                )
            else:
                lines.append(
                    f"  Daily: ❌ BEARISH — swept {srd['sweep_high']:.2f} above resistance, strong rejection"
                )
        else:
            lines.append(f"  Daily: ➖ {none_txt}")

    return lines


def _signal_message(signal: str) -> str:
    """Extract readable part after '=' or emoji prefix."""
    if "=" in signal:
        return signal.split("=", 1)[1].strip()
    return re.sub(r"^[✅❌⚠️➖]\s*", "", signal).strip()


_STATE_RANK: dict[str, int] = {
    "Strong Uptrend": 6,
    "Early Breakout": 5,
    "Bullish Momentum": 4,
    "Pullback Opportunity": 3,
    "No Clear Signal": 2,
    "Weakening Trend": 1,
    "Breakdown Risk": 0,
}


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


def _direction_suffix_en(direction: str) -> str:
    return {"improved": " — getting better", "worsened": " — watch out", "shifted": ""}[direction]


def _mail_label_text(portfolio: Portfolio, mail_type: MailType, lang: Lang = "tr") -> str:
    if portfolio == "bist" and lang == "en":
        return {"morning": "Morning", "evening": "Evening", "intraday": "Mid-day"}[mail_type]
    return _MAIL_LABEL[portfolio][mail_type]


def _comparison_label_localized(
    previous_snapshot: dict,
    portfolio: Portfolio,
    lang: Lang = "tr",
) -> str:
    prev_mail = previous_snapshot.get("mail_type", "")
    default = "son özet" if _is_turkish(portfolio, lang) else "last mail"
    prev_label = _MAIL_LABEL[portfolio].get(prev_mail, default)
    if portfolio == "bist" and lang == "en":
        prev_label = _mail_label_text(portfolio, prev_mail, lang) if prev_mail else "last mail"
    snap_date = date.fromisoformat(
        previous_snapshot.get("trading_date", previous_snapshot["saved_at"][:10])
    )
    today = date.today()
    gap_days = (today - snap_date).days

    if gap_days > 1 or (gap_days == 1 and today.weekday() == 0):
        if _is_turkish(portfolio, lang):
            days = ["Pzt", "Sal", "Çar", "Per", "Cum", "Cmt", "Paz"]
            day_label = f"{days[snap_date.weekday()]} {snap_date.day} {_TR_MONTHS[snap_date.month]}"
            return f"son kapanış ({prev_label}, {day_label})"
        day_label = snap_date.strftime("%a %d %b")
        return f"last close ({prev_label}, {day_label})"

    return prev_label


# ── Status change (localized) ─────────────────────────────────────────────────


def _format_status_change_localized(
    ticker: str,
    classification: dict,
    previous_snapshot: dict | None,
    portfolio: Portfolio,
    lang: Lang = "tr",
) -> str:
    L = _labels(portfolio, lang)

    if previous_snapshot is None:
        return f"📌 {L['first_briefing']}"

    previous = previous_snapshot.get("tickers", {}).get(ticker)
    if previous is None:
        return f"📌 {L['new_ticker']}"

    compare_label = _comparison_label_localized(previous_snapshot, portfolio, lang)
    prev_state = previous["state"]
    curr_state = classification["state"]
    prev_label = _state_label(prev_state, portfolio, lang)
    curr_label = _state_label(curr_state, portfolio, lang)

    if prev_state == curr_state:
        prev_conf = previous.get("confidence", "")
        curr_conf = classification["confidence"]
        if prev_conf != curr_conf:
            if _is_turkish(portfolio, lang):
                return (
                    f"📌 {compare_label} sonrası: {L['no_status_change']} ({curr_label}), "
                    f"güven {_conf_label(prev_conf, portfolio, lang)} → "
                    f"{_conf_label(curr_conf, portfolio, lang)}"
                )
            return (
                f"📌 Since {compare_label}: {L['no_status_change']} ({curr_label}), "
                f"trust {_conf_label(prev_conf, portfolio, lang)} → "
                f"{_conf_label(curr_conf, portfolio, lang)}"
            )
        if _is_turkish(portfolio, lang):
            return f"📌 {compare_label} sonrası: {L['no_status_change']} ({curr_label})"
        return f"📌 Since {compare_label}: {L['no_status_change']} ({curr_label})"

    direction = _change_direction(prev_state, curr_state)
    icon = _direction_icon(direction)
    if _is_turkish(portfolio, lang):
        suffix = {"improved": " — iyileşti", "worsened": " — dikkat", "shifted": ""}[direction]
        return f"{icon} {compare_label} sonrası: {prev_label} → {curr_label}{suffix}"
    suffix = _direction_suffix_en(direction)
    return f"{icon} Since {compare_label}: {prev_label} → {curr_label}{suffix}"


def _build_change_summary_localized(
    classifications: dict[str, dict],
    previous_snapshot: dict | None,
    portfolio: Portfolio,
    lang: Lang = "tr",
) -> list[str]:
    L = _labels(portfolio, lang)
    if previous_snapshot is None:
        return []

    compare_label = _comparison_label_localized(previous_snapshot, portfolio, lang)
    changed: list[tuple[str, str, str, str]] = []

    for ticker, current in classifications.items():
        previous = previous_snapshot.get("tickers", {}).get(ticker)
        if previous and previous["state"] != current["state"]:
            direction = _change_direction(previous["state"], current["state"])
            changed.append((ticker, previous["state"], current["state"], direction))

    if not changed:
        if _is_turkish(portfolio, lang):
            return [f"📋 {compare_label} sonrası: {L['no_changes']}"]
        return [f"📋 Since {compare_label}: {L['no_changes']}"]

    lines = [f"📋 {L['changes']} ({compare_label}):"]

    for ticker, old_state, new_state, direction in sorted(changed):
        icon = _direction_icon(direction)
        old_l = _state_label(old_state, portfolio, lang)
        new_l = _state_label(new_state, portfolio, lang)
        lines.append(f"  {icon} {ticker}: {old_l} → {new_l}")
    return lines


def _build_kap_alert_summary(
    kap_by_ticker: dict[str, list[KapNewsItem]] | None,
    portfolio: Portfolio,
    lang: Lang = "tr",
) -> list[str]:
    """Top-of-mail KAP alert — only when at least one ticker has disclosures."""
    if portfolio != "bist" or not kap_by_ticker:
        return []

    alerts: list[tuple[str, list[KapNewsItem]]] = [
        (ticker, items)
        for ticker, items in sorted(kap_by_ticker.items())
        if items
    ]
    if not alerts:
        return []

    L = _labels(portfolio, lang)
    lines = [L["kap_alert_title"]]
    for ticker, items in alerts:
        latest = items[0]
        extra = f" (+{len(items) - 1} more)" if len(items) > 1 else ""
        line = f"  • {ticker} — {latest.time} {latest.subject}{extra}"
        lines.append(line)
    return lines


# ── Ticker block ──────────────────────────────────────────────────────────────


def _format_kap_block(items: list[KapNewsItem], portfolio: Portfolio, lang: Lang = "tr") -> list[str]:
    L = _labels(portfolio, lang)
    lines = [f"📰 {L['kap_title']}:"]
    for item in items:
        lines.append(f"  • {item.time} — {item.subject}")
        if item.summary:
            lines.append(f"    {item.summary}")
    return lines


def build_ticker_section(
    ticker: str,
    classification: dict,
    ta_result: dict,
    client: anthropic.Anthropic,
    mail_type: MailType,
    live_prices: dict[str, float | None],
    previous_snapshot: dict | None = None,
    portfolio: Portfolio = "bist",
    kap_news: list[KapNewsItem] | None = None,
    lang: Lang = "tr",
) -> str:
    L = _labels(portfolio, lang)
    state = classification["state"]
    confidence = classification["confidence"]
    icon = _STATE_ICONS.get(state, "➖")

    context = _format_ticker_context(ticker, classification, ta_result, portfolio, lang)
    summary, for_buyers, for_holders, watch_closely = _call_claude(context, client, portfolio, lang)

    daily_ta = ta_result["daily"].get(ticker)
    hourly_ta = ta_result["hourly"].get(ticker)
    four_hour_ta = ta_result.get("four_hour", {}).get(ticker)

    market = _detect_market(ticker)
    cur = _currency(ticker)
    is_live = market in _live_markets(mail_type, portfolio)
    live_price = live_prices.get(ticker) if is_live else None

    lines: list[str] = [
        "",
        f"{ticker} — {icon} {_state_label(state, portfolio, lang)}  "
        f"({_conf_label(confidence, portfolio, lang)} {L['confidence']})",
    ]

    # ★ ACTION FIRST — user reads this before technicals
    lines.append("")
    lines.append(L["what_to_do"])
    lines.append(f"→ {L['new_buyer']}: {for_buyers}")
    lines.append(f"→ {L['holder']}: {for_holders}")
    lines.append(f"→ {L['watch']}: {watch_closely}")
    lines.append("")
    lines.append(L["summary"])
    lines.append(summary)

    lines.append("")
    lines.append(_format_status_change_localized(
        ticker, classification, previous_snapshot, portfolio, lang,
    ))

    if live_price is not None:
        lines.append(f"💰 {L['live']}: {cur}{live_price:.2f}")
    elif daily_ta and daily_ta.get("indicators"):
        lines.append(f"💰 {L['close']}: {cur}{daily_ta['indicators']['latest_close']:.2f}")

    if daily_ta and daily_ta.get("indicators"):
        sr = daily_ta["indicators"]["support_resistance"]
        lines.append(
            f"🔑 {L['support']}: {cur}{sr['nearest_support']:.2f}  |  "
            f"{L['resistance']}: {cur}{sr['nearest_resistance']:.2f}"
        )

    if portfolio == "bist" and ticker.endswith(".IS") and kap_news:
        lines.append("")
        lines.extend(_format_kap_block(kap_news, portfolio, lang))

    lines.append("")
    lines.extend(_format_signal_sections(
        _timeframe_sections(mail_type, hourly_ta, four_hour_ta, daily_ta, portfolio, lang),
        portfolio,
        lang,
    ))

    stop_run_lines = _format_stop_run_block(four_hour_ta, daily_ta, portfolio, lang)
    if stop_run_lines:
        lines.append("")
        lines.extend(stop_run_lines)

    if four_hour_ta and four_hour_ta.get("indicators") and four_hour_ta["indicators"].get("srp") is not None:
        lines.append("")
        lines.append(_format_srp_block(four_hour_ta["indicators"]["srp"], cur, portfolio, lang))

    if daily_ta and daily_ta.get("indicators") and daily_ta["indicators"].get("pap") is not None:
        lines.append("")
        lines.append(_format_pap_block(daily_ta["indicators"]["pap"], cur, portfolio, lang))

    lines.append("")
    lines.append(f"— {L['disclaimer']}")

    return "\n".join(lines)


# ── Email assembler ───────────────────────────────────────────────────────────

_SEP = "═" * 41


def _next_briefing(mail_type: MailType, portfolio: Portfolio = "bist", lang: Lang = "tr") -> str:
    if _is_turkish(portfolio, lang):
        if mail_type == "morning":
            return "Sonraki özet: Gün İçi (bugün 14:00)"
        if mail_type == "intraday":
            return "Sonraki özet: Akşam (bugün)"
        return "Sonraki özet: Sabah (yarın)"
    if portfolio == "usa":
        if mail_type == "morning":
            return "Next mail: Mid-day (today 1:30 PM ET)"
        if mail_type == "intraday":
            return "Next mail: Evening (today 4:15 PM ET)"
        return "Next mail: Morning (tomorrow 10:30 AM ET)"
    if mail_type == "morning":
        return "Next mail: Mid-day (today 2:00 PM Turkey)"
    if mail_type == "intraday":
        return "Next mail: Evening (today)"
    return "Next mail: Morning (tomorrow)"


def _time_label(mail_type: MailType, portfolio: Portfolio = "bist", lang: Lang = "tr") -> str:
    if portfolio == "usa":
        if mail_type == "morning":
            return "Morning · 10:30 AM ET"
        if mail_type == "intraday":
            return "Mid-day · 1:30 PM ET"
        return "Evening · 4:15 PM ET"

    if not _is_turkish(portfolio, lang):
        if mail_type == "intraday":
            return "Mid-day · 2:00 PM Turkey"
        is_summer = bool(datetime.now(ZoneInfo("Europe/Amsterdam")).dst())
        if mail_type == "morning":
            hour = "11:00" if is_summer else "12:00"
            return f"Morning · {hour} Turkey"
        hour = "18:45" if is_summer else "19:45"
        return f"Evening · {hour} Turkey"

    if mail_type == "intraday":
        return "Gün İçi · 14:00 Türkiye"
    is_summer = bool(datetime.now(ZoneInfo("Europe/Amsterdam")).dst())
    if mail_type == "morning":
        hour = "11:00" if is_summer else "12:00"
        return f"Sabah · {hour} Türkiye"
    hour = "18:45" if is_summer else "19:45"
    return f"Akşam · {hour} Türkiye"


def build_email_body(
    classifications: dict[str, dict],
    ta_result: dict,
    mail_type: MailType,
    client: anthropic.Anthropic,
    live_prices: dict[str, float | None],
    previous_snapshot: dict | None = None,
    portfolio: Portfolio = "bist",
    kap_by_ticker: dict[str, list[KapNewsItem]] | None = None,
    lang: Lang | None = None,
) -> str:
    if lang is None:
        lang = _mail_lang(portfolio)
    L = _labels(portfolio, lang)
    today = date.today()
    mail_label = _mail_label_text(portfolio, mail_type, lang)
    date_str = _format_date(today, portfolio, lang)

    lines: list[str] = [
        f"Subject: {L['subject']} — {date_str}, {mail_label}",
        "",
        _SEP,
        f"📊 {L['title']}",
        f"{date_str} — {_time_label(mail_type, portfolio, lang)}",
        _SEP,
    ]

    summary = _build_change_summary_localized(classifications, previous_snapshot, portfolio, lang)
    if summary:
        lines.append("")
        lines.extend(summary)

    kap_alert = _build_kap_alert_summary(kap_by_ticker, portfolio, lang)
    if kap_alert:
        lines.append("")
        lines.extend(kap_alert)

    groups = _group_by_market(classifications, portfolio)
    market_order = _market_order(portfolio)

    for market in market_order:
        tickers = groups.get(market, [])
        if not tickers:
            continue

        lines.append("")
        lines.append(_market_header(market, mail_type, portfolio, lang))
        lines.append("─" * 41)

        for ticker in tickers:
            classification = classifications.get(ticker)
            if not classification:
                continue
            try:
                lines.append(build_ticker_section(
                    ticker, classification, ta_result, client, mail_type, live_prices,
                    previous_snapshot, portfolio,
                    (kap_by_ticker or {}).get(ticker),
                    lang,
                ))
            except Exception as exc:
                logger.error("Failed to build section for %s: %s", ticker, exc)

    lines.append("")
    lines.append(_SEP)
    lines.append(_next_briefing(mail_type, portfolio, lang))

    return "\n".join(lines)


def save_briefing(
    body: str,
    mail_type: MailType,
    portfolio: Portfolio = "bist",
) -> Path:
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "-usa" if portfolio == "usa" else ""
    filename = f"{date.today().isoformat()}-{mail_type}{suffix}.txt"
    path = _REPORTS_DIR / filename
    path.write_text(body, encoding="utf-8")
    return path


def run_synthesizer(
    ta_result: dict,
    classifications: dict[str, dict],
    mail_type: MailType,
    live_prices: dict[str, float | None] | None = None,
    portfolio: Portfolio = "bist",
    kap_by_ticker: dict[str, list[KapNewsItem]] | None = None,
) -> Path:
    try:
        client = anthropic.Anthropic()
    except Exception:
        import os
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            logger.error("ANTHROPIC_API_KEY not set. Add it to your .env file.")
            sys.exit(1)
        client = anthropic.Anthropic(api_key=key)

    previous_snapshot = load_previous_snapshot(mail_type, portfolio)
    lang = _mail_lang(portfolio)
    logger.info("Building %s briefing (%s)...", portfolio.upper(), lang)

    body = build_email_body(
        classifications, ta_result, mail_type, client, live_prices or {},
        previous_snapshot, portfolio, kap_by_ticker, lang,
    )
    path = save_briefing(body, mail_type, portfolio)
    save_snapshot(classifications, mail_type, portfolio)
    logger.info("Briefing saved: %s", path)
    return path


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent / ".env")

    sys.path.insert(0, str(Path(__file__).parent))
    from data_fetcher import fetch_hourly_data, fetch_ticker_data
    from state_classifier import classify_all_tickers
    from ta_engine import run_ta_engine

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

    logger.info("─" * 50)
    logger.info("Briefing written to: %s", path)
    print(path.read_text(encoding="utf-8"))
