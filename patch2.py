with open('bot_v63.py', 'r') as f:
    code = f.read()

# Fix the candle_open_btc check - replace the old blocking logic
old = '''                    if not market or candle_open_btc is None:
                        await asyncio.sleep(0.1)
                        continue'''

new = '''                    # Wait for market
                    if not market:
                        await asyncio.sleep(0.1)
                        continue
                    
                    # Initialize candle price if joining mid-candle
                    if candle_open_btc is None:
                        candle_open_btc = btc_now
                        log(f"Joined candle | BTC: ${btc_now:,.2f}")'''

if old in code:
    code = code.replace(old, new)
    print("Fixed candle_open_btc!")
else:
    # Try alternate version
    old2 = '''                    if not market or candle_open_btc is None:
                        # Initialize on first valid market + price
                        if market and candle_open_btc is None:
                            candle_open_btc = btc_now
                            log(f"Joined candle | BTC: ${btc_now:,.2f}")
                        else:
                            await asyncio.sleep(0.1)
                            continue'''
    if old2 in code:
        code = code.replace(old2, new)
        print("Fixed candle_open_btc (alt)!")
    else:
        print("Could not find pattern! Searching...")
        # Find what's actually there
        import re
        match = re.search(r'if not market or candle_open_btc.*?continue', code, re.DOTALL)
        if match:
            print(f"Found: {repr(match.group())}")
            code = code.replace(match.group(), new.strip())
            print("Fixed with regex!")
        else:
            print("ERROR: Pattern not found at all")

with open('bot_v63.py', 'w') as f:
    f.write(code)

print("Done! Run: python3 bot_v63.py --trade")
