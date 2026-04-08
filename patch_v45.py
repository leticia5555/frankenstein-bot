import re

with open('bot_v44.py', 'r') as f:
    code = f.read()

# 1. Change version name
code = code.replace('v44 (PROVEN COMBO)', 'v45 (CHAINLINK FIX)')
code = code.replace('GABAGOOL v44', 'GABAGOOL v45')

# 2. Replace the main websocket section
old_ws = '''async with websockets.connect("wss://stream.binance.com:9443/ws/btcusdt@trade") as ws:
                print("Connected to Binance WebSocket")
                
                tick_count = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    
                    tick_count += 1
                    
                    btc_now = float(data["p"])'''

new_ws = '''async with websockets.connect("wss://ws-subscriptions-clob.polymarket.com/ws/rtds") as ws:
                print("Connected to Polymarket RTDS (Chainlink)")
                
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": "{\\"symbol\\":\\"btc/usd\\"}"}]}))
                print("Subscribed to Chainlink BTC/USD")
                
                tick_count = 0
                
                while True:
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    
                    if data.get("topic") != "crypto_prices_chainlink":
                        continue
                    payload = data.get("payload", {})
                    if payload.get("symbol") != "btc/usd":
                        continue
                    
                    tick_count += 1
                    
                    btc_now = float(payload.get("value", 0))'''

code = code.replace(old_ws, new_ws)

with open('bot_v45.py', 'w') as f:
    f.write(code)

print("Created bot_v45.py with Chainlink feed!")
