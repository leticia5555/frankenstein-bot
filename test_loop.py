import requests, time, asyncio, websockets, json

# Get market
ts = (int(time.time()) // 900) * 900
slug = f'btc-updown-15m-{ts}'
r = requests.get(f'https://gamma-api.polymarket.com/events?slug={slug}')
data = r.json()

# clobTokenIds is a JSON string, need to parse it
token_str = data[0]['markets'][0]['clobTokenIds']
if isinstance(token_str, str):
    tokens = json.loads(token_str)
else:
    tokens = token_str

print(f'Token UP: {tokens[0][:30]}...')
print(f'Token DN: {tokens[1][:30]}...')

async def loop():
    async with websockets.connect('wss://stream.binance.com:9443/ws/btcusdt@trade') as ws:
        for i in range(5):
            msg = await ws.recv()
            btc = float(json.loads(msg)['p'])
            
            up_r = requests.get(f'https://clob.polymarket.com/price?token_id={tokens[0]}&side=buy')
            dn_r = requests.get(f'https://clob.polymarket.com/price?token_id={tokens[1]}&side=buy')
            up = float(up_r.json().get('price', 0))
            dn = float(dn_r.json().get('price', 0))
            
            print(f'BTC: ${btc:,.0f} | UP: {up*100:.0f}c | DN: {dn*100:.0f}c')

asyncio.run(loop())
