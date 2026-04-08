import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
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

# === GABAGOOL STRATEGY SETTINGS ===
# The key insight: Don't predict direction. Buy BOTH sides when they're cheap.
# Goal: Combined average cost of UP + DN < 97c = guaranteed profit

TRADE_AMOUNT = 2              # Amount per buy
CHEAP_THRESHOLD = 0.40        # Buy when price <= 40c (very cheap)
GOOD_THRESHOLD = 0.45         # Also buy when price <= 45c (still good)
MAX_PRICE = 0.50              # Never buy above 50c

# Position tracking for both sides
MAX_BUYS_PER_SIDE = 5         # Max 5 buys per side per candle
TARGET_COMBINED_AVG = 0.97    # Goal: UP avg + DN avg < 97c

# Take profit settings
PROFIT_TARGET = 0.05          # Take profit at +5c on individual positions
STOP_LOSS = 0.08              # Wider stop loss -8c (we're hedged, can wait)

SLUG = None
tokens = None
candle_open = None
last_candle_time = None

# Track positions for BOTH sides
up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
up_buys = 0
dn_buys = 0

session_pnl = 0.0
trade_count = 0
win_count = 0


def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900


def find_active_market():
    global SLUG, tokens
    
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
                            print(f"\n{'='*50}")
                            print(f"*** NEW MARKET: {expected_slug} ***")
                            print(f"{'='*50}\n")
                            SLUG = expected_slug
                            
                            # Get tokens - same format as v29
                            clob_ids_str = market.get("clobTokenIds", "")
                            if clob_ids_str:
                                import json as j
                                try:
                                    clob_ids = j.loads(clob_ids_str) if isinstance(clob_ids_str, str) else clob_ids_str
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
    
    # Get token IDs
    up_t = None
    dn_t = None
    
    if isinstance(tokens, dict):
        up_t = tokens.get("up_t")
        dn_t = tokens.get("dn_t")
    elif isinstance(tokens, list) and len(tokens) >= 2:
        up_t = tokens[0].get("token_id")
        dn_t = tokens[1].get("token_id")
    
    if not up_t or not dn_t:
        return None
    
    try:
        up_price = float(client.get_price(up_t, "buy").get("price", 0))
        dn_price = float(client.get_price(dn_t, "buy").get("price", 0))
        
        return {
            "up": up_price,
            "dn": dn_price,
            "up_t": up_t,
            "dn_t": dn_t
        }
    except Exception as e:
        return None


def buy(token_id, price, side):
    """Execute a buy and track position for that side"""
    global up_position, dn_position, up_buys, dn_buys, trade_count
    
    print(f"\n>>> BUYING {side} at ~{price*100:.0f}c")
    
    try:
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token_id, amount=TRADE_AMOUNT, side=BUY)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success"):
            fills = resp.get("data", {}).get("fills", [])
            if fills:
                total_cost = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills)
                total_size = sum(float(f.get("size", 0)) for f in fills)
                avg_price = total_cost / total_size if total_size > 0 else price
            else:
                avg_price = price
                total_size = TRADE_AMOUNT / price
                total_cost = TRADE_AMOUNT
            
            # Update the appropriate position
            if side == "UP":
                new_shares = up_position["shares"] + total_size
                new_cost = up_position["cost"] + total_cost
                up_position = {
                    "shares": new_shares,
                    "cost": new_cost,
                    "avg_price": new_cost / new_shares if new_shares > 0 else 0,
                    "token_id": token_id
                }
                up_buys += 1
                print(f"    ✓ BOUGHT {total_size:.2f} UP @ {avg_price*100:.0f}c")
                print(f"    📊 UP Position: {up_position['shares']:.2f} shares @ avg {up_position['avg_price']*100:.0f}c (${up_position['cost']:.2f})")
            else:
                new_shares = dn_position["shares"] + total_size
                new_cost = dn_position["cost"] + total_cost
                dn_position = {
                    "shares": new_shares,
                    "cost": new_cost,
                    "avg_price": new_cost / new_shares if new_shares > 0 else 0,
                    "token_id": token_id
                }
                dn_buys += 1
                print(f"    ✓ BOUGHT {total_size:.2f} DN @ {avg_price*100:.0f}c")
                print(f"    📊 DN Position: {dn_position['shares']:.2f} shares @ avg {dn_position['avg_price']*100:.0f}c (${dn_position['cost']:.2f})")
            
            trade_count += 1
            
            # Show combined stats if we have both
            if up_position["shares"] > 0 and dn_position["shares"] > 0:
                combined_avg = up_position["avg_price"] + dn_position["avg_price"]
                print(f"    🎯 Combined avg: {combined_avg*100:.0f}c (target: <{TARGET_COMBINED_AVG*100:.0f}c)")
                if combined_avg < TARGET_COMBINED_AVG:
                    guaranteed_profit = (1 - combined_avg) * min(up_position["shares"], dn_position["shares"])
                    print(f"    💰 GUARANTEED PROFIT LOCKED: ~${guaranteed_profit:.2f}")
            
            return True
        else:
            err = resp.get("error", resp.get("data", "Unknown error"))
            print(f"    ✗ Error: {err}")
            return False
            
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False


def sell(side, reason):
    """Sell a position"""
    global up_position, dn_position, session_pnl, win_count
    
    position = up_position if side == "UP" else dn_position
    
    if position["shares"] <= 0:
        print(f"    No {side} position to sell!")
        return False
    
    print(f"\n<<< SELLING {side} - {reason}")
    
    try:
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=position["token_id"], amount=position["shares"], side=SELL)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success"):
            fills = resp.get("data", {}).get("fills", [])
            if fills:
                total_revenue = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills)
            else:
                total_revenue = position["shares"] * position["avg_price"]
            
            pnl = total_revenue - position["cost"]
            session_pnl += pnl
            
            if pnl >= 0:
                win_count += 1
                print(f"    ✅ SOLD {position['shares']:.2f} {side} @ ${total_revenue:.2f} | P/L: ${pnl:+.2f}")
            else:
                print(f"    ❌ SOLD {position['shares']:.2f} {side} @ ${total_revenue:.2f} | P/L: ${pnl:+.2f}")
            
            print(f"    Session Total: ${session_pnl:+.2f}")
            
            # Clear position
            if side == "UP":
                up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
            else:
                dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
            
            return True
        else:
            err = resp.get("error", "Failed")
            print(f"    {err}")
            return False
            
    except Exception as e:
        print(f"    Error selling: {e}")
        return False


def get_binance_candle():
    try:
        r = requests.get("https://api.binance.com/api/v3/klines", 
                        params={"symbol": "BTCUSDT", "interval": "15m", "limit": 1})
        k = r.json()[0]
        return k[0], float(k[1])
    except:
        return None, None


def check_new_candle():
    """Check if a new candle started and settle/reset positions"""
    global candle_open, last_candle_time, up_position, dn_position, up_buys, dn_buys, session_pnl
    
    is_new = find_active_market()
    if is_new:
        # Market expired - calculate settlement
        if up_position["shares"] > 0 or dn_position["shares"] > 0:
            print(f"\n{'='*50}")
            print(f"*** MARKET EXPIRED - SETTLING ***")
            
            # One side wins $1, other side loses everything
            # We don't know which yet, but we can estimate based on our positions
            total_cost = up_position["cost"] + dn_position["cost"]
            min_shares = min(up_position["shares"], dn_position["shares"])
            
            if min_shares > 0:
                # We had both sides - guaranteed profit on the hedged portion
                hedged_payout = min_shares * 1.0  # Winner pays $1
                hedged_cost = min_shares * (up_position["avg_price"] + dn_position["avg_price"])
                hedged_profit = hedged_payout - hedged_cost
                print(f"    Hedged portion: {min_shares:.2f} shares")
                print(f"    Cost: ${hedged_cost:.2f} → Payout: ${hedged_payout:.2f}")
                print(f"    💰 Hedged profit: ${hedged_profit:+.2f}")
                session_pnl += hedged_profit
            
            # Unhedged portion is a gamble (we don't track outcome here)
            up_excess = up_position["shares"] - min_shares
            dn_excess = dn_position["shares"] - min_shares
            if up_excess > 0:
                print(f"    ⚠️ Unhedged UP: {up_excess:.2f} shares (result unknown)")
            if dn_excess > 0:
                print(f"    ⚠️ Unhedged DN: {dn_excess:.2f} shares (result unknown)")
            
            print(f"{'='*50}\n")
        
        # Reset for new market
        up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
        dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
        up_buys = 0
        dn_buys = 0
        
        get_tokens()
        set_allowances()
        return True
    return False


async def main():
    global candle_open, tokens, last_candle_time, up_buys, dn_buys
    
    print("=" * 60)
    print("  BOT v31 - GABAGOOL STRATEGY")
    print("  Buy BOTH sides when cheap, lock in guaranteed profit")
    print("=" * 60)
    print(f"CHEAP: ≤{CHEAP_THRESHOLD*100:.0f}c | GOOD: ≤{GOOD_THRESHOLD*100:.0f}c | MAX: ≤{MAX_PRICE*100:.0f}c")
    print(f"Target combined avg: <{TARGET_COMBINED_AVG*100:.0f}c")
    print(f"Max {MAX_BUYS_PER_SIDE} buys per side per candle")
    print("=" * 60)
    
    find_active_market()
    if not SLUG:
        print("ERROR: No active market!")
        return
    
    get_tokens()
    set_allowances()
    last_candle_time, candle_open = get_binance_candle()
    
    print(f"Candle open: ${candle_open:,.2f}\n")
    
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
                    
                    if tick_count % 10 != 0:
                        continue
                    
                    d = json.loads(msg)
                    btc_now = float(d['p'])
                    
                    # Check for new candle
                    if tick_count % 50 == 0:
                        new_time, new_open = get_binance_candle()
                        if new_time and new_time != last_candle_time:
                            print(f"\n*** NEW CANDLE: ${new_open:,.2f} ***\n")
                            last_candle_time = new_time
                            candle_open = new_open
                        
                        if check_new_candle():
                            continue
                    
                    # Get market prices
                    m = get_market_prices()
                    if not m:
                        continue
                    
                    # Calculate BTC change
                    if candle_open:
                        btc_change = (btc_now - candle_open) / candle_open * 100
                    else:
                        btc_change = 0
                    
                    up_price = m["up"]
                    dn_price = m["dn"]
                    combined_price = up_price + dn_price
                    
                    # Current position status
                    up_shares = up_position["shares"]
                    dn_shares = dn_position["shares"]
                    up_avg = up_position["avg_price"]
                    dn_avg = dn_position["avg_price"]
                    
                    # Display status
                    status = f"BTC:{btc_change:+.2f}% | UP:{up_price*100:.0f}c DN:{dn_price*100:.0f}c (={combined_price*100:.0f}c)"
                    
                    if up_shares > 0 or dn_shares > 0:
                        # Show position values
                        up_val = up_shares * up_price if up_shares > 0 else 0
                        dn_val = dn_shares * dn_price if dn_shares > 0 else 0
                        up_pnl = up_val - up_position["cost"] if up_shares > 0 else 0
                        dn_pnl = dn_val - dn_position["cost"] if dn_shares > 0 else 0
                        
                        pos_str = ""
                        if up_shares > 0:
                            pos_str += f" | UP:{up_avg*100:.0f}c→{up_price*100:.0f}c (${up_pnl:+.2f})"
                        if dn_shares > 0:
                            pos_str += f" | DN:{dn_avg*100:.0f}c→{dn_price*100:.0f}c (${dn_pnl:+.2f})"
                        
                        print(f"HOLD{pos_str}")
                        
                        # Check for take profit on individual sides
                        if up_shares > 0 and (up_price - up_avg) >= PROFIT_TARGET:
                            sell("UP", f"PROFIT +{(up_price-up_avg)*100:.0f}c")
                        
                        if dn_shares > 0 and (dn_price - dn_avg) >= PROFIT_TARGET:
                            sell("DN", f"PROFIT +{(dn_price-dn_avg)*100:.0f}c")
                        
                        # Check for stop loss (only if NOT hedged)
                        if up_shares > 0 and dn_shares == 0 and (up_price - up_avg) <= -STOP_LOSS:
                            sell("UP", f"STOP -{abs(up_price-up_avg)*100:.0f}c")
                        
                        if dn_shares > 0 and up_shares == 0 and (dn_price - dn_avg) <= -STOP_LOSS:
                            sell("DN", f"STOP -{abs(dn_price-dn_avg)*100:.0f}c")
                    
                    else:
                        print(status)
                    
                    # === GABAGOOL STRATEGY: Buy whatever is cheap ===
                    
                    # Check if UP is cheap enough to buy
                    if up_price <= MAX_PRICE and up_buys < MAX_BUYS_PER_SIDE:
                        should_buy_up = False
                        reason = ""
                        
                        if up_price <= CHEAP_THRESHOLD:
                            should_buy_up = True
                            reason = f"🔥 VERY CHEAP"
                        elif up_price <= GOOD_THRESHOLD:
                            should_buy_up = True
                            reason = f"👍 GOOD PRICE"
                        elif up_price <= MAX_PRICE and dn_position["shares"] > 0:
                            # We have DN, buy UP to hedge if combined would be good
                            potential_combined = up_price + dn_avg
                            if potential_combined < TARGET_COMBINED_AVG:
                                should_buy_up = True
                                reason = f"🎯 HEDGE (combined would be {potential_combined*100:.0f}c)"
                        
                        if should_buy_up:
                            print(f"*** UP @ {up_price*100:.0f}c - {reason} ***")
                            buy(m["up_t"], up_price, "UP")
                    
                    # Check if DN is cheap enough to buy
                    if dn_price <= MAX_PRICE and dn_buys < MAX_BUYS_PER_SIDE:
                        should_buy_dn = False
                        reason = ""
                        
                        if dn_price <= CHEAP_THRESHOLD:
                            should_buy_dn = True
                            reason = f"🔥 VERY CHEAP"
                        elif dn_price <= GOOD_THRESHOLD:
                            should_buy_dn = True
                            reason = f"👍 GOOD PRICE"
                        elif dn_price <= MAX_PRICE and up_position["shares"] > 0:
                            # We have UP, buy DN to hedge if combined would be good
                            potential_combined = up_avg + dn_price
                            if potential_combined < TARGET_COMBINED_AVG:
                                should_buy_dn = True
                                reason = f"🎯 HEDGE (combined would be {potential_combined*100:.0f}c)"
                        
                        if should_buy_dn:
                            print(f"*** DN @ {dn_price*100:.0f}c - {reason} ***")
                            buy(m["dn_t"], dn_price, "DN")
                        
        except websockets.exceptions.ConnectionClosed:
            print("\nWebsocket closed, reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
