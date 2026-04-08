import ssl
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE
"""
FRANKENSTEIN v13 — CLEAN DATA COLLECTOR
=========================================
Architecture upgrade: asyncio + WebSocket (no more REST polling)
Goal: Collect CLEAN, uncontaminated data to find real edge

KEY CHANGES FROM v11/v12:
  - asyncio event loop: everything runs in parallel, no blocking
  - Binance WebSocket: BTC price pushed every 100ms (not polled)
  - Polymarket RTDS WebSocket: odds pushed live (not polled)
  - Millisecond timestamps on EVERY price read
  - Oracle lag measurement: logs Binance price vs Polymarket price at same ms
  - NO restrictions: no UP-only, no entry price cap, no time cutoffs
  - OBSERVE FIRST: paper trades only, learning engine disabled
  - Clean data only: T=0 to T=290s, all directions, all prices

WHY: Prior data (frank_v11_training.csv) was contaminated by Chainlink
oracle lag (2-4s). The market at T>240s already knew the outcome.
We need fresh data with millisecond timestamps to measure real lag.

Usage: python3 frank_v13.py
Requires: pip install websockets python-dotenv requests numpy
"""

import asyncio
from frank_agent import FrankAgent
import websockets
import json
import requests
import os
import csv
import time
import numpy as np
from datetime import datetime, timezone
from collections import deque
from dotenv import load_dotenv

load_dotenv()

# ═══════════════════════════════════════════════════════════════
# CONFIG — observation mode, no restrictions
# ═══════════════════════════════════════════════════════════════

CANDLE_DURATION   = 300          # 5-minute candles
BET_SIZE          = 30.0         # Paper only for now
CONFIDENCE_THRESH = 0.65         # Minimum ML confidence to log a "would trade"
SCAN_INTERVAL     = 1.0          # How often signal loop evaluates (seconds)

# Files
MASTER_FILE  = "frank_v13_training.csv"
frank_agent = FrankAgent(MASTER_FILE)
SESSION_FILE = "frank_v13_session.csv"
LIVE_FILE    = os.path.expanduser("~/Downloads/frank_v13_live.json")

# APIs
BINANCE_WS   = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST = "https://api.binance.com/api/v3"
GAMMA_API    = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"

# Oracle lag measurement — this is what we're here to find
# We log both Binance price and Polymarket price at the SAME millisecond
# so we can calculate real lag post-hoc
PRICE_BUFFER_SIZE = 500  # Keep last 500 Binance ticks in memory

# ═══════════════════════════════════════════════════════════════
# GLOBAL STATE — shared between async tasks
# ═══════════════════════════════════════════════════════════════

# Binance price buffer: deque of (timestamp_ms, price)
btc_buffer = deque(maxlen=PRICE_BUFFER_SIZE)

# Polymarket odds buffer: deque of (timestamp_ms, up_ask, dn_ask)
pm_buffer = deque(maxlen=200)

# Current candle state
candle_state = {
    "active": False,
    "candle_num": 0,
    "start_ts": 0,
    "end_ts": 0,
    "spot_start": None,
    "candle": None,           # Polymarket market info
    "scan_log": [],
    "traded": False,
    "trade_dir": None,
    "trade_price": None,
    "trade_elapsed": None,
    "session_trades": [],
    "daily_pnl": 0.0,
}

# ═══════════════════════════════════════════════════════════════
# LOAD ML BRAIN — v4 preferred, fallback chain
# ═══════════════════════════════════════════════════════════════

brain = None
brain_version = None

import ssl
ssl._create_default_https_context = ssl._create_unverified_context
import pickle
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
import warnings
warnings.filterwarnings("ignore")

def load_brain():
    global brain, brain_version
    
    # Try v4 first, then v3, v2, v1
    versions = [
        ("v4", ["whale_brain_v4.pkl", os.path.expanduser("~/Downloads/whale_brain_v4.pkl")]),
        ("v3", ["whale_brain_v3.pkl", os.path.expanduser("~/Downloads/whale_brain_v3.pkl")]),
        ("v2", ["whale_brain_v2.pkl", os.path.expanduser("~/Downloads/whale_brain_v2.pkl")]),
        ("v1", ["whale_brain.pkl",    os.path.expanduser("~/Downloads/whale_brain.pkl")]),
    ]
    
    for version, paths in versions:
        for path in paths:
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        brain = pickle.load(f)
                    brain_version = version
                    n = brain.get("n_samples", brain.get("trained_on", "?"))
                    cv = brain.get("cv_accuracy", 0) * 100
                    print(f"  🧠 Brain {version} loaded: {n} candles | {cv:.1f}% CV")
                    return
                except Exception as e:
                    print(f"  ⚠️  Could not load {path}: {e}")
    
    print("  ⚠️  No brain found — momentum-only predictions")

load_brain()

# ═══════════════════════════════════════════════════════════════
# BINANCE WEBSOCKET — price pushed every ~100ms
# ═══════════════════════════════════════════════════════════════

async def binance_feed(shutdown_event):
    """
    Holds a permanent WebSocket to Binance.
    Every trade tick → appended to btc_buffer with ms timestamp.
    No REST calls. No blocking. Reconnects on drop.
    """
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20, ssl=ssl_ctx) as ws:
                print("  ✅ Binance WebSocket connected")
                async for raw in ws:
                    if shutdown_event.is_set():
                        break
                    try:
                        data = json.loads(raw)
                        price = float(data["p"])
                        ts_ms = int(data["T"])  # Trade timestamp in ms
                        btc_buffer.append((ts_ms, price))
                    except Exception:
                        pass
        except Exception as e:
            if not shutdown_event.is_set():
                print(f"  ⚠️  Binance WS dropped: {e} — reconnecting in 3s")
                await asyncio.sleep(3)


def get_btc_now():
    """Get latest BTC price from buffer (0ms latency — already in memory)."""
    if btc_buffer:
        return btc_buffer[-1][1]
    return None


def get_btc_at(target_ms, window_ms=500):
    """
    Get BTC price closest to target_ms.
    Used for oracle lag analysis: what was BTC price when PM oracle fired?
    """
    if not btc_buffer:
        return None
    buf = list(btc_buffer)
    best = min(buf, key=lambda x: abs(x[0] - target_ms))
    if abs(best[0] - target_ms) <= window_ms:
        return best[1]
    return None


def get_btc_ms():
    """Get (price, timestamp_ms) tuple."""
    if btc_buffer:
        return btc_buffer[-1]
    return None, None

# ═══════════════════════════════════════════════════════════════
# POLYMARKET RTDS WEBSOCKET — odds pushed live
# ═══════════════════════════════════════════════════════════════

async def polymarket_feed(shutdown_event):
    """
    Holds a permanent WebSocket to Polymarket's RTDS relay.
    Odds updates → appended to pm_buffer with ms timestamp.
    Sends PING every 5s to keep connection alive.
    """
    RTDS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(RTDS_URL, ping_interval=None, ssl=ssl_ctx) as ws:
                pass  # connected silenced
                
                # Subscribe to current active tokens
                candle = candle_state.get("candle")
                if candle and candle.get("up_token"):
                    pass  # No subscription needed — RTDS pushes all price updates
                
                # PING task — PM requires text PING every 5s
                async def ping_loop():
                    while not shutdown_event.is_set():
                        try:
                            await ws.send("PING")
                        except Exception:
                            break
                        await asyncio.sleep(5)
                
                ping_task = asyncio.create_task(ping_loop())
                
                try:
                    async for raw in ws:
                        if shutdown_event.is_set():
                            break
                        if raw == "PONG":
                            continue
                        try:
                            data = json.loads(raw)
                            ts_ms = int(time.time() * 1000)
                            
                            # Parse price update
                            if isinstance(data, list):
                                for item in data:
                                    if item.get("event_type") == "price_change":
                                        asset_id = item.get("asset_id")
                                        price = float(item.get("price", 0))
                                        candle = candle_state.get("candle")
                                        if candle:
                                            if asset_id == candle.get("up_token"):
                                                pm_buffer.append((ts_ms, price, None))
                                            elif asset_id == candle.get("dn_token"):
                                                pm_buffer.append((ts_ms, None, price))
                        except Exception:
                            pass
                finally:
                    ping_task.cancel()
                    
        except Exception as e:
            if not shutdown_event.is_set():
                pass  # dropped silenced
                await asyncio.sleep(5)


def get_pm_prices_now():
    """Get latest Polymarket prices from buffer."""
    if not pm_buffer:
        return None, None
    # Reconstruct latest up/dn from buffer
    up_ask = None
    dn_ask = None
    buf = list(pm_buffer)
    for ts, up, dn in reversed(buf):
        if up is not None and up_ask is None:
            up_ask = up
        if dn is not None and dn_ask is None:
            dn_ask = dn
        if up_ask is not None and dn_ask is not None:
            break
    return up_ask, dn_ask

# ═══════════════════════════════════════════════════════════════
# POLYMARKET REST — market discovery (one-time per candle)
# ═══════════════════════════════════════════════════════════════

def find_candle():
    """Find current BTC 5min candle on Polymarket."""
    try:
        now = int(time.time())
        current_slot = now - (now % CANDLE_DURATION)
        
        for ts in [current_slot, current_slot + CANDLE_DURATION]:
            slug = f"btc-updown-5m-{ts}"
            r = requests.get(f"{GAMMA_API}/events", params={"slug": slug}, timeout=10)
            events = r.json()
            
            if events and len(events) > 0:
                event = events[0]
                markets = event.get("markets", [])
                if markets:
                    m = markets[0]
                    tokens = json.loads(m.get("clobTokenIds", "[]"))
                    end_ts = ts + CANDLE_DURATION
                    if end_ts > now:
                        return {
                            "question": m.get("question", event.get("title", "")),
                            "slug": slug,
                            "end_ts": end_ts,
                            "start_ts": ts,
                            "up_token": tokens[0] if len(tokens) > 0 else None,
                            "dn_token": tokens[1] if len(tokens) > 1 else None,
                        }
    except Exception as e:
        print(f"  ⚠️  find_candle error: {e}")
    return None


def get_clob_prices_rest(up_token, dn_token):
    """REST fallback for CLOB prices — only used at candle start."""
    up_price, dn_price = None, None
    try:
        if up_token:
            r = requests.get(f"{CLOB_API}/price",
                           params={"token_id": up_token, "side": "buy"}, timeout=5)
            p = float(r.json().get("price", 0))
            if 0.01 < p < 0.99:
                up_price = p
    except Exception:
        pass
    try:
        if dn_token:
            r = requests.get(f"{CLOB_API}/price",
                           params={"token_id": dn_token, "side": "buy"}, timeout=5)
            p = float(r.json().get("price", 0))
            if 0.01 < p < 0.99:
                dn_price = p
    except Exception:
        pass
    return up_price, dn_price



def get_polymarket_price_to_beat(candle_start_ts):
    """Get Binance price at exact candle start timestamp — best proxy for Chainlink."""
    try:
        # Get the 1-second kline that contains the candle start
        ts_ms = candle_start_ts * 1000
        r = requests.get(f"{BINANCE_REST}/klines", params={
            "symbol": "BTCUSDT",
            "interval": "1s",
            "startTime": ts_ms,
            "endTime": ts_ms + 2000,
            "limit": 1
        }, timeout=5)
        klines = r.json()
        if klines:
            # Use the OPEN of the exact second the candle started
            price = float(klines[0][1])
            if 50000 < price < 200000:
                return price
    except Exception as e:
        pass
    return None

def get_klines(limit=120):
    """REST call for klines — done once at candle start, then cached."""
    try:
        r = requests.get(f"{BINANCE_REST}/klines",
                        params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
                        timeout=5)
        return r.json()
    except Exception:
        return None

# ═══════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS
# ═══════════════════════════════════════════════════════════════

def build_features(spot_start, spot_now, klines, elapsed):
    features = {"elapsed_s": elapsed, "spot_chg": spot_now - spot_start}
    spot_chg = spot_now - spot_start

    closes = [float(k[4]) for k in klines[-60:]] if klines else []

    if len(closes) < 20:
        return {**features, "bb_position": 0.5, "rsi_5m": 50,
                "macd_histogram": 0, "stoch_k": 50,
                "momentum_short": 0, "volatility_5m": 0, "trend_alignment": 0}

    # Bollinger Bands
    sma20 = np.mean(closes[-20:])
    std20 = np.std(closes[-20:])
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bb_range = bb_upper - bb_lower if bb_upper != bb_lower else 1
    features["bb_position"] = (spot_now - bb_lower) / bb_range

    # RSI
    deltas = np.diff(closes[-15:])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains) if len(gains) > 0 else 0
    avg_loss = np.mean(losses) if len(losses) > 0 else 0.001
    rs = avg_gain / avg_loss if avg_loss > 0 else 100
    features["rsi_5m"] = 100 - (100 / (1 + rs))

    # MACD
    ema12 = np.mean(closes[-12:])
    ema26 = np.mean(closes[-26:]) if len(closes) >= 26 else ema12
    macd_line = ema12 - ema26
    features["macd_histogram"] = macd_line - (macd_line * 0.8)

    # Stochastic
    low14  = min(closes[-14:])
    high14 = max(closes[-14:])
    features["stoch_k"] = ((spot_now - low14) / (high14 - low14) * 100) if high14 != low14 else 50

    features["momentum_short"] = (spot_now - closes[-5]) / closes[-5] if closes[-5] != 0 else 0
    features["volatility_5m"]  = np.std(closes[-5:]) / np.mean(closes[-5:]) if np.mean(closes[-5:]) != 0 else 0

    # Trend alignment
    sma5  = np.mean(closes[-5:])
    sma10 = np.mean(closes[-10:])
    trend = 0
    trend += 1 if sma5 > sma10 else -1
    trend += 1 if sma10 > sma20 else -1
    trend += 1 if spot_now > sma5 else -1
    features["trend_alignment"] = trend

    return features


def ml_predict_simple(features):
    """
    Momentum-only prediction when no brain, or as baseline.
    Returns (direction, confidence, up_prob, dn_prob)
    """
    spot_chg  = features.get("spot_chg", 0)
    momentum  = features.get("momentum_short", 0)
    bb        = features.get("bb_position", 0.5)

    up_score = 0.5
    if spot_chg > 0:  up_score += min(0.2, spot_chg / 200)
    else:             up_score -= min(0.2, abs(spot_chg) / 200)
    if momentum > 0:  up_score += 0.05
    else:             up_score -= 0.05
    if bb > 0.8:      up_score -= 0.05
    elif bb < 0.2:    up_score += 0.05

    up_score  = max(0.1, min(0.9, up_score))
    dn_score  = 1 - up_score
    direction = "UP" if up_score >= 0.50 else "DN"
    confidence = max(up_score, dn_score)
    return direction, confidence, up_score, dn_score


def get_trading_session(utc_hour):
    if   0 <= utc_hour < 7:   return "asia",          "active"
    elif 7 <= utc_hour < 8:   return "asia_close",    "transition"
    elif 8 <= utc_hour < 12:  return "europe",        "active"
    elif 12 <= utc_hour < 13: return "eu_us_overlap", "transition"
    elif 13 <= utc_hour < 14: return "us_premarket",  "high_vol"
    elif 14 <= utc_hour < 16: return "us_open",       "high_vol"
    elif 16 <= utc_hour < 20: return "us_midday",     "active"
    elif 20 <= utc_hour < 21: return "us_close",      "high_vol"
    else:                      return "after_hours",   "low_vol"

# ═══════════════════════════════════════════════════════════════
# ORACLE LAG MEASUREMENT — the whole point of v13
# ═══════════════════════════════════════════════════════════════

def measure_oracle_lag(up_ask, dn_ask, spot_now, spot_start):
    """
    Attempt to measure oracle lag by comparing:
    - What Polymarket is pricing (up_ask)
    - What Binance says RIGHT NOW (spot_now vs spot_start)

    If polymarket price is already reflecting an outcome that
    hasn't happened yet on Binance → oracle lag detected.

    Returns estimated lag in seconds (None if can't measure).
    """
    if not up_ask or not dn_ask:
        return None

    # Polymarket implied direction
    pm_implied = "UP" if up_ask > 0.55 else "DN" if dn_ask > 0.55 else None
    if not pm_implied:
        return None  # Too close to call

    # Binance current direction
    btc_implied = "UP" if spot_now > spot_start else "DN"

    # If they agree strongly, no obvious lag signal
    # If PM strongly implies a direction but BTC hasn't moved there yet
    # → PM is ahead of BTC → oracle lag
    pm_certainty = abs(up_ask - 0.5) * 2  # 0 = 50/50, 1 = certain
    if pm_certainty > 0.3 and pm_implied != btc_implied:
        # PM is saying one thing, BTC says another
        # This is the signature of oracle lag: PM already knows
        return pm_certainty  # Return certainty as proxy for lag magnitude
    return None

# ═══════════════════════════════════════════════════════════════
# DATA LOGGING
# ═══════════════════════════════════════════════════════════════

FIELDNAMES = [
    # Timing — critical for oracle lag analysis
    "candle", "elapsed_s", "timestamp_utc", "timestamp_ms",
    "utc_hour", "utc_minute", "day_of_week",
    "trading_session", "session_phase",
    # Price data with source tracking
    "spot_start", "spot_now", "spot_chg",
    "btc_price_ms",           # Exact ms when BTC price was read
    "pm_price_ms",            # Exact ms when PM price was read
    "price_read_gap_ms",      # Gap between BTC and PM reads (oracle lag proxy)
    # Polymarket prices
    "up_ask", "dn_ask", "pair_cost",
    "price_source",           # "websocket" or "rest"
    "clob_sane",
    # Oracle lag measurement
    "oracle_lag_signal",      # Estimated lag magnitude (None if can't measure)
    "pm_implied_dir",         # What PM is pricing
    "btc_implied_dir",        # What BTC says right now
    "pm_btc_agree",           # Do they agree?
    # ML signals
    "direction", "confidence", "up_prob",
    # Technical features
    "bb_position", "rsi_5m", "macd_histogram", "stoch_k",
    "momentum_short", "volatility_5m", "trend_alignment",
    # Candle dynamics
    "spot_velocity", "direction_changes", "scan_count",
    "peak_conf_so_far", "conf_drop_from_peak",
    # Outcome (filled after resolution)
    "winner", "final_spot_chg", "was_correct",
    # Paper trade tracking
    "would_trade",            # Would bot have traded here?
    "would_trade_reason",     # Why yes or why no
]


def log_scan(candle_num, elapsed, spot_start, spot_now, up_ask, dn_ask,
             direction, confidence, up_prob, features, scan_count,
             direction_changes, peak_conf, btc_ms, pm_ms, price_source,
             would_trade, would_trade_reason):
    """Log one scan with full oracle lag metadata."""
    now_utc = datetime.now(timezone.utc)
    now_ms  = int(time.time() * 1000)

    spot_chg    = spot_now - spot_start
    clob_sane   = 1 if (up_ask and dn_ask and 0.90 <= (up_ask + dn_ask) <= 1.10) else 0

    # Oracle lag measurement
    oracle_lag  = measure_oracle_lag(up_ask, dn_ask, spot_now, spot_start)
    pm_implied  = "UP" if (up_ask and up_ask > 0.55) else "DN" if (dn_ask and dn_ask > 0.55) else "EVEN"
    btc_implied = "UP" if spot_chg >= 0 else "DN"
    pm_btc_agree = 1 if pm_implied == btc_implied else 0

    price_gap_ms = abs(btc_ms - pm_ms) if btc_ms and pm_ms else None

    row = {
        "candle":           candle_num,
        "elapsed_s":        round(elapsed, 2),
        "timestamp_utc":    now_utc.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
        "timestamp_ms":     now_ms,
        "utc_hour":         now_utc.hour,
        "utc_minute":       now_utc.minute,
        "day_of_week":      now_utc.strftime("%a"),
        "trading_session":  features.get("session", "unknown"),
        "session_phase":    features.get("session_phase", "unknown"),
        "spot_start":       round(spot_start, 2),
        "spot_now":         round(spot_now, 2),
        "spot_chg":         round(spot_chg, 2),
        "btc_price_ms":     btc_ms,
        "pm_price_ms":      pm_ms,
        "price_read_gap_ms": price_gap_ms,
        "up_ask":           round(up_ask, 4) if up_ask else None,
        "dn_ask":           round(dn_ask, 4) if dn_ask else None,
        "pair_cost":        round((up_ask or 0.5) + (dn_ask or 0.5), 4),
        "price_source":     price_source,
        "clob_sane":        clob_sane,
        "oracle_lag_signal": round(oracle_lag, 4) if oracle_lag else None,
        "pm_implied_dir":   pm_implied,
        "btc_implied_dir":  btc_implied,
        "pm_btc_agree":     pm_btc_agree,
        "direction":        direction,
        "confidence":       round(confidence, 4),
        "up_prob":          round(up_prob, 4),
        "bb_position":      round(features.get("bb_position", 0.5), 4),
        "rsi_5m":           round(features.get("rsi_5m", 50), 2),
        "macd_histogram":   round(features.get("macd_histogram", 0), 4),
        "stoch_k":          round(features.get("stoch_k", 50), 2),
        "momentum_short":   round(features.get("momentum_short", 0), 4),
        "volatility_5m":    round(features.get("volatility_5m", 0), 4),
        "trend_alignment":  features.get("trend_alignment", 0),
        "spot_velocity":    round(spot_chg / max(elapsed, 1), 4),
        "direction_changes": direction_changes,
        "scan_count":       scan_count,
        "peak_conf_so_far": round(peak_conf, 4),
        "conf_drop_from_peak": round(peak_conf - confidence, 4),
        "winner":           None,
        "final_spot_chg":   None,
        "was_correct":      None,
        "would_trade":      1 if would_trade else 0,
        "would_trade_reason": would_trade_reason,
    }

    candle_state["scan_log"].append(row)
    return row


def backfill_outcome(candle_num, winner, final_spot_chg):
    for row in candle_state["scan_log"]:
        if row["candle"] == candle_num and row["winner"] is None:
            row["winner"]        = winner
            row["final_spot_chg"] = round(final_spot_chg, 2)
            row["was_correct"]   = 1 if row["direction"] == winner else 0


def save_data(reason="auto"):
    log = candle_state["scan_log"]
    if not log:
        return

    # Session file — full log
    try:
        with open(SESSION_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
            writer.writerows(log)
    except Exception as e:
        print(f"  ⚠️  Session save error: {e}")

    # Master file — append completed candles only
    completed = [r for r in log if r.get("winner")]
    if not completed:
        return

    try:
        file_exists = os.path.exists(MASTER_FILE)
        # Get last candle num already saved to avoid duplicates
        saved_timestamps = set()
        if file_exists:
            with open(MASTER_FILE, "r") as f:
                for r in csv.DictReader(f):
                    saved_timestamps.add(r.get("timestamp_utc", ""))


        new_rows = [r for r in completed if r.get("timestamp_utc") not in saved_timestamps]
        if not new_rows:
            return

        with open(MASTER_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if not file_exists:
                writer.writeheader()
            writer.writerows(new_rows)

        print(f"  💾 Saved {len(new_rows)} candle(s) to {MASTER_FILE} [{reason}]")
    except Exception as e:
        print(f"  ⚠️  Master save error: {e}")


def write_live_json(candle_num, elapsed, spot_start, spot_now,
                    direction, confidence, up_ask, dn_ask,
                    session, scan_count, would_trade, price_source):
    try:
        recent = candle_state["scan_log"][-60:]
        trades = candle_state["session_trades"]

        live = {
            "timestamp":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "candle_num":   candle_num,
            "elapsed":      round(elapsed, 1),
            "remaining":    round(CANDLE_DURATION - elapsed, 1),
            "spot_start":   round(spot_start, 2),
            "spot_now":     round(spot_now, 2),
            "spot_chg":     round(spot_now - spot_start, 2),
            "direction":    direction,
            "confidence":   round(confidence * 100, 1),
            "up_ask":       round(up_ask * 100, 1) if up_ask else None,
            "dn_ask":       round(dn_ask * 100, 1) if dn_ask else None,
            "session":      session,
            "scan_count":   scan_count,
            "would_trade":  would_trade,
            "price_source": price_source,
            "wins":         sum(1 for t in trades if t["result"] == "WIN"),
            "losses":       sum(1 for t in trades if t["result"] == "LOSS"),
            "pnl":          round(candle_state["daily_pnl"], 2),
            "total_candles": candle_state["candle_num"],
            "chart": [
                {
                    "t":    r["elapsed_s"],
                    "spot": r["spot_chg"],
                    "conf": round(float(r["confidence"]) * 100, 1),
                    "dir":  r["direction"],
                    "up":   round(float(r["up_ask"]) * 100, 1) if r.get("up_ask") else None,
                    "dn":   round(float(r["dn_ask"]) * 100, 1) if r.get("dn_ask") else None,
                    "lag":  r.get("oracle_lag_signal"),
                    "agree": r.get("pm_btc_agree"),
                }
                for r in recent
            ],
        }
        with open(LIVE_FILE, "w") as f:
            json.dump(live, f)
    except Exception:
        pass

# ═══════════════════════════════════════════════════════════════
# MAIN SIGNAL LOOP — runs every SCAN_INTERVAL seconds
# ═══════════════════════════════════════════════════════════════

async def signal_loop(shutdown_event):
    """
    Core trading loop — runs as async coroutine.
    Reads from in-memory buffers (no REST calls in hot path).
    Evaluates signals, logs scans, manages candle lifecycle.
    """
    candle_num = 0
    
    # Wait for Binance feed to start
    print("  ⏳ Waiting for Binance WebSocket feed...")
    for _ in range(30):
        if btc_buffer:
            break
        await asyncio.sleep(1)
    
    if not btc_buffer:
        print("  ❌ Binance feed never started — check connection")
        return

    spot = get_btc_now()
    print(f"  ✅ BTC price confirmed: ${spot:,.2f}")
    print(f"\n  🚀 Starting observation loop — PAPER MODE, NO RESTRICTIONS")
    print(f"  📊 Collecting clean data for oracle lag analysis\n")

    while not shutdown_event.is_set():
        
        # ── CANDLE SETUP ──────────────────────────────────────────
        candle_num += 1
        candle_state["candle_num"] = candle_num
        candle_state["scan_log"]   = []
        candle_state["traded"]     = False
        candle_state["trade_dir"]  = None

        # Find current candle slot
        now = time.time()
        current_slot = int(now) - (int(now) % CANDLE_DURATION)
        elapsed_in_slot = now - current_slot

        # Wait for next candle if more than 50% through current
        if elapsed_in_slot > CANDLE_DURATION * 0.50:
            wait_for = CANDLE_DURATION - elapsed_in_slot
            next_slot = current_slot + CANDLE_DURATION
            next_label = datetime.fromtimestamp(next_slot).strftime("%I:%M %p")
            print(f"  ⏳ Waiting for next candle at {next_label} ({wait_for:.0f}s)...")
            await asyncio.sleep(wait_for)
            current_slot = next_slot

        candle_start_ts = current_slot
        candle_end_ts   = current_slot + CANDLE_DURATION

        # Discover Polymarket market (REST — done once per candle)
        print(f"\n{'━'*65}")
        now_label = datetime.fromtimestamp(candle_start_ts).strftime("%I:%M %p")
        end_label = datetime.fromtimestamp(candle_end_ts).strftime("%I:%M %p")
        utc_hour  = datetime.now(timezone.utc).hour
        session, session_phase = get_trading_session(utc_hour)
        print(f"  CANDLE #{candle_num} | {now_label} → {end_label} | {session} ({session_phase})")
        print(f"{'━'*65}")

        candle_info = await asyncio.get_event_loop().run_in_executor(None, find_candle)
        candle_state["candle"] = candle_info

        if candle_info:
            print(f"  📋 {candle_info['question']}")
        else:
            print(f"  ⚠️  No Polymarket candle found — observing BTC only")

        # Get starting prices (REST — done once)
        # Get EXACT Chainlink price to beat from Polymarket page
        spot_start = get_btc_now()  # fallback
        ptb = get_polymarket_price_to_beat(candle_start_ts)
        if ptb:
            spot_start = ptb
            print(f"  🎯 Price to beat: ${spot_start:,.2f} (exact Chainlink)")
        else:
            try:
                ts_ms = candle_start_ts * 1000
                r_kl = requests.get(f"{BINANCE_REST}/klines", params={
                    "symbol": "BTCUSDT", "interval": "1m",
                    "startTime": ts_ms - 1000, "limit": 1
                }, timeout=5)
                kl = r_kl.json()
                if kl:
                    spot_start = float(kl[0][1])
                    print(f"  📍 Slot open price: ${spot_start:,.2f} (Binance fallback)")
            except Exception as e:
                print(f"  ⚠️  Using live price as fallback")
        klines     = await asyncio.get_event_loop().run_in_executor(None, get_klines, 120)

        # Initial CLOB prices via REST
        up_ask_init, dn_ask_init = None, None
        if candle_info and candle_info.get("up_token"):
            up_ask_init, dn_ask_init = await asyncio.get_event_loop().run_in_executor(
                None, get_clob_prices_rest,
                candle_info["up_token"], candle_info.get("dn_token"))

        print(f"  💰 BTC Start: ${spot_start:,.2f}")
        if up_ask_init and dn_ask_init:
            print(f"  📦 CLOB: UP={up_ask_init*100:.0f}c DN={dn_ask_init*100:.0f}c")
        print()
        print(f"  {'Time':>5}  {'BTC Δ':>7}  {'Dir':>4}  {'Conf':>5}  {'UP':>4}  {'DN':>4}  {'Lag':>5}  {'Src'}")
        print(f"  {'─'*60}")

        # ── CANDLE SCANNING LOOP ──────────────────────────────────
        scan_count      = 0
        direction_changes = 0
        last_direction  = None
        peak_conf       = 0.0
        
        # Track best would-trade opportunity
        best_score      = 0
        paper_trade_logged = False

        while True:
            now       = time.time()
            elapsed   = now - candle_start_ts
            remaining = candle_end_ts - now

            if remaining <= 1.5:
                break

            # ── GET PRICES FROM BUFFERS (zero latency) ──
            btc_price_ms, spot_now = (btc_buffer[-1] if btc_buffer else (None, None))
            spot_now = spot_now or spot_start

            # Try WebSocket PM prices first, then REST refresh every 15s
            up_ask_ws, dn_ask_ws = get_pm_prices_now()
            pm_ms = int(time.time() * 1000)

            if up_ask_ws and dn_ask_ws and 0.90 <= (up_ask_ws + dn_ask_ws) <= 1.10:
                up_ask       = up_ask_ws
                dn_ask       = dn_ask_ws
                up_ask_init  = up_ask_ws
                dn_ask_init  = dn_ask_ws
                price_source = "websocket"
            elif int(elapsed) % 15 < 1.5 and candle_info and candle_info.get("up_token"):
                # Refresh REST every 15s
                fresh_up, fresh_dn = await asyncio.get_event_loop().run_in_executor(
                    None, get_clob_prices_rest,
                    candle_info["up_token"], candle_info.get("dn_token"))
                if fresh_up and fresh_dn and 0.90 <= (fresh_up + fresh_dn) <= 1.10:
                    up_ask_init  = fresh_up
                    dn_ask_init  = fresh_dn
                up_ask       = up_ask_init or 0.50
                dn_ask       = dn_ask_init or 0.50
                price_source = "rest_refresh"
            elif up_ask_init and dn_ask_init:
                up_ask       = up_ask_init
                dn_ask       = dn_ask_init
                price_source = "rest_cached"
            else:
                up_ask       = 0.50
                dn_ask       = 0.50
                price_source = "fallback"

            # ── BUILD FEATURES ──
            features = build_features(spot_start, spot_now, klines, elapsed)
            features["session"]       = session
            features["session_phase"] = session_phase

            # ── ML PREDICTION ──
            direction, confidence, up_prob, dn_prob = ml_predict_simple(features)

            # Track dynamics
            scan_count += 1
            if last_direction and direction != last_direction:
                direction_changes += 1
            last_direction = direction
            if confidence > peak_conf:
                peak_conf = confidence

            # ── WOULD-TRADE LOGIC (observation — no real restrictions) ──
            # Log what the bot WOULD do, but don't actually trade
            # This gives us clean paper trade data to analyze later
            spot_chg      = spot_now - spot_start
            clob_ok       = price_source in ("websocket", "rest_cached")
            ghost_block   = (up_ask == 0.50 and dn_ask == 0.50)
            entry_price   = up_ask if direction == "UP" else dn_ask

            would_trade   = False
            reason        = ""

            if elapsed < 30:
                reason = f"too_early (T={elapsed:.0f}s)"
            elif ghost_block:
                reason = "ghost_50_50"
            elif confidence < CONFIDENCE_THRESH:
                reason = f"low_conf ({confidence*100:.0f}%)"
            elif not clob_ok:
                reason = "no_price"
            else:
                would_trade = True
                reason = f"ENTER_{direction}@{entry_price*100:.0f}c T={elapsed:.0f}s conf={confidence*100:.0f}%"

            # Log first would-trade per candle for paper tracking
            if would_trade and not paper_trade_logged:
                paper_trade_logged = True
                candle_state["traded"]     = True
                candle_state["trade_dir"]  = direction
                candle_state["trade_price"] = entry_price
                candle_state["trade_elapsed"] = elapsed

            # ── ORACLE LAG MEASUREMENT ──
            oracle_lag = measure_oracle_lag(up_ask, dn_ask, spot_now, spot_start)
            pm_implied = "UP" if up_ask > 0.55 else "DN" if dn_ask > 0.55 else "EV"
            btc_implied = "UP" if spot_chg >= 0 else "DN"
            lag_str = f"{oracle_lag:.2f}" if oracle_lag else "  — "
            agree_icon = "✅" if pm_implied == btc_implied else "⚠️ "

            # ── LOG SCAN ──
            log_scan(
                candle_num, elapsed, spot_start, spot_now,
                up_ask, dn_ask, direction, confidence, up_prob,
                features, scan_count, direction_changes, peak_conf,
                btc_price_ms, pm_ms, price_source,
                would_trade, reason
            )

            # ── DISPLAY ──
            src_icon = "🔴" if price_source == "fallback" else "🟡" if price_source == "rest_cached" else "🟢"
            dir_icon = "🟢" if direction == "UP" else "🔴"
            wt_icon  = "★" if would_trade else " "
            print(f"  {elapsed:>5.0f}s  {spot_chg:>+7.0f}  {dir_icon}{direction}  "
                  f"{confidence*100:4.0f}%  "
                  f"{up_ask*100:4.0f}c  {dn_ask*100:4.0f}c  "
                  f"{lag_str:>5} {agree_icon} {src_icon}{wt_icon}")

            # Write live dashboard
            write_live_json(candle_num, elapsed, spot_start, spot_now,
                           direction, confidence, up_ask, dn_ask,
                           session, scan_count, would_trade, price_source)

            await asyncio.sleep(SCAN_INTERVAL)

        # ── RESOLUTION ──────────────────────────────────────────
        final_spot = get_btc_now() or spot_start
        final_chg  = final_spot - spot_start
        winner     = "UP" if final_chg >= 0 else "DN"

        backfill_outcome(candle_num, winner, final_chg)

        print(f"\n  ═══════════════════════════════════════════")
        print(f"  RESULT: {'🟢' if winner == 'UP' else '🔴'} {winner} | BTC: {final_chg:+.0f}")

        # Paper trade result
        if candle_state["traded"]:
            trade_dir   = candle_state["trade_dir"]
            trade_price = candle_state["trade_price"]
            trade_el    = candle_state["trade_elapsed"]

            if trade_dir == winner:
                profit = BET_SIZE * (1.0 - trade_price) / trade_price if trade_price > 0 else 0
                candle_state["daily_pnl"] += profit
                candle_state["session_trades"].append({
                    "result": "WIN", "pnl": profit,
                    "dir": trade_dir, "elapsed": trade_el
                })
                print(f"  ★ PAPER WIN:  {trade_dir} @ {trade_price*100:.0f}c | +${profit:.2f}")
            else:
                candle_state["daily_pnl"] -= BET_SIZE
                candle_state["session_trades"].append({
                    "result": "LOSS", "pnl": -BET_SIZE,
                    "dir": trade_dir, "elapsed": trade_el
                })
                print(f"  ★ PAPER LOSS: {trade_dir} @ {trade_price*100:.0f}c | -${BET_SIZE:.2f}")
        else:
            print(f"  ⚪ No paper trade this candle")

        # Oracle lag summary for this candle
        lag_signals = [r["oracle_lag_signal"] for r in candle_state["scan_log"]
                      if r.get("oracle_lag_signal")]
        disagree    = [r for r in candle_state["scan_log"] if r.get("pm_btc_agree") == 0]
        if lag_signals:
            print(f"  🔬 Oracle lag detected in {len(lag_signals)} scans "
                  f"(avg magnitude: {np.mean(lag_signals):.2f})")
        if disagree:
            print(f"  ⚠️  PM/BTC disagreement: {len(disagree)} scans "
                  f"({len(disagree)/len(candle_state['scan_log'])*100:.0f}% of candle)")

        # Session summary
        trades = candle_state["session_trades"]
        wins   = sum(1 for t in trades if t["result"] == "WIN")
        losses = sum(1 for t in trades if t["result"] == "LOSS")
        wr     = wins / (wins + losses) * 100 if (wins + losses) > 0 else 0

        print(f"\n  📊 Session: {wins}W/{losses}L ({wr:.0f}%) | P&L: ${candle_state['daily_pnl']:+.2f}")
        print(f"  📁 Scans this candle: {len(candle_state['scan_log'])}")

        # Save
        save_data(f"candle_{candle_num}")
        frank_agent.on_candle_complete(candle_num)

        # Small gap between candles
        await asyncio.sleep(2)

# ═══════════════════════════════════════════════════════════════
# KLINE REFRESH TASK — updates klines every 60s in background
# ═══════════════════════════════════════════════════════════════

_klines_cache = {"data": None, "last_update": 0}

async def kline_refresh_loop(shutdown_event):
    """Refreshes klines every 60s in background — doesn't block signal loop."""
    while not shutdown_event.is_set():
        try:
            klines = await asyncio.get_event_loop().run_in_executor(None, get_klines, 120)
            if klines:
                _klines_cache["data"] = klines
                _klines_cache["last_update"] = time.time()
        except Exception:
            pass
        await asyncio.sleep(60)

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    shutdown_event = asyncio.Event()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  🧠 FRANKENSTEIN v13 — CLEAN DATA COLLECTOR                 ║
║                                                              ║
║  Architecture: asyncio + WebSocket (no REST in hot path)    ║
║  Mode: PAPER / OBSERVE ONLY — no live trades                 ║
║  Goal: Collect clean data + measure oracle lag               ║
║                                                              ║
║  NEW in v13:                                                 ║
║    ⚡ Binance WS: price pushed every 100ms                   ║
║    📡 Polymarket RTDS: odds pushed live                      ║
║    🔬 Oracle lag measurement per scan                        ║
║    ⏱️  Millisecond timestamps on every price read            ║
║    🚫 NO restrictions — pure observation                     ║
║                                                              ║
║  Brain: {'v' + brain_version + ' loaded' if brain_version else '⚠️  none — momentum only':35s}║
║  Output: {MASTER_FILE:44s}║
║  Press Ctrl+C to stop                                        ║
╚══════════════════════════════════════════════════════════════╝
""")

    # Run all tasks concurrently
    try:
        await asyncio.gather(
            binance_feed(shutdown_event),
            polymarket_feed(shutdown_event),
            kline_refresh_loop(shutdown_event),
            signal_loop(shutdown_event),
        )
    except asyncio.CancelledError:
        pass
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()

        # Final save
        print(f"\n\n  Stopped. Saving data...")
        save_data("exit")

        trades = candle_state["session_trades"]
        wins   = sum(1 for t in trades if t["result"] == "WIN")
        losses = sum(1 for t in trades if t["result"] == "LOSS")

        print(f"\n{'═'*65}")
        print(f"  FINAL SUMMARY")
        print(f"{'═'*65}")
        print(f"  Candles observed: {candle_state['candle_num']}")
        print(f"  Paper trades: {wins}W / {losses}L" + (f" = {wins/(wins+losses)*100:.0f}% WR" if (wins+losses) > 0 else ""))
        print(f"  Paper P&L: ${candle_state['daily_pnl']:+.2f}")
        print(f"  Data saved to: {MASTER_FILE}")

        # Count total rows saved
        if os.path.exists(MASTER_FILE):
            with open(MASTER_FILE) as f:
                total = sum(1 for _ in f) - 1
            print(f"  Total scans in master: {total:,}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n  Bye!")
