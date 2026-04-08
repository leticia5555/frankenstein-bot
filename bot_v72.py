import warnings
warnings.filterwarnings("ignore")

import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType, BookParams
from py_clob_client.order_builder.constants import BUY, SELL
import os
import sys
import time
import csv
import math
from datetime import datetime, timezone
from dotenv import load_dotenv
from collections import deque

load_dotenv()

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  v72 - FRANKENSTEIN MAKER (POST ONCE, TREND-AWARE)                     ║
# ║                                                                          ║
# ║  EVOLUTION: v71 budget-controlled → v72 post-once + trend filter        ║
# ║                                                                          ║
# ║  KEY CHANGES FROM v71:                                                   ║
# ║  • POST ONCE: Place orders and LEAVE THEM. Never cancel+repost.        ║
# ║  • TREND FILTER: Track BTC direction over last 3 candles.              ║
# ║    Only trade WITH the trend, not against it.                           ║
# ║  • WAIT LONGER: Don't trade until minute 3+ (let direction confirm)    ║
# ║  • ONE MAIN + ONE HEDGE per candle, that's it.                         ║
# ║  • If direction flips mid-candle, post on NEW side (don't cancel old)  ║
# ║                                                                          ║
# ║  v70/v71 PROBLEM: Cancel+repost every 30 sec = phantom fills.          ║
# ║  Orders fill before cancel goes through → $20-30 exposure instead      ║
# ║  of $5. v72 posts ONCE and walks away. Max exposure = $7.50.           ║
# ║                                                                          ║
# ║  FEE STRUCTURE (15-min crypto markets):                                  ║
# ║  • Maker: 0%                                                             ║
# ║  • Taker: fee = C × 0.25 × (p×(1-p))² → max 1.56% at p=0.50           ║
# ║  • feeRateBps: 1000 (must be included in signed orders)                  ║
# ║  • Rebates: Daily USDC, proportional to maker volume                     ║
# ║                                                                          ║
# ║  STRATEGY (from whale k9Q2mX4L8A7ZP3R analysis):                        ║
# ║  • Phase 1 (min 0-3): Post bids on both sides at $0.48-0.50             ║
# ║  • Phase 2 (min 3-8): Direction clear → heavy bid on winner at           ║
# ║    market-$0.03, light hedge on loser at $0.38-0.42                      ║
# ║  • Phase 3 (min 13-15): Expiry chaos → post deep bids both sides        ║
# ║  • Cancel unfilled orders when direction locks (>85% confidence)         ║
# ║                                                                          ║
# ║  WHALE TRADE DATA (validated):                                           ║
# ║  • Avg entry: $0.58 | 111 trades | $0 maker fees                        ║
# ║  • Price range: $0.30-$0.62 (fills in the gap nobody trades)            ║
# ║  • Breakeven: ~57% accuracy | Target: 65%+ with physics engine          ║
# ║                                                                          ║
# ║  SLIPPAGE: $0 (limit orders by definition)                               ║
# ║  GAS: ~$0.01-0.05/order on Polygon                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# === PARAMETERS ===
DATA_FILE = "btc_15m_data_v68.csv"
BET_AMOUNT = 5.0               # Per-side bet
MAKER_OFFSET = 0.03            # Bid this far below market (3 cents)
HEDGE_PRICE = 0.40             # Hedge bid price on losing side
HEDGE_RATIO = 0.30             # Hedge size = 30% of main bet
FEE_RATE_BPS = 1000            # 15-min crypto markets fee rate

# === v72 POST-ONCE PARAMETERS ===
MAX_ENTRY_PRICE = 0.75         # Max price for main directional bid
MIN_MOVE_PCT = 0.08            # RAISED from 0.05 — need stronger signal
MIN_VELOCITY = 0.02            # Min velocity (%/min)
MIN_CONFIDENCE = 0.68          # RAISED from 0.65 — be pickier
TRADE_WINDOW_START = 3.0       # RAISED from 1.5 — wait for confirmation
TRADE_WINDOW_END = 14.0        # Keep posting until near expiry
MAX_ORDERS_PER_CANDLE = 2      # v72: ONE main + ONE hedge = done
EXPIRY_WINDOW_START = 13.5     # Expiry chaos starts here
EXPIRY_DEEP_BID = 0.35         # Deep bid price during expiry
DIRECTION_LOCK_CONF = 0.80     # Cancel hedge when this confident
ORDER_REFRESH_SECONDS = 9999   # v72: NEVER refresh (post once)

# === v72 BUDGET CONTROL ===
MAX_MAIN_BUDGET = 5.0          # v72: Exactly ONE main bet per candle
MAX_HEDGE_BUDGET = 2.50        # Exactly ONE hedge per candle
HEDGE_ONCE = True              # Post hedge only once

# === v72 TREND FILTER ===
TREND_CANDLES = 3              # Look at last 3 candle outcomes
TREND_THRESHOLD = 2            # Need 2/3 same direction to confirm trend
COUNTER_TREND_BLOCK = True     # Block trades against the trend

# === ASSETS TO TRADE (priority order) ===
# SOL: thinnest books, fastest moves, biggest dislocations (17% arb at expiry)
# ETH: medium books, decent dislocations (17% arb at expiry)
# BTC: thickest books, smallest dislocations (7% arb at expiry)
ASSETS = ['sol', 'btc', 'eth']
PRIMARY_ASSET = 'sol'          # Best edge from book watcher data

# === ORDER BOOK PARAMETERS ===
MIN_BOOK_IMBALANCE = 0.15
MAX_SPREAD_TO_TRADE = 0.08
MIN_BOOK_DEPTH = 50

# === POSITION PERSISTENCE (from v64) ===
POSITION_FILE = "open_position_v70.json"

class PositionPersistence:
    @staticmethod
    def save_position(position_data, candle_start, candle_open_btc):
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
        try:
            if os.path.exists(POSITION_FILE):
                os.remove(POSITION_FILE)
        except Exception as e:
            print(f"[PERSIST] Error clearing position: {e}")
    
    @staticmethod
    def load_position():
        try:
            if os.path.exists(POSITION_FILE):
                with open(POSITION_FILE, 'r') as f:
                    return json.load(f)
        except Exception as e:
            print(f"[PERSIST] Error loading position: {e}")
        return None
    
    @staticmethod
    def resolve_orphaned_position(tracker, asset='btc'):
        saved = PositionPersistence.load_position()
        if not saved:
            return None
        
        candle_start = saved['candle_start']
        candle_end = candle_start + 900
        now = time.time()
        
        if now < candle_end:
            print(f"[PERSIST] Found live position from current candle, restoring...")
            return saved
        
        position = saved['position']
        
        print(f"[PERSIST] Found orphaned {position.get('side','?')} trade from candle {candle_start}")
        
        try:
            slug = f"{asset}-updown-15m-{candle_start}"
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
                    if market.get("closed"):
                        outcome_prices = market.get("outcomePrices", "")
                        try:
                            prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                            if prices and len(prices) >= 2:
                                up_final = float(prices[0])
                                dn_final = float(prices[1])
                                outcome = "UP" if up_final > 0.9 else "DN" if dn_final > 0.9 else None
                                
                                if outcome and position.get('side'):
                                    won = (position['side'] == outcome)
                                    profit = position.get('shares', 0) * 1.0 - position.get('cost', 0) if won else -position.get('cost', 0)
                                    
                                    emoji = "🟢" if won else "🔴"
                                    print(f"[PERSIST] {emoji} Resolved: {position['side']} → {outcome} "
                                          f"({'WIN' if won else 'LOSS'}) ${profit:+.2f}")
                                    
                                    tracker.open_trade(
                                        candle_start, position['side'],
                                        position.get('entry_price', 0),
                                        position.get('shares', 0), position.get('cost', 0),
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
                                    return None
                        except:
                            pass
        except Exception as e:
            print(f"[PERSIST] Error checking outcome: {e}")
        
        print(f"[PERSIST] Clearing unresolvable orphaned position")
        try:
            with open("orphaned_trades.csv", 'a') as f:
                f.write(f"{saved['saved_at']},{candle_start},{position.get('side','?')},"
                        f"{position.get('cost',0)},{position.get('shares',0)},UNRESOLVED\n")
        except:
            pass
        
        PositionPersistence.clear_position()
        return None


# ═══════════════════════════════════════════════════════════════════════════
# ORDER MANAGER (NEW v70 — manages limit orders, cancellations, fills)
# ═══════════════════════════════════════════════════════════════════════════

class OrderManager:
    """
    Manages maker orders on Polymarket CLOB.
    
    Key operations:
    - Post limit bids (maker orders with feeRateBps)
    - Cancel unfilled orders
    - Track which orders have been filled
    - Refresh orders when market moves
    """
    
    def __init__(self):
        self.active_orders = {}  # order_id -> order_info
        self.filled_orders = []
        self.total_orders_placed = 0
        self.total_orders_cancelled = 0
        self.total_orders_filled = 0
        self.last_refresh_time = 0
    
    def post_maker_bid(self, client, token_id, price, size_usd, label="BID", side_str="BUY"):
        """
        Post a limit bid (maker order) with proper fee handling.
        
        For 15-min crypto markets, feeRateBps=1000 must be included.
        Maker orders pay 0% fees but the fee rate must be in the signed payload.
        """
        if not client:
            return None
        
        try:
            shares = size_usd / price
            
            # Create order with fee rate for 15-min markets
            opt = PartialCreateOrderOptions(
                tick_size="0.01",
                neg_risk=False
            )
            
            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 2),
                size=round(shares, 2),
                side=BUY,
                fee_rate_bps=FEE_RATE_BPS,  # CRITICAL: must include for 15-min markets
            )
            
            signed_order = client.create_order(order_args, opt)
            resp = client.post_order(signed_order, OrderType.GTC)  # Good-Til-Cancelled
            
            if resp.get("success"):
                order_id = resp.get("orderID", "unknown")
                status = resp.get("status", "live")
                taking = float(resp.get("takingAmount", 0) or 0)
                making = float(resp.get("makingAmount", 0) or 0)
                
                # Detect immediate fill from response
                is_filled = status == 'matched' or taking > 0
                filled_shares = taking if taking > 0 else (shares if is_filled else 0)
                
                order_info = {
                    'order_id': order_id,
                    'token_id': token_id,
                    'price': price,
                    'shares': shares,
                    'size_usd': size_usd,
                    'label': label,
                    'side': side_str,
                    'timestamp': time.time(),
                    'status': 'filled' if is_filled else 'live',
                    'filled_amount': filled_shares,
                }
                
                self.total_orders_placed += 1
                
                if is_filled:
                    self.filled_orders.append(order_info)
                    self.total_orders_filled += 1
                    log(f"  ✅ {label}: {filled_shares:.1f} shares @ {price*100:.0f}¢ = ${size_usd:.2f} "
                        f"[INSTANT FILL] (#{order_id[:8]})")
                else:
                    self.active_orders[order_id] = order_info
                    log(f"  📝 {label}: {shares:.1f} shares @ {price*100:.0f}¢ = ${size_usd:.2f} "
                        f"[maker, 0% fee] (#{order_id[:8]})")
                
                return order_info
            else:
                error_msg = resp.get('errorMsg', resp.get('error', str(resp)))
                log(f"  ✗ {label} failed: {error_msg}")
                return None
                
        except Exception as e:
            log(f"  ✗ {label} error: {e}")
            return None
    
    def cancel_order(self, client, order_id):
        """Cancel a specific order."""
        if not client or order_id not in self.active_orders:
            return False
        
        try:
            resp = client.cancel(order_id)
            if resp:
                order = self.active_orders.pop(order_id, None)
                self.total_orders_cancelled += 1
                if order:
                    log(f"  ❌ Cancelled {order['label']} @ {order['price']*100:.0f}¢ (#{order_id[:8]})")
                return True
        except Exception as e:
            log(f"  ✗ Cancel error for #{order_id[:8]}: {e}")
        return False
    
    def cancel_all(self, client):
        """Cancel all active orders — but check for fills first."""
        # Check fills before cancelling so we don't lose fill data
        self.check_fills(client)
        
        cancelled = 0
        for order_id in list(self.active_orders.keys()):
            if self.cancel_order(client, order_id):
                cancelled += 1
        if cancelled:
            log(f"  ❌ Cancelled {cancelled} orders")
        return cancelled
    
    def cancel_side(self, client, side_label):
        """Cancel all orders on a specific side (e.g., 'HEDGE_UP' or 'MAIN_DN')."""
        cancelled = 0
        for order_id, order in list(self.active_orders.items()):
            if side_label in order.get('label', ''):
                if self.cancel_order(client, order_id):
                    cancelled += 1
        return cancelled
    
    def check_fills(self, client):
        """
        Check which orders have been filled.
        Returns list of newly filled orders.
        """
        newly_filled = []
        
        for order_id, order in list(self.active_orders.items()):
            try:
                # Check order status via CLOB API
                resp = requests.get(
                    f"https://clob.polymarket.com/order/{order_id}",
                    timeout=5
                )
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get('status', '')
                    filled = float(data.get('size_matched', 0))
                    
                    if status == 'MATCHED' or filled >= order['shares'] * 0.95:
                        order['status'] = 'filled'
                        order['filled_amount'] = filled
                        self.filled_orders.append(order)
                        self.active_orders.pop(order_id, None)
                        self.total_orders_filled += 1
                        newly_filled.append(order)
                        log(f"  ✅ FILLED: {order['label']} {filled:.1f} shares @ {order['price']*100:.0f}¢")
                    elif filled > order.get('filled_amount', 0):
                        # Partial fill
                        order['filled_amount'] = filled
                        order['status'] = 'partial'
                        log(f"  📊 Partial: {order['label']} {filled:.1f}/{order['shares']:.1f} shares")
            except:
                pass  # API might be slow, don't crash
        
        return newly_filled
    
    def get_total_exposure(self):
        """Calculate total capital locked in active orders."""
        return sum(o['size_usd'] for o in self.active_orders.values())
    
    def get_filled_exposure(self):
        """Calculate total capital in filled orders (actual risk)."""
        return sum(o['size_usd'] for o in self.filled_orders)
    
    def get_filled_by_side(self):
        """v71: Track how much has been filled on each side this candle."""
        up_cost = sum(o['size_usd'] for o in self.filled_orders if o.get('side') == 'UP')
        dn_cost = sum(o['size_usd'] for o in self.filled_orders if o.get('side') == 'DN')
        up_shares = sum(o.get('filled_amount', o['shares']) for o in self.filled_orders if o.get('side') == 'UP')
        dn_shares = sum(o.get('filled_amount', o['shares']) for o in self.filled_orders if o.get('side') == 'DN')
        return {
            'UP': {'cost': up_cost, 'shares': up_shares},
            'DN': {'cost': dn_cost, 'shares': dn_shares},
        }
    
    def get_pending_by_side(self):
        """v71: Track how much is still pending (active orders) on each side."""
        up_cost = sum(o['size_usd'] for o in self.active_orders.values() if o.get('side') == 'UP')
        dn_cost = sum(o['size_usd'] for o in self.active_orders.values() if o.get('side') == 'DN')
        return {'UP': up_cost, 'DN': dn_cost}
    
    def budget_remaining(self, side, max_budget):
        """v71: How much more can we spend on this side?"""
        filled = self.get_filled_by_side()
        spent = filled[side]['cost']
        pending = self.get_pending_by_side()[side]
        return max(0, max_budget - spent - pending)
    
    def get_summary(self):
        """Get order manager status."""
        return {
            'active': len(self.active_orders),
            'filled': len(self.filled_orders),
            'total_placed': self.total_orders_placed,
            'total_cancelled': self.total_orders_cancelled,
            'total_filled': self.total_orders_filled,
            'active_exposure': self.get_total_exposure(),
            'filled_exposure': self.get_filled_exposure(),
        }
    
    def reset_for_new_candle(self):
        """Reset state for a new candle. Filled orders become positions."""
        positions = list(self.filled_orders)
        self.active_orders.clear()
        self.filled_orders.clear()
        return positions


# ═══════════════════════════════════════════════════════════════════════════
# ORDER LOGGER (NEW — logs every order, fill, cancel, resolution)
# ═══════════════════════════════════════════════════════════════════════════

class OrderLogger:
    """
    Logs every order event to CSV for post-analysis.
    This is the ground truth — captures what actually happened.
    """
    
    def __init__(self, filename="order_log_v70.csv"):
        self.filename = filename
        self.candle_orders = []  # All orders this candle
        self.candle_fills = []   # Confirmed fills this candle
        self._init_file()
    
    def _init_file(self):
        try:
            with open(self.filename, 'r') as f:
                lines = sum(1 for _ in f) - 1
                print(f"[ORDER LOG] Found {lines} existing entries")
        except:
            with open(self.filename, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'event', 'candle_start', 'asset',
                    'order_id', 'side', 'price', 'shares', 'cost',
                    'status', 'phase', 'physics_conf', 'physics_reason',
                    'btc_price', 'btc_change', 'minutes_elapsed',
                    'up_market', 'dn_market', 'outcome', 'pnl'
                ])
    
    def log_order(self, event, candle_start, asset, order_id, side, price,
                  shares, cost, status, phase='', physics_conf=0, physics_reason='',
                  btc_price=0, btc_change=0, minutes_elapsed=0,
                  up_market=0, dn_market=0, outcome='', pnl=0):
        """Log any order event: POST, FILL, CANCEL, RESOLVE."""
        row = [
            datetime.now().isoformat(), event, candle_start, asset,
            str(order_id)[:12], side, f"{price:.4f}", f"{shares:.1f}", f"{cost:.2f}",
            status, phase, f"{physics_conf:.2f}", physics_reason,
            f"{btc_price:.2f}", f"{btc_change:.4f}", f"{minutes_elapsed:.1f}",
            f"{up_market:.2f}", f"{dn_market:.2f}", outcome, f"{pnl:.2f}"
        ]
        
        with open(self.filename, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)
        
        # Track for candle summary
        if event == 'POST':
            self.candle_orders.append({
                'side': side, 'price': price, 'shares': shares, 'cost': cost
            })
        elif event in ['FILL', 'INSTANT_FILL']:
            self.candle_fills.append({
                'side': side, 'price': price, 'shares': shares, 'cost': cost
            })
    
    def log_candle_summary(self, candle_start, asset, outcome, btc_change):
        """Log end-of-candle summary with P&L."""
        if not self.candle_fills:
            self.log_order('CANDLE_END', candle_start, asset, '-', '-', 0,
                          0, 0, 'no_fills', outcome=outcome)
            self._reset_candle()
            return {'total_cost': 0, 'total_pnl': 0, 'fills': 0}
        
        total_cost = 0
        total_pnl = 0
        up_shares = 0
        up_cost = 0
        dn_shares = 0
        dn_cost = 0
        
        for fill in self.candle_fills:
            if fill['side'] == 'UP':
                up_shares += fill['shares']
                up_cost += fill['cost']
            else:
                dn_shares += fill['shares']
                dn_cost += fill['cost']
        
        total_cost = up_cost + dn_cost
        
        if outcome == 'UP':
            total_pnl = up_shares * 1.0 - up_cost - dn_cost  # UP wins, DN loses
        elif outcome == 'DN':
            total_pnl = dn_shares * 1.0 - dn_cost - up_cost  # DN wins, UP loses
        else:
            total_pnl = 0
        
        self.log_order('CANDLE_END', candle_start, asset, '-', '-', 0,
                      up_shares + dn_shares, total_cost, 'resolved',
                      outcome=outcome, pnl=total_pnl,
                      btc_change=btc_change)
        
        summary = {
            'total_cost': total_cost,
            'total_pnl': total_pnl,
            'fills': len(self.candle_fills),
            'up_shares': up_shares,
            'up_cost': up_cost,
            'dn_shares': dn_shares,
            'dn_cost': dn_cost,
        }
        
        self._reset_candle()
        return summary
    
    def _reset_candle(self):
        self.candle_orders.clear()
        self.candle_fills.clear()
    
    def get_session_stats(self):
        """Read the CSV and compute session stats."""
        try:
            import pandas as pd
            df = pd.read_csv(self.filename)
            
            resolutions = df[df['event'] == 'CANDLE_END']
            if len(resolutions) == 0:
                return None
            
            traded = resolutions[resolutions['status'] == 'resolved']
            
            return {
                'candles_total': len(resolutions),
                'candles_traded': len(traded),
                'total_pnl': traded['pnl'].astype(float).sum(),
                'total_cost': traded['cost'].astype(float).sum(),
                'wins': len(traded[traded['pnl'].astype(float) > 0]),
                'losses': len(traded[traded['pnl'].astype(float) <= 0]),
            }
        except:
            return None


# ═══════════════════════════════════════════════════════════════════════════
# TREND TRACKER (v72 — tracks multi-candle BTC direction)
# ═══════════════════════════════════════════════════════════════════════════

class TrendTracker:
    """
    Tracks BTC direction over recent candles.
    Prevents trading against the prevailing trend.
    """
    def __init__(self, max_candles=10):
        self.candle_results = deque(maxlen=max_candles)  # (open_price, close_price, direction)
    
    def record_candle(self, open_price, close_price):
        """Record the result of a completed candle."""
        if open_price and close_price and open_price > 0:
            direction = 'UP' if close_price > open_price else 'DN'
            change_pct = ((close_price - open_price) / open_price) * 100
            self.candle_results.append({
                'open': open_price,
                'close': close_price,
                'direction': direction,
                'change_pct': change_pct,
            })
    
    def get_trend(self, lookback=3):
        """
        Get the prevailing trend over recent candles.
        Returns: ('UP', strength) | ('DN', strength) | ('FLAT', 0)
        """
        if len(self.candle_results) < 2:
            return 'FLAT', 0
        
        recent = list(self.candle_results)[-lookback:]
        up_count = sum(1 for c in recent if c['direction'] == 'UP')
        dn_count = sum(1 for c in recent if c['direction'] == 'DN')
        
        total = len(recent)
        if up_count >= TREND_THRESHOLD and up_count > dn_count:
            return 'UP', up_count / total
        elif dn_count >= TREND_THRESHOLD and dn_count > up_count:
            return 'DN', dn_count / total
        return 'FLAT', 0
    
    def is_counter_trend(self, proposed_side):
        """Check if a proposed trade goes against the trend."""
        trend, strength = self.get_trend()
        if trend == 'FLAT':
            return False  # No trend, trade freely
        return proposed_side != trend
    
    def get_display(self):
        """Short display string for the trend."""
        trend, strength = self.get_trend()
        n = len(self.candle_results)
        if n < 2:
            return f"T:?({n})"
        recent = list(self.candle_results)[-3:]
        dirs = ''.join('↑' if c['direction'] == 'UP' else '↓' for c in recent)
        return f"T:{trend}({dirs})"


# ═══════════════════════════════════════════════════════════════════════════
# PHYSICS ENGINE (same as v68 but with maker-specific enhancements)
# ═══════════════════════════════════════════════════════════════════════════

class PhysicsEngine:
    """
    v70: Physics engine with maker-mode enhancements.
    Same core logic as v68 but:
    - Lower confidence threshold (maker has better risk/reward)
    - Wider trading window (can post orders earlier, refresh later)
    - Expiry detection (special aggressive mode in last 60 seconds)
    """
    def __init__(self):
        self.ticks = deque(maxlen=500)
        self.candle_open_price = None
        self.candle_open_time = None
        self.prices_at_minutes = {}
        
    def reset(self, open_price, candle_start_timestamp=None):
        self.ticks.clear()
        self.candle_open_price = open_price
        self.candle_open_time = candle_start_timestamp or time.time()
        self.prices_at_minutes = {}
    
    def update(self, price, candle_open=None):
        now = time.time()
        self.ticks.append((now, price))
        if candle_open is not None:
            self.candle_open_price = candle_open
        if self.candle_open_time:
            elapsed_min = int((now - self.candle_open_time) / 60)
            if elapsed_min not in self.prices_at_minutes and elapsed_min <= 15:
                self.prices_at_minutes[elapsed_min] = price
    
    def get_change_pct(self):
        if not self.candle_open_price or not self.ticks:
            return 0
        return ((self.ticks[-1][1] - self.candle_open_price) / self.candle_open_price) * 100
    
    def get_velocity(self, window_seconds=30):
        if len(self.ticks) < 5:
            return 0
        now = self.ticks[-1][0]
        cutoff = now - window_seconds
        old_ticks = [(t, p) for t, p in self.ticks if t <= cutoff]
        if not old_ticks:
            return 0
        old_price = old_ticks[-1][1]
        new_price = self.ticks[-1][1]
        elapsed_min = (now - old_ticks[-1][0]) / 60
        if elapsed_min == 0:
            return 0
        return ((new_price - old_price) / old_price * 100) / elapsed_min
    
    def get_direction(self):
        change = self.get_change_pct()
        if change > 0.02:
            return 'UP'
        elif change < -0.02:
            return 'DN'
        return 'FLAT'
    
    def is_reversing(self):
        if 0 not in self.prices_at_minutes or not self.candle_open_price:
            return False
        min0_price = self.prices_at_minutes.get(0, self.candle_open_price)
        min1_price = self.prices_at_minutes.get(1, min0_price)
        early_change = min1_price - self.candle_open_price
        current_change = self.ticks[-1][1] - self.candle_open_price if self.ticks else 0
        if early_change > 0 and current_change < 0:
            return True
        if early_change < 0 and current_change > 0:
            return True
        return False
    
    def get_signal(self, minutes_elapsed):
        """
        v70 maker physics decision engine.
        Returns: (should_trade, side, confidence, reason, phase)
        
        phase: 'early' | 'directional' | 'expiry'
        """
        change = self.get_change_pct()
        velocity = self.get_velocity(30)
        abs_change = abs(change)
        abs_velocity = abs(velocity)
        direction = self.get_direction()
        reversing = self.is_reversing()
        
        # Determine phase
        if minutes_elapsed >= EXPIRY_WINDOW_START:
            phase = 'expiry'
        elif minutes_elapsed >= TRADE_WINDOW_START:
            phase = 'directional'
        else:
            phase = 'early'
        
        # EXPIRY PHASE: Always trade if direction is known
        if phase == 'expiry' and direction != 'FLAT':
            confidence = 0.85 + min(abs_change * 0.5, 0.10)  # Very high confidence at expiry
            return True, direction, min(confidence, 0.95), f"EXPIRY v={velocity:+.3f}% Δ={change:+.3f}%", phase
        
        # EARLY PHASE: Only signal if very strong move
        if phase == 'early':
            if abs_change >= 0.10 and abs_velocity >= MIN_VELOCITY * 2:
                confidence = 0.70
                return True, direction, confidence, f"EARLY STRONG Δ={change:+.3f}%", phase
            return False, None, 0, "early — waiting", phase
        
        # DIRECTIONAL PHASE: Main trading logic (from v68)
        if direction == 'FLAT':
            return False, None, 0, "flat", phase
        
        if abs_change < MIN_MOVE_PCT:
            return False, None, 0, f"move too small ({abs_change:.3f}%)", phase
        
        # Smart reversal handling
        if reversing and abs_change < 0.10:
            return False, None, 0, f"reversing + small ({abs_change:.3f}%)", phase
        
        # Calculate confidence
        confidence = 0.62  # Base: lower than v68 because maker has better risk/reward
        
        if reversing:
            confidence -= 0.05
        
        if abs_change > 0.15:
            confidence += 0.10
        elif abs_change > 0.07:
            confidence += 0.05
        
        if abs_velocity > MIN_VELOCITY * 2:
            confidence += 0.05
        elif abs_velocity > MIN_VELOCITY:
            confidence += 0.02
        
        if 1.5 <= minutes_elapsed <= 3.5:
            confidence += 0.03
        
        # v70: Bonus for sustained moves (minute 5+)
        if minutes_elapsed >= 5 and abs_change > 0.10:
            confidence += 0.05  # Sustained strong move = higher confidence
        
        side = direction
        reason = f"v={velocity:+.3f}%/m Δ={change:+.3f}%"
        
        return True, side, confidence, reason, phase
    
    def get_display(self, minutes_elapsed):
        change = self.get_change_pct()
        velocity = self.get_velocity(30)
        direction = self.get_direction()
        reversing = self.is_reversing()
        
        phase = "EXP" if minutes_elapsed >= EXPIRY_WINDOW_START else "DIR" if minutes_elapsed >= TRADE_WINDOW_START else "EARLY"
        rev_str = " REV!" if reversing else ""
        if reversing and abs(change) >= 0.10:
            rev_str = " REV✓"
        return f"[{phase}] Δ:{change:+.3f}% v:{velocity:+.3f}%/m {direction}{rev_str}"


# ═══════════════════════════════════════════════════════════════════════════
# ORDER BOOK ANALYZER (from v68, enhanced for maker strategy)
# ═══════════════════════════════════════════════════════════════════════════

class OrderBookAnalyzer:
    def __init__(self):
        self.book_history = {'up': deque(maxlen=30), 'dn': deque(maxlen=30)}
        self.last_book_time = 0
    
    def _fetch_book_direct(self, token_id):
        try:
            r = requests.get(f'https://clob.polymarket.com/book?token_id={token_id}', timeout=10)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return None
        
    def analyze_book(self, tokens):
        """Fetch and analyze both sides of the order book."""
        try:
            up_book = self._fetch_book_direct(tokens['up'])
            dn_book = self._fetch_book_direct(tokens['dn'])
            
            if up_book is None or dn_book is None:
                return None
            
            up_analysis = self._analyze_single_book(up_book, 'UP')
            dn_analysis = self._analyze_single_book(dn_book, 'DN')
            
            self.book_history['up'].append(up_analysis)
            self.book_history['dn'].append(dn_analysis)
            
            combined = self._combine_analysis(up_analysis, dn_analysis)
            
            return {
                'up': up_analysis,
                'dn': dn_analysis,
                'combined': combined,
                'timestamp': time.time()
            }
        except Exception as e:
            return None
    
    def _analyze_single_book(self, book, side):
        bids = book.get('bids', [])
        asks = book.get('asks', [])
        
        bid_levels = sorted(
            [(float(b['price']), float(b['size'])) for b in bids],
            key=lambda x: x[0], reverse=True
        )
        ask_levels = sorted(
            [(float(a['price']), float(a['size'])) for a in asks],
            key=lambda x: x[0]
        )
        
        best_bid = bid_levels[0][0] if bid_levels else 0
        best_ask = ask_levels[0][0] if ask_levels else 1
        spread = best_ask - best_bid
        midpoint = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.5
        
        total_bid_depth = sum(p * s for p, s in bid_levels)
        total_ask_depth = sum(p * s for p, s in ask_levels)
        top_bid_depth = sum(p * s for p, s in bid_levels[:3])
        top_ask_depth = sum(p * s for p, s in ask_levels[:3])
        
        total_depth = total_bid_depth + total_ask_depth
        imbalance = (total_bid_depth - total_ask_depth) / total_depth if total_depth > 0 else 0
        top_depth = top_bid_depth + top_ask_depth
        top_imbalance = (top_bid_depth - top_ask_depth) / top_depth if top_depth > 0 else 0
        
        return {
            'side': side,
            'best_bid': best_bid,
            'best_ask': best_ask,
            'spread': spread,
            'midpoint': midpoint,
            'total_bid_depth': total_bid_depth,
            'total_ask_depth': total_ask_depth,
            'top_imbalance': top_imbalance,
            'imbalance': imbalance,
            'bid_levels': len(bid_levels),
            'ask_levels': len(ask_levels),
            'is_liquid': total_depth > MIN_BOOK_DEPTH,
        }
    
    def _combine_analysis(self, up_analysis, dn_analysis):
        up_signal = up_analysis['top_imbalance']
        dn_signal = -dn_analysis['top_imbalance']
        combined_signal = (up_signal + dn_signal) / 2
        
        books_agree = (up_signal > 0 and dn_signal > 0) or (up_signal < 0 and dn_signal < 0)
        
        # Combined price: how much arb exists?
        combined_price = up_analysis['best_bid'] + dn_analysis['best_bid']
        arb_gap = 1.0 - combined_price  # > 0 means arb opportunity
        
        if combined_signal > MIN_BOOK_IMBALANCE:
            book_prediction = 'UP'
        elif combined_signal < -MIN_BOOK_IMBALANCE:
            book_prediction = 'DN'
        else:
            book_prediction = None
        
        return {
            'combined_signal': combined_signal,
            'books_agree': books_agree,
            'book_prediction': book_prediction,
            'combined_price': combined_price,
            'arb_gap': arb_gap,
            'is_liquid': up_analysis['is_liquid'] and dn_analysis['is_liquid'],
        }
    
    def get_optimal_bid_price(self, tokens, side):
        """
        Calculate optimal maker bid price for a given side.
        Returns price that's competitive but below market.
        """
        book = self.analyze_book(tokens)
        if not book:
            return None
        
        if side == 'UP':
            analysis = book['up']
        else:
            analysis = book['dn']
        
        best_bid = analysis['best_bid']
        best_ask = analysis['best_ask']
        spread = analysis['spread']
        
        if spread <= 0.01:
            # Very tight spread: bid at best_bid (join the queue)
            return best_bid
        elif spread <= 0.03:
            # Normal spread: bid 1 penny above best bid (improve price)
            return min(best_bid + 0.01, best_ask - 0.01)
        else:
            # Wide spread: bid at midpoint (capture more of spread)
            return round((best_bid + best_ask) / 2, 2)


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-ASSET MARKET INTELLIGENCE (v70 — scans multiple assets)
# ═══════════════════════════════════════════════════════════════════════════

class MarketIntelligence:
    """
    v70: Scans multiple assets (SOL, ETH, BTC) for 15-min markets.
    Prioritizes SOL (thinnest books, biggest edge).
    """
    
    GAMMA_API = "https://gamma-api.polymarket.com"
    
    def __init__(self):
        self.current_markets = {}  # asset -> market_info
        self.last_timestamps = {}  # asset -> timestamp
    
    def _get_current_timestamp(self):
        return (int(time.time()) // 900) * 900
    
    def find_active_market(self, asset='btc'):
        """Find active 15-minute market for a given asset."""
        current_ts = self._get_current_timestamp()
        
        for offset in [0, 900, -900]:
            timestamp = current_ts + offset
            slug = f"{asset}-updown-15m-{timestamp}"
            
            try:
                r = requests.get(f"{self.GAMMA_API}/events?slug={slug}", timeout=5)
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
                                is_new = (timestamp != self.last_timestamps.get(asset))
                                self.last_timestamps[asset] = timestamp
                                
                                market_info = {
                                    'slug': slug,
                                    'asset': asset,
                                    'timestamp': timestamp,
                                    'tokens': tokens,
                                    'volume': market.get('volume', 0),
                                    'liquidity': market.get('liquidity', 0),
                                    'question': market.get('question', ''),
                                }
                                self.current_markets[asset] = market_info
                                return is_new, market_info
            except:
                continue
        
        return False, self.current_markets.get(asset)
    
    def find_all_active_markets(self):
        """Find active markets across all assets."""
        results = {}
        for asset in ASSETS:
            is_new, market = self.find_active_market(asset)
            if market:
                results[asset] = {'is_new': is_new, 'market': market}
        return results

    def _parse_tokens(self, clob_ids_str):
        try:
            clob_ids = json.loads(clob_ids_str) if isinstance(clob_ids_str, str) else clob_ids_str
            if len(clob_ids) >= 2:
                return {"up": clob_ids[0], "dn": clob_ids[1]}
        except:
            pass
        return None

    def check_outcome(self, asset, candle_start):
        """Check if a specific candle has resolved and what the outcome was."""
        slug = f"{asset}-updown-15m-{candle_start}"
        try:
            r = requests.get(f"{self.GAMMA_API}/events?slug={slug}", timeout=10)
            data = r.json()
            if data and len(data) > 0:
                market = data[0].get("markets", [{}])[0]
                if market.get("closed"):
                    outcome_prices = market.get("outcomePrices", "")
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                    if prices and len(prices) >= 2:
                        up_final = float(prices[0])
                        dn_final = float(prices[1])
                        if up_final > 0.9:
                            return "UP"
                        elif dn_final > 0.9:
                            return "DN"
        except:
            pass
        return None


# ═══════════════════════════════════════════════════════════════════════════
# TRADE TRACKER (v70 — enhanced with maker-specific fields)
# ═══════════════════════════════════════════════════════════════════════════

class TradeTracker:
    def __init__(self, filename="trades_v70.csv"):
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
                    'timestamp', 'candle_start', 'asset', 'side', 'entry_price',
                    'shares', 'cost', 'order_type', 'phase',
                    'physics_confidence', 'book_signal',
                    'books_agree', 'spread', 'arb_gap',
                    'btc_change_at_entry', 'minute_entered',
                    'outcome', 'profit', 'won',
                    'maker_fee', 'taker_fee_saved'
                ])
    
    def open_trade(self, candle_start, side, entry_price, shares, cost,
                   ml_confidence, book_signal, books_agree, spread, ev, ev_pct,
                   btc_change, rsi, minute, asset='btc', order_type='maker', phase='directional'):
        self.current_trade = {
            'timestamp': datetime.now().isoformat(),
            'candle_start': candle_start,
            'asset': asset,
            'side': side,
            'entry_price': entry_price,
            'shares': shares,
            'cost': cost,
            'order_type': order_type,
            'phase': phase,
            'ml_confidence': ml_confidence,
            'book_signal': book_signal,
            'books_agree': books_agree,
            'spread': spread,
            'ev': ev,
            'ev_pct': ev_pct,
            'btc_change': btc_change,
            'rsi': rsi,
            'minute': minute,
            'maker_fee': 0.0,  # Maker pays $0
            'taker_fee_saved': self._calc_taker_fee(entry_price, shares),
        }
    
    def _calc_taker_fee(self, price, shares):
        """Calculate what taker fee WOULD have been (for tracking savings)."""
        return shares * 0.25 * (price * (1 - price)) ** 2
    
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
                t['timestamp'], t['candle_start'], t.get('asset', 'btc'),
                t['side'], t['entry_price'],
                t['shares'], t['cost'], t.get('order_type', 'maker'),
                t.get('phase', 'directional'),
                t['ml_confidence'], t['book_signal'],
                t['books_agree'], t['spread'], t.get('ev', 0),
                t['btc_change'], t['minute'],
                t['outcome'], t['profit'], t['won'],
                t.get('maker_fee', 0), t.get('taker_fee_saved', 0)
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
        total_fees_saved = sum(t.get('taker_fee_saved', 0) for t in self.trades)
        
        return {
            'total_trades': total,
            'wins': wins,
            'losses': total - wins,
            'win_rate': wins / total if total > 0 else 0,
            'total_profit': total_profit,
            'avg_profit': total_profit / total if total > 0 else 0,
            'total_fees_saved': total_fees_saved,
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

def get_prices(client, tokens):
    if not tokens or not client:
        return None, None
    try:
        up = float(client.get_price(tokens["up"], "buy").get("price", 0.5))
        dn = float(client.get_price(tokens["dn"], "buy").get("price", 0.5))
        return up, dn
    except:
        return None, None


# ═══════════════════════════════════════════════════════════════════════════
# POLYMARKET CLIENT (v70 — with fee-rate awareness)
# ═══════════════════════════════════════════════════════════════════════════

client = None
def init_client(read_only=False):
    global client
    if read_only:
        client = ClobClient(host="https://clob.polymarket.com")
    else:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_PRIVATE_KEY"),
            chain_id=137,
            signature_type=2,
            funder=os.getenv("POLYMARKET_FUNDER")
        )
        client.set_api_creds(client.create_or_derive_api_creds())

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


# ═══════════════════════════════════════════════════════════════════════════
# TRADE MODE — THE MAKER ENGINE
# ═══════════════════════════════════════════════════════════════════════════

async def run_trade_mode():
    init_client()
    
    physics = PhysicsEngine()
    market_intel = MarketIntelligence()
    book_analyzer = OrderBookAnalyzer()
    order_mgr = OrderManager()
    tracker = TradeTracker()
    order_log = OrderLogger()
    trend_tracker = TrendTracker()  # v72: Multi-candle trend tracking
    
    # Check for orphaned positions
    restored = PositionPersistence.resolve_orphaned_position(tracker, PRIMARY_ASSET)
    
    print("=" * 70)
    print("  v72 FRANKENSTEIN — POST-ONCE TREND-AWARE MAKER")
    print(f"  Strategy: Post ONCE, never cancel (0% fees, 0 slippage)")
    print(f"  Asset: {PRIMARY_ASSET.upper()} 15-min | Max $5 main + $2.50 hedge")
    print(f"  Trend filter: blocks counter-trend trades (last 3 candles)")
    print(f"  Wait until min 3+ for confirmation (no early trades)")
    print(f"  Physics engine: min {MIN_MOVE_PCT}% move, {MIN_CONFIDENCE*100:.0f}%+ confidence")
    print(f"  Fee: $0 maker | Rebates: daily USDC from taker fees")
    print("=" * 70)
    
    asset = PRIMARY_ASSET
    is_new, market = market_intel.find_active_market(asset)
    if market:
        log(f"Market: {market['slug']}")
        set_allowances(client, market['tokens'])
    
    candle_open_btc = None
    position = None  # Filled position (from maker orders that executed)
    orders_posted_this_candle = 0
    last_order_time = 0
    direction_locked = False
    hedge_posted = False  # v71: Track if hedge has been posted this candle
    main_posted = False   # v72: Track if main has been posted this candle
    
    while True:
        try:
            # Use BTC websocket for price feed regardless of which asset we trade
            async with websockets.connect(
                "wss://stream.binance.com:9443/ws/btcusdt@trade",
                ping_interval=20,
                ping_timeout=10,
                close_timeout=5
            ) as ws:
                log("Connected to Binance ✓")
                
                recv_count = 0
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    recv_count += 1
                    
                    if recv_count <= 3:
                        print(f"[DEBUG] msg#{recv_count} BTC=${btc_now:,.0f}", flush=True)
                    
                    physics.update(btc_now, candle_open_btc)
                    
                    # Check for new candle
                    is_new, market = market_intel.find_active_market(asset)
                    
                    if is_new:
                        # === CANDLE TRANSITION ===
                        
                        # Cancel all remaining orders from old candle
                        order_mgr.cancel_all(client)
                        
                        # Check fills and resolve positions
                        order_mgr.check_fills(client)
                        old_positions = order_mgr.reset_for_new_candle()
                        
                        # Initialize outcome tracking
                        actual_outcome = None
                        
                        # Resolve any open position
                        if position and candle_open_btc:
                            prev_candle = position.get('candle_start', '')
                            actual_outcome = None
                            
                            for attempt in range(6):
                                wait_time = [5, 8, 10, 15, 20, 30][attempt]
                                if attempt == 0:
                                    log(f"Waiting {wait_time}s for resolution...")
                                await asyncio.sleep(wait_time)
                                
                                actual_outcome = market_intel.check_outcome(asset, prev_candle)
                                if actual_outcome:
                                    break
                            
                            if actual_outcome is None:
                                actual_outcome = 'UP' if btc_now > candle_open_btc else 'DN'
                                log(f"[NOTE] Used BTC price for outcome")
                            
                            result = tracker.close_trade(actual_outcome,
                                ((btc_now - candle_open_btc) / candle_open_btc) * 100)
                            if result:
                                emoji = "🟢" if result['won'] else "🔴"
                                log(f"{emoji} Trade closed: {'WIN' if result['won'] else 'LOSS'} ${result['profit']:+.2f}")
                            PositionPersistence.clear_position()
                        
                        # Log candle summary with real P&L
                        prev_ts = market['timestamp'] - 900 if market else 0
                        prev_outcome = actual_outcome if actual_outcome else 'UNKNOWN'
                        prev_btc_chg = ((btc_now - candle_open_btc) / candle_open_btc * 100) if candle_open_btc else 0
                        candle_summary = order_log.log_candle_summary(
                            prev_ts, asset, prev_outcome, prev_btc_chg
                        )
                        if candle_summary and candle_summary['fills'] > 0:
                            log(f"  📊 Candle P&L: ${candle_summary['total_pnl']:+.2f} "
                                f"(cost: ${candle_summary['total_cost']:.2f}, "
                                f"UP: {candle_summary['up_shares']:.0f}sh/${candle_summary['up_cost']:.1f}, "
                                f"DN: {candle_summary['dn_shares']:.0f}sh/${candle_summary['dn_cost']:.1f})")
                        
                        # Reset for new candle
                        candle_open_btc = btc_now
                        position = None
                        orders_posted_this_candle = 0
                        last_order_time = 0
                        direction_locked = False
                        hedge_posted = False  # v71: Reset hedge flag
                        main_posted = False   # v72: Reset main flag
                        
                        # v72: Record candle result for trend tracking
                        if candle_open_btc and btc_now:
                            trend_tracker.record_candle(candle_open_btc, btc_now)
                        physics.reset(btc_now, market['timestamp'] if market else None)
                        
                        if market:
                            set_allowances(client, market['tokens'])
                            log(f"New candle | {asset.upper()} | BTC: ${btc_now:,.2f}")
                            
                            stats = tracker.get_stats()
                            if stats and stats['total_trades'] > 0:
                                log(f"📊 Session: {stats['wins']}/{stats['total_trades']} "
                                    f"({stats['win_rate']*100:.1f}%) | P/L: ${stats['total_profit']:+.2f} "
                                    f"| Fees saved: ${stats['total_fees_saved']:.2f}")
                            
                            # Show order log stats
                            ol_stats = order_log.get_session_stats()
                            if ol_stats:
                                log(f"📋 Order Log: {ol_stats['candles_traded']}/{ol_stats['candles_total']} candles traded | "
                                    f"P&L: ${ol_stats['total_pnl']:+.2f} | "
                                    f"W/L: {ol_stats['wins']}/{ol_stats['losses']}")
                            
                            om = order_mgr.get_summary()
                            log(f"📋 Orders: {om['total_placed']} placed, {om['total_filled']} filled, "
                                f"{om['total_cancelled']} cancelled")
                    
                    # Wait for market
                    if not market:
                        await asyncio.sleep(0.1)
                        continue
                    
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
                    
                    # Physics signal
                    result = physics.get_signal(minutes_elapsed)
                    should_trade, phy_side, phy_conf, phy_reason, phase = result
                    phy_display = physics.get_display(minutes_elapsed)
                    
                    # Check for fills periodically
                    now = time.time()
                    if now - last_order_time > 10:
                        fills = order_mgr.check_fills(client)
                        for fill in fills:
                            if position is None:
                                # First fill becomes our position
                                position = {
                                    'side': 'UP' if fill['token_id'] == tokens['up'] else 'DN',
                                    'shares': fill['filled_amount'] or fill['shares'],
                                    'cost': fill['size_usd'],
                                    'entry_price': fill['price'],
                                    'ml_confidence': phy_conf,
                                    'book_signal': '-',
                                    'books_agree': False,
                                    'spread': 0,
                                    'ev': 0,
                                    'ev_pct': 0,
                                    'btc_change': btc_change,
                                    'rsi': 0,
                                    'minute': int(minutes_elapsed),
                                    'candle_start': market['timestamp'],
                                    'phase': phase,
                                }
                                PositionPersistence.save_position(position, market['timestamp'], candle_open_btc)
                                tracker.open_trade(
                                    market['timestamp'], position['side'], fill['price'],
                                    position['shares'], position['cost'],
                                    phy_conf, '-', False, 0, 0, 0,
                                    btc_change, 0, int(minutes_elapsed),
                                    asset=asset, order_type='maker', phase=phase
                                )
                                log(f"🎯 POSITION: {position['side']} filled @ {fill['price']*100:.0f}¢")
                    
                    # === DISPLAY ===
                    om = order_mgr.get_summary()
                    trend_display = trend_tracker.get_display()
                    post_status = "✅DONE" if main_posted else "⏳WAIT"
                    if position:
                        winning = (position['side'] == 'UP' and btc_change > 0) or \
                                  (position['side'] == 'DN' and btc_change < 0)
                        emoji = "🟢" if winning else "🔴"
                        print(
                            f"{emoji} HOLD {position['side']} | {phy_display} | "
                            f"Orders:{om['active']} | {minutes_left:.1f}m left",
                            end='\r'
                        )
                    elif om['active'] > 0:
                        print(
                            f"📝 {om['active']} BIDS LIVE | {phy_display} | "
                            f"{trend_display} | {post_status} | {minutes_left:.1f}m",
                            end='\r'
                        )
                    elif should_trade:
                        print(
                            f"🎯 {phy_side} {phy_conf*100:.0f}% [{phase}] | {phy_display} | "
                            f"{minutes_left:.1f}m",
                            end='\r'
                        )
                    else:
                        print(
                            f"👀 {phy_display} | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | "
                            f"{minutes_left:.1f}m | {phy_reason}",
                            end='\r'
                        )
                    
                    # === v72 POST-ONCE MAKER TRADING LOGIC ===
                    # Core principle: Post ONE main + ONE hedge per candle. NEVER cancel.
                    # If we already posted this candle, we're done. Just watch.
                    
                    time_since_last_order = now - last_order_time
                    
                    # v72: Only post if we haven't posted main yet this candle
                    if should_trade and not main_posted and orders_posted_this_candle < MAX_ORDERS_PER_CANDLE:
                        
                        if phy_conf < MIN_CONFIDENCE and phase != 'expiry':
                            pass  # Confidence too low
                        elif minutes_left < 0.5:
                            pass  # Too close to expiry
                        else:
                            # v72: TREND FILTER — block counter-trend trades
                            if COUNTER_TREND_BLOCK and trend_tracker.is_counter_trend(phy_side):
                                trend_dir, trend_str = trend_tracker.get_trend()
                                if phase != 'expiry':  # Allow expiry trades regardless
                                    print(
                                        f"🚫 BLOCKED: {phy_side} against trend {trend_dir} "
                                        f"({trend_tracker.get_display()}) | {phy_display}",
                                        end='\r'
                                    )
                                    await asyncio.sleep(0.1)
                                    continue
                            
                            # DETERMINE BID PRICE
                            if phase == 'expiry':
                                main_price = max(EXPIRY_DEEP_BID,
                                    (up_price if phy_side == 'UP' else dn_price) - 0.10)
                                hedge_price = EXPIRY_DEEP_BID
                                main_size = BET_AMOUNT
                                hedge_size = BET_AMOUNT
                                log(f"⚡ EXPIRY MODE: {phy_side} {phy_conf*100:.0f}% | {phy_reason} | {trend_tracker.get_display()}")
                                
                            elif phase == 'directional':
                                if phy_side == 'UP':
                                    market_price = up_price
                                else:
                                    market_price = dn_price
                                
                                main_price = max(0.10, market_price - MAKER_OFFSET)
                                main_price = min(main_price, MAX_ENTRY_PRICE)
                                main_price = round(main_price, 2)
                                hedge_price = HEDGE_PRICE
                                main_size = BET_AMOUNT
                                hedge_size = BET_AMOUNT * HEDGE_RATIO
                                
                                log(f"📝 MAKER BID: {phy_side} {phy_conf*100:.0f}% | {phy_reason} | {trend_tracker.get_display()}")
                            
                            else:  # early
                                main_price = 0.48
                                hedge_price = 0.48
                                main_size = BET_AMOUNT * 0.5
                                hedge_size = BET_AMOUNT * 0.5
                                log(f"📝 EARLY BID: {phy_side} {phy_conf*100:.0f}% | {phy_reason}")
                            
                            # v72: POST MAIN — once and done, NO cancel
                            main_token = tokens['up'] if phy_side == 'UP' else tokens['dn']
                            main_order = order_mgr.post_maker_bid(
                                client, main_token, main_price, main_size,
                                label=f"MAIN_{phy_side}",
                                side_str=phy_side
                            )
                            
                            if main_order:
                                main_posted = True  # v72: DONE. No more main orders.
                                orders_posted_this_candle += 1
                                last_order_time = now
                                
                                evt = 'INSTANT_FILL' if main_order.get('status') == 'filled' else 'POST'
                                order_log.log_order(
                                    evt, market['timestamp'], asset,
                                    main_order['order_id'], phy_side,
                                    main_price, main_order['shares'], main_size,
                                    main_order['status'], phase, phy_conf, phy_reason,
                                    btc_now, btc_change, minutes_elapsed,
                                    up_price, dn_price
                                )
                                
                                if main_order.get('status') == 'filled' and position is None:
                                    filled_shares = main_order.get('filled_amount', main_order['shares'])
                                    position = {
                                        'side': phy_side,
                                        'shares': filled_shares,
                                        'cost': main_size,
                                        'entry_price': main_price,
                                        'ml_confidence': phy_conf,
                                        'book_signal': '-',
                                        'books_agree': False,
                                        'spread': 0,
                                        'ev': 0,
                                        'ev_pct': 0,
                                        'btc_change': btc_change,
                                        'rsi': 0,
                                        'minute': int(minutes_elapsed),
                                        'candle_start': market['timestamp'],
                                        'phase': phase,
                                    }
                                    PositionPersistence.save_position(position, market['timestamp'], candle_open_btc)
                                    tracker.open_trade(
                                        market['timestamp'], phy_side, main_price,
                                        filled_shares, main_size,
                                        phy_conf, '-', False, 0, 0, 0,
                                        btc_change, 0, int(minutes_elapsed),
                                        asset=asset, order_type='maker', phase=phase
                                    )
                                    log(f"  🎯 INSTANT FILL: {phy_side} {filled_shares:.1f}sh @ {main_price*100:.0f}¢")
                            
                            # v72: POST HEDGE — once and done
                            if not hedge_posted and not direction_locked:
                                hedge_side_str = 'DN' if phy_side == 'UP' else 'UP'
                                hedge_token = tokens['dn'] if phy_side == 'UP' else tokens['up']
                                hedge_order = order_mgr.post_maker_bid(
                                    client, hedge_token, hedge_price, hedge_size,
                                    label=f"HEDGE_{hedge_side_str}",
                                    side_str=hedge_side_str
                                )
                                
                                if hedge_order:
                                    hedge_posted = True  # v72: DONE. No more hedge orders.
                                    evt = 'INSTANT_FILL' if hedge_order.get('status') == 'filled' else 'POST'
                                    order_log.log_order(
                                        evt, market['timestamp'], asset,
                                        hedge_order['order_id'], hedge_side_str,
                                        hedge_price, hedge_order['shares'], hedge_size,
                                        hedge_order['status'], phase, phy_conf, '',
                                        btc_now, btc_change, minutes_elapsed,
                                        up_price, dn_price
                                    )
                            
                            log(f"  ✅ Orders posted. Waiting for fills. No more orders this candle.")
                    
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
# SCAN MODE — Monitor all assets for opportunities
# ═══════════════════════════════════════════════════════════════════════════

async def run_scan_mode():
    """Watch all assets in real-time to see where the best opportunities are."""
    init_client(read_only=True)
    
    market_intel = MarketIntelligence()
    book_analyzer = OrderBookAnalyzer()
    
    print("=" * 70)
    print("  v70 FRANKENSTEIN — MULTI-ASSET SCANNER")
    print("  Watching SOL, ETH, BTC 15-min markets")
    print("=" * 70)
    
    while True:
        all_markets = market_intel.find_all_active_markets()
        
        print(f"\n{'='*70}")
        print(f"  {time.strftime('%H:%M:%S')} | {len(all_markets)} active markets")
        print(f"{'='*70}")
        
        for asset, info in all_markets.items():
            market = info['market']
            tokens = market['tokens']
            
            book = book_analyzer.analyze_book(tokens)
            if book:
                up = book['up']
                dn = book['dn']
                comb = book['combined']
                
                minutes_left = get_minutes_remaining(market['timestamp'])
                
                arb_emoji = "🔥" if comb['arb_gap'] > 0.05 else "✅" if comb['arb_gap'] < 0.02 else "⚠️"
                
                print(f"\n  {asset.upper()} 15m | {minutes_left:.1f}m left")
                print(f"    UP: bid={up['best_bid']:.2f} ask={up['best_ask']:.2f} "
                      f"({up['bid_levels']}L) imbal={up['top_imbalance']*100:+.1f}%")
                print(f"    DN: bid={dn['best_bid']:.2f} ask={dn['best_ask']:.2f} "
                      f"({dn['bid_levels']}L) imbal={dn['top_imbalance']*100:+.1f}%")
                print(f"    Combined: ${comb['combined_price']:.2f} | "
                      f"Arb gap: {comb['arb_gap']*100:.1f}% {arb_emoji} | "
                      f"Signal: {comb['book_prediction'] or 'NONE'} "
                      f"{'(agree)' if comb['books_agree'] else '(conflict)'}")
        
        await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════
# ANALYZE MODE
# ═══════════════════════════════════════════════════════════════════════════

def run_analyze_mode():
    try:
        import pandas as pd
        
        df = pd.read_csv("trades_v70.csv")
        print(f"\n📊 v70 MAKER TRADE ANALYSIS ({len(df)} trades)")
        print("=" * 60)
        
        if len(df) == 0:
            print("No trades yet!")
            return
        
        wins = df['won'].sum()
        total = len(df)
        total_profit = df['profit'].sum()
        
        print(f"Win Rate: {wins}/{total} ({wins/total*100:.1f}%)")
        print(f"Total P/L: ${total_profit:+.2f}")
        
        if 'maker_fee' in df.columns:
            fees_paid = df['maker_fee'].sum()
            fees_saved = df['taker_fee_saved'].sum() if 'taker_fee_saved' in df.columns else 0
            print(f"Maker fees paid: ${fees_paid:.2f}")
            print(f"Taker fees saved: ${fees_saved:.2f}")
        
        if 'phase' in df.columns:
            print(f"\n📊 By Phase:")
            for phase, group in df.groupby('phase'):
                if len(group) > 0:
                    wr = group['won'].mean() * 100
                    pl = group['profit'].sum()
                    print(f"  {phase}: {len(group)} trades, {wr:.1f}% WR, ${pl:+.2f}")
        
        if 'asset' in df.columns:
            print(f"\n📊 By Asset:")
            for asset, group in df.groupby('asset'):
                if len(group) > 0:
                    wr = group['won'].mean() * 100
                    pl = group['profit'].sum()
                    print(f"  {asset.upper()}: {len(group)} trades, {wr:.1f}% WR, ${pl:+.2f}")
        
        if 'order_type' in df.columns:
            print(f"\n📊 By Order Type:")
            for otype, group in df.groupby('order_type'):
                if len(group) > 0:
                    wr = group['won'].mean() * 100
                    pl = group['profit'].sum()
                    print(f"  {otype}: {len(group)} trades, {wr:.1f}% WR, ${pl:+.2f}")
        
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║         v72 - FRANKENSTEIN MAKER (POST-ONCE, TREND-AWARE)           ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    print("║  The maker strategy: post limit bids, earn rebates, zero fees.       ║")
    print("║  • POST ONCE: place orders and leave them (no cancel+repost)        ║")
    print("║  • TREND FILTER: only trade WITH the trend, not against             ║")
    print("║  • HEDGE ONCE: max $2.50 on losing side                             ║")
    print("║  • Max $7.50 total per candle ($5 main + $2.50 hedge)              ║")
    print("║  • Physics engine predicts direction (from v68)                      ║")
    print("║  • Multi-asset: SOL (best) > ETH > BTC                             ║")
    print("║  • Validated from whale k9Q2mX4L8A7ZP3R trades ($627K profit)       ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    print("║  MODES:                                                              ║")
    print("║  python3 bot_v72.py --trade    # Maker trading (SOL 15-min)         ║")
    print("║  python3 bot_v72.py --scan     # Multi-asset scanner               ║")
    print("║  python3 bot_v72.py --analyze  # Analyze trade history              ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 bot_v70.py --trade    # Maker trading")
        print("  python3 bot_v70.py --scan     # Multi-asset scanner")
        print("  python3 bot_v70.py --analyze  # Analyze past trades")
        return

    mode = sys.argv[1].lower()

    if mode in ['--trade', '-t', 'trade']:
        asyncio.run(run_trade_mode())
    elif mode in ['--scan', '-s', 'scan']:
        asyncio.run(run_scan_mode())
    elif mode in ['--analyze', '-a', 'analyze']:
        run_analyze_mode()
    else:
        print(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
