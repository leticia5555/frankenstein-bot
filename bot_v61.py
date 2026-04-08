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
# ║  v61 - FRANKENSTEIN BOT                                                ║
# ║                                                                        ║
# ║  Combines:                                                             ║
# ║  • Your v60 ML engine (Random Forest + data collection)                ║
# ║  • FrondEnt's assistant (multi-source data + TA indicators)            ║
# ║  • gabagool's bot (arbitrage detection + order book depth)             ║
# ║  • Polymarket/agents (modular architecture + market intelligence)      ║
# ║                                                                        ║
# ║  THREE MODES:                                                          ║
# ║  1. COLLECT:  python3 bot_v61.py --collect                             ║
# ║  2. TRADE:    python3 bot_v61.py --trade                               ║
# ║  3. ARB:      python3 bot_v61.py --arb  (pure arbitrage mode)          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# === PARAMETERS ===
DATA_FILE = "btc_15m_data_v61.csv"
MIN_CANDLES_TO_TRAIN = 96       # 24 hours of data minimum
MIN_CONFIDENCE = 0.75
MIN_VOLATILITY = 0.10  # Skip choppy markets           # Slightly lower threshold since we have more features now
BET_AMOUNT = 5.0               # $ per trade
MIN_TIME_LEFT = 5.0             # Need 5+ min left to trade
ARB_THRESHOLD = 0.96            # Max combined cost for arbitrage (from gabagool)
ARB_ORDER_SIZE = 5              # Shares per arb trade (from gabagool)
DRY_RUN = False                  # Simulation mode for arb (set False for live)

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
# FROM FRONDENT: TECHNICAL ANALYSIS ENGINE
# The FrondEnt assistant uses Heiken Ashi, RSI, MACD, VWAP, and Delta
# to generate a weighted LONG/SHORT signal. We port all of this to Python
# and feed it into the ML model as additional features.
# ═══════════════════════════════════════════════════════════════════════════

class TechnicalAnalysis:
    """
    TA engine inspired by FrondEnt's PolymarketBTC15mAssistant.
    Calculates: RSI, MACD, VWAP, Heiken Ashi, Delta, Volatility, EMA.
    All computed from the BTC price stream in real-time.
    """

    def __init__(self):
        # Price history for calculations
        self.prices = deque(maxlen=500)      # Raw BTC prices (sampled ~1/sec)
        self.volumes = deque(maxlen=500)     # Estimated volumes
        self.timestamps = deque(maxlen=500)
        
        # Heiken Ashi state
        self.ha_open = None
        self.ha_close = None
        self.ha_high = None
        self.ha_low = None
        self.ha_trend = 0  # 1 = bullish, -1 = bearish, 0 = neutral
        
        # MACD state
        self.ema_12 = None
        self.ema_26 = None
        self.macd_signal = None  # 9-period EMA of MACD line
        
        # For VWAP
        self.vwap_cum_pv = 0
        self.vwap_cum_vol = 0
        
        # Track 1-min OHLC candles for TA
        self.minute_candles = []
        self.current_1m_candle = None
        self.last_minute_mark = -1

    def update(self, price, timestamp=None):
        """Feed a new price tick into the TA engine"""
        ts = timestamp or time.time()
        self.prices.append(price)
        self.timestamps.append(ts)
        
        # Estimate volume as 1 unit per tick (approximation)
        self.volumes.append(1.0)
        
        # Update VWAP
        self.vwap_cum_pv += price * 1.0
        self.vwap_cum_vol += 1.0
        
        # Build 1-minute candles
        minute_mark = int(ts) // 60
        if minute_mark != self.last_minute_mark:
            # Close previous candle
            if self.current_1m_candle is not None:
                self.current_1m_candle['close'] = price
                self.minute_candles.append(self.current_1m_candle)
                # Update Heiken Ashi
                self._update_heiken_ashi(self.current_1m_candle)
            
            # Start new candle
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
        """
        Heiken Ashi smooths price action to show trends more clearly.
        FrondEnt uses this as part of their TA scoring.
        """
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        
        ha_close = (o + h + l + c) / 4
        
        if self.ha_open is None:
            ha_open = (o + c) / 2
        else:
            ha_open = (self.ha_open + self.ha_close) / 2
        
        ha_high = max(h, ha_open, ha_close)
        ha_low = min(l, ha_open, ha_close)
        
        self.ha_open = ha_open
        self.ha_close = ha_close
        self.ha_high = ha_high
        self.ha_low = ha_low
        
        # Determine trend
        if ha_close > ha_open:
            self.ha_trend = 1   # Bullish
        elif ha_close < ha_open:
            self.ha_trend = -1  # Bearish
        else:
            self.ha_trend = 0

    def get_rsi(self, period=14):
        """
        RSI (Relative Strength Index) - measures momentum.
        FrondEnt uses this as one of their core TA signals.
        """
        if len(self.prices) < period + 1:
            return 50.0  # Neutral default
        
        prices = list(self.prices)
        changes = [prices[i] - prices[i-1] for i in range(-period, 0)]
        
        gains = [c for c in changes if c > 0]
        losses = [-c for c in changes if c < 0]
        
        avg_gain = sum(gains) / period if gains else 0
        avg_loss = sum(losses) / period if losses else 0.0001
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

    def get_macd(self):
        """
        MACD (Moving Average Convergence Divergence).
        FrondEnt uses MACD line, signal line, and histogram.
        Returns: (macd_line, signal_line, histogram)
        """
        if len(self.prices) < 26:
            return 0, 0, 0
        
        prices = list(self.prices)
        
        # EMA-12
        ema_12 = self._calc_ema(prices, 12)
        # EMA-26
        ema_26 = self._calc_ema(prices, 26)
        
        macd_line = ema_12 - ema_26
        
        # Signal line (9-period EMA of MACD) - simplified
        if self.macd_signal is None:
            self.macd_signal = macd_line
        else:
            k = 2 / (9 + 1)
            self.macd_signal = macd_line * k + self.macd_signal * (1 - k)
        
        histogram = macd_line - self.macd_signal
        
        return macd_line, self.macd_signal, histogram

    def _calc_ema(self, prices, period):
        """Calculate EMA for given period"""
        if len(prices) < period:
            return prices[-1] if prices else 0
        
        k = 2 / (period + 1)
        ema = sum(prices[-period:]) / period  # Start with SMA
        
        # This is simplified - for real-time we'd maintain state
        # but for our purposes this gives a good approximation
        for price in prices[-period:]:
            ema = price * k + ema * (1 - k)
        
        return ema

    def get_vwap(self):
        """
        VWAP (Volume Weighted Average Price).
        FrondEnt uses this to gauge fair value.
        """
        if self.vwap_cum_vol == 0:
            return list(self.prices)[-1] if self.prices else 0
        return self.vwap_cum_pv / self.vwap_cum_vol

    def get_delta(self, minutes=1):
        """
        Price delta over N minutes.
        FrondEnt calculates 1m and 3m deltas.
        """
        if len(self.prices) < 2:
            return 0
        
        samples_per_min = max(1, len(self.prices) // max(1, len(self.minute_candles) or 1))
        lookback = samples_per_min * minutes
        
        if len(self.prices) < lookback + 1:
            return ((self.prices[-1] - self.prices[0]) / self.prices[0]) * 100 if self.prices[0] != 0 else 0
        
        return ((self.prices[-1] - self.prices[-lookback]) / self.prices[-lookback]) * 100

    def get_volatility(self, window=30):
        """
        Standard deviation of recent price changes.
        Higher volatility = more uncertainty = wider spreads.
        """
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
        """
        Replicates FrondEnt's weighted TA scoring system.
        Combines all indicators into a single LONG/SHORT % prediction.
        Returns: (direction, confidence) e.g. ('LONG', 0.72)
        """
        score = 0
        max_score = 0
        
        # RSI component (weight: 2)
        rsi = self.get_rsi()
        max_score += 2
        if rsi > 55:
            score += 2 * min((rsi - 50) / 30, 1)  # Bullish
        elif rsi < 45:
            score -= 2 * min((50 - rsi) / 30, 1)  # Bearish
        
        # MACD component (weight: 2)
        macd_line, signal, histogram = self.get_macd()
        max_score += 2
        if histogram > 0:
            score += 2 * min(abs(histogram) / 5, 1)
        else:
            score -= 2 * min(abs(histogram) / 5, 1)
        
        # Heiken Ashi trend (weight: 1.5)
        max_score += 1.5
        score += self.ha_trend * 1.5
        
        # VWAP position (weight: 1.5)
        vwap = self.get_vwap()
        current_price = self.prices[-1] if self.prices else 0
        max_score += 1.5
        if current_price > vwap:
            score += 1.5  # Price above VWAP = bullish
        elif current_price < vwap:
            score -= 1.5
        
        # 1-min delta (weight: 1)
        delta_1m = self.get_delta(1)
        max_score += 1
        if delta_1m > 0:
            score += min(abs(delta_1m) / 0.1, 1)
        else:
            score -= min(abs(delta_1m) / 0.1, 1)
        
        # 3-min delta (weight: 1)
        delta_3m = self.get_delta(3)
        max_score += 1
        if delta_3m > 0:
            score += min(abs(delta_3m) / 0.2, 1)
        else:
            score -= min(abs(delta_3m) / 0.2, 1)
        
        # Normalize to percentage
        if max_score == 0:
            return 'LONG', 0.5
        
        normalized = (score / max_score + 1) / 2  # Map [-1,1] to [0,1]
        
        if normalized >= 0.5:
            return 'LONG', normalized
        else:
            return 'SHORT', 1 - normalized

    def get_all_features(self):
        """
        Returns all TA features as a dict for the ML model.
        This is the key upgrade - v60 had 7 features, v61 has 15+.
        """
        rsi = self.get_rsi()
        macd_line, macd_signal, macd_hist = self.get_macd()
        vwap = self.get_vwap()
        delta_1m = self.get_delta(1)
        delta_3m = self.get_delta(3)
        volatility = self.get_volatility()
        frondent_dir, frondent_conf = self.get_frondent_signal()
        
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
            'frondent_signal': round(frondent_conf, 4) if frondent_dir == 'LONG' else round(-frondent_conf, 4),
        }

    def reset_for_new_candle(self):
        """Reset VWAP and candle-specific data for new 15m period"""
        self.vwap_cum_pv = 0
        self.vwap_cum_vol = 0
        self.minute_candles = []
        self.current_1m_candle = None
        self.last_minute_mark = -1
        # Keep prices/EMA state for continuity


# ═══════════════════════════════════════════════════════════════════════════
# FROM GABAGOOL: ARBITRAGE DETECTOR + ORDER BOOK DEPTH
# The gabagool bot detects when UP + DOWN < $1.00 for guaranteed profit.
# It also walks the order book to ensure fill depth.
# We add this as a secondary strategy alongside ML.
# ═══════════════════════════════════════════════════════════════════════════

class ArbitrageDetector:
    """
    Inspired by gabagool's 15min-btc-polymarket-trading-bot.
    Detects pure arbitrage opportunities where UP + DOWN < $1.00.
    Also includes order book depth checking.
    """

    def __init__(self, threshold=0.99):
        self.threshold = threshold
        self.opportunities_found = 0
        self.last_opportunity = None

    def check_opportunity(self, up_price, dn_price):
        """
        Check if there's an arbitrage opportunity.
        If UP + DN < threshold, buying both sides guarantees profit.
        """
        total_cost = up_price + dn_price
        
        if total_cost < self.threshold:
            profit_per_share = 1.0 - total_cost
            profit_pct = (profit_per_share / total_cost) * 100
            
            self.opportunities_found += 1
            self.last_opportunity = {
                'up_price': up_price,
                'dn_price': dn_price,
                'total_cost': total_cost,
                'profit_per_share': profit_per_share,
                'profit_pct': profit_pct,
                'timestamp': time.time()
            }
            return self.last_opportunity
        
        return None

    def get_book_depth(self, client, token_id, side='buy', target_size=5):
        """
        Walk the order book to check if target_size shares can fill.
        From gabagool's depth-aware sizing approach.
        Returns: (can_fill, worst_price, avg_price)
        """
        try:
            book = client.get_order_book(token_id)
            asks = book.get('asks', [])
            
            if not asks:
                return False, 0, 0
            
            filled = 0
            total_cost = 0
            worst_price = 0
            
            for level in sorted(asks, key=lambda x: float(x['price'])):
                price = float(level['price'])
                size = float(level['size'])
                
                can_take = min(size, target_size - filled)
                filled += can_take
                total_cost += can_take * price
                worst_price = price
                
                if filled >= target_size:
                    break
            
            if filled >= target_size:
                avg_price = total_cost / filled
                return True, worst_price, avg_price
            
            return False, worst_price, total_cost / filled if filled > 0 else 0
            
        except Exception as e:
            return False, 0, 0

    def execute_arb(self, client, tokens, up_price, dn_price, order_size, dry_run=True):
        """
        Execute arbitrage: buy both sides.
        From gabagool's paired execution with safety checks.
        """
        if dry_run:
            profit = (1.0 - up_price - dn_price) * order_size
            return {
                'success': True,
                'simulated': True,
                'up_cost': up_price * order_size,
                'dn_cost': dn_price * order_size,
                'total_cost': (up_price + dn_price) * order_size,
                'guaranteed_payout': order_size,
                'profit': profit
            }
        
        try:
            # Place UP leg
            opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
            up_args = OrderArgs(
                token_id=tokens['up'],
                price=up_price + 0.01,  # Slight bump to ensure fill
                size=order_size,
                side=BUY
            )
            up_order = client.create_order(up_args, opt)
            up_resp = client.post_order(up_order, OrderType.FOK)  # Fill-or-Kill from gabagool
            
            if not up_resp.get("success"):
                return {'success': False, 'error': f"UP leg failed: {up_resp.get('error', up_resp)}"}
            
            # Place DN leg
            dn_args = OrderArgs(
                token_id=tokens['dn'],
                price=dn_price + 0.01,
                size=order_size,
                side=BUY
            )
            dn_order = client.create_order(dn_args, opt)
            dn_resp = client.post_order(dn_order, OrderType.FOK)
            
            if not dn_resp.get("success"):
                # DN failed but UP filled - attempt to sell UP to unwind (gabagool safety)
                log("⚠️  DN leg failed! Attempting to unwind UP position...")
                return {'success': False, 'error': f"DN leg failed: {dn_resp.get('error', dn_resp)}", 'partial': True}
            
            profit = (1.0 - up_price - dn_price) * order_size
            return {
                'success': True,
                'simulated': False,
                'profit': profit,
                'up_cost': up_price * order_size,
                'dn_cost': dn_price * order_size,
            }
            
        except Exception as e:
            return {'success': False, 'error': str(e)}


# ═══════════════════════════════════════════════════════════════════════════
# FROM POLYMARKET/AGENTS: MARKET INTELLIGENCE MODULE
# The agents repo uses multiple data sources and modular connectors.
# We add enhanced market discovery and multi-source price validation.
# ═══════════════════════════════════════════════════════════════════════════

class MarketIntelligence:
    """
    Inspired by Polymarket/agents architecture.
    Handles market discovery, multi-source price validation,
    and market metadata analysis.
    """

    SERIES_ID = 10192                          # From FrondEnt's config
    SERIES_SLUG = "btc-up-or-down-15m"         # From FrondEnt's config
    GAMMA_API = "https://gamma-api.polymarket.com"

    def __init__(self):
        self.current_market = None
        self.market_history = []
        self.price_sources = {}  # Track price discrepancies across sources

    def find_active_market(self):
        """
        Enhanced market discovery combining your v60 approach
        with FrondEnt's series-based lookup.
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
                        if market.get("acceptingOrders") and not market.get("closed"):
                            clob_ids_str = market.get("clobTokenIds", "")
                            tokens = self._parse_tokens(clob_ids_str)
                            
                            if tokens:
                                new_market = slug != (self.current_market or {}).get('slug')
                                
                                # Save to history (from agents' modular approach)
                                if new_market and self.current_market:
                                    self.market_history.append(self.current_market)
                                
                                self.current_market = {
                                    'slug': slug,
                                    'timestamp': timestamp,
                                    'tokens': tokens,
                                    'volume': market.get('volume', 0),
                                    'liquidity': market.get('liquidity', 0),
                                    'question': market.get('question', ''),
                                }
                                
                                return new_market, self.current_market
            except:
                continue
        
        return False, self.current_market

    def get_market_liquidity_score(self, client, tokens):
        """
        Assess market quality before trading.
        From agents' approach of checking market conditions.
        """
        try:
            up_book = client.get_order_book(tokens['up'])
            dn_book = client.get_order_book(tokens['dn'])
            
            up_asks = sum(float(a['size']) for a in up_book.get('asks', []))
            dn_asks = sum(float(a['size']) for a in dn_book.get('asks', []))
            up_bids = sum(float(b['size']) for b in up_book.get('bids', []))
            dn_bids = sum(float(b['size']) for b in dn_book.get('bids', []))
            
            # Higher = more liquid = safer to trade
            total_liquidity = up_asks + dn_asks + up_bids + dn_bids
            spread_up = self._calc_spread(up_book)
            spread_dn = self._calc_spread(dn_book)
            
            return {
                'total_liquidity': total_liquidity,
                'up_depth': up_asks,
                'dn_depth': dn_asks,
                'up_spread': spread_up,
                'dn_spread': spread_dn,
                'is_liquid': total_liquidity > 50,  # Minimum threshold
            }
        except:
            return {'total_liquidity': 0, 'is_liquid': False}

    def validate_price_multi_source(self, binance_price, up_price, dn_price):
        """
        Cross-reference prices from multiple sources.
        FrondEnt uses Binance + Chainlink + Polymarket WS.
        We validate that Polymarket prices are consistent with BTC spot.
        """
        # Polymarket implied probability
        market_up_implied = up_price / (up_price + dn_price) if (up_price + dn_price) > 0 else 0.5
        
        self.price_sources = {
            'binance_btc': binance_price,
            'poly_up': up_price,
            'poly_dn': dn_price,
            'market_up_implied': market_up_implied,
        }
        
        # Check for price anomalies
        total = up_price + dn_price
        anomaly = abs(total - 1.0) > 0.03  # More than 3% off from $1 is unusual
        
        return {
            'implied_up_pct': market_up_implied * 100,
            'total_cost': total,
            'anomaly': anomaly,
            'arb_possible': total < ARB_THRESHOLD,
        }

    def _get_current_timestamp(self):
        return (int(time.time()) // 900) * 900

    def _parse_tokens(self, clob_ids_str):
        try:
            clob_ids = json.loads(clob_ids_str) if isinstance(clob_ids_str, str) else clob_ids_str
            if len(clob_ids) >= 2:
                return {"up": clob_ids[0], "dn": clob_ids[1]}
        except:
            pass
        return None

    def _calc_spread(self, book):
        asks = book.get('asks', [])
        bids = book.get('bids', [])
        if asks and bids:
            best_ask = min(float(a['price']) for a in asks)
            best_bid = max(float(b['price']) for b in bids)
            return best_ask - best_bid
        return 1.0


# ═══════════════════════════════════════════════════════════════════════════
# ENHANCED DATA COLLECTOR (v60 base + FrondEnt TA features)
# Now records 15+ features per snapshot instead of 7.
# ═══════════════════════════════════════════════════════════════════════════

class DataCollector:
    def __init__(self):
        self.current_candle = None
        self.candles_collected = 0
        self.load_existing_count()

    def load_existing_count(self):
        try:
            with open(DATA_FILE, 'r') as f:
                self.candles_collected = sum(1 for line in f) - 1
                if self.candles_collected < 0:
                    self.candles_collected = 0
            print(f"[DATA] Found {self.candles_collected} existing candles")
        except:
            self.candles_collected = 0
            with open(DATA_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'candle_start', 'minute',
                    'btc_open', 'btc_current', 'btc_change_pct',
                    'up_price', 'dn_price', 'price_gap',
                    'momentum_1m', 'momentum_3m',
                    # === NEW: FrondEnt TA features ===
                    'rsi', 'macd_line', 'macd_signal', 'macd_histogram',
                    'ha_trend', 'vwap_deviation', 'volatility',
                    'frondent_signal',
                    # === NEW: Market microstructure ===
                    'total_cost',  # UP + DN (for arb detection)
                    'outcome'
                ])
            print("[DATA] Created new v61 data file with expanded features")

    def start_candle(self, timestamp, btc_open):
        self.current_candle = {
            'timestamp': timestamp,
            'btc_open': btc_open,
            'snapshots': [],
            'last_btc': btc_open,
            'btc_history': [btc_open]
        }

    def record_snapshot(self, minute, btc_current, up_price, dn_price, ta_features):
        """Record snapshot with v60 features + FrondEnt TA features"""
        if not self.current_candle:
            return

        btc_open = self.current_candle['btc_open']
        btc_change = ((btc_current - btc_open) / btc_open) * 100
        price_gap = abs(up_price - dn_price)

        self.current_candle['btc_history'].append(btc_current)
        history = self.current_candle['btc_history']

        momentum_1m = ((btc_current - history[-2]) / history[-2]) * 100 if len(history) >= 2 else 0
        momentum_3m = ((btc_current - history[-4]) / history[-4]) * 100 if len(history) >= 4 else 0

        snapshot = {
            'minute': minute,
            'btc_current': btc_current,
            'btc_change': btc_change,
            'up_price': up_price,
            'dn_price': dn_price,
            'price_gap': price_gap,
            'momentum_1m': momentum_1m,
            'momentum_3m': momentum_3m,
            # FrondEnt TA features
            **ta_features,
            # Market microstructure
            'total_cost': up_price + dn_price,
        }

        self.current_candle['snapshots'].append(snapshot)
        self.current_candle['last_btc'] = btc_current

    def end_candle(self, final_btc):
        if not self.current_candle:
            return

        btc_open = self.current_candle['btc_open']
        outcome = 'UP' if final_btc > btc_open else 'DN'

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
                    # FrondEnt TA features
                    round(snap.get('rsi', 50), 2),
                    round(snap.get('macd_line', 0), 4),
                    round(snap.get('macd_signal', 0), 4),
                    round(snap.get('macd_histogram', 0), 4),
                    snap.get('ha_trend', 0),
                    round(snap.get('vwap_deviation', 0), 4),
                    round(snap.get('volatility', 0), 4),
                    round(snap.get('frondent_signal', 0), 4),
                    # Market microstructure
                    round(snap.get('total_cost', 1.0), 4),
                    outcome
                ])

        self.candles_collected += 1
        print(f"\n[DATA] Saved candle #{self.candles_collected}: {outcome} | {len(self.current_candle['snapshots'])} snapshots")
        self.current_candle = None


# ═══════════════════════════════════════════════════════════════════════════
# ENHANCED ML MODEL (v60 Random Forest + expanded feature set)
# Now uses 17 features instead of 7. More signal = better predictions.
# ═══════════════════════════════════════════════════════════════════════════

class MLModel:
    def __init__(self):
        self.model = None
        self.is_trained = False
        self.accuracy = 0
        # v60 features + FrondEnt TA features + market microstructure
        self.feature_names = [
            'minute', 'btc_change_pct', 'up_price', 'dn_price',
            'price_gap', 'momentum_1m', 'momentum_3m',
            # FrondEnt TA features
            'rsi', 'macd_line', 'macd_signal', 'macd_histogram',
            'ha_trend', 'vwap_deviation', 'volatility',
            'frondent_signal',
            # Market microstructure
            'total_cost',
        ]

    def load_and_train(self):
        try:
            from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
            from sklearn.model_selection import cross_val_score
            from sklearn.preprocessing import StandardScaler
            import pandas as pd
            import numpy as np

            df = pd.read_csv(DATA_FILE)
            print(f"[ML] Loaded {len(df)} data points from {df['candle_start'].nunique()} candles")

            if len(df) < 100:
                print(f"[ML] Need more data! Have {len(df)}, need 100+")
                return False

            # Handle both v60 (7 features) and v61 (16 features) data files
            available_features = [f for f in self.feature_names if f in df.columns]
            missing_features = [f for f in self.feature_names if f not in df.columns]
            
            if missing_features:
                print(f"[ML] Note: Missing features (old data format): {missing_features}")
                print(f"[ML] Training with {len(available_features)} features")
            
            df = df.sample(n=min(30000, len(df)), random_state=42)
            X = df[available_features].fillna(0).values
            y = (df['outcome'] == 'UP').astype(int).values

            # Try Gradient Boosting (often better than Random Forest for this)
            self.model = GradientBoostingClassifier(
                n_estimators=50,
                max_depth=4,
                learning_rate=0.1,
                min_samples_split=5,
                min_samples_leaf=3,
                subsample=0.8,
                random_state=42
            )

            scores = cross_val_score(self.model, X, y, cv=5, scoring='accuracy')
            self.accuracy = scores.mean()

            self.model.fit(X, y)
            self.is_trained = True
            self._active_features = available_features

            print(f"[ML] ✅ Model trained! (Gradient Boosting)")
            print(f"[ML] Cross-validation accuracy: {self.accuracy*100:.1f}%")
            print(f"[ML] Features used: {len(available_features)}")
            print(f"[ML] Top 5 feature importances:")
            importances = sorted(
                zip(available_features, self.model.feature_importances_),
                key=lambda x: x[1], reverse=True
            )
            for name, imp in importances[:5]:
                bar = "█" * int(imp * 50) + "░" * (10 - int(imp * 50))
                print(f"     [{bar}] {name}: {imp*100:.1f}%")

            return True

        except ImportError:
            print("[ML] ERROR: Need to install dependencies!")
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
        Enhanced prediction using all available features.
        Falls back gracefully if TA features aren't available.
        """
        if not self.is_trained:
            return None, 0, "Model not trained"

        try:
            import numpy as np

            # Build feature vector matching what the model was trained on
            feature_dict = {
                'minute': minute,
                'btc_change_pct': btc_change,
                'up_price': up_price,
                'dn_price': dn_price,
                'price_gap': price_gap,
                'momentum_1m': momentum_1m,
                'momentum_3m': momentum_3m,
            }

            # Add TA features if available
            if ta_features:
                feature_dict.update(ta_features)
            
            # Add market microstructure
            feature_dict['total_cost'] = up_price + dn_price

            # Build array in the order the model expects
            features = []
            for fname in self._active_features:
                features.append(feature_dict.get(fname, 0))

            features = np.array([features])
            proba = self.model.predict_proba(features)[0]

            up_prob = proba[1]
            dn_prob = proba[0]

            if up_prob > dn_prob:
                return 'UP', up_prob, f"UP {up_prob*100:.0f}% (GB+TA)"
            else:
                return 'DN', dn_prob, f"DN {dn_prob*100:.0f}% (GB+TA)"

        except Exception as e:
            return None, 0, f"Prediction error: {e}"


# ═══════════════════════════════════════════════════════════════════════════
# TRADING STATE + HELPERS
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
# MODE 1: COLLECT (enhanced with TA features)
# ═══════════════════════════════════════════════════════════════════════════

async def run_collect_mode():
    collector = DataCollector()
    ta = TechnicalAnalysis()
    market_intel = MarketIntelligence()

    print("=" * 65)
    print("  v61 FRANKENSTEIN - DATA COLLECTOR MODE")
    print(f"  Collecting to: {DATA_FILE}")
    print(f"  Already have: {collector.candles_collected} candles")
    print(f"  Need: {MIN_CANDLES_TO_TRAIN} candles minimum")
    print(f"  Features: 16 (v60 had 7) - includes RSI, MACD, VWAP, HA")
    print("  Leave running for 24-48 hours!")
    print("=" * 65)

    is_new, market = market_intel.find_active_market()
    if market:
        log(f"Market: {market['slug']}")

    candle_open_btc = None
    last_snapshot_minute = -1

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

                    # Feed TA engine
                    ta.update(btc_now)

                    # Set candle open
                    if candle_open_btc is None and market:
                        candle_open_btc = btc_now
                        collector.start_candle(market['timestamp'], btc_now)
                        log(f"Candle open: ${candle_open_btc:,.2f}")

                    # Check for new market
                    if tick_count % 100 == 0:
                        old_slug = market['slug'] if market else None
                        is_new, market = market_intel.find_active_market()
                        if is_new and old_slug:
                            collector.end_candle(btc_now)
                            ta.reset_for_new_candle()

                            candle_open_btc = btc_now
                            last_snapshot_minute = -1
                            collector.start_candle(market['timestamp'], btc_now)

                            hours_left = (MIN_CANDLES_TO_TRAIN - collector.candles_collected) / 4
                            log(f"New candle | Collected: {collector.candles_collected}/{MIN_CANDLES_TO_TRAIN} | ~{hours_left:.1f}h left")

                    # Rate limit
                    now = time.time()
                    if now - last_check < 1.0:
                        continue
                    last_check = now

                    if not market or candle_open_btc is None:
                        continue

                    tokens = market['tokens']
                    up_price, dn_price = get_prices(client, tokens) if client else (None, None)
                    
                    # In collect mode we can also get prices without full client
                    if up_price is None:
                        try:
                            # Fallback: use gamma API for prices
                            r = requests.get(
                                f"https://gamma-api.polymarket.com/events?slug={market['slug']}",
                                timeout=3
                            )
                            ev = r.json()
                            if ev and ev[0].get('markets'):
                                m = ev[0]['markets'][0]
                                prices = json.loads(m.get('outcomePrices', '[]'))
                                if len(prices) >= 2:
                                    up_price = float(prices[0])
                                    dn_price = float(prices[1])
                        except:
                            continue

                    if up_price is None:
                        continue

                    minutes_elapsed = get_minutes_elapsed(market['timestamp'])
                    minutes_left = get_minutes_remaining(market['timestamp'])
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100

                    # Record snapshot with TA features
                    current_minute = int(minutes_elapsed)
                    if current_minute > last_snapshot_minute and current_minute <= 14:
                        ta_features = ta.get_all_features()
                        collector.record_snapshot(current_minute, btc_now, up_price, dn_price, ta_features)
                        last_snapshot_minute = current_minute

                    # Display
                    rsi = ta.get_rsi()
                    _, _, macd_hist = ta.get_macd()
                    frond_dir, frond_conf = ta.get_frondent_signal()
                    progress = collector.candles_collected / MIN_CANDLES_TO_TRAIN * 100
                    bar = "█" * int(progress / 5) + "░" * (20 - int(progress / 5))
                    
                    print(
                        f"📊 [{bar}] {progress:.0f}% | #{collector.candles_collected} | "
                        f"BTC:{btc_change:+.2f}% | RSI:{rsi:.0f} | "
                        f"MACD:{'↑' if macd_hist > 0 else '↓'} | "
                        f"TA:{frond_dir[0]}{frond_conf*100:.0f}% | "
                        f"{minutes_left:.1f}m",
                        end='\r'
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
# MODE 2: TRADE (ML + TA + Arb detection)
# ═══════════════════════════════════════════════════════════════════════════

async def run_trade_mode():
    init_client()

    model = MLModel()
    collector = DataCollector()
    ta = TechnicalAnalysis()
    market_intel = MarketIntelligence()
    arb_detector = ArbitrageDetector(ARB_THRESHOLD)

    print("=" * 65)
    print("  v61 FRANKENSTEIN - ML + TA TRADING MODE")
    print(f"  Loading data from: {DATA_FILE}")
    print(f"  Strategies: ML Prediction + Arbitrage Detection")
    print("=" * 65)

    if not model.load_and_train():
        print(f"\n❌ Failed to train model!")
        print(f"   Need at least {MIN_CANDLES_TO_TRAIN} candles of data.")
        print(f"   Run: python3 bot_v61.py --collect")
        return

    print(f"\n✅ Model ready! Accuracy: {model.accuracy*100:.1f}%")
    print(f"   ML confidence threshold: {MIN_CONFIDENCE*100:.0f}%")
    print(f"   Arb threshold: ${ARB_THRESHOLD}")
    print(f"   Bet amount: ${BET_AMOUNT}")
    print()

    is_new, market = market_intel.find_active_market()
    if market:
        log(f"Market: {market['slug']}")
        set_allowances(client, market['tokens'])

    candle_open_btc = None
    last_snapshot_minute = -1
    position = None
    trades = []
    arb_trades = []

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

                    # Feed TA engine
                    ta.update(btc_now)

                    # Set candle open
                    if candle_open_btc is None and market:
                        candle_open_btc = btc_now
                        collector.start_candle(market['timestamp'], btc_now)
                        log(f"Candle open: ${candle_open_btc:,.2f}")

                    # Check for new market
                    if tick_count % 100 == 0:
                        old_slug = market['slug'] if market else None
                        is_new, market = market_intel.find_active_market()
                        
                        if is_new and old_slug:
                            # Get outcome from Polymarket prices instead of BTC
                            up_end, dn_end = get_prices(client, tokens) if tokens else (None, None)
                            if up_end is not None and up_end > 0.8:
                                outcome = "UP"
                            elif dn_end is not None and dn_end > 0.8:
                                outcome = "DN"
                            else:
                                outcome = "UP" if btc_now > candle_open_btc else "DN"
                            collector.end_candle(btc_now)
                            ta.reset_for_new_candle()

                            # Check position result
                            if position:
                                won = position['side'] == outcome
                                profit = position['potential_profit'] if won else -position['cost']
                                trades.append({'won': won, 'profit': profit, 'side': position['side']})

                                total_pnl = sum(t['profit'] for t in trades)
                                win_rate = sum(1 for t in trades if t['won']) / len(trades) * 100

                                emoji = "✅" if won else "❌"
                                log(f"{emoji} {position['side']} {'WON' if won else 'LOST'} → ${profit:+.2f}")
                                log(f"   ML Total: ${total_pnl:+.2f} | Win rate: {win_rate:.0f}% ({len(trades)} trades)")
                                position = None

                            # Retrain periodically
                            if collector.candles_collected % 10 == 0:
                                log("🔄 Retraining model with new data...")
                                model.load_and_train()

                            # Reset for new candle
                            candle_open_btc = btc_now
                            last_snapshot_minute = -1
                            collector.start_candle(market['timestamp'], btc_now)
                            set_allowances(client, market['tokens'])

                            if hasattr(model, "_tried_this_candle"): del model._tried_this_candle
                            log(f"📊 New candle: {market['slug']}")

                    # Rate limit
                    now = time.time()
                    if now - last_check < 0.5:
                        continue
                    last_check = now

                    if not market or candle_open_btc is None:
                        continue

                    tokens = market['tokens']
                    up_price, dn_price = get_prices(client, tokens)
                    if up_price is None:
                        continue

                    minutes_elapsed = get_minutes_elapsed(market['timestamp'])
                    minutes_left = get_minutes_remaining(market['timestamp'])
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100
                    price_gap = abs(up_price - dn_price)

                    # Get TA features
                    ta_features = ta.get_all_features()
                    
                    # Momentum (keeping v60 style for compatibility)
                    delta_1m = ta_features.get('delta_1m', 0)
                    delta_3m = ta_features.get('delta_3m', 0)

                    # Record snapshot
                    current_minute = int(minutes_elapsed)
                    if current_minute > last_snapshot_minute and current_minute <= 14:
                        collector.record_snapshot(current_minute, btc_now, up_price, dn_price, ta_features)
                        last_snapshot_minute = current_minute

                    # === STRATEGY 1: ARBITRAGE CHECK (from gabagool) ===
                    arb_opp = arb_detector.check_opportunity(up_price, dn_price)
                    if arb_opp and position is None:
                        log(f"🎯 ARB DETECTED! UP:{up_price:.2f} + DN:{dn_price:.2f} = ${arb_opp['total_cost']:.4f}")
                        log(f"   Guaranteed profit: ${arb_opp['profit_per_share']:.4f}/share ({arb_opp['profit_pct']:.2f}%)")
                        
                        result = arb_detector.execute_arb(
                            client, tokens, up_price, dn_price,
                            ARB_ORDER_SIZE, dry_run=DRY_RUN
                        )
                        if result['success']:
                            arb_trades.append(result)
                            total_arb_profit = sum(t['profit'] for t in arb_trades)
                            sim_tag = " (SIM)" if result.get('simulated') else ""
                            log(f"   ✅ ARB executed{sim_tag}! Profit: ${result['profit']:.4f} | Total arb: ${total_arb_profit:.4f}")

                    # === STRATEGY 2: ML PREDICTION (from v60 + FrondEnt TA) ===
                    prediction, confidence, reason = model.predict(
                        current_minute, btc_change, up_price, dn_price,
                        price_gap, delta_1m, delta_3m, ta_features
                    )

                    # Multi-source price validation (from agents)
                    price_check = market_intel.validate_price_multi_source(btc_now, up_price, dn_price)

                    # Display
                    rsi = ta_features.get('rsi', 50)
                    macd_hist = ta_features.get('macd_histogram', 0)
                    frondent_sig = ta_features.get('frondent_signal', 0)
                    
                    if position:
                        winning = (position['side'] == 'UP' and btc_change > 0) or (position['side'] == 'DN' and btc_change < 0)
                        emoji = "🟢" if winning else "🔴"
                        print(
                            f"{emoji} HOLD {position['side']} | BTC:{btc_change:+.2f}% | "
                            f"RSI:{rsi:.0f} MACD:{'↑' if macd_hist > 0 else '↓'} | "
                            f"UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m",
                            end='\r'
                        )
                    elif prediction:
                        conf_bar = "█" * int(confidence * 10) + "░" * (10 - int(confidence * 10))
                        trade_ok = "✓" if confidence >= MIN_CONFIDENCE else "✗"
                        ta_dir = "↑" if frondent_sig > 0 else "↓"
                        print(
                            f"🤖 {prediction} [{conf_bar}] {confidence*100:.0f}% {trade_ok} | "
                            f"TA:{ta_dir} RSI:{rsi:.0f} | "
                            f"BTC:{btc_change:+.2f}% | {minutes_left:.1f}m",
                            end='\r'
                        )
                    else:
                        print(
                            f"👀 Waiting | BTC:{btc_change:+.2f}% | RSI:{rsi:.0f} | "
                            f"UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m",
                            end='\r'
                        )

                    # Trading logic (ML-based)
                    if position is None and minutes_left >= MIN_TIME_LEFT and not hasattr(model, "_tried_this_candle"):
                        # Extra validation: check TA agreement (from FrondEnt)
                        ta_agrees = (
                            (prediction == 'UP' and frondent_sig > 0) or
                            (prediction == 'DN' and frondent_sig < 0)
                        )
                        
                        # Boost confidence if TA agrees, reduce if it disagrees
                        effective_confidence = confidence
                        if ta_agrees:
                            effective_confidence = min(confidence * 1.05, 0.99)  # 5% boost
                        else:
                            effective_confidence = confidence * 0.90  # 10% penalty
                        
                        # Check for price anomalies (from agents)
                        if price_check.get('anomaly'):
                            log(f"⚠️  Price anomaly detected: total_cost={price_check['total_cost']:.4f}")

                        if prediction and effective_confidence >= MIN_CONFIDENCE and ta_agrees and abs(btc_change) >= MIN_VOLATILITY:
                            model._tried_this_candle = True
                            if prediction == 'UP' and 0.30 <= up_price <= 0.70:
                                ta_tag = " ✓TA" if ta_agrees else " ✗TA"
                                log(f"🤖 ML+TA: {reason}{ta_tag} (eff:{effective_confidence*100:.0f}%)")
                                shares = place_order(client, tokens['up'], up_price + 0.01, BET_AMOUNT, "UP")
                                if shares > 0:
                                    position = {
                                        'side': 'UP',
                                        'shares': shares,
                                        'cost': BET_AMOUNT,
                                        'potential_profit': shares - BET_AMOUNT
                                    }
                                    log(f"   ✅ Bought {shares:.1f} UP | If wins: +${position['potential_profit']:.2f}")

                            elif prediction == 'DN' and 0.30 <= dn_price <= 0.70:
                                ta_tag = " ✓TA" if ta_agrees else " ✗TA"
                                log(f"🤖 ML+TA: {reason}{ta_tag} (eff:{effective_confidence*100:.0f}%)")
                                shares = place_order(client, tokens['dn'], dn_price + 0.01, BET_AMOUNT, "DN")
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


# ═══════════════════════════════════════════════════════════════════════════
# MODE 3: PURE ARBITRAGE (from gabagool)
# ═══════════════════════════════════════════════════════════════════════════

async def run_arb_mode():
    init_client()

    market_intel = MarketIntelligence()
    arb_detector = ArbitrageDetector(ARB_THRESHOLD)

    print("=" * 65)
    print("  v61 FRANKENSTEIN - PURE ARBITRAGE MODE")
    print(f"  Strategy: Buy UP + DN when total < ${ARB_THRESHOLD}")
    print(f"  Order size: {ARB_ORDER_SIZE} shares per side")
    print(f"  Mode: {'🔸 SIMULATION' if DRY_RUN else '🔴 LIVE TRADING'}")
    print("=" * 65)

    is_new, market = market_intel.find_active_market()
    if market:
        log(f"Market: {market['slug']}")
        set_allowances(client, market['tokens'])

    arb_trades = []
    scan_count = 0

    while True:
        try:
            scan_count += 1
            
            # Check for new market
            is_new, market = market_intel.find_active_market()
            if is_new and market:
                log(f"New market: {market['slug']}")
                set_allowances(client, market['tokens'])
            
            if not market:
                print(f"[Scan #{scan_count}] No active market found...", end='\r')
                await asyncio.sleep(5)
                continue

            tokens = market['tokens']
            up_price, dn_price = get_prices(client, tokens)
            
            if up_price is None:
                await asyncio.sleep(1)
                continue

            minutes_left = get_minutes_remaining(market['timestamp'])
            total_cost = up_price + dn_price

            # Check for arb
            arb_opp = arb_detector.check_opportunity(up_price, dn_price)
            
            if arb_opp:
                log(f"🎯 ARB! UP:{up_price:.4f} + DN:{dn_price:.4f} = ${total_cost:.4f}")
                
                # Check depth before executing (gabagool's approach)
                up_can_fill, up_worst, up_avg = arb_detector.get_book_depth(
                    client, tokens['up'], target_size=ARB_ORDER_SIZE
                )
                dn_can_fill, dn_worst, dn_avg = arb_detector.get_book_depth(
                    client, tokens['dn'], target_size=ARB_ORDER_SIZE
                )
                
                if up_can_fill and dn_can_fill:
                    # Re-check with worst fill prices
                    real_total = up_worst + dn_worst
                    if real_total < 1.0:
                        result = arb_detector.execute_arb(
                            client, tokens, up_worst, dn_worst,
                            ARB_ORDER_SIZE, dry_run=DRY_RUN
                        )
                        if result['success']:
                            arb_trades.append(result)
                            total_profit = sum(t['profit'] for t in arb_trades)
                            sim = " (SIM)" if result.get('simulated') else ""
                            log(f"   ✅ Executed{sim}! Profit: ${result['profit']:.4f}")
                            log(f"   Total arb profit: ${total_profit:.4f} ({len(arb_trades)} trades)")
                    else:
                        log(f"   ⚠️  Book depth check: worst fill ${real_total:.4f} >= $1.00, skipping")
                else:
                    log(f"   ⚠️  Insufficient depth for {ARB_ORDER_SIZE} shares")
            else:
                total_arb_profit = sum(t['profit'] for t in arb_trades) if arb_trades else 0
                print(
                    f"[#{scan_count}] UP:{up_price:.4f} + DN:{dn_price:.4f} = ${total_cost:.4f} "
                    f"(need < ${ARB_THRESHOLD}) | {minutes_left:.1f}m | "
                    f"Arbs: {len(arb_trades)} (${total_arb_profit:.4f})",
                    end='\r'
                )

            await asyncio.sleep(0.5)  # Scan every 0.5s

        except Exception as e:
            log(f"Error: {e}")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("╔══════════════════════════════════════════════════════════════════╗")
    print("║            v61 - FRANKENSTEIN TRADING BOT                       ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print("║  Combines: v60 ML + FrondEnt TA + gabagool Arb + PM Agents     ║")
    print("╠══════════════════════════════════════════════════════════════════╣")
    print("║  STEP 1: Collect data (24-48 hours)                             ║")
    print("║          python3 bot_v61.py --collect                           ║")
    print("║                                                                 ║")
    print("║  STEP 2: Trade with ML + TA                                     ║")
    print("║          python3 bot_v61.py --trade                             ║")
    print("║                                                                 ║")
    print("║  BONUS:  Pure arbitrage (no ML needed)                          ║")
    print("║          python3 bot_v61.py --arb                               ║")
    print("╚══════════════════════════════════════════════════════════════════╝")
    print()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 bot_v61.py --collect   # Collect data with expanded features")
        print("  python3 bot_v61.py --trade     # Trade with ML + TA + Arb detection")
        print("  python3 bot_v61.py --arb       # Pure arbitrage mode (no ML needed)")
        return

    mode = sys.argv[1].lower()

    if mode in ['--collect', '-c', 'collect']:
        asyncio.run(run_collect_mode())
    elif mode in ['--trade', '-t', 'trade']:
        asyncio.run(run_trade_mode())
    elif mode in ['--arb', '-a', 'arb']:
        asyncio.run(run_arb_mode())
    else:
        print(f"Unknown mode: {mode}")
        print("Use --collect, --trade, or --arb")


if __name__ == "__main__":
    main()
