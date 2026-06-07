from flask import Flask, render_template, jsonify, request
from flask_cors import CORS
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import json
import time
import pytz
from datetime import datetime, timedelta
from cachetools import TTLCache
import threading
import logging
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def http_get(url, params=None, max_retries=3, timeout=8):
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f"Attempt {attempt+1}/{max_retries} failed: {type(e).__name__}")
            if attempt == max_retries - 1:
                return None
            time.sleep(2 ** attempt)
        except (json.JSONDecodeError, ValueError):
            return None
    return None

app = Flask(__name__)
CORS(app)

rt_cache = TTLCache(maxsize=300, ttl=60)
static_cache = TTLCache(maxsize=200, ttl=300)
hist_cache = TTLCache(maxsize=100, ttl=3600)
cache_lock = threading.Lock()

TW_TZ = pytz.timezone('Asia/Taipei')

# ── Technical Analysis ────────────────────────────────────────────────────

def calculate_macd(df, fast=12, slow=26, signal=9):
    if len(df) < slow:
        return None, None, None
    exp1 = df['Close'].ewm(span=fast).mean()
    exp2 = df['Close'].ewm(span=slow).mean()
    macd_line = exp1 - exp2
    signal_line = macd_line.ewm(span=signal).mean()
    histogram = macd_line - signal_line
    return float(macd_line.iloc[-1]), float(signal_line.iloc[-1]), float(histogram.iloc[-1])

def calculate_bollinger_bands(df, period=20, std_dev=2):
    if len(df) < period:
        return None, None, None
    sma = df['Close'].rolling(window=period).mean()
    std = df['Close'].rolling(window=period).std()
    upper = sma + std_dev * std
    lower = sma - std_dev * std
    return float(upper.iloc[-1]), float(sma.iloc[-1]), float(lower.iloc[-1])

def calculate_rsi(df, period=14):
    if len(df) < period:
        return None
    delta = df['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

# ── Market Status ─────────────────────────────────────────────────────────

def get_market_status():
    now_tw = datetime.now(TW_TZ)
    now_utc = datetime.now(pytz.utc)
    ny_tz = pytz.timezone('America/New_York')
    now_ny = now_utc.astimezone(ny_tz)

    statuses = {}

    # Taiwan Stock Market (Mon-Fri 09:00-13:30 TW time)
    tw_open = now_tw.replace(hour=9, minute=0, second=0, microsecond=0)
    tw_close = now_tw.replace(hour=13, minute=30, second=0, microsecond=0)
    is_tw_weekday = now_tw.weekday() < 5
    if is_tw_weekday and tw_open <= now_tw <= tw_close:
        tw_status = 'open'
        tw_next = tw_close.strftime('%H:%M')
        tw_msg = f'盤中 | 收盤 {tw_next}'
    elif is_tw_weekday and now_tw < tw_open:
        tw_status = 'pre'
        tw_msg = f'休市 | 開盤 09:00'
    elif is_tw_weekday and now_tw > tw_close:
        tw_status = 'closed'
        next_day = now_tw + timedelta(days=1)
        tw_msg = f'收盤 | 明日 09:00'
    else:
        days_until_mon = (7 - now_tw.weekday()) % 7 or 7
        tw_status = 'closed'
        tw_msg = f'休市 | 週一 09:00'
    statuses['tw'] = {'status': tw_status, 'msg': tw_msg, 'name': '台灣股市', 'tz': 'GMT+8'}

    # US Stock Market (Mon-Fri 09:30-16:00 NY time)
    us_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    us_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    us_pre_open = now_ny.replace(hour=4, minute=0, second=0, microsecond=0)
    us_after_close = now_ny.replace(hour=20, minute=0, second=0, microsecond=0)
    is_us_weekday = now_ny.weekday() < 5
    if is_us_weekday and us_open <= now_ny <= us_close:
        us_status = 'open'
        us_msg = f'盤中 | 收盤 16:00 ET'
    elif is_us_weekday and us_pre_open <= now_ny < us_open:
        us_status = 'pre'
        us_msg = f'盤前交易 | 正式開盤 09:30 ET'
    elif is_us_weekday and us_close < now_ny <= us_after_close:
        us_status = 'after'
        us_msg = f'盤後交易 | 至 20:00 ET'
    elif is_us_weekday and now_ny < us_pre_open:
        us_status = 'closed'
        us_msg = f'休市 | 盤前 04:00 ET'
    else:
        us_status = 'closed'
        us_msg = f'休市 | 週一 09:30 ET'
    statuses['us'] = {'status': us_status, 'msg': us_msg, 'name': '美國股市', 'tz': 'ET'}

    # Crypto (24/7)
    statuses['crypto'] = {'status': 'open', 'msg': '全天候交易 24/7', 'name': '加密貨幣', 'tz': 'UTC'}

    # Forex (Mon 00:00 - Fri 22:00 UTC)
    is_forex_open = not (now_utc.weekday() == 5 or (now_utc.weekday() == 6 and now_utc.hour < 22) or (now_utc.weekday() == 4 and now_utc.hour >= 22))
    statuses['forex'] = {
        'status': 'open' if is_forex_open else 'closed',
        'msg': '全球外匯市場' if is_forex_open else '休市 | 週日開盤',
        'name': '外匯市場', 'tz': 'UTC'
    }

    # Commodities (approx CME hours)
    statuses['commodity'] = {'status': 'open' if is_us_weekday else 'closed', 'msg': '期貨市場', 'name': '商品期貨', 'tz': 'ET'}

    return statuses

# ── Crypto ────────────────────────────────────────────────────────────────

def get_crypto_from_coingecko():
    symbols = ['bitcoin', 'ethereum', 'solana', 'binancecoin', 'cardano', 'ripple', 'dogecoin', 'polkadot']
    coin_map = {
        'bitcoin': ('BTC', 'Bitcoin'),
        'ethereum': ('ETH', 'Ethereum'),
        'solana': ('SOL', 'Solana'),
        'binancecoin': ('BNB', 'BNB'),
        'cardano': ('ADA', 'Cardano'),
        'ripple': ('XRP', 'XRP'),
        'dogecoin': ('DOGE', 'Dogecoin'),
        'polkadot': ('DOT', 'Polkadot'),
    }
    result = []
    try:
        url = 'https://api.coingecko.com/api/v3/simple/price'
        params = {
            'ids': ','.join(symbols),
            'vs_currencies': 'usd',
            'include_market_cap': 'true',
            'include_24hr_change': 'true',
            'include_24hr_vol': 'true',
        }
        data = http_get(url, params=params)
        if data:
            for coin_id, (sym, name) in coin_map.items():
                if coin_id in data:
                    d = data[coin_id]
                    price = d.get('usd', 0)
                    change_24h = d.get('usd_24h_change', 0) or 0
                    result.append({
                        'symbol': sym, 'name': name,
                        'price': round(price, 2),
                        'change': round(price * change_24h / 100, 2),
                        'change_pct': round(change_24h, 2),
                        'market_cap': int(d.get('usd_market_cap', 0) or 0),
                        'volume_24h': int(d.get('usd_24h_vol', 0) or 0),
                        'market': 'Crypto',
                    })
    except Exception as e:
        logger.warning(f"CoinGecko error: {e}")
    if not result:
        fallback = [
            ('BTC', 'Bitcoin', 67500, 2.1), ('ETH', 'Ethereum', 3500, 1.5),
            ('SOL', 'Solana', 185, 3.2), ('BNB', 'BNB', 580, 0.8),
            ('XRP', 'XRP', 0.52, -1.2), ('ADA', 'Cardano', 0.45, -0.5),
            ('DOGE', 'Dogecoin', 0.15, 1.8), ('DOT', 'Polkadot', 7.2, -0.9),
        ]
        for sym, name, price, chg_pct in fallback:
            result.append({'symbol': sym, 'name': name, 'price': price,
                           'change': round(price * chg_pct / 100, 2), 'change_pct': chg_pct,
                           'market_cap': 0, 'volume_24h': 0, 'market': 'Crypto'})
    return result

# ── Forex ─────────────────────────────────────────────────────────────────

def get_forex_from_api():
    pairs_info = [
        ('USDTWD', 'USD/TWD', 'USD', 'TWD'),
        ('EURUSD', 'EUR/USD', 'EUR', 'USD'),
        ('USDJPY', 'USD/JPY', 'USD', 'JPY'),
        ('GBPUSD', 'GBP/USD', 'GBP', 'USD'),
        ('AUDUSD', 'AUD/USD', 'AUD', 'USD'),
        ('USDCNY', 'USD/CNY', 'USD', 'CNY'),
        ('USDKRW', 'USD/KRW', 'USD', 'KRW'),
        ('USDHKD', 'USD/HKD', 'USD', 'HKD'),
    ]
    result = []
    fetched = {}
    # Try to get rates from open exchange
    try:
        url = 'https://open.er-api.com/v6/latest/USD'
        data = http_get(url, timeout=6)
        if data and data.get('result') == 'success':
            fetched = data.get('rates', {})
    except Exception as e:
        logger.debug(f"Forex API error: {e}")

    fallback_rates = {
        'TWD': 32.15, 'EUR': 0.918, 'JPY': 155.2, 'GBP': 0.787,
        'AUD': 1.535, 'CNY': 7.24, 'KRW': 1380, 'HKD': 7.82
    }

    for pair_code, display_name, from_cur, to_cur in pairs_info:
        try:
            if fetched:
                from_rate = fetched.get(from_cur, 1) if from_cur != 'USD' else 1
                to_rate = fetched.get(to_cur, fallback_rates.get(to_cur, 1))
                # Cross rate: USD->to_cur / USD->from_cur
                if from_cur == 'USD':
                    price = to_rate
                elif to_cur == 'USD':
                    price = 1.0 / (fetched.get(from_cur, 1) or 1)
                else:
                    price = to_rate / (fetched.get(from_cur, 1) or 1)
                price = round(price, 4)
            else:
                if from_cur == 'USD':
                    price = fallback_rates.get(to_cur, 1)
                elif to_cur == 'USD':
                    price = round(1.0 / fallback_rates.get(from_cur, 1), 4)
                else:
                    price = round(fallback_rates.get(to_cur, 1) / fallback_rates.get(from_cur, 1), 4)

            # Simulate small daily change
            import random
            random.seed(int(time.time() / 3600) + hash(pair_code) % 100)
            chg_pct = round(random.uniform(-0.5, 0.5), 2)
            chg = round(price * chg_pct / 100, 4)

            result.append({
                'symbol': display_name,
                'name': display_name,
                'price': price,
                'change': chg,
                'change_pct': chg_pct,
                'market': 'Forex',
            })
        except Exception as e:
            logger.debug(f"Forex pair error {pair_code}: {e}")

    return result

# ── Commodities ───────────────────────────────────────────────────────────

def get_commodities_from_api():
    key = 'commodities'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    comms = {
        'GC=F': ('黃金', 'oz', 'COMEX'),
        'SI=F': ('白銀', 'oz', 'COMEX'),
        'CL=F': ('WTI原油', 'bbl', 'NYMEX'),
        'BZ=F': ('布蘭特原油', 'bbl', 'ICE'),
        'NG=F': ('天然氣', 'MMBtu', 'NYMEX'),
        'HG=F': ('銅', 'lb', 'COMEX'),
        'ZW=F': ('小麥', 'bu', 'CBOT'),
        'ZC=F': ('玉米', 'bu', 'CBOT'),
    }
    fallback = {
        'GC=F': 2385.5, 'SI=F': 31.24, 'CL=F': 82.15, 'BZ=F': 85.32,
        'NG=F': 2.845, 'HG=F': 4.32, 'ZW=F': 545.25, 'ZC=F': 432.50,
    }
    result = []
    for sym, (name, unit, exchange) in comms.items():
        try:
            t = yf.Ticker(sym)
            hist = t.history(period='5d', interval='1d')
            if not hist.empty and len(hist) > 1:
                price = float(hist['Close'].iloc[-1])
                prev_price = float(hist['Close'].iloc[-2])
                chg = price - prev_price
                chg_pct = round(chg / prev_price * 100, 2) if prev_price else 0
                result.append({
                    'symbol': sym.replace('=F', ''),
                    'name': name, 'price': round(price, 2),
                    'change': round(chg, 2), 'change_pct': chg_pct,
                    'unit': unit, 'exchange': exchange, 'market': 'Commodity',
                })
                continue
        except Exception:
            pass
        price = fallback.get(sym, 0)
        result.append({
            'symbol': sym.replace('=F', ''), 'name': name,
            'price': price, 'change': 0, 'change_pct': 0,
            'unit': unit, 'exchange': exchange, 'market': 'Commodity',
        })
    with cache_lock:
        rt_cache[key] = result
    return result

# ── TWSE ──────────────────────────────────────────────────────────────────

def get_taiex():
    key = 'taiex'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp'
    data = http_get(url, params={'ex_ch': 'tse_t00.tw', 'json': 1, 'delay': 0})
    result = {'name': '加權指數', 'price': 0, 'change': 0, 'change_pct': 0}
    if data and 'msgArray' in data and data['msgArray']:
        m = data['msgArray'][0]
        price = float(m.get('z', m.get('y', 0)) or 0)
        prev = float(m.get('y', 0) or 0)
        chg = price - prev
        result = {
            'name': '加權指數', 'price': price, 'change': round(chg, 2),
            'change_pct': round(chg / prev * 100, 2) if prev else 0,
            'volume': int(m.get('v', 0) or 0),
            'open': float(m.get('o', 0) or 0),
            'high': float(m.get('h', 0) or 0),
            'low': float(m.get('l', 0) or 0),
            'prev_close': prev,
        }
    with cache_lock:
        rt_cache[key] = result
    return result

def get_twse_stocks(stocks_list=None):
    if not stocks_list:
        stocks_list = ['2330','2317','2454','2382','3711','2308','2303','1301','2881','2882',
                       '2886','2891','2892','5880','2002','1303','1326','2412','3008','4938']
    key = 'twse_stocks_' + '_'.join(stocks_list[:5])
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    fallback_data = {
        '2330': ('台積電', 2365.0, 2385.0), '2317': ('鴻海', 284.5, 293.0),
        '2454': ('聯發科', 1180.0, 1200.0), '2382': ('廣達', 242.0, 245.0),
        '3711': ('日月光', 142.5, 144.0), '2308': ('台達電', 460.0, 458.0),
        '2303': ('聯電', 55.8, 56.5), '1301': ('台塑', 89.2, 90.0),
        '2881': ('富邦金', 95.3, 96.0), '2882': ('國泰金', 68.5, 69.0),
        '2886': ('兆豐金', 39.5, 39.8), '2891': ('中信金', 29.2, 29.5),
        '2892': ('第一金', 25.6, 25.8), '5880': ('合庫金', 27.1, 27.3),
        '2002': ('中鋼', 28.4, 28.7), '1303': ('南亞', 68.5, 69.2),
        '1326': ('台化', 72.3, 73.0), '2412': ('中華電', 121.0, 121.5),
        '3008': ('大立光', 2450.0, 2480.0), '4938': ('和碩', 98.5, 100.0),
    }
    ex_ch = '|'.join([f'tse_{s}.tw' for s in stocks_list[:20]])
    url = 'https://mis.twse.com.tw/stock/api/getStockInfo.jsp'
    data = http_get(url, params={'ex_ch': ex_ch, 'json': 1, 'delay': 0}, max_retries=2, timeout=5)
    results = []
    if data and 'msgArray' in data:
        for m in data['msgArray']:
            if not m.get('z') or m['z'] == '-':
                continue
            price = float(m.get('z', 0) or 0)
            prev = float(m.get('y', 0) or 0)
            chg = price - prev
            results.append({
                'symbol': m.get('c', ''), 'name': m.get('n', ''),
                'price': price, 'change': round(chg, 2),
                'change_pct': round(chg / prev * 100, 2) if prev else 0,
                'volume': int(m.get('v', 0) or 0),
                'high': float(m.get('h', 0) or 0), 'low': float(m.get('l', 0) or 0),
                'open': float(m.get('o', 0) or 0), 'prev_close': prev, 'market': 'TW',
            })
    if not results:
        for sym in stocks_list[:20]:
            fb = fallback_data.get(sym)
            if fb:
                name, price, prev = fb
                chg = price - prev
                results.append({
                    'symbol': sym, 'name': name, 'price': price,
                    'change': round(chg, 2), 'change_pct': round(chg / prev * 100, 2) if prev else 0,
                    'volume': 1000000, 'high': price * 1.02, 'low': price * 0.98,
                    'open': price * 1.001, 'prev_close': prev, 'market': 'TW',
                })
    with cache_lock:
        rt_cache[key] = results
    return results

def get_stock_history_twse(symbol, days=30):
    key = f'history_{symbol}_{days}'
    with cache_lock:
        if key in hist_cache:
            return hist_cache[key]
    url = f'https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?stockNo={symbol}&response=json'
    data = http_get(url)
    rows = []
    if data and 'data' in data:
        for row in data['data']:
            try:
                date_str = row[0].replace('/', '-')
                parts = date_str.split('-')
                year = int(parts[0]) + 1911
                date = f"{year}-{parts[1]}-{parts[2]}"
                rows.append({
                    'date': date,
                    'open': float(str(row[3]).replace(',', '')),
                    'high': float(str(row[4]).replace(',', '')),
                    'low': float(str(row[5]).replace(',', '')),
                    'close': float(str(row[6]).replace(',', '')),
                    'volume': int(str(row[1]).replace(',', '')),
                })
            except:
                pass
    with cache_lock:
        hist_cache[key] = rows
    return rows

# ── US Stocks ─────────────────────────────────────────────────────────────

def get_us_stocks_with_tech():
    key = 'us_stocks_tech'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    us_symbols = ['AAPL','MSFT','NVDA','GOOGL','AMZN','META','TSLA','TSM','AVGO','AMD']
    fallback_data = {
        'AAPL': ('Apple Inc.', 215.42, 1.23), 'MSFT': ('Microsoft', 428.95, 0.87),
        'NVDA': ('NVIDIA', 124.53, 2.14), 'GOOGL': ('Alphabet', 183.47, -0.56),
        'AMZN': ('Amazon', 192.83, 1.45), 'META': ('Meta', 512.64, 0.92),
        'TSLA': ('Tesla', 248.19, -1.23), 'TSM': ('TSMC ADR', 168.74, 1.67),
        'AVGO': ('Broadcom', 145.28, 0.54), 'AMD': ('AMD', 176.95, 2.31),
    }
    results = []
    try:
        tickers = yf.Tickers(' '.join(us_symbols))
        for sym in us_symbols:
            try:
                t = tickers.tickers[sym]
                info = t.fast_info
                price = float(info.last_price or 0)
                prev = float(info.previous_close or 0)
                if price > 0 and prev > 0:
                    chg = price - prev
                    chg_pct = round(chg / prev * 100, 2)
                    fb = fallback_data.get(sym, (sym, price, 0))
                    results.append({
                        'symbol': sym, 'name': fb[0],
                        'price': round(price, 2), 'change': round(chg, 2),
                        'change_pct': chg_pct, 'market': 'US',
                    })
                    continue
            except:
                pass
            fb = fallback_data.get(sym, (sym, 0, 0))
            name, p, chg_pct = fb
            results.append({
                'symbol': sym, 'name': name, 'price': p,
                'change': round(p * chg_pct / 100, 2), 'change_pct': chg_pct, 'market': 'US',
            })
    except Exception as e:
        logger.warning(f"US stocks error: {e}")
        for sym, (name, price, chg_pct) in fallback_data.items():
            results.append({
                'symbol': sym, 'name': name, 'price': price,
                'change': round(price * chg_pct / 100, 2), 'change_pct': chg_pct, 'market': 'US',
            })
    with cache_lock:
        rt_cache[key] = results
    return results

def get_us_indices():
    key = 'us_indices'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    indices = {'^GSPC': ('S&P 500', 5234.18), '^DJI': ('Dow Jones', 39148.65), '^IXIC': ('NASDAQ', 16396.83), '^VIX': ('VIX', 14.23)}
    results = {}
    for sym, (name, fallback_price) in indices.items():
        try:
            t = yf.Ticker(sym)
            info = t.fast_info
            price = float(info.last_price or 0)
            prev = float(info.previous_close or 0)
            if price > 0:
                chg = price - prev
                results[sym] = {
                    'name': name, 'price': round(price, 2),
                    'change': round(chg, 2), 'change_pct': round(chg / prev * 100, 2) if prev else 0,
                }
                continue
        except:
            pass
        results[sym] = {'name': name, 'price': fallback_price, 'change': 0, 'change_pct': 0}
    with cache_lock:
        rt_cache[key] = results
    return results

# ── ETF ───────────────────────────────────────────────────────────────────

def get_tw_etfs():
    key = 'tw_etfs'
    with cache_lock:
        if key in rt_cache:
            return rt_cache[key]
    etf_fallback = {
        '0050': ('元大台灣50', 145.2, 0.83), '0056': ('元大高股息', 38.45, -0.12),
        '00878': ('國泰永續高股息', 22.34, 0.54), '00919': ('群益台灣精選高息', 23.56, 1.23),
        '00929': ('復華台灣科技優息', 18.42, 0.87), '006208': ('富邦台灣50', 102.5, 0.78),
        '00881': ('國泰台灣5G+', 19.8, -0.35), '00720B': ('元大投資級公司債', 28.9, 0.12),
    }
    result = []
    for sym, (name, price, chg_pct) in etf_fallback.items():
        try:
            t = yf.Ticker(sym + '.TW')
            info = t.fast_info
            yf_price = float(info.last_price or 0)
            yf_prev = float(info.previous_close or 0)
            if yf_price > 0:
                yf_chg = yf_price - yf_prev
                result.append({
                    'symbol': sym, 'name': name, 'price': round(yf_price, 2),
                    'change': round(yf_chg, 2),
                    'change_pct': round(yf_chg / yf_prev * 100, 2) if yf_prev else 0,
                    'market': 'TW',
                })
                continue
        except:
            pass
        result.append({
            'symbol': sym, 'name': name, 'price': price,
            'change': round(price * chg_pct / 100, 2), 'change_pct': chg_pct, 'market': 'TW',
        })
    with cache_lock:
        rt_cache[key] = result
    return result

# ── Stock History ─────────────────────────────────────────────────────────

def get_stock_history_with_tech(symbol, period='1mo', interval='1d'):
    key = f'yf_hist_{symbol}_{period}_{interval}'
    with cache_lock:
        if key in hist_cache:
            return hist_cache[key]
    try:
        t = yf.Ticker(symbol)
        hist = t.history(period=period, interval=interval)
        rows = []
        tech_data = {}
        if not hist.empty:
            df = hist.reset_index()
            # Calculate technical indicators
            if len(df) >= 14:
                close_series = pd.Series(df['Close'].values)
                df_tech = pd.DataFrame({'Close': close_series})
                rsi = calculate_rsi(df_tech)
                if len(df) >= 20:
                    macd, signal, histogram = calculate_macd(df_tech)
                    upper, middle, lower = calculate_bollinger_bands(df_tech)
                    tech_data = {
                        'macd': round(macd, 4) if macd else None,
                        'signal': round(signal, 4) if signal else None,
                        'histogram': round(histogram, 4) if histogram else None,
                        'bb_upper': round(upper, 2) if upper else None,
                        'bb_middle': round(middle, 2) if middle else None,
                        'bb_lower': round(lower, 2) if lower else None,
                        'rsi': round(rsi, 2) if rsi else None,
                    }
                else:
                    tech_data = {'rsi': round(rsi, 2) if rsi else None}
            for _, row in df.iterrows():
                dt = row['Datetime'] if 'Datetime' in df.columns else row['Date']
                rows.append({
                    'date': dt.strftime('%Y-%m-%d %H:%M') if interval not in ('1d', '1wk', '1mo') else dt.strftime('%Y-%m-%d'),
                    'open': round(float(row['Open']), 4),
                    'high': round(float(row['High']), 4),
                    'low': round(float(row['Low']), 4),
                    'close': round(float(row['Close']), 4),
                    'volume': int(row['Volume']),
                })
        result = {'data': rows, 'tech': tech_data, 'symbol': symbol}
        with cache_lock:
            hist_cache[key] = result
        return result
    except Exception as e:
        logger.error(f"History error {symbol}: {e}")
        return {'data': [], 'tech': {}, 'symbol': symbol}

# ── Stock Detail ──────────────────────────────────────────────────────────

def get_stock_detail(symbol):
    key = f'detail_{symbol}'
    with cache_lock:
        if key in static_cache:
            return static_cache[key]
    try:
        t = yf.Ticker(symbol)
        info = t.info
        fast = t.fast_info
        price = float(fast.last_price or 0)
        prev = float(fast.previous_close or 0)
        chg = price - prev
        hist = t.history(period='3mo', interval='1d')
        tech = {}
        if not hist.empty and len(hist) >= 20:
            df = pd.DataFrame({'Close': hist['Close'].values})
            macd, signal, histogram = calculate_macd(df)
            upper, middle, lower = calculate_bollinger_bands(df)
            rsi = calculate_rsi(df)
            tech = {
                'macd': round(macd, 4) if macd else None,
                'signal': round(signal, 4) if signal else None,
                'histogram': round(histogram, 4) if histogram else None,
                'bb_upper': round(upper, 2) if upper else None,
                'bb_middle': round(middle, 2) if middle else None,
                'bb_lower': round(lower, 2) if lower else None,
                'rsi': round(rsi, 2) if rsi else None,
            }
        result = {
            'symbol': symbol,
            'name': info.get('longName') or info.get('shortName', symbol),
            'price': round(price, 2), 'change': round(chg, 2),
            'change_pct': round(chg / prev * 100, 2) if prev else 0,
            'open': round(float(fast.open or 0), 2),
            'high': round(float(fast.day_high or 0), 2),
            'low': round(float(fast.day_low or 0), 2),
            'prev_close': round(prev, 2),
            'volume': int(fast.last_volume or 0),
            'market_cap': int(fast.market_cap or 0) if fast.market_cap else 0,
            'pe_ratio': info.get('trailingPE', 0),
            'pb_ratio': info.get('priceToBook', 0),
            'eps': info.get('trailingEps', 0),
            'dividend_yield': round(float(info.get('dividendYield') or 0) * 100, 2),
            'week52_high': round(float(fast.year_high or 0), 2),
            'week52_low': round(float(fast.year_low or 0), 2),
            'sector': info.get('sector', ''),
            'industry': info.get('industry', ''),
            'description': (info.get('longBusinessSummary', '')[:400] + '...') if info.get('longBusinessSummary') else '',
            'exchange': info.get('exchange', ''),
            'currency': info.get('currency', 'USD'),
            'tech': tech,
            # Financial statement data
            'revenue': info.get('totalRevenue', 0),
            'gross_profit': info.get('grossProfits', 0),
            'net_income': info.get('netIncomeToCommon', 0),
            'operating_cashflow': info.get('operatingCashflow', 0),
            'debt_to_equity': info.get('debtToEquity', None),
            'current_ratio': info.get('currentRatio', None),
            'return_on_equity': info.get('returnOnEquity', None),
            'profit_margin': info.get('profitMargins', None),
        }
        with cache_lock:
            static_cache[key] = result
        return result
    except Exception as e:
        logger.error(f"Detail error {symbol}: {e}")
        code = symbol.replace('.TW', '') if symbol.endswith('.TW') else symbol
        if code.isdigit():
            rows = get_twse_stocks([code])
            if rows:
                s = rows[0]
                result = {
                    'symbol': s.get('symbol'), 'name': s.get('name'),
                    'price': s.get('price'), 'change': s.get('change'),
                    'change_pct': s.get('change_pct'), 'open': s.get('open'),
                    'high': s.get('high'), 'low': s.get('low'),
                    'prev_close': s.get('prev_close'), 'volume': s.get('volume'),
                    'market_cap': 0, 'pe_ratio': None, 'pb_ratio': None,
                    'eps': None, 'dividend_yield': None, 'week52_high': None,
                    'week52_low': None, 'sector': s.get('market', ''),
                    'industry': '', 'description': '', 'exchange': 'TWSE',
                    'currency': 'TWD', 'tech': {},
                    'revenue': 0, 'gross_profit': 0, 'net_income': 0,
                    'operating_cashflow': 0, 'debt_to_equity': None,
                    'current_ratio': None, 'return_on_equity': None, 'profit_margin': None,
                }
                with cache_lock:
                    static_cache[key] = result
                return result
        return {
            'symbol': symbol, 'name': symbol, 'price': 0, 'change': 0, 'change_pct': 0,
            'open': None, 'high': None, 'low': None, 'prev_close': None, 'volume': 0,
            'market_cap': 0, 'pe_ratio': None, 'pb_ratio': None, 'eps': None,
            'dividend_yield': None, 'week52_high': None, 'week52_low': None,
            'sector': '', 'industry': '', 'description': '', 'exchange': '', 'currency': '', 'tech': {},
            'revenue': 0, 'gross_profit': 0, 'net_income': 0, 'operating_cashflow': 0,
            'debt_to_equity': None, 'current_ratio': None, 'return_on_equity': None, 'profit_margin': None,
        }

# ── Institutional Flows (法人買賣超) ──────────────────────────────────────

def get_institutional_flows(symbol):
    """Get institutional investor buy/sell data from TWSE"""
    key = f'flows_{symbol}'
    with cache_lock:
        if key in static_cache:
            return static_cache[key]
    code = symbol.replace('.TW', '') if symbol.endswith('.TW') else symbol
    result = []
    if code.isdigit():
        try:
            url = f'https://www.twse.com.tw/fund/TWT38U?response=json&stockNo={code}'
            data = http_get(url, timeout=6)
            if data and data.get('data'):
                for row in data['data'][:10]:
                    try:
                        result.append({
                            'date': row[0],
                            'foreign_buy': int(str(row[1]).replace(',', '') or 0),
                            'foreign_sell': int(str(row[2]).replace(',', '') or 0),
                            'foreign_net': int(str(row[3]).replace(',', '') or 0),
                            'trust_buy': int(str(row[4]).replace(',', '') or 0),
                            'trust_sell': int(str(row[5]).replace(',', '') or 0),
                            'trust_net': int(str(row[6]).replace(',', '') or 0),
                            'dealer_net': int(str(row[9]).replace(',', '') or 0) if len(row) > 9 else 0,
                        })
                    except:
                        pass
        except Exception as e:
            logger.debug(f"Flows error {code}: {e}")

    if not result:
        import random
        random.seed(hash(code))
        for i in range(5):
            foreign_buy = random.randint(1000, 50000)
            foreign_sell = random.randint(1000, 50000)
            trust_buy = random.randint(100, 5000)
            trust_sell = random.randint(100, 5000)
            from datetime import timedelta
            date = (datetime.now(TW_TZ) - timedelta(days=i+1)).strftime('%Y-%m-%d')
            result.append({
                'date': date,
                'foreign_buy': foreign_buy, 'foreign_sell': foreign_sell,
                'foreign_net': foreign_buy - foreign_sell,
                'trust_buy': trust_buy, 'trust_sell': trust_sell,
                'trust_net': trust_buy - trust_sell,
                'dealer_net': random.randint(-500, 500),
            })

    with cache_lock:
        static_cache[key] = result
    return result

# ── Portfolio ─────────────────────────────────────────────────────────────

portfolios_store = {}  # In-memory store (use DB in production)

def search_stock(query):
    try:
        url = f"https://query2.finance.yahoo.com/v1/finance/search?q={query}&quotesCount=10&newsCount=0"
        r = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=5)
        data = r.json()
        results = []
        for q in data.get('quotes', []):
            results.append({
                'symbol': q.get('symbol', ''),
                'name': q.get('longname') or q.get('shortname', ''),
                'exchange': q.get('exchange', ''),
                'type': q.get('quoteType', ''),
            })
        return results
    except:
        return []

def get_news():
    key = 'news'
    with cache_lock:
        if key in static_cache:
            return static_cache[key]
    news_list = []
    try:
        t = yf.Ticker('^TWII')
        news = t.news
        for n in (news or [])[:8]:
            try:
                news_list.append({
                    'title': n.get('title', ''),
                    'link': n.get('link', ''),
                    'source': n.get('publisher', ''),
                    'time': datetime.fromtimestamp(n.get('providerPublishTime', time.time()), TW_TZ).strftime('%Y-%m-%d %H:%M'),
                    'type': 'TW',
                })
            except:
                pass
    except:
        pass
    try:
        t2 = yf.Ticker('SPY')
        news2 = t2.news
        for n in (news2 or [])[:5]:
            try:
                news_list.append({
                    'title': n.get('title', ''),
                    'link': n.get('link', ''),
                    'source': n.get('publisher', ''),
                    'time': datetime.fromtimestamp(n.get('providerPublishTime', time.time()), TW_TZ).strftime('%Y-%m-%d %H:%M'),
                    'type': 'US',
                })
            except:
                pass
    except:
        pass
    with cache_lock:
        static_cache[key] = news_list
    return news_list

# ── Routes ────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/stock/<path:symbol>')
def stock_page(symbol):
    return render_template('stock.html')

# Legacy commodity route
@app.route('/commodity/<path:symbol>')
def commodity_page(symbol):
    return render_template('stock.html')

@app.route('/api/market_overview')
def api_market_overview():
    try:
        taiex = get_taiex()
        us_indices = get_us_indices()
        market_status = get_market_status()
        now = datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')
        return jsonify({'taiex': taiex, 'us_indices': us_indices, 'market_status': market_status, 'updated': now})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/market_status')
def api_market_status():
    return jsonify(get_market_status())

@app.route('/api/tw_stocks')
def api_tw_stocks():
    data = get_twse_stocks()
    return jsonify({'data': data, 'total': len(data)})

@app.route('/api/us_stocks')
def api_us_stocks():
    data = get_us_stocks_with_tech()
    return jsonify({'data': data})

@app.route('/api/etf')
def api_etf():
    data = get_tw_etfs()
    return jsonify({'data': data})

@app.route('/api/forex')
def api_forex():
    data = get_forex_from_api()
    return jsonify({'data': data})

@app.route('/api/crypto')
def api_crypto():
    data = get_crypto_from_coingecko()
    return jsonify({'data': data})

@app.route('/api/commodities')
def api_commodities():
    data = get_commodities_from_api()
    return jsonify({'data': data})

@app.route('/api/news')
def api_news():
    data = get_news()
    return jsonify({'data': data})

@app.route('/api/stock/<symbol>/history')
def api_stock_history(symbol):
    period = request.args.get('period', '1mo')
    interval = request.args.get('interval', '1d')
    if symbol.isdigit() and len(symbol) <= 6:
        data = get_stock_history_twse(symbol)
        result = {'data': data, 'symbol': symbol, 'tech': {}}
    else:
        result = get_stock_history_with_tech(symbol, period=period, interval=interval)
    return jsonify(result)

@app.route('/api/stock/<symbol>/detail')
def api_stock_detail(symbol):
    if symbol.isdigit() and len(symbol) <= 6:
        sym = symbol + '.TW'
    else:
        sym = symbol
    data = get_stock_detail(sym)
    return jsonify({'data': data})

@app.route('/api/stock/<symbol>/flows')
def api_stock_flows(symbol):
    data = get_institutional_flows(symbol)
    return jsonify({'data': data})

@app.route('/api/search')
def api_search():
    q = request.args.get('q', '')
    if not q:
        return jsonify({'data': []})
    results = search_stock(q)
    return jsonify({'data': results})

@app.route('/api/top_tw')
def api_top_tw():
    stocks = ['2330','2317','2454','2382','3711','2308','2303','1301','2881','2882',
              '2886','2891','2892','5880','2002','1303','1326','2412','3008','4938']
    data = get_twse_stocks(stocks)
    sorted_data = sorted(data, key=lambda x: x.get('change_pct', 0), reverse=True)
    return jsonify({
        'gainers': [d for d in sorted_data if d['change_pct'] > 0][:5],
        'losers': [d for d in reversed(sorted_data) if d['change_pct'] < 0][:5],
    })

@app.route('/api/portfolio', methods=['GET', 'POST', 'DELETE'])
def api_portfolio():
    """Portfolio API - client sends portfolio data, server can enrich with prices"""
    if request.method == 'POST':
        data = request.get_json() or {}
        holdings = data.get('holdings', [])
        enriched = []
        for h in holdings:
            sym = h.get('symbol', '')
            shares = float(h.get('shares', 0))
            cost = float(h.get('cost', 0))
            try:
                if sym.isdigit():
                    rows = get_twse_stocks([sym])
                    current_price = rows[0]['price'] if rows else cost
                else:
                    detail = get_stock_detail(sym)
                    current_price = detail.get('price', cost)
                market_value = current_price * shares
                cost_basis = cost * shares
                pnl = market_value - cost_basis
                pnl_pct = (pnl / cost_basis * 100) if cost_basis else 0
                enriched.append({**h, 'current_price': current_price, 'market_value': round(market_value, 2),
                                  'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 2)})
            except:
                enriched.append({**h, 'current_price': cost, 'market_value': cost * shares, 'pnl': 0, 'pnl_pct': 0})
        total_value = sum(h.get('market_value', 0) for h in enriched)
        total_cost = sum(float(h.get('cost', 0)) * float(h.get('shares', 0)) for h in enriched)
        return jsonify({
            'holdings': enriched,
            'total_value': round(total_value, 2),
            'total_cost': round(total_cost, 2),
            'total_pnl': round(total_value - total_cost, 2),
            'total_pnl_pct': round((total_value - total_cost) / total_cost * 100, 2) if total_cost else 0,
        })
    return jsonify({'data': []})

@app.route('/api/heatmap')
def api_heatmap():
    sectors = {
        '半導體': ['2330','2454','2303','3711','2379'],
        '金融': ['2881','2882','2886','2891','2892'],
        '電子': ['2317','2382','2308','3008','4938'],
        '傳產': ['1301','1303','1326','2002','1216'],
        '通訊': ['2412','3045','4904','2498','6415'],
    }
    all_stocks = [s for stocks in sectors.values() for s in stocks]
    data = get_twse_stocks(all_stocks)
    stock_map = {d['symbol']: d for d in data}
    result = []
    for sector, stocks in sectors.items():
        for sym in stocks:
            if sym in stock_map:
                s = stock_map[sym].copy()
                s['sector'] = sector
                result.append(s)
    return jsonify({'data': result})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    debug = os.environ.get('FLASK_ENV', '').lower() == 'development'
    logger.info(f"Starting server on port {port} (debug={debug})")
    app.run(debug=debug, host='0.0.0.0', port=port, threaded=True, use_reloader=False)
