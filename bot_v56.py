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

# === QUANT BOT v56 - HYBRID ===
# 
# TWO STRATEGIES IN ONE:
#
# 1. SCALP MODE (edge 8-20%):
#    - Quick entry/exit
#    - Take profit at +4¢
#    - Stop loss at -5¢
#    - Many trades, small wins
#
# 2. HOLD MODE (edge > 20%):
#    - Hold to settlement
#    - No stop loss
#    - Fewer trades, bigger wins
#
# The bot decides which mode based on edge size!

# === PARAMETERS ===
BANKROLL = 50

# Scalp mode (small edge)
SCALP_MIN_EDGE = 0.08        # 8% minimum
SCALP_MAX_EDGE = 0.20        # Below 20% = scalp
SCALP_TAKE_PROFIT = 0.04     # +4¢ quick exit
SCALP_STOP_LOSS = 0.05       # -5¢ cut losses
SCALP_MIN_TIME = 5.0         # Can trade with 5+ min left
SCALP_COOLDOWN = 20          # 20 sec between scalps

# Hold mode (big edge)
HOLD_MIN_EDGE = 0.20         # 20%+ = hold to settlement
HOLD_MIN_TIME = 8.0          # Need 8+ min for hold
HOLD_EXIT_PROB = 0.25        # Only exit if prob < 25%

# General
MIN_PRICE = 0.25             # Don't buy below 25¢
MAX_PRICE = 0.52             # Don't buy above 52¢
MAX_BTC_MOMENTUM = 0.08      # Skip if BTC moved >0.08% in 60s
MIN_BET = 3
MAX_BET = 10

# BTC volatility
BTC_15MIN_VOLATILITY = 0.0018

SLUG = None
tokens = None
candle_start_time = None
candle_open_btc = None

# Position tracking
position = None  # {side, shares, avg_price, cost, token_id, entry_time, mode}
pending_order = None
last_trade_time = 0

# BTC history
btc_history = deque(maxlen=120)

# Stats
session_pnl = 0.0
scalp_wins = 0
scalp_losses = 0
hold_wins = 0
hold_losses = 0


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
        return True
    except:
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


def normal_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def calculate_true_probability(btc_now, btc_open, seconds_left):
    if btc_open <= 0 or seconds_left <= 0:
        return 0.5
    
    current_return = (btc_now - btc_open) / btc_open
    time_fraction = seconds_left / 900
    remaining_volatility = BTC_15MIN_VOLATILITY * math.sqrt(time_fraction)
    
    if remaining_volatility <= 0:
        return 1.0 if current_return > 0 else 0.0
    
    z_score = current_return / remaining_volatility
    prob_up = normal_cdf(z_score)
    return max(0.02, min(0.98, prob_up))


def calculate_edge(true_prob, market_price, side):
    if side == "UP":
        return true_prob - market_price
    else:
        return (1 - true_prob) - market_price


def kelly_bet_size(edge, price):
    if edge <= 0 or price <= 0 or price >= 1:
        return MIN_BET
    
    kelly = (edge / (1 - price)) * 0.25
    bet = BANKROLL * min(kelly, 0.20)
    return max(MIN_BET, min(MAX_BET, bet))


def get_btc_momentum():
    if len(btc_history) < 60:
        return 0
    prices = list(btc_history)
    if prices[0] <= 0:
        return 0
    return abs((prices[-1] - prices[0]) / prices[0]) * 100


def place_buy_order(token_id, price, amount, side):
    global pending_order
    try:
        shares = amount / price
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        order_args = OrderArgs(token_id=token_id, price=price, size=shares, side=BUY)
        signed_order = client.create_order(order_args, opt)
        resp = client.post_order(signed_order, OrderType.GTC)
        
        if resp.get("success"):
            order_id = resp.get("orderID") or resp.get("data", {}).get("orderID")
            log(f"✓ BUY: {shares:.1f} {side} @ {price*100:.0f}¢ (${amount:.2f})")
            pending_order = {
                "side": side, "order_id": order_id, "price": price,
                "shares": shares, "token_id": token_id, "placed_at": time.time()
            }
            return True
        return False
    except Exception as e:
        log(f"✗ Buy error: {e}")
        return False


def place_sell_order(token_id, shares, price, side):
    try:
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        order_args = OrderArgs(token_id=token_id, price=price, size=shares, side=SELL)
        signed_order = client.create_order(order_args, opt)
        resp = client.post_order(signed_order, OrderType.GTC)
        
        if resp.get("success"):
            order_id = resp.get("orderID") or resp.get("data", {}).get("orderID")
            log(f"✓ SELL: {shares:.1f} {side} @ {price*100:.0f}¢")
            return order_id
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
    except:
        pass


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


async def execute_sell(current_price):
    """Execute sell with retry logic"""
    global position, session_pnl, scalp_wins, scalp_losses, hold_wins, hold_losses, last_trade_time
    
    if not position:
        return
    
    await asyncio.sleep(2)
    
    # Re-set allowance
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=position["token_id"])
        client.update_balance_allowance(params)
    except:
        pass
    
    await asyncio.sleep(1)
    
    sell_price = max(current_price - 0.02, 0.01)
    order_id = place_sell_order(position["token_id"], position["shares"], sell_price, position["side"])
    
    if order_id:
        await asyncio.sleep(2)
        filled, _ = check_order_filled(order_id)
        
        if filled > 0:
            pnl = (sell_price * filled) - position["cost"]
            session_pnl += pnl
            
            mode = position.get("mode", "SCALP")
            if pnl > 0:
                if mode == "SCALP":
                    scalp_wins += 1
                else:
                    hold_wins += 1
            else:
                if mode == "SCALP":
                    scalp_losses += 1
                else:
                    hold_losses += 1
            
            log(f"P/L: ${pnl:+.2f} | Session: ${session_pnl:+.2f}")
            log(f"Stats: Scalp {scalp_wins}W/{scalp_losses}L | Hold {hold_wins}W/{hold_losses}L")
            last_trade_time = time.time()
            position = None
            return True
        else:
            cancel_all()
            # Try lower price
            lower_price = max(current_price - 0.05, 0.01)
            order_id2 = place_sell_order(position["token_id"], position["shares"], lower_price, position["side"])
            if order_id2:
                await asyncio.sleep(2)
                filled2, _ = check_order_filled(order_id2)
                if filled2 > 0:
                    pnl = (lower_price * filled2) - position["cost"]
                    session_pnl += pnl
                    log(f"P/L: ${pnl:+.2f} | Session: ${session_pnl:+.2f}")
                    last_trade_time = time.time()
                    position = None
                    return True
    
    return False


async def main():
    global SLUG, tokens, candle_start_time, candle_open_btc
    global position, pending_order, last_trade_time
    global session_pnl
    
    print("=" * 60)
    print("QUANT BOT v56 - HYBRID (SCALP + HOLD)")
    print(f"SCALP: 8-20% edge → TP +4¢ | HOLD: 20%+ edge → Settlement")
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
                last_analysis = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    tick_count += 1
                    
                    btc_history.append(btc_now)
                    
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        log(f"Candle open: ${candle_open_btc:,.2f}")
                    
                    # Check new market
                    if tick_count % 100 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
                                if position:
                                    log("*** CANDLE ENDED ***")
                                reset_state()
                                set_allowances()
                                candle_open_btc = btc_now
                                log(f"New candle: {SLUG}")
                    
                    # Rate limit
                    now = time.time()
                    if now - last_analysis < 0.3:
                        continue
                    last_analysis = now
                    
                    if not tokens:
                        continue
                    
                    up_price, dn_price = get_prices()
                    if up_price is None:
                        continue
                    
                    seconds_left = get_seconds_remaining()
                    minutes_left = seconds_left / 60
                    
                    true_prob_up = calculate_true_probability(btc_now, candle_open_btc, seconds_left)
                    true_prob_dn = 1 - true_prob_up
                    
                    edge_up = calculate_edge(true_prob_up, up_price, "UP")
                    edge_dn = calculate_edge(true_prob_up, dn_price, "DN")
                    
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100 if candle_open_btc else 0
                    momentum = get_btc_momentum()
                    
                    # === PENDING ORDER ===
                    if pending_order and not position:
                        filled, fill_price = check_order_filled(pending_order["order_id"])
                        
                        if filled > 0:
                            mode = pending_order.get("mode", "SCALP")
                            log(f"💚 FILLED [{mode}]: {filled:.1f} {pending_order['side']} @ {fill_price*100:.0f}¢")
                            position = {
                                "side": pending_order["side"],
                                "shares": filled,
                                "avg_price": fill_price,
                                "cost": filled * fill_price,
                                "token_id": pending_order["token_id"],
                                "entry_time": time.time(),
                                "mode": mode,
                                "entry_edge": pending_order.get("edge", 0)
                            }
                            pending_order = None
                        
                        elif now - pending_order["placed_at"] > 10:
                            cancel_order(pending_order["order_id"])
                            pending_order = None
                        else:
                            wait = int(now - pending_order["placed_at"])
                            print(f"⏳ {pending_order['side']} @ {pending_order['price']*100:.0f}¢ ({wait}s)", end='\r')
                        continue
                    
                    # === HAVE POSITION ===
                    if position:
                        current_price = up_price if position["side"] == "UP" else dn_price
                        profit = current_price - position["avg_price"]
                        unrealized = profit * position["shares"]
                        hold_time = now - position["entry_time"]
                        our_prob = true_prob_up if position["side"] == "UP" else true_prob_dn
                        mode = position.get("mode", "SCALP")
                        
                        # Display
                        emoji = "🟢" if profit >= 0 else "🔴"
                        print(f"{emoji} [{mode}] {position['side']} | {position['avg_price']*100:.0f}→{current_price*100:.0f}¢ | ${unrealized:+.2f} | P:{our_prob*100:.0f}% | {minutes_left:.1f}m", end='\r')
                        
                        should_sell = False
                        reason = ""
                        
                        if mode == "SCALP":
                            # SCALP MODE: Quick TP/SL
                            if profit >= SCALP_TAKE_PROFIT:
                                should_sell = True
                                reason = "💰 SCALP TP"
                            elif profit <= -SCALP_STOP_LOSS and hold_time >= 15:
                                should_sell = True
                                reason = "🛑 SCALP SL"
                            elif minutes_left < 1.0:
                                should_sell = True
                                reason = "⏰ TIME"
                        
                        else:
                            # HOLD MODE: Only exit if prob tanked
                            if our_prob < HOLD_EXIT_PROB and hold_time >= 30:
                                should_sell = True
                                reason = f"📉 PROB {our_prob*100:.0f}%"
                            elif minutes_left < 0.5 and our_prob < 0.45:
                                should_sell = True
                                reason = "⏰ TIME (losing)"
                        
                        if should_sell:
                            log(reason)
                            await execute_sell(current_price)
                        
                        continue
                    
                    # === LOOKING FOR ENTRY ===
                    if not position and not pending_order:
                        
                        # Cooldown check
                        if last_trade_time > 0 and (now - last_trade_time) < SCALP_COOLDOWN:
                            cd = int(SCALP_COOLDOWN - (now - last_trade_time))
                            print(f"⏳ Cooldown {cd}s | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m", end='\r')
                            continue
                        
                        # Momentum check
                        if momentum > MAX_BTC_MOMENTUM:
                            print(f"⚡ Volatile ({momentum:.2f}%) | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | {minutes_left:.1f}m", end='\r')
                            continue
                        
                        # Find best opportunity
                        best_side = None
                        best_edge = 0
                        best_price = 0
                        best_token = None
                        best_prob = 0
                        
                        # Check UP
                        if edge_up >= SCALP_MIN_EDGE and MIN_PRICE <= up_price <= MAX_PRICE:
                            best_side = "UP"
                            best_edge = edge_up
                            best_price = up_price
                            best_token = tokens["up_t"]
                            best_prob = true_prob_up
                        
                        # Check DN (prefer higher edge)
                        if edge_dn >= SCALP_MIN_EDGE and MIN_PRICE <= dn_price <= MAX_PRICE:
                            if edge_dn > best_edge:
                                best_side = "DN"
                                best_edge = edge_dn
                                best_price = dn_price
                                best_token = tokens["dn_t"]
                                best_prob = true_prob_dn
                        
                        if best_side:
                            # Decide mode based on edge size and time
                            if best_edge >= HOLD_MIN_EDGE and minutes_left >= HOLD_MIN_TIME:
                                mode = "HOLD"
                            elif minutes_left >= SCALP_MIN_TIME:
                                mode = "SCALP"
                            else:
                                mode = None  # Too late
                            
                            if mode:
                                bet = kelly_bet_size(best_edge, best_price)
                                fill_price = min(best_price + 0.01, 0.55)
                                
                                log(f"🎯 [{mode}] {best_side} | Edge:{best_edge*100:.1f}% | Prob:{best_prob*100:.0f}%")
                                
                                if place_buy_order(best_token, fill_price, bet, best_side):
                                    pending_order["mode"] = mode
                                    pending_order["edge"] = best_edge
                        
                        else:
                            # No edge
                            e_up = edge_up * 100
                            e_dn = edge_dn * 100
                            print(f"👀 UP:{up_price*100:.0f}¢({e_up:+.0f}) DN:{dn_price*100:.0f}¢({e_dn:+.0f}) | BTC:{btc_change:+.2f}% | {minutes_left:.1f}m", end='\r')
                    
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
