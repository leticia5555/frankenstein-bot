"""
FRANK TRADER — Strategy Simulator
===================================
Runs alongside frank_v13.py WITHOUT touching it.
Reads the same CSV every 5 minutes, applies Strategy A + B,
logs simulated trades, tracks P&L.

Strategy A: Momentum + T120 confirmation
Strategy B: Oracle lag cheap entry (<40c)

Usage:
  python3 frank_trader.py

Reads:  ~/Downloads/frank_v13_training.csv  (live data from bot)
Writes: ~/Downloads/frank_trader_log.json   (trade decisions)
"""

import csv
import json
import time
import os
from datetime import datetime, timezone
from collections import defaultdict

# ── CONFIG ──────────────────────────────────────────────────
DATA_FILE   = os.path.expanduser("~/Downloads/frank_v13_training.csv")
LOG_FILE    = os.path.expanduser("~/Downloads/frank_trader_log.json")
BET         = 30.0
CHECK_EVERY = 20   # seconds between CSV checks
MIN_CANDLES = 10   # need this many before trading

# Strategy A thresholds
A_MIN_PRICE      = 0.35
A_MAX_PRICE      = 0.65
A_MIN_CLOB_SPREAD = 0.05   # |clob - 0.5| minimum
A_MAX_PREV_BTC   = 50.0    # max prev candle BTC move

# Strategy B thresholds
B_MAX_PRICE      = 0.40    # only cheap entries
B_MIN_LAG_SCANS  = 2       # need confirmed lag

# ── HELPERS ─────────────────────────────────────────────────

def load_csv():
    if not os.path.exists(DATA_FILE):
        return []
    try:
        with open(DATA_FILE) as f:
            return list(csv.DictReader(f))
    except:
        return []

def sf(s, k, d=None):
    try: return float(s[k]) if s and s.get(k) else d
    except: return d

def build_candles(rows):
    by_candle = defaultdict(list)
    for r in rows:
        if r.get('winner'):
            by_candle[r.get('timestamp_utc','')[:16]].append(r)
    
    candles = []
    for ts in sorted(by_candle.keys()):
        scans = sorted(by_candle[ts], key=lambda x: float(x.get('elapsed_s',0) or 0))
        winner  = scans[0].get('winner','')
        session = scans[0].get('trading_session','')
        if not winner: continue
        
        first = scans[0]; last = scans[-1]
        
        def at(t):
            return next((s for s in scans if abs(float(s.get('elapsed_s',0) or 0)-t)<3), None)
        
        t60=at(60); t120=at(120)
        btc120 = sf(t120,'spot_chg')
        up120  = sf(t120,'up_ask',0.5)
        dn120  = sf(t120,'dn_ask',0.5)
        start_clob = float(first.get('up_ask',0.5) or 0.5)
        final_chg  = float(last.get('spot_chg',0) or 0)

        btc_dir120 = ('UP' if (btc120 or 0)>=0 else 'DN') if btc120 is not None else ''
        clob_spread = abs(start_clob - 0.5)
        lag_scans   = sum(1 for s in scans if float(s.get('oracle_lag_signal') or 0)>0)

        # Best cheap entry price (for Strategy B)
        cheap_lag_price = None
        for s in scans:
            up_ask = float(s.get('up_ask',0.5) or 0.5)
            dn_ask = float(s.get('dn_ask',0.5) or 0.5)
            has_lag = float(s.get('oracle_lag_signal') or 0) > 0
            btc_dir = s.get('btc_implied_dir','')
            elapsed = float(s.get('elapsed_s',0) or 0)
            
            if has_lag and elapsed < 120:  # only early in candle
                if btc_dir == 'UP' and up_ask < B_MAX_PRICE:
                    if cheap_lag_price is None or up_ask < cheap_lag_price[1]:
                        cheap_lag_price = ('UP', up_ask, elapsed)
                elif btc_dir == 'DN' and dn_ask < B_MAX_PRICE:
                    if cheap_lag_price is None or dn_ask < cheap_lag_price[1]:
                        cheap_lag_price = ('DN', dn_ask, elapsed)

        candles.append({
            'ts': ts, 'winner': winner, 'session': session,
            'start_clob': start_clob, 'final_chg': final_chg,
            'btc_dir120': btc_dir120, 'clob_spread': clob_spread,
            'up120': up120, 'dn120': dn120,
            'lag_scans': lag_scans,
            'cheap_lag_price': cheap_lag_price,
        })
    return candles

def load_log():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE) as f:
                return json.load(f)
        except:
            pass
    return {
        'trades': [],
        'stats': {'total': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0},
        'last_candle': None,
    }

def save_log(log):
    with open(LOG_FILE, 'w') as f:
        json.dump(log, f, indent=2)

def calc_pnl(direction, price, winner, bet=BET):
    if price <= 0.01 or price >= 0.99: return None
    return round(bet/price - bet, 2) if direction == winner else -bet

def print_banner():
    print("\n" + "═"*60)
    print("  🤖 FRANK TRADER — Strategy Simulator")
    print("  Reads live data, simulates Strategy A + B")
    print("  Does NOT interfere with frank_v13.py")
    print("═"*60)
    print(f"  Data file: {DATA_FILE}")
    print(f"  Log file:  {LOG_FILE}")
    print(f"  Bet size:  ${BET}")
    print("═"*60 + "\n")

def print_trade(trade):
    icon = "★ WIN " if trade['result']=='WIN' else "  LOSS"
    strat = f"[{trade['strategy']}]"
    print(f"  {icon} {strat:6} {trade['direction']} @ {trade['price']:.2f}c | "
          f"{trade['session']:12} | P&L: ${trade['pnl']:+.2f} | "
          f"Total: ${trade['cumulative_pnl']:+.2f}")

def print_stats(log):
    s = log['stats']
    t = s['total']
    if t == 0: return
    wr = 100*s['wins']//t
    print(f"\n  📊 Session: {s['wins']}W/{s['losses']}L ({wr}%) | "
          f"P&L: ${s['pnl']:+.2f} | Trades: {t}")
    
    # By strategy
    by_strat = defaultdict(lambda:[0,0,0.0])
    for tr in log['trades']:
        k = tr['strategy']
        if tr['result']=='WIN': by_strat[k][0]+=1
        else: by_strat[k][1]+=1
        by_strat[k][2]+=tr['pnl']
    
    for strat,(w,l,p) in by_strat.items():
        tt=w+l
        print(f"     {strat}: {tt}tr | {100*w//tt}% WR | ${p:+.2f}")

# ── MAIN LOOP ────────────────────────────────────────────────

def main():
    print_banner()
    log = load_log()
    seen_candles = set(t['candle_ts'] for t in log['trades'])
    
    print(f"  Loaded {len(log['trades'])} previous trades")
    print(f"  Watching for new candles...\n")
    
    while True:
        try:
            rows = load_csv()
            if not rows:
                time.sleep(CHECK_EVERY)
                continue
            
            candles = build_candles(rows)
            if len(candles) < MIN_CANDLES:
                time.sleep(CHECK_EVERY)
                continue
            
            # Process new candles
            new_trades = 0
            for i in range(1, len(candles)):
                prev = candles[i-1]
                c    = candles[i]
                
                if c['ts'] in seen_candles:
                    continue  # already processed
                
                seen_candles.add(c['ts'])
                momentum_dir = prev['winner']
                trade = None
                
                # ── STRATEGY B: Oracle lag cheap entry ──
                if (c['cheap_lag_price'] and 
                    c['cheap_lag_price'][0] == momentum_dir and
                    c['lag_scans'] >= B_MIN_LAG_SCANS):
                    
                    direction = c['cheap_lag_price'][0]
                    price     = c['cheap_lag_price'][1]
                    elapsed   = c['cheap_lag_price'][2]
                    
                    if 0.01 < price < 0.99:
                        r = calc_pnl(direction, price, c['winner'])
                        if r is not None:
                            trade = {
                                'strategy':       'B_LAG',
                                'candle_ts':      c['ts'],
                                'session':        c['session'],
                                'direction':      direction,
                                'price':          round(price, 3),
                                'entry_elapsed':  elapsed,
                                'winner':         c['winner'],
                                'result':         'WIN' if r>0 else 'LOSS',
                                'pnl':            r,
                                'prev_btc':       round(prev['final_chg'], 1),
                            }
                
                # ── STRATEGY A: T120 momentum confirmation ──
                if trade is None:
                    if (c['btc_dir120'] == momentum_dir and
                        abs(prev['final_chg']) <= A_MAX_PREV_BTC and
                        c['clob_spread'] >= A_MIN_CLOB_SPREAD):
                        
                        direction = momentum_dir
                        price = c['up120'] if direction=='UP' else c['dn120']
                        
                        if price and A_MIN_PRICE <= price <= A_MAX_PRICE:
                            r = calc_pnl(direction, price, c['winner'])
                            if r is not None:
                                trade = {
                                    'strategy':       'A_T120',
                                    'candle_ts':      c['ts'],
                                    'session':        c['session'],
                                    'direction':      direction,
                                    'price':          round(price, 3),
                                    'entry_elapsed':  120,
                                    'winner':         c['winner'],
                                    'result':         'WIN' if r>0 else 'LOSS',
                                    'pnl':            r,
                                    'prev_btc':       round(prev['final_chg'], 1),
                                }
                
                # Record trade
                if trade:
                    log['stats']['total'] += 1
                    log['stats']['pnl']   += trade['pnl']
                    if trade['result']=='WIN':
                        log['stats']['wins'] += 1
                    else:
                        log['stats']['losses'] += 1
                    
                    trade['cumulative_pnl'] = round(log['stats']['pnl'], 2)
                    log['trades'].append(trade)
                    log['last_candle'] = c['ts']
                    
                    print_trade(trade)
                    new_trades += 1
            
            if new_trades > 0:
                print_stats(log)
                save_log(log)
                print()
            
            time.sleep(CHECK_EVERY)
            
        except KeyboardInterrupt:
            print("\n\n  Stopping trader...")
            print_stats(log)
            save_log(log)
            break
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(CHECK_EVERY)

if __name__ == "__main__":
    main()
