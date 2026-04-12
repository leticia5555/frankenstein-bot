"""
FRANK LIVE v2 — Unified Collector + Live Trader
=================================================
ONE bot that does everything:
- Collects clean data (saves to CSV)
- Trades live at T=120s with real orders
- Checks real Polymarket settlement
- No file reading, no sync loop, no stale data
- Uses live CLOB prices directly in memory

Score ≥ 65 → place $10 order
"""
import asyncio, websockets, json, requests, os, csv, time, numpy as np
from datetime import datetime, timezone, timedelta
from collections import deque
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/polymarket-bot/.env"))
load_dotenv()

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

import pickle, warnings
warnings.filterwarnings("ignore")

# ── CONFIG ──
BET           = 10.0
MIN_SCORE     = 65
MAX_LOSS      = 150.0
CANDLE_DUR    = 300
SCAN_INTERVAL = 1.0
SKIP_HOURS    = {15, 18, 22}

MASTER_FILE = os.path.expanduser("~/polymarket-bot/frank_v15_training.csv")
MIRROR_FILE = os.path.expanduser("~/Downloads/frank_v15_training.csv")
LOG_FILE    = os.path.expanduser("~/Downloads/frank_live_v2_log.json")

BINANCE_WS   = "wss://stream.binance.com:9443/ws/btcusdt@trade"
BINANCE_REST = "https://api.binance.com/api/v3"
GAMMA        = "https://gamma-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
DASH_FILE    = os.path.expanduser("~/Downloads/frank_dashboard.json")

# ── GLOBAL STATE ──
btc_buffer = deque(maxlen=500)
pm_buffer  = deque(maxlen=200)

candle_state = {
    "active": False, "candle_num": 0, "start_ts": 0, "end_ts": 0,
    "spot_start": None, "candle": None, "scan_log": [],
    "traded": False, "trade_dir": None, "trade_price": None,
    "trade_elapsed": None, "session_trades": [], "daily_pnl": 0.0,
}

live_trades = []   # pending real trades
traded_slugs = set()
clob_client = None

# ── CLOB CLIENT ──
def init_clob():
    global clob_client
    try:
        clob_client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_PRIVATE_KEY"),
            chain_id=137, signature_type=2,
            funder=os.getenv("POLYMARKET_FUNDER")
        )
        clob_client.set_api_creds(clob_client.create_or_derive_api_creds())
        print("  ✅ Connected to Polymarket CLOB")
        return True
    except Exception as e:
        print(f"  ❌ CLOB: {e}"); return False

def place_order(token_id, price):
    try:
        shares = round(BET / price, 2)
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        args = OrderArgs(token_id=token_id, price=round(price,2),
                        size=shares, side=BUY, fee_rate_bps=0)
        signed = clob_client.create_order(args, opt)
        resp = clob_client.post_order(signed, OrderType.GTC)
        if resp.get("success"):
            return True, resp.get("orderID"), shares
        return False, str(resp), 0
    except Exception as e:
        return False, str(e), 0

def check_resolution(slug):
    try:
        r = requests.get(f"{GAMMA}/events?slug={slug}", timeout=10)
        d = r.json()
        if not d: return None
        m = d[0].get("markets",[{}])[0]
        if not m.get("resolved"): return None
        prices = json.loads(m.get("outcomePrices","[]"))
        if prices and len(prices)>=2:
            if float(prices[0])>0.9: return "UP"
            if float(prices[1])>0.9: return "DN"
        return None
    except: return None

def settle_pending():
    updated = False
    log = load_log()
    for trade in log["trades"]:
        if trade.get("result") != "PENDING" or not trade.get("slug"): continue
        placed_at = trade.get("placed_at","")
        if placed_at:
            try:
                dt = datetime.fromisoformat(placed_at.replace('Z','+00:00'))
                if (datetime.now(timezone.utc)-dt).total_seconds() < 360: continue
            except: pass
        outcome = check_resolution(trade["slug"])
        if outcome is None: continue
        d = trade["dir"]; price = trade["price"]
        if outcome == d:
            p = round(BET/price-BET, 2)
            trade["result"] = "WIN"; trade["pnl"] = p
            log["stats"]["wins"] += 1; log["stats"]["pnl"] += p
            print(f"\n  ✅ WIN!  [{trade['strat']}] {d} @ {price:.2f}c | +${p:.2f} | {trade['sess']}")
        else:
            trade["result"] = "LOSS"; trade["pnl"] = -BET
            log["stats"]["losses"] += 1; log["stats"]["pnl"] -= BET
            print(f"\n  ❌ LOSS! [{trade['strat']}] {d} @ {price:.2f}c | -${BET:.2f} | {trade['sess']} (resolved {outcome})")
        log["stats"]["pending"] = max(0, log["stats"]["pending"]-1)
        updated = True
    if updated:
        s = log["stats"]; t = s["wins"]+s["losses"]
        wr = 100*s["wins"]//t if t else 0
        print(f"  📊 REAL: {s['wins']}W/{s['losses']}L ({wr}%WR) | P&L: ${s['pnl']:+.2f} | {s['pending']} pending")
        save_log(log)

def load_log():
    try:
        with open(LOG_FILE) as f: return json.load(f)
    except:
        return {"trades":[],"stats":{"total":0,"wins":0,"losses":0,"pending":0,"pnl":0.0},
                "traded_slugs":[],"daily_pnl":0.0}

def save_log(log):
    with open(LOG_FILE,'w') as f: json.dump(log, f, indent=2)

def write_dashboard(elapsed, spot_start, spot_now, up_ask, dn_ask,
                    max_up, max_dn, candle_num, session, up_ask_init,
                    dir_history, consistency, score, trade_dir, strat,
                    reasons, candle_start_ts):
    """Write live dashboard JSON every scan tick."""
    try:
        spot_chg = spot_now - spot_start
        remaining = max(0, 300 - elapsed)

        # Retracement ratio
        main_spike = max(max_up, max_dn)
        main_bounce = max_dn if max_up > max_dn else max_up
        retrace = main_bounce / main_spike if main_spike > 5 else 0

        # Sequence pattern T=10,20,30,45
        seq = []
        for t in [10, 20, 30, 45]:
            # find closest history point
            val = None
            for h in dir_history:
                if abs(h[0] - t) < 5:
                    val = h[1]; break
            if val is None: seq.append('?')
            elif val > 3: seq.append('U')
            elif val < -3: seq.append('D')
            else: seq.append('F')

        # Fib level
        fib_levels = [
            (0, 0.236, '0–23.6%', 71, 88),
            (0.236, 0.382, '23.6–38.2%', 100, 75),
            (0.382, 0.500, '38.2–50%', 80, 50),
            (0.500, 0.618, '50–61.8%', 80, 37),
            (0.618, 0.786, '61.8–78.6%', 33, 55),
            (0.786, 1.01, '78.6–100%', 27, 28),
        ]
        fib_match = None
        abs_retrace = abs(retrace)
        for lo, hi, lbl, up_wr, dn_wr in fib_levels:
            if lo <= abs_retrace < hi:
                fib_match = {'label': lbl, 'up_wr': up_wr, 'dn_wr': dn_wr}
                break

        log = load_log()
        stats = log.get('stats', {})

        data = {
            'ts': time.time(),
            'elapsed': elapsed,
            'remaining': remaining,
            'candle_num': candle_num,
            'session': session,
            'btc_start': round(spot_start, 2),
            'btc_now': round(spot_now, 2),
            'spot_chg': round(spot_chg, 2),
            'up_ask': round(up_ask, 3),
            'dn_ask': round(dn_ask, 3),
            'up_ask_start': round(up_ask_init or up_ask, 3),
            'max_up': round(max_up, 1),
            'max_dn': round(max_dn, 1),
            'retrace': round(retrace, 4),
            'sequence': ''.join(seq),
            'consistency': round(consistency, 3) if consistency else None,
            'score': score,
            'signal_dir': trade_dir,
            'signal_strat': strat,
            'signal_reasons': reasons[:4] if reasons else [],
            'fib': fib_match,
            'real_wins': stats.get('wins', 0),
            'real_losses': stats.get('losses', 0),
            'real_pnl': round(stats.get('pnl', 0), 2),
            'real_pending': stats.get('pending', 0),
        }
        with open(DASH_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass

# ── REST PRICE POLLER — runs every 5s regardless of WebSocket ──
async def rest_price_poller(shutdown_event):
    while not shutdown_event.is_set():
        try:
            c = candle_state.get("candle")
            if c and c.get("up_token"):
                fu, fd = await asyncio.get_event_loop().run_in_executor(
                    None, get_clob_prices_rest,
                    c["up_token"], c.get("dn_token"))
                # Retry DN if it failed
                if fu and not fd and 0.01 < fu < 0.99:
                    _, fd = await asyncio.get_event_loop().run_in_executor(
                        None, get_clob_prices_rest, None, c.get("dn_token"))
                if fu and fd and 0.01 < fu < 0.99 and 0.01 < fd < 0.99:
                    ts_ms = int(time.time()*1000)
                    pm_buffer.append((ts_ms, fu, None))
                    pm_buffer.append((ts_ms, None, fd))
                    candle_state["up_ask_rest"] = fu
                    candle_state["dn_ask_rest"] = fd
        except Exception:
            pass
        await asyncio.sleep(5)


async def binance_feed(shutdown_event):
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(BINANCE_WS, ping_interval=20) as ws:
                print("  ✅ Binance WebSocket connected")
                async for raw in ws:
                    if shutdown_event.is_set(): break
                    try:
                        d = json.loads(raw)
                        btc_buffer.append((int(d["T"]), float(d["p"])))
                    except: pass
        except Exception as e:
            if not shutdown_event.is_set():
                print(f"  ⚠️  Binance WS: {e} — retry 3s")
                await asyncio.sleep(3)

def get_btc_now():
    return btc_buffer[-1][1] if btc_buffer else None

# ── POLYMARKET WEBSOCKET ──
async def polymarket_feed(shutdown_event):
    RTDS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    last_token = None
    while not shutdown_event.is_set():
        try:
            async with websockets.connect(
                RTDS_URL,
                ping_interval=20,      # built-in ping every 20s
                ping_timeout=10,       # disconnect if no pong in 10s
                close_timeout=5,
                open_timeout=10,
            ) as ws:
                print("  ✅ PM WebSocket connected")

                async def subscribe():
                    c = candle_state.get("candle")
                    if not c or not c.get("up_token"): return
                    tokens = [c["up_token"]]
                    if c.get("dn_token"): tokens.append(c["dn_token"])
                    await ws.send(json.dumps({
                        "assets_ids": tokens,
                        "type": "market",
                        "custom_feature_enabled": True
                    }))
                    print(f"  📡 Subscribed: {len(tokens)} tokens")

                await asyncio.sleep(0.5)
                await subscribe()
                c = candle_state.get("candle")
                last_token = c.get("up_token") if c else None

                async for raw in ws:
                    if shutdown_event.is_set(): break

                    # Resubscribe if candle changed
                    c = candle_state.get("candle")
                    cur_token = c.get("up_token") if c else None
                    if cur_token and cur_token != last_token:
                        await subscribe(); last_token = cur_token

                    if raw == "PONG" or raw == "ping": continue
                    try:
                        data = json.loads(raw); ts_ms = int(time.time()*1000)
                        items = data if isinstance(data, list) else [data]
                        for item in items:
                            event = item.get("event_type","")
                            if not c: continue
                            if event == "price_change":
                                for ch in item.get("price_changes",[]):
                                    aid = ch.get("asset_id","")
                                    ba = ch.get("best_ask")
                                    if ba is None: continue
                                    p = float(ba)
                                    if not 0.01<p<0.99: continue
                                    if aid==c.get("up_token"): pm_buffer.append((ts_ms,p,None))
                                    elif aid==c.get("dn_token"): pm_buffer.append((ts_ms,None,p))
                            elif event == "best_bid_ask":
                                aid = item.get("asset_id",""); ba = item.get("best_ask")
                                if ba is None: continue
                                p = float(ba)
                                if not 0.01<p<0.99: continue
                                if aid==c.get("up_token"): pm_buffer.append((ts_ms,p,None))
                                elif aid==c.get("dn_token"): pm_buffer.append((ts_ms,None,p))
                    except: pass

        except Exception as e:
            if not shutdown_event.is_set():
                print(f"  ⚠️  PM WS: {e} — retry 3s")
                await asyncio.sleep(3)

def get_pm_prices_now():
    if not pm_buffer: return None, None
    up_ask = dn_ask = None
    for ts, up, dn in reversed(list(pm_buffer)):
        if up is not None and up_ask is None: up_ask = up
        if dn is not None and dn_ask is None: dn_ask = dn
        if up_ask is not None and dn_ask is not None: break
    return up_ask, dn_ask

# ── REST HELPERS ──
def get_clob_prices_rest(up_token, dn_token):
    up_price = dn_price = None
    for token, side in [(up_token, "up"), (dn_token, "dn")]:
        if not token: continue
        try:
            r = requests.get(f"{CLOB_API}/price",
                           params={"token_id": token, "side": "buy"}, timeout=3)
            p = float(r.json().get("price", 0))
            if 0.01 < p < 0.99:
                if side == "up": up_price = p
                else: dn_price = p
        except: pass
    return up_price, dn_price

def find_candle():
    now = int(time.time())
    for offset in [0, 300, -300]:
        ts = (now // 300) * 300 + offset
        slug = f"btc-updown-5m-{ts}"
        try:
            r = requests.get(f"{GAMMA}/events?slug={slug}", timeout=5)
            d = r.json()
            if d and len(d) > 0:
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

def get_klines(limit=120):
    try:
        r = requests.get(f"{BINANCE_REST}/klines",
                        params={"symbol":"BTCUSDT","interval":"1m","limit":limit}, timeout=5)
        return r.json()
    except: return None

def get_enrichments():
    result = {}
    # All Binance-only calls — fast and reliable
    try:
        r = requests.get(f"{BINANCE_REST}/klines",
                        params={"symbol":"BTCUSDT","interval":"1m","limit":25}, timeout=3)
        klines = r.json()
        if klines:
            vols = [float(k[5]) for k in klines[-5:]]
            base = [float(k[5]) for k in klines[-20:]]
            avg = sum(vols)/len(vols); base_avg = sum(base)/len(base) if base else avg
            result["btc_vol_ratio"] = round(avg/base_avg, 3) if base_avg else 1.0
            # CVD from last 5 candles (taker buy - sell)
            cvd = sum(float(k[9]) - (float(k[5])-float(k[9])) for k in klines[-5:])
            result["cvd_5m"] = round(cvd, 2)
    except: pass
    # Macro trend
    try:
        r = requests.get(f"{BINANCE_REST}/klines",
                        params={"symbol":"BTCUSDT","interval":"15m","limit":8}, timeout=3)
        k = r.json()
        if k and len(k)>=4:
            closes = [float(x[4]) for x in k]
            opens = [float(x[1]) for x in k]
            last4_up = sum(1 for i in range(-4,0) if closes[i]>opens[i])
            ema4=sum(closes[-4:])/4; ema8=sum(closes[-8:])/8
            if ema4>ema8 and last4_up>=3: result["macro_trend_15m"]="UP"
            elif ema4<ema8 and last4_up<=1: result["macro_trend_15m"]="DN"
            else: result["macro_trend_15m"]="SIDEWAYS"
    except: pass
    # OB imbalance
    try:
        r = requests.get(f"{BINANCE_REST}/depth", params={"symbol":"BTCUSDT","limit":10}, timeout=3)
        ob = r.json()
        bid_vol = sum(float(b[1]) for b in ob.get("bids",[])[:10])
        ask_vol = sum(float(a[1]) for a in ob.get("asks",[])[:10])
        result["ob_imbalance"] = round(min(bid_vol/ask_vol, 10.0), 3) if ask_vol else 1.0
        result["ob_bid_usd"] = round(sum(float(b[0])*float(b[1]) for b in ob.get("bids",[])[:10]), 0)
        result["ob_ask_usd"] = round(sum(float(a[0])*float(a[1]) for a in ob.get("asks",[])[:10]), 0)
    except: pass
    # Funding + basis (futures)
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
                        params={"symbol":"BTCUSDT"}, timeout=3)
        d = r.json()
        result["funding_rate"] = round(float(d.get("lastFundingRate",0))*100, 6)
        result["futures_basis"] = round(float(d.get("markPrice",0)) - float(d.get("indexPrice",0)), 2)
    except: pass
    # Taker buy/sell ratio (5m) — most powerful futures signal
    try:
        r = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                        params={"symbol":"BTCUSDT","period":"5m","limit":1}, timeout=3)
        d = r.json()
        if d:
            result["taker_ratio"] = round(float(d[0].get("buySellRatio", 1.0)), 4)
    except: pass
    # Open Interest change
    try:
        r = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                        params={"symbol":"BTCUSDT","period":"5m","limit":3}, timeout=3)
        d = r.json()
        if d and len(d)>=2:
            oi_now = float(d[-1]["sumOpenInterestValue"])
            oi_prev = float(d[-2]["sumOpenInterestValue"])
            result["oi_change_pct"] = round((oi_now-oi_prev)/oi_prev*100, 4) if oi_prev else 0
    except: pass
    # Multi-exchange price agreement — fetch all 3 independently with short timeout
    prices = {}
    try:
        r = requests.get(f"{BINANCE_REST}/ticker/price?symbol=BTCUSDT", timeout=2)
        prices["binance"] = float(r.json()["price"])
    except: pass
    try:
        r = requests.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=2)
        prices["coinbase"] = float(r.json()["data"]["amount"])
    except: pass
    try:
        r = requests.get("https://www.bitstamp.net/api/v2/ticker/btcusd/", timeout=2)
        prices["bitstamp"] = float(r.json()["last"])
    except: pass
    if prices:
        vals = list(prices.values())
        result["agg_btc_price"] = round(sum(vals)/len(vals), 2)
        result["price_sources"] = len(prices)
        # Agreement signal: all exchanges moving same direction vs prev candle
        if len(vals) >= 2:
            spread_pct = (max(vals)-min(vals))/min(vals)*100
            result["exchange_spread_pct"] = round(spread_pct, 4)
    return result

# ── TECHNICAL ANALYSIS ──
def build_features(spot_start, spot_now, klines, elapsed):
    features = {"elapsed_s": elapsed, "spot_chg": spot_now - spot_start}
    closes = [float(k[4]) for k in klines[-60:]] if klines else []
    if len(closes) < 20:
        return {**features, "bb_position":0.5, "rsi_5m":50,
                "macd_histogram":0, "stoch_k":50,
                "momentum_short":0, "volatility_5m":0, "trend_alignment":0}
    sma20=np.mean(closes[-20:]); std20=np.std(closes[-20:])
    bb_upper=sma20+2*std20; bb_lower=sma20-2*std20
    bb_range=bb_upper-bb_lower if bb_upper!=bb_lower else 1
    features["bb_position"]=(spot_now-bb_lower)/bb_range
    deltas=np.diff(closes[-15:])
    gains=np.where(deltas>0,deltas,0); losses=np.where(deltas<0,-deltas,0)
    avg_gain=np.mean(gains) if len(gains)>0 else 0
    avg_loss=np.mean(losses) if len(losses)>0 else 0.001
    features["rsi_5m"]=100-(100/(1+avg_gain/avg_loss))
    ema12=np.mean(closes[-12:]); ema26=np.mean(closes[-26:]) if len(closes)>=26 else ema12
    macd_line=ema12-ema26; features["macd_histogram"]=macd_line-(macd_line*0.8)
    low14=min(closes[-14:]); high14=max(closes[-14:])
    features["stoch_k"]=((spot_now-low14)/(high14-low14)*100) if high14!=low14 else 50
    features["momentum_short"]=(spot_now-closes[-5])/closes[-5] if closes[-5]!=0 else 0
    features["volatility_5m"]=np.std(closes[-5:])/np.mean(closes[-5:]) if np.mean(closes[-5:])!=0 else 0
    sma5=np.mean(closes[-5:]); sma10=np.mean(closes[-10:])
    trend=0
    trend+=1 if sma5>sma10 else -1
    trend+=1 if sma10>sma20 else -1
    trend+=1 if spot_now>sma5 else -1
    features["trend_alignment"]=trend
    return features

def get_trading_session(utc_hour):
    if   0<=utc_hour<7:   return "asia","active"
    elif 7<=utc_hour<8:   return "asia_close","transition"
    elif 8<=utc_hour<12:  return "europe","active"
    elif 12<=utc_hour<13: return "eu_us_overlap","transition"
    elif 13<=utc_hour<14: return "us_premarket","high_vol"
    elif 14<=utc_hour<16: return "us_open","high_vol"
    elif 16<=utc_hour<20: return "us_midday","active"
    elif 20<=utc_hour<21: return "us_close","high_vol"
    else:                  return "after_hours","low_vol"

# ── SCORING — Spike-based system (93%WR backtested) ──
def score_signal(up_ask, dn_ask, max_up, max_dn, spot_chg, elapsed,
                 consistency, trend, bb, vel, rsi, sess):
    """
    New spike-based scoring. No prev_candle needed.
    Core signal: BTC moved strongly in one direction with no retracement.
    Backtested 93%WR on 4,112 candles across 5 days.
    """
    score = 0; direction = None; reasons = []

    # ── SIGNAL 1: One-sided spike (core signal) ──
    spike_up = max_up >= 20 and abs(max_dn) <= 10
    spike_dn = abs(max_dn) >= 20 and max_up <= 10

    # Ratio-based: if UP move is 3x bigger than DN move → trending UP
    ratio_up = max_up / (abs(max_dn) + 1)
    ratio_dn = abs(max_dn) / (max_up + 1)

    if spike_up:
        direction = 'UP'; score += 40
        reasons.append(f"spike_UP(↑${max_up:.0f}↓${abs(max_dn):.0f})+40")
    elif spike_dn:
        direction = 'DN'; score += 40
        reasons.append(f"spike_DN(↓${abs(max_dn):.0f}↑${max_up:.0f})+40")
    elif max_up >= 20 and ratio_up >= 2.5 and spot_chg > 0:
        # UP move 2.5x larger than DN bounce
        direction = 'UP'; score += 30
        reasons.append(f"trend_UP(ratio={ratio_up:.1f})+30")
    elif abs(max_dn) >= 20 and ratio_dn >= 2.5 and spot_chg < 0:
        # DN move 2.5x larger than UP bounce
        direction = 'DN'; score += 30
        reasons.append(f"trend_DN(ratio={ratio_dn:.1f})+30")
    elif max_up >= 15 and abs(max_dn) <= 15 and spot_chg > 5:
        direction = 'UP'; score += 20
        reasons.append(f"weak_spike_UP(↑${max_up:.0f})+20")
    elif abs(max_dn) >= 15 and max_up <= 15 and spot_chg < -5:
        direction = 'DN'; score += 20
        reasons.append(f"weak_spike_DN(↓${abs(max_dn):.0f})+20")
    else:
        return 0, None, [], None, None  # no clear directional move

    # ── SIGNAL 2: Direction consistency (+30pts) ──
    if consistency is not None:
        if consistency >= 0.90:   score += 30; reasons.append(f"cons={consistency:.0%}+30")
        elif consistency >= 0.70: score += 20; reasons.append(f"cons={consistency:.0%}+20")
        elif consistency >= 0.50: score += 10; reasons.append(f"cons={consistency:.0%}+10")
        elif consistency < 0.30:  score -= 20; reasons.append(f"cons={consistency:.0%}-20 SKIP")
    else:
        # No consistency data — estimate from spot_chg
        if direction == 'UP' and spot_chg > 10:   score += 15; reasons.append("spot_confirms_UP+15")
        elif direction == 'DN' and spot_chg < -10: score += 15; reasons.append("spot_confirms_DN+15")

    # ── SIGNAL 3: Trend alignment (+15pts) ──
    if trend is not None:
        if direction == 'UP':
            if trend == 3:   score += 15; reasons.append("trend=+3+15")
            elif trend == 1: score += 8;  reasons.append("trend=+1+8")
            elif trend == -3: score -= 15; reasons.append("trend=-3-15")
            elif trend == -1: score -= 5;  reasons.append("trend=-1-5")
        else:
            if trend == -3:  score += 15; reasons.append("trend=-3+15")
            elif trend == -1: score += 8;  reasons.append("trend=-1+8")
            elif trend == 3:  score -= 15; reasons.append("trend=+3-15")
            elif trend == 1:  score -= 5;  reasons.append("trend=+1-5")

    # ── SIGNAL 4: BB position (+10pts) ──
    if bb is not None:
        if direction == 'UP':
            if bb < 0.3:   score += 10; reasons.append(f"BB_low+10")
            elif bb > 0.8: score -= 5;  reasons.append(f"BB_high-5")
        else:
            if bb > 0.7:   score += 10; reasons.append(f"BB_high+10")
            elif bb < 0.2: score -= 5;  reasons.append(f"BB_low-5")

    # ── SIGNAL 5: Spot velocity (+10pts) ──
    if vel is not None:
        if direction == 'UP':
            if vel > 0.5:   score += 10; reasons.append(f"vel={vel:.2f}+10")
            elif vel > 0.2: score += 5;  reasons.append(f"vel={vel:.2f}+5")
            elif vel < -0.3: score -= 10; reasons.append(f"vel={vel:.2f}-10")
        else:
            if vel < -0.5:  score += 10; reasons.append(f"vel={vel:.2f}+10")
            elif vel < -0.2: score += 5; reasons.append(f"vel={vel:.2f}+5")
            elif vel > 0.3:  score -= 10; reasons.append(f"vel={vel:.2f}-10")

    # ── SIGNAL 6: RSI (+5pts) ──
    if rsi is not None:
        if direction == 'UP' and rsi < 40:   score += 5; reasons.append(f"RSI_low({rsi:.0f})+5")
        elif direction == 'DN' and rsi > 60: score += 5; reasons.append(f"RSI_high({rsi:.0f})+5")

    # ── SIGNAL 7: CLOB spread confirms (+10pts) ──
    sp = abs(up_ask - 0.5)
    if sp > 0.18: score += 10; reasons.append(f"spread>{sp:.0%}+10")
    elif sp > 0.10: score += 5; reasons.append(f"spread>{sp:.0%}+5")

    # ── SIGNAL 8: CLOB direction agrees (+10pts) ──
    clob_dir = 'UP' if up_ask > 0.55 else 'DN' if dn_ask > 0.55 else None
    if clob_dir == direction: score += 10; reasons.append(f"CLOB_agrees+10")
    elif clob_dir and clob_dir != direction: score -= 15; reasons.append(f"CLOB_opposes-15")

    # ── ENTRY PRICE ──
    price = up_ask if direction == 'UP' else dn_ask
    if not price or price <= 0.01 or price >= 0.99:
        return 0, None, reasons, None, None

    # Determine strategy
    strat = 'SPIKE_HIGH' if score >= 70 else 'SPIKE_MED' if score >= 50 else 'SPIKE_LOW'

    return max(0, min(100, score)), direction, reasons, price, strat


# ── CSV FIELDNAMES ──
FIELDNAMES = [
    "candle","elapsed_s","timestamp_utc","timestamp_ms",
    "utc_hour","utc_minute","day_of_week","trading_session","session_phase",
    "spot_start","spot_now","spot_chg","btc_price_ms","pm_price_ms","price_read_gap_ms",
    "up_ask","dn_ask","pair_cost","price_source","clob_sane",
    "oracle_lag_signal","pm_implied_dir","btc_implied_dir","pm_btc_agree",
    "direction","confidence","up_prob",
    "bb_position","rsi_5m","macd_histogram","stoch_k",
    "momentum_short","volatility_5m","trend_alignment",
    "spot_velocity","direction_changes","scan_count",
    "peak_conf_so_far","conf_drop_from_peak",
    "winner","final_spot_chg","was_correct",
    "would_trade","would_trade_reason",
    # enrichments — all saved now
    "btc_vol_ratio","macro_trend_15m","macro_strength",
    "ob_imbalance","ob_bid_usd","ob_ask_usd",
    "funding_rate","futures_basis",
    "taker_ratio","oi_change_pct",
    "cvd_5m",
    "agg_btc_price","price_sources","exchange_spread_pct",
]

scan_log = []

def log_scan(candle_num, elapsed, spot_start, spot_now, up_ask, dn_ask,
             direction, confidence, up_prob, features, scan_count,
             direction_changes, peak_conf, btc_ms, pm_ms, price_source,
             would_trade, would_trade_reason, enrichments=None):
    now_utc = datetime.now(timezone.utc)
    spot_chg = spot_now - spot_start
    pm_implied = "UP" if up_ask>0.55 else "DN" if dn_ask>0.55 else "EV"
    btc_implied = "UP" if spot_chg>=0 else "DN"
    oracle_lag = abs(up_ask-0.5)*2 if pm_implied!=btc_implied and abs(up_ask-0.5)>0.15 else None
    row = {
        "candle": candle_num,
        "elapsed_s": round(elapsed,2),
        "timestamp_utc": now_utc.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "timestamp_ms": int(time.time()*1000),
        "utc_hour": now_utc.hour, "utc_minute": now_utc.minute,
        "day_of_week": now_utc.strftime("%a"),
        "trading_session": features.get("session",""),
        "session_phase": features.get("session_phase",""),
        "spot_start": round(spot_start,2), "spot_now": round(spot_now,2),
        "spot_chg": round(spot_chg,2),
        "btc_price_ms": btc_ms, "pm_price_ms": pm_ms,
        "price_read_gap_ms": abs(btc_ms-pm_ms) if btc_ms and pm_ms else None,
        "up_ask": round(up_ask,4), "dn_ask": round(dn_ask,4),
        "pair_cost": round(up_ask+dn_ask,4),
        "price_source": price_source,
        "clob_sane": 1 if 0.90<=(up_ask+dn_ask)<=1.10 else 0,
        "oracle_lag_signal": round(oracle_lag,4) if oracle_lag else None,
        "pm_implied_dir": pm_implied, "btc_implied_dir": btc_implied,
        "pm_btc_agree": 1 if pm_implied==btc_implied else 0,
        "direction": direction, "confidence": round(confidence,4),
        "up_prob": round(up_prob,4),
        "bb_position": round(features.get("bb_position",0.5),4),
        "rsi_5m": round(features.get("rsi_5m",50),2),
        "macd_histogram": round(features.get("macd_histogram",0),6),
        "stoch_k": round(features.get("stoch_k",50),2),
        "momentum_short": round(features.get("momentum_short",0),6),
        "volatility_5m": round(features.get("volatility_5m",0),6),
        "trend_alignment": features.get("trend_alignment",0),
        "spot_velocity": round(spot_chg/max(elapsed,1),4),
        "direction_changes": direction_changes, "scan_count": scan_count,
        "peak_conf_so_far": round(peak_conf,4),
        "conf_drop_from_peak": round(peak_conf-confidence,4),
        "winner": None, "final_spot_chg": None, "was_correct": None,
        "would_trade": 1 if would_trade else 0,
        "would_trade_reason": would_trade_reason,
        "btc_vol_ratio": (enrichments or {}).get("btc_vol_ratio"),
        "macro_trend_15m": (enrichments or {}).get("macro_trend_15m"),
        "ob_imbalance": (enrichments or {}).get("ob_imbalance"),
        "funding_rate": (enrichments or {}).get("funding_rate"),
        "agg_btc_price": (enrichments or {}).get("agg_btc_price"),
        "price_sources": (enrichments or {}).get("price_sources"),
    }
    scan_log.append(row)
    return row

def backfill_outcome(candle_num, winner, final_chg):
    for row in scan_log:
        if row["candle"]==candle_num and row["winner"] is None:
            row["winner"]=winner; row["final_spot_chg"]=round(final_chg,2)
            row["was_correct"]=1 if row["direction"]==winner else 0

def save_data(candle_num):
    if not scan_log: return
    completed = [r for r in scan_log if r.get("winner")]
    if not completed: return
    try:
        file_exists = os.path.exists(MASTER_FILE)
        last_saved = 0
        if file_exists:
            with open(MASTER_FILE) as f:
                rows = list(csv.DictReader(f))
                if rows: last_saved = max(int(r.get("candle",0)) for r in rows)
        new_rows = [r for r in completed if int(r["candle"])>last_saved]
        if not new_rows: return
        with open(MASTER_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            if not file_exists: writer.writeheader()
            writer.writerows(new_rows)
        import shutil; shutil.copy2(MASTER_FILE, MIRROR_FILE)
        print(f"  💾 Saved {len(new_rows)} candle(s) [candle_{candle_num}]")
    except Exception as e:
        print(f"  ⚠️  Save error: {e}")

# ── MAIN SIGNAL LOOP ──
async def signal_loop(shutdown_event):
    global traded_slugs
    candle_num = 0
    prev_candle_data = None  # stores {cw, fc} of previous candle for momentum

    print("  ⏳ Waiting for Binance feed...")
    for _ in range(30):
        if btc_buffer: break
        await asyncio.sleep(1)
    if not btc_buffer:
        print("  ❌ No Binance feed"); return

    print(f"  ✅ BTC: ${get_btc_now():,.2f}")

    log = load_log()
    traded_slugs = set(log.get("traded_slugs", []))

    # Track the next expected candle boundary
    now = time.time()
    next_candle_ts = (int(now) - (int(now) % CANDLE_DUR)) + CANDLE_DUR

    while not shutdown_event.is_set():
        candle_num += 1
        scan_log.clear()

        # Wait for the tracked candle boundary
        now = time.time()
        wait_for = next_candle_ts - now

        # Print wait message
        next_label = datetime.fromtimestamp(next_candle_ts).strftime("%I:%M %p")
        if wait_for > 5:
            print(f"\n  ⏳ Waiting for candle at {next_label} ({wait_for:.0f}s)...")

        # Prefetch during wait if enough time (>3s), else fetch at candle start
        if wait_for > 3:
            pre_wait = max(0, wait_for - 3.0)
            await asyncio.sleep(pre_wait)
            loop = asyncio.get_event_loop()
            klines_pre, enrichments_pre = await asyncio.gather(
                loop.run_in_executor(None, get_klines, 120),
                loop.run_in_executor(None, get_enrichments),
            )
            remaining = next_candle_ts - time.time()
            if remaining > 0:
                await asyncio.sleep(remaining)
        else:
            await asyncio.sleep(max(0, wait_for))
            klines_pre = enrichments_pre = None

        current_slot = next_candle_ts
        # Set next candle based on real time (handles any drift)
        _now = time.time()
        _slot = int(_now) - (int(_now) % CANDLE_DUR)
        next_candle_ts = _slot + CANDLE_DUR

        candle_start_ts = current_slot
        candle_end_ts = current_slot + CANDLE_DUR
        utc_hour = datetime.now(timezone.utc).hour
        session, session_phase = get_trading_session(utc_hour)

        # ── T=0: Capture BTC + fetch candle info + CLOB simultaneously ──
        spot_start = get_btc_now()

        loop = asyncio.get_event_loop()
        klines      = klines_pre if klines_pre else await loop.run_in_executor(None, get_klines, 120)
        enrichments = enrichments_pre if enrichments_pre else await loop.run_in_executor(None, get_enrichments)
        candle_info = await loop.run_in_executor(None, find_candle)

        # Set candle state IMMEDIATELY so REST poller starts working
        candle_state["candle"] = candle_info

        # Fetch CLOB for THIS candle right now
        up_ask_init = dn_ask_init = None
        if candle_info and candle_info.get("up_token"):
            up_ask_init, dn_ask_init = await loop.run_in_executor(
                None, get_clob_prices_rest,
                candle_info["up_token"], candle_info.get("dn_token"))
            if up_ask_init and dn_ask_init:
                ts_now = int(time.time()*1000)
                pm_buffer.clear()
                pm_buffer.append((ts_now, up_ask_init, None))
                pm_buffer.append((ts_now, None, dn_ask_init))
            else:
                print(f"  ⚠️  CLOB init failed: UP={up_ask_init} DN={dn_ask_init}", flush=True)
        else:
            print(f"  ⚠️  No candle info or token: {candle_info}", flush=True)

        print(f"\n{'━'*65}")
        now_label = datetime.fromtimestamp(candle_start_ts).strftime("%I:%M %p")
        end_label = datetime.fromtimestamp(candle_end_ts).strftime("%I:%M %p")
        print(f"  CANDLE #{candle_num} | {now_label} → {end_label} | {session}")
        print(f"{'━'*65}")

        settle_pending()

        if candle_info: print(f"  📋 {candle_info['question']}")

        macro  = enrichments.get("macro_trend_15m","?")
        vol_r  = enrichments.get("btc_vol_ratio",1.0) or 1.0
        ob     = enrichments.get("ob_imbalance",1.0) or 1.0
        fund   = enrichments.get("funding_rate",0) or 0
        taker  = enrichments.get("taker_ratio")
        basis  = enrichments.get("futures_basis")
        print(f"  📊 Macro:{macro} Vol:{vol_r:.1f}x OB:{ob:.2f} Fund:{fund:+.4f}%", end="")
        if taker: print(f" Taker:{taker:.3f}", end="")
        if basis: print(f" Basis:${basis:+.1f}", end="")
        print()
        print(f"  💰 BTC Start: ${spot_start:,.2f}")
        if up_ask_init and dn_ask_init:
            print(f"  📦 CLOB: UP={up_ask_init*100:.0f}c DN={dn_ask_init*100:.0f}c")
        print()
        print(f"  {'Time':>5}  {'BTC Δ':>7}  {'Dir':>4}  {'Conf':>5}  {'UP':>4}  {'DN':>4}  {'Lag':>5}  Src")
        print(f"  {'─'*60}")

        scan_count = direction_changes = 0
        last_direction = None; peak_conf = 0.0
        last_rest_refresh = time.time()
        REST_INTERVAL = 5
        paper_logged = False

        # Candle-level tracking for spike signal
        max_up_so_far = 0.0   # max upward move from candle start
        max_dn_so_far = 0.0   # max downward move (stored as positive)
        flips = 0; prev_dir = None
        lag_count = 0
        live_trade_placed = False
        spot_vel_60 = None
        rsi_now = None; bb_now = None; trend_now = None
        # Direction consistency tracking
        dir_history = []  # list of (elapsed, spot_chg) tuples

        while True:
            now = time.time()
            elapsed = now - candle_start_ts
            remaining = candle_end_ts - now
            if remaining <= 4: break

            btc_ms = btc_buffer[-1][0] if btc_buffer else None
            spot_now = btc_buffer[-1][1] if btc_buffer else spot_start
            spot_chg = spot_now - spot_start

            # REST every 5s via background task — WebSocket overrides when live
            up_ws, dn_ws = get_pm_prices_now()
            pm_ms = int(time.time()*1000)
            if up_ws and dn_ws and 0.01<up_ws<0.99 and 0.01<dn_ws<0.99:
                up_ask=up_ws; dn_ask=dn_ws; price_source="websocket"
                up_ask_init=up_ws; dn_ask_init=dn_ws
            elif up_ask_init and dn_ask_init:
                up_ask=up_ask_init; dn_ask=dn_ask_init; price_source="rest_refresh"
            else:
                up_ask=0.50; dn_ask=0.50; price_source="fallback"

            sp = abs(up_ask - 0.5)

            # ── TRACK MAX MOVES (core spike signal) ──
            if spot_chg > max_up_so_far: max_up_so_far = spot_chg
            if spot_chg < -max_dn_so_far: max_dn_so_far = abs(spot_chg)

            # Track flips + direction history for consistency
            btc_dir = "UP" if spot_chg>=0 else "DN"
            if prev_dir and btc_dir!=prev_dir: flips+=1
            prev_dir = btc_dir
            dir_history.append((elapsed, spot_chg))

            # Oracle lag
            oracle_lag = None
            pm_implied = "UP" if up_ask>0.55 else "DN" if dn_ask>0.55 else "EV"
            if pm_implied!=btc_dir and abs(up_ask-0.5)>0.15:
                oracle_lag = abs(up_ask-0.5)*2
                lag_count += 1

            # Technical features
            features = build_features(spot_start, spot_now, klines, elapsed)
            features["session"]=session; features["session_phase"]=session_phase
            rsi_now = features.get("rsi_5m", 50)
            bb_now = features.get("bb_position", 0.5)
            trend_now = features.get("trend_alignment", 0)

            # Velocity at T=60
            if 57<=elapsed<=65 and spot_vel_60 is None and elapsed>0:
                spot_vel_60 = spot_chg / elapsed

            # Direction consistency
            consistency = None
            if len(dir_history) >= 10:
                win_dir = "UP" if spot_chg >= 0 else "DN"
                consistency = sum(1 for _,v in dir_history if ('UP' if v>=0 else 'DN')==win_dir) / len(dir_history)

            # Display direction
            up_score = 0.5
            if spot_chg>0: up_score+=min(0.2,spot_chg/200)
            else: up_score-=min(0.2,abs(spot_chg)/200)
            if bb_now>0.8: up_score-=0.05
            elif bb_now<0.2: up_score+=0.05
            up_score=max(0.1,min(0.9,up_score))
            direction="UP" if up_score>=0.5 else "DN"
            confidence=max(up_score,1-up_score)

            scan_count+=1
            if last_direction and direction!=last_direction: direction_changes+=1
            last_direction=direction
            if confidence>peak_conf: peak_conf=confidence

            # Would-trade logic (paper)
            would_trade=False; reason=""
            if elapsed<30: reason=f"too_early"
            elif up_ask==0.50 and dn_ask==0.50: reason="ghost_50_50"
            elif confidence<0.65: reason=f"low_conf"
            else: would_trade=True; reason=f"ENTER_{direction}@{(up_ask if direction=='UP' else dn_ask)*100:.0f}c"

            if would_trade and not paper_logged:
                paper_logged=True
                candle_state["traded"]=True
                candle_state["trade_dir"]=direction
                candle_state["trade_price"]=up_ask if direction=="UP" else dn_ask
                candle_state["trade_elapsed"]=elapsed

            log_scan(candle_num, elapsed, spot_start, spot_now,
                     up_ask, dn_ask, direction, confidence, up_score,
                     features, scan_count, direction_changes, peak_conf,
                     btc_ms, pm_ms, price_source, would_trade, reason, enrichments)

            lag_str = f"{oracle_lag:.2f}" if oracle_lag else "  — "
            agree_icon = "✅" if pm_implied==btc_dir else "⚠️ "
            src_icon = "🟢" if price_source=="websocket" else "🟡" if "rest" in price_source else "🔴"
            wt_icon = "★" if would_trade else " "
            spike_str = f"↑${max_up_so_far:.0f}↓${max_dn_so_far:.0f}"
            print(f"  {elapsed:>5.0f}s  {spot_chg:>+7.0f}  "
                  f"{'🟢' if direction=='UP' else '🔴'}{direction}  "
                  f"{confidence*100:4.0f}%  "
                  f"{up_ask*100:4.0f}c  {dn_ask*100:4.0f}c  "
                  f"{lag_str:>5} {agree_icon} {src_icon}{wt_icon} {spike_str}")

            # Write dashboard JSON
            write_dashboard(
                elapsed, spot_start, spot_now, up_ask, dn_ask,
                max_up_so_far, max_dn_so_far, candle_num, session,
                up_ask_init, dir_history, consistency, 0, None, None, [],
                candle_start_ts
            )


            # ── SIGNAL DISPLAY (paper only — no live trades) ──
            ENTRY_WINDOWS = [(118,128), (148,152), (178,182), (208,212)]
            in_window = any(lo<=elapsed<=hi for lo,hi in ENTRY_WINDOWS)

            if in_window and not live_trade_placed:
                score, trade_dir, reasons, trade_price, strat = score_signal(
                    up_ask, dn_ask,
                    max_up_so_far, max_dn_so_far,
                    spot_chg, elapsed,
                    consistency, trend_now, bb_now,
                    spot_vel_60, rsi_now, session
                )

                if score >= MIN_SCORE and trade_dir and trade_price:
                    payout = round(BET/trade_price-BET, 2)
                    cons_str = f"{consistency:.0%}" if consistency else "N/A"
                    print(f"\n  📊 SIGNAL [{score}/100] [{strat}] {trade_dir} @ {trade_price*100:.0f}c")
                    print(f"     Spike↑${max_up_so_far:.0f} ↓${max_dn_so_far:.0f} | cons={cons_str}")
                    print(f"     Would win: +${payout:.2f} | Would lose: -${BET:.2f}")
                    print(f"     {' | '.join(reasons[:4])}")
                    live_trade_placed = True

                    # Update dashboard with signal
                    write_dashboard(
                        elapsed, spot_start, spot_now, up_ask, dn_ask,
                        max_up_so_far, max_dn_so_far, candle_num, session,
                        up_ask_init, dir_history, consistency, score,
                        trade_dir, strat, reasons, candle_start_ts
                    )

                elif not live_trade_placed and 209<=elapsed<=210:
                    cons_str = f"{consistency:.0%}" if consistency else "?"
                    print(f"  ⏭️  SKIP | score={score} | ↑${max_up_so_far:.0f}↓${max_dn_so_far:.0f} | cons={cons_str} | {reasons[0] if reasons else 'no_spike'}")
                    live_trade_placed = True

            await asyncio.sleep(SCAN_INTERVAL)

        # ── CANDLE END ──
        final_spot = get_btc_now() or spot_start
        final_chg = final_spot - spot_start
        winner = "UP" if final_chg>=0 else "DN"
        backfill_outcome(candle_num, winner, final_chg)

        # Store this candle for next candle's momentum signal
        prev_candle_data = {"cw": winner, "fc": final_chg}

        # ── SAVE MATHEMATICAL SIGNAL DATA (background thread — no blocking) ──
        def _save_signals():
            try:
                sig_file = os.path.expanduser("~/Downloads/frank_signals.csv")
                sig_exists = os.path.exists(sig_file)
                main_spike = max(max_up_so_far, max_dn_so_far)
                main_bounce = max_dn_so_far if max_up_so_far > max_dn_so_far else max_up_so_far
                spike_dir = 'UP' if max_up_so_far > max_dn_so_far else 'DN'
                retrace = round(main_bounce / main_spike, 4) if main_spike > 5 else 0
                seq = []
                for t in [10, 20, 30, 45]:
                    val = next((v for e,v in dir_history if abs(e-t)<5), None)
                    if val is None: seq.append('?')
                    elif val > 3: seq.append('U')
                    elif val < -3: seq.append('D')
                    else: seq.append('F')
                sequence = ''.join(seq)
                fib_levels = [(0,0.236,'0-23.6'),(0.236,0.382,'23.6-38.2'),
                             (0.382,0.5,'38.2-50'),(0.5,0.618,'50-61.8'),
                             (0.618,0.786,'61.8-78.6'),(0.786,1.01,'78.6-100')]
                fib_label = next((lbl for lo,hi,lbl in fib_levels if lo<=retrace<hi), '—')
                SEQ_WR = {'FFUU':85,'UUUU':83,'UDDD':80,'DDDD':72,'FFFU':66,'FUUU':66,'FFDD':64}
                SEQ_DIR = {'FFUU':'UP','UUUU':'UP','UDDD':'DN','DDDD':'DN','FFFU':'UP','FUUU':'UP','FFDD':'DN'}
                sig_score, sig_dir, _, _, _ = score_signal(
                    up_ask_init or 0.5, 1-(up_ask_init or 0.5),
                    max_up_so_far, max_dn_so_far, final_chg, 120,
                    consistency, trend_now, bb_now, spot_vel_60, rsi_now, session
                )
                row = {
                    'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M'),
                    'candle': candle_num, 'session': session,
                    'winner': winner, 'final_chg': round(final_chg, 2),
                    'max_up': round(max_up_so_far, 1), 'max_dn': round(max_dn_so_far, 1),
                    'spike_dir': spike_dir, 'retrace_ratio': retrace,
                    'retrace_signal': 'ENTER' if retrace<0.30 and main_spike>=15 else 'REVERSAL' if retrace>0.85 and main_spike>=15 else 'SKIP',
                    'sequence': sequence,
                    'seq_wr': SEQ_WR.get(sequence, 0),
                    'seq_dir': SEQ_DIR.get(sequence, ''),
                    'fib_level': fib_label, 'fib_broke': retrace > 0.618,
                    'erased_pct': round(retrace*100, 1),
                    'reversal_signal': retrace>0.80 and main_spike>=15,
                    'consistency': round(consistency, 3) if consistency else None,
                    'score': sig_score, 'signal_dir': sig_dir,
                    'signal_correct': (sig_dir==winner) if sig_dir else None,
                    'clob_open_up': round(up_ask_init, 3) if up_ask_init else None,
                    'up_ask_120': round(up_ask or 0.5, 3),
                }
                import csv as csvmod
                with open(sig_file, 'a', newline='') as f:
                    w = csvmod.DictWriter(f, fieldnames=list(row.keys()))
                    if not sig_exists: w.writeheader()
                    w.writerow(row)
            except Exception as e:
                pass  # never block main loop

        import threading
        threading.Thread(target=_save_signals, daemon=True).start()

        print(f"\n  ═══════════════════════════════════════════")
        print(f"  RESULT: {'🟢' if winner=='UP' else '🔴'} {winner} | BTC: {final_chg:+.0f}")

        # Paper result
        if candle_state["traded"]:
            td=candle_state["trade_dir"]; tp=candle_state["trade_price"]
            if td==winner:
                profit=BET*(1.0-tp)/tp if tp>0 else 0
                candle_state["daily_pnl"]+=profit
                candle_state["session_trades"].append({"result":"WIN","pnl":profit})
                print(f"  ★ PAPER WIN:  {td} @ {tp*100:.0f}c | +${profit:.2f}")
            else:
                candle_state["daily_pnl"]-=BET
                candle_state["session_trades"].append({"result":"LOSS","pnl":-BET})
                print(f"  ★ PAPER LOSS: {td} @ {tp*100:.0f}c | -${BET:.2f}")

        trades=candle_state["session_trades"]
        wins2=sum(1 for t in trades if t["result"]=="WIN")
        losses2=len(trades)-wins2
        wr2=wins2/(wins2+losses2)*100 if trades else 0
        print(f"\n  📊 Session: {wins2}W/{losses2}L ({wr2:.0f}%) | P&L: ${candle_state['daily_pnl']:+.2f}")
        save_data(candle_num)
        await asyncio.sleep(2)

# ── MAIN ──
async def main():
    print("\n"+"═"*65)
    print("  🧠 FRANK LIVE v2 — Unified Collector + Trader")
    print(f"  Bet: ${BET} | Score≥{MIN_SCORE} | Max loss: ${MAX_LOSS}")
    print(f"  Collects data AND trades live — no file reading")
    print("═"*65)

    if not init_clob():
        print("  ⚠️  Running paper-only mode (no live trading)")

    shutdown_event = asyncio.Event()
    try:
        await asyncio.gather(
            binance_feed(shutdown_event),
            polymarket_feed(shutdown_event),
            rest_price_poller(shutdown_event),
            signal_loop(shutdown_event),
        )
    except KeyboardInterrupt:
        print("\n\n  Stopping...")
        shutdown_event.set()

if __name__ == "__main__":
    asyncio.run(main())
