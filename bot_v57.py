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

# === v57 - BTC MOMENTUM + LOTTERY ===
#
# Based on real profitable trader analysis:
#
# 1. Follow BTC momentum on Binance
# 2. Buy the WINNING side at 70-85¢ (BIG bet)
# 3. Buy LOTTERY on losing side at <10¢ (tiny bet)
# 4. HOLD TO SETTLEMENT - no selling, no stop loss
#
# Math:
# - Main bet wins: +27% profit
# - Main bet loses but lottery wins: -5% loss
# - Both lose: rare (momentum was wrong)

# === PARAMETERS ===
MAIN_BET = 20.0              # $ on winning side
LOTTERY_BET = 1.0            # $ on losing side (insurance)
MIN_MOMENTUM = 0.03          # BTC must move 0.03%+ to trade
MIN_MAIN_PRICE = 0.65        # Only buy main if price 65-88¢
MAX_MAIN_PRICE = 0.88        # Don't buy above 88¢ (not enough profit)
MAX_LOTTERY_PRICE = 0.12     # Lottery must be under 12¢
MIN_TIME_LEFT = 5.0          # Need 5+ min left
COOLDOWN = 30                # Seconds between trades

# State
SLUG = None
tokens = None
candle_start_time = None
candle_open_btc = None
last_trade_time = 0
position = None  # {"main_side", "main_shares", "lottery_shares", "total_cost"}

# Stats
session_pnl = 0.0
wins = 0
losses = 0


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


def place_order(token_id, price, amount, side_name):
    """Place a GTC limit order"""
    try:
        shares = amount / price
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        order_args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed_order = client.create_order(order_args, opt)
        resp = client.post_order(signed_order, OrderType.GTC)
        
        if resp.get("success"):
            log(f"  ✓ {side_name}: {shares:.1f} shares @ {price*100:.0f}¢ = ${amount:.2f}")
            return shares
        else:
            log(f"  ✗ {side_name} failed: {resp}")
            return 0
    except Exception as e:
        log(f"  ✗ {side_name} error: {e}")
        return 0


def reset_state():
    global position, candle_open_btc
    position = None
    candle_open_btc = None


async def main():
    global SLUG, tokens, candle_start_time, candle_open_btc
    global position, last_trade_time
    global session_pnl, wins, losses
    
    print("=" * 60)
    print("v57 - BTC MOMENTUM + LOTTERY")
    print(f"Main: ${MAIN_BET} @ 65-88¢ | Lottery: ${LOTTERY_BET} @ <12¢")
    print("HOLD TO SETTLEMENT - No selling!")
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
                                # Candle ended - calculate P/L
                                if position:
                                    log("*** CANDLE ENDED ***")
                                    # We'll see result in Polymarket history
                                    position = None
                                
                                reset_state()
                                set_allowances()
                                candle_open_btc = btc_now
                                log(f"New candle: {SLUG}")
                    
                    # Rate limit
                    now = time.time()
                    if now - last_check < 0.5:
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
                    
                    # === HAVE POSITION - JUST MONITOR ===
                    if position:
                        main_price = up_price if position["main_side"] == "UP" else dn_price
                        emoji = "🟢" if main_price > 0.5 else "🔴"
                        print(f"{emoji} HOLDING {position['main_side']} | Main:{main_price*100:.0f}¢ | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m | Cost:${position['total_cost']:.2f}", end='\r')
                        continue
                    
                    # === NO POSITION - LOOK FOR ENTRY ===
                    
                    # Cooldown check
                    if last_trade_time > 0 and (now - last_trade_time) < COOLDOWN:
                        cd = int(COOLDOWN - (now - last_trade_time))
                        print(f"⏳ Cooldown {cd}s | BTC:{btc_change:+.3f}% | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢", end='\r')
                        continue
                    
                    # Time check
                    if minutes_left < MIN_TIME_LEFT:
                        print(f"⏰ Too late ({minutes_left:.1f}m) | BTC:{btc_change:+.3f}% | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢", end='\r')
                        continue
                    
                    # Momentum check
                    if abs(btc_change) < MIN_MOMENTUM:
                        print(f"👀 No momentum | BTC:{btc_change:+.3f}% | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m", end='\r')
                        continue
                    
                    # Determine direction
                    if btc_change > 0:
                        main_side = "UP"
                        main_price = up_price
                        main_token = tokens["up"]
                        lottery_side = "DN"
                        lottery_price = dn_price
                        lottery_token = tokens["dn"]
                    else:
                        main_side = "DN"
                        main_price = dn_price
                        main_token = tokens["dn"]
                        lottery_side = "UP"
                        lottery_price = up_price
                        lottery_token = tokens["up"]
                    
                    # Check prices are in range
                    if not (MIN_MAIN_PRICE <= main_price <= MAX_MAIN_PRICE):
                        print(f"👀 Main price {main_price*100:.0f}¢ not in range | BTC:{btc_change:+.3f}% {main_side} | {minutes_left:.1f}m", end='\r')
                        continue
                    
                    if lottery_price > MAX_LOTTERY_PRICE:
                        print(f"👀 Lottery {lottery_price*100:.0f}¢ too expensive | BTC:{btc_change:+.3f}% {main_side} | {minutes_left:.1f}m", end='\r')
                        continue
                    
                    # === EXECUTE TRADE ===
                    log(f"🎯 BTC {btc_change:+.3f}% → {main_side} is WINNING")
                    log(f"   Main: {main_side} @ {main_price*100:.0f}¢")
                    log(f"   Lottery: {lottery_side} @ {lottery_price*100:.0f}¢")
                    
                    # Place main bet
                    main_shares = place_order(main_token, main_price + 0.01, MAIN_BET, f"MAIN {main_side}")
                    
                    # Place lottery bet
                    lottery_shares = place_order(lottery_token, lottery_price + 0.01, LOTTERY_BET, f"LOTTERY {lottery_side}")
                    
                    if main_shares > 0:
                        total_cost = MAIN_BET + (LOTTERY_BET if lottery_shares > 0 else 0)
                        position = {
                            "main_side": main_side,
                            "main_shares": main_shares,
                            "lottery_side": lottery_side,
                            "lottery_shares": lottery_shares,
                            "total_cost": total_cost,
                            "entry_time": now
                        }
                        
                        potential_win = main_shares - total_cost
                        potential_loss = lottery_shares - total_cost if lottery_shares > 0 else -total_cost
                        
                        log(f"   ✅ Position open!")
                        log(f"   If {main_side} wins: +${potential_win:.2f}")
                        log(f"   If {lottery_side} wins: ${potential_loss:.2f}")
                        log(f"   HOLDING TO SETTLEMENT...")
                        
                        last_trade_time = now
                    else:
                        log("   ❌ Main order failed - no position")
                    
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
