import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
import os
import time
from dotenv import load_dotenv
from datetime import datetime
from collections import deque

load_dotenv()

client = ClobClient(
    host="https://clob.polymarket.com", 
    key=os.getenv("POLYMARKET_PRIVATE_KEY"), 
    chain_id=137, 
    signature_type=2, 
    funder=os.getenv("POLYMARKET_FUNDER")
)
client.set_api_creds(client.create_or_derive_api_creds())

# === SPIKE SCALPER v50 ===
# Buy when market price is significantly below "fair" price
# Sell when it reverts OR hold to settlement
#
# Fair price = based on BTC % change
# If BTC is flat, fair = 50/50
# If actual price is 15%+ below fair = BUY opportunity

TRADE_SIZE = 5              # $ per scalp
MIN_DISCOUNT = 0.12         # Buy when 12%+ below fair value
TAKE_PROFIT = 0.08          # Sell when price rises 8¢+
STOP_LOSS = -0.15           # Cut loss if down 15¢ (optional, can disable)
MAX_POSITIONS = 5           # Max concurrent positions
MIN_PRICE = 0.20            # Don't buy below 20¢ (too risky)
MAX_PRICE = 0.45            # Don't buy above 45¢ (not enough discount)

SLUG = None
tokens = None
candle_start_time = None
candle_open = None

# Track positions - list of {side, shares, cost, avg_price, token_id}
positions = []

# Price history for spike detection
price_history = {
    "up": deque(maxlen=30),  # Last 30 prices (~30 seconds)
    "dn": deque(maxlen=30)
}

session_pnl = 0.0
trades_won = 0
trades_lost = 0


def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900


def get_minutes_remaining():
    if candle_start_time:
        end_ts = candle_start_time + 900
        remaining = end_ts - time.time()
        return max(0, remaining / 60)
    return 15.0


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
                                        tokens = {"up_t": clob_ids[0], "dn_t": clob_ids[1]}
                                except:
                                    tokens = None
                            
                            return True
                        return False
        except Exception as e:
            print(f"Error checking {expected_slug}: {e}")
    
    return False


def get_tokens():
    global tokens
    
    if not SLUG:
        return None
    
    try:
        r = requests.get(f"https://gamma-api.polymarket.com/events?slug={SLUG}", timeout=5)
        data = r.json()
        
        if data:
            markets = data[0].get("markets", [])
            if markets:
                m = markets[0]
                clob_ids = m.get("clobTokenIds", "[]")
                t = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                
                if t and len(t) >= 2:
                    tokens = {"up_t": t[0], "dn_t": t[1]}
                    print(f"Tokens loaded ✓")
                    return tokens
    except Exception as e:
        print(f"Token error: {e}")
    
    return None


def set_allowances():
    if not tokens:
        get_tokens()
    
    if not tokens:
        return False
    
    try:
        params1 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["up_t"])
        client.update_balance_allowance(params1)
        params2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["dn_t"])
        client.update_balance_allowance(params2)
        print("Allowances set ✓")
        return True
    except Exception as e:
        print(f"Allowance error: {e}")
        return False


def get_market_prices():
    if not tokens:
        return None
    
    try:
        up_price = float(client.get_price(tokens["up_t"], "buy").get("price", 0))
        dn_price = float(client.get_price(tokens["dn_t"], "buy").get("price", 0))
        
        return {
            "up": up_price,
            "dn": dn_price,
            "up_t": tokens["up_t"],
            "dn_t": tokens["dn_t"]
        }
    except Exception as e:
        return None


def calculate_fair_price(btc_change):
    """
    Calculate fair price based on BTC % change
    If BTC is flat (0%), fair is 50/50
    If BTC is +0.10%, UP should be ~55%, DN ~45%
    """
    # Simple linear model: 1% BTC move = ~20% price move
    # Capped between 0.20 and 0.80
    up_fair = 0.50 + (btc_change * 15)  # 15x multiplier
    up_fair = max(0.20, min(0.80, up_fair))
    dn_fair = 1 - up_fair
    
    return up_fair, dn_fair


def buy_limit(token_id, price, amount, side):
    """Place limit buy order"""
    try:
        shares = amount / price
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=BUY
        )
        signed_order = client.create_order(order_args, opt)
        resp = client.post_order(signed_order, OrderType.GTC)
        
        if resp.get("success"):
            order_id = resp.get("orderID") or resp.get("data", {}).get("orderID")
            print(f"    ✓ LIMIT BUY: {shares:.1f} {side} @ {price*100:.0f}¢ (${amount:.2f})")
            return order_id, shares
        else:
            err = resp.get("error", resp.get("data", "Unknown"))
            print(f"    ✗ Buy failed: {err}")
            return None, 0
            
    except Exception as e:
        print(f"    ✗ Buy error: {e}")
        return None, 0


def buy_market(token_id, amount, side):
    """Market buy for immediate fill - with slippage tolerance"""
    try:
        # Add 2% slippage tolerance to ensure fill
        amount_with_slippage = amount * 1.02
        
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token_id, amount=amount_with_slippage, side=BUY)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success"):
            fills = resp.get("data", {}).get("fills", [])
            if fills:
                total_cost = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills)
                total_size = sum(float(f.get("size", 0)) for f in fills)
                avg_price = total_cost / total_size if total_size > 0 else 0
            else:
                avg_price = 0
                total_size = 0
                total_cost = 0
            
            print(f"    ✓ BOUGHT: {total_size:.1f} {side} @ {avg_price*100:.0f}¢ (${total_cost:.2f})")
            return total_size, avg_price, total_cost
        else:
            err = resp.get("error", resp.get("data", "Unknown"))
            # If FOK fails, try with GTC limit order at slightly higher price
            if "fully filled" in str(err).lower():
                print(f"    ⟳ FOK failed, trying limit order...")
                current_price = float(client.get_price(token_id, "buy").get("price", 0))
                # Place limit at current ask + 2¢
                limit_price = min(current_price + 0.02, 0.50)
                shares = amount / limit_price
                
                opt2 = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
                order_args = OrderArgs(
                    token_id=token_id,
                    price=limit_price,
                    size=shares,
                    side=BUY
                )
                signed_order = client.create_order(order_args, opt2)
                resp2 = client.post_order(signed_order, OrderType.GTC)
                
                if resp2.get("success"):
                    print(f"    ✓ LIMIT ORDER: {shares:.1f} {side} @ {limit_price*100:.0f}¢")
                    # Return estimated values - actual fill may differ
                    return shares, limit_price, amount
            
            print(f"    ✗ Buy failed: {err}")
            return 0, 0, 0
            
    except Exception as e:
        print(f"    ✗ Buy error: {e}")
        return 0, 0, 0


def sell_market(token_id, shares, side):
    """Market sell for immediate exit"""
    global session_pnl, trades_won, trades_lost
    
    try:
        # Get current price for sell
        current_price = float(client.get_price(token_id, "sell").get("price", 0))
        sell_value = shares * current_price
        
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token_id, amount=sell_value, side=SELL)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success"):
            fills = resp.get("data", {}).get("fills", [])
            if fills:
                total_received = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills)
                total_size = sum(float(f.get("size", 0)) for f in fills)
                avg_price = total_received / total_size if total_size > 0 else 0
            else:
                total_received = sell_value
                total_size = shares
                avg_price = current_price
            
            print(f"    ✓ SOLD: {total_size:.1f} {side} @ {avg_price*100:.0f}¢ (${total_received:.2f})")
            return total_received, avg_price
        else:
            err = resp.get("error", resp.get("data", "Unknown"))
            print(f"    ✗ Sell failed: {err}")
            return 0, 0
            
    except Exception as e:
        print(f"    ✗ Sell error: {e}")
        return 0, 0


def check_spike(side, current_price):
    """Check if there was a sudden price drop (spike down)"""
    history = price_history[side]
    
    if len(history) < 10:
        return False, 0
    
    # Get price from 10 ticks ago (~10 seconds)
    old_price = history[-10]
    drop = old_price - current_price
    
    # Spike = dropped more than 10¢ in 10 seconds
    if drop >= 0.10:
        return True, drop
    
    return False, drop


def reset_for_new_candle():
    global positions, price_history, candle_open
    
    # Settle any remaining positions at candle end
    # (In reality they'd settle based on outcome, but we track our own P/L)
    
    positions = []
    price_history = {"up": deque(maxlen=30), "dn": deque(maxlen=30)}
    candle_open = None


async def main():
    global SLUG, tokens, candle_start_time, candle_open
    global positions, session_pnl, trades_won, trades_lost
    
    print("=" * 50)
    print("SPIKE SCALPER v50")
    print("Buy fear, sell greed!")
    print(f"Trade size: ${TRADE_SIZE} | Min discount: {MIN_DISCOUNT*100:.0f}%")
    print("=" * 50)
    
    find_active_market()
    
    if SLUG:
        print(f"Active market: {SLUG}")
    
    if not tokens:
        get_tokens()
    
    if tokens:
        set_allowances()
    
    while True:
        try:
            async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                print("\nConnected to Binance ✓\n")
                
                tick_count = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    
                    btc_now = float(data["p"])
                    tick_count += 1
                    
                    # Set candle open on first tick
                    if candle_open is None:
                        candle_open = btc_now
                        print(f"Candle open: ${candle_open:,.2f}")
                    
                    # Check for new market every 50 ticks
                    if tick_count % 50 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
                                print(f"\n*** NEW CANDLE ***")
                                reset_for_new_candle()
                            
                            print(f"Market: {SLUG}")
                            get_tokens()
                            set_allowances()
                            candle_open = btc_now
                    
                    # Get market prices
                    m = get_market_prices()
                    if not m:
                        continue
                    
                    up_price = m["up"]
                    dn_price = m["dn"]
                    
                    # Update price history
                    price_history["up"].append(up_price)
                    price_history["dn"].append(dn_price)
                    
                    # Calculate BTC change and fair prices
                    btc_change = (btc_now - candle_open) / candle_open * 100 if candle_open else 0
                    up_fair, dn_fair = calculate_fair_price(btc_change)
                    
                    minutes_left = get_minutes_remaining()
                    
                    # Calculate discounts from fair value
                    up_discount = up_fair - up_price
                    dn_discount = dn_fair - dn_price
                    
                    # Check for spikes
                    up_spike, up_drop = check_spike("up", up_price)
                    dn_spike, dn_drop = check_spike("dn", dn_price)
                    
                    # Status display
                    pos_str = f"[{len(positions)} pos]" if positions else ""
                    spike_str = ""
                    if up_spike:
                        spike_str = f" ⚡UP dropped {up_drop*100:.0f}¢!"
                    if dn_spike:
                        spike_str = f" ⚡DN dropped {dn_drop*100:.0f}¢!"
                    
                    discount_str = ""
                    if up_discount > 0.05:
                        discount_str += f" UP:{up_discount*100:+.0f}¢"
                    if dn_discount > 0.05:
                        discount_str += f" DN:{dn_discount*100:+.0f}¢"
                    
                    print(f"👀 UP:{up_price*100:.0f}¢(fair:{up_fair*100:.0f}¢) DN:{dn_price*100:.0f}¢(fair:{dn_fair*100:.0f}¢) | BTC:{btc_change:+.2f}% | {minutes_left:.1f}m {pos_str}{spike_str}{discount_str}")
                    
                    # === SCALPING LOGIC ===
                    
                    # Don't open NEW trades in last 30 seconds
                    if minutes_left < 0.5:
                        continue
                    
                    # Check existing positions for take profit
                    for i, pos in enumerate(positions[:]):  # Copy list to allow removal
                        current = up_price if pos["side"] == "UP" else dn_price
                        profit = current - pos["avg_price"]
                        
                        # Take profit
                        if profit >= TAKE_PROFIT:
                            print(f"\n*** 💰 TAKE PROFIT on {pos['side']} ***")
                            token_id = tokens["up_t"] if pos["side"] == "UP" else tokens["dn_t"]
                            received, _ = sell_market(token_id, pos["shares"], pos["side"])
                            
                            if received > 0:
                                pnl = received - pos["cost"]
                                session_pnl += pnl
                                trades_won += 1
                                print(f"    P/L: ${pnl:+.2f} | Session: ${session_pnl:+.2f} ({trades_won}W/{trades_lost}L)")
                                positions.remove(pos)
                    
                    # Look for new scalp opportunities
                    if len(positions) < MAX_POSITIONS:
                        
                        # Check UP discount
                        if (up_discount >= MIN_DISCOUNT and 
                            up_price >= MIN_PRICE and 
                            up_price <= MAX_PRICE and
                            not any(p["side"] == "UP" for p in positions)):  # Don't double up
                            
                            print(f"\n*** 🎯 SCALP BUY: UP @ {up_price*100:.0f}¢ (fair:{up_fair*100:.0f}¢, discount:{up_discount*100:.0f}¢) ***")
                            shares, avg, cost = buy_market(tokens["up_t"], TRADE_SIZE, "UP")
                            
                            if shares > 0:
                                positions.append({
                                    "side": "UP",
                                    "shares": shares,
                                    "cost": cost,
                                    "avg_price": avg,
                                    "token_id": tokens["up_t"]
                                })
                        
                        # Check DN discount
                        elif (dn_discount >= MIN_DISCOUNT and 
                              dn_price >= MIN_PRICE and 
                              dn_price <= MAX_PRICE and
                              not any(p["side"] == "DN" for p in positions)):
                            
                            print(f"\n*** 🎯 SCALP BUY: DN @ {dn_price*100:.0f}¢ (fair:{dn_fair*100:.0f}¢, discount:{dn_discount*100:.0f}¢) ***")
                            shares, avg, cost = buy_market(tokens["dn_t"], TRADE_SIZE, "DN")
                            
                            if shares > 0:
                                positions.append({
                                    "side": "DN",
                                    "shares": shares,
                                    "cost": cost,
                                    "avg_price": avg,
                                    "token_id": tokens["dn_t"]
                                })
                    
                    await asyncio.sleep(0.5)
                        
        except websockets.exceptions.ConnectionClosed:
            print("\nReconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
