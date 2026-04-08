import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
import os
import time
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

client = ClobClient(
    host="https://clob.polymarket.com", 
    key=os.getenv("POLYMARKET_PRIVATE_KEY"), 
    chain_id=137, 
    signature_type=2, 
    funder=os.getenv("POLYMARKET_FUNDER")
)
client.set_api_creds(client.create_or_derive_api_creds())

TRADE_AMOUNT = 2
EDGE_THRESHOLD = 0.10
PROFIT_TARGET = 0.04
STOP_LOSS = 0.06

position = None
SLUG = None
tokens = None
candle_open = None
last_candle_time = None


def get_current_market_timestamp():
    """Calculate the current 15-minute market window timestamp"""
    now = int(time.time())
    # Round down to nearest 15-minute interval (900 seconds)
    return (now // 900) * 900


def find_active_market():
    """Find current active BTC 15m market by calculating expected timestamp"""
    global SLUG
    
    current_ts = get_current_market_timestamp()
    
    # Try current window, next window, and previous window
    # (in case we're at the boundary)
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
                # Check if market is active and accepting orders
                markets = event.get("markets", [])
                if markets:
                    market = markets[0]
                    if market.get("acceptingOrders") and not market.get("closed"):
                        if expected_slug != SLUG:
                            print(f"\n{'='*50}")
                            print(f"*** NEW MARKET DETECTED ***")
                            print(f"Slug: {expected_slug}")
                            print(f"Timestamp: {datetime.fromtimestamp(timestamp)}")
                            print(f"{'='*50}\n")
                            SLUG = expected_slug
                            return True  # New market found
                        return False  # Same market, still active
        except Exception as e:
            print(f"Error checking {expected_slug}: {e}")
    
    # Fallback: search by pattern
    print("Using fallback search...")
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100",
            timeout=10
        )
        for e in r.json():
            slug = e.get("slug", "")
            if "btc" in slug.lower() and "updown" in slug.lower() and "15m" in slug:
                markets = e.get("markets", [])
                if markets and markets[0].get("acceptingOrders"):
                    if slug != SLUG:
                        print(f"\n*** NEW MARKET (fallback): {slug} ***")
                        SLUG = slug
                        return True
                    return False
    except Exception as e:
        print(f"Fallback search error: {e}")
    
    return False


def get_tokens():
    """Get token IDs for current market"""
    global tokens
    tokens = None  # Reset tokens for new market
    
    if not SLUG:
        print("No market slug set!")
        return None
    
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/events?slug={SLUG}",
            timeout=5
        )
        data = r.json()
        
        if data:
            markets = data[0].get("markets", [])
            if markets:
                m = markets[0]
                clob_ids = m.get("clobTokenIds", "[]")
                t = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                
                if t and len(t) >= 2:
                    tokens = {"up_t": t[0], "dn_t": t[1]}
                    print(f"Tokens loaded: UP={t[0][:20]}... DN={t[1][:20]}...")
                    return tokens
    except Exception as e:
        print(f"Error getting tokens: {e}")
    
    return None


def set_allowances():
    """Set token allowances for trading"""
    t = get_tokens()
    if not t:
        print("Cannot set allowances - no tokens!")
        return False
    
    try:
        params1 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=t["up_t"])
        client.update_balance_allowance(params1)
        params2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=t["dn_t"])
        client.update_balance_allowance(params2)
        print("✓ Allowances set!")
        return True
    except Exception as e:
        print(f"Allowance error: {e}")
        return False


def get_prices():
    """Get current UP/DOWN prices"""
    if not tokens:
        return None
    
    try:
        up = float(client.get_price(tokens["up_t"], "buy").get("price", 0))
        dn = float(client.get_price(tokens["dn_t"], "buy").get("price", 0))
        return {
            "up": up, 
            "dn": dn, 
            "up_t": tokens["up_t"], 
            "dn_t": tokens["dn_t"]
        }
    except Exception as e:
        print(f"Price error: {e}")
        return None


def get_binance_candle():
    """Get current 15m candle open price and time from Binance"""
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "15m", "limit": 1},
            timeout=5
        )
        data = r.json()[0]
        open_time = data[0]      # Candle open timestamp (ms)
        open_price = float(data[1])
        return open_time, open_price
    except Exception as e:
        print(f"Binance error: {e}")
        return None, None


def buy(token, price, side):
    """Execute a buy order"""
    global position
    
    try:
        print(f">>> TREND BUY {side} at {price*100:.0f}c")
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token, amount=TRADE_AMOUNT, side=BUY)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success") and resp.get("status") == "matched":
            shares = float(resp.get("takingAmount", 0))
            cost = float(resp.get("makingAmount", 0))
            fill_price = cost / shares if shares > 0 else price
            position = {
                "token": token, 
                "price": fill_price, 
                "size": shares, 
                "side": side, 
                "cost": cost
            }
            print(f"    ✓ FILLED! {shares:.1f} shares at {fill_price*100:.0f}c (${cost:.2f})")
            return True
        else:
            print(f"    ✗ Order not filled: {resp.get('status', 'unknown')}")
    except Exception as e:
        print(f"    ✗ Buy failed: {e}")
    
    return False


def sell(reason):
    """Execute a sell order"""
    global position
    
    if not position:
        return
    
    try:
        print(f"<<< SELLING {position['side']} - {reason}")
        
        # Get actual balance
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, 
            token_id=position["token"]
        )
        bal = client.get_balance_allowance(params)
        raw_balance = int(bal.get("balance", 0))
        actual_shares = raw_balance / 1_000_000
        
        if actual_shares <= 0:
            print("    No shares to sell!")
            position = None
            return
        
        # Execute sell
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(
            token_id=position["token"], 
            amount=actual_shares, 
            side=SELL
        )
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success") and resp.get("status") == "matched":
            received = float(resp.get("makingAmount", 0))
            pnl = received - position["cost"]
            emoji = "✓" if pnl >= 0 else "✗"
            print(f"    {emoji} SOLD! ${received:.2f}, P/L: ${pnl:+.2f}")
            position = None
        else:
            print(f"    ✗ Sell not filled: {resp.get('status', 'unknown')}")
            # Try again with reduced size if needed
    except Exception as e:
        print(f"    ✗ Sell error: {e}")


def check_for_new_candle():
    """Check if a new 15m candle has started"""
    global last_candle_time, candle_open
    
    new_time, new_open = get_binance_candle()
    
    if new_time is None:
        return False
    
    if new_time != last_candle_time:
        print(f"\n{'='*50}")
        print(f"*** NEW CANDLE DETECTED ***")
        print(f"Old open: ${candle_open:,.2f}" if candle_open else "First candle")
        print(f"New open: ${new_open:,.2f}")
        print(f"Time: {datetime.fromtimestamp(new_time/1000)}")
        print(f"{'='*50}\n")
        
        last_candle_time = new_time
        candle_open = new_open
        return True
    
    return False


def check_for_new_market():
    """Check if market has changed and handle transition"""
    global position
    
    old_slug = SLUG
    is_new = find_active_market()
    
    if is_new:
        # Sell any position before switching markets
        if position:
            sell("MARKET CHANGE")
        
        # Setup new market
        get_tokens()
        set_allowances()
        
        return True
    
    return False


async def main():
    global position, candle_open, tokens, last_candle_time
    
    print("=" * 60)
    print("  POLYMARKET BTC 15M TREND FOLLOWING BOT (FIXED)")
    print("=" * 60)
    print(f"Strategy: Trade WITH the trend direction")
    print(f"Trade Size: ${TRADE_AMOUNT}")
    print(f"Edge Threshold: {EDGE_THRESHOLD*100:.0f}c")
    print(f"Profit Target: +{PROFIT_TARGET*100:.0f}c")
    print(f"Stop Loss: -{STOP_LOSS*100:.0f}c")
    print("=" * 60)
    
    # Initial setup
    print("\n[INIT] Finding active market...")
    find_active_market()
    
    if not SLUG:
        print("ERROR: Could not find active BTC 15m market!")
        print("Make sure there's an active market on Polymarket")
        return
    
    print(f"[INIT] Setting up tokens and allowances...")
    get_tokens()
    set_allowances()
    
    print(f"[INIT] Getting initial candle data...")
    last_candle_time, candle_open = get_binance_candle()
    
    if candle_open is None:
        print("ERROR: Could not get Binance candle data!")
        return
    
    print(f"\n[READY] Market: {SLUG}")
    print(f"[READY] Candle open: ${candle_open:,.2f}")
    print(f"[READY] Candle time: {datetime.fromtimestamp(last_candle_time/1000)}")
    print("\n" + "=" * 60)
    print("  STARTING TRADING LOOP")
    print("=" * 60 + "\n")
    
    tick_count = 0
    
    while True:
        try:
            async with websockets.connect(
                "wss://stream.binance.com:9443/ws/btcusdt@trade",
                ping_interval=20,
                ping_timeout=10
            ) as ws:
                
                async for msg in ws:
                    tick_count += 1
                    
                    # Process every 10th tick for efficiency
                    if tick_count % 10 != 0:
                        continue
                    
                    d = json.loads(msg)
                    btc_now = float(d['p'])
                    
                    # Check for new candle every 50 ticks (~5 seconds)
                    if tick_count % 50 == 0:
                        candle_changed = check_for_new_candle()
                        
                        if candle_changed:
                            # Also check for new market when candle changes
                            check_for_new_market()
                    
                    # Check for new market every 100 ticks (~10 seconds)
                    if tick_count % 100 == 0:
                        check_for_new_market()
                    
                    # Skip if no tokens
                    if not tokens:
                        print("Waiting for market setup...")
                        continue
                    
                    # Get current prices
                    m = get_prices()
                    if not m or m["up"] == 0:
                        print("Market closed or no prices")
                        continue
                    
                    # Calculate BTC change from candle open
                    if candle_open is None or candle_open == 0:
                        continue
                    
                    btc_change = ((btc_now - candle_open) / candle_open) * 100
                    
                    # Determine trend and edge
                    if btc_change > 0.05:
                        trend = "UP"
                        trend_token = m["up_t"]
                        trend_price = m["up"]
                        fair = min(0.85, 0.50 + btc_change * 2)
                        edge = fair - m["up"]
                    elif btc_change < -0.05:
                        trend = "DN"
                        trend_token = m["dn_t"]
                        trend_price = m["dn"]
                        fair = min(0.85, 0.50 + abs(btc_change) * 2)
                        edge = fair - m["dn"]
                    else:
                        trend = "FLAT"
                        edge = 0
                    
                    # If we have a position, manage it
                    if position:
                        current = m["up"] if position["side"] == "UP" else m["dn"]
                        profit = current - position["price"]
                        pnl = profit * position["size"]
                        
                        print(f"HOLD {position['side']} | Entry:{position['price']*100:.0f}c Now:{current*100:.0f}c | P/L:${pnl:+.2f} | BTC:{btc_change:+.3f}%")
                        
                        # Exit conditions
                        if profit >= PROFIT_TARGET:
                            sell("PROFIT TARGET")
                        elif profit <= -STOP_LOSS:
                            sell("STOP LOSS")
                        elif position["side"] == "UP" and btc_change < -0.05:
                            sell("TREND FLIP TO DOWN")
                        elif position["side"] == "DN" and btc_change > 0.05:
                            sell("TREND FLIP TO UP")
                    
                    # If no position, look for entry
                    else:
                        print(f"BTC:{btc_change:+.3f}% | Trend:{trend} | UP:{m['up']*100:.0f}c DN:{m['dn']*100:.0f}c | Edge:{edge*100:+.0f}c")
                        
                        # Entry conditions
                        if trend == "UP" and edge >= EDGE_THRESHOLD and m["up"] < 0.85:
                            print(f"*** UP trend detected + {edge*100:.0f}c edge! ***")
                            buy(trend_token, trend_price, "UP")
                        
                        elif trend == "DN" and edge >= EDGE_THRESHOLD and m["dn"] < 0.85:
                            print(f"*** DN trend detected + {edge*100:.0f}c edge! ***")
                            buy(trend_token, trend_price, "DN")
        
        except websockets.exceptions.ConnectionClosed:
            print("\nWebSocket disconnected, reconnecting...")
            await asyncio.sleep(3)
        except Exception as e:
            print(f"\nError: {e}")
            print("Reconnecting in 3 seconds...")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
