import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
import os
import time
import math
from dotenv import load_dotenv
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

# === QUANT BOT v55 ===
# 
# HOLD TO SETTLEMENT STRATEGY
# 
# Key insight: If we have edge, let the math play out.
# Don't panic sell. Trust the probability.
#
# Changes from v54:
# - No stop loss (let it ride)
# - Earlier entries only (10+ min left)
# - Higher edge requirement (15%)
# - Momentum filter (skip if BTC volatile)
# - Only exit if probability drops below 30%

# === PARAMETERS ===
BANKROLL = 50                # Total bankroll
MIN_EDGE = 0.15              # Minimum 15% edge to trade
MAX_BET_FRACTION = 0.20      # Max 20% of bankroll per trade
MIN_BET = 3                  # Minimum $3 bet
MAX_BET = 10                 # Maximum $10 bet
MIN_TIME_LEFT = 10.0         # Only trade with 10+ min left
MIN_PRICE = 0.25             # Don't buy below 25¢
MAX_PRICE = 0.55             # Don't buy above 55¢
EXIT_PROB_THRESHOLD = 0.30   # Only exit if our prob drops below 30%
MAX_BTC_MOMENTUM = 0.10      # Skip if BTC moved >0.10% in last 60 sec

# === BTC VOLATILITY MODEL ===
BTC_15MIN_VOLATILITY = 0.0018  # 0.18% std dev per 15 min

SLUG = None
tokens = None
candle_start_time = None
candle_open_btc = None

# Position tracking
position = None
pending_order = None
last_trade_time = 0

# BTC price history for momentum
btc_history = deque(maxlen=120)  # Last 60 seconds (2 per sec)

session_pnl = 0.0
trades_won = 0
trades_lost = 0
trades_skipped = 0


def log(msg):
    print(f"\n[{time.strftime('%H:%M:%S')}] {msg}")


def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900


def get_seconds_remaining():
    if candle_start_time:
        end_ts = candle_start_time + 900
        remaining = end_ts - time.time()
        return max(0, remaining)
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


# === PROBABILITY MATH ===

def normal_cdf(x):
    """Standard normal CDF"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def calculate_true_probability(btc_now, btc_open, seconds_left, volatility=BTC_15MIN_VOLATILITY):
    """
    Calculate TRUE probability that BTC finishes ABOVE open.
    Uses Black-Scholes style math for binary options.
    """
    if btc_open <= 0 or seconds_left <= 0:
        return 0.5
    
    current_return = (btc_now - btc_open) / btc_open
    time_fraction = seconds_left / 900
    remaining_volatility = volatility * math.sqrt(time_fraction)
    
    if remaining_volatility <= 0:
        return 1.0 if current_return > 0 else 0.0
    
    z_score = current_return / remaining_volatility
    prob_up = normal_cdf(z_score)
    prob_up = max(0.02, min(0.98, prob_up))
    
    return prob_up


def calculate_edge(true_prob, market_price, side):
    """Calculate edge for a bet"""
    if side == "UP":
        edge = true_prob - market_price
    else:
        edge = (1 - true_prob) - market_price
    return edge


def kelly_bet_size(edge, price, bankroll):
    """Kelly criterion bet sizing (25% Kelly for safety)"""
    if edge <= 0 or price <= 0 or price >= 1:
        return 0
    
    kelly_fraction = edge / (1 - price)
    conservative_kelly = kelly_fraction * 0.25
    bet_fraction = min(conservative_kelly, MAX_BET_FRACTION)
    bet_amount = bankroll * bet_fraction
    bet_amount = max(MIN_BET, min(MAX_BET, bet_amount))
    
    return bet_amount


def get_btc_momentum():
    """
    Calculate how much BTC moved in the last 60 seconds.
    Returns absolute % change.
    """
    if len(btc_history) < 60:
        return 0
    
    recent = list(btc_history)
    price_60s_ago = recent[0]
    price_now = recent[-1]
    
    if price_60s_ago <= 0:
        return 0
    
    momentum = abs((price_now - price_60s_ago) / price_60s_ago) * 100
    return momentum


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
            log(f"✓ BUY: {shares:.1f} {side} @ {price*100:.0f}¢ (${amount:.2f})")
            
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
            log(f"✓ SELL: {shares:.1f} {side} @ {price*100:.0f}¢")
            return order_id
        else:
            err = resp.get("error", resp.get("data", "Unknown"))
            log(f"✗ Sell failed: {err}")
            return None
    except Exception as e:
        log(f"✗ Sell error: {e}")
        return None


def check_order_filled(order_id):
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
    global position, pending_order, candle_open_btc, last_trade_time, btc_history
    cancel_all()
    position = None
    pending_order = None
    candle_open_btc = None
    last_trade_time = 0
    btc_history.clear()


async def main():
    global SLUG, tokens, candle_start_time, candle_open_btc
    global position, pending_order, last_trade_time
    global session_pnl, trades_won, trades_lost, trades_skipped
    
    print("=" * 60)
    print("QUANT BOT v55 - HOLD TO SETTLEMENT")
    print(f"Min Edge: {MIN_EDGE*100:.0f}% | Min Time: {MIN_TIME_LEFT:.0f}m | No Stop Loss")
    print("=" * 60)
    
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
                last_analysis = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    tick_count += 1
                    
                    # Track BTC history for momentum
                    btc_history.append(btc_now)
                    
                    # Set candle open
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        log(f"Candle open: ${candle_open_btc:,.2f}")
                    
                    # Check for new market
                    if tick_count % 100 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
                                # CANDLE ENDED - check if we won or lost
                                if position:
                                    log("*** CANDLE ENDED - Position settles automatically ***")
                                    # Can't know result here, but shares will settle on-chain
                                    position = None
                                
                                log("*** NEW CANDLE ***")
                                reset_state()
                                set_allowances()
                                candle_open_btc = btc_now
                    
                    # Rate limit analysis
                    now = time.time()
                    if now - last_analysis < 0.5:
                        continue
                    last_analysis = now
                    
                    if not tokens:
                        continue
                    
                    # Get market prices
                    up_price, dn_price = get_prices()
                    if up_price is None:
                        continue
                    
                    seconds_left = get_seconds_remaining()
                    minutes_left = seconds_left / 60
                    
                    # Calculate true probability
                    true_prob_up = calculate_true_probability(btc_now, candle_open_btc, seconds_left)
                    true_prob_dn = 1 - true_prob_up
                    
                    # Calculate edges
                    edge_up = calculate_edge(true_prob_up, up_price, "UP")
                    edge_dn = calculate_edge(true_prob_up, dn_price, "DN")
                    
                    # BTC stats
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100 if candle_open_btc else 0
                    momentum = get_btc_momentum()
                    
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
                                "token_id": pending_order["token_id"],
                                "entry_time": time.time()
                            }
                            pending_order = None
                        
                        elif now - pending_order["placed_at"] > 15:
                            log("⏰ Order timeout")
                            cancel_order(pending_order["order_id"])
                            pending_order = None
                        
                        else:
                            wait = int(now - pending_order["placed_at"])
                            print(f"⏳ Waiting... {pending_order['side']} @ {pending_order['price']*100:.0f}¢ ({wait}s) | BTC:{btc_change:+.3f}%", end='\r')
                        continue
                    
                    # === STATE: HAVE POSITION (HOLD TO SETTLEMENT) ===
                    if position:
                        current_price = up_price if position["side"] == "UP" else dn_price
                        profit = current_price - position["avg_price"]
                        unrealized = profit * position["shares"]
                        hold_time = now - position.get("entry_time", now)
                        
                        our_prob = true_prob_up if position["side"] == "UP" else true_prob_dn
                        
                        # Display
                        emoji = "📈" if our_prob >= 0.5 else "📉"
                        status = "WINNING" if our_prob >= 0.5 else "LOSING"
                        print(f"{emoji} {position['side']} | Entry:{position['avg_price']*100:.0f}¢ | Prob:{our_prob*100:.0f}% {status} | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m", end='\r')
                        
                        should_sell = False
                        reason = ""
                        
                        # ONLY exit if probability drops below 30% (we're very likely to lose)
                        if our_prob < EXIT_PROB_THRESHOLD and hold_time >= 30:
                            should_sell = True
                            reason = f"📉 PROB TOO LOW ({our_prob*100:.0f}%)"
                        
                        # Or if less than 1 min left and we're losing
                        elif minutes_left < 1.0 and our_prob < 0.45:
                            should_sell = True
                            reason = "⏰ TIME EXIT (losing)"
                        
                        if should_sell:
                            log(f"{reason}")
                            
                            await asyncio.sleep(2)
                            
                            # Re-set allowance
                            try:
                                params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=position["token_id"])
                                client.update_balance_allowance(params)
                            except:
                                pass
                            
                            await asyncio.sleep(1)
                            
                            sell_price = max(current_price - 0.02, 0.01)
                            order_id = place_sell_order(
                                position["token_id"],
                                position["shares"],
                                sell_price,
                                position["side"]
                            )
                            
                            if order_id:
                                await asyncio.sleep(2)
                                filled, _ = check_order_filled(order_id)
                                
                                if filled > 0:
                                    pnl = (sell_price * filled) - position["cost"]
                                    session_pnl += pnl
                                    if pnl > 0:
                                        trades_won += 1
                                    else:
                                        trades_lost += 1
                                    log(f"P/L: ${pnl:+.2f} | Session: ${session_pnl:+.2f} ({trades_won}W/{trades_lost}L)")
                                    last_trade_time = time.time()
                                else:
                                    cancel_all()
                                    log("⚠️ Sell failed - holding to settlement")
                                    continue  # Keep position, let it settle
                            
                            position = None
                        
                        # If winning (prob > 50%), just hold and let it settle
                        continue
                    
                    # === STATE: LOOKING FOR EDGE ===
                    if not position and not pending_order:
                        
                        # Don't trade too late
                        if minutes_left < MIN_TIME_LEFT:
                            print(f"👀 UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m | ⏰ Too late (need {MIN_TIME_LEFT:.0f}m+)", end='\r')
                            continue
                        
                        # Check momentum - skip if BTC too volatile
                        if momentum > MAX_BTC_MOMENTUM:
                            trades_skipped += 1
                            print(f"👀 UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m | ⚡ Too volatile ({momentum:.2f}%)", end='\r')
                            continue
                        
                        # Find best edge with price filter
                        best_side = None
                        best_edge = 0
                        best_price = 0
                        best_token = None
                        best_prob = 0
                        
                        if edge_up >= MIN_EDGE and MIN_PRICE <= up_price <= MAX_PRICE:
                            best_side = "UP"
                            best_edge = edge_up
                            best_price = up_price
                            best_token = tokens["up_t"]
                            best_prob = true_prob_up
                        
                        if edge_dn >= MIN_EDGE and MIN_PRICE <= dn_price <= MAX_PRICE:
                            if edge_dn > best_edge:
                                best_side = "DN"
                                best_edge = edge_dn
                                best_price = dn_price
                                best_token = tokens["dn_t"]
                                best_prob = true_prob_dn
                        
                        if best_side:
                            bet_amount = kelly_bet_size(best_edge, best_price, BANKROLL)
                            fill_price = min(best_price + 0.01, 0.60)
                            
                            log(f"🎯 EDGE FOUND: {best_side}")
                            log(f"   Market: {best_price*100:.0f}¢ | True: {best_prob*100:.0f}%")
                            log(f"   Edge: {best_edge*100:.1f}% | Bet: ${bet_amount:.2f}")
                            log(f"   Strategy: HOLD TO SETTLEMENT")
                            
                            place_buy_order(best_token, fill_price, bet_amount, best_side)
                        
                        else:
                            # No edge
                            up_diff = (true_prob_up - up_price) * 100
                            dn_diff = (true_prob_dn - dn_price) * 100
                            print(f"👀 UP:{up_price*100:.0f}¢({up_diff:+.0f}) DN:{dn_price*100:.0f}¢({dn_diff:+.0f}) | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m | No edge", end='\r')
                    
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
