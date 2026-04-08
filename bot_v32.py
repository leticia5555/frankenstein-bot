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

# === GABAGOOL STRATEGY v32 ===
# Buy BOTH sides when cheap, hold to expiry, collect guaranteed profit
# Key: Only buy if we can realistically hedge (combined < 97c)

TRADE_AMOUNT = 2              # Amount per buy
MAX_SPEND_PER_SIDE = 4        # Max $4 per side (2 buys each)
MIN_PRICE = 0.15              # Don't buy below 15c (outcome already decided)
MAX_PRICE = 0.45              # Don't buy above 45c (not cheap enough)
TARGET_COMBINED = 0.95        # Goal: UP avg + DN avg < 95c (accounting for 2% fee)

# NO STOP LOSS - hold to expiry!
# NO TAKE PROFIT - let it settle at $1 or $0

SLUG = None
tokens = None
candle_open = None
last_candle_time = None

# Track positions for BOTH sides
up_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}
dn_position = {"shares": 0, "cost": 0, "avg_price": 0, "token_id": None}

session_pnl = 0.0
candles_traded = 0


def get_current_market_timestamp():
    now = int(time.time())
    return (now // 900) * 900


def find_active_market():
    global SLUG, tokens
    
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
                            
                            # Get tokens
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


def buy(token_id, price, side):
    """Execute a buy and track position"""
    global up_position, dn_position
    
    print(f"\n>>> BUYING {side} at ~{price*100:.0f}c")
    
    try:
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token_id, amount=TRADE_AMOUNT, side=BUY)
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
                total_size = TRADE_AMOUNT / price
                total_cost = TRADE_AMOUNT
            
            # Update position
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
            
            # Show hedge status
            show_hedge_status()
            return True
        else:
            err = resp.get("error", resp.get("data", "Unknown"))
            print(f"    ✗ {err}")
            return False
            
    except Exception as e:
        print(f"    ✗ {e}")
        return False


def show_hedge_status():
    """Show current hedge status"""
    up_cost = up_position["cost"]
    dn_cost = dn_position["cost"]
    up_avg = up_position["avg_price"]
    dn_avg = dn_position["avg_price"]
    up_shares = up_position["shares"]
    dn_shares = dn_position["shares"]
    
    print(f"\n    📊 POSITION STATUS:")
    if up_shares > 0:
        print(f"       UP: {up_shares:.2f} shares @ {up_avg*100:.0f}c (${up_cost:.2f})")
    if dn_shares > 0:
        print(f"       DN: {dn_shares:.2f} shares @ {dn_avg*100:.0f}c (${dn_cost:.2f})")
    
    if up_shares > 0 and dn_shares > 0:
        combined = up_avg + dn_avg
        min_shares = min(up_shares, dn_shares)
        hedged_cost = min_shares * combined
        hedged_payout = min_shares * 1.0
        guaranteed_profit = hedged_payout - hedged_cost
        
        print(f"       🎯 Combined avg: {combined*100:.0f}c")
        print(f"       💰 HEDGED: {min_shares:.2f} shares = ${guaranteed_profit:.2f} guaranteed profit!")
    elif up_shares > 0:
        need_dn = TARGET_COMBINED - up_avg
        print(f"       ⏳ Need DN ≤ {need_dn*100:.0f}c to hedge")
    elif dn_shares > 0:
        need_up = TARGET_COMBINED - dn_avg
        print(f"       ⏳ Need UP ≤ {need_up*100:.0f}c to hedge")
    print()


def can_buy_side(side, price, other_avg):
    """
    Check if we should buy this side.
    Returns True if:
    1. Price is in the sweet spot (15c-45c)
    2. We haven't spent max on this side yet
    3. Either: no position on other side, OR combined would be < 97c
    """
    if side == "UP":
        current_cost = up_position["cost"]
    else:
        current_cost = dn_position["cost"]
    
    # Check 1: Price must be in sweet spot (not too cheap, not too expensive)
    if price < MIN_PRICE:
        return False, f"Price {price*100:.0f}c < min {MIN_PRICE*100:.0f}c (outcome decided)"
    if price > MAX_PRICE:
        return False, f"Price {price*100:.0f}c > max {MAX_PRICE*100:.0f}c"
    
    # Check 2: Haven't maxed out this side
    if current_cost >= MAX_SPEND_PER_SIDE:
        return False, f"Already spent ${current_cost:.2f} on {side} (max ${MAX_SPEND_PER_SIDE})"
    
    # Check 3: Can we hedge?
    if other_avg > 0:
        combined = price + other_avg
        if combined >= TARGET_COMBINED:
            return False, f"Combined {price*100:.0f}c + {other_avg*100:.0f}c = {combined*100:.0f}c ≥ {TARGET_COMBINED*100:.0f}c"
    
    return True, "OK"


def settle_positions(btc_final_change):
    """Calculate P/L at candle end"""
    global session_pnl, candles_traded, up_position, dn_position
    
    up_shares = up_position["shares"]
    dn_shares = dn_position["shares"]
    up_cost = up_position["cost"]
    dn_cost = dn_position["cost"]
    
    if up_shares == 0 and dn_shares == 0:
        return
    
    print(f"\n{'='*50}")
    print(f"*** CANDLE SETTLED - BTC {btc_final_change:+.2f}% ***")
    print(f"{'='*50}")
    
    # Determine winner
    if btc_final_change >= 0:
        winner = "UP"
        up_payout = up_shares * 1.0  # UP wins = $1 each
        dn_payout = 0                 # DN loses = $0
    else:
        winner = "DN"
        up_payout = 0                 # UP loses = $0
        dn_payout = dn_shares * 1.0  # DN wins = $1 each
    
    total_payout = up_payout + dn_payout
    total_cost = up_cost + dn_cost
    pnl = total_payout - total_cost
    
    print(f"Winner: {winner}")
    print(f"UP: {up_shares:.2f} shares → ${up_payout:.2f}")
    print(f"DN: {dn_shares:.2f} shares → ${dn_payout:.2f}")
    print(f"Total cost: ${total_cost:.2f}")
    print(f"Total payout: ${total_payout:.2f}")
    
    if pnl >= 0:
        print(f"✅ PROFIT: ${pnl:+.2f}")
    else:
        print(f"❌ LOSS: ${pnl:+.2f}")
    
    session_pnl += pnl
    candles_traded += 1
    print(f"\nSession: {candles_traded} candles | Total P/L: ${session_pnl:+.2f}")
    print(f"{'='*50}\n")
    
    # Reset positions
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
    global candle_open, tokens, last_candle_time, up_position, dn_position
    
    print("=" * 60)
    print("  BOT v32 - GABAGOOL STRATEGY (PROPER)")
    print("  Buy both sides cheap, hold to expiry, profit guaranteed")
    print("=" * 60)
    print(f"Max per side: ${MAX_SPEND_PER_SIDE}")
    print(f"Buy when: {MIN_PRICE*100:.0f}c - {MAX_PRICE*100:.0f}c (sweet spot)")
    print(f"Target combined: <{TARGET_COMBINED*100:.0f}c (after 2% fee = profit)")
    print(f"NO STOP LOSS - hold to expiry!")
    print("=" * 60)
    
    find_active_market()
    if not SLUG:
        print("ERROR: No active market!")
        return
    
    print(f"\n*** MARKET: {SLUG} ***\n")
    get_tokens()
    set_allowances()
    last_candle_time, candle_open = get_binance_candle()
    
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
                    
                    if tick_count % 10 != 0:
                        continue
                    
                    d = json.loads(msg)
                    btc_now = float(d['p'])
                    
                    # Check for new candle
                    if tick_count % 50 == 0:
                        new_time, new_open = get_binance_candle()
                        if new_time and new_time != last_candle_time:
                            # Settle old positions
                            settle_positions(last_btc_change)
                            
                            print(f"\n*** NEW CANDLE: ${new_open:,.2f} ***\n")
                            last_candle_time = new_time
                            candle_open = new_open
                        
                        if find_active_market():
                            print(f"\n*** NEW MARKET: {SLUG} ***\n")
                            get_tokens()
                            set_allowances()
                    
                    # Get prices
                    m = get_market_prices()
                    if not m:
                        continue
                    
                    # Calculate BTC change
                    if candle_open:
                        btc_change = (btc_now - candle_open) / candle_open * 100
                        last_btc_change = btc_change
                    else:
                        btc_change = 0
                    
                    up_price = m["up"]
                    dn_price = m["dn"]
                    combined = up_price + dn_price
                    
                    # Current positions
                    up_avg = up_position["avg_price"]
                    dn_avg = dn_position["avg_price"]
                    up_shares = up_position["shares"]
                    dn_shares = dn_position["shares"]
                    
                    # Status display
                    if up_shares > 0 or dn_shares > 0:
                        # Show holding status
                        pnl_parts = []
                        if up_shares > 0:
                            up_pnl = (up_price - up_avg) * up_shares
                            pnl_parts.append(f"UP:{up_avg*100:.0f}→{up_price*100:.0f}c(${up_pnl:+.2f})")
                        if dn_shares > 0:
                            dn_pnl = (dn_price - dn_avg) * dn_shares
                            pnl_parts.append(f"DN:{dn_avg*100:.0f}→{dn_price*100:.0f}c(${dn_pnl:+.2f})")
                        
                        hedge_status = ""
                        if up_shares > 0 and dn_shares > 0:
                            hedge_status = " 🔒HEDGED"
                        
                        print(f"HOLD{hedge_status} | {' | '.join(pnl_parts)} | BTC:{btc_change:+.2f}%")
                    else:
                        print(f"BTC:{btc_change:+.2f}% | UP:{up_price*100:.0f}c DN:{dn_price*100:.0f}c (={combined*100:.0f}c)")
                    
                    # === BUYING LOGIC ===
                    
                    # Check if we should buy UP
                    can_up, reason_up = can_buy_side("UP", up_price, dn_avg)
                    if can_up:
                        print(f"*** UP @ {up_price*100:.0f}c is CHEAP! ***")
                        buy(m["up_t"], up_price, "UP")
                    
                    # Check if we should buy DN
                    can_dn, reason_dn = can_buy_side("DN", dn_price, up_avg)
                    if can_dn:
                        print(f"*** DN @ {dn_price*100:.0f}c is CHEAP! ***")
                        buy(m["dn_t"], dn_price, "DN")
                        
        except websockets.exceptions.ConnectionClosed:
            print("\nWebsocket closed, reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
