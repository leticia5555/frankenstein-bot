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

# === SPIKE SCALPER v51 - FIXED ===
# 
# FIXES:
# 1. Only ONE position at a time (no spam buying)
# 2. Cooldown between trades
# 3. Auto-sell when profit target hit OR time runs out
# 4. Track position by ATTEMPT not just fills

TRADE_SIZE = 5              # $ per scalp
MIN_DISCOUNT = 0.12         # Buy when 12%+ below fair value
TAKE_PROFIT = 0.08          # Sell when price rises 8¢+
MIN_TIME_LEFT = 1.0         # Sell everything with 1 min left
COOLDOWN_SECONDS = 30       # Wait 30 sec between trade attempts

SLUG = None
tokens = None
candle_start_time = None
candle_open = None

# POSITION TRACKING - only ONE position at a time
current_position = None  # {side, shares, avg_price, cost, token_id, buy_time}
last_trade_time = 0      # Cooldown tracking

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
    """Calculate fair price based on BTC % change"""
    up_fair = 0.50 + (btc_change * 15)
    up_fair = max(0.20, min(0.80, up_fair))
    dn_fair = 1 - up_fair
    return up_fair, dn_fair


def buy_shares(token_id, amount, side):
    """Buy shares - returns (shares, avg_price, cost) or (0, 0, 0) on failure"""
    global last_trade_time
    
    try:
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token_id, amount=amount, side=BUY)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success"):
            fills = resp.get("data", {}).get("fills", [])
            if fills and len(fills) > 0:
                total_cost = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills)
                total_size = sum(float(f.get("size", 0)) for f in fills)
                avg_price = total_cost / total_size if total_size > 0 else 0
                
                if total_size > 0:
                    last_trade_time = time.time()
                    print(f"    ✓ BOUGHT: {total_size:.1f} {side} @ {avg_price*100:.0f}¢ (${total_cost:.2f})")
                    return total_size, avg_price, total_cost
        
        # No fills
        err = resp.get("error", resp.get("data", "No fills"))
        print(f"    ✗ No fill: {err}")
        return 0, 0, 0
            
    except Exception as e:
        print(f"    ✗ Buy error: {e}")
        return 0, 0, 0


def sell_shares(token_id, shares, side):
    """Sell shares - returns (received, avg_price) or (0, 0) on failure"""
    global last_trade_time
    
    try:
        # Get current sell price
        sell_price = float(client.get_price(token_id, "sell").get("price", 0))
        if sell_price <= 0:
            print(f"    ✗ No sell price available")
            return 0, 0
        
        sell_amount = shares * sell_price
        
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token_id, amount=sell_amount, side=SELL)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success"):
            fills = resp.get("data", {}).get("fills", [])
            if fills and len(fills) > 0:
                total_received = sum(float(f.get("price", 0)) * float(f.get("size", 0)) for f in fills)
                total_size = sum(float(f.get("size", 0)) for f in fills)
                avg_price = total_received / total_size if total_size > 0 else 0
                
                if total_size > 0:
                    last_trade_time = time.time()
                    print(f"    ✓ SOLD: {total_size:.1f} {side} @ {avg_price*100:.0f}¢ (${total_received:.2f})")
                    return total_received, avg_price
        
        err = resp.get("error", resp.get("data", "No fills"))
        print(f"    ✗ Sell failed: {err}")
        return 0, 0
            
    except Exception as e:
        print(f"    ✗ Sell error: {e}")
        return 0, 0


def reset_for_new_candle():
    global current_position, candle_open, last_trade_time
    current_position = None
    candle_open = None
    last_trade_time = 0


async def main():
    global SLUG, tokens, candle_start_time, candle_open
    global current_position, last_trade_time
    global session_pnl, trades_won, trades_lost
    
    print("=" * 50)
    print("SPIKE SCALPER v51 - FIXED")
    print("One position at a time, auto-sell")
    print(f"Trade: ${TRADE_SIZE} | Discount: {MIN_DISCOUNT*100:.0f}% | TP: {TAKE_PROFIT*100:.0f}¢")
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
                    
                    # Set candle open
                    if candle_open is None:
                        candle_open = btc_now
                        print(f"Candle open: ${candle_open:,.2f}")
                    
                    # Check for new market
                    if tick_count % 50 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
                                # Sell any remaining position
                                if current_position:
                                    print(f"\n*** NEW CANDLE - Closing position ***")
                                    token_id = current_position["token_id"]
                                    received, _ = sell_shares(token_id, current_position["shares"], current_position["side"])
                                    if received > 0:
                                        pnl = received - current_position["cost"]
                                        session_pnl += pnl
                                        if pnl > 0:
                                            trades_won += 1
                                        else:
                                            trades_lost += 1
                                        print(f"    P/L: ${pnl:+.2f}")
                                
                                reset_for_new_candle()
                                print(f"\n*** NEW CANDLE: {SLUG} ***")
                            
                            get_tokens()
                            set_allowances()
                            candle_open = btc_now
                    
                    # Get prices
                    m = get_market_prices()
                    if not m:
                        continue
                    
                    up_price = m["up"]
                    dn_price = m["dn"]
                    
                    # Calculate fair prices
                    btc_change = (btc_now - candle_open) / candle_open * 100 if candle_open else 0
                    up_fair, dn_fair = calculate_fair_price(btc_change)
                    
                    minutes_left = get_minutes_remaining()
                    
                    # Discounts
                    up_discount = up_fair - up_price
                    dn_discount = dn_fair - dn_price
                    
                    # Cooldown check
                    time_since_trade = time.time() - last_trade_time
                    on_cooldown = time_since_trade < COOLDOWN_SECONDS
                    
                    # Status display
                    if current_position:
                        pos = current_position
                        current = up_price if pos["side"] == "UP" else dn_price
                        unrealized_pnl = (current - pos["avg_price"]) * pos["shares"]
                        print(f"📈 {pos['side']} {pos['shares']:.0f}@{pos['avg_price']*100:.0f}¢ | Now:{current*100:.0f}¢ | P/L:${unrealized_pnl:+.2f} | {minutes_left:.1f}m")
                    else:
                        cd_str = f" [CD:{COOLDOWN_SECONDS - time_since_trade:.0f}s]" if on_cooldown else ""
                        disc_str = ""
                        if up_discount >= MIN_DISCOUNT:
                            disc_str = f" | UP +{up_discount*100:.0f}¢ 🎯"
                        elif dn_discount >= MIN_DISCOUNT:
                            disc_str = f" | DN +{dn_discount*100:.0f}¢ 🎯"
                        print(f"👀 UP:{up_price*100:.0f}¢(f:{up_fair*100:.0f}) DN:{dn_price*100:.0f}¢(f:{dn_fair*100:.0f}) | BTC:{btc_change:+.2f}% | {minutes_left:.1f}m{cd_str}{disc_str}")
                    
                    # === TRADING LOGIC ===
                    
                    # 1. CHECK FOR SELL CONDITIONS (if we have position)
                    if current_position:
                        pos = current_position
                        current = up_price if pos["side"] == "UP" else dn_price
                        profit = current - pos["avg_price"]
                        
                        should_sell = False
                        reason = ""
                        
                        # Take profit
                        if profit >= TAKE_PROFIT:
                            should_sell = True
                            reason = "TAKE PROFIT"
                        
                        # Time's up - sell before settlement
                        elif minutes_left < MIN_TIME_LEFT:
                            should_sell = True
                            reason = "TIME EXIT"
                        
                        if should_sell:
                            print(f"\n*** 💰 {reason}: Selling {pos['side']} ***")
                            received, _ = sell_shares(pos["token_id"], pos["shares"], pos["side"])
                            
                            if received > 0:
                                pnl = received - pos["cost"]
                                session_pnl += pnl
                                if pnl > 0:
                                    trades_won += 1
                                else:
                                    trades_lost += 1
                                print(f"    P/L: ${pnl:+.2f} | Session: ${session_pnl:+.2f} ({trades_won}W/{trades_lost}L)")
                                current_position = None
                            else:
                                print(f"    ⚠️ Sell failed, will retry...")
                    
                    # 2. CHECK FOR BUY CONDITIONS (if no position and not on cooldown)
                    elif not on_cooldown and minutes_left > 2:  # Don't enter in last 2 min
                        
                        # Check UP discount
                        if up_discount >= MIN_DISCOUNT and up_price >= 0.20 and up_price <= 0.45:
                            print(f"\n*** 🎯 SCALP: UP @ {up_price*100:.0f}¢ (fair:{up_fair*100:.0f}¢, +{up_discount*100:.0f}¢) ***")
                            shares, avg, cost = buy_shares(tokens["up_t"], TRADE_SIZE, "UP")
                            
                            if shares > 0:
                                current_position = {
                                    "side": "UP",
                                    "shares": shares,
                                    "avg_price": avg,
                                    "cost": cost,
                                    "token_id": tokens["up_t"],
                                    "buy_time": time.time()
                                }
                            else:
                                # Failed to buy - set cooldown anyway to avoid spam
                                last_trade_time = time.time()
                        
                        # Check DN discount
                        elif dn_discount >= MIN_DISCOUNT and dn_price >= 0.20 and dn_price <= 0.45:
                            print(f"\n*** 🎯 SCALP: DN @ {dn_price*100:.0f}¢ (fair:{dn_fair*100:.0f}¢, +{dn_discount*100:.0f}¢) ***")
                            shares, avg, cost = buy_shares(tokens["dn_t"], TRADE_SIZE, "DN")
                            
                            if shares > 0:
                                current_position = {
                                    "side": "DN",
                                    "shares": shares,
                                    "avg_price": avg,
                                    "cost": cost,
                                    "token_id": tokens["dn_t"],
                                    "buy_time": time.time()
                                }
                            else:
                                last_trade_time = time.time()
                    
                    await asyncio.sleep(0.5)
                        
        except websockets.exceptions.ConnectionClosed:
            print("\nReconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
