import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
import os
import time
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

client = ClobClient(
    host="https://clob.polymarket.com", 
    key=os.getenv("POLYMARKET_PRIVATE_KEY"), 
    chain_id=137, 
    signature_type=2, 
    funder=os.getenv("POLYMARKET_FUNDER")
)
client.set_api_creds(client.create_or_derive_api_creds())

# === GABAGOOL STRATEGY v41 (DOUBLE SIZE EDITION) ===
# Exact copy of v40 but with $4 hedges instead of $2
# 1. Try to hedge if both sides 40-55c
# 2. Double down on winner when ≥70c (if hedged)
# 3. OR just buy winner directly at ≥70c if momentum is clear!

TOTAL_BUDGET = 16             # Total $ to spend per candle (doubled)
FIRST_BUY = 4                 # First hedge buy (was 2)
HEDGE_BUY = 4                 # Second hedge buy (was 2)
WINNER_BUY = 6                # Buy winner at 70c+ (was 3)
MAX_COMBINED_AVG = 0.95       # Combined must be < 95c for hedge
MAX_COMBINED_ENTRY = 0.96     # Only ENTER hedge if combined ≤ 96c
MIN_PRICE = 0.40              # Min price for hedge entry
MAX_PRICE = 0.55              # Max price for hedge entry
WINNER_THRESHOLD = 0.70       # Buy/double down when price hits 70c+
MAX_WINNER_PRICE = 0.85       # Don't buy winner above 85c (no margin)
MOMENTUM_THRESHOLD = 0.03     # BTC must be > 0.03% or < -0.03%

SLUG = None
tokens = None
candle_open = None
candle_start_time = None
last_candle_time = None

# Track positions
up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}

# Track actions
doubled_down = False
bought_winner_direct = False

session_pnl = 0.0
candles_traded = 0
candles_skipped = 0
hedged_trades = 0
doubledown_trades = 0
momentum_trades = 0


def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900


def get_candle_minutes_elapsed():
    if not candle_start_time:
        return 0
    elapsed = time.time() - candle_start_time
    return elapsed / 60


def get_minutes_remaining():
    return 15 - get_candle_minutes_elapsed()


def get_momentum(btc_change):
    if btc_change >= MOMENTUM_THRESHOLD:
        return 'UP'
    elif btc_change <= -MOMENTUM_THRESHOLD:
        return 'DOWN'
    else:
        return 'CHOPPY'


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
    
    up_t = tokens.get("up_t")
    dn_t = tokens.get("dn_t")
    
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


def buy_with_retry(token_id, amount, price, side, retries=3):
    global up_position, dn_position
    
    for attempt in range(retries):
        try:
            opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
            mo = MarketOrderArgs(token_id=token_id, amount=amount, side=BUY)
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
                    total_size = amount / price
                    total_cost = amount
                
                if side == "UP":
                    new_shares = up_position["shares"] + total_size
                    new_cost = up_position["cost"] + total_cost
                    up_position = {
                        "shares": new_shares,
                        "cost": new_cost,
                        "avg_price": new_cost / new_shares if new_shares > 0 else 0,
                        "token_id": token_id
                    }
                    print(f"    ✓ BOUGHT {total_size:.2f} UP @ {avg_price*100:.0f}c (${total_cost:.2f})")
                else:
                    new_shares = dn_position["shares"] + total_size
                    new_cost = dn_position["cost"] + total_cost
                    dn_position = {
                        "shares": new_shares,
                        "cost": new_cost,
                        "avg_price": new_cost / new_shares if new_shares > 0 else 0,
                        "token_id": token_id
                    }
                    print(f"    ✓ BOUGHT {total_size:.2f} DN @ {avg_price*100:.0f}c (${total_cost:.2f})")
                
                show_position_status()
                return True
            else:
                err = resp.get("error", resp.get("data", "Unknown"))
                if "fully filled" in str(err) and attempt < retries - 1:
                    print(f"    ⟳ FOK retry {attempt + 1}/{retries}...")
                    time.sleep(0.5)
                    continue
                print(f"    ✗ {err}")
                return False
                
        except Exception as e:
            if attempt < retries - 1:
                print(f"    ⟳ Retry {attempt + 1}/{retries} after error...")
                time.sleep(0.5)
                continue
            print(f"    ✗ {e}")
            return False
    
    return False


def show_position_status():
    up_cost = up_position["cost"]
    dn_cost = dn_position["cost"]
    up_shares = up_position["shares"]
    dn_shares = dn_position["shares"]
    up_avg = up_position["avg_price"]
    dn_avg = dn_position["avg_price"]
    
    print(f"\n    📊 POSITION STATUS:")
    if up_shares > 0:
        print(f"       UP: {up_shares:.2f} shares @ {up_avg*100:.0f}c (${up_cost:.2f})")
    if dn_shares > 0:
        print(f"       DN: {dn_shares:.2f} shares @ {dn_avg*100:.0f}c (${dn_cost:.2f})")
    
    if up_shares > 0 and dn_shares > 0:
        combined_avg = up_avg + dn_avg
        hedged_shares = min(up_shares, dn_shares)
        print(f"       🎯 Combined avg: {combined_avg*100:.0f}c")
        print(f"       🔒 Hedged: {hedged_shares:.2f} shares")
        if up_shares > dn_shares + 0.5:
            print(f"       📈 EXTRA UP: {up_shares - dn_shares:.2f} shares!")
        elif dn_shares > up_shares + 0.5:
            print(f"       📉 EXTRA DN: {dn_shares - up_shares:.2f} shares!")
    elif up_shares > 0:
        print(f"       🎯 MOMENTUM: UP only (need UP to win!)")
    elif dn_shares > 0:
        print(f"       🎯 MOMENTUM: DN only (need DN to win!)")
    print()


def is_hedged():
    return up_position["shares"] > 0 and dn_position["shares"] > 0


def has_position():
    return up_position["shares"] > 0 or dn_position["shares"] > 0


def check_hedge_entry(up_price, dn_price):
    combined = up_price + dn_price
    
    if up_price < MIN_PRICE or up_price > MAX_PRICE:
        return False, f"UP {up_price*100:.0f}c out of range"
    if dn_price < MIN_PRICE or dn_price > MAX_PRICE:
        return False, f"DN {dn_price*100:.0f}c out of range"
    if combined > MAX_COMBINED_ENTRY:
        return False, f"Combined {combined*100:.0f}c > {MAX_COMBINED_ENTRY*100:.0f}c"
    
    return True, f"✓ READY ({combined*100:.0f}c)"


def settle_positions(btc_final_change):
    global session_pnl, candles_traded, candles_skipped
    global hedged_trades, doubledown_trades, momentum_trades
    global up_position, dn_position, doubled_down, bought_winner_direct
    
    up_shares = up_position["shares"]
    dn_shares = dn_position["shares"]
    up_cost = up_position["cost"]
    dn_cost = dn_position["cost"]
    
    # Reset flags for next candle
    doubled_down = False
    bought_winner_direct = False
    
    if up_shares == 0 and dn_shares == 0:
        candles_skipped += 1
        print(f"\n⏭️  Candle skipped. Total skipped: {candles_skipped}")
        return
    
    print(f"\n{'='*50}")
    print(f"*** CANDLE SETTLED - BTC {btc_final_change:+.2f}% ***")
    print(f"{'='*50}")
    
    if btc_final_change >= 0:
        winner = "UP"
        up_payout = up_shares * 1.0
        dn_payout = 0
    else:
        winner = "DN"
        up_payout = 0
        dn_payout = dn_shares * 1.0
    
    total_payout = up_payout + dn_payout
    total_cost = up_cost + dn_cost
    
    fee = total_payout * 0.02
    net_payout = total_payout - fee
    pnl = net_payout - total_cost
    
    was_hedged = up_shares > 0 and dn_shares > 0
    had_extra = abs(up_shares - dn_shares) > 0.5
    was_momentum_only = (up_shares > 0 and dn_shares == 0) or (dn_shares > 0 and up_shares == 0)
    
    print(f"Winner: {winner}")
    print(f"UP: {up_shares:.2f} shares → ${up_payout:.2f}")
    print(f"DN: {dn_shares:.2f} shares → ${dn_payout:.2f}")
    print(f"Total cost: ${total_cost:.2f}")
    print(f"Fee (2%): -${fee:.2f}")
    print(f"Net payout: ${net_payout:.2f}")
    
    if was_hedged and had_extra:
        print(f"🔒📈 HEDGE + DOUBLE DOWN")
        doubledown_trades += 1
    elif was_hedged:
        print(f"🔒 HEDGED")
        hedged_trades += 1
    elif was_momentum_only:
        print(f"🎯 MOMENTUM PLAY")
        momentum_trades += 1
    
    if pnl >= 0:
        print(f"✅ PROFIT: ${pnl:+.2f}")
    else:
        print(f"❌ LOSS: ${pnl:+.2f}")
    
    session_pnl += pnl
    candles_traded += 1
    print(f"\nSession: {candles_traded} trades")
    print(f"  🔒 Hedged: {hedged_trades}")
    print(f"  🔒📈 Double: {doubledown_trades}")
    print(f"  🎯 Momentum: {momentum_trades}")
    print(f"Skipped: {candles_skipped} | Total P/L: ${session_pnl:+.2f}")
    print(f"{'='*50}\n")
    
    up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
    dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}


def get_binance_candle():
    try:
        r = requests.get("https://api.binance.com/api/v3/klines", 
                        params={"symbol": "BTCUSDT", "interval": "15m", "limit": 1})
        k = r.json()[0]
        return k[0], float(k[1])
    except:
        return None, None


async def main():
    global candle_open, tokens, last_candle_time, candle_start_time
    global up_position, dn_position, doubled_down, bought_winner_direct
    
    print("=" * 60)
    print("  🚀 GABAGOOL v41 - DOUBLE SIZE EDITION")
    print("  $4 hedges | $6 double-down | Same perfect logic")
    print("=" * 60)
    print(f"Budget: ${TOTAL_BUDGET}")
    print(f"Hedge buys: ${FIRST_BUY} + ${HEDGE_BUY}")
    print(f"Winner buy: ${WINNER_BUY} when {WINNER_THRESHOLD*100:.0f}c-{MAX_WINNER_PRICE*100:.0f}c")
    print(f"Hedge range: {MIN_PRICE*100:.0f}c - {MAX_PRICE*100:.0f}c")
    print(f"Momentum: ±{MOMENTUM_THRESHOLD*100:.2f}%")
    print("=" * 60)
    
    find_active_market()
    if not SLUG:
        print("ERROR: No active market!")
        return
    
    print(f"\n*** MARKET: {SLUG} ***\n")
    get_tokens()
    set_allowances()
    last_candle_time, candle_open = get_binance_candle()
    candle_start_time = time.time()
    
    print(f"Candle open: ${candle_open:,.2f}\n")
    
    tick_count = 0
    last_btc_change = 0
    
    while True:
        try:
            async with websockets.connect(
                "wss://stream.binance.com:9443/ws/btcusdt@trade",
                ping_interval=20,
                ping_timeout=10
            ) as ws:
                
                async for msg in ws:
                    tick_count += 1
                    
                    if tick_count % 3 != 0:
                        continue
                    
                    d = json.loads(msg)
                    btc_now = float(d['p'])
                    
                    # Check for new candle
                    # Check for new candle (every 30 ticks instead of 50)
                    if tick_count % 30 == 0:
                        new_time, new_open = get_binance_candle()
                        if new_time and new_time != last_candle_time:
                            settle_positions(last_btc_change)
                            
                            print(f"\n*** NEW CANDLE: ${new_open:,.2f} ***\n")
                            last_candle_time = new_time
                            candle_open = new_open
                            candle_start_time = time.time()
                            doubled_down = False
                            bought_winner_direct = False
                        
                        if find_active_market():
                            print(f"\n*** NEW MARKET: {SLUG} ***\n")
                            get_tokens()
                            set_allowances()
                    
                    m = get_market_prices()
                    if not m:
                        continue
                    
                    if candle_open:
                        btc_change = (btc_now - candle_open) / candle_open * 100
                        last_btc_change = btc_change
                    else:
                        btc_change = 0
                    
                    up_price = m["up"]
                    dn_price = m["dn"]
                    combined = up_price + dn_price
                    
                    minutes_left = get_minutes_remaining()
                    momentum = get_momentum(btc_change)
                    
                    up_shares = up_position["shares"]
                    dn_shares = dn_position["shares"]
                    
                    # Status display
                    if is_hedged():
                        up_pnl = (up_price - up_position["avg_price"]) * up_shares
                        dn_pnl = (dn_price - dn_position["avg_price"]) * dn_shares
                        extra = ""
                        if up_shares > dn_shares + 0.5:
                            extra = " 📈+UP"
                        elif dn_shares > up_shares + 0.5:
                            extra = " 📉+DN"
                        print(f"🔒{extra} | UP:{up_shares:.1f}(${up_pnl:+.2f}) DN:{dn_shares:.1f}(${dn_pnl:+.2f}) | BTC:{btc_change:+.2f}% | {minutes_left:.1f}m")
                    elif has_position():
                        if up_shares > 0:
                            pnl = (up_price - up_position["avg_price"]) * up_shares
                            print(f"🎯 UP | {up_shares:.1f}@{up_position['avg_price']*100:.0f}c(${pnl:+.2f}) | BTC:{btc_change:+.2f}% ({momentum}) | {minutes_left:.1f}m")
                        else:
                            pnl = (dn_price - dn_position["avg_price"]) * dn_shares
                            print(f"🎯 DN | {dn_shares:.1f}@{dn_position['avg_price']*100:.0f}c(${pnl:+.2f}) | BTC:{btc_change:+.2f}% ({momentum}) | {minutes_left:.1f}m")
                    else:
                        can_hedge, reason = check_hedge_entry(up_price, dn_price)
                        # Show winner opportunity
                        winner_opp = ""
                        if up_price >= WINNER_THRESHOLD and up_price <= MAX_WINNER_PRICE and momentum == 'UP':
                            winner_opp = f" | 🎯 UP@{up_price*100:.0f}c!"
                        elif dn_price >= WINNER_THRESHOLD and dn_price <= MAX_WINNER_PRICE and momentum == 'DOWN':
                            winner_opp = f" | 🎯 DN@{dn_price*100:.0f}c!"
                        print(f"👀 | UP:{up_price*100:.0f}c DN:{dn_price*100:.0f}c | BTC:{btc_change:+.2f}% ({momentum}) | {minutes_left:.1f}m | {reason}{winner_opp}")
                    
                    # === TRADING LOGIC ===
                    
                    total_spent = up_position["cost"] + dn_position["cost"]
                    remaining = TOTAL_BUDGET - total_spent
                    
                    # === STRATEGY 1: HEDGE ===
                    if not is_hedged() and not bought_winner_direct:
                        
                        # No position - try to enter hedge
                        if not has_position():
                            can_enter, reason = check_hedge_entry(up_price, dn_price)
                            if can_enter:
                                if up_price <= dn_price:
                                    print(f"\n*** HEDGE ENTRY: UP @ {up_price*100:.0f}c ***")
                                    buy_with_retry(m["up_t"], FIRST_BUY, up_price, "UP")
                                else:
                                    print(f"\n*** HEDGE ENTRY: DN @ {dn_price*100:.0f}c ***")
                                    buy_with_retry(m["dn_t"], FIRST_BUY, dn_price, "DN")
                        
                        # Have UP - try to complete hedge
                        elif up_shares > 0 and dn_shares == 0:
                            combined_check = up_position["avg_price"] + dn_price
                            if combined_check < MAX_COMBINED_AVG and remaining >= 1:
                                amount = min(up_shares * dn_price, remaining, HEDGE_BUY)
                                if amount >= 1:
                                    print(f"\n*** COMPLETING HEDGE: DN @ {dn_price*100:.0f}c (combined {combined_check*100:.0f}c) ***")
                                    buy_with_retry(m["dn_t"], amount, dn_price, "DN")
                        
                        # Have DN - try to complete hedge
                        elif dn_shares > 0 and up_shares == 0:
                            combined_check = up_price + dn_position["avg_price"]
                            if combined_check < MAX_COMBINED_AVG and remaining >= 1:
                                amount = min(dn_shares * up_price, remaining, HEDGE_BUY)
                                if amount >= 1:
                                    print(f"\n*** COMPLETING HEDGE: UP @ {up_price*100:.0f}c (combined {combined_check*100:.0f}c) ***")
                                    buy_with_retry(m["up_t"], amount, up_price, "UP")
                    
                    # === STRATEGY 2: DOUBLE DOWN (if hedged) ===
                    if is_hedged() and not doubled_down and remaining >= 1:
                        
                        if up_price >= WINNER_THRESHOLD and up_price <= MAX_WINNER_PRICE:
                            buy_amount = min(WINNER_BUY, remaining)
                            print(f"\n*** 🚀 DOUBLE DOWN: UP @ {up_price*100:.0f}c (winner emerging!) ***")
                            if buy_with_retry(m["up_t"], buy_amount, up_price, "UP"):
                                doubled_down = True
                        
                        elif dn_price >= WINNER_THRESHOLD and dn_price <= MAX_WINNER_PRICE:
                            buy_amount = min(WINNER_BUY, remaining)
                            print(f"\n*** 🚀 DOUBLE DOWN: DN @ {dn_price*100:.0f}c (winner emerging!) ***")
                            if buy_with_retry(m["dn_t"], buy_amount, dn_price, "DN"):
                                doubled_down = True
                    
                    # === STRATEGY 3: BUY WINNER DIRECTLY (if no hedge possible) ===
                    if not has_position() and not bought_winner_direct and remaining >= WINNER_BUY:
                        
                        # Can't hedge (prices out of range), but clear momentum + good winner price
                        can_hedge, _ = check_hedge_entry(up_price, dn_price)
                        
                        if not can_hedge:
                            # UP is winning + momentum confirms + good price
                            if (up_price >= WINNER_THRESHOLD and 
                                up_price <= MAX_WINNER_PRICE and 
                                momentum == 'UP'):
                                print(f"\n*** 🎯 MOMENTUM BUY: UP @ {up_price*100:.0f}c (BTC {btc_change:+.2f}%, no hedge possible) ***")
                                if buy_with_retry(m["up_t"], WINNER_BUY, up_price, "UP"):
                                    bought_winner_direct = True
                            
                            # DN is winning + momentum confirms + good price
                            elif (dn_price >= WINNER_THRESHOLD and 
                                  dn_price <= MAX_WINNER_PRICE and 
                                  momentum == 'DOWN'):
                                print(f"\n*** 🎯 MOMENTUM BUY: DN @ {dn_price*100:.0f}c (BTC {btc_change:+.2f}%, no hedge possible) ***")
                                if buy_with_retry(m["dn_t"], WINNER_BUY, dn_price, "DN"):
                                    bought_winner_direct = True
                        
        except websockets.exceptions.ConnectionClosed:
            print("\nWebsocket closed, reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
