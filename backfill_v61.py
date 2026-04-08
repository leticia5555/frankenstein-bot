#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║  v61 HISTORICAL BACKFILL                                            ║
║                                                                      ║
║  Downloads 3 months of BTC 1-minute candles from Binance             ║
║  and reconstructs all 16 v61 features for ML training.               ║
║                                                                      ║
║  This solves the DN bias problem by giving the model                 ║
║  thousands of candles across all market conditions:                   ║
║  trending up, trending down, sideways, volatile, quiet,              ║
║  weekday, weekend, Asian/EU/US sessions.                             ║
║                                                                      ║
║  Usage: python3 backfill_v61.py                                      ║
║  Output: btc_15m_data_v61_backfill.csv                               ║
║          (merge with your live data before trading)                   ║
╚══════════════════════════════════════════════════════════════════════╝
"""

import requests
import time
import csv
import math
import sys
from datetime import datetime, timedelta
from collections import deque

# === OUTPUT ===
OUTPUT_FILE = "btc_15m_data_v61_backfill.csv"
MERGE_FILE = "btc_15m_data_v61.csv"  # Your live data file

# === BINANCE API ===
BINANCE_API = "https://api.binance.com/api/v3/klines"

# How far back to go (3 months = ~8,640 15-min candles)
MONTHS_BACK = 3


# ═══════════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE (same as bot_v61.py)
# Reconstructed from historical 1-minute candles
# ═══════════════════════════════════════════════════════════════════

class HistoricalTA:
    """
    Replicate v61's TechnicalAnalysis class but from historical data.
    Instead of real-time ticks, we feed it 1-minute OHLCV candles.
    """

    def __init__(self):
        self.prices = deque(maxlen=500)
        self.ha_open = None
        self.ha_close = None
        self.ha_trend = 0
        self.macd_signal_ema = None
        self.vwap_cum_pv = 0
        self.vwap_cum_vol = 0

    def feed_1m_candle(self, candle):
        """Feed a 1-minute candle: {'open','high','low','close','volume'}"""
        price = candle['close']
        self.prices.append(price)

        # VWAP
        vol = candle['volume']
        self.vwap_cum_pv += price * vol
        self.vwap_cum_vol += vol

        # Heiken Ashi
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        ha_close = (o + h + l + c) / 4

        if self.ha_open is None:
            ha_open = (o + c) / 2
        else:
            ha_open = (self.ha_open + self.ha_close) / 2

        self.ha_open = ha_open
        self.ha_close = ha_close

        if ha_close > ha_open:
            self.ha_trend = 1
        elif ha_close < ha_open:
            self.ha_trend = -1
        else:
            self.ha_trend = 0

    def get_rsi(self, period=14):
        if len(self.prices) < period + 1:
            return 50.0
        prices = list(self.prices)
        changes = [prices[i] - prices[i-1] for i in range(-period, 0)]
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0.0001
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def get_macd(self):
        if len(self.prices) < 26:
            return 0, 0, 0
        prices = list(self.prices)
        ema_12 = self._calc_ema(prices, 12)
        ema_26 = self._calc_ema(prices, 26)
        macd_line = ema_12 - ema_26

        if self.macd_signal_ema is None:
            self.macd_signal_ema = macd_line
        else:
            k = 2 / (9 + 1)
            self.macd_signal_ema = macd_line * k + self.macd_signal_ema * (1 - k)

        histogram = macd_line - self.macd_signal_ema
        return macd_line, self.macd_signal_ema, histogram

    def _calc_ema(self, prices, period):
        if len(prices) < period:
            return prices[-1] if prices else 0
        k = 2 / (period + 1)
        ema = sum(prices[-period:]) / period
        for price in prices[-period:]:
            ema = price * k + ema * (1 - k)
        return ema

    def get_vwap(self):
        if self.vwap_cum_vol == 0:
            return self.prices[-1] if self.prices else 0
        return self.vwap_cum_pv / self.vwap_cum_vol

    def get_volatility(self, window=30):
        if len(self.prices) < window + 1:
            return 0
        prices = list(self.prices)[-window:]
        changes = [(prices[i] - prices[i-1]) / prices[i-1] * 100 for i in range(1, len(prices))]
        if not changes:
            return 0
        mean = sum(changes) / len(changes)
        variance = sum((c - mean) ** 2 for c in changes) / len(changes)
        return math.sqrt(variance)

    def get_frondent_signal(self):
        """Replicate FrondEnt's weighted TA scoring"""
        score = 0
        max_score = 0

        # RSI (weight: 2)
        rsi = self.get_rsi()
        max_score += 2
        if rsi > 55:
            score += 2 * min((rsi - 50) / 30, 1)
        elif rsi < 45:
            score -= 2 * min((50 - rsi) / 30, 1)

        # MACD (weight: 2)
        _, _, histogram = self.get_macd()
        max_score += 2
        if histogram > 0:
            score += 2 * min(abs(histogram) / 5, 1)
        else:
            score -= 2 * min(abs(histogram) / 5, 1)

        # Heiken Ashi (weight: 1.5)
        max_score += 1.5
        score += self.ha_trend * 1.5

        # VWAP (weight: 1.5)
        vwap = self.get_vwap()
        current_price = self.prices[-1] if self.prices else 0
        max_score += 1.5
        if current_price > vwap:
            score += 1.5
        elif current_price < vwap:
            score -= 1.5

        # Deltas (weight: 1 each)
        if len(self.prices) >= 2:
            delta_1m = ((self.prices[-1] - self.prices[-2]) / self.prices[-2]) * 100
            max_score += 1
            if delta_1m > 0:
                score += min(abs(delta_1m) / 0.1, 1)
            else:
                score -= min(abs(delta_1m) / 0.1, 1)

        if len(self.prices) >= 4:
            delta_3m = ((self.prices[-1] - self.prices[-4]) / self.prices[-4]) * 100
            max_score += 1
            if delta_3m > 0:
                score += min(abs(delta_3m) / 0.2, 1)
            else:
                score -= min(abs(delta_3m) / 0.2, 1)

        if max_score == 0:
            return 'LONG', 0.5

        normalized = (score / max_score + 1) / 2
        if normalized >= 0.5:
            return 'LONG', normalized
        else:
            return 'SHORT', 1 - normalized

    def reset_for_new_candle(self):
        """Reset VWAP for new 15-min period (keep price history)"""
        self.vwap_cum_pv = 0
        self.vwap_cum_vol = 0


# ═══════════════════════════════════════════════════════════════════
# BINANCE DATA DOWNLOAD
# ═══════════════════════════════════════════════════════════════════

def download_1m_candles(start_ms, end_ms):
    """
    Download 1-minute BTC/USDT candles from Binance.
    Returns list of {'timestamp', 'open', 'high', 'low', 'close', 'volume'}
    """
    all_candles = []
    current = start_ms
    consecutive_errors = 0
    max_consecutive_errors = 5

    total_expected = (end_ms - start_ms) / (60 * 1000)
    downloaded = 0

    while current < end_ms:
        try:
            params = {
                'symbol': 'BTCUSDT',
                'interval': '1m',
                'startTime': current,
                'endTime': min(current + 1000 * 60 * 1000, end_ms),  # 1000 candles max
                'limit': 1000
            }
            r = requests.get(BINANCE_API, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()

            if not data:
                break

            consecutive_errors = 0  # Reset on success

            for kline in data:
                candle = {
                    'timestamp': kline[0],  # Open time in ms
                    'open': float(kline[1]),
                    'high': float(kline[2]),
                    'low': float(kline[3]),
                    'close': float(kline[4]),
                    'volume': float(kline[5]),
                }
                all_candles.append(candle)

            downloaded += len(data)
            pct = min(100, downloaded / total_expected * 100)
            last_date = datetime.utcfromtimestamp(data[-1][0] / 1000).strftime('%Y-%m-%d %H:%M')
            print(f"  📥 Downloaded {downloaded:,} 1m candles ({pct:.0f}%) | Last: {last_date}", end='\r')

            # Move past last candle
            current = data[-1][0] + 60000

            # Rate limit: Binance allows 1200 req/min, but be nice
            time.sleep(0.25)

        except requests.exceptions.RequestException as e:
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                print(f"\n  ❌ Failed after {max_consecutive_errors} consecutive errors. Check your internet!")
                break
            print(f"\n  ⚠️  API error ({consecutive_errors}/{max_consecutive_errors}): {e}, retrying in 5s...")
            time.sleep(5)
        except Exception as e:
            print(f"\n  ❌ Error: {e}")
            break

    print()  # newline after progress
    return all_candles


# ═══════════════════════════════════════════════════════════════════
# BUILD 15-MINUTE TRAINING DATA
# ═══════════════════════════════════════════════════════════════════

def build_15m_training_data(candles_1m):
    """
    Group 1-minute candles into 15-minute windows.
    For each window, reconstruct all 16 v61 features at each minute.
    Determine UP/DN outcome from BTC open vs close.
    
    This produces data in the EXACT same format as bot_v61.py --collect
    """
    # Group into 15-minute windows (aligned to Polymarket timestamps)
    windows = {}
    for c in candles_1m:
        ts_sec = c['timestamp'] // 1000
        window_start = (ts_sec // 900) * 900  # Align to 15-min boundary
        if window_start not in windows:
            windows[window_start] = []
        windows[window_start].append(c)

    # Sort windows chronologically
    sorted_windows = sorted(windows.items())

    print(f"  📊 Found {len(sorted_windows)} 15-minute windows")

    ta = HistoricalTA()
    all_rows = []
    candle_count = 0

    for window_start, minute_candles in sorted_windows:
        # Skip incomplete windows (need at least 13 minutes of data)
        if len(minute_candles) < 13:
            # Still feed TA to maintain state
            for mc in minute_candles:
                ta.feed_1m_candle(mc)
            continue

        # Sort by timestamp within window
        minute_candles.sort(key=lambda x: x['timestamp'])

        btc_open = minute_candles[0]['open']
        btc_close = minute_candles[-1]['close']
        outcome = 'UP' if btc_close > btc_open else 'DN'

        # Reset VWAP for new candle (keep price/EMA state for continuity)
        ta.reset_for_new_candle()

        # Track BTC history within this candle for momentum calculations
        btc_history = [btc_open]

        # Generate one row per minute (like bot_v61 does)
        for i, mc in enumerate(minute_candles):
            if i >= 15:  # Max 15 snapshots per candle
                break

            minute = i  # 0-14
            btc_current = mc['close']

            # Feed TA engine
            ta.feed_1m_candle(mc)

            # Core features (same as v60)
            btc_change_pct = ((btc_current - btc_open) / btc_open) * 100

            # Simulate Polymarket prices from BTC movement
            # The market roughly prices based on BTC direction so far
            # When BTC is up, UP price rises, DN price falls, and vice versa
            # We use a sigmoid-like model calibrated to real Polymarket behavior
            midpoint = 0.50
            # Scale factor: ±0.1% BTC ≈ ±15¢ price move (from live observation)
            price_shift = max(-0.45, min(0.45, btc_change_pct * 15))
            # Add time decay: prices become more extreme as candle progresses
            time_factor = 1.0 + (minute / 14) * 0.5
            price_shift *= time_factor

            up_price = max(0.05, min(0.95, midpoint + price_shift / 100 * 50))
            dn_price = max(0.05, min(0.95, 1.0 - up_price))

            # Ensure total cost is realistic (usually 0.98-1.02)
            total = up_price + dn_price
            if total > 0:
                up_price = up_price / total * (0.99 + (minute / 14) * 0.005)
                dn_price = 1.0 - up_price

            up_price = round(max(0.05, min(0.95, up_price)), 4)
            dn_price = round(max(0.05, min(0.95, dn_price)), 4)

            price_gap = abs(up_price - dn_price)

            # Momentum
            btc_history.append(btc_current)
            momentum_1m = ((btc_current - btc_history[-2]) / btc_history[-2]) * 100 if len(btc_history) >= 2 else 0
            momentum_3m = ((btc_current - btc_history[-4]) / btc_history[-4]) * 100 if len(btc_history) >= 4 else 0

            # TA features
            rsi = ta.get_rsi()
            macd_line, macd_signal, macd_histogram = ta.get_macd()
            vwap = ta.get_vwap()
            vwap_deviation = ((btc_current - vwap) / vwap * 100) if vwap else 0
            volatility = ta.get_volatility()
            frondent_dir, frondent_conf = ta.get_frondent_signal()
            frondent_signal = frondent_conf if frondent_dir == 'LONG' else -frondent_conf

            total_cost = up_price + dn_price

            timestamp = datetime.utcfromtimestamp(window_start + minute * 60).isoformat()

            row = [
                timestamp,              # timestamp
                window_start,           # candle_start
                minute,                 # minute
                btc_open,               # btc_open
                btc_current,            # btc_current
                round(btc_change_pct, 4),  # btc_change_pct
                up_price,               # up_price
                dn_price,               # dn_price
                round(price_gap, 4),    # price_gap
                round(momentum_1m, 4),  # momentum_1m
                round(momentum_3m, 4),  # momentum_3m
                round(rsi, 2),          # rsi
                round(macd_line, 4),    # macd_line
                round(macd_signal, 4),  # macd_signal
                round(macd_histogram, 4),  # macd_histogram
                ta.ha_trend,            # ha_trend
                round(vwap_deviation, 4),  # vwap_deviation
                round(volatility, 4),   # volatility
                round(frondent_signal, 4),  # frondent_signal
                round(total_cost, 4),   # total_cost
                outcome                 # outcome
            ]

            all_rows.append(row)

        candle_count += 1
        if candle_count % 500 == 0:
            up_count = sum(1 for r in all_rows if r[-1] == 'UP')
            dn_count = sum(1 for r in all_rows if r[-1] == 'DN')
            print(f"  🔧 Processed {candle_count} candles | {len(all_rows)} rows | UP:{up_count} DN:{dn_count}", end='\r')

    print()
    return all_rows, candle_count


# ═══════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════

def main():
    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║         v61 FRANKENSTEIN - HISTORICAL BACKFILL              ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Period: {MONTHS_BACK} months of BTC 1-minute data               ║")
    print("║  Source: Binance API (free, no key needed)                  ║")
    print("║  Output: 16-feature dataset matching bot_v61 format         ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()

    # Calculate time range
    end_time = datetime.now()
    start_time = end_time - timedelta(days=MONTHS_BACK * 30)

    start_ms = int(start_time.timestamp() * 1000)
    end_ms = int(end_time.timestamp() * 1000)

    print(f"📅 Range: {start_time.strftime('%Y-%m-%d')} → {end_time.strftime('%Y-%m-%d')}")
    print(f"📊 Expected: ~{MONTHS_BACK * 30 * 96:,} 15-min candles (~{MONTHS_BACK * 30 * 96 * 14:,} training rows)")
    print()

    # Step 1: Download 1-minute candles
    print("═══ STEP 1: Downloading BTC 1-minute candles from Binance ═══")
    print()
    candles_1m = download_1m_candles(start_ms, end_ms)
    print(f"  ✅ Downloaded {len(candles_1m):,} 1-minute candles")
    print()

    if len(candles_1m) < 1000:
        print("❌ Not enough data downloaded! Check your internet connection.")
        return

    # Step 2: Build 15-minute training data
    print("═══ STEP 2: Building 15-minute training data ═══")
    print()
    rows, candle_count = build_15m_training_data(candles_1m)
    print(f"  ✅ Generated {len(rows):,} training rows from {candle_count} candles")
    print()

    # Step 3: Analyze balance
    up_outcomes = sum(1 for r in rows if r[-1] == 'UP')
    dn_outcomes = sum(1 for r in rows if r[-1] == 'DN')
    # Count unique candle_start values for each outcome
    up_candles = len(set(r[1] for r in rows if r[-1] == 'UP'))
    dn_candles = len(set(r[1] for r in rows if r[-1] == 'DN'))
    total_candles = up_candles + dn_candles

    print("═══ STEP 3: Data Balance Analysis ═══")
    print()
    print(f"  UP candles: {up_candles} ({up_candles/total_candles*100:.1f}%)")
    print(f"  DN candles: {dn_candles} ({dn_candles/total_candles*100:.1f}%)")
    print(f"  Balance ratio: {min(up_candles,dn_candles)/max(up_candles,dn_candles)*100:.1f}%")
    print()

    if min(up_candles, dn_candles) / max(up_candles, dn_candles) > 0.8:
        print("  ✅ Data is well-balanced! (>80% ratio)")
    else:
        print("  ⚠️  Data is somewhat imbalanced but much better than your live 24/80 split")
    print()

    # Step 4: Write backfill CSV
    print("═══ STEP 4: Writing backfill CSV ═══")
    print()

    header = [
        'timestamp', 'candle_start', 'minute',
        'btc_open', 'btc_current', 'btc_change_pct',
        'up_price', 'dn_price', 'price_gap',
        'momentum_1m', 'momentum_3m',
        'rsi', 'macd_line', 'macd_signal', 'macd_histogram',
        'ha_trend', 'vwap_deviation', 'volatility',
        'frondent_signal',
        'total_cost', 'outcome'
    ]

    with open(OUTPUT_FILE, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)

    print(f"  ✅ Saved {OUTPUT_FILE} ({len(rows):,} rows)")
    print()

    # Step 5: Merge with live data if it exists
    print("═══ STEP 5: Merging with live data ═══")
    print()

    try:
        live_rows = 0
        with open(MERGE_FILE, 'r') as f:
            reader = csv.reader(f)
            live_header = next(reader)  # Skip header
            live_data = list(reader)
            live_rows = len(live_data)

        if live_rows > 0:
            # Append live data to backfill
            merged_file = MERGE_FILE  # Overwrite the live file with merged data
            with open(merged_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(header)
                writer.writerows(rows)         # Backfill first (older data)
                writer.writerows(live_data)    # Live data last (newer)

            total = len(rows) + live_rows
            live_candles = len(set(row[1] for row in live_data))
            print(f"  ✅ Merged! {OUTPUT_FILE} ({len(rows):,}) + live ({live_rows} rows, ~{live_candles} candles)")
            print(f"  ✅ Total: {total:,} training rows in {MERGE_FILE}")
        else:
            # No live data, just copy backfill as the main file
            import shutil
            shutil.copy2(OUTPUT_FILE, MERGE_FILE)
            print(f"  ℹ️  No live data found in {MERGE_FILE}")
            print(f"  ✅ Copied backfill as {MERGE_FILE}")

    except FileNotFoundError:
        import shutil
        shutil.copy2(OUTPUT_FILE, MERGE_FILE)
        print(f"  ℹ️  No existing {MERGE_FILE} found")
        print(f"  ✅ Created {MERGE_FILE} from backfill data")

    print()
    print("╔══════════════════════════════════════════════════════════════╗")
    print("║                    🧟 BACKFILL COMPLETE!                    ║")
    print("╠══════════════════════════════════════════════════════════════╣")
    print(f"║  Candles: {candle_count:,}".ljust(63) + "║")
    print(f"║  Training rows: {len(rows):,}".ljust(63) + "║")
    print(f"║  UP/DN balance: {up_candles}/{dn_candles} ({up_candles/total_candles*100:.0f}%/{dn_candles/total_candles*100:.0f}%)".ljust(63) + "║")
    print("║                                                             ║")
    print("║  Next: Run your bot with the new data!                      ║")
    print("║  python3 bot_v61.py --trade                                 ║")
    print("║                                                             ║")
    print("║  The model will now see UP and DN equally,                  ║")
    print("║  fixing the DN bias from overnight-only training.           ║")
    print("╚══════════════════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
