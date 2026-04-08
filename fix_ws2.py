with open('bot_v45.py', 'r') as f:
    code = f.read()

code = code.replace(
    'wss://ws-subscriptions-clob.polymarket.com/ws/live',
    'wss://ws-live-data.polymarket.com/ws'
)

with open('bot_v45.py', 'w') as f:
    f.write(code)

print("Fixed URL! Try again.")
