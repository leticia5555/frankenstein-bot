with open('bot_v45.py', 'r') as f:
    code = f.read()

# Find and replace the message parsing section
old_parse = '''msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    data = json.loads(msg)
                    
                    if data.get("topic") != "crypto_prices_chainlink":
                        continue'''

new_parse = '''msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    
                    # Skip empty or non-JSON messages
                    if not msg or msg == "":
                        continue
                    try:
                        data = json.loads(msg)
                    except json.JSONDecodeError:
                        continue
                    
                    if data.get("topic") != "crypto_prices_chainlink":
                        continue'''

code = code.replace(old_parse, new_parse)

with open('bot_v45.py', 'w') as f:
    f.write(code)

print("Fixed JSON parsing! Try again.")
