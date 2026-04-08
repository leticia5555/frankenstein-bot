with open('bot_v45.py', 'r') as f:
    code = f.read()

old = '''def get_minutes_remaining():
    return 15 - get_candle_minutes_elapsed()'''

new = '''def get_minutes_remaining():
    if not candle_start_time:
        return 15
    market_end = candle_start_time + 900
    remaining = (market_end - time.time()) / 60
    return max(0, remaining)'''

code = code.replace(old, new)

with open('bot_v45.py', 'w') as f:
    f.write(code)

print("Fixed!")
