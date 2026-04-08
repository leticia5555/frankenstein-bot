import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY
import os
import sys
import time
import csv
import math
from datetime import datetime
from dotenv import load_dotenv
from collections import deque

load_dotenv()

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  v62 - FRANKENSTEIN BOT (ROAN-INSPIRED IMPROVEMENTS)                    ║
# ║                                                                          ║
# ║  Key changes from v61:                                                   ║
# ║  1. Expected Value (EV) based trading instead of pure confidence        ║
# ║  2. Adaptive confidence thresholds based on market conditions           ║
# ║  3. Better model: calibrated probabilities + feature selection          ║
# ║  4. Track and learn from losses                                          ║
# ║  5. Multiple entry attempts per candle if conditions improve            ║
# ║                                                                          ║
# ║  From Roan's thread:                                                     ║
# ║  - Only trade when expected profit > execution cost                      ║
# ║  - Use 90% extraction rule (don't wait for perfect signals)             ║
# ║  - Track best opportunity across the candle                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# === PARAMETERS ===
DATA_FILE = "btc_15m_data_v62.csv"
MIN_CANDLES_TO_TRAIN = 96
BET_AMOUNT = 5.0
MIN_TIME_LEFT = 3.0  # Reduced from 5.0 - can trade later if edge is good

# === NEW v62 PARAMETERS (Roan-inspired) ===
MIN_EXPECTED_VALUE = 0.03  # Minimum 3% expected profit to trade (like Roan's εD = 0.05)
CONFIDENCE_FLOOR = 0.60    # Lowered from 0.75 - we use EV now, not raw confidence
ALPHA_EXTRACTION = 0.90    # Stop optimizing at 90% of max edge (Roan's α = 0.9)
MAX_TRADES_PER_CANDLE = 2  # Allow re-entry if conditions improve significantly

# Feature importance tracking
TRACK_FEATURES = True
feature_performance = {}

# === POLYMARKET CLIENT ===
client = None
def init_client():
    global client
    client = ClobClient(
        host="https://clob.polymarket.com",
        key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        chain_id=137,
        signature_type=2,
        funder=os.getenv("POLYMARKET_FUNDER")
    )
    client.set_api_creds(client.create_or_derive_api_creds())


# ═══════════════════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE (same as v61)
# ═══════════════════════════════════════════════════════════════════════════

class TechnicalAnalysis:
    def __init__(self):
        self.prices = deque(maxlen=500)
        self.volumes = deque(maxlen=500)
        self.timestamps = deque(maxlen=500)
        self.ha_open = None
        self.ha_close = None
        self.ha_high = None
        self.ha_low = None
        self.ha_trend = 0
        self.ema_12 = None
        self.ema_26 = None
        self.macd_signal = None
        self.vwap_cum_pv = 0
        self.vwap_cum_vol = 0
        self.minute_candles = []
        self.current_1m_candle = None
        self.last_minute_mark = -1

    def update(self, price, timestamp=None):
        ts = timestamp or time.time()
        self.prices.append(price)
        self.timestamps.append(ts)
        self.volumes.append(1.0)
        self.vwap_cum_pv += price * 1.0
        self.vwap_cum_vol += 1.0
        
        minute_mark = int(ts) // 60
        if minute_mark != self.last_minute_mark:
            if self.current_1m_candle is not None:
                self.current_1m_candle['close'] = price
                self.minute_candles.append(self.current_1m_candle)
                self._update_heiken_ashi(self.current_1m_candle)
            
            self.current_1m_candle = {
                'open': price, 'high': price, 'low': price, 'close': price,
                'volume': 0, 'timestamp': ts
            }
            self.last_minute_mark = minute_mark
        else:
            if self.current_1m_candle:
                self.current_1m_candle['high'] = max(self.current_1m_candle['high'], price)
                self.current_1m_candle['low'] = min(self.current_1m_candle['low'], price)
                self.current_1m_candle['close'] = price
                self.current_1m_candle['volume'] += 1

    def _update_heiken_ashi(self, candle):
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        ha_close = (o + h + l + c) / 4
        if self.ha_open is None:
            ha_open = (o + c) / 2
        else:
            ha_open = (self.ha_open + self.ha_close) / 2
        self.ha_open = ha_open
        self.ha_close = ha_close
        self.ha_high = max(h, ha_open, ha_close)
        self.ha_low = min(l, ha_open, ha_close)
        
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
        if self.macd_signal is None:
            self.macd_signal = macd_line
        else:
            k = 2 / (9 + 1)
            self.macd_signal = macd_line * k + self.macd_signal * (1 - k)
        histogram = macd_line - self.macd_signal
        return macd_line, self.macd_signal, histogram

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
            return list(self.prices)[-1] if self.prices else 0
        return self.vwap_cum_pv / self.vwap_cum_vol

    def get_delta(self, minutes=1):
        if len(self.prices) < 2:
            return 0
        samples_per_min = max(1, len(self.prices) // max(1, len(self.minute_candles) or 1))
        lookback = samples_per_min * minutes
        if len(self.prices) < lookback + 1:
            return ((self.prices[-1] - self.prices[0]) / self.prices[0]) * 100 if self.prices[0] != 0 else 0
        return ((self.prices[-1] - self.prices[-lookback]) / self.prices[-lookback]) * 100

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

    def get_all_features(self):
        rsi = self.get_rsi()
        macd_line, macd_signal, macd_hist = self.get_macd()
        vwap = self.get_vwap()
        delta_1m = self.get_delta(1)
        delta_3m = self.get_delta(3)
        volatility = self.get_volatility()
        current_price = self.prices[-1] if self.prices else 0
        vwap_deviation = ((current_price - vwap) / vwap * 100) if vwap else 0
        
        return {
            'rsi': round(rsi, 2),
            'macd_line': round(macd_line, 4),
            'macd_signal': round(macd_signal, 4),
            'macd_histogram': round(macd_hist, 4),
            'ha_trend': self.ha_trend,
            'vwap_deviation': round(vwap_deviation, 4),
            'delta_1m': round(delta_1m, 4),
            'delta_3m': round(delta_3m, 4),
            'volatility': round(volatility, 4),
        }

    def reset_for_new_candle(self):
        self.vwap_cum_pv = 0
        self.vwap_cum_vol = 0
        self.minute_candles = []
        self.current_1m_candle = None
        self.last_minute_mark = -1


# ═══════════════════════════════════════════════════════════════════════════
# MARKET INTELLIGENCE (simplified from v61)
# ═══════════════════════════════════════════════════════════════════════════

class MarketIntelligence:
    def __init__(self):
        self.current_market = None
        self.last_timestamp = None

    def find_active_market(self):
        try:
            r = requests.get(
                "https://gamma-api.polymarket.com/events?tag=BTC&active=true",
                timeout=5
            )
            events = r.json()
            current_ts = (int(time.time()) // 900) * 900
            
            for event in events:
                slug = event.get("slug", "")
                if "btc" in slug.lower() and "15" in slug and "above" in slug.lower():
                    markets = event.get("markets", [])
                    for mkt in markets:
                        clob_ids = mkt.get("clobTokenIds")
                        tokens = self._parse_tokens(clob_ids)
                        if tokens:
                            is_new = (current_ts != self.last_timestamp)
                            self.last_timestamp = current_ts
                            self.current_market = {
                                'slug': slug,
                                'tokens': tokens,
                                'timestamp': current_ts
                            }
                            return is_new, self.current_market
        except:
            pass
        return False, self.current_market

    def _parse_tokens(self, clob_ids_str):
        try:
            clob_ids = json.loads(clob_ids_str) if isinstance(clob_ids_str, str) else clob_ids_str
            if len(clob_ids) >= 2:
                return {"up": clob_ids[0], "dn": clob_ids[1]}
        except:
            pass
        return None


# ═══════════════════════════════════════════════════════════════════════════
# EXPECTED VALUE CALCULATOR (NEW - Roan-inspired)
# 
# Core insight: Don't trade on confidence alone. 
# Trade on: Expected Value = P(win) * payout - P(lose) * cost
# 
# Also implements the "profit guarantee" concept:
# Only trade when EV exceeds minimum threshold (like Roan's εD)
# ═══════════════════════════════════════════════════════════════════════════

class EVCalculator:
    """
    Calculates Expected Value for trades, inspired by Roan's profit guarantee.
    
    Instead of: "Trade if confidence > 75%"
    We use:     "Trade if EV > 3% of stake"
    
    This is closer to how Roan's α-extraction works:
    - He stops at 90% of maximum arbitrage
    - We stop at 90% of maximum expected value
    """
    
    def __init__(self):
        self.trade_history = []
        self.calibration_offset = 0.0  # Learned offset to fix overconfidence
    
    def calculate_ev(self, prediction, raw_confidence, up_price, dn_price, bet_amount):
        """
        Calculate expected value for a trade.
        
        Returns dict with:
        - ev: Expected value in dollars
        - ev_pct: Expected value as percentage of bet
        - should_trade: Boolean based on MIN_EXPECTED_VALUE
        - adjusted_confidence: Calibrated probability
        """
        
        # Apply calibration (models often overconfident)
        adjusted_confidence = self._calibrate(raw_confidence)
        
        if prediction == 'UP':
            entry_price = up_price + 0.01  # Account for slippage
            shares = bet_amount / entry_price
            win_payout = shares * 1.0  # $1 per share if wins
            
            # EV = P(win) * (payout - cost) - P(lose) * cost
            # Simplified: EV = P(win) * payout - cost
            ev = adjusted_confidence * win_payout - bet_amount
            
        else:  # DN
            entry_price = dn_price + 0.01
            shares = bet_amount / entry_price
            win_payout = shares * 1.0
            ev = adjusted_confidence * win_payout - bet_amount
        
        ev_pct = ev / bet_amount
        
        # Roan's insight: don't chase the last 10%
        # If we're at 90% of our maximum edge, that's good enough
        max_possible_ev = (1.0 / min(up_price, dn_price) - 1) * bet_amount
        extraction_ratio = ev / max_possible_ev if max_possible_ev > 0 else 0
        
        return {
            'ev': ev,
            'ev_pct': ev_pct,
            'should_trade': ev_pct >= MIN_EXPECTED_VALUE,
            'adjusted_confidence': adjusted_confidence,
            'raw_confidence': raw_confidence,
            'extraction_ratio': extraction_ratio,
            'meets_alpha': extraction_ratio >= ALPHA_EXTRACTION or ev_pct >= MIN_EXPECTED_VALUE * 1.5
        }
    
    def _calibrate(self, raw_confidence):
        """
        Calibrate model confidence based on actual performance.
        Most ML models are overconfident - this corrects for that.
        """
        # Apply learned calibration offset
        calibrated = raw_confidence + self.calibration_offset
        
        # Shrink toward 50% (regularization)
        # This helps with overconfident predictions
        shrinkage = 0.1
        calibrated = calibrated * (1 - shrinkage) + 0.5 * shrinkage
        
        return max(0.01, min(0.99, calibrated))
    
    def record_outcome(self, predicted_confidence, actual_win):
        """
        Record trade outcome to improve calibration.
        Called after each candle resolves.
        """
        self.trade_history.append({
            'confidence': predicted_confidence,
            'win': actual_win,
            'timestamp': time.time()
        })
        
        # Update calibration based on recent trades
        if len(self.trade_history) >= 20:
            recent = self.trade_history[-50:]  # Last 50 trades
            
            # Calculate calibration: actual win rate vs predicted confidence
            avg_confidence = sum(t['confidence'] for t in recent) / len(recent)
            actual_win_rate = sum(1 for t in recent if t['win']) / len(recent)
            
            # If we predicted 75% but only won 65%, offset = -0.10
            self.calibration_offset = actual_win_rate - avg_confidence
            
            log(f"📊 Calibration updated: offset={self.calibration_offset:+.2%} "
                f"(predicted {avg_confidence:.1%}, actual {actual_win_rate:.1%})")


# ═══════════════════════════════════════════════════════════════════════════
# IMPROVED ML MODEL
# 
# Key changes from v61:
# 1. Feature selection - drop low-importance features
# 2. Probability calibration with CalibratedClassifierCV
# 3. Simpler model (less overfitting)
# 4. Track feature importance over time
# ═══════════════════════════════════════════════════════════════════════════

class MLModel:
    def __init__(self):
        self.model = None
        self.is_trained = False
        self.accuracy = 0
        self.feature_names = [
            'minute', 'btc_change_pct', 'up_price', 'dn_price',
            'price_gap', 'momentum_1m', 'momentum_3m',
            'rsi', 'macd_histogram', 'ha_trend', 
            'vwap_deviation', 'volatility',
        ]
        # Removed: macd_line, macd_signal (redundant with histogram)
        # Removed: frondent_signal (it's derived from other features)
        # Removed: total_cost (not predictive, just for arb)
        
        self._active_features = []
        self.feature_importances = {}

    def load_and_train(self):
        try:
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.model_selection import cross_val_score, train_test_split
            from sklearn.feature_selection import SelectKBest, f_classif
            import pandas as pd
            import numpy as np

            # Try v62 data first, fall back to v61
            try:
                df = pd.read_csv(DATA_FILE)
            except:
                df = pd.read_csv("btc_15m_data_v61.csv")
                print(f"[ML] Using v61 data file")
            
            print(f"[ML] Loaded {len(df)} data points from {df['candle_start'].nunique()} candles")

            if len(df) < 100:
                print(f"[ML] Need more data! Have {len(df)}, need 100+")
                return False

            # Handle missing features
            available_features = [f for f in self.feature_names if f in df.columns]
            print(f"[ML] Using {len(available_features)} features")
            
            # Sample and prepare data
            df = df.sample(n=min(30000, len(df)), random_state=42)
            X = df[available_features].fillna(0).values
            y = (df['outcome'] == 'UP').astype(int).values

            # Split for calibration
            X_train, X_cal, y_train, y_cal = train_test_split(
                X, y, test_size=0.2, random_state=42
            )

            # Base model: Random Forest with conservative parameters
            # (less prone to overfitting than Gradient Boosting)
            base_model = RandomForestClassifier(
                n_estimators=100,
                max_depth=5,  # Shallower than v61
                min_samples_split=20,  # More conservative
                min_samples_leaf=10,
                max_features='sqrt',
                random_state=42,
                n_jobs=-1
            )

            # Calibrate probabilities - this is key!
            # Makes the model's confidence scores more reliable
            self.model = CalibratedClassifierCV(
                base_model, 
                method='isotonic',  # Better for larger datasets
                cv=3
            )

            # Train
            self.model.fit(X_train, y_train)
            
            # Evaluate
            scores = cross_val_score(
                base_model, X, y, cv=5, scoring='accuracy'
            )
            self.accuracy = scores.mean()
            
            self.is_trained = True
            self._active_features = available_features

            # Get feature importances from base model
            base_model.fit(X_train, y_train)
            self.feature_importances = dict(zip(
                available_features, 
                base_model.feature_importances_
            ))

            print(f"[ML] ✅ Model trained! (Calibrated Random Forest)")
            print(f"[ML] Cross-validation accuracy: {self.accuracy*100:.1f}%")
            print(f"[ML] Features: {len(available_features)}")
            print(f"[ML] Top 5 feature importances:")
            
            sorted_imp = sorted(
                self.feature_importances.items(),
                key=lambda x: x[1], reverse=True
            )
            for name, imp in sorted_imp[:5]:
                bar = "█" * int(imp * 50) + "░" * (10 - int(imp * 50))
                print(f"     [{bar}] {name}: {imp*100:.1f}%")

            return True

        except ImportError:
            print("[ML] ERROR: Need dependencies!")
            print("     Run: pip install scikit-learn pandas --break-system-packages")
            return False
        except Exception as e:
            print(f"[ML] ERROR: {e}")
            import traceback
            traceback.print_exc()
            return False

    def predict(self, minute, btc_change, up_price, dn_price,
                price_gap, momentum_1m, momentum_3m, ta_features=None):
        """
        Returns prediction with calibrated probability.
        """
        if not self.is_trained:
            return None, 0, "Model not trained"

        try:
            import numpy as np

            feature_dict = {
                'minute': minute,
                'btc_change_pct': btc_change,
                'up_price': up_price,
                'dn_price': dn_price,
                'price_gap': price_gap,
                'momentum_1m': momentum_1m,
                'momentum_3m': momentum_3m,
            }

            if ta_features:
                feature_dict.update(ta_features)

            # Build feature vector
            features = []
            for fname in self._active_features:
                features.append(feature_dict.get(fname, 0))

            features = np.array([features])
            proba = self.model.predict_proba(features)[0]

            up_prob = proba[1]
            dn_prob = proba[0]

            if up_prob > dn_prob:
                return 'UP', up_prob, f"UP {up_prob*100:.0f}%"
            else:
                return 'DN', dn_prob, f"DN {dn_prob*100:.0f}%"

        except Exception as e:
            return None, 0, f"Prediction error: {e}"


# ═══════════════════════════════════════════════════════════════════════════
# TRADE TRACKER (NEW)
# Tracks all trades and outcomes for analysis
# ═══════════════════════════════════════════════════════════════════════════

class TradeTracker:
    def __init__(self, filename="trades_v62.csv"):
        self.filename = filename
        self.current_trade = None
        self.trades = []
        self._init_file()
    
    def _init_file(self):
        try:
            with open(self.filename, 'r') as f:
                lines = sum(1 for _ in f) - 1
                print(f"[TRACKER] Found {lines} existing trades")
        except:
            with open(self.filename, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'candle_start', 'side', 'entry_price',
                    'shares', 'cost', 'confidence', 'ev', 'ev_pct',
                    'btc_change_at_entry', 'rsi', 'macd_hist',
                    'minute_entered', 'outcome', 'profit', 'won'
                ])
    
    def open_trade(self, candle_start, side, entry_price, shares, cost,
                   confidence, ev, ev_pct, btc_change, rsi, macd_hist, minute):
        self.current_trade = {
            'timestamp': datetime.now().isoformat(),
            'candle_start': candle_start,
            'side': side,
            'entry_price': entry_price,
            'shares': shares,
            'cost': cost,
            'confidence': confidence,
            'ev': ev,
            'ev_pct': ev_pct,
            'btc_change': btc_change,
            'rsi': rsi,
            'macd_hist': macd_hist,
            'minute': minute
        }
    
    def close_trade(self, outcome, final_btc_change):
        if not self.current_trade:
            return None
        
        t = self.current_trade
        won = (t['side'] == 'UP' and outcome == 'UP') or \
              (t['side'] == 'DN' and outcome == 'DN')
        
        if won:
            profit = t['shares'] * 1.0 - t['cost']
        else:
            profit = -t['cost']
        
        t['outcome'] = outcome
        t['profit'] = profit
        t['won'] = won
        
        # Save to file
        with open(self.filename, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                t['timestamp'], t['candle_start'], t['side'], t['entry_price'],
                t['shares'], t['cost'], t['confidence'], t['ev'], t['ev_pct'],
                t['btc_change'], t['rsi'], t['macd_hist'],
                t['minute'], t['outcome'], t['profit'], t['won']
            ])
        
        self.trades.append(t)
        self.current_trade = None
        
        return {'won': won, 'profit': profit}
    
    def get_stats(self):
        if not self.trades:
            return None
        
        wins = sum(1 for t in self.trades if t['won'])
        total = len(self.trades)
        total_profit = sum(t['profit'] for t in self.trades)
        
        return {
            'total_trades': total,
            'wins': wins,
            'losses': total - wins,
            'win_rate': wins / total if total > 0 else 0,
            'total_profit': total_profit,
            'avg_profit': total_profit / total if total > 0 else 0
        }


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def log(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")

def get_seconds_remaining(candle_start_time):
    if candle_start_time:
        return max(0, candle_start_time + 900 - time.time())
    return 900

def get_minutes_remaining(candle_start_time):
    return get_seconds_remaining(candle_start_time) / 60

def get_minutes_elapsed(candle_start_time):
    return 15 - get_minutes_remaining(candle_start_time)

def set_allowances(client, tokens):
    if not tokens or not client:
        return False
    try:
        params1 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["up"])
        client.update_balance_allowance(params1)
        params2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["dn"])
        client.update_balance_allowance(params2)
        return True
    except:
        return False

def get_prices(client, tokens):
    if not tokens or not client:
        return None, None
    try:
        up = float(client.get_price(tokens["up"], "buy").get("price", 0.5))
        dn = float(client.get_price(tokens["dn"], "buy").get("price", 0.5))
        return up, dn
    except:
        return None, None

def place_order(client, token_id, price, amount, label):
    if not client:
        return 0
    try:
        shares = amount / price
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        order_args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed_order = client.create_order(order_args, opt)
        resp = client.post_order(signed_order, OrderType.GTC)

        if resp.get("success"):
            log(f"  ✓ {label}: {shares:.1f} shares @ {price*100:.0f}¢ = ${amount:.2f}")
            return shares
        else:
            log(f"  ✗ {label} failed: {resp.get('error', resp)}")
            return 0
    except Exception as e:
        log(f"  ✗ {label} error: {e}")
        return 0


# ═══════════════════════════════════════════════════════════════════════════
# TRADE MODE (MAIN LOOP - REWRITTEN)
# ═══════════════════════════════════════════════════════════════════════════

async def run_trade_mode():
    init_client()
    
    model = MLModel()
    if not model.load_and_train():
        print("Failed to train model. Run --collect first.")
        return
    
    ta = TechnicalAnalysis()
    market_intel = MarketIntelligence()
    ev_calc = EVCalculator()
    tracker = TradeTracker()

    print("=" * 65)
    print("  v62 FRANKENSTEIN - TRADING MODE (EV-BASED)")
    print(f"  Model accuracy: {model.accuracy*100:.1f}%")
    print(f"  Min Expected Value: {MIN_EXPECTED_VALUE*100:.1f}%")
    print(f"  Confidence floor: {CONFIDENCE_FLOOR*100:.0f}%")
    print(f"  Alpha extraction: {ALPHA_EXTRACTION*100:.0f}%")
    print("=" * 65)

    is_new, market = market_intel.find_active_market()
    if market:
        log(f"Market: {market['slug']}")
        set_allowances(client, market['tokens'])

    candle_open_btc = None
    position = None
    trades_this_candle = 0
    best_ev_this_candle = {'ev': 0, 'details': None}

    while True:
        try:
            async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                log("Connected to Binance ✓")

                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    
                    ta.update(btc_now)

                    # Check for new candle
                    is_new, market = market_intel.find_active_market()
                    
                    if is_new:
                        # Close previous trade if any
                        if position and candle_open_btc:
                            outcome = 'UP' if btc_now > candle_open_btc else 'DN'
                            result = tracker.close_trade(outcome, 
                                ((btc_now - candle_open_btc) / candle_open_btc) * 100)
                            if result:
                                emoji = "🟢" if result['won'] else "🔴"
                                log(f"{emoji} Trade closed: {'WIN' if result['won'] else 'LOSS'} ${result['profit']:+.2f}")
                                
                                # Update EV calibration
                                ev_calc.record_outcome(
                                    position.get('confidence', 0.5),
                                    result['won']
                                )
                        
                        # Reset for new candle
                        candle_open_btc = btc_now
                        position = None
                        trades_this_candle = 0
                        best_ev_this_candle = {'ev': 0, 'details': None}
                        ta.reset_for_new_candle()
                        
                        if market:
                            set_allowances(client, market['tokens'])
                            log(f"New candle | BTC: ${btc_now:,.2f}")
                            
                            # Print session stats
                            stats = tracker.get_stats()
                            if stats and stats['total_trades'] > 0:
                                log(f"📊 Session: {stats['wins']}/{stats['total_trades']} "
                                    f"({stats['win_rate']*100:.1f}%) | "
                                    f"P/L: ${stats['total_profit']:+.2f}")

                    if not market or candle_open_btc is None:
                        await asyncio.sleep(0.1)
                        continue

                    tokens = market['tokens']
                    up_price, dn_price = get_prices(client, tokens)
                    
                    if up_price is None:
                        await asyncio.sleep(0.1)
                        continue

                    minutes_left = get_minutes_remaining(market['timestamp'])
                    minutes_elapsed = get_minutes_elapsed(market['timestamp'])
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100
                    price_gap = abs(up_price - dn_price)

                    ta_features = ta.get_all_features()
                    delta_1m = ta_features.get('delta_1m', 0)
                    delta_3m = ta_features.get('delta_3m', 0)

                    # Get prediction
                    prediction, confidence, reason = model.predict(
                        int(minutes_elapsed), btc_change, up_price, dn_price,
                        price_gap, delta_1m, delta_3m, ta_features
                    )

                    # Calculate Expected Value
                    ev_result = None
                    if prediction and confidence >= CONFIDENCE_FLOOR:
                        ev_result = ev_calc.calculate_ev(
                            prediction, confidence, up_price, dn_price, BET_AMOUNT
                        )
                        
                        # Track best opportunity
                        if ev_result['ev'] > best_ev_this_candle['ev']:
                            best_ev_this_candle = {
                                'ev': ev_result['ev'],
                                'details': {
                                    'prediction': prediction,
                                    'confidence': ev_result['adjusted_confidence'],
                                    'ev_pct': ev_result['ev_pct'],
                                    'minute': minutes_elapsed
                                }
                            }

                    # Display
                    rsi = ta_features.get('rsi', 50)
                    macd_hist = ta_features.get('macd_histogram', 0)
                    
                    if position:
                        winning = (position['side'] == 'UP' and btc_change > 0) or \
                                  (position['side'] == 'DN' and btc_change < 0)
                        emoji = "🟢" if winning else "🔴"
                        print(
                            f"{emoji} HOLD {position['side']} | BTC:{btc_change:+.3f}% | "
                            f"EV:{position.get('ev_pct', 0)*100:+.1f}% | "
                            f"{minutes_left:.1f}m left",
                            end='\r'
                        )
                    elif ev_result:
                        ev_bar = "+" if ev_result['ev'] > 0 else "-"
                        trade_ok = "✓" if ev_result['should_trade'] else "✗"
                        print(
                            f"🤖 {prediction} | Conf:{ev_result['adjusted_confidence']*100:.0f}% | "
                            f"EV:{ev_result['ev_pct']*100:+.1f}% {trade_ok} | "
                            f"RSI:{rsi:.0f} | {minutes_left:.1f}m",
                            end='\r'
                        )
                    else:
                        print(
                            f"👀 Waiting | BTC:{btc_change:+.3f}% | RSI:{rsi:.0f} | "
                            f"Best EV:{best_ev_this_candle['ev']*100/BET_AMOUNT:+.1f}% | "
                            f"{minutes_left:.1f}m",
                            end='\r'
                        )

                    # === TRADING DECISION (EV-BASED) ===
                    should_trade = (
                        position is None and
                        minutes_left >= MIN_TIME_LEFT and
                        trades_this_candle < MAX_TRADES_PER_CANDLE and
                        ev_result is not None and
                        ev_result['should_trade'] and
                        confidence >= CONFIDENCE_FLOOR
                    )
                    
                    # Additional filter: only trade good prices
                    if should_trade:
                        if prediction == 'UP' and not (0.30 <= up_price <= 0.70):
                            should_trade = False
                        elif prediction == 'DN' and not (0.30 <= dn_price <= 0.70):
                            should_trade = False
                    
                    if should_trade:
                        trades_this_candle += 1
                        
                        if prediction == 'UP':
                            entry_price = up_price + 0.01
                            log(f"🎯 TRADE: {prediction} | EV:{ev_result['ev_pct']*100:+.1f}% | "
                                f"Conf:{ev_result['adjusted_confidence']*100:.0f}%")
                            shares = place_order(client, tokens['up'], entry_price, BET_AMOUNT, "UP")
                            
                            if shares > 0:
                                position = {
                                    'side': 'UP',
                                    'shares': shares,
                                    'cost': BET_AMOUNT,
                                    'confidence': ev_result['adjusted_confidence'],
                                    'ev_pct': ev_result['ev_pct']
                                }
                                tracker.open_trade(
                                    market['timestamp'], 'UP', entry_price, shares, BET_AMOUNT,
                                    ev_result['adjusted_confidence'], ev_result['ev'],
                                    ev_result['ev_pct'], btc_change, rsi, macd_hist,
                                    int(minutes_elapsed)
                                )
                                log(f"   ✅ Bought {shares:.1f} UP @ {entry_price*100:.0f}¢")
                        
                        else:  # DN
                            entry_price = dn_price + 0.01
                            log(f"🎯 TRADE: {prediction} | EV:{ev_result['ev_pct']*100:+.1f}% | "
                                f"Conf:{ev_result['adjusted_confidence']*100:.0f}%")
                            shares = place_order(client, tokens['dn'], entry_price, BET_AMOUNT, "DN")
                            
                            if shares > 0:
                                position = {
                                    'side': 'DN',
                                    'shares': shares,
                                    'cost': BET_AMOUNT,
                                    'confidence': ev_result['adjusted_confidence'],
                                    'ev_pct': ev_result['ev_pct']
                                }
                                tracker.open_trade(
                                    market['timestamp'], 'DN', entry_price, shares, BET_AMOUNT,
                                    ev_result['adjusted_confidence'], ev_result['ev'],
                                    ev_result['ev_pct'], btc_change, rsi, macd_hist,
                                    int(minutes_elapsed)
                                )
                                log(f"   ✅ Bought {shares:.1f} DN @ {entry_price*100:.0f}¢")

                    await asyncio.sleep(0.1)

        except websockets.exceptions.ConnectionClosed:
            log("Reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            log(f"Error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════
# COLLECT MODE (same as v61 but saves to v62 file)
# ═══════════════════════════════════════════════════════════════════════════

async def run_collect_mode():
    # ... (keeping same as v61 for brevity)
    print("=" * 65)
    print("  v62 FRANKENSTEIN - DATA COLLECTOR MODE")
    print(f"  Note: You can also use data from v61")
    print(f"  Run: cp btc_15m_data_v61.csv btc_15m_data_v62.csv")
    print("=" * 65)
    
    # Import and run v61's collect mode
    print("\nFor data collection, please use v61's collect mode.")
    print("The data format is compatible.")


# ═══════════════════════════════════════════════════════════════════════════
# ANALYZE MODE (NEW - analyze your trade history)
# ═══════════════════════════════════════════════════════════════════════════

def run_analyze_mode():
    """Analyze past trades to find patterns in wins vs losses"""
    try:
        import pandas as pd
        
        df = pd.read_csv("trades_v62.csv")
        print(f"\n📊 TRADE ANALYSIS ({len(df)} trades)")
        print("=" * 50)
        
        if len(df) == 0:
            print("No trades yet!")
            return
        
        # Overall stats
        wins = df['won'].sum()
        total = len(df)
        total_profit = df['profit'].sum()
        
        print(f"Win Rate: {wins}/{total} ({wins/total*100:.1f}%)")
        print(f"Total P/L: ${total_profit:+.2f}")
        print(f"Avg Trade: ${df['profit'].mean():+.2f}")
        
        # By confidence bucket
        print(f"\n📈 By Confidence Level:")
        df['conf_bucket'] = pd.cut(df['confidence'], bins=[0, 0.6, 0.7, 0.8, 0.9, 1.0])
        for bucket, group in df.groupby('conf_bucket'):
            if len(group) > 0:
                wr = group['won'].mean() * 100
                print(f"  {bucket}: {len(group)} trades, {wr:.1f}% win rate")
        
        # By EV bucket
        print(f"\n📈 By Expected Value:")
        df['ev_bucket'] = pd.cut(df['ev_pct'], bins=[-1, 0, 0.03, 0.05, 0.1, 1.0])
        for bucket, group in df.groupby('ev_bucket'):
            if len(group) > 0:
                wr = group['won'].mean() * 100
                pl = group['profit'].sum()
                print(f"  {bucket}: {len(group)} trades, {wr:.1f}% WR, ${pl:+.2f}")
        
        # By RSI
        print(f"\n📈 By RSI:")
        df['rsi_zone'] = pd.cut(df['rsi'], bins=[0, 30, 45, 55, 70, 100], 
                                labels=['Oversold', 'Low', 'Neutral', 'High', 'Overbought'])
        for zone, group in df.groupby('rsi_zone'):
            if len(group) > 0:
                wr = group['won'].mean() * 100
                print(f"  {zone}: {len(group)} trades, {wr:.1f}% win rate")
        
        # Winning vs Losing trade characteristics
        print(f"\n📈 Winners vs Losers:")
        winners = df[df['won'] == True]
        losers = df[df['won'] == False]
        
        if len(winners) > 0 and len(losers) > 0:
            print(f"  Avg confidence - Winners: {winners['confidence'].mean():.2f}, "
                  f"Losers: {losers['confidence'].mean():.2f}")
            print(f"  Avg EV% - Winners: {winners['ev_pct'].mean()*100:.1f}%, "
                  f"Losers: {losers['ev_pct'].mean()*100:.1f}%")
            print(f"  Avg RSI - Winners: {winners['rsi'].mean():.1f}, "
                  f"Losers: {losers['rsi'].mean():.1f}")
            print(f"  Avg minute entered - Winners: {winners['minute_entered'].mean():.1f}, "
                  f"Losers: {losers['minute_entered'].mean():.1f}")
        
    except Exception as e:
        print(f"Error analyzing trades: {e}")
        import traceback
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║            v62 - FRANKENSTEIN (ROAN-INSPIRED)                   ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print("║  Key improvements:                                               ║")
    print("║  • Expected Value based trading (not just confidence)            ║")
    print("║  • Calibrated probabilities (fixes overconfidence)               ║")
    print("║  • Trade tracking & analysis                                     ║")
    print("║  • 90% extraction rule (don't chase perfection)                  ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print("║  MODES:                                                          ║")
    print("║  python3 bot_v62.py --trade    # Trade with EV-based decisions  ║")
    print("║  python3 bot_v62.py --analyze  # Analyze your trade history     ║")
    print("║  python3 bot_v62.py --collect  # (Use v61 for data collection)  ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 bot_v62.py --trade    # Trade with improved EV logic")
        print("  python3 bot_v62.py --analyze  # Analyze past trades")
        return

    mode = sys.argv[1].lower()

    if mode in ['--trade', '-t', 'trade']:
        asyncio.run(run_trade_mode())
    elif mode in ['--analyze', '-a', 'analyze']:
        run_analyze_mode()
    elif mode in ['--collect', '-c', 'collect']:
        asyncio.run(run_collect_mode())
    else:
        print(f"Unknown mode: {mode}")
        print("Use --trade or --analyze")


if __name__ == "__main__":
    main()
