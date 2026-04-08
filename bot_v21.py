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

# === SETTINGS ===
TRADE_AMOUNT = 2
MIN_EDGE = 0.12          # Need 12c edge minimum to enter
MAX_PRICE = 0.50         # Don't buy if price > 50c (edge trades only - buy cheap!)
PROFIT_TARGET = 0.05     # Take profit at +5c
STOP_LOSS = 0.06         # Stop loss at -6c

# === LAG TRADING SETTINGS ===
LAG_BTC_SPIKE = 0.03     # BTC must move 0.03% in short time to trigger
LAG_LOOKBACK = 3         # Check last 3 seconds of prices
LAG_PROFIT_TARGET = 0.02 # Take quick profit at +2c
LAG_MAX_PRICE = 0.60     # Can buy up to 60c for lag trades (faster exit)

# === SURE THING SETTINGS ===
SURE_THING_BTC_MOVE = 0.08    # BTC must move at least 0.08% to trigger (was 0.10)
SURE_THING_MIN_PROFIT = 0.05  # Need at least 5c profit to enter
SURE_THING_MAX_PRICE = 0.92   # Don't buy above 92c (still want 8c profit minimum)
SURE_THING_PRICE_TRIGGER = 0.80  # If price is ≥80c, market has decided - but need BTC confirmation!
SURE_THING_PRICE_MIN_BTC = 0.03  # For price-based trigger, BTC must move at least 0.03%

# === ARBITRAGE SETTINGS ===
ARB_MAX_COMBINED = 0.00       # DISABLED - slippage makes this unprofitable
ARB_AMOUNT = 2                # Amount to spend on EACH side for arbitrage

# === MOMENTUM SETTINGS ===
MIN_MOMENTUM = 0.02      # BTC must move at least 0.02% from candle open
MOMENTUM_SAMPLES = 5     # Track last 5 prices to confirm direction

position = None
position_is_sure_thing = False  # Track if this is a Sure Thing (hold to expiry!)
position_is_lag_trade = False   # Track if this is a Lag trade (quick exit!)
has_traded_this_candle = False  # Only ONE trade per candle!
edge_trades_up = 0              # Count UP edge trades (max 2 per candle)
edge_trades_dn = 0              # Count DN edge trades (max 2 per candle)
MAX_EDGE_TRADES_PER_SIDE = 2    # Limit edge trades per side per candle
SLUG = None
tokens = None
candle_open = None
last_candle_time = None

session_pnl = 0.0
trade_count = 0
win_count = 0

# Track recent prices to confirm momentum direction
recent_prices = deque(maxlen=MOMENTUM_SAMPLES)

# Track BTC prices for lag detection (with timestamps)
lag_prices = deque(maxlen=30)  # Last 30 ticks for lag detection


def get_momentum(btc_now):
    """
    Returns momentum direction based on:
    1. Current price vs candle open (are we up or down?)
    2. Recent price action (are we still moving that direction?)
    
    Returns: 'UP', 'DOWN', or None (no clear momentum)
    """
    if candle_open is None or candle_open == 0:
        return None
    
    # How far from candle open?
    change_from_open = ((btc_now - candle_open) / candle_open) * 100
    
    # Not enough movement yet
    if abs(change_from_open) < MIN_MOMENTUM:
        return None
    
    # Check if we're still moving in that direction (not reversing)
    if len(recent_prices) >= 3:
        # Compare current to average of last few prices
        avg_recent = sum(recent_prices) / len(recent_prices)
        
        if change_from_open > 0:  # We're above open
            # Confirm still moving up (current > recent average)
            if btc_now >= avg_recent:
                return 'UP'
            else:
                return None  # Was up but now pulling back
        else:  # We're below open
            # Confirm still moving down (current < recent average)
            if btc_now <= avg_recent:
                return 'DOWN'
            else:
                return None  # Was down but now bouncing
    
    # Not enough samples yet, just use direction from open
    if change_from_open > MIN_MOMENTUM:
        return 'UP'
    elif change_from_open < -MIN_MOMENTUM:
        return 'DOWN'
    
    return None


def detect_lag_opportunity(btc_now):
    """
    Detect if BTC just spiked quickly (lag opportunity).
    Returns: 'UP', 'DOWN', or None
    """
    if len(lag_prices) < 5:
        return None
    
    # Get price from ~3 seconds ago (roughly 5-10 ticks back depending on volume)
    old_price = lag_prices[0]
    
    # Calculate change
    change = ((btc_now - old_price) / old_price) * 100
    
    if change >= LAG_BTC_SPIKE:
        return 'UP'
    elif change <= -LAG_BTC_SPIKE:
        return 'DOWN'
    
    return None


def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900


def find_active_market():
    global SLUG
    
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
                            return True
                        return False
        except Exception as e:
            print(f"Error: {e}")
    
    return False


def get_tokens():
    global tokens
    tokens = None
    
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
    t = get_tokens()
    if not t:
        return False
    
    try:
        params1 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=t["up_t"])
        client.update_balance_allowance(params1)
        params2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=t["dn_t"])
        client.update_balance_allowance(params2)
        print("Allowances set ✓")
        return True
    except Exception as e:
        print(f"Allowance error: {e}")
        return False


def get_prices():
    if not tokens:
        return None
    
    try:
        up = float(client.get_price(tokens["up_t"], "buy").get("price", 0))
        dn = float(client.get_price(tokens["dn_t"], "buy").get("price", 0))
        return {"up": up, "dn": dn, "up_t": tokens["up_t"], "dn_t": tokens["dn_t"]}
    except:
        return None


def get_binance_candle():
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "15m", "limit": 1},
            timeout=5
        )
        data = r.json()[0]
        return data[0], float(data[1])
    except:
        return None, None


def buy(token, price, side):
    global position
    
    try:
        print(f"\n>>> BUYING {side} at ~{price*100:.0f}c")
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token, amount=TRADE_AMOUNT, side=BUY)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success") and resp.get("status") == "matched":
            usdc_spent = float(resp.get("makingAmount", 0))
            shares_received = float(resp.get("takingAmount", 0))
            fill_price = usdc_spent / shares_received if shares_received > 0 else price
            
            position = {
                "token": token, 
                "entry_price": fill_price,
                "size": shares_received, 
                "side": side, 
                "cost": usdc_spent
            }
            print(f"    ✓ BOUGHT {shares_received:.2f} @ {fill_price*100:.0f}c (${usdc_spent:.2f})")
            return True
        else:
            print(f"    ✗ Not filled")
    except Exception as e:
        print(f"    ✗ Error: {e}")
    
    return False


def sell(reason):
    global position, session_pnl, trade_count, win_count, position_is_sure_thing, position_is_lag_trade
    
    if not position:
        return False
    
    try:
        print(f"\n<<< SELLING {position['side']} - {reason}")
        
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=position["token"])
        bal = client.get_balance_allowance(params)
        raw_balance = int(bal.get("balance", 0))
        actual_shares = raw_balance / 1_000_000
        
        if actual_shares <= 0:
            print("    No shares!")
            position = None
            position_is_sure_thing = False
            position_is_lag_trade = False
            return False
        
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=position["token"], amount=actual_shares, side=SELL)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        if resp.get("success") and resp.get("status") == "matched":
            usdc_received = float(resp.get("takingAmount", 0))
            cost = position["cost"]
            pnl = usdc_received - cost
            
            session_pnl += pnl
            trade_count += 1
            if pnl > 0:
                win_count += 1
            
            emoji = "✅" if pnl > 0 else "❌"
            print(f"    {emoji} SOLD @ ${usdc_received:.2f} | P/L: ${pnl:+.2f}")
            print(f"    Session: {win_count}/{trade_count} wins | Total: ${session_pnl:+.2f}")
            
            position = None
            position_is_sure_thing = False
            position_is_lag_trade = False
            return True
        else:
            print(f"    ✗ Sell failed, retrying...")
            return False
            
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False


def check_for_new_candle():
    global last_candle_time, candle_open, recent_prices, has_traded_this_candle, edge_trades_up, edge_trades_dn, position_is_sure_thing, position_is_lag_trade
    
    new_time, new_open = get_binance_candle()
    if new_time is None:
        return False
    
    if new_time != last_candle_time:
        print(f"\n*** NEW CANDLE: ${new_open:,.2f} ***\n")
        last_candle_time = new_time
        candle_open = new_open
        recent_prices.clear()
        has_traded_this_candle = False  # Reset for new candle!
        edge_trades_up = 0              # Reset UP edge counter
        edge_trades_dn = 0              # Reset DN edge counter
        
        # If holding a Sure Thing from previous candle, convert to regular position
        # so it can take profit/stop loss in the new candle
        if position_is_sure_thing:
            print("    📝 Sure Thing → converted to regular HOLD (new candle)")
            position_is_sure_thing = False
        if position_is_lag_trade:
            position_is_lag_trade = False
        
        return True
    return False


def check_for_new_market():
    global position, recent_prices, position_is_sure_thing, has_traded_this_candle, edge_trades_up, edge_trades_dn
    
    is_new = find_active_market()
    if is_new:
        if position:
            sell("MARKET CHANGE")
        get_tokens()
        set_allowances()
        recent_prices.clear()
        position_is_sure_thing = False  # Reset for new market
        has_traded_this_candle = False  # Reset for new market
        edge_trades_up = 0              # Reset for new market
        edge_trades_dn = 0              # Reset for new market
        return True
    return False


async def main():
    global position, candle_open, tokens, last_candle_time, recent_prices, position_is_sure_thing, position_is_lag_trade, has_traded_this_candle, edge_trades_up, edge_trades_dn, lag_prices
    
    print("=" * 60)
    print("  BOT v21 - SURE THING RESETS ON NEW CANDLE")
    print("=" * 60)
    print(f"EDGE: {MIN_EDGE*100:.0f}c min | ≤{MAX_PRICE*100:.0f}c | TP +{PROFIT_TARGET*100:.0f}c | 2 UP + 2 DN")
    print(f"LAG:  {LAG_BTC_SPIKE}% spike | ≤{LAG_MAX_PRICE*100:.0f}c | TP +{LAG_PROFIT_TARGET*100:.0f}c | unlimited")
    print(f"SURE THING: BTC ≥{SURE_THING_BTC_MOVE}% | hold to expiry (resets on new candle)")
    print("=" * 60)
    
    find_active_market()
    if not SLUG:
        print("ERROR: No active market!")
        return
    
    get_tokens()
    set_allowances()
    last_candle_time, candle_open = get_binance_candle()
    
    print(f"Market: {SLUG}")
    print(f"Candle: ${candle_open:,.2f}\n")
    
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
                    
                    # Track recent prices for momentum confirmation
                    recent_prices.append(btc_now)
                    
                    # Track prices for lag detection
                    lag_prices.append(btc_now)
                    
                    # Get momentum direction
                    momentum = get_momentum(btc_now)
                    
                    # Detect lag opportunity (BTC spiked quickly)
                    lag_signal = detect_lag_opportunity(btc_now)
                    
                    if tick_count % 50 == 0:
                        if check_for_new_candle():
                            check_for_new_market()
                    
                    if tick_count % 100 == 0:
                        check_for_new_market()
                    
                    if not tokens:
                        continue
                    
                    m = get_prices()
                    if not m or m["up"] == 0:
                        continue
                    
                    if candle_open is None or candle_open == 0:
                        continue
                    
                    btc_change = ((btc_now - candle_open) / candle_open) * 100
                    
                    # Calculate fair value and edge for both sides
                    up_fair = min(0.85, 0.50 + btc_change * 5)
                    dn_fair = min(0.85, 0.50 - btc_change * 5)
                    
                    up_edge = up_fair - m["up"]
                    dn_edge = dn_fair - m["dn"]
                    
                    # Momentum indicator for display
                    if momentum == 'UP':
                        mom_str = "🟢UP"
                    elif momentum == 'DOWN':
                        mom_str = "🔴DN"
                    else:
                        mom_str = "⚪FLAT"
                    
                    # HOLDING POSITION
                    if position:
                        curr = m["up"] if position["side"] == "UP" or position["side"] == "UP-ARB" else m["dn"]
                        price_diff = curr - position["entry_price"]
                        est_pnl = price_diff * position["size"]
                        
                        # Show position type
                        if position_is_sure_thing:
                            hold_type = "💰SURE"
                        elif position_is_lag_trade:
                            hold_type = "⚡LAG"
                        else:
                            hold_type = "HOLD"
                        print(f"{hold_type} {position['side']} | {position['entry_price']*100:.0f}c→{curr*100:.0f}c ({price_diff*100:+.0f}c) | ~${est_pnl:+.2f} | BTC:{btc_change:+.2f}% {mom_str}")
                        
                        # Exit conditions based on position type
                        if position_is_sure_thing:
                            # Sure Thing = hold to expiry, no early exit
                            pass
                        elif position_is_lag_trade:
                            # Lag trade = quick exit at +2c or stop at -4c
                            if price_diff >= LAG_PROFIT_TARGET:
                                sell(f"LAG PROFIT +{price_diff*100:.0f}c")
                            elif price_diff <= -0.04:  # Tighter stop for lag trades
                                sell(f"LAG STOP -{abs(price_diff)*100:.0f}c")
                        else:
                            # Regular edge trade
                            if price_diff >= PROFIT_TARGET:
                                sell(f"PROFIT +{price_diff*100:.0f}c")
                            elif price_diff <= -STOP_LOSS:
                                sell(f"STOP -{abs(price_diff)*100:.0f}c")
                    
                    # LOOKING FOR ENTRY
                    else:
                        # Find which side has better edge
                        if up_edge > dn_edge:
                            best_side = "UP"
                            best_edge = up_edge
                            best_price = m["up"]
                            best_token = m["up_t"]
                        else:
                            best_side = "DN"
                            best_edge = dn_edge
                            best_price = m["dn"]
                            best_token = m["dn_t"]
                        
                        print(f"BTC:{btc_change:+.2f}% {mom_str} | UP:{m['up']*100:.0f}c({up_edge*100:+.0f}c) DN:{m['dn']*100:.0f}c({dn_edge*100:+.0f}c) | Best:{best_side} +{best_edge*100:.0f}c")
                        
                        # === SURE THING MODE (can trigger even if already traded!) ===
                        # When BTC has moved significantly OR price is very high,
                        # buy the winning side for guaranteed profit
                        # IMPORTANT: Price trigger (≥80c) now requires BTC confirmation!
                        
                        sure_thing = False
                        sure_side = None
                        sure_token = None
                        sure_price = None
                        
                        # Method 1: BTC clearly DOWN → buy Down
                        if btc_change <= -SURE_THING_BTC_MOVE and m["dn"] <= SURE_THING_MAX_PRICE:
                            sure_thing = True
                            sure_side = "DN"
                            sure_token = m["dn_t"]
                            sure_price = m["dn"]
                        
                        # Method 1: BTC clearly UP → buy Up
                        elif btc_change >= SURE_THING_BTC_MOVE and m["up"] <= SURE_THING_MAX_PRICE:
                            sure_thing = True
                            sure_side = "UP"
                            sure_token = m["up_t"]
                            sure_price = m["up"]
                        
                        # Method 2: Price is very high (≥80c) BUT only if BTC confirms direction!
                        elif m["up"] >= SURE_THING_PRICE_TRIGGER and m["up"] <= SURE_THING_MAX_PRICE:
                            if btc_change >= SURE_THING_PRICE_MIN_BTC:
                                sure_thing = True
                                sure_side = "UP"
                                sure_token = m["up_t"]
                                sure_price = m["up"]
                        
                        elif m["dn"] >= SURE_THING_PRICE_TRIGGER and m["dn"] <= SURE_THING_MAX_PRICE:
                            if btc_change <= -SURE_THING_PRICE_MIN_BTC:
                                sure_thing = True
                                sure_side = "DN"
                                sure_token = m["dn_t"]
                                sure_price = m["dn"]
                        
                        # Execute Sure Thing (even if already traded this candle!)
                        if sure_thing and (1 - sure_price) >= SURE_THING_MIN_PROFIT and not position:
                            expected_profit = (1 - sure_price) * 100
                            print(f"*** 💰 SURE THING: BTC {btc_change:+.2f}% → {sure_side} @ {sure_price*100:.0f}c = ~{expected_profit:.0f}c profit ***")
                            if buy(sure_token, sure_price, sure_side):
                                position_is_sure_thing = True
                            continue
                        
                        # === LAG TRADING MODE ===
                        # When BTC spikes quickly, buy before Polymarket catches up
                        if lag_signal and not position:
                            if lag_signal == 'UP' and m["up"] <= LAG_MAX_PRICE:
                                print(f"*** ⚡ LAG TRADE: BTC spiked UP → buying UP @ {m['up']*100:.0f}c ***")
                                if buy(m["up_t"], m["up"], "UP"):
                                    position_is_lag_trade = True
                                continue
                            elif lag_signal == 'DOWN' and m["dn"] <= LAG_MAX_PRICE:
                                print(f"*** ⚡ LAG TRADE: BTC dropped DOWN → buying DN @ {m['dn']*100:.0f}c ***")
                                if buy(m["dn_t"], m["dn"], "DN"):
                                    position_is_lag_trade = True
                                continue
                        
                        # === ALREADY TRADED THIS CANDLE? ===
                        if has_traded_this_candle:
                            continue  # Wait for next candle
                        
                        # === ARBITRAGE MODE ===
                        # If UP + DOWN < 97c, buy BOTH for guaranteed profit!
                        combined_price = m["up"] + m["dn"]
                        if combined_price <= ARB_MAX_COMBINED:
                            guaranteed_profit = (1 - combined_price) * 100
                            print(f"*** 🎰 ARBITRAGE: UP {m['up']*100:.0f}c + DN {m['dn']*100:.0f}c = {combined_price*100:.0f}c → {guaranteed_profit:.0f}c FREE MONEY! ***")
                            
                            # Buy UP
                            buy(m["up_t"], m["up"], "UP-ARB")
                            
                            # Small delay then buy DOWN
                            await asyncio.sleep(0.5)
                            
                            # Buy DOWN (need to clear position first for this)
                            opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
                            mo = MarketOrderArgs(token_id=m["dn_t"], amount=ARB_AMOUNT, side=BUY)
                            order = client.create_market_order(mo, opt)
                            resp = client.post_order(order, OrderType.FOK)
                            if resp.get("success"):
                                print(f"    ✓ Also bought DN for arbitrage")
                            
                            # Mark as traded and Sure Thing (hold to expiry!)
                            has_traded_this_candle = True
                            position_is_sure_thing = True
                            position = None
                            continue
                        
                        # === MOMENTUM FILTER (original logic) ===
                        # Only trade if momentum confirms our direction
                        
                        can_trade = False
                        skip_reason = None
                        
                        if momentum is None:
                            skip_reason = "NO MOMENTUM (flat/choppy)"
                        elif best_side == "UP" and momentum == "UP":
                            can_trade = True
                        elif best_side == "DN" and momentum == "DOWN":
                            can_trade = True
                        elif best_side == "UP" and momentum == "DOWN":
                            skip_reason = "UP signal but momentum DOWN"
                        elif best_side == "DN" and momentum == "UP":
                            skip_reason = "DN signal but momentum UP"
                        
                        # Check per-side edge trade limit
                        side_trades = edge_trades_up if best_side == "UP" else edge_trades_dn
                        side_maxed = side_trades >= MAX_EDGE_TRADES_PER_SIDE
                        
                        # Entry: edge good + price cheap + momentum confirms + side not maxed
                        if best_edge >= MIN_EDGE and best_price <= MAX_PRICE:
                            if side_maxed:
                                print(f"    ⏸ SKIP: Already {side_trades} {best_side} edge trades (max {MAX_EDGE_TRADES_PER_SIDE})")
                            elif can_trade:
                                print(f"*** {best_side} SIGNAL: +{best_edge*100:.0f}c edge @ {best_price*100:.0f}c | MOMENTUM ✓ | {best_side} #{side_trades+1} ***")
                                if buy(best_token, best_price, best_side):
                                    if best_side == "UP":
                                        edge_trades_up += 1
                                    else:
                                        edge_trades_dn += 1
                                # Note: Edge trades do NOT set has_traded_this_candle
                                # This allows multiple edge scalps per candle
                                # Only Sure Thing locks the candle
                            else:
                                print(f"    ⏸ SKIP: {skip_reason}")
                        elif best_edge >= MIN_EDGE and best_price > MAX_PRICE:
                            print(f"    ⏸ SKIP: Price {best_price*100:.0f}c > max {MAX_PRICE*100:.0f}c (wait for cheaper entry)")
        
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
