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

# === QUANT BOT v54 ===
# 
# TRUE PROBABILITY-BASED TRADING
# 
# Uses Black-Scholes style math to calculate the TRUE probability
# that BTC will finish above/below the candle open.
# 
# Only trades when: Market Price != True Probability (edge exists)
# 
# Key insight: This is a binary option. We can calculate exact probabilities.

# === PARAMETERS ===
BANKROLL = 50                # Total bankroll for Kelly sizing
MIN_EDGE = 0.12              # Minimum 12% edge to trade (higher threshold)
MAX_BET_FRACTION = 0.15      # Never bet more than 15% of bankroll
MIN_BET = 3                  # Minimum $3 bet
MAX_BET = 10                 # Maximum $10 bet
TAKE_PROFIT = 0.08           # Take profit at 8¢ gain
STOP_LOSS = 0.06             # Stop loss at 6¢ loss
MIN_TIME_LEFT = 3.0          # Don't trade with less than 3 min left
COOLDOWN_AFTER_TRADE = 60    # Wait 60 seconds after any trade
MIN_PRICE = 0.30             # Don't buy below 30¢ (too risky)
MAX_PRICE = 0.55             # Don't buy above 55¢

# === BTC VOLATILITY MODEL ===
# Based on historical BTC data:
# - 15-minute standard deviation ≈ 0.15% to 0.25%
# - We use 0.18% as baseline (conservative)
BTC_15MIN_VOLATILITY = 0.0018  # 0.18% standard deviation per 15 min

SLUG = None
tokens = None
candle_start_time = None
candle_open_btc = None

# Position tracking
position = None
pending_order = None
last_trade_time = 0  # Cooldown tracking

# BTC price history for volatility calculation
btc_prices = deque(maxlen=1000)  # Last 1000 ticks

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


# === THE QUANT MATH ===

def normal_cdf(x):
    """
    Cumulative distribution function for standard normal distribution.
    This is the probability that a standard normal random variable is <= x.
    """
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def calculate_true_probability(btc_now, btc_open, seconds_left, volatility=BTC_15MIN_VOLATILITY):
    """
    Calculate the TRUE probability that BTC will finish ABOVE the open price.
    
    This uses the same math as Black-Scholes for binary options:
    
    Given:
    - Current price S
    - Strike price K (the candle open)
    - Time to expiration T (in fraction of 15 minutes)
    - Volatility σ (standard deviation per 15 minutes)
    
    The probability that S(T) > K is:
    P(up) = N(d2)
    
    where:
    d2 = [ln(S/K) + (σ²/2)T] / (σ√T)
    
    For small movements, this simplifies to:
    d2 ≈ (current_return) / (σ × √T)
    
    where current_return = (S - K) / K
    """
    
    if btc_open <= 0 or seconds_left <= 0:
        return 0.5
    
    # Current return (how much BTC has moved from open)
    current_return = (btc_now - btc_open) / btc_open
    
    # Time remaining as fraction of 15 minutes
    time_fraction = seconds_left / 900
    
    # Volatility scales with square root of time
    # If we have T time left, the expected movement is σ × √T
    remaining_volatility = volatility * math.sqrt(time_fraction)
    
    if remaining_volatility <= 0:
        # No time left, probability is 1 if up, 0 if down
        return 1.0 if current_return > 0 else 0.0
    
    # z-score: how many standard deviations is current price from open?
    # Positive z means BTC is above open
    z_score = current_return / remaining_volatility
    
    # Probability that BTC finishes above open
    # = Probability that final price > open
    # = Probability that (current + remaining movement) > open
    # = Probability that remaining movement > -current_return
    # = N(z_score) where z = current_return / remaining_volatility
    
    prob_up = normal_cdf(z_score)
    
    # Clamp to reasonable bounds (never 0% or 100% certain)
    prob_up = max(0.02, min(0.98, prob_up))
    
    return prob_up


def calculate_edge(true_prob, market_price, side):
    """
    Calculate the edge (expected value) of a bet.
    
    For a binary option:
    - If we buy at price P, we pay P and receive 1 if we win, 0 if we lose
    - EV = (prob_win × 1) - P = prob_win - P
    - Edge = EV / P = (prob_win - P) / P = (prob_win / P) - 1
    
    For buying UP:
    - prob_win = true_prob_up
    - edge = true_prob_up - market_price_up
    
    For buying DN:
    - prob_win = 1 - true_prob_up
    - edge = (1 - true_prob_up) - market_price_dn
    """
    
    if side == "UP":
        edge = true_prob - market_price
    else:  # DN
        edge = (1 - true_prob) - market_price
    
    return edge


def kelly_bet_size(edge, price, bankroll):
    """
    Kelly Criterion for optimal bet sizing.
    
    For a binary bet:
    - Probability of winning: p
    - Odds: (1/price) - 1 (if price is 0.4, odds are 1.5:1)
    - Kelly fraction: f = (p × (b+1) - 1) / b
    
    where b = (1 - price) / price = payout odds
    
    Simplified for binary options where payout is always $1:
    f = (edge) / (1 - price)
    
    We use fractional Kelly (25%) to be conservative.
    """
    
    if edge <= 0 or price <= 0 or price >= 1:
        return 0
    
    # Full Kelly
    kelly_fraction = edge / (1 - price)
    
    # Use 25% Kelly (more conservative, smoother equity curve)
    conservative_kelly = kelly_fraction * 0.25
    
    # Apply our limits
    bet_fraction = min(conservative_kelly, MAX_BET_FRACTION)
    bet_amount = bankroll * bet_fraction
    
    # Clamp to min/max
    bet_amount = max(MIN_BET, min(MAX_BET, bet_amount))
    
    return bet_amount


def calculate_realtime_volatility():
    """
    Calculate realized volatility from recent price data.
    Returns annualized volatility, which we convert to 15-min.
    """
    if len(btc_prices) < 100:
        return BTC_15MIN_VOLATILITY
    
    # Calculate returns
    prices = list(btc_prices)
    returns = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            ret = (prices[i] - prices[i-1]) / prices[i-1]
            returns.append(ret)
    
    if len(returns) < 50:
        return BTC_15MIN_VOLATILITY
    
    # Standard deviation of returns
    mean_ret = sum(returns) / len(returns)
    variance = sum((r - mean_ret) ** 2 for r in returns) / len(returns)
    std_dev = math.sqrt(variance)
    
    # Scale to 15-minute (assuming ~2 ticks per second, 1800 ticks in 15 min)
    # This is approximate
    ticks_per_15min = 1800
    vol_15min = std_dev * math.sqrt(ticks_per_15min / len(returns) * len(btc_prices))
    
    # Clamp to reasonable range
    vol_15min = max(0.001, min(0.005, vol_15min))
    
    return vol_15min


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
    global position, pending_order, candle_open_btc, last_trade_time
    cancel_all()
    position = None
    pending_order = None
    candle_open_btc = None
    last_trade_time = 0


async def main():
    global SLUG, tokens, candle_start_time, candle_open_btc
    global position, pending_order, last_trade_time
    global session_pnl, trades_won, trades_lost, trades_skipped
    
    print("=" * 60)
    print("QUANT BOT v54 - PROBABILITY-BASED TRADING")
    print(f"Min Edge: {MIN_EDGE*100:.0f}% | Kelly Sizing | No Guessing")
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
                    
                    # Track BTC prices for volatility
                    btc_prices.append(btc_now)
                    
                    # Set candle open
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        log(f"Candle open: ${candle_open_btc:,.2f}")
                    
                    # Check for new market
                    if tick_count % 100 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if old_slug and old_slug != SLUG:
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
                    
                    # === CALCULATE TRUE PROBABILITY ===
                    true_prob_up = calculate_true_probability(
                        btc_now, 
                        candle_open_btc, 
                        seconds_left
                    )
                    true_prob_dn = 1 - true_prob_up
                    
                    # === CALCULATE EDGES ===
                    edge_up = calculate_edge(true_prob_up, up_price, "UP")
                    edge_dn = calculate_edge(true_prob_up, dn_price, "DN")
                    
                    # BTC change
                    btc_change = ((btc_now - candle_open_btc) / candle_open_btc) * 100 if candle_open_btc else 0
                    
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
                            print(f"⏳ Waiting for fill... {pending_order['side']} @ {pending_order['price']*100:.0f}¢ ({wait}s)", end='\r')
                        continue
                    
                    # === STATE: HAVE POSITION ===
                    if position:
                        current_price = up_price if position["side"] == "UP" else dn_price
                        profit = current_price - position["avg_price"]
                        unrealized = profit * position["shares"]
                        hold_time = now - position.get("entry_time", now)
                        
                        # Show position with probability context
                        our_prob = true_prob_up if position["side"] == "UP" else true_prob_dn
                        emoji = "📈" if profit > 0 else "📉"
                        print(f"{emoji} {position['side']} | Cost:{position['avg_price']*100:.0f}¢ Now:{current_price*100:.0f}¢ | P/L:${unrealized:+.2f} | True:{our_prob*100:.0f}% | {minutes_left:.1f}m | {hold_time:.0f}s", end='\r')
                        
                        should_sell = False
                        reason = ""
                        
                        # Take profit (can trigger anytime)
                        if profit >= TAKE_PROFIT:
                            should_sell = True
                            reason = "💰 TAKE PROFIT"
                        
                        # Stop loss (only after 30 seconds hold)
                        elif profit <= -STOP_LOSS and hold_time >= 30:
                            should_sell = True
                            reason = "🛑 STOP LOSS"
                        
                        # Time exit
                        elif minutes_left < 1.0:
                            should_sell = True
                            reason = "⏰ TIME EXIT"
                        
                        # Edge disappeared (probability shifted against us) - only after 20s
                        elif our_prob < 0.35 and hold_time >= 20:
                            should_sell = True
                            reason = "📉 EDGE GONE"
                        
                        if should_sell:
                            log(f"{reason}")
                            
                            # Wait for shares to settle on blockchain
                            await asyncio.sleep(3)
                            
                            # Re-set allowance to sell our shares
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
                                    log(f"P/L: ${pnl:+.2f} | Session: ${session_pnl:+.2f} ({trades_won}W/{trades_lost}L/{trades_skipped}skip)")
                                    last_trade_time = time.time()  # Start cooldown
                                else:
                                    # Try again with lower price
                                    cancel_all()
                                    log("⚠️ Sell didn't fill, trying lower price...")
                                    await asyncio.sleep(1)
                                    lower_price = max(current_price - 0.05, 0.01)
                                    order_id2 = place_sell_order(
                                        position["token_id"],
                                        position["shares"],
                                        lower_price,
                                        position["side"]
                                    )
                                    if order_id2:
                                        await asyncio.sleep(2)
                                        filled2, _ = check_order_filled(order_id2)
                                        if filled2 > 0:
                                            pnl = (lower_price * filled2) - position["cost"]
                                            session_pnl += pnl
                                            if pnl > 0:
                                                trades_won += 1
                                            else:
                                                trades_lost += 1
                                            log(f"P/L: ${pnl:+.2f} | Session: ${session_pnl:+.2f}")
                                            last_trade_time = time.time()
                            
                            position = None
                        continue
                    
                    # === STATE: LOOKING FOR EDGE ===
                    if not position and not pending_order:
                        
                        # Check cooldown
                        time_since_trade = now - last_trade_time
                        if last_trade_time > 0 and time_since_trade < COOLDOWN_AFTER_TRADE:
                            cd_left = int(COOLDOWN_AFTER_TRADE - time_since_trade)
                            print(f"⏳ Cooldown: {cd_left}s | UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m", end='\r')
                            continue
                        
                        # Don't trade too late
                        if minutes_left < MIN_TIME_LEFT:
                            print(f"⏳ UP:{up_price*100:.0f}¢ DN:{dn_price*100:.0f}¢ | True UP:{true_prob_up*100:.0f}% | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m | Too late", end='\r')
                            continue
                        
                        # Find best edge (with price range filter)
                        best_side = None
                        best_edge = 0
                        best_price = 0
                        best_token = None
                        
                        # Only consider UP if price is in safe range
                        if edge_up > edge_dn and edge_up >= MIN_EDGE:
                            if MIN_PRICE <= up_price <= MAX_PRICE:
                                best_side = "UP"
                                best_edge = edge_up
                                best_price = up_price
                                best_token = tokens["up_t"]
                        
                        # Only consider DN if price is in safe range
                        elif edge_dn > edge_up and edge_dn >= MIN_EDGE:
                            if MIN_PRICE <= dn_price <= MAX_PRICE:
                                best_side = "DN"
                                best_edge = edge_dn
                                best_price = dn_price
                                best_token = tokens["dn_t"]
                        
                        if best_side:
                            # Calculate Kelly bet size
                            bet_amount = kelly_bet_size(best_edge, best_price, BANKROLL)
                            
                            # Add 1¢ to ensure fill
                            fill_price = min(best_price + 0.01, 0.60)
                            
                            log(f"🎯 EDGE FOUND: {best_side}")
                            log(f"   Market: {best_price*100:.0f}¢ | True: {(true_prob_up if best_side=='UP' else true_prob_dn)*100:.0f}%")
                            log(f"   Edge: {best_edge*100:.1f}% | Kelly bet: ${bet_amount:.2f}")
                            
                            place_buy_order(best_token, fill_price, bet_amount, best_side)
                        
                        else:
                            # No edge - display analysis
                            trades_skipped += 1
                            
                            # Color code the display
                            up_diff = (true_prob_up - up_price) * 100
                            dn_diff = (true_prob_dn - dn_price) * 100
                            
                            print(f"👀 UP:{up_price*100:.0f}¢(true:{true_prob_up*100:.0f}% {up_diff:+.0f}) DN:{dn_price*100:.0f}¢(true:{true_prob_dn*100:.0f}% {dn_diff:+.0f}) | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m | No edge", end='\r')
                    
                    await asyncio.sleep(0.1)
                    
        except websockets.exceptions.ConnectionClosed:
            log("Reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            log(f"Error: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
