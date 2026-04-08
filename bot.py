import asyncio, json, websockets, requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType, PartialCreateOrderOptions, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL
import os
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

client = ClobClient(host="https://clob.polymarket.com", key=os.getenv("POLYMARKET_PRIVATE_KEY"), chain_id=137, signature_type=2, funder=os.getenv("POLYMARKET_FUNDER"))
client.set_api_creds(client.create_or_derive_api_creds())

TRADE_AMOUNT = 2
EDGE_THRESHOLD = 0.10
PROFIT_TARGET = 0.04
STOP_LOSS = 0.06

position = None
SLUG = None
tokens = None
candle_open = None
last_candle_time = None

def find_active_market():
    """Find current active BTC 15m market"""
    global SLUG
    r = requests.get("https://gamma-api.polymarket.com/events?active=true&closed=false&limit=50")
    for e in r.json():
        slug = e.get("slug", "")
        if "btc" in slug.lower() and "updown" in slug.lower() and "15m" in slug:
            if slug != SLUG:
                print(f"\n*** NEW MARKET: {slug} ***")
                SLUG = slug
                return True  # New market found
    return False  # Same market

def get_tokens():
    global tokens
    tokens = None  # Reset tokens for new market
    r = requests.get(f"https://gamma-api.polymarket.com/events?slug={SLUG}")
    data = r.json()
    if data:
        m = data[0].get("markets", [{}])[0]
        t = json.loads(m.get("clobTokenIds", "[]")) if isinstance(m.get("clobTokenIds"), str) else m.get("clobTokenIds", [])
        if t:
            tokens = {"up_t": t[0], "dn_t": t[1]}
            return tokens
    return None

def set_allowances():
    t = get_tokens()
    if not t: return
    try:
        params1 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=t["up_t"])
        client.update_balance_allowance(params1)
        params2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=t["dn_t"])
        client.update_balance_allowance(params2)
        print("Allowances set!")
    except Exception as e:
        print(f"Allowance error: {e}")

def get_prices():
    t = tokens
    if not t: return None
    try:
        up = float(client.get_price(t["up_t"], "buy").get("price", 0))
        dn = float(client.get_price(t["dn_t"], "buy").get("price", 0))
        return {"up": up, "dn": dn, "up_t": t["up_t"], "dn_t": t["dn_t"]}
    except:
        return None

def get_binance_candle():
    """Get current 15m candle open price and time"""
    r = requests.get("https://api.binance.com/api/v3/klines", params={"symbol": "BTCUSDT", "interval": "15m", "limit": 1})
    data = r.json()[0]
    open_time = data[0]  # Candle open timestamp
    open_price = float(data[1])
    return open_time, open_price

def buy(token, price, side):
    global position
    try:
        print(f">>> TREND BUY {side} at {price*100:.0f}c")
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=token, amount=TRADE_AMOUNT, side=BUY)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        if resp.get("success") and resp.get("status") == "matched":
            shares = float(resp.get("takingAmount", 0))
            cost = float(resp.get("makingAmount", 0))
            fill_price = cost / shares if shares > 0 else price
            position = {"token": token, "price": fill_price, "size": shares, "side": side, "cost": cost}
            print(f"    FILLED! {shares:.1f} shares at {fill_price*100:.0f}c (${cost:.2f})")
            return True
        else:
            print(f"    Order not filled")
    except Exception as e:
        print(f"    Buy failed: {e}")
    return False

def sell(reason):
    global position
    if not position: return
    try:
        print(f"<<< SELLING {position['side']} - {reason}")
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=position["token"])
        bal = client.get_balance_allowance(params)
        raw_balance = int(bal.get("balance", 0))
        actual_shares = raw_balance / 1_000_000
        
        if actual_shares <= 0:
            print("    No shares!")
            position = None
            return
        
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        mo = MarketOrderArgs(token_id=position["token"], amount=actual_shares, side=SELL)
        order = client.create_market_order(mo, opt)
        resp = client.post_order(order, OrderType.FOK)
        if resp.get("success") and resp.get("status") == "matched":
            received = float(resp.get("makingAmount", 0))
            pnl = received - position["cost"]
            print(f"    SOLD! ${received:.2f}, P/L: ${pnl:+.2f}")
            position = None
        else:
            print(f"    Sell not filled, retrying...")
    except Exception as e:
        print(f"    Sell error: {e}")

async def main():
    global position, candle_open, tokens, last_candle_time
    
    print("=" * 50)
    print("TREND FOLLOWING BOT - BTC 15M (FIXED)")
    print("Strategy: Trade WITH the trend, not against it")
    print(f"Trade: ${TRADE_AMOUNT} | Edge: {EDGE_THRESHOLD*100:.0f}c | Profit: +{PROFIT_TARGET*100:.0f}c | Stop: -{STOP_LOSS*100:.0f}c")
    print("=" * 50)
    
    # Initial setup
    find_active_market()
    get_tokens()
    set_allowances()
    last_candle_time, candle_open = get_binance_candle()
    print(f"Market: {SLUG}")
    print(f"Candle open: ${candle_open:,.2f}")
    print(f"Candle time: {datetime.fromtimestamp(last_candle_time/1000)}\n")
    
    while True:
        try:
            async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade", ping_interval=20, ping_timeout=10) as ws:
                count = 0
                async for msg in ws:
                    count += 1
                    if count % 10 == 0:
                        d = json.loads(msg)
                        btc_now = float(d['p'])
                        
                        # Check for new candle every 50 ticks
                        if count % 50 == 0:
                            new_time, new_open = get_binance_candle()
                            if new_time != last_candle_time:
                                print(f"\n*** NEW CANDLE DETECTED ***")
                                print(f"Old open: ${candle_open:,.2f} -> New open: ${new_open:,.2f}")
                                last_candle_time = new_time
                                candle_open = new_open
                                
                                # Also check for new market
                                if find_active_market():
                                    if position:
                                        sell("NEW MARKET")
                                    get_tokens()
                                    set_allowances()
                                print(f"Market: {SLUG}\n")
                        
                        m = get_prices()
                        if not m or m["up"] == 0:
                            print("Market closed")
                            continue
                        
                        btc_change = ((btc_now - candle_open) / candle_open) * 100
                        
                        if btc_change > 0.05:
                            trend = "UP"
                            trend_token = m["up_t"]
                            trend_price = m["up"]
                            fair = min(0.85, 0.50 + btc_change * 2)
                            edge = fair - m["up"]
                        elif btc_change < -0.05:
                            trend = "DN"
                            trend_token = m["dn_t"]
                            trend_price = m["dn"]
                            fair = min(0.85, 0.50 + abs(btc_change) * 2)
                            edge = fair - m["dn"]
                        else:
                            trend = "FLAT"
                            edge = 0
                        
                        if position:
                            current = m["up"] if position["side"] == "UP" else m["dn"]
                            profit = current - position["price"]
                            pnl = profit * position["size"]
                            
                            print(f"HOLD {position['side']} | Entry:{position['price']*100:.0f}c Now:{current*100:.0f}c | P/L:${pnl:+.2f} | BTC:{btc_change:+.3f}%")
                            
                            if profit >= PROFIT_TARGET:
                                sell("PROFIT TARGET")
                            elif profit <= -STOP_LOSS:
                                sell("STOP LOSS")
                            elif position["side"] == "UP" and btc_change < -0.05:
                                sell("TREND FLIP")
                            elif position["side"] == "DN" and btc_change > 0.05:
                                sell("TREND FLIP")
                        else:
                            print(f"BTC:{btc_change:+.3f}% | Trend:{trend} | UP:{m['up']*100:.0f}c DN:{m['dn']*100:.0f}c | Edge:{edge*100:+.0f}c")
                            
                            if trend == "UP" and edge >= EDGE_THRESHOLD and m["up"] < 0.85:
                                print(f"*** UP trend + {edge*100:.0f}c edge! ***")
                                buy(trend_token, trend_price, "UP")
                            elif trend == "DN" and edge >= EDGE_THRESHOLD and m["dn"] < 0.85:
                                print(f"*** DN trend + {edge*100:.0f}c edge! ***")
                                buy(trend_token, trend_price, "DN")
                                    
        except Exception as e:
            print(f"Reconnecting... {e}")
            await asyncio.sleep(3)

asyncio.run(main())
