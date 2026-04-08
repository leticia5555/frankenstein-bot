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

# === SETTINGS ===
TRADE_AMOUNT = 2
EDGE_THRESHOLD = 0.10      # Back to 10c - be more selective
TREND_THRESHOLD = 0.03     # 0.03% BTC move = trend
PROFIT_TARGET = 0.05       # Take profit at +5c (was 4c)
STOP_LOSS = 0.04           # Tighter stop loss at -4c (was 6c)

position = None
SLUG = None
tokens = None
candle_open = None
last_candle_time = None

# Track session P/L correctly
session_pnl = 0.0
trade_count = 0
win_count = 0


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
                            print(f"*** NEW MARKET DETECTED ***")
                            print(f"Slug: {expected_slug}")
                            print(f"Timestamp: {datetime.fromtimestamp(timestamp)}")
                            print(f"{'='*50}\n")
                            SLUG = expected_slug
                            return True
                        return False
        except Exception as e:
            print(f"Error checking {expected_slug}: {e}")
    
    return False


def get_tokens():
    global tokens
    tokens = None
    
    if not SLUG:
        return None
    
    try:
        r = requests.get(
            f"https://gamma-api.polymarket.com/events?slug={SLUG}",
            timeout=5
        )
        data = r.json()
        
        if data:
            markets = data[0].get("markets", [])
            if markets:
                m = markets[0]
                clob_ids = m.get("clobTokenIds", "[]")
                t = json.loads(clob_ids) if isinstance(clob_ids, str) else clob_ids
                
                if t and len(t) >= 2:
                    tokens = {"up_t": t[0], "dn_t": t[1]}
                    print(f"Tokens: UP={t[0][:16]}... DN={t[1][:16]}...")
                    return tokens
    except Exception as e:
        print(f"Error getting tokens: {e}")
    
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
        print("✓ Allowances set!")
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
        return {
            "up": up, 
            "dn": dn, 
            "up_t": tokens["up_t"], 
            "dn_t": tokens["dn_t"]
        }
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
        
        print(f"    API Response: {resp}")  # Debug
        
        if resp.get("success") and resp.get("status") == "matched":
            # For BUY: makingAmount = USDC spent, takingAmount = shares received
            usdc_spent = float(resp.get("makingAmount", 0))
            shares_received = float(resp.get("takingAmount", 0))
            
            if shares_received > 0:
                fill_price = usdc_spent / shares_received
            else:
                fill_price = price
            
            position = {
                "token": token, 
                "entry_price": fill_price,
                "size": shares_received, 
                "side": side, 
                "cost": usdc_spent
            }
            print(f"    ✓ BOUGHT {shares_received:.2f} shares at {fill_price*100:.0f}c")
            print(f"    ✓ Spent: ${usdc_spent:.2f}")
            return True
        else:
            print(f"    ✗ Not filled: {resp}")
    except Exception as e:
        print(f"    ✗ Buy error: {e}")
    
    return False


def sell(reason):
    global position, session_pnl, trade_count, win_count
    
    if not position:
        return False
    
    try:
        print(f"\n<<< SELLING {position['side']} - {reason}")
        
        # Get actual balance
        params = BalanceAllowanceParams(
            asset_type=AssetType.CONDITIONAL, 
            token_id=position["token"]
        )
        bal = client.get_balance_allowance(params)
        raw_balance = int(bal.get("balance", 0))
        actual_shares = raw_balance / 1_000_000
        
        if actual_shares <= 0:
            print("    No shares!")
            position = None
            return False
        
        # Try to sell
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(
            token_id=position["token"], 
            amount=actual_shares, 
            side=SELL
        )
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        
        print(f"    API Response: {resp}")  # Debug
        
        if resp.get("success") and resp.get("status") == "matched":
            # For SELL: takingAmount = USDC received, makingAmount = shares sold
            usdc_received = float(resp.get("takingAmount", 0))
            shares_sold = float(resp.get("makingAmount", 0))
            
            # Calculate real P/L
            cost = position["cost"]
            pnl = usdc_received - cost
            
            session_pnl += pnl
            trade_count += 1
            if pnl > 0:
                win_count += 1
            
            emoji = "✅" if pnl > 0 else "❌"
            print(f"    {emoji} SOLD {shares_sold:.2f} shares")
            print(f"    Cost: ${cost:.2f} → Received: ${usdc_received:.2f}")
            print(f"    P/L: ${pnl:+.2f}")
            print(f"    Session: {win_count}/{trade_count} wins | Total: ${session_pnl:+.2f}")
            
            position = None
            return True
        else:
            print(f"    ✗ Sell failed: {resp}")
            return False
            
    except Exception as e:
        print(f"    ✗ Sell error: {e}")
        return False


def check_for_new_candle():
    global last_candle_time, candle_open
    
    new_time, new_open = get_binance_candle()
    
    if new_time is None:
        return False
    
    if new_time != last_candle_time:
        print(f"\n*** NEW CANDLE: ${new_open:,.2f} ***\n")
        last_candle_time = new_time
        candle_open = new_open
        return True
    
    return False


def check_for_new_market():
    global position
    
    is_new = find_active_market()
    
    if is_new:
        if position:
            sell("MARKET CHANGE")
        get_tokens()
        set_allowances()
        return True
    
    return False


async def main():
    global position, candle_open, tokens, last_candle_time
    
    print("=" * 60)
    print("  POLYMARKET BOT v4 (Fixed P/L + Debug)")
    print("=" * 60)
    print(f"Trade: ${TRADE_AMOUNT} | Edge: {EDGE_THRESHOLD*100:.0f}c | TP: +{PROFIT_TARGET*100:.0f}c | SL: -{STOP_LOSS*100:.0f}c")
    print("=" * 60)
    
    find_active_market()
    if not SLUG:
        print("ERROR: No active market!")
        return
    
    get_tokens()
    set_allowances()
    last_candle_time, candle_open = get_binance_candle()
    
    print(f"\nMarket: {SLUG}")
    print(f"Candle: ${candle_open:,.2f}")
    print("\n" + "=" * 60 + "\n")
    
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
                    
                    # Calculate edges
                    up_fair = min(0.85, 0.50 + max(0, btc_change) * 2)
                    dn_fair = min(0.85, 0.50 + max(0, -btc_change) * 2)
                    up_edge = up_fair - m["up"]
                    dn_edge = dn_fair - m["dn"]
                    
                    # Determine trend
                    if btc_change > TREND_THRESHOLD:
                        trend = "UP"
                        trend_token = m["up_t"]
                        trend_price = m["up"]
                        edge = up_edge
                    elif btc_change < -TREND_THRESHOLD:
                        trend = "DN"
                        trend_token = m["dn_t"]
                        trend_price = m["dn"]
                        edge = dn_edge
                    else:
                        trend = "--"
                        trend_token = None
                        trend_price = 0
                        edge = max(up_edge, dn_edge)
                    
                    # HOLDING POSITION
                    if position:
                        curr = m["up"] if position["side"] == "UP" else m["dn"]
                        price_diff = curr - position["entry_price"]
                        est_pnl = price_diff * position["size"]
                        
                        print(f"HOLD {position['side']} | {position['entry_price']*100:.0f}c→{curr*100:.0f}c ({price_diff*100:+.0f}c) | ~${est_pnl:+.2f} | BTC:{btc_change:+.2f}%")
                        
                        # Exit: Profit target
                        if price_diff >= PROFIT_TARGET:
                            sell(f"PROFIT +{price_diff*100:.0f}c")
                        
                        # Exit: Stop loss
                        elif price_diff <= -STOP_LOSS:
                            sell(f"STOP LOSS {price_diff*100:.0f}c")
                        
                        # Exit: Trend flip (tighter - exit when BTC crosses 0)
                        elif position["side"] == "UP" and btc_change < 0:
                            sell("TREND FLIP (BTC negative)")
                        elif position["side"] == "DN" and btc_change > 0:
                            sell("TREND FLIP (BTC positive)")
                    
                    # LOOKING FOR ENTRY
                    else:
                        print(f"BTC:{btc_change:+.2f}% | {trend} | UP:{m['up']*100:.0f}c({up_edge*100:+.0f}c) DN:{m['dn']*100:.0f}c({dn_edge*100:+.0f}c)")
                        
                        # Only enter with strong trend + edge
                        if trend == "UP" and edge >= EDGE_THRESHOLD and m["up"] < 0.80:
                            print(f"*** UP SIGNAL: +{edge*100:.0f}c edge ***")
                            buy(trend_token, trend_price, "UP")
                        
                        elif trend == "DN" and edge >= EDGE_THRESHOLD and m["dn"] < 0.80:
                            print(f"*** DN SIGNAL: +{edge*100:.0f}c edge ***")
                            buy(trend_token, trend_price, "DN")
        
        except Exception as e:
            print(f"\nError: {e}")
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
