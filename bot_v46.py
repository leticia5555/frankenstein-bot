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

# === GABAGOOL STRATEGY v46 (CHAINLINK + FIXED TIMING) ===
# v45 Chainlink feed (matches oracle!)
# + Fixed minutes remaining calculation
# 
# KEY CHANGES:
# 1. Uses Chainlink BTC/USD via Polymarket RTDS (same as oracle)
# 2. Minutes remaining calculated from market timestamp, not local time

TOTAL_BUDGET = 16
FIRST_BUY = 4
HEDGE_BUY = 4
WINNER_BUY = 6
MAX_COMBINED_AVG = 0.95
MAX_COMBINED_ENTRY = 0.96
MIN_PRICE = 0.40
MAX_PRICE = 0.55
WINNER_THRESHOLD = 0.70
MAX_WINNER_PRICE = 0.85
MOMENTUM_THRESHOLD = 0.08

SLUG = None
tokens = None
candle_open = None
candle_start_time = None  # Now stores market's Unix timestamp
last_candle_time = None

up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}

doubled_down = False
bought_winner_direct = False

session_pnl = 0.0
candles_traded = 0
candles_skipped = 0


def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900


def get_minutes_remaining():
    """Calculate minutes remaining based on market timestamp, not local time."""
    if not candle_start_time:
        return 15
    # Market ends 900 seconds (15 min) after candle_start_time
    market_end = candle_start_time + 900
    now = time.time()
    remaining = (market_end - now) / 60
    return max(0, remaining)


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
                            # Use the timestamp from the slug as candle_start_time
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
                    time.sleep(0.3)
                    continue
                print(f"    ✗ {err}")
                return False
                
        except Exception as e:
            if attempt < retries - 1:
                print(f"    ⟳ Retry {attempt + 1}/{retries} after error...")
                time.sleep(0.3)
            else:
                print(f"    ✗ Error: {e}")
    
    return False


def show_position_status():
    up_s = up_position["shares"]
    dn_s = dn_position["shares"]
    up_c = up_position["cost"]
    dn_c = dn_position["cost"]
    up_a = up_position["avg_price"]
    dn_a = dn_position["avg_price"]
    
    if up_s > 0 and dn_s > 0:
        combined = up_a + dn_a
        print(f"    📊 HEDGED: UP {up_s:.1f}@{up_a*100:.0f}c + DN {dn_s:.1f}@{dn_a*100:.0f}c = {combined*100:.0f}c (${up_c + dn_c:.2f})")
    elif up_s > 0:
        print(f"    📊 POSITION: UP {up_s:.1f}@{up_a*100:.0f}c (${up_c:.2f})")
    elif dn_s > 0:
        print(f"    📊 POSITION: DN {dn_s:.1f}@{dn_a*100:.0f}c (${dn_c:.2f})")


def has_position():
    return up_position["shares"] > 0 or dn_position["shares"] > 0


def is_hedged():
    return up_position["shares"] > 0 and dn_position["shares"] > 0


def check_hedge_entry(up_price, dn_price):
    combined = up_price + dn_price
    
    if combined > MAX_COMBINED_ENTRY:
        return False, f"Combined {combined*100:.0f}c > {MAX_COMBINED_ENTRY*100:.0f}c"
    
    if up_price < MIN_PRICE or up_price > MAX_PRICE:
        return False, f"UP {up_price*100:.0f}c out of range"
    
    if dn_price < MIN_PRICE or dn_price > MAX_PRICE:
        return False, f"DN {dn_price*100:.0f}c out of range"
    
    return True, f"✓ READY ({combined*100:.0f}c)"


def reset_positions():
    global up_position, dn_position
    up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
    dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}


def settle_positions(btc_change):
    global session_pnl, candles_traded, candles_skipped
    
    if not has_position():
        candles_skipped += 1
        print(f"\n📊 Candle settled - NO POSITION")
        return
    
    up_s = up_position["shares"]
    dn_s = dn_position["shares"]
    up_c = up_position["cost"]
    dn_c = dn_position["cost"]
    total_cost = up_c + dn_c
    
    # Determine winner based on BTC change (from Chainlink!)
    if btc_change >= 0:
        payout = up_s * 1.0
        winner = "UP"
    else:
        payout = dn_s * 1.0
        winner = "DN"
    
    pnl = payout - total_cost
    session_pnl += pnl
    candles_traded += 1
    
    print(f"\n{'='*50}")
    print(f"📊 CANDLE SETTLED - {winner} WINS (BTC {btc_change:+.4f}%)")
    print(f"   UP: {up_s:.1f} shares (${up_c:.2f})")
    print(f"   DN: {dn_s:.1f} shares (${dn_c:.2f})")
    print(f"   Payout: ${payout:.2f}")
    print(f"   P/L: ${pnl:+.2f}")
    print(f"   Session: ${session_pnl:+.2f} ({candles_traded}T/{candles_skipped}S)")
    print(f"{'='*50}\n")
    
    reset_positions()


async def main():
    global SLUG, tokens, candle_open, candle_start_time, last_candle_time
    global doubled_down, bought_winner_direct
    
    print("=" * 50)
    print("GABAGOOL v46 - CHAINLINK + FIXED TIMING")
    print("Uses oracle's price source + correct minutes")
    print("=" * 50)
    
    find_active_market()
    
    if SLUG:
        print(f"Active market: {SLUG}")
        # Extract timestamp from slug for accurate timing
        try:
            candle_start_time = int(SLUG.split("-")[-1])
            print(f"Market started: {datetime.fromtimestamp(candle_start_time)}")
        except:
            pass
    
    if not tokens:
        get_tokens()
    
    if tokens:
        set_allowances()
    
    last_btc_change = 0
    
    while True:
        try:
            # Connect to Polymarket RTDS for Chainlink BTC/USD
            async with websockets.connect("wss://ws-live-data.polymarket.com") as ws:
                print("Connected to Polymarket RTDS (Chainlink)")
                
                # Subscribe to Chainlink BTC/USD
                await ws.send(json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "crypto_prices_chainlink",
                        "type": "*",
                        "filters": "{\"symbol\":\"btc/usd\"}"
                    }]
                }))
                print("Subscribed to Chainlink BTC/USD")
                
                tick_count = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    
                    # Skip empty or non-JSON messages
                    if not msg or msg == "":
                        continue
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    
                    if data.get("topic") != "crypto_prices_chainlink":
                        continue
                    
                    payload = data.get("payload", {})
                    if payload.get("symbol") != "btc/usd":
                        continue
                    
                    btc_now = float(payload.get("value", 0))
                    
                    tick_count += 1
                    
                    # Set initial candle_open from first Chainlink price
                    if candle_open is None:
                        candle_open = btc_now
                        print(f"Initial price (Chainlink): ${candle_open:,.2f}")
                    
                    # Check for new market every 30 ticks
                    if tick_count % 30 == 0:
                        old_slug = SLUG
                        if find_active_market():
                            if SLUG != old_slug:
                                # New market - settle old positions
                                settle_positions(last_btc_change)
                                
                                print(f"\n*** NEW MARKET: {SLUG} ***")
                                # Update candle_open to current price for new market
                                candle_open = btc_now
                                print(f"New candle open: ${candle_open:,.2f}")
                                doubled_down = False
                                bought_winner_direct = False
                                get_tokens()
                                set_allowances()
                    
                    m = get_market_prices()
                    if not m:
                        continue
                    
                    # Calculate BTC change using Chainlink prices
                    if candle_open and candle_open > 0:
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
                        print(f"🔒{extra} | UP:{up_shares:.1f}(${up_pnl:+.2f}) DN:{dn_shares:.1f}(${dn_pnl:+.2f}) | BTC:{btc_change:+.3f}% | {minutes_left:.1f}m")
                    elif has_position():
                        if up_shares > 0:
                            pnl = (up_price - up_position["avg_price"]) * up_shares
                            print(f"🎯 UP | {up_shares:.1f}@{up_position['avg_price']*100:.0f}c(${pnl:+.2f}) | BTC:{btc_change:+.3f}% ({momentum}) | {minutes_left:.1f}m")
                        else:
                            pnl = (dn_price - dn_position["avg_price"]) * dn_shares
                            print(f"🎯 DN | {dn_shares:.1f}@{dn_position['avg_price']*100:.0f}c(${pnl:+.2f}) | BTC:{btc_change:+.3f}% ({momentum}) | {minutes_left:.1f}m")
                    else:
                        can_hedge, reason = check_hedge_entry(up_price, dn_price)
                        winner_opp = ""
                        if up_price >= WINNER_THRESHOLD and up_price <= MAX_WINNER_PRICE and momentum == 'UP':
                            winner_opp = f" | 🎯 UP@{up_price*100:.0f}c!"
                        elif dn_price >= WINNER_THRESHOLD and dn_price <= MAX_WINNER_PRICE and momentum == 'DOWN':
                            winner_opp = f" | 🎯 DN@{dn_price*100:.0f}c!"
                        print(f"👀 | UP:{up_price*100:.0f}c DN:{dn_price*100:.0f}c | BTC:{btc_change:+.3f}% ({momentum}) | {minutes_left:.1f}m | {reason}{winner_opp}")
                    
                    # === TRADING LOGIC ===
                    
                    total_spent = up_position["cost"] + dn_position["cost"]
                    remaining = TOTAL_BUDGET - total_spent
                    
                    # === STRATEGY 1: HEDGE ===
                    if not has_position() and not bought_winner_direct:
                        can_enter, reason = check_hedge_entry(up_price, dn_price)
                        if can_enter:
                            print(f"\n*** 🎯 HEDGE ENTRY: UP@{up_price*100:.0f}c + DN@{dn_price*100:.0f}c = {combined*100:.0f}c ***")
                            if buy_with_retry(m["up_t"], FIRST_BUY, up_price, "UP"):
                                time.sleep(0.1)
                                buy_with_retry(m["dn_t"], HEDGE_BUY, dn_price, "DN")
                    
                    # === COMPLETE PARTIAL HEDGE ===
                    if has_position() and not is_hedged() and not bought_winner_direct:
                        if up_shares > 0 and dn_shares == 0:
                            combined_check = up_position["avg_price"] + dn_price
                            if combined_check < MAX_COMBINED_AVG and remaining >= 1:
                                amount = min(up_shares * dn_price, remaining, HEDGE_BUY)
                                if amount >= 1:
                                    print(f"\n*** COMPLETING HEDGE: DN @ {dn_price*100:.0f}c ***")
                                    buy_with_retry(m["dn_t"], amount, dn_price, "DN")
                        elif dn_shares > 0 and up_shares == 0:
                            combined_check = up_price + dn_position["avg_price"]
                            if combined_check < MAX_COMBINED_AVG and remaining >= 1:
                                amount = min(dn_shares * up_price, remaining, HEDGE_BUY)
                                if amount >= 1:
                                    print(f"\n*** COMPLETING HEDGE: UP @ {up_price*100:.0f}c ***")
                                    buy_with_retry(m["up_t"], amount, up_price, "UP")
                    
                    # === STRATEGY 2: DOUBLE DOWN ===
                    if is_hedged() and not doubled_down and remaining >= 1:
                        if (up_price >= WINNER_THRESHOLD and 
                            up_price <= MAX_WINNER_PRICE and 
                            btc_change > 0):
                            buy_amount = min(WINNER_BUY, remaining)
                            print(f"\n*** 🚀 DD: UP @ {up_price*100:.0f}c (BTC {btc_change:+.3f}%) ***")
                            if buy_with_retry(m["up_t"], buy_amount, up_price, "UP"):
                                doubled_down = True
                        elif (dn_price >= WINNER_THRESHOLD and 
                              dn_price <= MAX_WINNER_PRICE and 
                              btc_change < 0):
                            buy_amount = min(WINNER_BUY, remaining)
                            print(f"\n*** 🚀 DD: DN @ {dn_price*100:.0f}c (BTC {btc_change:+.3f}%) ***")
                            if buy_with_retry(m["dn_t"], buy_amount, dn_price, "DN"):
                                doubled_down = True
                    
                    # === STRATEGY 3: MOMENTUM BUY ===
                    if not has_position() and not bought_winner_direct and remaining >= WINNER_BUY:
                        can_hedge, _ = check_hedge_entry(up_price, dn_price)
                        if not can_hedge:
                            if (up_price >= WINNER_THRESHOLD and 
                                up_price <= MAX_WINNER_PRICE and 
                                momentum == 'UP'):
                                print(f"\n*** 🎯 MOMENTUM: UP @ {up_price*100:.0f}c (BTC {btc_change:+.3f}%) ***")
                                if buy_with_retry(m["up_t"], WINNER_BUY, up_price, "UP"):
                                    bought_winner_direct = True
                            elif (dn_price >= WINNER_THRESHOLD and 
                                  dn_price <= MAX_WINNER_PRICE and 
                                  momentum == 'DOWN'):
                                print(f"\n*** 🎯 MOMENTUM: DN @ {dn_price*100:.0f}c (BTC {btc_change:+.3f}%) ***")
                                if buy_with_retry(m["dn_t"], WINNER_BUY, dn_price, "DN"):
                                    bought_winner_direct = True
                        
        except websockets.exceptions.ConnectionClosed:
            print("\nRTDS connection closed, reconnecting...")
            await asyncio.sleep(2)
        except asyncio.TimeoutError:
            print("\nRTDS timeout, reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
