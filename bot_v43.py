import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
import os
import time
from dotenv import load_dotenv
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

load_dotenv()

client = ClobClient(
    host="https://clob.polymarket.com", 
    key=os.getenv("POLYMARKET_PRIVATE_KEY"), 
    chain_id=137, 
    signature_type=2, 
    funder=os.getenv("POLYMARKET_FUNDER")
)
client.set_api_creds(client.create_or_derive_api_creds())

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
candle_start_time = None
last_candle_time = None

up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
doubled_down = False
bought_winner_direct = False
session_pnl = 0.0
executor = ThreadPoolExecutor(max_workers=2)

def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900

def get_minutes_remaining():
    if not candle_start_time:
        return 15
    elapsed = time.time() - candle_start_time
    return 15 - (elapsed / 60)

def get_momentum(btc_change):
    if btc_change >= MOMENTUM_THRESHOLD:
        return 'UP'
    elif btc_change <= -MOMENTUM_THRESHOLD:
        return 'DOWN'
    return 'CHOPPY'

def find_active_market():
    global SLUG, tokens, candle_start_time
    current_ts = get_current_market_timestamp()
    for offset in [0, 900, -900]:
        timestamp = current_ts + offset
        expected_slug = f"btc-updown-15m-{timestamp}"
        try:
            r = requests.get(f"https://gamma-api.polymarket.com/events?slug={expected_slug}", timeout=5)
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
            print(f"Error: {e}")
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
    try:
        up_price = float(client.get_price(tokens["up_t"], "buy").get("price", 0))
        dn_price = float(client.get_price(tokens["dn_t"], "buy").get("price", 0))
        return {"up": up_price, "dn": dn_price, "up_t": tokens["up_t"], "dn_t": tokens["dn_t"]}
    except:
        return None

def buy_single(token_id, amount, price, side):
    global up_position, dn_position
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
            return {"success": True, "side": side, "shares": total_size, "cost": total_cost, "avg_price": avg_price, "token_id": token_id}
        return {"success": False, "side": side, "error": resp.get("error", "Unknown")}
    except Exception as e:
        return {"success": False, "side": side, "error": str(e)}

def buy_both_fast(up_token, dn_token, up_price, dn_price, up_amount, dn_amount):
    global up_position, dn_position
    print(f"\n*** ⚡ FAST HEDGE: UP@{up_price*100:.0f}c + DN@{dn_price*100:.0f}c ***")
    future_up = executor.submit(buy_single, up_token, up_amount, up_price, "UP")
    future_dn = executor.submit(buy_single, dn_token, dn_amount, dn_price, "DN")
    result_up = future_up.result(timeout=10)
    result_dn = future_dn.result(timeout=10)
    if result_up["success"]:
        up_position = {"shares": result_up["shares"], "cost": result_up["cost"], "avg_price": result_up["avg_price"], "token_id": result_up["token_id"]}
        print(f"    ✓ BOUGHT {result_up['shares']:.2f} UP @ {result_up['avg_price']*100:.0f}c (${result_up['cost']:.2f})")
    else:
        print(f"    ✗ UP failed: {result_up.get('error')}")
    if result_dn["success"]:
        dn_position = {"shares": result_dn["shares"], "cost": result_dn["cost"], "avg_price": result_dn["avg_price"], "token_id": result_dn["token_id"]}
        print(f"    ✓ BOUGHT {result_dn['shares']:.2f} DN @ {result_dn['avg_price']*100:.0f}c (${result_dn['cost']:.2f})")
    else:
        print(f"    ✗ DN failed: {result_dn.get('error')}")
    if result_up["success"] and result_dn["success"]:
        combined = up_position["avg_price"] + dn_position["avg_price"]
        print(f"    ⚡ HEDGED @ {combined*100:.0f}c combined!")
    return result_up["success"] or result_dn["success"]

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
                    up_position = {"shares": new_shares, "cost": new_cost, "avg_price": new_cost / new_shares if new_shares > 0 else 0, "token_id": token_id}
                    print(f"    ✓ BOUGHT {total_size:.2f} UP @ {avg_price*100:.0f}c (${total_cost:.2f})")
                else:
                    new_shares = dn_position["shares"] + total_size
                    new_cost = dn_position["cost"] + total_cost
                    dn_position = {"shares": new_shares, "cost": new_cost, "avg_price": new_cost / new_shares if new_shares > 0 else 0, "token_id": token_id}
                    print(f"    ✓ BOUGHT {total_size:.2f} DN @ {avg_price*100:.0f}c (${total_cost:.2f})")
                return True
            else:
                if attempt < retries - 1:
                    print(f"    ⟳ Retry {attempt + 1}/{retries}...")
                    time.sleep(0.3)
                    continue
                return False
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(0.3)
                continue
            print(f"    ✗ {e}")
            return False
    return False

def is_hedged():
    return up_position["shares"] > 0 and dn_position["shares"] > 0

def has_position():
    return up_position["shares"] > 0 or dn_position["shares"] > 0

def check_hedge_entry(up_price, dn_price):
    combined = up_price + dn_price
    if combined > MAX_COMBINED_ENTRY:
        return False, f"Combined {combined*100:.0f}c > {MAX_COMBINED_ENTRY*100:.0f}c"
    if up_price < MIN_PRICE or up_price > MAX_PRICE:
        return False, f"UP {up_price*100:.0f}c out of range"
    if dn_price < MIN_PRICE or dn_price > MAX_PRICE:
        return False, f"DN {dn_price*100:.0f}c out of range"
    return True, f"✓ READY ({combined*100:.0f}c)"

def get_binance_candle():
    try:
        r = requests.get("https://api.binance.com/api/v3/klines", params={"symbol": "BTCUSDT", "interval": "15m", "limit": 1}, timeout=5)
        data = r.json()
        if data and len(data) > 0:
            return data[0][0], float(data[0][1])
    except:
        pass
    return None, None

def settle_positions(btc_change):
    global up_position, dn_position, session_pnl, doubled_down, bought_winner_direct
    if not has_position():
        return
    up_shares = up_position["shares"]
    dn_shares = dn_position["shares"]
    total_cost = up_position["cost"] + dn_position["cost"]
    if btc_change > 0:
        winner = "UP"
        payout = up_shares * 1.0
    elif btc_change < 0:
        winner = "DN"
        payout = dn_shares * 1.0
    else:
        winner = "FLAT"
        payout = 0
    pnl = payout - total_cost
    session_pnl += pnl
    print(f"\n{'='*50}")
    print(f"🏁 SETTLED - {winner} WINS (BTC {btc_change:+.2f}%)")
    print(f"   Cost: ${total_cost:.2f} | Payout: ${payout:.2f} | P/L: ${pnl:+.2f}")
    print(f"   Session: ${session_pnl:+.2f}")
    print(f"{'='*50}\n")
    up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
    dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
    doubled_down = False
    bought_winner_direct = False

async def main():
    global candle_open, last_candle_time, candle_start_time, doubled_down, bought_winner_direct
    find_active_market()
    if not tokens:
        get_tokens()
    if tokens:
        set_allowances()
    last_candle_time, candle_open = get_binance_candle()
    if candle_open:
        candle_start_time = time.time()
        print(f"Initial candle: ${candle_open:,.2f}")
    last_btc_change = 0
    while True:
        try:
            async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                print("Connected to Binance WebSocket")
                tick_count = 0
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    tick_count += 1
                    btc_now = float(data["p"])
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
                    # Status
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
                        winner_opp = ""
                        if up_price >= WINNER_THRESHOLD and up_price <= MAX_WINNER_PRICE and momentum == 'UP':
                            winner_opp = f" | 🎯 UP@{up_price*100:.0f}c!"
                        elif dn_price >= WINNER_THRESHOLD and dn_price <= MAX_WINNER_PRICE and momentum == 'DOWN':
                            winner_opp = f" | 🎯 DN@{dn_price*100:.0f}c!"
                        print(f"👀 | UP:{up_price*100:.0f}c DN:{dn_price*100:.0f}c | BTC:{btc_change:+.2f}% ({momentum}) | {minutes_left:.1f}m | {reason}{winner_opp}")
                    # TRADING
                    total_spent = up_position["cost"] + dn_position["cost"]
                    remaining = TOTAL_BUDGET - total_spent
                    # FAST HEDGE
                    if not has_position() and not bought_winner_direct:
                        can_enter, _ = check_hedge_entry(up_price, dn_price)
                        if can_enter:
                            buy_both_fast(m["up_t"], m["dn_t"], up_price, dn_price, FIRST_BUY, FIRST_BUY)
                    # DD (v40 style - 70c+ with direction only)
                    if is_hedged() and not doubled_down and remaining >= 1:
                        if up_price >= WINNER_THRESHOLD and up_price <= MAX_WINNER_PRICE and btc_change > 0:
                            print(f"\n*** 🚀 DD: UP @ {up_price*100:.0f}c (BTC {btc_change:+.2f}%) ***")
                            if buy_with_retry(m["up_t"], min(WINNER_BUY, remaining), up_price, "UP"):
                                doubled_down = True
                        elif dn_price >= WINNER_THRESHOLD and dn_price <= MAX_WINNER_PRICE and btc_change < 0:
                            print(f"\n*** 🚀 DD: DN @ {dn_price*100:.0f}c (BTC {btc_change:+.2f}%) ***")
                            if buy_with_retry(m["dn_t"], min(WINNER_BUY, remaining), dn_price, "DN"):
                                doubled_down = True
                    # MOMENTUM (needs 0.08%)
                    if not has_position() and not bought_winner_direct and remaining >= WINNER_BUY:
                        can_hedge, _ = check_hedge_entry(up_price, dn_price)
                        if not can_hedge:
                            if up_price >= WINNER_THRESHOLD and up_price <= MAX_WINNER_PRICE and momentum == 'UP':
                                print(f"\n*** 🎯 MOMENTUM: UP @ {up_price*100:.0f}c (BTC {btc_change:+.2f}%) ***")
                                if buy_with_retry(m["up_t"], WINNER_BUY, up_price, "UP"):
                                    bought_winner_direct = True
                            elif dn_price >= WINNER_THRESHOLD and dn_price <= MAX_WINNER_PRICE and momentum == 'DOWN':
                                print(f"\n*** 🎯 MOMENTUM: DN @ {dn_price*100:.0f}c (BTC {btc_change:+.2f}%) ***")
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
