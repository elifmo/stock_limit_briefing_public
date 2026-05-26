# Stock Limit Briefing

A personal investment monitoring assistant that sends twice-daily email briefings with technical analysis for a manually selected list of stocks and crypto assets.

Runs automatically via GitHub Actions — no server needed.

---

## What it does

- Fetches OHLCV data for up to 10 tickers (stocks, ETFs, crypto) via yfinance
- Runs technical analysis on three timeframes: **1H**, **4H**, and **Daily**
- Detects: EMA momentum, RSI, ATR, Bollinger Bands, Support/Resistance, Breakouts, and **Stop Run Reversals** (AGPro method)
- Classifies each ticker into a market state: Strong Uptrend, Bullish Momentum, Early Breakout, Pullback Opportunity, Weakening Trend, Breakdown Risk
- Generates a human-readable narrative using Claude AI
- Sends the briefing by email — morning and evening

---

## Example output

```
📊 STOCK LIMIT BRIEFING
May 26, 2026 — Evening (19:35 Turkey)

🇹🇷 TÜRKİYE (Kapanış)
─────────────────────────────────────────

NVDA — 📈 Strong Uptrend  (High confidence)

📊 SHORT-TERM (1H):
✅ EMA5 > EMA20 = Momentum building
✅ EMA20 > EMA50 = Strong uptrend
...

📈 LONG-TERM (Daily):
✅ RSI 62 = Strong uptrend (room to go)
✅ EMA20 > EMA50 = Strong uptrend
...

🔑 KEY LEVELS:
  🟢 Support:    118.40
  🔴 Resistance: 135.20

🎯 STOP RUN REVERSAL (AGPro):
  4H:    ✅ BULLISH — swept 0.38 below support, closed above → Reversal signal
  Daily: ➖ No stop run detected

💭 WHAT THIS MEANS:
Uptrend structure intact across timeframes. RSI at 62 shows room
to run. 4H stop run detected — smart money defended support.

📋 BEFORE YOU ACT:
✅ Confirm volume picks up on the next move
✅ Watch support at $118.40 if sellers test it
❌ Don't trust the move without volume confirmation
```

---

## Mail schedule

| Mail | Time (Turkey) | Logic |
|---|---|---|
| Morning | 12:00 winter / 11:00 summer | After Netherlands + Turkey open + 1 hour |
| Evening | 19:35 winter / 18:35 summer | After Netherlands + Turkey close |

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/stock-limit-briefing.git
cd stock-limit-briefing
```

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Configure your tickers

```bash
cp config/tickers.yaml.example config/tickers.yaml
```

Then edit `config/tickers.yaml` with your own picks (this file stays private — it's in `.gitignore`):

```yaml
tickers:
  - AAPL
  - NVDA
  - ASML.AS      # Amsterdam exchange
  - TUPRS.IS     # Istanbul exchange
  - BTC-USD      # Crypto
  - GLD          # ETF
```

Any ticker supported by [yfinance](https://github.com/ranaroussi/yfinance) works.

### 4. Set up environment variables

Copy the example file and fill in your values:

```bash
cp .env.example .env
```

```
GMAIL_USER=you@gmail.com
GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
TO_EMAIL=you@gmail.com,partner@gmail.com
ANTHROPIC_API_KEY=sk-ant-...
```

**Gmail App Password:** Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) — you need 2-factor authentication enabled on your Google account.

**Anthropic API key:** Go to [console.anthropic.com](https://console.anthropic.com) — the Claude Haiku model used here is very cheap (a few cents per month).

### 5. Run manually

```bash
python src/main.py morning
python src/main.py evening
```

---

## Automated runs via GitHub Actions

The workflow in `.github/workflows/daily-briefing.yml` runs automatically twice a day.

### Add your secrets to GitHub

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic key |
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Your Gmail App Password |
| `TO_EMAIL` | Recipient addresses (comma-separated) |

That's it. Push to `main` and the workflow runs on schedule automatically.

### Trigger a run manually

Go to **Actions → Daily Briefing → Run workflow** and select `morning` or `evening`.

---

## Project structure

```
src/
  data_fetcher.py      # yfinance OHLCV fetching (1D, 1H, 4H)
  ta_engine.py         # Technical indicators + Stop Run detection
  state_classifier.py  # Market state classification
  ai_synthesizer.py    # Claude AI narrative + email assembly
  mail_sender.py       # Gmail SMTP sender
  main.py              # Entry point

config/
  tickers.yaml         # Your ticker list

.github/workflows/
  daily-briefing.yml   # Scheduled GitHub Actions workflow

.env.example           # Environment variable template
requirements.txt
```

---

## Indicators used

| Indicator | Timeframes |
|---|---|
| EMA 5 / 20 / 50 | 1H, 4H, Daily |
| RSI 14 | 4H, Daily |
| ATR 14 | 4H, Daily |
| Bollinger Bands (20, 2σ) | 1H, 4H, Daily |
| Support / Resistance | 1H, 4H, Daily |
| Breakout Detection | 1H, 4H, Daily |
| Stop Run Reversal (AGPro) | 4H, Daily |

---

## Disclaimer

This tool is a **signal detector**, not a trading advisor. It detects technical changes and explains them in plain language. All investment decisions are your own responsibility. No buy/sell recommendations. No price targets. No guarantees.
