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

# === GABAGOOL STRATEGY v34 ===
# TRUE Gabagool: Dynamic buy amounts to keep shares balanced
# - Calculates exact $ needed to match shares
# - Combined avg < 97c = guaranteed profit

TOTAL_BUDGET = 8              # Total $ to spend per candle
MAX_COMBINED_AVG = 0.95       # Combined must be < 95c (profit after 2% fee)
MIN_PRICE = 0.15              # Don't buy if outcome decided (too cheap)
MAX_PRICE = 0.70              # Don't buy if outcome decided (too expensive)
MIN_BUY = 1.0                 # Minimum buy amount ($1)

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


def buy(token_id, amount, price, side):
    """Execute a buy with specific $ amount and track position"""
    global up_position, dn_position
    
    print(f"\n>>> BUYING {side} ${amount:.2f} at ~{price*100:.0f}c")
    
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
        profit_per_share = 1.0 - combined_avg
        guaranteed_profit = hedged_shares * profit_per_share
        print(f"       🎯 Combined avg: {combined_avg*100:.0f}c")
        print(f"       📈 Shares: {up_shares:.1f} UP vs {dn_shares:.1f} DN")
        print(f"       💰 HEDGED: {hedged_shares:.2f} shares = ${guaranteed_profit:.2f} guaranteed profit!")
    elif up_shares > 0:
        max_dn_price = MAX_COMBINED_AVG - up_avg
        print(f"       ⏳ Need DN ≤ {max_dn_price*100:.0f}c to hedge")
    elif dn_shares > 0:
        max_up_price = MAX_COMBINED_AVG - dn_avg
        print(f"       ⏳ Need UP ≤ {max_up_price*100:.0f}c to hedge")
    print()


def calculate_buy_amount(side, price):
    """
    GABAGOOL LOGIC: 
    - First buy: half budget on whichever side is in range
    - Second buy: ONLY if combined < 97c
    
    Returns: (should_buy, amount, reason)
    """
    up_shares = up_position["shares"]
    dn_shares = dn_position["shares"]
    up_cost = up_position["cost"]
    dn_cost = dn_position["cost"]
    up_avg = up_position["avg_price"]
    dn_avg = dn_position["avg_price"]
    total_spent = up_cost + dn_cost
    remaining_budget = TOTAL_BUDGET - total_spent
    
    # Check price limits
    if price < MIN_PRICE:
        return False, 0, f"Price {price*100:.0f}c too low"
    if price > MAX_PRICE:
        return False, 0, f"Price {price*100:.0f}c too high"
    
    # No budget left
    if remaining_budget < MIN_BUY:
        return False, 0, "Budget exhausted"
    
    # CASE 1: No position at all - first buy
    if up_shares == 0 and dn_shares == 0:
        if side == "UP":
            amount = min(TOTAL_BUDGET / 2, remaining_budget)
            return True, amount, "First buy (half budget)"
        else:
            amount = min(TOTAL_BUDGET / 2, remaining_budget)
            return True, amount, "First buy (half budget)"
    
    # CASE 2: Already have one side - need to hedge
    if side == "UP" and dn_shares > 0 and up_shares == 0:
        # Check if combined would be profitable
        combined = price + dn_avg
        if combined >= MAX_COMBINED_AVG:
            return False, 0, f"Combined {combined*100:.0f}c ≥ 97c (waiting...)"
        
        # Good! Buy to match shares
        shares_needed = dn_shares
        amount_needed = shares_needed * price
        amount = min(amount_needed, remaining_budget)
        
        if amount < MIN_BUY:
            return False, 0, "Amount too small"
        return True, amount, f"Hedge @ {combined*100:.0f}c combined"
    
    if side == "DN" and up_shares > 0 and dn_shares == 0:
        # Check if combined would be profitable
        combined = up_avg + price
        if combined >= MAX_COMBINED_AVG:
            return False, 0, f"Combined {combined*100:.0f}c ≥ 97c (waiting...)"
        
        # Good! Buy to match shares
        shares_needed = up_shares
        amount_needed = shares_needed * price
        amount = min(amount_needed, remaining_budget)
        
        if amount < MIN_BUY:
            return False, 0, "Amount too small"
        return True, amount, f"Hedge @ {combined*100:.0f}c combined"
    
    # CASE 3: Already hedged or same side
    return False, 0, "Already positioned"


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
        up_payout = up_shares * 1.0
        dn_payout = 0
    else:
        winner = "DN"
        up_payout = 0
        dn_payout = dn_shares * 1.0
    
    total_payout = up_payout + dn_payout
    total_cost = up_cost + dn_cost
    
    # Account for 2% fee on winnings
    fee = total_payout * 0.02
    net_payout = total_payout - fee
    pnl = net_payout - total_cost
    
    print(f"Winner: {winner}")
    print(f"UP: {up_shares:.2f} shares → ${up_payout:.2f}")
    print(f"DN: {dn_shares:.2f} shares → ${dn_payout:.2f}")
    print(f"Total cost: ${total_cost:.2f}")
    print(f"Gross payout: ${total_payout:.2f}")
    print(f"Fee (2%): -${fee:.2f}")
    print(f"Net payout: ${net_payout:.2f}")
    
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
    print("  BOT v34 - TRUE GABAGOOL (Dynamic Amounts)")
    print("  Calculates exact $ to balance shares")
    print("=" * 60)
    print(f"Total budget: ${TOTAL_BUDGET}")
    print(f"Price range: {MIN_PRICE*100:.0f}c - {MAX_PRICE*100:.0f}c")
    print(f"Max combined: {MAX_COMBINED_AVG*100:.0f}c (guarantees profit after 2% fee)")
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
                    up_shares = up_position["shares"]
                    dn_shares = dn_position["shares"]
                    up_avg = up_position["avg_price"]
                    dn_avg = dn_position["avg_price"]
                    
                    # Status display
                    if up_shares > 0 or dn_shares > 0:
                        pnl_parts = []
                        if up_shares > 0:
                            up_pnl = (up_price - up_avg) * up_shares
                            pnl_parts.append(f"UP:{up_shares:.1f}@{up_avg*100:.0f}c(${up_pnl:+.2f})")
                        if dn_shares > 0:
                            dn_pnl = (dn_price - dn_avg) * dn_shares
                            pnl_parts.append(f"DN:{dn_shares:.1f}@{dn_avg*100:.0f}c(${dn_pnl:+.2f})")
                        
                        hedge_status = ""
                        if up_shares > 0 and dn_shares > 0:
                            hedge_status = " 🔒"
                        
                        print(f"HOLD{hedge_status} | {' | '.join(pnl_parts)} | BTC:{btc_change:+.2f}%")
                    else:
                        print(f"BTC:{btc_change:+.2f}% | UP:{up_price*100:.0f}c DN:{dn_price*100:.0f}c (={combined*100:.0f}c)")
                    
                    # === GABAGOOL BUYING LOGIC ===
                    # Check which side needs buying to balance
                    # ONLY BUY ONE SIDE PER TICK!
                    
                    bought_this_tick = False
                    
                    should_up, up_amount, up_reason = calculate_buy_amount("UP", up_price)
                    if should_up and not bought_this_tick:
                        print(f"*** UP @ {up_price*100:.0f}c - {up_reason} ***")
                        if buy(m["up_t"], up_amount, up_price, "UP"):
                            bought_this_tick = True
                    
                    should_dn, dn_amount, dn_reason = calculate_buy_amount("DN", dn_price)
                    if should_dn and not bought_this_tick:
                        print(f"*** DN @ {dn_price*100:.0f}c - {dn_reason} ***")
                        if buy(m["dn_t"], dn_amount, dn_price, "DN"):
                            bought_this_tick = True
                        
        except websockets.exceptions.ConnectionClosed:
            print("\nWebsocket closed, reconnecting...")
            await asyncio.sleep(2)
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
