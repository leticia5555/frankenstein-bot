import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY
import os
import sys
import time
import csv
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# === v60 - MACHINE LEARNING BOT ===
#
# TWO MODES:
#
# 1. COLLECT MODE (run for 24-48 hours):
#    python3 bot_v60.py --collect
#    - Records every candle to CSV
#    - No trading, just watching
#    - Leave running overnight
#
# 2. TRADE MODE (after collecting data):
#    python3 bot_v60.py --trade
#    - Loads CSV data
#    - Trains ML model (Random Forest)
#    - Trades based on predictions
#    - Keeps learning from new data
#

# === PARAMETERS ===
DATA_FILE = "btc_15m_data.csv"
MIN_CANDLES_TO_TRAIN = 96      # 24 hours of data minimum
MIN_CONFIDENCE = 0.65           # Only trade if 65%+ confident
BET_AMOUNT = 15.0               # $ per trade
MIN_TIME_LEFT = 5.0             # Need 5+ min left to trade

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

# === DATA STORAGE ===
class DataCollector:
    def __init__(self):
        self.current_candle = None
        self.candles_collected = 0
        self.load_existing_count()
    
    def load_existing_count(self):
        """Count existing rows in CSV"""
        try:
            with open(DATA_FILE, 'r') as f:
                self.candles_collected = sum(1 for line in f) - 1  # Minus header
                if self.candles_collected < 0:
                    self.candles_collected = 0
            print(f"[DATA] Found {self.candles_collected} existing candles")
        except:
            self.candles_collected = 0
            # Create file with header
            with open(DATA_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'candle_start', 'minute', 
                    'btc_open', 'btc_current', 'btc_change_pct',
                    'up_price', 'dn_price', 'price_gap',
                    'momentum_1m', 'momentum_3m',
                    'outcome'
                ])
            print("[DATA] Created new data file")
    
    def start_candle(self, timestamp, btc_open):
        """Start tracking a new candle"""
        self.current_candle = {
            'timestamp': timestamp,
            'btc_open': btc_open,
            'snapshots': [],
            'last_btc': btc_open,
            'btc_history': [btc_open]
        }
    
    def record_snapshot(self, minute, btc_current, up_price, dn_price):
        """Record a snapshot during the candle"""
        if not self.current_candle:
            return
        
        btc_open = self.current_candle['btc_open']
        btc_change = ((btc_current - btc_open) / btc_open) * 100
        price_gap = abs(up_price - dn_price)
        
        # Momentum calculations
        self.current_candle['btc_history'].append(btc_current)
        history = self.current_candle['btc_history']
        
        # 1-minute momentum (change from 1 min ago)
        if len(history) >= 2:
            momentum_1m = ((btc_current - history[-2]) / history[-2]) * 100
        else:
            momentum_1m = 0
        
        # 3-minute momentum (change from 3 min ago)
        if len(history) >= 4:
            momentum_3m = ((btc_current - history[-4]) / history[-4]) * 100
        else:
            momentum_3m = 0
        
        snapshot = {
            'minute': minute,
            'btc_current': btc_current,
            'btc_change': btc_change,
            'up_price': up_price,
            'dn_price': dn_price,
            'price_gap': price_gap,
            'momentum_1m': momentum_1m,
            'momentum_3m': momentum_3m
        }
        
        self.current_candle['snapshots'].append(snapshot)
        self.current_candle['last_btc'] = btc_current
    
    def end_candle(self, final_btc):
        """End candle and save all snapshots to CSV"""
        if not self.current_candle:
            return
        
        btc_open = self.current_candle['btc_open']
        outcome = 'UP' if final_btc > btc_open else 'DN'
        
        # Save each snapshot as a row
        with open(DATA_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            for snap in self.current_candle['snapshots']:
                writer.writerow([
                    datetime.now().isoformat(),
                    self.current_candle['timestamp'],
                    snap['minute'],
                    btc_open,
                    snap['btc_current'],
                    round(snap['btc_change'], 4),
                    round(snap['up_price'], 4),
                    round(snap['dn_price'], 4),
                    round(snap['price_gap'], 4),
                    round(snap['momentum_1m'], 4),
                    round(snap['momentum_3m'], 4),
                    outcome
                ])
        
        self.candles_collected += 1
        print(f"\n[DATA] Saved candle #{self.candles_collected}: {outcome} won | {len(self.current_candle['snapshots'])} snapshots")
        self.current_candle = None


# === ML MODEL ===
class MLModel:
    def __init__(self):
        self.model = None
        self.is_trained = False
        self.accuracy = 0
        self.feature_names = [
            'minute', 'btc_change_pct', 'up_price', 'dn_price', 
            'price_gap', 'momentum_1m', 'momentum_3m'
        ]
    
    def load_and_train(self):
        """Load data and train the model"""
        try:
            # Import ML libraries
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.model_selection import cross_val_score
            import pandas as pd
            import numpy as np
            
            # Load data
            df = pd.read_csv(DATA_FILE)
            print(f"[ML] Loaded {len(df)} data points from {df['candle_start'].nunique()} candles")
            
            if len(df) < 100:
                print(f"[ML] Need more data! Have {len(df)}, need 100+")
                return False
            
            # Prepare features
            X = df[self.feature_names].values
            y = (df['outcome'] == 'UP').astype(int).values  # 1 = UP, 0 = DN
            
            # Train Random Forest
            self.model = RandomForestClassifier(
                n_estimators=100,
                max_depth=10,
                min_samples_split=5,
                min_samples_leaf=2,
                random_state=42,
                n_jobs=-1
            )
            
            # Cross-validation to check accuracy
            scores = cross_val_score(self.model, X, y, cv=5, scoring='accuracy')
            self.accuracy = scores.mean()
            
            # Train on all data
            self.model.fit(X, y)
            self.is_trained = True
            
            print(f"[ML] Model trained!")
            print(f"[ML] Cross-validation accuracy: {self.accuracy*100:.1f}%")
            print(f"[ML] Feature importances:")
            for name, importance in sorted(zip(self.feature_names, self.model.feature_importances_), 
                                          key=lambda x: x[1], reverse=True):
                print(f"     {name}: {importance*100:.1f}%")
            
            return True
            
        except ImportError:
            print("[ML] ERROR: Need to install scikit-learn and pandas!")
            print("     Run: pip install scikit-learn pandas --break-system-packages")
            return False
        except Exception as e:
            print(f"[ML] ERROR: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def predict(self, minute, btc_change, up_price, dn_price, price_gap, momentum_1m, momentum_3m):
        """Predict outcome with probability"""
        if not self.is_trained:
            return None, 0, "Model not trained"
        
        try:
            import numpy as np
            
            features = np.array([[
                minute, btc_change, up_price, dn_price,
                price_gap, momentum_1m, momentum_3m
            ]])
            
            # Get probability
            proba = self.model.predict_proba(features)[0]
            
            # proba[0] = P(DN), proba[1] = P(UP)
            up_prob = proba[1]
            dn_prob = proba[0]
            
            if up_prob > dn_prob:
                return 'UP', up_prob, f"UP {up_prob*100:.0f}% (RF)"
            else:
                return 'DN', dn_prob, f"DN {dn_prob*100:.0f}% (RF)"
                
        except Exception as e:
            return None, 0, f"Prediction error: {e}"


# === TRADING STATE ===
SLUG = None
tokens = None
candle_start_time = None
candle_open_btc = None
last_snapshot_minute = -1
position = None
btc_history = []

def log(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")

def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900

def get_seconds_remaining():
    if candle_start_time:
        end_ts = candle_start_time + 900
        return max(0, end_ts - time.time())
    return 900

def get_minutes_remaining():
    return get_seconds_remaining() / 60

def get_minutes_elapsed():
    return 15 - get_minutes_remaining()

def find_active_market():
    global SLUG, tokens, candle_start_time
    
    current_ts = get_current_market_timestamp()
    
    for offset in [0, 900, -900]:
        timestamp = current_ts + offset
        expected_slug = f"btc-updown-15m-{timestamp}"
        
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/events?slug={expected_slug}",
                timeout=5
            )
            data = r.json()
            
            if data and len(data) > 0:
                event = data[0]
                markets = event.get("markets", [])
                if markets:
                    market = markets[0]
                    if market.get("acceptingOrders") and not market.get("closed"):
                        if expected_slug != SLUG:
                            SLUG = expected_slug
                            candle_start_time = timestamp
                            clob_ids_str = market.get("clobTokenIds", "")
                            if clob_ids_str:
                                try:
                                    clob_ids = json.loads(clob_ids_str) if isinstance(clob_ids_str, str) else clob_ids_str
                                    if len(clob_ids) >= 2:
                                        tokens = {"up": clob_ids[0], "dn": clob_ids[1]}
                                except:
                                    tokens = None
                            return True
                        return False
        except:
            pass
    return False

def set_allowances():
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

def get_prices():
    if not tokens or not client:
        return None, None
    try:
        up = float(client.get_price(tokens["up"], "buy").get("price", 0.5))
        dn = float(client.get_price(tokens["dn"], "buy").get("price", 0.5))
        return up, dn
    except:
        return None, None

def place_order(token_id, price, amount, label):
    """Place a GTC limit order"""
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


# === COLLECT MODE ===
async def run_collect_mode():
    global SLUG, tokens, candle_start_time, candle_open_btc
    global last_snapshot_minute, btc_history
    
    collector = DataCollector()
    
    print("=" * 60)
    print("v60 - DATA COLLECTOR MODE")
    print(f"Collecting data to: {DATA_FILE}")
    print(f"Already have: {collector.candles_collected} candles")
    print(f"Need: {MIN_CANDLES_TO_TRAIN} candles minimum")
    print("Leave running for 24-48 hours!")
    print("=" * 60)
    
    find_active_market()
    if SLUG:
        log(f"Market: {SLUG}")
    
    while True:
        try:
            async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                log("Connected to Binance ✓")
                
                tick_count = 0
                last_check = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    tick_count += 1
                    
                    # Set candle open
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        btc_history = [btc_now]
                        collector.start_candle(candle_start_time, btc_now)
                        log(f"Candle open: ${candle_open_btc:,.2f}")
                    
                    # Check for new market
                    if tick_count % 100 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
                                # End old candle
                                collector.end_candle(btc_now)
                                
                                # Reset for new candle
                                candle_open_btc = btc_now
                                btc_history = [btc_now]
                                last_snapshot_minute = -1
                                collector.start_candle(candle_start_time, btc_now)
                                
                                hours_left = (MIN_CANDLES_TO_TRAIN - collector.candles_collected) / 4
                                log(f"New candle | Collected: {collector.candles_collected}/{MIN_CANDLES_TO_TRAIN} | ~{hours_left:.1f}h left")
                    
                    # Rate limit
                    now = time.time()
                    if now - last_check < 1.0:  # Every 1 second
                        continue
                    last_check = now
                    
                    if not tokens or candle_open_btc is None:
                        continue
                    
                    # Get prices
                    up_price, dn_price = get_prices()
                    if up_price is None:
                        continue
                    
                    minutes_elapsed = get_minutes_elapsed()
                    minutes_left = get_minutes_remaining()
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100
                    
                    # Record snapshot every minute
                    current_minute = int(minutes_elapsed)
                    if current_minute > last_snapshot_minute and current_minute <= 14:
                        collector.record_snapshot(current_minute, btc_now, up_price, dn_price)
                        last_snapshot_minute = current_minute
                    
                    # Display
                    progress = collector.candles_collected / MIN_CANDLES_TO_TRAIN * 100
                    bar = "█" * int(progress / 5) + "░" * (20 - int(progress / 5))
                    print(f"📊 [{bar}] {progress:.0f}% | Candle {collector.candles_collected}/{MIN_CANDLES_TO_TRAIN} | BTC:{btc_change:+.2f}% | {minutes_left:.1f}m left", end='\r')
                    
                    await asyncio.sleep(0.1)
                    
        except websockets.exceptions.ConnectionClosed:
            log("Reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            log(f"Error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)


# === TRADE MODE ===
async def run_trade_mode():
    global SLUG, tokens, candle_start_time, candle_open_btc
    global last_snapshot_minute, position, btc_history
    
    init_client()
    
    # Initialize and train model
    model = MLModel()
    collector = DataCollector()
    
    print("=" * 60)
    print("v60 - ML TRADING MODE")
    print(f"Loading data from: {DATA_FILE}")
    print("=" * 60)
    
    if not model.load_and_train():
        print("\n❌ Failed to train model!")
        print(f"   Need at least {MIN_CANDLES_TO_TRAIN} candles of data.")
        print(f"   Run: python3 bot_v60.py --collect")
        return
    
    print(f"\n✅ Model ready! Accuracy: {model.accuracy*100:.1f}%")
    print(f"   Min confidence to trade: {MIN_CONFIDENCE*100:.0f}%")
    print(f"   Bet amount: ${BET_AMOUNT}")
    print()
    
    find_active_market()
    if SLUG:
        log(f"Market: {SLUG}")
    
    if tokens:
        set_allowances()
    
    trades = []
    
    while True:
        try:
            async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                log("Connected to Binance ✓")
                
                tick_count = 0
                last_check = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    tick_count += 1
                    
                    # Set candle open
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        btc_history = [btc_now]
                        collector.start_candle(candle_start_time, btc_now)
                        log(f"Candle open: ${candle_open_btc:,.2f}")
                    
                    # Check for new market
                    if tick_count % 100 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
                                # Determine outcome
                                outcome = 'UP' if btc_now > candle_open_btc else 'DN'
                                collector.end_candle(btc_now)
                                
                                # Check position result
                                if position:
                                    won = position['side'] == outcome
                                    profit = position['potential_profit'] if won else -position['cost']
                                    trades.append({'won': won, 'profit': profit})
                                    
                                    total_pnl = sum(t['profit'] for t in trades)
                                    win_rate = sum(1 for t in trades if t['won']) / len(trades) * 100
                                    
                                    emoji = "✅" if won else "❌"
                                    log(f"{emoji} {position['side']} {'WON' if won else 'LOST'} → ${profit:+.2f}")
                                    log(f"   Total: ${total_pnl:+.2f} | Win rate: {win_rate:.0f}% ({len(trades)} trades)")
                                    position = None
                                
                                # Retrain model periodically (every 10 candles)
                                if collector.candles_collected % 10 == 0:
                                    log("Retraining model with new data...")
                                    model.load_and_train()
                                
                                # Reset for new candle
                                candle_open_btc = btc_now
                                btc_history = [btc_now]
                                last_snapshot_minute = -1
                                collector.start_candle(candle_start_time, btc_now)
                                set_allowances()
                                
                                log(f"New candle: {SLUG}")
                    
                    # Rate limit
                    now = time.time()
                    if now - last_check < 0.5:
                        continue
                    last_check = now
                    
                    if not tokens or candle_open_btc is None:
                        continue
                    
                    # Get prices
                    up_price, dn_price = get_prices()
                    if up_price is None:
                        continue
                    
                    minutes_elapsed = get_minutes_elapsed()
                    minutes_left = get_minutes_remaining()
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100
                    price_gap = abs(up_price - dn_price)
                    
                    # Update BTC history
                    btc_history.append(btc_now)
                    
                    # Calculate momentum
                    if len(btc_history) >= 2:
                        momentum_1m = ((btc_now - btc_history[-2]) / btc_history[-2]) * 100
                    else:
                        momentum_1m = 0
                    
                    if len(btc_history) >= 4:
                        momentum_3m = ((btc_now - btc_history[-4]) / btc_history[-4]) * 100
                    else:
                        momentum_3m = 0
                    
                    # Record snapshot
                    current_minute = int(minutes_elapsed)
                    if current_minute > last_snapshot_minute and current_minute <= 14:
                        collector.record_snapshot(current_minute, btc_now, up_price, dn_price)
                        last_snapshot_minute = current_minute
                    
                    # Get ML prediction
                    prediction, confidence, reason = model.predict(
                        current_minute, btc_change, up_price, dn_price,
                        price_gap, momentum_1m, momentum_3m
                    )
                    
                    # Display
                    if position:
                        emoji = "🟢" if (position['side'] == 'UP' and btc_change > 0) or (position['side'] == 'DN' and btc_change < 0) else "🔴"
                        print(f"{emoji} HOLDING {position['side']} | BTC:{btc_change:+.2f}% | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m", end='\r')
                    elif prediction:
                        conf_bar = "█" * int(confidence * 10) + "░" * (10 - int(confidence * 10))
                        trade_ok = "✓" if confidence >= MIN_CONFIDENCE else "✗"
                        print(f"🤖 {prediction} [{conf_bar}] {confidence*100:.0f}% {trade_ok} | BTC:{btc_change:+.2f}% | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m", end='\r')
                    else:
                        print(f"👀 Waiting... | BTC:{btc_change:+.2f}% | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m", end='\r')
                    
                    # Trading logic
                    if position is None and minutes_left >= MIN_TIME_LEFT:
                        if prediction and confidence >= MIN_CONFIDENCE:
                            if prediction == 'UP' and 0.30 <= up_price <= 0.85:
                                log(f"🤖 ML PREDICTION: {reason}")
                                shares = place_order(tokens['up'], up_price + 0.01, BET_AMOUNT, "UP")
                                if shares > 0:
                                    position = {
                                        'side': 'UP',
                                        'shares': shares,
                                        'cost': BET_AMOUNT,
                                        'potential_profit': shares - BET_AMOUNT
                                    }
                                    log(f"   ✅ Bought {shares:.1f} UP | If wins: +${position['potential_profit']:.2f}")
                            
                            elif prediction == 'DN' and 0.30 <= dn_price <= 0.85:
                                log(f"🤖 ML PREDICTION: {reason}")
                                shares = place_order(tokens['dn'], dn_price + 0.01, BET_AMOUNT, "DN")
                                if shares > 0:
                                    position = {
                                        'side': 'DN',
                                        'shares': shares,
                                        'cost': BET_AMOUNT,
                                        'potential_profit': shares - BET_AMOUNT
                                    }
                                    log(f"   ✅ Bought {shares:.1f} DN | If wins: +${position['potential_profit']:.2f}")
                    
                    await asyncio.sleep(0.1)
                    
        except websockets.exceptions.ConnectionClosed:
            log("Reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            log(f"Error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)


# === MAIN ===
def main():
    print()
    print("╔════════════════════════════════════════════════════════════╗")
    print("║         v60 - MACHINE LEARNING TRADING BOT                 ║")
    print("╠════════════════════════════════════════════════════════════╣")
    print("║  STEP 1: Collect data (run for 24-48 hours)                ║")
    print("║          python3 bot_v60.py --collect                      ║")
    print("║                                                            ║")
    print("║  STEP 2: Trade with ML (after collecting data)             ║")
    print("║          python3 bot_v60.py --trade                        ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()
    
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 bot_v60.py --collect   # Collect data first")
        print("  python3 bot_v60.py --trade     # Trade with ML")
        return
    
    mode = sys.argv[1].lower()
    
    if mode in ['--collect', '-c', 'collect']:
        asyncio.run(run_collect_mode())
    elif mode in ['--trade', '-t', 'trade']:
        asyncio.run(run_trade_mode())
    else:
        print(f"Unknown mode: {mode}")
        print("Use --collect or --trade")


if __name__ == "__main__":
    main()
