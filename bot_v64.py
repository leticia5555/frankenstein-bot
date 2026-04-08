import warnings
warnings.filterwarnings("ignore")
import os
os.environ["PYTHONWARNINGS"] = "ignore"
import warnings
warnings.filterwarnings("ignore")

import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType, BookParams
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

# ═══════════════════════════════════════════════════════════════════════════
# POSITION PERSISTENCE (NEW v64 - crash-proof tracking)
#
# Saves open positions to disk so they survive bot crashes/restarts.
# On startup, checks if there's an unresolved trade and records the outcome.
# ═══════════════════════════════════════════════════════════════════════════

POSITION_FILE = "open_position.json"

class PositionPersistence:
    """
    Writes open positions to disk. If the bot crashes mid-trade,
    on restart it can detect the orphaned position and resolve it.
    """
    
    @staticmethod
    def save_position(position_data, candle_start, candle_open_btc):
        """Save an open position to disk when a trade is placed."""
        record = {
            'position': position_data,
            'candle_start': candle_start,
            'candle_open_btc': candle_open_btc,
            'opened_at': time.time(),
            'saved_at': datetime.now().isoformat()
        }
        try:
            with open(POSITION_FILE, 'w') as f:
                json.dump(record, f, indent=2)
        except Exception as e:
            print(f"[PERSIST] Error saving position: {e}")
    
    @staticmethod
    def clear_position():
        """Remove the position file when a trade is resolved."""
        try:
            if os.path.exists(POSITION_FILE):
                os.remove(POSITION_FILE)
        except Exception as e:
            print(f"[PERSIST] Error clearing position: {e}")
    
    @staticmethod
    def load_position():
        """Load an orphaned position from disk (if any)."""
        try:
            if os.path.exists(POSITION_FILE):
                with open(POSITION_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f"[PERSIST] Error loading position: {e}")
        return None
    
    @staticmethod
    def resolve_orphaned_position(tracker):
        """
        Check if there's a position from a previous run that was never resolved.
        If the candle has ended (current time > candle_start + 900), we can
        determine the outcome by checking what BTC did.
        """
        saved = PositionPersistence.load_position()
        if not saved:
            return None
        
        candle_start = saved['candle_start']
        candle_end = candle_start + 900
        now = time.time()
        
        if now < candle_end:
            # Candle hasn't ended yet — position is still live
            print(f"[PERSIST] Found live position from current candle, restoring...")
            return saved
        
        # Candle has ended — need to figure out the outcome
        position = saved['position']
        candle_open_btc = saved['candle_open_btc']
        
        print(f"[PERSIST] Found orphaned {position['side']} trade from candle {candle_start}")
        print(f"[PERSIST] Candle ended at {datetime.fromtimestamp(candle_end).strftime('%H:%M:%S')}")
        
        # Try to get BTC price at candle close by checking what happened
        # We can't know the exact close price, but we can check the market outcome
        # via the Polymarket API
        try:
            slug = f"btc-updown-15m-{candle_start}"
            r = requests.get(
                f"https://gamma-api.polymarket.com/events?slug={slug}",
                timeout=10
            )
            data = r.json()
            
            if data and len(data) > 0:
                event = data[0]
                markets = event.get("markets", [])
                if markets:
                    market = markets[0]
                    outcome_str = market.get("outcome", "")
                    
                    # Check if market is resolved
                    if market.get("closed"):
                        # Try to determine outcome from outcomePrices
                        outcome_prices = market.get("outcomePrices", "")
                        try:
                            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                            if prices and len(prices) >= 2:
                                up_final = float(prices[0])
                                dn_final = float(prices[1])
                                if up_final > 0.9:
                                    outcome = "UP"
                                elif dn_final > 0.9:
                                    outcome = "DN"
                                else:
                                    outcome = None
                                
                                if outcome:
                                    won = (position['side'] == outcome)
                                    profit = position['shares'] * 1.0 - position['cost'] if won else -position['cost']
                                    
                                    emoji = "🟢" if won else "🔴"
                                    print(f"[PERSIST] {emoji} Resolved orphaned trade: {position['side']} → {outcome} "
                                          f"({'WIN' if won else 'LOSS'}) ${profit:+.2f}")
                                    
                                    # Record in tracker
                                    tracker.open_trade(
                                        candle_start, position['side'], 
                                        position.get('entry_price', 0),
                                        position['shares'], position['cost'],
                                        position.get('ml_confidence', 0),
                                        position.get('book_signal', '-'),
                                        position.get('books_agree', False),
                                        position.get('spread', 0),
                                        position.get('ev', 0),
                                        position.get('ev_pct', 0),
                                        position.get('btc_change', 0),
                                        position.get('rsi', 50),
                                        position.get('minute', 0)
                                    )
                                    tracker.close_trade(outcome, 0)
                                    
                                    PositionPersistence.clear_position()
                                    print(f"[PERSIST] ✅ Orphaned trade recorded to CSV")
                                    return None
                        except:
                            pass
                    
                    # Market not resolved yet or can't determine outcome
                    print(f"[PERSIST] ⚠️ Could not determine outcome for candle {candle_start}")
                    print(f"[PERSIST] Market closed={market.get('closed')}")
            
        except Exception as e:
            print(f"[PERSIST] Error checking market outcome: {e}")
        
        # If we can't determine outcome, log it and clear
        print(f"[PERSIST] ⚠️ Clearing unresolvable orphaned position")
        print(f"[PERSIST] Manual check needed for candle {candle_start} ({position['side']})")
        
        # Write to a separate lost trades file so nothing is silently dropped
        try:
            with open("orphaned_trades.csv", 'a') as f:
                f.write(f"{saved['saved_at']},{candle_start},{position['side']},"
                        f"{position['cost']},{position['shares']},UNRESOLVED\n")
        except:
            pass
        
        PositionPersistence.clear_position()
        return None

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  v64 - FRANKENSTEIN BOT (CRASH-PROOF TRACKING)                          ║
# ║                                                                          ║
# ║  NEW in v64:                                                             ║
# ║  • Crash-proof position persistence (saves to disk)                      ║
# ║  • Positions survive bot restarts and WebSocket drops                    ║
# ║  • Automatic trade resolution on startup                                 ║
# ║  • ALL trades now recorded — wins AND losses                             ║
# ║                                                                          ║
# ║  KEPT from v63:                                                          ║
# ║  • Order book imbalance detection (bid/ask pressure)                     ║
# ║  • ML + Book + EV signal combination                                     ║
# ║  • Spread analysis and depth-weighted signals                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# === PARAMETERS ===
DATA_FILE = "btc_15m_data_v64.csv"
MIN_CANDLES_TO_TRAIN = 96
BET_AMOUNT = 5.0
MIN_TIME_LEFT = 3.0

# === v62 PARAMETERS (kept) ===
MIN_EXPECTED_VALUE = 0.03
CONFIDENCE_FLOOR = 0.60
ALPHA_EXTRACTION = 0.90
MAX_TRADES_PER_CANDLE = 2

# === NEW v63 PARAMETERS (Order Book) ===
MIN_BOOK_IMBALANCE = 0.15      # Minimum 15% imbalance to boost signal
SPREAD_CONFIDENCE_BOOST = 0.05 # Boost confidence when spread is tight
MAX_SPREAD_TO_TRADE = 0.08     # Don't trade if spread > 8 cents
MIN_BOOK_DEPTH = 50            # Minimum $50 on each side to trade

# === POLYMARKET CLIENT ===
client = None
def init_client(read_only=False):
    global client
    if read_only:
        # Read-only client for viewing order books (no auth needed)
        client = ClobClient(
            host="https://clob.polymarket.com",
        )
    else:
        # Full client for trading
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_PRIVATE_KEY"),
            chain_id=137,
            signature_type=2,
            funder=os.getenv("POLYMARKET_FUNDER")
        )
        client.set_api_creds(client.create_or_derive_api_creds())


# ═══════════════════════════════════════════════════════════════════════════
# ORDER BOOK ANALYZER (NEW - The key addition in v63)
#
# This analyzes the CLOB order book to extract predictive signals:
# 1. Imbalance: More bids than asks = bullish pressure
# 2. Spread: Tight spread = market is confident in current price
# 3. Depth: Thick book = harder to move price
# 4. Volume at price: Where are the big orders?
# ═══════════════════════════════════════════════════════════════════════════

class OrderBookAnalyzer:
    """
    Analyzes Polymarket's Central Limit Order Book (CLOB) for trading signals.
    
    Key insight: In a CLOB, price is determined by order flow.
    If there are more aggressive buyers (lifting asks) than sellers (hitting bids),
    price will move up. We can detect this by looking at book imbalance.
    """
    
    def __init__(self):
        self.book_history = {
            'up': deque(maxlen=30),   # Last 30 snapshots
            'dn': deque(maxlen=30)
        }
        self.last_book_time = 0
        self.imbalance_history = deque(maxlen=60)  # Track imbalance over time
    
    def _fetch_book_direct(self, token_id):
        """Fetch order book using direct API call (more reliable than py_clob_client)."""
        try:
            r = requests.get(f'https://clob.polymarket.com/book?token_id={token_id}', timeout=10)
            if r.status_code == 200:
                return r.json()
            return None
        except:
            return None
        
    def analyze_book(self, client, tokens):
        """
        Fetch and analyze order books for both UP and DN tokens.
        Returns comprehensive book metrics.
        """
        try:
            # Use direct API call instead of py_clob_client (which returns object not dict)
            up_book = self._fetch_book_direct(tokens['up'])
            dn_book = self._fetch_book_direct(tokens['dn'])
            
            if up_book is None or dn_book is None:
                return None
            
            up_analysis = self._analyze_single_book(up_book, 'UP')
            dn_analysis = self._analyze_single_book(dn_book, 'DN')
            
            # Store for historical analysis
            self.book_history['up'].append(up_analysis)
            self.book_history['dn'].append(dn_analysis)
            
            # Combined analysis
            combined = self._combine_analysis(up_analysis, dn_analysis)
            
            return {
                'up': up_analysis,
                'dn': dn_analysis,
                'combined': combined,
                'timestamp': time.time()
            }
            
        except Exception as e:
            print(f"[BOOK] Error analyzing book: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _analyze_single_book(self, book, side):
        """
        Analyze a single order book and extract metrics.
        """
        bids = book.get('bids', [])
        asks = book.get('asks', [])
        
        # Parse and sort
        bid_levels = [(float(b['price']), float(b['size'])) for b in bids]
        ask_levels = [(float(a['price']), float(a['size'])) for a in asks]
        
        bid_levels.sort(key=lambda x: x[0], reverse=True)  # Highest bid first
        ask_levels.sort(key=lambda x: x[0])  # Lowest ask first
        
        # Best bid/ask
        best_bid = bid_levels[0][0] if bid_levels else 0
        best_ask = ask_levels[0][0] if ask_levels else 1
        
        # Spread
        spread = best_ask - best_bid
        midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.5
        spread_pct = spread / midpoint if midpoint else 0
        
        # Total depth (in dollars)
        total_bid_depth = sum(p * s for p, s in bid_levels)
        total_ask_depth = sum(p * s for p, s in ask_levels)
        
        # Depth at top 3 levels (most relevant for immediate trades)
        top_bid_depth = sum(p * s for p, s in bid_levels[:3])
        top_ask_depth = sum(p * s for p, s in ask_levels[:3])
        
        # Imbalance: positive = more bids (bullish), negative = more asks (bearish)
        total_depth = total_bid_depth + total_ask_depth
        if total_depth > 0:
            imbalance = (total_bid_depth - total_ask_depth) / total_depth
        else:
            imbalance = 0
        
        # Top-of-book imbalance (more actionable)
        top_depth = top_bid_depth + top_ask_depth
        if top_depth > 0:
            top_imbalance = (top_bid_depth - top_ask_depth) / top_depth
        else:
            top_imbalance = 0
        
        # Depth-weighted midpoint (where is the "true" price based on volume?)
        if total_depth > 0:
            weighted_price = (
                sum(p * p * s for p, s in bid_levels) + 
                sum(p * p * s for p, s in ask_levels)
            ) / (
                sum(p * s for p, s in bid_levels) + 
                sum(p * s for p, s in ask_levels)
            )
        else:
            weighted_price = midpoint
        
        return {
            'side': side,
            'best_bid': best_bid,
            'best_ask': best_ask,
            'spread': spread,
            'spread_pct': spread_pct,
            'midpoint': midpoint,
            'total_bid_depth': total_bid_depth,
            'total_ask_depth': total_ask_depth,
            'top_bid_depth': top_bid_depth,
            'top_ask_depth': top_ask_depth,
            'imbalance': imbalance,
            'top_imbalance': top_imbalance,
            'weighted_price': weighted_price,
            'bid_levels': len(bid_levels),
            'ask_levels': len(ask_levels),
            'is_liquid': total_depth > MIN_BOOK_DEPTH,
            'is_tight': spread_pct < MAX_SPREAD_TO_TRADE
        }
    
    def _combine_analysis(self, up_analysis, dn_analysis):
        """
        Combine UP and DN book analysis to get overall market signal.
        
        Key insight: In Polymarket, YES + NO = $1.
        So if UP book shows bullish imbalance AND DN book shows bearish imbalance,
        that's a strong signal that the market expects UP.
        """
        
        # UP imbalance > 0 means more people want to BUY UP
        # DN imbalance < 0 means more people want to SELL DN (equivalent to buying UP)
        
        up_signal = up_analysis['top_imbalance']
        dn_signal = -dn_analysis['top_imbalance']  # Flip sign
        
        # Combined signal: average of both perspectives
        combined_signal = (up_signal + dn_signal) / 2
        
        # Confidence: if both books agree, higher confidence
        if (up_signal > 0 and dn_signal > 0) or (up_signal < 0 and dn_signal < 0):
            books_agree = True
            signal_strength = abs(combined_signal)
        else:
            books_agree = False
            signal_strength = abs(combined_signal) * 0.5  # Discount conflicting signals
        
        # Spread analysis
        avg_spread = (up_analysis['spread'] + dn_analysis['spread']) / 2
        
        # Liquidity check
        is_liquid = up_analysis['is_liquid'] and dn_analysis['is_liquid']
        is_tight = up_analysis['is_tight'] and dn_analysis['is_tight']
        
        # Derive prediction from book state
        if combined_signal > MIN_BOOK_IMBALANCE:
            book_prediction = 'UP'
            book_confidence = min(0.5 + signal_strength, 0.85)
        elif combined_signal < -MIN_BOOK_IMBALANCE:
            book_prediction = 'DN'
            book_confidence = min(0.5 + signal_strength, 0.85)
        else:
            book_prediction = None
            book_confidence = 0.5
        
        return {
            'combined_signal': combined_signal,
            'signal_strength': signal_strength,
            'books_agree': books_agree,
            'book_prediction': book_prediction,
            'book_confidence': book_confidence,
            'avg_spread': avg_spread,
            'is_liquid': is_liquid,
            'is_tight': is_tight,
            'up_imbalance': up_analysis['top_imbalance'],
            'dn_imbalance': dn_analysis['top_imbalance'],
        }
    
    def get_book_signal(self, client, tokens):
        """
        Main entry point: get a trading signal from the order book.
        Returns a dict with prediction, confidence, and whether to trade.
        """
        analysis = self.analyze_book(client, tokens)
        
        if analysis is None:
            return {
                'has_signal': False,
                'reason': 'Could not fetch order book'
            }
        
        combined = analysis['combined']
        
        # Check tradability
        if not combined['is_liquid']:
            return {
                'has_signal': False,
                'reason': f'Insufficient liquidity (need ${MIN_BOOK_DEPTH}+)'
            }
        
        if not combined['is_tight']:
            return {
                'has_signal': False,
                'reason': f'Spread too wide ({combined["avg_spread"]*100:.1f}¢ > {MAX_SPREAD_TO_TRADE*100:.0f}¢)'
            }
        
        return {
            'has_signal': combined['book_prediction'] is not None,
            'prediction': combined['book_prediction'],
            'confidence': combined['book_confidence'],
            'signal_strength': combined['signal_strength'],
            'books_agree': combined['books_agree'],
            'up_imbalance': combined['up_imbalance'],
            'dn_imbalance': combined['dn_imbalance'],
            'spread': combined['avg_spread'],
            'analysis': analysis
        }
    
    def get_imbalance_momentum(self):
        """
        Track how imbalance is changing over time.
        Rising imbalance = strengthening signal.
        """
        if len(self.imbalance_history) < 5:
            return 0
        
        recent = list(self.imbalance_history)[-5:]
        older = list(self.imbalance_history)[-10:-5] if len(self.imbalance_history) >= 10 else recent
        
        recent_avg = sum(recent) / len(recent)
        older_avg = sum(older) / len(older)
        
        return recent_avg - older_avg  # Positive = imbalance trending bullish


# ═══════════════════════════════════════════════════════════════════════════
# TECHNICAL ANALYSIS ENGINE (same as v62)
# ═══════════════════════════════════════════════════════════════════════════

class TechnicalAnalysis:
    def __init__(self):
        self.prices = deque(maxlen=500)
        self.volumes = deque(maxlen=500)
        self.timestamps = deque(maxlen=500)
        self.ha_open = None
        self.ha_close = None
        self.ha_trend = 0
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
# MARKET INTELLIGENCE (fixed - uses slug-based lookup like v61)
# ═══════════════════════════════════════════════════════════════════════════

class MarketIntelligence:
    """
    Uses timestamp-based slug pattern: btc-updown-15m-{timestamp}
    This is the correct method from v61.
    """
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    
    def __init__(self):
        self.current_market = None
        self.last_timestamp = None
        self.market_history = []

    def _get_current_timestamp(self):
        """Get the current 15-minute window timestamp."""
        return (int(time.time()) // 900) * 900

    def find_active_market(self):
        """
        Find active BTC 15-minute market using slug-based lookup.
        Tries current, next, and previous windows.
        """
        current_ts = self._get_current_timestamp()
        
        # Try current, next, and previous windows
        for offset in [0, 900, -900]:
            timestamp = current_ts + offset
            slug = f"btc-updown-15m-{timestamp}"
            
            try:
                r = requests.get(
                    f"{self.GAMMA_API}/events?slug={slug}",
                    timeout=5
                )
                data = r.json()
                
                if data and len(data) > 0:
                    event = data[0]
                    markets = event.get("markets", [])
                    if markets:
                        market = markets[0]
                        # Check if market is accepting orders and not closed
                        if market.get("acceptingOrders") and not market.get("closed"):
                            clob_ids_str = market.get("clobTokenIds", "")
                            tokens = self._parse_tokens(clob_ids_str)
                            
                            if tokens:
                                is_new = (timestamp != self.last_timestamp)
                                
                                # Save to history
                                if is_new and self.current_market:
                                    self.market_history.append(self.current_market)
                                
                                self.last_timestamp = timestamp
                                self.current_market = {
                                    'slug': slug,
                                    'timestamp': timestamp,
                                    'tokens': tokens,
                                    'volume': market.get('volume', 0),
                                    'liquidity': market.get('liquidity', 0),
                                    'question': market.get('question', ''),
                                }
                                
                                return is_new, self.current_market
            except Exception as e:
                continue
        
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
# EXPECTED VALUE CALCULATOR (enhanced with book data)
# ═══════════════════════════════════════════════════════════════════════════

class EVCalculator:
    def __init__(self):
        self.trade_history = []
        self.calibration_offset = 0.0
    
    def calculate_ev(self, prediction, raw_confidence, up_price, dn_price, 
                     bet_amount, book_signal=None):
        """
        Calculate expected value, now incorporating order book signals.
        """
        adjusted_confidence = self._calibrate(raw_confidence)
        
        # NEW: Boost confidence if book agrees with ML prediction
        if book_signal and book_signal.get('has_signal'):
            if book_signal.get('prediction') == prediction:
                # Book agrees - boost confidence
                boost = min(book_signal.get('signal_strength', 0) * 0.1, 0.05)
                adjusted_confidence = min(adjusted_confidence + boost, 0.95)
                
                # Extra boost if both books agree with each other
                if book_signal.get('books_agree'):
                    adjusted_confidence = min(adjusted_confidence + 0.02, 0.95)
            else:
                # Book disagrees - reduce confidence
                adjusted_confidence = adjusted_confidence * 0.95
        
        # NEW: Adjust for spread (tighter spread = more confident market)
        if book_signal and book_signal.get('spread'):
            spread = book_signal['spread']
            if spread < 0.02:  # Very tight spread
                adjusted_confidence = min(adjusted_confidence + SPREAD_CONFIDENCE_BOOST, 0.95)
            elif spread > 0.05:  # Wide spread
                adjusted_confidence = adjusted_confidence * 0.97
        
        if prediction == 'UP':
            entry_price = up_price + 0.01
            shares = bet_amount / entry_price
            win_payout = shares * 1.0
            ev = adjusted_confidence * win_payout - bet_amount
        else:
            entry_price = dn_price + 0.01
            shares = bet_amount / entry_price
            win_payout = shares * 1.0
            ev = adjusted_confidence * win_payout - bet_amount
        
        ev_pct = ev / bet_amount
        max_possible_ev = (1.0 / max(min(up_price, dn_price), 0.01) - 1) * bet_amount
        extraction_ratio = ev / max_possible_ev if max_possible_ev > 0 else 0
        
        return {
            'ev': ev,
            'ev_pct': ev_pct,
            'should_trade': ev_pct >= MIN_EXPECTED_VALUE,
            'adjusted_confidence': adjusted_confidence,
            'raw_confidence': raw_confidence,
            'extraction_ratio': extraction_ratio,
            'book_boost_applied': book_signal is not None and book_signal.get('has_signal')
        }
    
    def _calibrate(self, raw_confidence):
        calibrated = raw_confidence + self.calibration_offset
        shrinkage = 0.1
        calibrated = calibrated * (1 - shrinkage) + 0.5 * shrinkage
        return max(0.01, min(0.99, calibrated))
    
    def record_outcome(self, predicted_confidence, actual_win):
        self.trade_history.append({
            'confidence': predicted_confidence,
            'win': actual_win,
            'timestamp': time.time()
        })
        
        if len(self.trade_history) >= 20:
            recent = self.trade_history[-50:]
            avg_confidence = sum(t['confidence'] for t in recent) / len(recent)
            actual_win_rate = sum(1 for t in recent if t['win']) / len(recent)
            self.calibration_offset = actual_win_rate - avg_confidence


# ═══════════════════════════════════════════════════════════════════════════
# ML MODEL (same as v62)
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
        self._active_features = []
        self.feature_importances = {}

    def load_and_train(self):
        try:
            from sklearn.ensemble import RandomForestClassifier
            from sklearn.calibration import CalibratedClassifierCV
            from sklearn.model_selection import cross_val_score, train_test_split
            import pandas as pd
            import numpy as np

            # Try multiple data files
            for data_file in [DATA_FILE, "btc_15m_data_v63.csv", "btc_15m_data_v62.csv", "btc_15m_data_v61.csv"]:
                try:
                    df = pd.read_csv(data_file)
                    print(f"[ML] Using data from: {data_file}")
                    break
                except:
                    continue
            else:
                print("[ML] No data file found!")
                return False
            
            print(f"[ML] Loaded {len(df)} data points from {df['candle_start'].nunique()} candles")

            if len(df) < 100:
                print(f"[ML] Need more data! Have {len(df)}, need 100+")
                return False

            available_features = [f for f in self.feature_names if f in df.columns]
            print(f"[ML] Using {len(available_features)} features")
            
            df = df.sample(n=min(30000, len(df)), random_state=42)
            X = df[available_features].fillna(0).values
            y = (df['outcome'] == 'UP').astype(int).values

            X_train, X_cal, y_train, y_cal = train_test_split(X, y, test_size=0.2, random_state=42)

            base_model = RandomForestClassifier(
                n_estimators=100,
                max_depth=5,
                min_samples_split=20,
                min_samples_leaf=10,
                max_features='sqrt',
                random_state=42,
                n_jobs=-1
            )

            self.model = CalibratedClassifierCV(base_model, method='isotonic', cv=3)
            self.model.fit(X_train, y_train)
            
            scores = cross_val_score(base_model, X, y, cv=5, scoring='accuracy')
            self.accuracy = scores.mean()
            
            self.is_trained = True
            self._active_features = available_features

            base_model.fit(X_train, y_train)
            self.feature_importances = dict(zip(available_features, base_model.feature_importances_))

            print(f"[ML] ✅ Model trained! Accuracy: {self.accuracy*100:.1f}%")
            return True

        except Exception as e:
            print(f"[ML] ERROR: {e}")
            import traceback
            traceback.print_exc()
            return False

    def predict(self, minute, btc_change, up_price, dn_price,
                price_gap, momentum_1m, momentum_3m, ta_features=None):
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

            features = [feature_dict.get(fname, 0) for fname in self._active_features]
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
# TRADE TRACKER (same as v62)
# ═══════════════════════════════════════════════════════════════════════════

class TradeTracker:
    def __init__(self, filename="trades_v64.csv"):
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
                    'shares', 'cost', 'ml_confidence', 'book_signal',
                    'books_agree', 'spread', 'ev', 'ev_pct',
                    'btc_change_at_entry', 'rsi', 
                    'minute_entered', 'outcome', 'profit', 'won'
                ])
    
    def open_trade(self, candle_start, side, entry_price, shares, cost,
                   ml_confidence, book_signal, books_agree, spread, ev, ev_pct, 
                   btc_change, rsi, minute):
        self.current_trade = {
            'timestamp': datetime.now().isoformat(),
            'candle_start': candle_start,
            'side': side,
            'entry_price': entry_price,
            'shares': shares,
            'cost': cost,
            'ml_confidence': ml_confidence,
            'book_signal': book_signal,
            'books_agree': books_agree,
            'spread': spread,
            'ev': ev,
            'ev_pct': ev_pct,
            'btc_change': btc_change,
            'rsi': rsi,
            'minute': minute
        }
    
    def close_trade(self, outcome, final_btc_change):
        if not self.current_trade:
            return None
        
        t = self.current_trade
        won = (t['side'] == 'UP' and outcome == 'UP') or \
              (t['side'] == 'DN' and outcome == 'DN')
        
        profit = t['shares'] * 1.0 - t['cost'] if won else -t['cost']
        
        t['outcome'] = outcome
        t['profit'] = profit
        t['won'] = won
        
        with open(self.filename, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                t['timestamp'], t['candle_start'], t['side'], t['entry_price'],
                t['shares'], t['cost'], t['ml_confidence'], t['book_signal'],
                t['books_agree'], t['spread'], t['ev'], t['ev_pct'],
                t['btc_change'], t['rsi'], t['minute'], t['outcome'], 
                t['profit'], t['won']
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
# TRADE MODE (ENHANCED WITH ORDER BOOK ANALYSIS)
# ═══════════════════════════════════════════════════════════════════════════

async def run_trade_mode():
    init_client()
    
    model = MLModel()
    if not model.load_and_train():
        print("Failed to train model. Run data collection first.")
        return
    
    ta = TechnicalAnalysis()
    market_intel = MarketIntelligence()
    ev_calc = EVCalculator()
    tracker = TradeTracker()
    book_analyzer = OrderBookAnalyzer()

    # v64: Check for orphaned positions from previous run
    restored = PositionPersistence.resolve_orphaned_position(tracker)
    if restored:
        log("Restored live position from previous run")

    print("=" * 65)
    print("  v64 FRANKENSTEIN - TRADING MODE (CRASH-PROOF)")
    print(f"  Model accuracy: {model.accuracy*100:.1f}%")
    print(f"  Min Expected Value: {MIN_EXPECTED_VALUE*100:.1f}%")
    print(f"  Min Book Imbalance: {MIN_BOOK_IMBALANCE*100:.0f}%")
    print(f"  Max Spread: {MAX_SPREAD_TO_TRADE*100:.0f}¢")
    print("=" * 65)

    is_new, market = market_intel.find_active_market()
    if market:
        log(f"Market: {market['slug']}")
        set_allowances(client, market['tokens'])

    candle_open_btc = None
    position = None
    trades_this_candle = 0
    last_book_check = 0

    # v64: If we restored a live position, set it up
    if restored:
        position = restored['position']
        candle_open_btc = restored['candle_open_btc']
        trades_this_candle = 1
        log(f"Resuming {position['side']} position from previous run | Open BTC: ${candle_open_btc:,.2f}")

    while True:
        try:
            async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                print("[DEBUG] WS connected!", flush=True)
                log("Connected to Binance ✓")

                recv_count = 0
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    recv_count += 1
                    if recv_count <= 3:
                        print(f"[DEBUG] msg#{recv_count} BTC=${btc_now:,.0f} market={market is not None} open={candle_open_btc}", flush=True)
                    
                    ta.update(btc_now)

                    is_new, market = market_intel.find_active_market()
                    
                    if is_new:
                        if position and candle_open_btc:
                            outcome = 'UP' if btc_now > candle_open_btc else 'DN'
                            result = tracker.close_trade(outcome, 
                                ((btc_now - candle_open_btc) / candle_open_btc) * 100)
                            if result:
                                emoji = "🟢" if result['won'] else "🔴"
                                log(f"{emoji} Trade closed: {'WIN' if result['won'] else 'LOSS'} ${result['profit']:+.2f}")
                                ev_calc.record_outcome(position.get('ml_confidence', 0.5), result['won'])
                            # v64: Clear saved position from disk
                            PositionPersistence.clear_position()
                        
                        candle_open_btc = btc_now
                        position = None
                        trades_this_candle = 0
                        ta.reset_for_new_candle()
                        
                        if market:
                            set_allowances(client, market['tokens'])
                            log(f"New candle | BTC: ${btc_now:,.2f}")
                            
                            stats = tracker.get_stats()
                            if stats and stats['total_trades'] > 0:
                                log(f"📊 Session: {stats['wins']}/{stats['total_trades']} "
                                    f"({stats['win_rate']*100:.1f}%) | P/L: ${stats['total_profit']:+.2f}")

                    # Wait for market
                    if not market:
                        await asyncio.sleep(0.1)
                        continue
                    
                    # Initialize candle price if joining mid-candle
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        log(f"Joined candle | BTC: ${btc_now:,.2f}")

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

                    # Get ML prediction
                    prediction, ml_confidence, reason = model.predict(
                        int(minutes_elapsed), btc_change, up_price, dn_price,
                        price_gap, delta_1m, delta_3m, ta_features
                    )

                    # === NEW: GET ORDER BOOK SIGNAL ===
                    book_signal = None
                    now = time.time()
                    if now - last_book_check >= 2.0:  # Check book every 2 seconds
                        book_signal = book_analyzer.get_book_signal(client, tokens)
                        last_book_check = now

                    # Calculate EV with book data
                    ev_result = None
                    if prediction and ml_confidence >= CONFIDENCE_FLOOR:
                        ev_result = ev_calc.calculate_ev(
                            prediction, ml_confidence, up_price, dn_price, 
                            BET_AMOUNT, book_signal
                        )

                    # Display
                    rsi = ta_features.get('rsi', 50)
                    
                    if position:
                        winning = (position['side'] == 'UP' and btc_change > 0) or \
                                  (position['side'] == 'DN' and btc_change < 0)
                        emoji = "🟢" if winning else "🔴"
                        print(
                            f"{emoji} HOLD {position['side']} | BTC:{btc_change:+.3f}% | "
                            f"{minutes_left:.1f}m left",
                            end='\r'
                        )
                    elif ev_result and book_signal:
                        book_pred = book_signal.get('prediction', '-')
                        book_str = f"📚{book_pred}" if book_signal.get('has_signal') else "📚-"
                        agree = "✓" if book_signal.get('has_signal') and book_signal.get('prediction') == prediction else ""
                        
                        print(
                            f"🤖 {prediction} {ml_confidence*100:.0f}% | {book_str}{agree} | "
                            f"EV:{ev_result['ev_pct']*100:+.1f}% | "
                            f"RSI:{rsi:.0f} | {minutes_left:.1f}m",
                            end='\r'
                        )
                    else:
                        print(
                            f"👀 BTC:{btc_change:+.3f}% | RSI:{rsi:.0f} | "
                            f"UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m",
                            end='\r'
                        )

                    # === TRADING DECISION ===
                    should_trade = (
                        position is None and
                        minutes_left >= MIN_TIME_LEFT and
                        trades_this_candle < MAX_TRADES_PER_CANDLE and
                        ev_result is not None and
                        ev_result['should_trade'] and
                        ml_confidence >= CONFIDENCE_FLOOR
                    )
                    
                    # Book-based filters
                    if should_trade and book_signal:
                        # Don't trade if spread is too wide
                        if not book_signal.get('analysis', {}).get('combined', {}).get('is_tight', True):
                            should_trade = False
                        
                        # Don't trade if book strongly disagrees with ML
                        if book_signal.get('has_signal') and book_signal.get('prediction') != prediction:
                            if book_signal.get('signal_strength', 0) > 0.3:  # Strong disagreement
                                should_trade = False
                    
                    # Price range filter
                    if should_trade:
                        if prediction == 'UP' and not (0.30 <= up_price <= 0.70):
                            should_trade = False
                        elif prediction == 'DN' and not (0.30 <= dn_price <= 0.70):
                            should_trade = False
                    
                    if should_trade:
                        trades_this_candle += 1
                        
                        book_pred = book_signal.get('prediction', '-') if book_signal else '-'
                        books_agree = book_signal.get('books_agree', False) if book_signal else False
                        spread = book_signal.get('spread', 0) if book_signal else 0
                        
                        if prediction == 'UP':
                            entry_price = up_price + 0.01
                            log(f"🎯 TRADE: UP | ML:{ml_confidence*100:.0f}% | Book:{book_pred} | "
                                f"EV:{ev_result['ev_pct']*100:+.1f}%")
                            shares = place_order(client, tokens['up'], entry_price, BET_AMOUNT, "UP")
                            
                            if shares > 0:
                                position = {
                                    'side': 'UP',
                                    'shares': shares,
                                    'cost': BET_AMOUNT,
                                    'entry_price': entry_price,
                                    'ml_confidence': ev_result['adjusted_confidence'],
                                    'ev_pct': ev_result['ev_pct'],
                                    'book_signal': book_pred,
                                    'books_agree': books_agree,
                                    'spread': spread,
                                    'ev': ev_result['ev'],
                                    'btc_change': btc_change,
                                    'rsi': rsi,
                                    'minute': int(minutes_elapsed)
                                }
                                # v64: Save to disk
                                PositionPersistence.save_position(position, market['timestamp'], candle_open_btc)
                                tracker.open_trade(
                                    market['timestamp'], 'UP', entry_price, shares, BET_AMOUNT,
                                    ev_result['adjusted_confidence'], book_pred, books_agree,
                                    spread, ev_result['ev'], ev_result['ev_pct'],
                                    btc_change, rsi, int(minutes_elapsed)
                                )
                        
                        else:
                            entry_price = dn_price + 0.01
                            log(f"🎯 TRADE: DN | ML:{ml_confidence*100:.0f}% | Book:{book_pred} | "
                                f"EV:{ev_result['ev_pct']*100:+.1f}%")
                            shares = place_order(client, tokens['dn'], entry_price, BET_AMOUNT, "DN")
                            
                            if shares > 0:
                                position = {
                                    'side': 'DN',
                                    'shares': shares,
                                    'cost': BET_AMOUNT,
                                    'entry_price': entry_price,
                                    'ml_confidence': ev_result['adjusted_confidence'],
                                    'ev_pct': ev_result['ev_pct'],
                                    'book_signal': book_pred,
                                    'books_agree': books_agree,
                                    'spread': spread,
                                    'ev': ev_result['ev'],
                                    'btc_change': btc_change,
                                    'rsi': rsi,
                                    'minute': int(minutes_elapsed)
                                }
                                # v64: Save to disk
                                PositionPersistence.save_position(position, market['timestamp'], candle_open_btc)
                                tracker.open_trade(
                                    market['timestamp'], 'DN', entry_price, shares, BET_AMOUNT,
                                    ev_result['adjusted_confidence'], book_pred, books_agree,
                                    spread, ev_result['ev'], ev_result['ev_pct'],
                                    btc_change, rsi, int(minutes_elapsed)
                                )

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
# BOOK ANALYSIS MODE (NEW - see what the order book looks like)
# ═══════════════════════════════════════════════════════════════════════════

async def run_book_mode():
    """Watch the order book in real-time to understand market structure."""
    init_client(read_only=True)  # Read-only for viewing
    
    market_intel = MarketIntelligence()
    book_analyzer = OrderBookAnalyzer()
    
    print("=" * 65)
    print("  v64 FRANKENSTEIN - ORDER BOOK VIEWER")
    print("  Watch real-time order book imbalance")
    print("=" * 65)
    
    while True:
        is_new, market = market_intel.find_active_market()
        
        if not market:
            print("Waiting for market...", end='\r')
            await asyncio.sleep(5)
            continue
        
        tokens = market['tokens']
        
        # Get book analysis
        signal = book_analyzer.get_book_signal(client, tokens)
        
        if signal and signal.get('analysis'):
            up = signal['analysis']['up']
            dn = signal['analysis']['dn']
            comb = signal['analysis']['combined']
            
            print(f"\n{'='*60}")
            print(f"UP Book: Bid ${up['total_bid_depth']:.0f} | Ask ${up['total_ask_depth']:.0f} | "
                  f"Imbal: {up['top_imbalance']*100:+.1f}% | Spread: {up['spread']*100:.1f}¢")
            print(f"DN Book: Bid ${dn['total_bid_depth']:.0f} | Ask ${dn['total_ask_depth']:.0f} | "
                  f"Imbal: {dn['top_imbalance']*100:+.1f}% | Spread: {dn['spread']*100:.1f}¢")
            print(f"Combined: Signal={comb['combined_signal']*100:+.1f}% | "
                  f"Prediction={comb['book_prediction'] or 'None'} | "
                  f"Books Agree={comb['books_agree']}")
            
            # Visual imbalance bar
            imbal = comb['combined_signal']
            bar_len = 20
            mid = bar_len // 2
            if imbal > 0:
                bar = "─" * mid + "█" * int(imbal * mid) + " " * (mid - int(imbal * mid))
            else:
                bar = " " * (mid + int(imbal * mid)) + "█" * (-int(imbal * mid)) + "─" * mid
            print(f"[DN {bar} UP]")
        else:
            print(f"Could not fetch book: {signal.get('reason', 'Unknown')}", end='\r')
        
        await asyncio.sleep(3)


# ═══════════════════════════════════════════════════════════════════════════
# ANALYZE MODE (enhanced to show book signal performance)
# ═══════════════════════════════════════════════════════════════════════════

def run_analyze_mode():
    try:
        import pandas as pd
        
        df = pd.read_csv("trades_v64.csv")
        print(f"\n📊 TRADE ANALYSIS ({len(df)} trades)")
        print("=" * 50)
        
        if len(df) == 0:
            print("No trades yet!")
            return
        
        wins = df['won'].sum()
        total = len(df)
        total_profit = df['profit'].sum()
        
        print(f"Win Rate: {wins}/{total} ({wins/total*100:.1f}%)")
        print(f"Total P/L: ${total_profit:+.2f}")
        
        # NEW: By book agreement
        if 'books_agree' in df.columns:
            print(f"\n📚 By Book Agreement:")
            for agree, group in df.groupby('books_agree'):
                if len(group) > 0:
                    wr = group['won'].mean() * 100
                    pl = group['profit'].sum()
                    print(f"  Books Agree={agree}: {len(group)} trades, {wr:.1f}% WR, ${pl:+.2f}")
        
        # NEW: ML agrees with book
        if 'book_signal' in df.columns:
            print(f"\n📚 ML + Book Agreement:")
            # This would need the ML prediction stored - for now just show book signal
            for sig, group in df.groupby('book_signal'):
                if len(group) > 0:
                    wr = group['won'].mean() * 100
                    print(f"  Book Signal={sig}: {len(group)} trades, {wr:.1f}% WR")
        
        # By spread
        if 'spread' in df.columns:
            print(f"\n📈 By Spread:")
            df['spread_bucket'] = pd.cut(df['spread'], bins=[0, 0.02, 0.04, 0.06, 1.0],
                                         labels=['<2¢', '2-4¢', '4-6¢', '>6¢'])
            for bucket, group in df.groupby('spread_bucket'):
                if len(group) > 0:
                    wr = group['won'].mean() * 100
                    print(f"  Spread {bucket}: {len(group)} trades, {wr:.1f}% WR")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║            v64 - FRANKENSTEIN (CRASH-PROOF EDITION)             ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print("║  NEW: Crash-proof position tracking                             ║")
    print("║  • Positions saved to disk, survive restarts                    ║")
    print("║  • Orphaned trades auto-resolved on startup                     ║")
    print("║  • ALL trades recorded — wins AND losses                        ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print("║  MODES:                                                          ║")
    print("║  python3 bot_v64.py --trade    # Trade with crash-proof track  ║")
    print("║  python3 bot_v64.py --book     # Watch order book in real-time  ║")
    print("║  python3 bot_v64.py --analyze  # Analyze trade history          ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 bot_v64.py --trade    # Trade with crash-proof tracking")
        print("  python3 bot_v64.py --book     # Watch order book live")
        print("  python3 bot_v64.py --analyze  # Analyze past trades")
        return

    mode = sys.argv[1].lower()

    if mode in ['--trade', '-t', 'trade']:
        asyncio.run(run_trade_mode())
    elif mode in ['--book', '-b', 'book']:
        asyncio.run(run_book_mode())
    elif mode in ['--analyze', '-a', 'analyze']:
        run_analyze_mode()
    else:
        print(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
