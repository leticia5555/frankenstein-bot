with open('bot_v45.py', 'r') as f:
    code = f.read()

# Try different RTDS endpoints
code = code.replace(
    'wss://ws-subscriptions-clob.polymarket.com/ws/rtds',
    'wss://ws-subscriptions-clob.polymarket.com/ws/live'
)

with open('bot_v45.py', 'w') as f:
    f.write(code)

print("Fixed! Try running again.")
