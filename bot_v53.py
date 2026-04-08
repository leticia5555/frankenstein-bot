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

# === SMART SCALPER v53 ===
# 
# RULES:
# 1. Follow BTC direction - only buy the winning side
# 2. Fast take profit at +4¢
# 3. Stop loss at -4¢
# 4. One position at a time
# 5. No trades in last 5 minutes

TRADE_SIZE = 5              # $ per trade
MAX_BUY_PRICE = 0.42        # Only buy if price <= 42¢
TAKE_PROFIT = 0.04          # Sell at +4¢ (faster exits)
STOP_LOSS = 0.04            # Sell at -4¢ (cut losses)
MIN_TIME_LEFT = 5.0         # Don't enter after 10 min mark
BTC_THRESHOLD = 0.01        # BTC must move 0.01%+ to have direction

SLUG = None
tokens = None
candle_start_time = None
candle_open_btc = None

# Position tracking
position = None  # {side, shares, avg_price, cost, token_id}
pending_order = None  # {side, order_id, price, shares, token_id, placed_at}

session_pnl = 0.0
trades_won = 0
trades_lost = 0


def log(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")


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
        except:
            pass
    return False


def set_allowances():
    if not tokens:
        return False
    try:
        params1 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["up_t"])
        client.update_balance_allowance(params1)
        params2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tokens["dn_t"])
        client.update_balance_allowance(params2)
        log("Allowances set ✓")
        return True
    except Exception as e:
        log(f"Allowance error: {e}")
        return False


def get_prices():
    if not tokens:
        return None, None
    try:
        up = float(client.get_price(tokens["up_t"], "buy").get("price", 0.5))
        dn = float(client.get_price(tokens["dn_t"], "buy").get("price", 0.5))
        return up, dn
    except:
        return None, None


def place_buy_order(token_id, price, amount, side):
    """Place GTC limit buy order"""
    global pending_order
    
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
            log(f"✓ BUY ORDER: {shares:.1f} {side} @ {price*100:.0f}¢")
            
            pending_order = {
                "side": side,
                "order_id": order_id,
                "price": price,
                "shares": shares,
                "token_id": token_id,
                "placed_at": time.time()
            }
            return True
        else:
            err = resp.get("error", resp.get("data", "Unknown"))
            log(f"✗ Buy failed: {err}")
            return False
    except Exception as e:
        log(f"✗ Buy error: {e}")
        return False


def place_sell_order(token_id, shares, price, side):
    """Place GTC limit sell order"""
    try:
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side=SELL
        )
        
        signed_order = client.create_order(order_args, opt)
        resp = client.post_order(signed_order, OrderType.GTC)
        
        if resp.get("success"):
            order_id = resp.get("orderID") or resp.get("data", {}).get("orderID")
            log(f"✓ SELL ORDER: {shares:.1f} {side} @ {price*100:.0f}¢")
            return order_id
        else:
            err = resp.get("error", resp.get("data", "Unknown"))
            log(f"✗ Sell failed: {err}")
            return None
    except Exception as e:
        log(f"✗ Sell error: {e}")
        return None


def check_order_filled(order_id):
    """Check if order is filled"""
    try:
        order = client.get_order(order_id)
        if order:
            size_matched = float(order.get("size_matched", 0))
            if size_matched > 0:
                return size_matched, float(order.get("price", 0))
        return 0, 0
    except:
        return 0, 0


def cancel_order(order_id):
    try:
        client.cancel(order_id)
        return True
    except:
        return False


def cancel_all():
    try:
        client.cancel_all()
    except:
        pass


def reset_state():
    global position, pending_order, candle_open_btc
    cancel_all()
    position = None
    pending_order = None
    candle_open_btc = None


def get_btc_direction(btc_now):
    """Returns 'UP', 'DN', or 'FLAT' based on BTC movement"""
    if candle_open_btc is None:
        return "FLAT"
    
    change_pct = ((btc_now - candle_open_btc) / candle_open_btc) * 100
    
    if change_pct >= BTC_THRESHOLD:
        return "UP"
    elif change_pct <= -BTC_THRESHOLD:
        return "DN"
    else:
        return "FLAT"


async def main():
    global SLUG, tokens, candle_start_time, candle_open_btc
    global position, pending_order
    global session_pnl, trades_won, trades_lost
    
    print("=" * 50)
    print("SMART SCALPER v53")
    print(f"Follow BTC | TP:+{TAKE_PROFIT*100:.0f}¢ | SL:-{STOP_LOSS*100:.0f}¢")
    print("=" * 50)
    
    find_active_market()
    if SLUG:
        log(f"Market: {SLUG}")
    
    if tokens:
        set_allowances()
    
    while True:
        try:
            async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                log("Connected to Binance ✓")
                
                tick_count = 0
                last_price_check = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    tick_count += 1
                    
                    # Set candle open
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        log(f"Candle open: ${candle_open_btc:,.2f}")
                    
                    # Check for new market every 100 ticks
                    if tick_count % 100 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
                                log("*** NEW CANDLE ***")
                                reset_state()
                                set_allowances()
                                candle_open_btc = btc_now
                    
                    # Rate limit price checks (every 0.3 sec)
                    now = time.time()
                    if now - last_price_check < 0.3:
                        continue
                    last_price_check = now
                    
                    if not tokens:
                        continue
                    
                    # Get market prices
                    up_price, dn_price = get_prices()
                    if up_price is None:
                        continue
                    
                    minutes_left = get_minutes_remaining()
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100 if candle_open_btc else 0
                    btc_dir = get_btc_direction(btc_now)
                    
                    # === STATE: PENDING ORDER ===
                    if pending_order and not position:
                        filled, fill_price = check_order_filled(pending_order["order_id"])
                        
                        if filled > 0:
                            log(f"💚 FILLED: {filled:.1f} {pending_order['side']} @ {fill_price*100:.0f}¢")
                            position = {
                                "side": pending_order["side"],
                                "shares": filled,
                                "avg_price": fill_price,
                                "cost": filled * fill_price,
                                "token_id": pending_order["token_id"]
                            }
                            pending_order = None
                        
                        # Cancel if waited too long or wrong direction
                        elif now - pending_order["placed_at"] > 10:
                            log("⏰ Order timeout - cancelling")
                            cancel_order(pending_order["order_id"])
                            pending_order = None
                        
                        # Cancel if BTC direction changed against us
                        elif (pending_order["side"] == "UP" and btc_dir == "DN") or \
                             (pending_order["side"] == "DN" and btc_dir == "UP"):
                            log(f"🔄 BTC reversed to {btc_dir} - cancelling")
                            cancel_order(pending_order["order_id"])
                            pending_order = None
                        
                        else:
                            wait = int(now - pending_order["placed_at"])
                            print(f"⏳ {pending_order['side']} @ {pending_order['price']*100:.0f}¢ | BTC:{btc_change:+.2f}% {btc_dir} | {wait}s | {minutes_left:.1f}m   ", end='\r')
                        continue
                    
                    # === STATE: HAVE POSITION ===
                    if position:
                        current_price = up_price if position["side"] == "UP" else dn_price
                        profit = current_price - position["avg_price"]
                        unrealized = profit * position["shares"]
                        
                        # Display
                        emoji = "📈" if profit > 0 else "📉"
                        print(f"{emoji} {position['side']} {position['shares']:.0f}@{position['avg_price']*100:.0f}¢ → {current_price*100:.0f}¢ | P/L:${unrealized:+.2f} | BTC:{btc_change:+.2f}% | {minutes_left:.1f}m   ", end='\r')
                        
                        should_sell = False
                        reason = ""
                        
                        # Take profit
                        if profit >= TAKE_PROFIT:
                            should_sell = True
                            reason = "💰 TAKE PROFIT"
                        
                        # Stop loss
                        elif profit <= -STOP_LOSS:
                            should_sell = True
                            reason = "🛑 STOP LOSS"
                        
                        # Time exit
                        elif minutes_left < 1.5:
                            should_sell = True
                            reason = "⏰ TIME EXIT"
                        
                        # Direction changed against us
                        elif (position["side"] == "UP" and btc_dir == "DN" and profit < 0) or \
                             (position["side"] == "DN" and btc_dir == "UP" and profit < 0):
                            should_sell = True
                            reason = "🔄 DIRECTION EXIT"
                        
                        if should_sell:
                            log(f"{reason} - Selling {position['side']}")
                            
                            sell_price = max(current_price - 0.01, 0.01)
                            order_id = place_sell_order(
                                position["token_id"],
                                position["shares"],
                                sell_price,
                                position["side"]
                            )
                            
                            if order_id:
                                await asyncio.sleep(1)
                                filled, _ = check_order_filled(order_id)
                                
                                if filled > 0:
                                    pnl = (sell_price * filled) - position["cost"]
                                    session_pnl += pnl
                                    if pnl > 0:
                                        trades_won += 1
                                    else:
                                        trades_lost += 1
                                    log(f"P/L: ${pnl:+.2f} | Session: ${session_pnl:+.2f} ({trades_won}W/{trades_lost}L)")
                                else:
                                    # Try to cancel and accept loss
                                    cancel_all()
                                    log("⚠️ Sell may not have filled")
                            
                            position = None
                        continue
                    
                    # === STATE: LOOKING FOR ENTRY ===
                    if not position and not pending_order:
                        
                        # Don't enter too late
                        if minutes_left < MIN_TIME_LEFT:
                            print(f"👀 UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | BTC:{btc_change:+.2f}% {btc_dir} | {minutes_left:.1f}m | Too late   ", end='\r')
                            continue
                        
                        # Determine which side to buy based on BTC direction
                        target_side = None
                        target_price = None
                        target_token = None
                        
                        if btc_dir == "UP" and up_price <= MAX_BUY_PRICE:
                            # BTC rising, buy UP if cheap
                            target_side = "UP"
                            target_price = up_price
                            target_token = tokens["up_t"]
                        
                        elif btc_dir == "DN" and dn_price <= MAX_BUY_PRICE:
                            # BTC falling, buy DN if cheap
                            target_side = "DN"
                            target_price = dn_price
                            target_token = tokens["dn_t"]
                        
                        elif btc_dir == "FLAT":
                            # BTC flat, buy whichever is cheaper
                            if up_price <= MAX_BUY_PRICE and up_price < dn_price:
                                target_side = "UP"
                                target_price = up_price
                                target_token = tokens["up_t"]
                            elif dn_price <= MAX_BUY_PRICE:
                                target_side = "DN"
                                target_price = dn_price
                                target_token = tokens["dn_t"]
                        
                        if target_side:
                            # Add 2¢ to ensure we get filled (be a taker)
                            fill_price = min(target_price + 0.02, 0.50)
                            log(f"🎯 BTC {btc_dir} → Buying {target_side} @ {fill_price*100:.0f}¢")
                            place_buy_order(target_token, fill_price, TRADE_SIZE, target_side)
                        else:
                            print(f"👀 UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | BTC:{btc_change:+.2f}% {btc_dir} | {minutes_left:.1f}m | Waiting...   ", end='\r')
                    
                    await asyncio.sleep(0.1)
                    
        except websockets.exceptions.ConnectionClosed:
            log("Reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            log(f"Error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
