#!/usr/bin/env python3
"""
FRANK MEGA COLLECTOR v1
Combines the best data from 3 repos into one collector.
Saves ~80 columns per row — pure data collection, no trading.

Sources:
  - Chainlink oracle (exact Polymarket settlement price)
  - Binance (BTC spot + orderbook + CVD + ATR + RVOL)
  - Polymarket CLOB (5-level orderbook depth, microprice, eat-flow)
  - Black-Scholes binary probability + EWMA volatility
  - Range asymmetry + commitment + velocity (our math signals)
"""

import asyncio, json, os, time, csv, math, requests, websockets
from collections import deque
from datetime import datetime, timezone
import numpy as np

# ── CONFIG ──
CANDLE_DUR   = 300
CSV_FILE     = os.path.expanduser("~/polymarket-bot/frank_mega_training.csv")
BINANCE_WS   = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST = "https://api.binance.com/api/v3"
CHAINLINK_WS = "wss://ws-live-data.polymarket.com"
GAMMA        = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
FAPI         = "https://fapi.binance.com"

# ── BUFFERS ──
btc_ticks    = deque(maxlen=2000)   # (timestamp_ms, price)
cl_ticks     = deque(maxlen=500)    # (timestamp_ms, price) chainlink
pm_buffer    = deque(maxlen=200)    # (timestamp_ms, up_ask, dn_ask)
candle_state = {"candle": None}

# ── CSV COLUMNS ──
FIELDNAMES = [
    # identity
    "candle","row","timestamp_utc","timestamp_ms",
    "utc_hour","utc_minute","day_of_week","trading_session","elapsed_s",
    # BTC price
    "btc_binance","btc_chainlink","btc_spread_bps","btc_spot_chg",
    "spot_start","spot_now",
    # BTC returns (like polyrec)
    "ret_1s","ret_5s","ret_10s","ret_30s","ret_60s",
    # BTC volatility
    "ewma_vol","atr_30s","rvol_30s","rvol_60s",
    # BTC volume / CVD
    "volume_1s","volume_5s","cvd_1s","cvd_5s","cvd_30s",
    # BTC momentum
    "roc_1m","roc_5m","momentum_short",
    # Candle running stats
    "max_up","max_dn","ratio","dominance","commitment","velocity",
    # Black-Scholes
    "bs_prob_up","bs_d2","bs_sigma","bs_T_remaining",
    # Polymarket CLOB
    "up_ask","dn_ask","pair_cost","clob_spread","clob_midprice",
    # 5-level orderbook UP side
    "ob_up_bid1","ob_up_bid1_sz","ob_up_bid2","ob_up_bid2_sz",
    "ob_up_ask1","ob_up_ask1_sz","ob_up_ask2","ob_up_ask2_sz",
    # 5-level orderbook DN side
    "ob_dn_bid1","ob_dn_bid1_sz","ob_dn_ask1","ob_dn_ask1_sz",
    # CLOB microstructure
    "ob_imbalance","ob_microprice","ob_eat_flow",
    "ob_bid_depth","ob_ask_depth",
    # Taker ratio / OI / funding (futures)
    "taker_ratio","oi_change_pct","funding_rate","futures_basis",
    # Binance spot orderbook
    "spot_ob_bid","spot_ob_ask","spot_ob_spread","spot_ob_imbalance",
    # outcome
    "winner","final_spot_chg",
]

# ── EWMA VOLATILITY (lambda=0.94, per-second) ──
ewma_var = None
ewma_lambda = 0.94
last_price_for_ewma = None

def update_ewma(price):
    global ewma_var, last_price_for_ewma
    if last_price_for_ewma is None:
        last_price_for_ewma = price
        return 0.0
    r = (price - last_price_for_ewma) / last_price_for_ewma
    dt = 1.0
    r2_per_s = r**2 / dt
    if ewma_var is None:
        ewma_var = r2_per_s
    else:
        ewma_var = ewma_lambda * ewma_var + (1 - ewma_lambda) * r2_per_s
    last_price_for_ewma = price
    return math.sqrt(ewma_var) if ewma_var > 0 else 0.0

# ── BLACK-SCHOLES BINARY ──
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs_binary_prob(spot, strike, sigma_per_s, T_seconds):
    """P(BTC > strike) using Black-Scholes binary option."""
    if sigma_per_s <= 0 or T_seconds <= 0 or strike <= 0:
        return 0.5, 0.0
    sigma_T = sigma_per_s * math.sqrt(T_seconds)
    if sigma_T <= 0:
        return 0.5, 0.0
    d2 = (math.log(spot / strike) - 0.5 * sigma_per_s**2 * T_seconds) / sigma_T
    return norm_cdf(d2), d2

# ── ATR / RVOL ──
def compute_atr(ticks, window_s=30):
    now = time.time() * 1000
    recent = [p for t,p in ticks if (now-t) < window_s*1000]
    if len(recent) < 2: return 0.0
    ranges = [abs(recent[i]-recent[i-1]) for i in range(1,len(recent))]
    return np.mean(ranges) if ranges else 0.0

def compute_rvol(ticks, window_s=30, baseline_s=300):
    now = time.time() * 1000
    recent_vol = len([p for t,p in ticks if (now-t) < window_s*1000])
    baseline_vol = len([p for t,p in ticks if (now-t) < baseline_s*1000])
    baseline_rate = baseline_vol / (baseline_s / window_s) if baseline_vol else 1
    return recent_vol / baseline_rate if baseline_rate > 0 else 1.0

def compute_cvd(ticks, window_s=5):
    """Cumulative Volume Delta — approximate from price direction."""
    now = time.time() * 1000
    recent = [(t,p) for t,p in ticks if (now-t) < window_s*1000]
    if len(recent) < 2: return 0.0
    cvd = 0.0
    for i in range(1, len(recent)):
        dp = recent[i][1] - recent[i-1][1]
        cvd += dp  # positive = buy pressure
    return cvd

def get_returns(ticks, seconds):
    """Return over last N seconds."""
    now = time.time() * 1000
    cutoff = now - seconds * 1000
    recent = [p for t,p in ticks if t >= cutoff]
    old = [p for t,p in ticks if t < cutoff]
    if not recent or not old:
        return 0.0
    return (recent[-1] - old[-1]) / old[-1] * 100

# ── HELPERS ──
def get_btc_now():
    return btc_ticks[-1][1] if btc_ticks else None

def get_chainlink_now():
    return cl_ticks[-1][1] if cl_ticks else None

def get_pm_prices():
    if not pm_buffer: return None, None
    up = dn = None
    for ts, u, d in reversed(list(pm_buffer)):
        if u is not None and up is None: up = u
        if d is not None and dn is None: dn = d
        if up and dn: break
    return up, dn

def get_session(h):
    if   0<=h<7:   return "asia"
    elif 7<=h<12:  return "europe"
    elif 12<=h<14: return "eu_us"
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
                        return {"slug":slug,"question":m.get("question",""),
                                "end_ts":ts+300,"start_ts":ts,
                                "up_token":t[0],"dn_token":t[1]}
        except: continue
    return None

def get_clob_rest(up_token, dn_token):
    """Fetch CLOB prices."""
    up = dn = None
    for tok, side in [(up_token,"up"),(dn_token,"dn")]:
        if not tok: continue
        try:
            r = requests.get(f"{CLOB_API}/price",
                params={"token_id":tok,"side":"buy"}, timeout=3)
            p = float(r.json().get("price",0))
            if 0.01<p<0.99:
                if side=="up": up=p
                else: dn=p
        except: pass
    return up, dn

def get_clob_orderbook(token_id):
    """Fetch full orderbook for a token."""
    try:
        r = requests.get(f"{CLOB_API}/book",
            params={"token_id":token_id}, timeout=3)
        return r.json()
    except: return {}

def get_futures_data():
    result = {}
    try:
        r = requests.get(f"{FAPI}/fapi/v1/premiumIndex",
            params={"symbol":"BTCUSDT"}, timeout=3)
        d = r.json()
        result["funding_rate"] = float(d.get("lastFundingRate",0))*100
        result["futures_basis"] = float(d.get("markPrice",0)) - float(d.get("indexPrice",0))
    except: pass
    try:
        r = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
            params={"symbol":"BTCUSDT","period":"5m","limit":1}, timeout=3)
        d = r.json()
        if d: result["taker_ratio"] = float(d[0].get("buySellRatio",1.0))
    except: pass
    try:
        r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol":"BTCUSDT","period":"5m","limit":3}, timeout=3)
        d = r.json()
        if d and len(d)>=2:
            oi1 = float(d[-1]["sumOpenInterestValue"])
            oi2 = float(d[-2]["sumOpenInterestValue"])
            result["oi_change_pct"] = (oi1-oi2)/oi2*100 if oi2 else 0
    except: pass
    return result

def get_spot_orderbook():
    try:
        r = requests.get(f"{BINANCE_REST}/depth",
            params={"symbol":"BTCUSDT","limit":5}, timeout=3)
        ob = r.json()
        bids = ob.get("bids",[])
        asks = ob.get("asks",[])
        if bids and asks:
            bid_p = float(bids[0][0]); bid_s = float(bids[0][1])
            ask_p = float(asks[0][0]); ask_s = float(asks[0][1])
            total = bid_s + ask_s
            return {
                "bid": bid_p, "ask": ask_p,
                "spread": ask_p - bid_p,
                "imbalance": bid_s/total if total else 0.5
            }
    except: pass
    return {}

def parse_ob_levels(ob, side="bids", n=2):
    """Extract N levels from orderbook."""
    levels = ob.get(side, [])
    result = {}
    for i in range(n):
        if i < len(levels):
            result[f"p{i+1}"] = float(levels[i].get("price", levels[i][0] if isinstance(levels[i],list) else 0))
            result[f"s{i+1}"] = float(levels[i].get("size", levels[i][1] if isinstance(levels[i],list) else 0))
        else:
            result[f"p{i+1}"] = 0; result[f"s{i+1}"] = 0
    return result

def compute_microprice(bids, asks):
    """Microprice = weighted mid price by size."""
    if not bids or not asks: return 0.5
    try:
        bp = float(bids[0].get("price",0)); bs = float(bids[0].get("size",1))
        ap = float(asks[0].get("price",0)); as_ = float(asks[0].get("size",1))
        total = bs + as_
        return (bp*as_ + ap*bs) / total if total > 0 else (bp+ap)/2
    except: return 0.5

def compute_eat_flow(ob_history):
    """Eat-flow: net aggressive order flow in last N updates."""
    if len(ob_history) < 2: return 0.0
    # Simplified: change in best ask size (negative = asks being eaten)
    prev = ob_history[-2]; curr = ob_history[-1]
    try:
        prev_ask_sz = float(prev.get("asks",[{}])[0].get("size",0)) if prev.get("asks") else 0
        curr_ask_sz = float(curr.get("asks",[{}])[0].get("size",0)) if curr.get("asks") else 0
        return prev_ask_sz - curr_ask_sz  # positive = asks being consumed = buy pressure
    except: return 0.0

def init_csv():
    if not os.path.exists(CSV_FILE):
        with open(CSV_FILE, 'w', newline='') as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()
        print(f"  📁 Created {CSV_FILE}")

def save_row(row):
    with open(CSV_FILE, 'a', newline='') as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction='ignore')
        w.writerow({k: (round(v,6) if isinstance(v,float) else v)
                    for k,v in row.items()})

# ── FEEDS ──
async def binance_feed(shutdown_event):
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                print("  ✅ Binance WS connected")
                while not shutdown_event.is_set():
                    msg = await asyncio.wait_for(ws.recv(), timeout=10)
                    d = json.loads(msg)
                    price = float(d["p"])
                    ts = int(d["T"])
                    btc_ticks.append((ts, price))
                    update_ewma(price)
        except: pass
        if not shutdown_event.is_set():
            await asyncio.sleep(2)

async def chainlink_feed(shutdown_event):
    """Subscribe to Polymarket's Chainlink oracle — exact settlement price."""
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(CHAINLINK_WS, ping_interval=20) as ws:
                # Subscribe to BTC/USD Chainlink feed
                sub = json.dumps({"type":"subscribe","channel":"crypto_prices_chainlink",
                                  "assets":["btc/usd"]})
                await ws.send(sub)
                print("  ✅ Chainlink oracle connected")
                while not shutdown_event.is_set():
                    msg = await asyncio.wait_for(ws.recv(), timeout=30)
                    d = json.loads(msg)
                    if isinstance(d, dict):
                        price = d.get("price") or d.get("p") or d.get("data",{}).get("price")
                        if price:
                            cl_ticks.append((int(time.time()*1000), float(price)))
                    elif isinstance(d, list):
                        for item in d:
                            price = item.get("price") or item.get("p")
                            if price:
                                cl_ticks.append((int(time.time()*1000), float(price)))
        except Exception as e:
            pass
        if not shutdown_event.is_set():
            await asyncio.sleep(3)

async def clob_poller(shutdown_event):
    """REST CLOB polling every 3s."""
    while not shutdown_event.is_set():
        try:
            c = candle_state.get("candle")
            if c and c.get("up_token"):
                loop = asyncio.get_event_loop()
                fu, fd = await loop.run_in_executor(
                    None, get_clob_rest, c["up_token"], c.get("dn_token"))
                if fu and fd and 0.01<fu<0.99 and 0.01<fd<0.99:
                    pm_buffer.append((int(time.time()*1000), fu, fd))
        except: pass
        await asyncio.sleep(3)

# ── MAIN COLLECTION LOOP ──
async def main_loop(shutdown_event):
    candle_num = 0
    row_num = 0
    ob_history = []

    print(f"\n{'═'*65}")
    print(f"  📡 FRANK MEGA COLLECTOR v1")
    print(f"  Collecting ~80 columns per second")
    print(f"  Saving to: {CSV_FILE}")
    print(f"{'═'*65}")

    for _ in range(30):
        if btc_ticks: break
        await asyncio.sleep(1)
    print(f"  ✅ BTC: ${get_btc_now():,.2f}")

    now = time.time()
    next_candle_ts = (int(now) - (int(now) % CANDLE_DUR)) + CANDLE_DUR

    while not shutdown_event.is_set():
        candle_num += 1

        now = time.time()
        wait_for = next_candle_ts - now
        if wait_for > 5:
            label = datetime.fromtimestamp(next_candle_ts).strftime("%I:%M %p")
            print(f"\n  ⏳ Waiting for candle at {label} ({wait_for:.0f}s)...")

        if wait_for > 3:
            pre_wait = max(0, wait_for - 3.0)
            await asyncio.sleep(pre_wait)
            # Prefetch slow data during wait
            loop = asyncio.get_event_loop()
            futures_data = await loop.run_in_executor(None, get_futures_data)
            remaining = next_candle_ts - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
        else:
            await asyncio.sleep(max(0, wait_for))
            futures_data = {}

        # ── T=0 ──
        spot_start = get_btc_now()
        candle_start_ts = next_candle_ts
        candle_end_ts = candle_start_ts + CANDLE_DUR

        _now = time.time()
        _slot = int(_now) - (int(_now) % CANDLE_DUR)
        next_candle_ts = _slot + CANDLE_DUR

        utc_now = datetime.now(timezone.utc)
        session = get_session(utc_now.hour)

        loop = asyncio.get_event_loop()
        candle_info = await loop.run_in_executor(None, find_candle)
        candle_state["candle"] = candle_info

        # Initial CLOB fetch
        up_init = dn_init = None
        if candle_info and candle_info.get("up_token"):
            up_init, dn_init = await loop.run_in_executor(
                None, get_clob_rest,
                candle_info["up_token"], candle_info.get("dn_token"))
            if up_init and dn_init:
                pm_buffer.append((int(time.time()*1000), up_init, dn_init))

        strike = spot_start  # Polymarket strike = BTC price at candle start

        print(f"\n{'━'*65}")
        q = candle_info['question'] if candle_info else '?'
        print(f"  CANDLE #{candle_num} | {datetime.fromtimestamp(candle_start_ts).strftime('%I:%M %p')} | {session}")
        print(f"  📋 {q}")
        print(f"  💰 BTC: ${spot_start:,.2f} | CLOB: UP={up_init*100:.0f}c DN={dn_init*100:.0f}c" if up_init else f"  💰 BTC: ${spot_start:,.2f}")
        print(f"  Collecting data...")

        # Running candle stats
        max_up_so_far = 0.0
        max_dn_so_far = 0.0
        last_save_s = -1

        while True:
            now = time.time()
            elapsed = now - candle_start_ts
            remaining = candle_end_ts - now
            if remaining <= 4: break

            spot_now = get_btc_now() or spot_start
            cl_now   = get_chainlink_now()
            spot_chg = spot_now - spot_start if spot_start else 0

            # Update max moves
            if spot_chg > max_up_so_far: max_up_so_far = spot_chg
            if spot_chg < -max_dn_so_far: max_dn_so_far = abs(spot_chg)

            # Only save every second
            if int(elapsed) == last_save_s:
                await asyncio.sleep(0.1)
                continue
            last_save_s = int(elapsed)

            # ── COMPUTE ALL FEATURES ──

            # Returns
            ret_1s  = get_returns(btc_ticks, 1)
            ret_5s  = get_returns(btc_ticks, 5)
            ret_10s = get_returns(btc_ticks, 10)
            ret_30s = get_returns(btc_ticks, 30)
            ret_60s = get_returns(btc_ticks, 60)

            # Volatility
            sigma = update_ewma(spot_now)
            atr   = compute_atr(btc_ticks, 30)
            rvol30 = compute_rvol(btc_ticks, 30, 300)
            rvol60 = compute_rvol(btc_ticks, 60, 300)

            # CVD
            cvd_1s  = compute_cvd(btc_ticks, 1)
            cvd_5s  = compute_cvd(btc_ticks, 5)
            cvd_30s = compute_cvd(btc_ticks, 30)

            # Volume
            t_now = time.time()*1000
            vol_1s = len([p for t,p in btc_ticks if t_now-t < 1000])
            vol_5s = len([p for t,p in btc_ticks if t_now-t < 5000])

            # Momentum
            roc_1m = get_returns(btc_ticks, 60)
            roc_5m = get_returns(btc_ticks, 300)
            mom = (spot_now - (btc_ticks[-6][1] if len(btc_ticks)>=6 else spot_now)) / spot_now if spot_now else 0

            # Range asymmetry
            total_move = max_up_so_far + max_dn_so_far
            dominance  = (max_up_so_far - max_dn_so_far) / total_move if total_move > 0 else 0
            dominant   = max(max_up_so_far, max_dn_so_far)
            other      = min(max_up_so_far, max_dn_so_far)
            ratio      = dominant / other if other > 0.5 else dominant / 0.5
            commitment = abs(spot_chg) / dominant if dominant > 0 else 0
            velocity   = dominant / max(elapsed, 1)

            # Black-Scholes
            T_remaining = remaining
            bs_prob, bs_d2 = bs_binary_prob(spot_now, strike, sigma, T_remaining)

            # CLOB prices
            up_ask, dn_ask = get_pm_prices()
            if not up_ask: up_ask = up_init or 0.5
            if not dn_ask: dn_ask = dn_init or 0.5
            clob_spread = up_ask - dn_ask if up_ask and dn_ask else 0
            clob_mid = (up_ask + dn_ask) / 2 if up_ask and dn_ask else 0.5

            # Chainlink spread
            btc_cl_spread = 0
            if cl_now and spot_now:
                btc_cl_spread = abs(spot_now - cl_now) / spot_now * 10000  # bps

            # CLOB orderbook (async fetch every 10s)
            ob_up = ob_dn = {}
            if int(elapsed) % 10 == 0 and candle_info:
                try:
                    ob_up = await loop.run_in_executor(None, get_clob_orderbook, candle_info["up_token"])
                    ob_dn = await loop.run_in_executor(None, get_clob_orderbook, candle_info["dn_token"])
                    ob_history.append(ob_up)
                    if len(ob_history) > 10: ob_history.pop(0)
                except: pass

            up_bids = parse_ob_levels(ob_up, "bids", 2)
            up_asks = parse_ob_levels(ob_up, "asks", 2)
            dn_bids = parse_ob_levels(ob_dn, "bids", 1)
            dn_asks = parse_ob_levels(ob_dn, "asks", 1)

            ob_bid_depth = sum(up_bids.get(f"s{i}",0) for i in range(1,3))
            ob_ask_depth = sum(up_asks.get(f"s{i}",0) for i in range(1,3))
            ob_imb = ob_bid_depth/(ob_bid_depth+ob_ask_depth) if (ob_bid_depth+ob_ask_depth)>0 else 0.5

            up_bids_raw = ob_up.get("bids",[])
            up_asks_raw = ob_up.get("asks",[])
            microprice = compute_microprice(up_bids_raw, up_asks_raw) if up_bids_raw else up_ask or 0.5
            eat_flow = compute_eat_flow(ob_history)

            # Spot orderbook (every 10s)
            spot_ob = {}
            if int(elapsed) % 10 == 0:
                spot_ob = await loop.run_in_executor(None, get_spot_orderbook)

            # Build row
            row_num += 1
            row = {
                "candle": candle_num, "row": row_num,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "timestamp_ms": int(time.time()*1000),
                "utc_hour": utc_now.hour, "utc_minute": utc_now.minute,
                "day_of_week": utc_now.strftime("%A"),
                "trading_session": session, "elapsed_s": round(elapsed,1),
                # BTC
                "btc_binance": round(spot_now,2) if spot_now else "",
                "btc_chainlink": round(cl_now,2) if cl_now else "",
                "btc_spread_bps": round(btc_cl_spread,2),
                "btc_spot_chg": round(spot_chg,2),
                "spot_start": round(spot_start,2) if spot_start else "",
                "spot_now": round(spot_now,2) if spot_now else "",
                # Returns
                "ret_1s":ret_1s,"ret_5s":ret_5s,"ret_10s":ret_10s,
                "ret_30s":ret_30s,"ret_60s":ret_60s,
                # Volatility
                "ewma_vol":round(sigma,8),"atr_30s":round(atr,4),
                "rvol_30s":round(rvol30,3),"rvol_60s":round(rvol60,3),
                # CVD/Volume
                "volume_1s":vol_1s,"volume_5s":vol_5s,
                "cvd_1s":round(cvd_1s,4),"cvd_5s":round(cvd_5s,4),"cvd_30s":round(cvd_30s,4),
                # Momentum
                "roc_1m":round(roc_1m,4),"roc_5m":round(roc_5m,4),"momentum_short":round(mom,8),
                # Range asymmetry
                "max_up":round(max_up_so_far,2),"max_dn":round(max_dn_so_far,2),
                "ratio":round(ratio,2),"dominance":round(dominance,4),
                "commitment":round(commitment,4),"velocity":round(velocity,4),
                # Black-Scholes
                "bs_prob_up":round(bs_prob,4),"bs_d2":round(bs_d2,4),
                "bs_sigma":round(sigma,8),"bs_T_remaining":round(T_remaining,0),
                # CLOB
                "up_ask":round(up_ask,3) if up_ask else "","dn_ask":round(dn_ask,3) if dn_ask else "",
                "pair_cost":round(up_ask+dn_ask,3) if up_ask and dn_ask else "",
                "clob_spread":round(clob_spread,4),"clob_midprice":round(clob_mid,4),
                # Orderbook UP
                "ob_up_bid1":up_bids.get("p1",""),"ob_up_bid1_sz":up_bids.get("s1",""),
                "ob_up_bid2":up_bids.get("p2",""),"ob_up_bid2_sz":up_bids.get("s2",""),
                "ob_up_ask1":up_asks.get("p1",""),"ob_up_ask1_sz":up_asks.get("s1",""),
                "ob_up_ask2":up_asks.get("p2",""),"ob_up_ask2_sz":up_asks.get("s2",""),
                # Orderbook DN
                "ob_dn_bid1":dn_bids.get("p1",""),"ob_dn_bid1_sz":dn_bids.get("s1",""),
                "ob_dn_ask1":dn_asks.get("p1",""),"ob_dn_ask1_sz":dn_asks.get("s1",""),
                # Microstructure
                "ob_imbalance":round(ob_imb,4),"ob_microprice":round(microprice,4),
                "ob_eat_flow":round(eat_flow,4),
                "ob_bid_depth":round(ob_bid_depth,2),"ob_ask_depth":round(ob_ask_depth,2),
                # Futures
                "taker_ratio":futures_data.get("taker_ratio",""),
                "oi_change_pct":futures_data.get("oi_change_pct",""),
                "funding_rate":futures_data.get("funding_rate",""),
                "futures_basis":futures_data.get("futures_basis",""),
                # Spot orderbook
                "spot_ob_bid":spot_ob.get("bid",""),"spot_ob_ask":spot_ob.get("ask",""),
                "spot_ob_spread":spot_ob.get("spread",""),"spot_ob_imbalance":spot_ob.get("imbalance",""),
                # Outcome (filled after settlement)
                "winner":"","final_spot_chg":"",
            }
            save_row(row)

            if int(elapsed) % 30 == 0:
                print(f"  T={elapsed:.0f}s | BTC:{spot_chg:>+.0f} | max_up:{max_up_so_far:.0f} max_dn:{max_dn_so_far:.0f} | BS:{bs_prob:.2f} | rows:{row_num}")

            await asyncio.sleep(0.9)

        # ── SETTLE ──
        await asyncio.sleep(8)
        if candle_info:
            try:
                r = requests.get(f"{GAMMA}/events?slug={candle_info['slug']}", timeout=5)
                d = r.json()
                if d:
                    m = d[0].get("markets",[{}])[0]
                    outcome = m.get("outcomePrices","")
                    if outcome:
                        prices = json.loads(outcome) if isinstance(outcome,str) else outcome
                        winner = "UP" if float(prices[0]) > 0.5 else "DN"
                        final_chg = (get_btc_now() or spot_start) - spot_start
                        # Update last N rows with winner
                        print(f"  RESULT: {'🟢 UP' if winner=='UP' else '🔴 DN'} | BTC: {final_chg:>+.0f}")
                        # Re-read CSV and update winner for this candle's rows
                        # (Simple approach: append a summary row)
                        summary = {k:"" for k in FIELDNAMES}
                        summary.update({
                            "candle":candle_num,"row":row_num+1,
                            "timestamp_utc":datetime.now(timezone.utc).isoformat(),
                            "elapsed_s":296,"winner":winner,
                            "final_spot_chg":round(final_chg,2),
                            "trading_session":session,
                        })
                        save_row(summary)
            except: pass

        candle_state["candle"] = None

async def main():
    init_csv()
    shutdown_event = asyncio.Event()
    try:
        await asyncio.gather(
            binance_feed(shutdown_event),
            chainlink_feed(shutdown_event),
            clob_poller(shutdown_event),
            main_loop(shutdown_event),
        )
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        shutdown_event.set()

if __name__ == "__main__":
    asyncio.run(main())
EOF
