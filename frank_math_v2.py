#!/usr/bin/env python3
"""
FRANK MATH v1 — Pure Range Asymmetry Bot
Signal: if max_dn > max_up → bet DN | if max_up > max_dn → bet UP
Entry at T=120s when ratio > 1.5x (90%WR backtested)
Paper trading only.
"""
import asyncio, json, os, time, csv, requests, websockets
from collections import deque
from datetime import datetime, timezone

# ── CONFIG ──
BET           = 10.0
CANDLE_DUR    = 300
RATIO_THRESH  = 1.5    # dominant side must be > other × 1.5
ENTRY_TIME    = 120    # enter at T=120s
MIN_MOVE      = 10     # dominant move must be at least $10
PAPER_ONLY    = True   # no live trades

BINANCE_WS   = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST = "https://api.binance.com/api/v3"
GAMMA        = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"

btc_buffer   = deque(maxlen=500)
pm_buffer    = deque(maxlen=200)
candle_state = {"candle": None}

CSV_FILE = os.path.expanduser("~/polymarket-bot/frank_math_training.csv")
FIELDNAMES = [
    "candle","timestamp_utc","utc_hour","utc_minute","trading_session",
    "elapsed_s","spot_start","spot_chg","up_ask","dn_ask",
    "max_up","max_dn","ratio","signal_dir","signal_price",
    "entry_time","winner","pnl","would_win",
]

# ── CSV ──
def init_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='') as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()

def log_trade(row):
    with open(CSV_FILE, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
        w.writerow(row)

# ── HELPERS ──
def get_btc_now():
    return btc_buffer[-1][1] if btc_buffer else None

def get_pm_prices_now():
    up_ask = dn_ask = None
    for ts, up, dn in reversed(list(pm_buffer)):
        if up is not None and up_ask is None: up_ask = up
        if dn is not None and dn_ask is None: dn_ask = dn
        if up_ask and dn_ask: break
    return up_ask, dn_ask

def get_session(h):
    if   0<=h<7:  return "asia"
    elif 7<=h<12: return "europe"
    elif 12<=h<14: return "us_pre"
    elif 14<=h<20: return "us_open"
    else:          return "after_hours"

def find_candle():
    now = int(time.time())
    for offset in [0, 300, -300]:
        ts = (now // 300) * 300 + offset
        slug = f"btc-updown-5m-{ts}"
        try:
            r = requests.get(f"{GAMMA}/events?slug={slug}", timeout=5)
            d = r.json()
            if d:
                m = d[0].get("markets",[{}])[0]
                if m.get("acceptingOrders") and not m.get("closed"):
                    ids = m.get("clobTokenIds","")
                    t = json.loads(ids) if isinstance(ids,str) else ids
                    if len(t)>=2:
                        return {"question":m.get("question",""), "slug":slug,
                                "end_ts":ts+300, "start_ts":ts,
                                "up_token":t[0], "dn_token":t[1]}
        except: continue
    return None

def get_clob_prices(up_token, dn_token):
    up = dn = None
    for token, side in [(up_token,"up"),(dn_token,"dn")]:
        if not token: continue
        try:
            r = requests.get(f"{CLOB_API}/price",
                params={"token_id": token, "side": "buy"}, timeout=3)
            p = float(r.json().get("price",0))
            if 0.01<p<0.99:
                if side=="up": up=p
                else: dn=p
        except: pass
    return up, dn

def settle_candle(candle_info, signal_dir, signal_price, max_up, max_dn, ratio,
                  spot_start, candle_num, session, entry_elapsed):
    """Check result and log it."""
    if not candle_info: return
    slug = candle_info.get("slug","")
    try:
        r = requests.get(f"{GAMMA}/events?slug={slug}", timeout=5)
        d = r.json()
        if not d: return
        m = d[0].get("markets",[{}])[0]
        outcome = m.get("outcomePrices","")
        if not outcome: return
        prices = json.loads(outcome) if isinstance(outcome,str) else outcome
        if len(prices)<2: return
        up_price = float(prices[0])
        winner = "UP" if up_price > 0.5 else "DN"
        final_btc = get_btc_now()
        final_chg = (final_btc - spot_start) if final_btc and spot_start else 0

        would_win = signal_dir == winner if signal_dir else None
        if would_win is True:
            pnl = round(BET/signal_price - BET, 2)
            print(f"  ★ PAPER WIN:  {signal_dir} @ {signal_price:.0%} | +${pnl:.2f}")
        elif would_win is False:
            pnl = -BET
            print(f"  ✗ PAPER LOSS: {signal_dir} @ {signal_price:.0%} | -${BET:.2f}")
        else:
            pnl = 0

        now_utc = datetime.now(timezone.utc)
        log_trade({
            "candle": candle_num,
            "timestamp_utc": now_utc.isoformat(),
            "utc_hour": now_utc.hour,
            "utc_minute": now_utc.minute,
            "trading_session": session,
            "elapsed_s": entry_elapsed,
            "spot_start": round(spot_start,2) if spot_start else "",
            "spot_chg": round(final_chg,2),
            "up_ask": signal_price if signal_dir=="UP" else 1-(signal_price or 0.5),
            "dn_ask": signal_price if signal_dir=="DN" else 1-(signal_price or 0.5),
            "max_up": round(max_up,2),
            "max_dn": round(max_dn,2),
            "ratio": round(ratio,3),
            "signal_dir": signal_dir or "NONE",
            "signal_price": round(signal_price,3) if signal_price else "",
            "entry_time": entry_elapsed,
            "winner": winner,
            "pnl": pnl,
            "would_win": would_win,
        })
        return winner, final_chg, pnl, would_win
    except Exception as e:
        return None, 0, 0, None

# ── FEEDS ──
async def binance_feed(shutdown_event):
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                print("  ✅ Binance WS connected")
                while not shutdown_event.is_set():
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    d = json.loads(msg)
                    btc_buffer.append((int(d["T"]), float(d["p"])))
        except: pass
        if not shutdown_event.is_set():
            await asyncio.sleep(2)

async def clob_poller(shutdown_event):
    """REST-only CLOB — reliable, no WebSocket drops."""
    print("  ✅ CLOB REST poller started (every 3s)")
    while not shutdown_event.is_set():
        try:
            c = candle_state.get("candle")
            if c and c.get("up_token"):
                loop = asyncio.get_event_loop()
                fu, fd = await loop.run_in_executor(
                    None, get_clob_prices, c["up_token"], c.get("dn_token"))
                if fu and fd and 0.01<fu<0.99 and 0.01<fd<0.99:
                    ts_ms = int(time.time()*1000)
                    pm_buffer.append((ts_ms, fu, None))
                    pm_buffer.append((ts_ms, None, fd))
        except: pass
        await asyncio.sleep(3)

# ── MAIN LOOP ──
async def main_loop(shutdown_event):
    candle_num = 0
    session_wins = session_losses = 0
    session_pnl = 0.0

    print(f"\n{'═'*65}")
    print(f"  🧠 FRANK MATH v1 — Range Asymmetry Bot")
    print(f"  Signal: max_up vs max_dn | ratio>{RATIO_THRESH}x | entry@T={ENTRY_TIME}s")
    print(f"  Bet: ${BET} | PAPER ONLY")
    print(f"{'═'*65}")

    # Wait for BTC feed
    for _ in range(30):
        if btc_buffer: break
        await asyncio.sleep(1)
    print(f"  ✅ BTC: ${get_btc_now():,.2f}")

    # Track next candle boundary
    now = time.time()
    next_candle_ts = (int(now) - (int(now) % CANDLE_DUR)) + CANDLE_DUR

    while not shutdown_event.is_set():
        candle_num += 1

        # Wait for candle boundary
        now = time.time()
        wait_for = next_candle_ts - now
        if wait_for > 5:
            label = datetime.fromtimestamp(next_candle_ts).strftime("%I:%M %p")
            print(f"\n  ⏳ Waiting for candle at {label} ({wait_for:.0f}s)...")

        # Prefetch during wait
        if wait_for > 3:
            pre_wait = max(0, wait_for - 3.0)
            await asyncio.sleep(pre_wait)
            remaining = next_candle_ts - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
        else:
            await asyncio.sleep(max(0, wait_for))

        # ── T=0 ──
        spot_start = get_btc_now()
        candle_start_ts = next_candle_ts

        # Update next candle ts from real time
        _now = time.time()
        _slot = int(_now) - (int(_now) % CANDLE_DUR)
        next_candle_ts = _slot + CANDLE_DUR

        utc_hour = datetime.now(timezone.utc).hour
        utc_min  = datetime.now(timezone.utc).minute
        session = get_session(utc_hour)
        candle_end_ts = candle_start_ts + CANDLE_DUR

        # Fetch candle info + CLOB
        loop = asyncio.get_event_loop()
        candle_info = await loop.run_in_executor(None, find_candle)
        candle_state["candle"] = candle_info

        up_ask_init = dn_ask_init = None
        if candle_info and candle_info.get("up_token"):
            up_ask_init, dn_ask_init = await loop.run_in_executor(
                None, get_clob_prices,
                candle_info["up_token"], candle_info.get("dn_token"))
            if up_ask_init and dn_ask_init:
                ts_now = int(time.time()*1000)
                pm_buffer.clear()
                pm_buffer.append((ts_now, up_ask_init, None))
                pm_buffer.append((ts_now, None, dn_ask_init))

        now_label = datetime.fromtimestamp(candle_start_ts).strftime("%I:%M %p")
        end_label = datetime.fromtimestamp(candle_end_ts).strftime("%I:%M %p")
        print(f"\n{'━'*65}")
        print(f"  CANDLE #{candle_num} | {now_label} → {end_label} | {session}")
        print(f"{'━'*65}")
        if candle_info: print(f"  📋 {candle_info['question']}")
        print(f"  💰 BTC Start: ${spot_start:,.2f}")
        if up_ask_init: print(f"  📦 CLOB: UP={up_ask_init*100:.0f}c DN={dn_ask_init*100:.0f}c")
        print(f"\n  {'Time':>5}  {'BTC Δ':>7}  {'max_up':>7}  {'max_dn':>7}  {'ratio':>6}  {'signal':>8}  {'UP':>4}  {'DN':>4}")
        print(f"  {'─'*65}")

        # Candle tracking
        max_up_so_far = 0.0
        max_dn_so_far = 0.0
        signal_fired = False
        signal_dir = None
        signal_price = None
        signal_elapsed = None
        last_print_s = -1

        while True:
            now = time.time()
            elapsed = now - candle_start_ts
            remaining = candle_end_ts - now
            if remaining <= 4: break

            spot_now = get_btc_now() or spot_start
            spot_chg = spot_now - spot_start if spot_start else 0

            # Update max moves
            if spot_chg > max_up_so_far: max_up_so_far = spot_chg
            if spot_chg < -max_dn_so_far: max_dn_so_far = abs(spot_chg)

            # Compute ratio
            dominant = max(max_up_so_far, max_dn_so_far)
            other    = min(max_up_so_far, max_dn_so_far)
            ratio = dominant / other if other > 0.5 else dominant / 0.5

            # Get CLOB
            up_ask, dn_ask = get_pm_prices_now()
            if not up_ask: up_ask = up_ask_init or 0.5
            if not dn_ask: dn_ask = dn_ask_init or 0.5

            # ── SIGNAL CHECK at T=ENTRY_TIME ──
            if not signal_fired and elapsed >= ENTRY_TIME:
                direction = None
                if max_up_so_far > max_dn_so_far * RATIO_THRESH and max_up_so_far >= MIN_MOVE:
                    direction = 'UP'
                    price = up_ask
                elif max_dn_so_far > max_up_so_far * RATIO_THRESH and max_dn_so_far >= MIN_MOVE:
                    direction = 'DN'
                    price = dn_ask

                if direction and price and 0.01<price<0.99:
                    signal_fired = True
                    signal_dir = direction
                    signal_price = price
                    signal_elapsed = int(elapsed)
                    win_amt = round(BET/price-BET,2)
                    print(f"\n  📊 SIGNAL → {direction} @ {price*100:.0f}c | ratio={ratio:.1f}x")
                    print(f"     max_up=${max_up_so_far:.0f} max_dn=${max_dn_so_far:.0f}")
                    print(f"     PAPER: would win +${win_amt:.2f} | would lose -${BET:.2f}\n")
                elif not signal_fired and elapsed >= ENTRY_TIME + 5:
                    # No signal — ratio not met
                    signal_fired = True  # don't check again
                    print(f"\n  ⏭️  NO SIGNAL | ratio={ratio:.1f}x max_up=${max_up_so_far:.0f} max_dn=${max_dn_so_far:.0f}\n")

            # Print every 10s
            if int(elapsed) % 10 == 0 and int(elapsed) != last_print_s:
                last_print_s = int(elapsed)
                sig_str = f"→{signal_dir}" if signal_dir else "—"
                up_s = f"{up_ask*100:.0f}c" if up_ask else "—"
                dn_s = f"{dn_ask*100:.0f}c" if dn_ask else "—"
                print(f"  {elapsed:>5.0f}s  {spot_chg:>+7.0f}  {max_up_so_far:>7.0f}  {max_dn_so_far:>7.0f}  {ratio:>6.1f}x  {sig_str:>8}  {up_s:>4}  {dn_s:>4}")

            await asyncio.sleep(1)

        # ── RESULT ──
        spot_final = get_btc_now() or spot_start
        final_chg = (spot_final - spot_start) if spot_start else 0
        dominant = max(max_up_so_far, max_dn_so_far)
        other    = min(max_up_so_far, max_dn_so_far)
        final_ratio = dominant/other if other>0.5 else dominant/0.5

        print(f"\n  ═══════════════════════════════════════════")
        print(f"  BTC: {final_chg:>+.0f} | max_up=${max_up_so_far:.0f} max_dn=${max_dn_so_far:.0f} ratio={final_ratio:.1f}x")

        # Settle
        await asyncio.sleep(8)
        result = settle_candle(
            candle_info, signal_dir, signal_price,
            max_up_so_far, max_dn_so_far, final_ratio,
            spot_start, candle_num, session,
            signal_elapsed or int(ENTRY_TIME))

        if result:
            winner, chg, pnl, would_win = result
            print(f"  RESULT: {'🟢 UP' if winner=='UP' else '🔴 DN'} | BTC: {chg:>+.0f}")
            if would_win is True: session_wins+=1; session_pnl+=pnl
            elif would_win is False: session_losses+=1; session_pnl-=BET

        total = session_wins+session_losses
        wr = 100*session_wins//total if total else 0
        print(f"  📊 Session: {session_wins}W/{session_losses}L ({wr}%) | P&L: ${session_pnl:>+.2f}")

        candle_state["candle"] = None

async def main():
    init_csv()
    shutdown_event = asyncio.Event()
    try:
        await asyncio.gather(
            binance_feed(shutdown_event),
            clob_poller(shutdown_event),
            main_loop(shutdown_event),
        )
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        shutdown_event.set()

if __name__ == "__main__":
    asyncio.run(main())
EOF
