# AI Agent Instructions for HARRY

## Project overview
- Single-service Python Flask app in `app.py`.
- Provides a financial dashboard with market overview and asset details.
- Uses `yfinance`, `requests`, `pandas`, `numpy`, `pytz`, and caching via `cachetools`.
- Serves HTML templates from `index.html` and `commodity.html`.

## Key behavior
- Primary application entrypoint is `app.py`.
- Routes expose JSON APIs under `/api/*` and server-side pages under `/` and `/stock/<symbol>`.
- Cached data is maintained in memory via `TTLCache` and protected by `threading.Lock`.
- The app provides fallback data when external APIs fail.
- The app supports both TWSE and US markets plus crypto, forex, commodities, and news.

## Development and run commands
- Install dependencies: `pip install -r requirements.txt`
- Run locally: `python app.py`
- Production server: `gunicorn app:app --bind 0.0.0.0:$PORT`
- Environment variables:
  - `PORT` (default `5001`)
  - `FLASK_ENV=development` enables debug mode in the current app.

## What agents should do first
- Prefer modifying `app.py` for backend logic.
- Keep API contract stable for routes used by the frontend.
- Preserve caching semantics and fallback behavior when changing data-fetching logic.
- Avoid unnecessary refactors in HTML templates unless fixing specific UI issues.

## Useful patterns
- Use `http_get(...)` for HTTP requests with retry/timing handling.
- Add new endpoint logic under route functions and keep response JSON shape consistent.
- Use `get_*` helper functions for market-specific data and `get_market_status()` for trading hours logic.
- Preserve timezone-aware logic for TW/TWSE and NY/US market hours.

## Notes for future agents
- There is no existing `README.md` or documentation file in this repo.
- No test suite is present; validate changes by running the app and exercising relevant endpoints.
- Keep the repository lightweight: do not introduce heavy frameworks unless required.
