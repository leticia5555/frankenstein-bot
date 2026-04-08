import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
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

# === GABAGOOL STRATEGY v49 - LIMIT ORDERS ===
# Pure arbitrage with limit orders - no slippage!
# 
# Strategy:
# 1. Place limit buy for UP at target price (e.g. 48¢)
# 2. Place limit buy for DN at target price (e.g. 48¢)
# 3. Wait for fills - market comes to us
# 4. Combined < 97¢ = guaranteed profit
# 5. NO double down - pure arbitrage only

BUDGET_PER_SIDE = 10            # $ per side
TARGET_PRICE = 0.48             # Target buy price for each side
MAX_COMBINED = 0.96             # Max combined for profit
MIN_PRICE = 0.35                # Don't buy below this (too risky)
MAX_PRICE = 0.52                # Don't buy above this

SLUG = None
tokens = None
candle_start_time = None

# Track orders
up_order_id = None
dn_order_id = None

# Track positions
up_position = {"shares": 0, "cost": 0, "avg_price": 0}
dn_position = {"shares": 0, "cost": 0, "avg_price": 0}

session_pnl = 0.0
candles_traded = 0


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
    """Get current best bid/ask prices"""
    if not tokens:
        return None
    
    try:
        # Use get_price for buy (ask) and sell (bid) prices
        up_buy = client.get_price(tokens["up_t"], "buy")
        up_sell = client.get_price(tokens["up_t"], "sell")
        dn_buy = client.get_price(tokens["dn_t"], "buy")
        dn_sell = client.get_price(tokens["dn_t"], "sell")
        
        up_ask = float(up_buy.get("price", 1.0))  # What we pay to buy
        up_bid = float(up_sell.get("price", 0.0))  # What we get to sell
        dn_ask = float(dn_buy.get("price", 1.0))
        dn_bid = float(dn_sell.get("price", 0.0))
        
        return {
            "up_ask": up_ask,
            "dn_ask": dn_ask,
            "up_bid": up_bid,
            "dn_bid": dn_bid,
            "up_t": tokens["up_t"],
            "dn_t": tokens["dn_t"]
        }
    except Exception as e:
        print(f"Price error: {e}")
        return None


def place_limit_order(token_id, price, size, side_name):
    """Place a GTC limit order"""
    try:
        # Calculate shares from dollar amount
        shares = size / price
        
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
            print(f"    ✓ LIMIT ORDER: {side_name} {shares:.2f} shares @ {price*100:.0f}¢ (${size:.2f})")
            return order_id
        else:
            err = resp.get("error", resp.get("data", "Unknown"))
            print(f"    ✗ Order failed: {err}")
            return None
            
    except Exception as e:
        print(f"    ✗ Error placing order: {e}")
        return None


def cancel_order(order_id):
    """Cancel an open order"""
    if not order_id:
        return
    try:
        client.cancel(order_id)
        print(f"    ✓ Cancelled order {order_id[:8]}...")
    except Exception as e:
        print(f"    ✗ Cancel error: {e}")


def get_order_status(order_id):
    """Check if order is filled"""
    if not order_id:
        return None
    try:
        order = client.get_order(order_id)
        return order
    except:
        return None


def check_fills():
    """Check our open orders and update positions"""
    global up_position, dn_position, up_order_id, dn_order_id
    
    # Check UP order
    if up_order_id:
        try:
            order = client.get_order(up_order_id)
            if order:
                size_matched = float(order.get("size_matched", 0))
                if size_matched > 0:
                    price = float(order.get("price", 0))
                    cost = size_matched * price
                    up_position["shares"] += size_matched
                    up_position["cost"] += cost
                    up_position["avg_price"] = up_position["cost"] / up_position["shares"] if up_position["shares"] > 0 else 0
                    print(f"    💚 UP FILLED: {size_matched:.2f} @ {price*100:.0f}¢")
                    
                # Check if fully filled
                original_size = float(order.get("original_size", 0))
                if size_matched >= original_size * 0.99:  # 99% filled = done
                    up_order_id = None
        except Exception as e:
            pass
    
    # Check DN order
    if dn_order_id:
        try:
            order = client.get_order(dn_order_id)
            if order:
                size_matched = float(order.get("size_matched", 0))
                if size_matched > 0:
                    price = float(order.get("price", 0))
                    cost = size_matched * price
                    dn_position["shares"] += size_matched
                    dn_position["cost"] += cost
                    dn_position["avg_price"] = dn_position["cost"] / dn_position["shares"] if dn_position["shares"] > 0 else 0
                    print(f"    💚 DN FILLED: {size_matched:.2f} @ {price*100:.0f}¢")
                    
                original_size = float(order.get("original_size", 0))
                if size_matched >= original_size * 0.99:
                    dn_order_id = None
        except Exception as e:
            pass


def get_open_orders():
    """Get list of our open orders"""
    try:
        orders = client.get_orders()
        return orders if orders else []
    except:
        return []


def cancel_all_orders():
    """Cancel all open orders"""
    global up_order_id, dn_order_id
    
    try:
        client.cancel_all()
        print("    ✓ All orders cancelled")
    except Exception as e:
        print(f"    Cancel all error: {e}")
    
    up_order_id = None
    dn_order_id = None


def has_position():
    return up_position["shares"] > 0 or dn_position["shares"] > 0


def is_hedged():
    return up_position["shares"] > 0 and dn_position["shares"] > 0


def reset_positions():
    global up_position, dn_position, up_order_id, dn_order_id
    up_position = {"shares": 0, "cost": 0, "avg_price": 0}
    dn_position = {"shares": 0, "cost": 0, "avg_price": 0}
    up_order_id = None
    dn_order_id = None


def settle_positions():
    global session_pnl, candles_traded
    
    if not has_position():
        print(f"\n📊 Candle ended - NO POSITION")
        return
    
    up_s = up_position["shares"]
    dn_s = dn_position["shares"]
    total_cost = up_position["cost"] + dn_position["cost"]
    
    # Payout = max shares (one side wins $1 per share)
    payout = max(up_s, dn_s)
    pnl = payout - total_cost
    
    session_pnl += pnl
    candles_traded += 1
    
    combined = 0
    if up_s > 0 and dn_s > 0:
        combined = (up_position["cost"] + dn_position["cost"]) / min(up_s, dn_s)
    
    print(f"\n{'='*50}")
    print(f"📊 CANDLE SETTLED")
    print(f"   UP: {up_s:.1f} shares @ {up_position['avg_price']*100:.0f}¢ (${up_position['cost']:.2f})")
    print(f"   DN: {dn_s:.1f} shares @ {dn_position['avg_price']*100:.0f}¢ (${dn_position['cost']:.2f})")
    print(f"   Combined: {combined*100:.0f}¢")
    print(f"   Expected payout: ${payout:.2f}")
    print(f"   Expected P/L: ${pnl:+.2f}")
    print(f"   Session: ${session_pnl:+.2f} ({candles_traded} trades)")
    print(f"{'='*50}\n")
    
    reset_positions()


async def main():
    global SLUG, tokens, candle_start_time
    global up_order_id, dn_order_id
    global session_pnl
    
    print("=" * 50)
    print("GABAGOOL v49 - LIMIT ORDERS")
    print("Pure arbitrage - no slippage!")
    print("=" * 50)
    
    find_active_market()
    
    if SLUG:
        print(f"Active market: {SLUG}")
    
    if not tokens:
        get_tokens()
    
    if tokens:
        set_allowances()
    
    last_check = 0
    orders_placed = False
    
    while True:
        try:
            now = time.time()
            
            # Check every 2 seconds
            if now - last_check < 2:
                await asyncio.sleep(0.5)
                continue
            
            last_check = now
            
            # Check for new market
            old_slug = SLUG
            if find_active_market():
                if old_slug and old_slug != SLUG:
                    # Cancel old orders, settle positions
                    cancel_all_orders()
                    settle_positions()
                    orders_placed = False
                
                print(f"\n*** NEW MARKET: {SLUG} ***\n")
                get_tokens()
                set_allowances()
            
            if not tokens:
                continue
            
            m = get_market_prices()
            if not m:
                continue
            
            minutes_left = get_minutes_remaining()
            
            # Check for fills
            check_fills()
            
            up_s = up_position["shares"]
            dn_s = dn_position["shares"]
            
            # Calculate current combined
            combined_ask = m["up_ask"] + m["dn_ask"]
            combined_bid = m["up_bid"] + m["dn_bid"]
            
            # Status display
            order_status = ""
            if up_order_id:
                order_status += "UP⏳ "
            if dn_order_id:
                order_status += "DN⏳ "
            
            if is_hedged():
                combined_paid = (up_position["cost"] + dn_position["cost"]) / min(up_s, dn_s)
                expected_profit = min(up_s, dn_s) - up_position["cost"] - dn_position["cost"]
                print(f"🔒 HEDGED | UP:{up_s:.1f}@{up_position['avg_price']*100:.0f}¢ DN:{dn_s:.1f}@{dn_position['avg_price']*100:.0f}¢ | Combined:{combined_paid*100:.0f}¢ | +${expected_profit:.2f} | {minutes_left:.1f}m")
            elif has_position():
                if up_s > 0:
                    print(f"⏳ PARTIAL | UP:{up_s:.1f}@{up_position['avg_price']*100:.0f}¢ | {order_status}| {minutes_left:.1f}m")
                else:
                    print(f"⏳ PARTIAL | DN:{dn_s:.1f}@{dn_position['avg_price']*100:.0f}¢ | {order_status}| {minutes_left:.1f}m")
            else:
                arb_available = "✓ ARB!" if combined_ask <= MAX_COMBINED else ""
                print(f"👀 | Ask: UP:{m['up_ask']*100:.0f}¢ DN:{m['dn_ask']*100:.0f}¢ = {combined_ask*100:.0f}¢ | Bid: {combined_bid*100:.0f}¢ | {order_status}{arb_available} | {minutes_left:.1f}m")
            
            # === TRADING LOGIC ===
            
            # Don't place new orders in last 2 minutes
            if minutes_left < 2:
                if up_order_id or dn_order_id:
                    print("\n*** Last 2 min - cancelling unfilled orders ***")
                    cancel_all_orders()
                continue
            
            # If we already have both orders or both positions, wait
            if is_hedged():
                continue
            
            # Place orders if we haven't yet and there's opportunity
            if not up_order_id and up_position["shares"] == 0:
                # Buy UP if it's cheap enough
                if m["up_ask"] <= MAX_PRICE:
                    target_up = m["up_ask"]  # Buy at current ask
                    
                    # Check if combined would be good
                    if target_up + m["dn_ask"] <= MAX_COMBINED + 0.02:  # Some margin
                        print(f"\n*** Placing UP limit order @ {target_up*100:.0f}¢ ***")
                        up_order_id = place_limit_order(tokens["up_t"], target_up, BUDGET_PER_SIDE, "UP")
            
            if not dn_order_id and dn_position["shares"] == 0:
                # Buy DN if it's cheap enough
                if m["dn_ask"] <= MAX_PRICE:
                    target_dn = m["dn_ask"]  # Buy at current ask
                    
                    # Check if combined would be good (with UP we have or want)
                    up_price = up_position["avg_price"] if up_position["shares"] > 0 else m["up_ask"]
                    if up_price + target_dn <= MAX_COMBINED + 0.02:
                        print(f"\n*** Placing DN limit order @ {target_dn*100:.0f}¢ ***")
                        dn_order_id = place_limit_order(tokens["dn_t"], target_dn, BUDGET_PER_SIDE, "DN")
            
            # No need to update orders - limit orders will fill if price drops to our level
            # The market comes to us!
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
