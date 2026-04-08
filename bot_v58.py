import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
import os
import time
from dotenv import load_dotenv

load_dotenv()

client = ClobClient(
    host="https://clob.polymarket.com", 
    key=os.getenv("POLYMARKET_PRIVATE_KEY"), 
    chain_id=137, 
    signature_type=2, 
    funder=os.getenv("POLYMARKET_FUNDER")
)
client.set_api_creds(client.create_or_derive_api_creds())

# === v58 - AVERAGING + LOTTERY ===
#
# Based on real profitable traders:
#
# 1. Buy MULTIPLE times as momentum grows (averaging)
# 2. Lottery protection on opposite side
# 3. HOLD TO SETTLEMENT
#
# Example:
#   BTC -0.03% → Buy DN @ 65¢ ($5)
#   BTC -0.05% → Buy DN @ 70¢ ($5)
#   BTC -0.08% → Buy DN @ 78¢ ($5)
#   BTC -0.10% → Buy DN @ 82¢ ($5)
#   + Lottery UP @ 10¢ ($2)
#
# Total: $22 invested, averaging into winner

# === PARAMETERS ===
BUY_AMOUNT = 5.0             # $ per buy
MAX_BUYS = 4                 # Max 4 buys per side per candle
LOTTERY_BET = 2.0            # $ on lottery (fixed min order issue)

# Entry thresholds (BTC % change triggers)
ENTRY_LEVELS = [0.03, 0.05, 0.08, 0.12]  # Buy at each level

# Price ranges
MIN_MAIN_PRICE = 0.60        # Only buy main if 60%+
MAX_MAIN_PRICE = 0.90        # Don't buy above 90¢
MAX_LOTTERY_PRICE = 0.35     # Lottery under 35¢

MIN_TIME_LEFT = 3.0          # Need 3+ min left

# State
SLUG = None
tokens = None
candle_start_time = None
candle_open_btc = None

# Position tracking
position = {
    "side": None,            # "UP" or "DN"
    "buys": 0,               # Number of buys made
    "total_shares": 0,       # Total shares
    "total_cost": 0,         # Total $ spent on main
    "levels_hit": [],        # Which levels triggered
    "lottery_bought": False, # Did we buy lottery?
    "lottery_shares": 0
}

# Stats
session_pnl = 0.0
trades = []


def log(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")


def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900


def get_seconds_remaining():
    if candle_start_time:
        end_ts = candle_start_time + 900
        return max(0, end_ts - time.time())
    return 900


def get_minutes_remaining():
    return get_seconds_remaining() / 60


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
                                        tokens = {"up": clob_ids[0], "dn": clob_ids[1]}
                                except:
                                    tokens = None
                            return True
                        return False
        except:
            pass
    return False


def set_allowances():
    if not tokens:
        return False
    try:
        params1 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["up"])
        client.update_balance_allowance(params1)
        params2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["dn"])
        client.update_balance_allowance(params2)
        return True
    except:
        return False


def get_prices():
    if not tokens:
        return None, None
    try:
        up = float(client.get_price(tokens["up"], "buy").get("price", 0.5))
        dn = float(client.get_price(tokens["dn"], "buy").get("price", 0.5))
        return up, dn
    except:
        return None, None


def place_order(token_id, price, amount, label):
    """Place a GTC limit order"""
    try:
        shares = amount / price
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        order_args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed_order = client.create_order(order_args, opt)
        resp = client.post_order(signed_order, OrderType.GTC)
        
        if resp.get("success"):
            log(f"  ✓ {label}: {shares:.1f} @ {price*100:.0f}¢ = ${amount:.2f}")
            return shares
        else:
            log(f"  ✗ {label} failed: {resp.get('error', resp)}")
            return 0
    except Exception as e:
        log(f"  ✗ {label} error: {e}")
        return 0


def reset_position():
    global position
    position = {
        "side": None,
        "buys": 0,
        "total_shares": 0,
        "total_cost": 0,
        "levels_hit": [],
        "lottery_bought": False,
        "lottery_shares": 0
    }


def get_current_level(btc_change_abs):
    """Return which level we're at based on BTC change"""
    for i, level in enumerate(ENTRY_LEVELS):
        if btc_change_abs < level:
            return i
    return len(ENTRY_LEVELS)


async def main():
    global SLUG, tokens, candle_start_time, candle_open_btc
    global position, session_pnl, trades
    
    print("=" * 60)
    print("v58 - AVERAGING + LOTTERY")
    print(f"${BUY_AMOUNT} x {MAX_BUYS} buys | Lottery: ${LOTTERY_BET}")
    print(f"Levels: {[f'{l*100:.0f}%' for l in ENTRY_LEVELS]}")
    print("HOLD TO SETTLEMENT")
    print("=" * 60)
    
    find_active_market()
    if SLUG:
        log(f"Market: {SLUG}")
    
    if tokens:
        set_allowances()
        log("Ready ✓")
    
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
                    
                    # Set candle open
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        log(f"Candle open: ${candle_open_btc:,.2f}")
                    
                    # Check for new market
                    if tick_count % 100 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
                                # Log final position
                                if position["side"]:
                                    log(f"*** CANDLE ENDED ***")
                                    log(f"Final: {position['side']} | {position['total_shares']:.1f} shares | Cost: ${position['total_cost']:.2f}")
                                    trades.append({
                                        "side": position["side"],
                                        "shares": position["total_shares"],
                                        "cost": position["total_cost"],
                                        "buys": position["buys"]
                                    })
                                
                                reset_position()
                                candle_open_btc = btc_now
                                set_allowances()
                                log(f"New candle: {SLUG}")
                    
                    # Rate limit (faster now - every 0.3 sec)
                    now = time.time()
                    if now - last_check < 0.3:
                        continue
                    last_check = now
                    
                    if not tokens:
                        continue
                    
                    # Get prices
                    up_price, dn_price = get_prices()
                    if up_price is None:
                        continue
                    
                    minutes_left = get_minutes_remaining()
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100 if candle_open_btc else 0
                    btc_change_abs = abs(btc_change)
                    
                    # Determine winning side
                    if btc_change > 0:
                        winning_side = "UP"
                        winning_price = up_price
                        winning_token = tokens["up"]
                        losing_side = "DN"
                        losing_price = dn_price
                        losing_token = tokens["dn"]
                    else:
                        winning_side = "DN"
                        winning_price = dn_price
                        winning_token = tokens["dn"]
                        losing_side = "UP"
                        losing_price = up_price
                        losing_token = tokens["up"]
                    
                    # === DISPLAY STATUS ===
                    if position["side"]:
                        avg_price = position["total_cost"] / position["total_shares"] if position["total_shares"] > 0 else 0
                        emoji = "🟢" if winning_side == position["side"] else "🔴"
                        lottery_str = "L✓" if position["lottery_bought"] else "L✗"
                        print(f"{emoji} {position['side']} | {position['buys']}buys | {position['total_shares']:.0f}sh @ {avg_price*100:.0f}¢ | ${position['total_cost']:.0f} | {lottery_str} | BTC:{btc_change:+.2f}% | {minutes_left:.1f}m", end='\r')
                    else:
                        level = get_current_level(btc_change_abs)
                        print(f"👀 L{level}/{len(ENTRY_LEVELS)} | BTC:{btc_change:+.2f}% | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m", end='\r')
                    
                    # === CHECK FOR ENTRY ===
                    if minutes_left < MIN_TIME_LEFT:
                        continue
                    
                    # What level should we be at?
                    current_level = get_current_level(btc_change_abs)
                    
                    # Skip if no momentum
                    if current_level == 0:
                        continue
                    
                    # Check if we should buy
                    should_buy = False
                    
                    # First buy - establish position
                    if position["side"] is None and current_level >= 1:
                        if MIN_MAIN_PRICE <= winning_price <= MAX_MAIN_PRICE:
                            should_buy = True
                            position["side"] = winning_side
                    
                    # Additional buys - same side only, new level
                    elif position["side"] == winning_side:
                        if position["buys"] < MAX_BUYS:
                            if current_level > len(position["levels_hit"]):
                                if MIN_MAIN_PRICE <= winning_price <= MAX_MAIN_PRICE:
                                    should_buy = True
                    
                    # Execute buy
                    if should_buy:
                        log(f"🎯 BUY #{position['buys']+1} | BTC {btc_change:+.2f}% | {winning_side} @ {winning_price*100:.0f}¢")
                        
                        shares = place_order(winning_token, winning_price + 0.01, BUY_AMOUNT, f"{winning_side}")
                        
                        if shares > 0:
                            position["buys"] += 1
                            position["total_shares"] += shares
                            position["total_cost"] += BUY_AMOUNT
                            position["levels_hit"].append(current_level)
                            
                            # Buy lottery on first buy
                            if not position["lottery_bought"] and losing_price <= MAX_LOTTERY_PRICE:
                                log(f"🎲 LOTTERY | {losing_side} @ {losing_price*100:.0f}¢")
                                lottery_shares = place_order(losing_token, losing_price + 0.01, LOTTERY_BET, f"LOTTERY {losing_side}")
                                if lottery_shares > 0:
                                    position["lottery_bought"] = True
                                    position["lottery_shares"] = lottery_shares
                                    position["total_cost"] += LOTTERY_BET
                            
                            # Show position summary
                            potential_win = position["total_shares"] - position["total_cost"]
                            log(f"   Position: {position['total_shares']:.1f} shares | Cost: ${position['total_cost']:.2f} | Potential: +${potential_win:.2f}")
                    
                    await asyncio.sleep(0.1)
                    
        except websockets.exceptions.ConnectionClosed:
            log("Reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            log(f"Error: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
