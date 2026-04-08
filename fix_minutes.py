with open('bot_v45.py', 'r') as f:
    code = f.read()

# Fix: Use market timestamp for candle timing, not local time
old_timing = '''def get_minutes_remaining():
    return 15 - get_candle_minutes_elapsed()'''

new_timing = '''def get_minutes_remaining():
    if not candle_start_time:
        return 15
    # candle_start_time is the market's Unix timestamp
    # Market ends 900 seconds (15 min) after that
    market_end = candle_start_time + 900
    now = time.time()
    remaining = (market_end - now) / 60
    return max(0, remaining)'''

code = code.replace(old_timing, new_timing)

with open('bot_v45.py', 'w') as f:
    f.write(code)

print("Fixed minutes calculation!")
