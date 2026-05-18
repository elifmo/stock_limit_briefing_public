# Stock Limit Briefing — Project Context

## Purpose
Personal investment briefing system. Runs twice daily (08:00, 18:00)
to analyze chosen 5 stocks/ETFs with TA, interpret with Claude,
and send via Gmail.

## Tech Stack
- Python 3.11+
- yfinance (market data)
- pandas (TA calculations, pure — no extra libraries)
- anthropic SDK (Claude synthesis)
- smtplib (Gmail SMTP)
- GitHub Actions (cron automation)

## Architecture
```
Terminal CLI (python -m src.main --tickers NVDA,AAPL,...)
  ↓
data_fetcher.py (fetch data from yfinance)
  ↓
ta_engine.py (TA signals: golden cross, volume, breakout)
  ↓
portfolio.py (portfolio value, P&L)
  ↓
ai_synthesizer.py (send to Claude, get explanations)
  ↓
mail_sender.py (send via Gmail)
```

## File Structure
- src/
  - __init__.py (empty)
  - data_fetcher.py
  - ta_engine.py
  - portfolio.py
  - ai_synthesizer.py
  - mail_sender.py
  - main.py (CLI + orchestration)
- config/
  - ta_rules.yaml
- .env (secret keys)
- .github/workflows/daily-briefing.yml

## Code Standards
- Type hints on all functions
- Short but clear docstrings
- logging module (not print)
- Error handling: if one ticker fails, others continue
- Each module testable independently

## Running
```bash
# Local test (specify tickers manually)
python -m src.main --tickers "NVDA,AAPL,BTC-USD,GLD,ASML.AS"

# Cronjob (GitHub Actions, reads from env)
python -m src.main
```

## Config Files
- config/ta_rules.yaml: TA parameters (enabled/disabled signals)
- .env: API keys (secret, never pushed to repo)

## Next Steps
1. Phase 1: data_fetcher.py
2. Phase 2: ta_engine.py
3. Phase 3: portfolio.py
4. Phase 4: ai_synthesizer.py
5. Phase 5: mail_sender.py
6. Phase 6: main.py (CLI)
7. Phase 7: GitHub Actions
8. Phase 8: Test & Deploy
