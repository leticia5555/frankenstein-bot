import re

# Read current bot
with open('bot_v63.py', 'r') as f:
    code = f.read()

# Fix 1: Add debug prints after websocket connect
old = 'async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:\n                log("Connected to Binance'
new = 'async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:\n                print("[DEBUG] WS connected!", flush=True)\n                log("Connected to Binance'
code = code.replace(old, new)

# Fix 2: Add debug after first recv
old = '''                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    
                    ta.update(btc_now)

                    is_new, market = market_intel.find_active_market()'''

new = '''                recv_count = 0
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    btc_now = float(data["p"])
                    recv_count += 1
                    if recv_count <= 3:
                        print(f"[DEBUG] msg#{recv_count} BTC=${btc_now:,.0f} market={market is not None} open={candle_open_btc}", flush=True)
                    
                    ta.update(btc_now)

                    is_new, market = market_intel.find_active_market()'''
code = code.replace(old, new)

with open('bot_v63.py', 'w') as f:
    f.write(code)

print("Patched! Run: python3 bot_v63.py --trade")
