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

# === SCALPER v52 - GTC LIMIT ORDERS ===
# 
# Strategy: Buy cheap side, sell when it rebounds
# Uses GTC limit orders that WILL fill
# One trade at a time, proper tracking

TRADE_SIZE = 5              # $ per trade
MAX_BUY_PRICE = 0.42        # Only buy if price <= 42¢
TAKE_PROFIT = 0.06          # Sell when +6¢ profit
MIN_TIME_LEFT = 1.5         # Exit with 1.5 min left

SLUG = None
tokens = None
candle_start_time = None

# Position tracking
position = None  # {side, shares, avg_price, cost, token_id, order_id}
pending_order = None  # {side, order_id, price, shares, token_id}

session_pnl = 0.0
trades_won = 0
trades_lost = 0


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


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
            log(f"Error checking {expected_slug}: {e}")
    
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
            log(f"✓ BUY ORDER: {shares:.1f} {side} @ {price*100:.0f}¢ (${amount:.2f})")
            
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
            log(f"✗ Buy order failed: {err}")
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
            log(f"✗ Sell order failed: {err}")
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
            original_size = float(order.get("original_size", 0))
            
            if size_matched > 0:
                return size_matched, float(order.get("price", 0))
        return 0, 0
    except:
        return 0, 0


def cancel_order(order_id):
    """Cancel an order"""
    try:
        client.cancel(order_id)
        log(f"✓ Order cancelled")
        return True
    except Exception as e:
        log(f"✗ Cancel failed: {e}")
        return False


def cancel_all():
    """Cancel all open orders"""
    try:
        client.cancel_all()
        log("✓ All orders cancelled")
    except:
        pass


def reset_state():
    global position, pending_order
    cancel_all()
    position = None
    pending_order = None


async def main():
    global SLUG, tokens, candle_start_time
    global position, pending_order
    global session_pnl, trades_won, trades_lost
    
    print("=" * 50)
    print("SCALPER v52 - GTC LIMIT ORDERS")
    print(f"Buy ≤{MAX_BUY_PRICE*100:.0f}¢ | TP: +{TAKE_PROFIT*100:.0f}¢")
    print("=" * 50)
    
    find_active_market()
    if SLUG:
        log(f"Market: {SLUG}")
    
    if tokens:
        set_allowances()
    
    last_check = 0
    
    while True:
        try:
            now = time.time()
            
            if now - last_check < 1:
                await asyncio.sleep(0.3)
                continue
            
            last_check = now
            
            # Check for new market
            old_slug = SLUG
            if find_active_market():
                if old_slug and old_slug != SLUG:
                    log("*** NEW CANDLE ***")
                    reset_state()
                    set_allowances()
            
            if not tokens:
                continue
            
            up_price, dn_price = get_prices()
            if up_price is None:
                continue
            
            minutes_left = get_minutes_remaining()
            
            # === STATE MACHINE ===
            
            # STATE 1: We have a pending buy order - check if filled
            if pending_order and not position:
                filled, fill_price = check_order_filled(pending_order["order_id"])
                
                if filled > 0:
                    log(f"💚 BUY FILLED: {filled:.1f} {pending_order['side']} @ {fill_price*100:.0f}¢")
                    position = {
                        "side": pending_order["side"],
                        "shares": filled,
                        "avg_price": fill_price,
                        "cost": filled * fill_price,
                        "token_id": pending_order["token_id"]
                    }
                    pending_order = None
                
                # Cancel if too old (>60 sec) or time running out
                elif time.time() - pending_order["placed_at"] > 60 or minutes_left < 2:
                    log("⏰ Cancelling unfilled order")
                    cancel_order(pending_order["order_id"])
                    pending_order = None
                
                else:
                    # Still waiting
                    wait_time = int(time.time() - pending_order["placed_at"])
                    print(f"⏳ Waiting for fill... {pending_order['side']} @ {pending_order['price']*100:.0f}¢ ({wait_time}s) | {minutes_left:.1f}m", end='\r')
                    continue
            
            # STATE 2: We have a position - manage it
            if position:
                current_price = up_price if position["side"] == "UP" else dn_price
                profit = current_price - position["avg_price"]
                unrealized = profit * position["shares"]
                
                print(f"📈 {position['side']} {position['shares']:.0f}@{position['avg_price']*100:.0f}¢ | Now:{current_price*100:.0f}¢ | P/L:${unrealized:+.2f} | {minutes_left:.1f}m", end='\r')
                
                should_sell = False
                reason = ""
                
                # Take profit
                if profit >= TAKE_PROFIT:
                    should_sell = True
                    reason = "TAKE PROFIT"
                
                # Time exit
                elif minutes_left < MIN_TIME_LEFT:
                    should_sell = True
                    reason = "TIME EXIT"
                
                if should_sell:
                    print()  # New line
                    log(f"💰 {reason} - Selling {position['side']}")
                    
                    # Place sell order at current price (should fill immediately)
                    sell_price = current_price - 0.01  # Slightly below to ensure fill
                    order_id = place_sell_order(
                        position["token_id"],
                        position["shares"],
                        sell_price,
                        position["side"]
                    )
                    
                    if order_id:
                        # Wait a moment and check fill
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
                            log("⚠️ Sell may not have filled completely")
                    
                    position = None
                
                continue
            
            # STATE 3: No position, no pending order - look for opportunity
            if not position and not pending_order and minutes_left > 3:
                # Find the cheaper side
                if up_price <= MAX_BUY_PRICE and up_price < dn_price:
                    log(f"🎯 BUYING UP @ {up_price*100:.0f}¢")
                    place_buy_order(tokens["up_t"], up_price, TRADE_SIZE, "UP")
                
                elif dn_price <= MAX_BUY_PRICE and dn_price < up_price:
                    log(f"🎯 BUYING DN @ {dn_price*100:.0f}¢")
                    place_buy_order(tokens["dn_t"], dn_price, TRADE_SIZE, "DN")
                
                else:
                    # No opportunity - just display
                    print(f"👀 UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m | Waiting for ≤{MAX_BUY_PRICE*100:.0f}¢...", end='\r')
            
            elif not position and not pending_order:
                print(f"👀 UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m | Too late to enter", end='\r')
            
            await asyncio.sleep(0.5)
            
        except Exception as e:
            log(f"Error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
