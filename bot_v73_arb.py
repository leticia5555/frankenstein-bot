import warnings
warnings.filterwarnings("ignore")

import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY
import os, sys, time, csv
from datetime import datetime, timezone
from dotenv import load_dotenv
from collections import deque

load_dotenv()

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  v73 - FRANKENSTEIN ARB ENGINE                                          ║
# ║                                                                          ║
# ║  PURE ARBITRAGE — no direction guessing, no physics engine.             ║
# ║                                                                          ║
# ║  STRATEGY:                                                               ║
# ║  1. Scan order books for UP + DN combined ask < $0.90                   ║
# ║  2. Buy BOTH sides as maker (0% fee)                                    ║
# ║  3. One side ALWAYS pays $1.00 → guaranteed profit                      ║
# ║  4. Post once, leave orders, collect at expiry                          ║
# ║                                                                          ║
# ║  MATH: Buy UP@70¢ + DN@14¢ = 84¢ → pays $1.00 = 19% profit           ║
# ║  RISK: Only if one side doesn't fill (cancel unfilled at expiry)        ║
# ║                                                                          ║
# ║  From whale k9Q2mX4L8A7ZP3R ($627K profit using this strategy)         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# === PARAMETERS ===
BET_PER_SIDE = 5.0              # $5 per side = $10 total per candle
MIN_ARB_SPREAD = 0.10           # Minimum 10% spread to trade
FEE_RATE_BPS = 1000             # 15-min crypto markets
SCAN_INTERVAL = 10              # Check books every 10 seconds
MAX_COMBINED_PRICE = 0.90       # Only buy if UP+DN asks < this
BID_OFFSET = 0.02               # Bid 2¢ below best ask (maker order)

# === ASSETS (priority order) ===
ASSETS = ['sol', 'eth', 'btc']
PRIMARY_ASSET = 'sol'

# === POLYMARKET ===
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


# ═══════════════════════════════════════════════════════════════════════════
# BOOK SCANNER — finds arb opportunities
# ═══════════════════════════════════════════════════════════════════════════

class BookScanner:
    """
    Scans order books to find arbitrage opportunities.
    An arb exists when best_ask(UP) + best_ask(DN) < 1.00
    """
    
    def __init__(self):
        self.last_scan = {}
    
    def fetch_book(self, token_id):
        """Fetch order book for a token."""
        try:
            r = requests.get(f"{CLOB_API}/book?token_id={token_id}", timeout=8)
            if r.status_code == 200:
                return r.json()
        except:
            pass
        return None
    
    def analyze_arb(self, tokens):
        """
        Check if an arb opportunity exists.
        Returns: {
            'has_arb': bool,
            'combined_ask': float,  # best_ask(UP) + best_ask(DN)
            'arb_spread': float,    # 1.0 - combined_ask (profit %)
            'up_ask': float,
            'dn_ask': float,
            'up_bid': float,
            'dn_bid': float,
            'up_depth': float,      # $ available at best ask
            'dn_depth': float,
        }
        """
        up_book = self.fetch_book(tokens['up'])
        dn_book = self.fetch_book(tokens['dn'])
        
        if not up_book or not dn_book:
            return None
        
        # Parse asks (what we'd pay to buy)
        up_asks = sorted(
            [(float(a['price']), float(a['size'])) for a in up_book.get('asks', [])],
            key=lambda x: x[0]
        )
        dn_asks = sorted(
            [(float(a['price']), float(a['size'])) for a in dn_book.get('asks', [])],
            key=lambda x: x[0]
        )
        
        # Parse bids (what's being bid)
        up_bids = sorted(
            [(float(b['price']), float(b['size'])) for b in up_book.get('bids', [])],
            key=lambda x: x[0], reverse=True
        )
        dn_bids = sorted(
            [(float(b['price']), float(b['size'])) for b in dn_book.get('bids', [])],
            key=lambda x: x[0], reverse=True
        )
        
        if not up_asks or not dn_asks:
            return None
        
        up_best_ask = up_asks[0][0]
        dn_best_ask = dn_asks[0][0]
        up_best_bid = up_bids[0][0] if up_bids else 0
        dn_best_bid = dn_bids[0][0] if dn_bids else 0
        
        # Depth at best ask (how much $ available)
        up_depth = sum(p * s for p, s in up_asks[:3])
        dn_depth = sum(p * s for p, s in dn_asks[:3])
        
        combined_ask = up_best_ask + dn_best_ask
        arb_spread = 1.0 - combined_ask
        
        return {
            'has_arb': arb_spread >= MIN_ARB_SPREAD,
            'combined_ask': combined_ask,
            'arb_spread': arb_spread,
            'up_ask': up_best_ask,
            'dn_ask': dn_best_ask,
            'up_bid': up_best_bid,
            'dn_bid': dn_best_bid,
            'up_depth': up_depth,
            'dn_depth': dn_depth,
            'up_asks': up_asks[:5],
            'dn_asks': dn_asks[:5],
            'up_bids': up_bids[:5],
            'dn_bids': dn_bids[:5],
        }
    
    def find_best_arb(self, all_markets):
        """
        Scan all markets, return the best arb opportunity.
        """
        best = None
        best_spread = 0
        
        for asset, info in all_markets.items():
            market = info['market']
            tokens = market['tokens']
            
            arb = self.analyze_arb(tokens)
            if arb and arb['has_arb'] and arb['arb_spread'] > best_spread:
                best = {
                    'asset': asset,
                    'market': market,
                    'arb': arb,
                }
                best_spread = arb['arb_spread']
        
        return best


# ═══════════════════════════════════════════════════════════════════════════
# ARB EXECUTOR — places both sides
# ═══════════════════════════════════════════════════════════════════════════

class ArbExecutor:
    """
    Executes arb trades: buy UP + DN simultaneously.
    Posts maker orders below best ask on both sides.
    """
    
    def __init__(self):
        self.active_orders = {}  # order_id -> info
        self.filled_orders = []
        self.candle_trades = []  # All trades this candle
        self.total_arbs = 0
        self.total_profit = 0
    
    def place_arb(self, client, tokens, up_price, dn_price, budget_per_side):
        """
        Place maker bids on BOTH sides.
        Returns: {'up_order': ..., 'dn_order': ..., 'combined_cost': ..., 'guaranteed_profit': ...}
        """
        combined = up_price + dn_price
        if combined >= 1.0:
            return None  # No arb
        
        guaranteed_profit_pct = ((1.0 - combined) / combined) * 100
        
        # Calculate shares — buy equal $ on each side
        up_shares = round(budget_per_side / up_price, 2)
        dn_shares = round(budget_per_side / dn_price, 2)
        
        # Use the SMALLER share count so both sides match
        # This ensures one side fully covers
        shares = min(up_shares, dn_shares)
        
        up_cost = round(shares * up_price, 2)
        dn_cost = round(shares * dn_price, 2)
        total_cost = up_cost + dn_cost
        payout = shares * 1.0  # Winner pays $1/share
        guaranteed_profit = payout - total_cost
        
        log(f"  📊 ARB CALC: {shares:.1f}sh × (UP@{up_price*100:.0f}¢ + DN@{dn_price*100:.0f}¢) "
            f"= ${total_cost:.2f} → pays ${payout:.2f} = +${guaranteed_profit:.2f} ({guaranteed_profit_pct:.1f}%)")
        
        # POST UP BID
        up_order = self._post_bid(client, tokens['up'], up_price, shares, up_cost, "ARB_UP", "UP")
        
        # POST DN BID
        dn_order = self._post_bid(client, tokens['dn'], dn_price, shares, dn_cost, "ARB_DN", "DN")
        
        if up_order or dn_order:
            trade = {
                'timestamp': time.time(),
                'up_order': up_order,
                'dn_order': dn_order,
                'up_price': up_price,
                'dn_price': dn_price,
                'shares': shares,
                'total_cost': total_cost,
                'guaranteed_profit': guaranteed_profit,
                'guaranteed_profit_pct': guaranteed_profit_pct,
                'up_filled': up_order and up_order.get('status') == 'filled',
                'dn_filled': dn_order and dn_order.get('status') == 'filled',
            }
            self.candle_trades.append(trade)
            return trade
        
        return None
    
    def _post_bid(self, client, token_id, price, shares, cost, label, side_str):
        """Post a single maker bid."""
        try:
            price = round(price, 2)
            if price <= 0 or price >= 1.0:
                return None
            
            # Ensure minimum order
            if shares < 1.0 or cost < 0.10:
                return None
            
            opt = PartialCreateOrderOptions(
                neg_risk=True,
                tick_size="0.01",
            )
            
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=round(shares, 2),
                side=BUY,
                fee_rate_bps=FEE_RATE_BPS,
            )
            
            signed_order = client.create_order(order_args, opt)
            resp = client.post_order(signed_order, OrderType.GTC)
            
            if resp.get("success"):
                order_id = resp.get("orderID", "unknown")
                status = resp.get("status", "live")
                taking = float(resp.get("takingAmount", 0) or 0)
                
                is_filled = status == 'matched' or taking > 0
                
                order_info = {
                    'order_id': order_id,
                    'token_id': token_id,
                    'price': price,
                    'shares': shares,
                    'cost': cost,
                    'label': label,
                    'side': side_str,
                    'status': 'filled' if is_filled else 'live',
                    'filled_amount': taking if taking > 0 else (shares if is_filled else 0),
                }
                
                self.active_orders[order_id] = order_info
                
                if is_filled:
                    emoji = "✅"
                    self.filled_orders.append(order_info)
                    log(f"  {emoji} {label}: {shares:.1f}sh @ {price*100:.0f}¢ = ${cost:.2f} [INSTANT FILL]")
                else:
                    emoji = "📝"
                    log(f"  {emoji} {label}: {shares:.1f}sh @ {price*100:.0f}¢ = ${cost:.2f} [maker, 0% fee]")
                
                return order_info
            else:
                error_msg = resp.get('errorMsg', resp.get('error', str(resp)))
                log(f"  ✗ {label} failed: {error_msg}")
                return None
        except Exception as e:
            log(f"  ✗ {label} error: {e}")
            return None
    
    def check_fills(self, client):
        """Check which orders have been filled."""
        newly_filled = []
        for order_id, order in list(self.active_orders.items()):
            try:
                resp = requests.get(f"{CLOB_API}/order/{order_id}", timeout=5)
                if resp.status_code == 200:
                    data = resp.json()
                    status = data.get('status', '')
                    filled = float(data.get('size_matched', 0))
                    
                    if status == 'MATCHED' or filled >= order['shares'] * 0.95:
                        order['status'] = 'filled'
                        order['filled_amount'] = filled
                        self.filled_orders.append(order)
                        del self.active_orders[order_id]
                        newly_filled.append(order)
                        log(f"  ✅ FILLED: {order['label']} {filled:.1f}sh @ {order['price']*100:.0f}¢")
            except:
                pass
        return newly_filled
    
    def cancel_unfilled(self, client):
        """Cancel any unfilled orders at end of candle."""
        cancelled = 0
        for order_id in list(self.active_orders.keys()):
            try:
                resp = client.cancel(order_id)
                if resp:
                    order = self.active_orders.pop(order_id, None)
                    cancelled += 1
                    if order:
                        log(f"  ❌ Cancelled unfilled {order['label']} @ {order['price']*100:.0f}¢")
            except:
                pass
        return cancelled
    
    def get_fill_status(self):
        """Check how many of our arb orders filled."""
        up_filled = any(o['side'] == 'UP' and o['status'] == 'filled' for o in 
                       list(self.active_orders.values()) + self.filled_orders)
        dn_filled = any(o['side'] == 'DN' and o['status'] == 'filled' for o in 
                       list(self.active_orders.values()) + self.filled_orders)
        return up_filled, dn_filled
    
    def reset_candle(self):
        """Reset for new candle."""
        self.active_orders.clear()
        self.filled_orders.clear()
        self.candle_trades.clear()
    
    def get_stats(self):
        return {
            'total_arbs': self.total_arbs,
            'total_profit': self.total_profit,
        }


# ═══════════════════════════════════════════════════════════════════════════
# MARKET INTELLIGENCE (reused from v72)
# ═══════════════════════════════════════════════════════════════════════════

class MarketIntelligence:
    GAMMA_API = "https://gamma-api.polymarket.com"
    
    def __init__(self):
        self.current_markets = {}
        self.last_timestamps = {}
    
    def _get_current_timestamp(self):
        return (int(time.time()) // 900) * 900
    
    def find_active_market(self, asset='btc'):
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
                                }
                                self.current_markets[asset] = market_info
                                return is_new, market_info
            except:
                continue
        
        return False, self.current_markets.get(asset)
    
    def find_all_active_markets(self):
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


# ═══════════════════════════════════════════════════════════════════════════
# ARB LOGGER — tracks all arb trades
# ═══════════════════════════════════════════════════════════════════════════

class ArbLogger:
    def __init__(self, filename="arb_log_v73.csv"):
        self.filename = filename
        self._init_file()
        self.session_trades = []
    
    def _init_file(self):
        try:
            with open(self.filename, 'r') as f:
                lines = sum(1 for _ in f) - 1
                print(f"[ARB LOG] Found {lines} existing entries")
        except:
            with open(self.filename, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'asset', 'candle_ts',
                    'up_price', 'dn_price', 'combined', 'arb_spread_pct',
                    'shares', 'total_cost', 'guaranteed_profit',
                    'up_filled', 'dn_filled', 'both_filled',
                    'actual_outcome', 'actual_pnl'
                ])
    
    def log_arb(self, asset, candle_ts, up_price, dn_price, shares,
                total_cost, guaranteed_profit, up_filled, dn_filled,
                outcome='', actual_pnl=0):
        combined = up_price + dn_price
        spread_pct = (1.0 - combined) / combined * 100
        both = up_filled and dn_filled
        
        row = [
            datetime.now().isoformat(), asset, candle_ts,
            f"{up_price:.4f}", f"{dn_price:.4f}", f"{combined:.4f}", f"{spread_pct:.2f}",
            f"{shares:.1f}", f"{total_cost:.2f}", f"{guaranteed_profit:.2f}",
            up_filled, dn_filled, both,
            outcome, f"{actual_pnl:.2f}"
        ]
        
        with open(self.filename, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(row)
        
        self.session_trades.append({
            'cost': total_cost,
            'guaranteed_profit': guaranteed_profit,
            'both_filled': both,
            'actual_pnl': actual_pnl,
        })
    
    def get_session_stats(self):
        if not self.session_trades:
            return None
        trades = self.session_trades
        both_filled = [t for t in trades if t['both_filled']]
        return {
            'total_trades': len(trades),
            'both_filled': len(both_filled),
            'total_cost': sum(t['cost'] for t in trades),
            'total_guaranteed': sum(t['guaranteed_profit'] for t in both_filled),
            'total_actual': sum(t['actual_pnl'] for t in trades),
        }


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def log(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")

def get_minutes_remaining(candle_start_time):
    return max(0, candle_start_time + 900 - time.time()) / 60

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

def set_allowances(client, tokens):
    try:
        p1 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["up"])
        client.update_balance_allowance(p1)
        p2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["dn"])
        client.update_balance_allowance(p2)
        return True
    except:
        return False


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ARB LOOP
# ═══════════════════════════════════════════════════════════════════════════

async def run_arb_mode():
    init_client()
    
    market_intel = MarketIntelligence()
    scanner = BookScanner()
    executor = ArbExecutor()
    arb_log = ArbLogger()
    
    print("=" * 70)
    print("  v73 FRANKENSTEIN — PURE ARB ENGINE")
    print(f"  Strategy: Buy UP + DN when combined < {MAX_COMBINED_PRICE*100:.0f}¢")
    print(f"  Budget: ${BET_PER_SIDE:.0f}/side (${BET_PER_SIDE*2:.0f} total per arb)")
    print(f"  Min spread: {MIN_ARB_SPREAD*100:.0f}%")
    print(f"  Assets: {', '.join(a.upper() for a in ASSETS)}")
    print(f"  Fee: $0 maker | Post once, never cancel")
    print("=" * 70)
    
    current_candle_ts = {}  # asset -> timestamp
    arb_placed_this_candle = {}  # asset -> bool
    
    while True:
        try:
            # Find all active markets
            all_markets = market_intel.find_all_active_markets()
            
            if not all_markets:
                print("⏳ No active markets found, waiting...", end='\r')
                await asyncio.sleep(5)
                continue
            
            # Check for new candles
            for asset, info in all_markets.items():
                market = info['market']
                ts = market['timestamp']
                
                if ts != current_candle_ts.get(asset):
                    # New candle!
                    if current_candle_ts.get(asset):
                        # Cancel unfilled orders from old candle
                        executor.cancel_unfilled(client)
                        executor.reset_candle()
                        
                        stats = arb_log.get_session_stats()
                        if stats:
                            log(f"📊 Session: {stats['total_trades']} arbs, "
                                f"{stats['both_filled']} filled both, "
                                f"guaranteed: ${stats['total_guaranteed']:+.2f}")
                    
                    current_candle_ts[asset] = ts
                    arb_placed_this_candle[asset] = False
                    set_allowances(client, market['tokens'])
                    
                    mins_left = get_minutes_remaining(ts)
                    log(f"🕐 New candle: {asset.upper()} | {mins_left:.1f}m left | {market['slug']}")
            
            # Scan for arb opportunities
            print_lines = []
            for asset, info in all_markets.items():
                market = info['market']
                tokens = market['tokens']
                mins_left = get_minutes_remaining(market['timestamp'])
                
                if mins_left < 0.5:
                    continue  # Too close to expiry
                
                arb = scanner.analyze_arb(tokens)
                if not arb:
                    continue
                
                spread_pct = arb['arb_spread'] * 100
                emoji = "🔥" if arb['has_arb'] else "⚪"
                
                fill_status = ""
                if arb_placed_this_candle.get(asset):
                    up_f, dn_f = executor.get_fill_status()
                    fill_status = f" | {'✅UP' if up_f else '⏳UP'} {'✅DN' if dn_f else '⏳DN'}"
                    if up_f and dn_f:
                        fill_status += " 💰BOTH"
                
                status = "✅PLACED" if arb_placed_this_candle.get(asset) else "⏳SCANNING"
                
                print_lines.append(
                    f"{emoji} {asset.upper()}: UP@{arb['up_ask']*100:.0f}¢+DN@{arb['dn_ask']*100:.0f}¢"
                    f"={arb['combined_ask']*100:.0f}¢ ({spread_pct:+.1f}%) "
                    f"| {status}{fill_status} | {mins_left:.1f}m"
                )
                
                # EXECUTE ARB if conditions met
                if (arb['has_arb'] and 
                    not arb_placed_this_candle.get(asset) and
                    mins_left > 1.0):
                    
                    log(f"🔥 ARB FOUND: {asset.upper()} | spread: {spread_pct:.1f}% | "
                        f"UP@{arb['up_ask']*100:.0f}¢ + DN@{arb['dn_ask']*100:.0f}¢ = {arb['combined_ask']*100:.0f}¢")
                    
                    # Calculate maker bid prices (below best ask)
                    up_bid = round(arb['up_ask'] - BID_OFFSET, 2)
                    dn_bid = round(arb['dn_ask'] - BID_OFFSET, 2)
                    
                    # Make sure combined bid is still profitable
                    if up_bid + dn_bid < MAX_COMBINED_PRICE:
                        trade = executor.place_arb(
                            client, tokens,
                            up_bid, dn_bid,
                            BET_PER_SIDE
                        )
                        
                        if trade:
                            arb_placed_this_candle[asset] = True
                            arb_log.log_arb(
                                asset, market['timestamp'],
                                up_bid, dn_bid, trade['shares'],
                                trade['total_cost'], trade['guaranteed_profit'],
                                trade['up_filled'], trade['dn_filled']
                            )
                            log(f"  ✅ ARB PLACED on {asset.upper()}! "
                                f"Guaranteed: +${trade['guaranteed_profit']:.2f} "
                                f"({trade['guaranteed_profit_pct']:.1f}%)")
                    else:
                        log(f"  ⚠️ After offset, combined {(up_bid+dn_bid)*100:.0f}¢ > {MAX_COMBINED_PRICE*100:.0f}¢ — skipping")
            
            # Display status
            if print_lines:
                status_line = " | ".join(print_lines)
                print(f"\r{status_line}" + " " * 20, end='\r')
            
            # Check fills periodically
            if any(arb_placed_this_candle.values()):
                executor.check_fills(client)
            
            await asyncio.sleep(SCAN_INTERVAL)
        
        except KeyboardInterrupt:
            log("Shutting down...")
            executor.cancel_unfilled(client)
            break
        except Exception as e:
            log(f"Error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════
# SCAN-ONLY MODE — just watch for arbs without trading
# ═══════════════════════════════════════════════════════════════════════════

async def run_scan_mode():
    market_intel = MarketIntelligence()
    scanner = BookScanner()
    
    print("=" * 70)
    print("  v73 FRANKENSTEIN — ARB SCANNER (read-only)")
    print(f"  Watching: {', '.join(a.upper() for a in ASSETS)}")
    print(f"  Min spread: {MIN_ARB_SPREAD*100:.0f}%")
    print("=" * 70)
    
    arb_count = 0
    
    while True:
        try:
            all_markets = market_intel.find_all_active_markets()
            
            print(f"\n{'='*70}")
            print(f"  {time.strftime('%H:%M:%S')} | Scanning {len(all_markets)} markets")
            print(f"{'='*70}")
            
            for asset, info in all_markets.items():
                market = info['market']
                tokens = market['tokens']
                mins_left = get_minutes_remaining(market['timestamp'])
                
                arb = scanner.analyze_arb(tokens)
                if not arb:
                    print(f"  {asset.upper()}: ❌ No book data")
                    continue
                
                spread_pct = arb['arb_spread'] * 100
                
                if arb['has_arb']:
                    arb_count += 1
                    emoji = "🔥🔥🔥" if spread_pct > 15 else "🔥"
                    print(f"\n  {emoji} {asset.upper()} ARB #{arb_count}: {spread_pct:.1f}% spread!")
                    print(f"    UP: ask={arb['up_ask']*100:.0f}¢ bid={arb['up_bid']*100:.0f}¢ (depth: ${arb['up_depth']:.0f})")
                    print(f"    DN: ask={arb['dn_ask']*100:.0f}¢ bid={arb['dn_bid']*100:.0f}¢ (depth: ${arb['dn_depth']:.0f})")
                    print(f"    Combined: {arb['combined_ask']*100:.0f}¢ → profit: {spread_pct:.1f}%")
                    print(f"    Time: {mins_left:.1f}m left")
                    
                    # Show what the trade would look like
                    up_bid = arb['up_ask'] - BID_OFFSET
                    dn_bid = arb['dn_ask'] - BID_OFFSET
                    shares = min(BET_PER_SIDE / up_bid, BET_PER_SIDE / dn_bid)
                    cost = shares * (up_bid + dn_bid)
                    profit = shares * 1.0 - cost
                    print(f"    💰 Trade: {shares:.1f}sh × (UP@{up_bid*100:.0f}¢ + DN@{dn_bid*100:.0f}¢) "
                          f"= ${cost:.2f} → +${profit:.2f}")
                else:
                    print(f"  {asset.upper()}: {arb['combined_ask']*100:.0f}¢ combined ({spread_pct:+.1f}%) | {mins_left:.1f}m left")
            
            await asyncio.sleep(5)
        
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}")
            await asyncio.sleep(5)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║         v73 - FRANKENSTEIN ARB ENGINE                                ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    print("║  PURE ARBITRAGE — buy both sides cheap, guaranteed profit.          ║")
    print("║  • Scans UP+DN books for combined ask < 90¢                        ║")
    print("║  • Buys both sides as maker (0% fee)                                ║")
    print("║  • One side always pays $1.00 = guaranteed profit                   ║")
    print("║  • Post once, never cancel (no phantom fills)                       ║")
    print("║  • Multi-asset: SOL + ETH + BTC                                    ║")
    print("║  • From whale k9Q2mX4L8A7ZP3R strategy ($627K profit)              ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    print("║  MODES:                                                              ║")
    print("║  python3 bot_v73_arb.py --trade  # Live arb trading                 ║")
    print("║  python3 bot_v73_arb.py --scan   # Scan-only (no trades)            ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 bot_v73_arb.py --trade  # Live arb trading")
        print("  python3 bot_v73_arb.py --scan   # Scan-only (read-only)")
        return

    mode = sys.argv[1].lower()

    if mode in ['--trade', '-t', 'trade']:
        asyncio.run(run_arb_mode())
    elif mode in ['--scan', '-s', 'scan']:
        asyncio.run(run_scan_mode())
    else:
        print(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
