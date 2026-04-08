import warnings
warnings.filterwarnings("ignore")

import asyncio, json, requests, time, sys
from collections import deque

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  v73 LADDER SCANNER — Watch the books, simulate the whale              ║
# ║                                                                          ║
# ║  Shows full order book depth on both sides every scan.                  ║
# ║  Simulates a ladder of bids and shows which would have filled.          ║
# ║  No trading — just watching and learning.                               ║
# ╚══════════════════════════════════════════════════════════════════════════╝

ASSETS = ['sol', 'eth', 'btc']
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"

# Our simulated ladder: bids at these prices on BOTH sides
LADDER_PRICES = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45]
LADDER_BET = 1.0  # $1 at each level


class MarketIntelligence:
    def __init__(self):
        self.last_timestamps = {}
    
    def _get_current_timestamp(self):
        return (int(time.time()) // 900) * 900
    
    def find_active_market(self, asset):
        current_ts = self._get_current_timestamp()
        for offset in [0, 900, -900]:
            timestamp = current_ts + offset
            slug = f"{asset}-updown-15m-{timestamp}"
            try:
                r = requests.get(f"{GAMMA_API}/events?slug={slug}", timeout=5)
                data = r.json()
                if data and len(data) > 0:
                    market = data[0].get("markets", [{}])[0]
                    if market.get("acceptingOrders") and not market.get("closed"):
                        clob_ids = json.loads(market.get("clobTokenIds", "[]"))
                        if len(clob_ids) >= 2:
                            is_new = (timestamp != self.last_timestamps.get(asset))
                            self.last_timestamps[asset] = timestamp
                            return is_new, {
                                'slug': slug, 'asset': asset,
                                'timestamp': timestamp,
                                'tokens': {'up': clob_ids[0], 'dn': clob_ids[1]},
                            }
            except:
                continue
        return False, None


def fetch_book(token_id):
    try:
        r = requests.get(f"{CLOB_API}/book?token_id={token_id}", timeout=8)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None


def get_minutes_remaining(ts):
    return max(0, ts + 900 - time.time()) / 60


class LadderSimulator:
    """
    Simulates a ladder of bids on both sides.
    Tracks which bids WOULD have been filled based on market activity.
    """
    def __init__(self):
        self.candle_data = {}  # asset -> candle tracking
        self.history = []      # completed candles
    
    def new_candle(self, asset, timestamp):
        self.candle_data[asset] = {
            'timestamp': timestamp,
            'up_lowest_ask_seen': 1.0,   # Lowest ask we've seen (= sellers willing to sell at)
            'dn_lowest_ask_seen': 1.0,
            'up_fills': {},   # price -> shares that would fill
            'dn_fills': {},
            'scans': 0,
        }
    
    def update(self, asset, up_asks, dn_asks):
        """
        Update with current book data.
        Check if any asks have dropped below our ladder bids.
        If best ask < our bid price, our bid WOULD have been filled.
        """
        if asset not in self.candle_data:
            return
        
        cd = self.candle_data[asset]
        cd['scans'] += 1
        
        # Track lowest asks seen
        if up_asks:
            up_best = up_asks[0][0]
            cd['up_lowest_ask_seen'] = min(cd['up_lowest_ask_seen'], up_best)
        
        if dn_asks:
            dn_best = dn_asks[0][0]
            cd['dn_lowest_ask_seen'] = min(cd['dn_lowest_ask_seen'], dn_best)
        
        # Check which ladder bids would fill
        # A bid fills when someone SELLS at or below our bid price
        # This means the ask drops to our bid level
        for price in LADDER_PRICES:
            # UP side: if someone is asking <= our bid, we'd fill
            if up_asks:
                up_best = up_asks[0][0]
                if up_best <= price and price not in cd['up_fills']:
                    shares = LADDER_BET / price
                    cd['up_fills'][price] = {
                        'shares': shares,
                        'cost': LADDER_BET,
                        'fill_price': up_best,  # Actual price we'd get
                        'time': time.time(),
                    }
            
            # DN side
            if dn_asks:
                dn_best = dn_asks[0][0]
                if dn_best <= price and price not in cd['dn_fills']:
                    shares = LADDER_BET / price
                    cd['dn_fills'][price] = {
                        'shares': shares,
                        'cost': LADDER_BET,
                        'fill_price': dn_best,
                        'time': time.time(),
                    }
    
    def get_status(self, asset):
        """Get current ladder fill status for display."""
        if asset not in self.candle_data:
            return None
        
        cd = self.candle_data[asset]
        
        up_total_cost = sum(f['cost'] for f in cd['up_fills'].values())
        up_total_shares = sum(f['shares'] for f in cd['up_fills'].values())
        dn_total_cost = sum(f['cost'] for f in cd['dn_fills'].values())
        dn_total_shares = sum(f['shares'] for f in cd['dn_fills'].values())
        
        # Calculate P&L if both sides have fills
        # Matched shares = min of both sides
        matched = min(up_total_shares, dn_total_shares)
        
        if matched > 0 and up_total_shares > 0 and dn_total_shares > 0:
            # Average cost per share each side
            up_avg = up_total_cost / up_total_shares if up_total_shares > 0 else 0
            dn_avg = dn_total_cost / dn_total_shares if dn_total_shares > 0 else 0
            combined_avg = up_avg + dn_avg
            guaranteed_profit = matched * (1.0 - combined_avg)
            guaranteed_pct = ((1.0 - combined_avg) / combined_avg) * 100 if combined_avg > 0 else 0
        else:
            combined_avg = 0
            guaranteed_profit = 0
            guaranteed_pct = 0
        
        return {
            'up_fills': len(cd['up_fills']),
            'dn_fills': len(cd['dn_fills']),
            'up_cost': up_total_cost,
            'dn_cost': dn_total_cost,
            'up_shares': up_total_shares,
            'dn_shares': dn_total_shares,
            'up_lowest': cd['up_lowest_ask_seen'],
            'dn_lowest': cd['dn_lowest_ask_seen'],
            'matched_shares': matched,
            'combined_avg': combined_avg,
            'guaranteed_profit': guaranteed_profit,
            'guaranteed_pct': guaranteed_pct,
            'both_sides': len(cd['up_fills']) > 0 and len(cd['dn_fills']) > 0,
        }
    
    def close_candle(self, asset):
        """Record completed candle data."""
        if asset in self.candle_data:
            status = self.get_status(asset)
            if status:
                self.history.append(status)
            del self.candle_data[asset]
    
    def get_session_stats(self):
        """How many candles would have been profitable?"""
        if not self.history:
            return None
        both = [h for h in self.history if h['both_sides']]
        return {
            'total_candles': len(self.history),
            'both_filled': len(both),
            'total_profit': sum(h['guaranteed_profit'] for h in both),
        }


async def run_scanner():
    intel = MarketIntelligence()
    sim = LadderSimulator()
    
    print("=" * 70)
    print("  v73 LADDER SCANNER — Simulating the whale")
    print(f"  Ladder: ${LADDER_BET:.0f} bids at {[int(p*100) for p in LADDER_PRICES]}¢")
    print(f"  Total deployed: ${len(LADDER_PRICES) * LADDER_BET * 2:.0f} "
          f"(${len(LADDER_PRICES) * LADDER_BET:.0f} each side)")
    print(f"  Watching: {', '.join(a.upper() for a in ASSETS)}")
    print("=" * 70)
    
    current_ts = {}
    
    while True:
        try:
            for asset in ASSETS:
                is_new, market = intel.find_active_market(asset)
                if not market:
                    continue
                
                ts = market['timestamp']
                mins_left = get_minutes_remaining(ts)
                
                # New candle
                if ts != current_ts.get(asset):
                    if asset in current_ts:
                        # Close old candle
                        sim.close_candle(asset)
                        stats = sim.get_session_stats()
                        if stats:
                            print(f"\n{'='*70}")
                            print(f"  📊 SESSION: {stats['both_filled']}/{stats['total_candles']} candles "
                                  f"with both sides filled | Simulated profit: ${stats['total_profit']:+.2f}")
                            print(f"{'='*70}")
                    
                    current_ts[asset] = ts
                    sim.new_candle(asset, ts)
                    print(f"\n🕐 NEW CANDLE: {asset.upper()} | {market['slug']}")
                
                # Fetch books
                tokens = market['tokens']
                up_book = fetch_book(tokens['up'])
                dn_book = fetch_book(tokens['dn'])
                
                if not up_book or not dn_book:
                    continue
                
                up_asks = sorted(
                    [(float(a['price']), float(a['size'])) for a in up_book.get('asks', [])],
                    key=lambda x: x[0]
                )
                dn_asks = sorted(
                    [(float(a['price']), float(a['size'])) for a in dn_book.get('asks', [])],
                    key=lambda x: x[0]
                )
                up_bids = sorted(
                    [(float(b['price']), float(b['size'])) for b in up_book.get('bids', [])],
                    key=lambda x: x[0], reverse=True
                )
                dn_bids = sorted(
                    [(float(b['price']), float(b['size'])) for b in dn_book.get('bids', [])],
                    key=lambda x: x[0], reverse=True
                )
                
                # Update simulator
                sim.update(asset, up_asks, dn_asks)
                status = sim.get_status(asset)
                
                # Display
                up_best_ask = up_asks[0][0] if up_asks else 0
                dn_best_ask = dn_asks[0][0] if dn_asks else 0
                up_best_bid = up_bids[0][0] if up_bids else 0
                dn_best_bid = dn_bids[0][0] if dn_bids else 0
                combined_ask = up_best_ask + dn_best_ask
                combined_bid = up_best_bid + dn_best_bid
                
                # Build ladder display
                ladder_up = ""
                ladder_dn = ""
                for p in LADDER_PRICES:
                    up_hit = "✅" if status and p in sim.candle_data.get(asset, {}).get('up_fills', {}) else "⬜"
                    dn_hit = "✅" if status and p in sim.candle_data.get(asset, {}).get('dn_fills', {}) else "⬜"
                    ladder_up += up_hit
                    ladder_dn += dn_hit
                
                # Profit display
                if status and status['both_sides']:
                    profit_str = f"💰 +${status['guaranteed_profit']:.2f} ({status['guaranteed_pct']:.0f}%)"
                elif status and (status['up_fills'] > 0 or status['dn_fills'] > 0):
                    profit_str = f"⏳ 1 side only"
                else:
                    profit_str = "🔍 waiting"
                
                print(
                    f"\n  {asset.upper()} | {mins_left:.1f}m | "
                    f"asks: UP={up_best_ask*100:.0f}¢ DN={dn_best_ask*100:.0f}¢ "
                    f"={combined_ask*100:.0f}¢ | "
                    f"bids: UP={up_best_bid*100:.0f}¢ DN={dn_best_bid*100:.0f}¢ "
                    f"={combined_bid*100:.0f}¢"
                )
                
                # Show book depth (top 5 levels)
                up_ask_str = " ".join(f"{p*100:.0f}¢×{s:.0f}" for p, s in up_asks[:5])
                dn_ask_str = " ".join(f"{p*100:.0f}¢×{s:.0f}" for p, s in dn_asks[:5])
                print(f"    UP asks: {up_ask_str}")
                print(f"    DN asks: {dn_ask_str}")
                
                # Show ladder status
                prices_str = " ".join(f"{int(p*100):2d}¢" for p in LADDER_PRICES)
                print(f"    Ladder:  {prices_str}")
                print(f"    UP bids: {ladder_up}  (lowest ask seen: {status['up_lowest']*100:.0f}¢)" if status else "")
                print(f"    DN bids: {ladder_dn}  (lowest ask seen: {status['dn_lowest']*100:.0f}¢)" if status else "")
                
                if status:
                    print(f"    Fills: UP {status['up_fills']}×${status['up_cost']:.2f} "
                          f"| DN {status['dn_fills']}×${status['dn_cost']:.2f} "
                          f"| {profit_str}")
            
            await asyncio.sleep(5)
        
        except KeyboardInterrupt:
            print("\n\nFinal stats:")
            stats = sim.get_session_stats()
            if stats:
                print(f"  Candles: {stats['total_candles']}")
                print(f"  Both sides filled: {stats['both_filled']}")
                print(f"  Simulated profit: ${stats['total_profit']:+.2f}")
            break
        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)


def main():
    print()
    print("╔══════════════════════════════════════════════════════════════════════╗")
    print("║  v73 LADDER SCANNER — Simulate the whale strategy                   ║")
    print("╠══════════════════════════════════════════════════════════════════════╣")
    print("║  Posts simulated $1 bids at 10¢-45¢ on BOTH sides                  ║")
    print("║  Watches which bids would fill as the market moves                  ║")
    print("║  Calculates guaranteed profit when both sides fill                  ║")
    print("║  NO REAL MONEY — just watching and learning                         ║")
    print("╚══════════════════════════════════════════════════════════════════════╝")
    print()
    asyncio.run(run_scanner())


if __name__ == "__main__":
    main()
