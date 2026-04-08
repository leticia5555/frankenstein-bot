"""
FRANKENSTEIN v2 — Fixed Smart Agent
=====================================
Fixes from audit:
1. Hard block: one trade per 5-min slug
2. REQUIRE BTC@T120 to confirm direction (not just no-oppose)
3. REQUIRE BTC@T60 data (skip if N/A)
4. Score threshold raised to 65
5. Better settlement checking
"""
import warnings; warnings.filterwarnings("ignore")
import csv, json, time, os, requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/polymarket-bot/.env"))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType, PartialCreateOrderOptions
from py_clob_client.order_builder.constants import BUY

# ── CONFIG ──
BET        = 10.0
MAX_LOSS   = 100.0
MIN_SCORE  = 65     # raised from 60
DATA_FILE  = os.path.expanduser("~/Downloads/frank_v13_training.csv")
LOG_FILE   = os.path.expanduser("~/Downloads/frankenstein_log.json")
GAMMA      = "https://gamma-api.polymarket.com"

A_MIN=0.35; A_MAX=0.65; A_SPREAD=0.05
B_MAX=0.40; B_LAG=2

SKIP_HOURS = {15, 18, 22}

client = None

def init_client():
    global client
    try:
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=os.getenv("POLYMARKET_PRIVATE_KEY"),
            chain_id=137, signature_type=2,
            funder=os.getenv("POLYMARKET_FUNDER")
        )
        client.set_api_creds(client.create_or_derive_api_creds())
        print("  ✅ Connected to Polymarket")
        return True
    except Exception as e:
        print(f"  ❌ {e}"); return False

def get_market():
    ts = (int(time.time()) // 300) * 300
    for offset in [0, 300, -300]:
        slug = f"btc-updown-5m-{ts+offset}"
        try:
            r = requests.get(f"{GAMMA}/events?slug={slug}", timeout=5)
            d = r.json()
            if d and len(d) > 0:
                m = d[0].get("markets",[{}])[0]
                if m.get("acceptingOrders") and not m.get("closed"):
                    ids = m.get("clobTokenIds","")
                    t = json.loads(ids) if isinstance(ids,str) else ids
                    if len(t)>=2:
                        return slug, {"q":m.get("question",""),"up":t[0],"dn":t[1]}
        except: continue
    return None, None

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

def place_order(token_id, price):
    try:
        shares = round(BET/price, 2)
        opt = PartialCreateOrderOptions(tick_size="0.01", neg_risk=False)
        args = OrderArgs(token_id=token_id, price=round(price,2),
                        size=shares, side=BUY, fee_rate_bps=0)
        signed = client.create_order(args, opt)
        resp = client.post_order(signed, OrderType.GTC)
        if resp.get("success"):
            return True, resp.get("orderID"), shares
        return False, str(resp), 0
    except Exception as e:
        return False, str(e), 0

def sf(s,k,d=None):
    try: return float(s[k]) if s and s.get(k) else d
    except: return d

def build_candles(rows):
    by = defaultdict(list)
    for r in rows:
        if r.get("winner"): by[r["timestamp_utc"][:16]].append(r)
    out = []
    for ts in sorted(by):
        sc = sorted(by[ts], key=lambda x: float(x.get("elapsed_s",0) or 0))
        w=sc[0].get("winner","")
        if not w: continue
        def at(t): return next((s for s in sc if abs(float(s.get("elapsed_s",0) or 0)-t)<3),None)
        t120=at(120); t60=at(60)
        b120=sf(t120,"spot_chg"); u120=sf(t120,"up_ask",0.5); d120=sf(t120,"dn_ask",0.5)
        b60=sf(t60,"spot_chg")
        sc0=float(sc[0].get("up_ask",0.5) or 0.5)
        fc=float(sc[-1].get("spot_chg",0) or 0)
        bd120=("UP" if (b120 or 0)>=0 else "DN") if b120 is not None else ""
        sp=abs(sc0-0.5)
        lag=sum(1 for s in sc if float(s.get("oracle_lag_signal") or 0)>0)
        # direction flips
        flips=0; prev_dir=None
        for s in sc:
            btc_d=s.get("btc_implied_dir","")
            if prev_dir and btc_d and btc_d!=prev_dir: flips+=1
            if btc_d: prev_dir=btc_d
        cheap=None
        for s in sc:
            ua=float(s.get("up_ask",0.5) or 0.5); da=float(s.get("dn_ask",0.5) or 0.5)
            hl=float(s.get("oracle_lag_signal") or 0)>0
            bd=s.get("btc_implied_dir",""); el=float(s.get("elapsed_s",0) or 0)
            if hl and el<120:
                if bd=="UP" and ua<B_MAX and (cheap is None or ua<cheap[1]): cheap=("UP",ua)
                elif bd=="DN" and da<B_MAX and (cheap is None or da<cheap[1]): cheap=("DN",da)
        out.append({"ts":ts,"w":w,"sess":sc[0].get("trading_session",""),
                    "sc0":sc0,"fc":fc,"sp":sp,"bd120":bd120,
                    "u120":u120,"d120":d120,"b60":b60,"b120":b120,
                    "lag":lag,"cheap":cheap,"flips":flips})
    return out

def score_signal(prev, c):
    d=prev['w']; score=0; reasons=[]

    # Base momentum
    score+=20; reasons.append(f"momentum({d})+20")

    # ── FIX: REQUIRE BTC@T60 data ──
    b60=c.get('b60')
    if b60 is None:
        return 0, None, None, None, ["NO_BTC60_DATA→skip"]

    btc60_dir='UP' if b60>=0 else 'DN'
    if btc60_dir==d:
        if abs(b60)>50: score+=35; reasons.append(f"BTC@T60>$50+35")
        elif abs(b60)>25: score+=25; reasons.append(f"BTC@T60>$25+25")
        elif abs(b60)>10: score+=15; reasons.append(f"BTC@T60>$10+15")
        else: score+=5; reasons.append(f"BTC@T60<$10+5")
    else:
        score-=25; reasons.append(f"BTC@T60_opposes-25")

    # ── FIX: REQUIRE BTC@T120 to confirm ──
    if c.get('bd120')==d:
        score+=15; reasons.append("BTC120_confirms+15")
    elif c.get('bd120')=='':
        return 0, None, None, None, ["NO_BTC120_DATA→skip"]
    else:
        return 0, None, None, None, [f"BTC120_OPPOSES→skip"]

    # CLOB spread
    sp=c.get('sp',0)
    if sp>0.18: score+=25; reasons.append("spread>18%+25")
    elif sp>0.12: score+=15; reasons.append("spread>12%+15")
    elif sp>0.08: score+=10; reasons.append("spread>8%+10")
    elif sp>0.05: score+=5;  reasons.append("spread>5%+5")
    else: score-=10; reasons.append("neutral_spread-10")

    # Prev candle BTC size
    pbtc=abs(prev.get('fc',0))
    if pbtc>120: score+=15; reasons.append("prevBTC>$120+15")
    elif pbtc>60: score+=12; reasons.append("prevBTC>$60+12")
    elif pbtc>30: score+=8;  reasons.append("prevBTC>$30+8")
    elif pbtc>15: score+=4;  reasons.append("prevBTC>$15+4")
    else: score-=5; reasons.append("prevBTC<$15-5")

    # Hour bonus
    hour=int(c['ts'][11:13])
    if hour in {0,3,4,5,7,9,11,16,17,20}: score+=10; reasons.append(f"best_hour+10")
    elif hour in {15,18,22}: score-=15; reasons.append(f"bad_hour-15")

    # ── FIX: Skip choppy candles ──
    if c.get('flips',0)>=3:
        return 0, None, None, None, ["TOO_CHOPPY→skip"]

    # Strategy B: lag cheap entry
    strat=None; price=None
    if c.get('cheap') and c['cheap'][0]==d and c.get('lag',0)>=B_LAG:
        pr=c['cheap'][1]
        if 0.01<pr<B_MAX:
            strat='B_LAG'; price=pr; score+=20; reasons.append("lag_cheap+20")

    # Strategy A: T120
    if strat is None:
        pr=c.get('u120') if d=='UP' else c.get('d120')
        if pr and A_MIN<=pr<=A_MAX and sp>=A_SPREAD:
            strat='A_T120'; price=pr

    if strat is None or price is None:
        return 0, None, None, None, ["no_valid_entry"]

    return max(0,min(100,score)), d, price, strat, reasons

def load_log():
    try:
        with open(LOG_FILE) as f: return json.load(f)
    except:
        return {"trades":[],"stats":{"total":0,"wins":0,"losses":0,"pending":0,"pnl":0.0},
                "traded_slugs":[],"daily_pnl":0.0}

def save_log(log):
    with open(LOG_FILE,'w') as f: json.dump(log,f,indent=2)

def settle_pending(log):
    updated=False
    for trade in log['trades']:
        if trade.get('result')!='PENDING' or not trade.get('slug'): continue
        placed_at=trade.get('placed_at','')
        if placed_at:
            try:
                dt=datetime.fromisoformat(placed_at.replace('Z','+00:00'))
                if (datetime.now(timezone.utc)-dt).total_seconds()<360: continue
            except: pass
        outcome=check_resolution(trade['slug'])
        if outcome is None: continue
        d=trade['dir']; price=trade['price']
        if outcome==d:
            p=round(BET/price-BET,2); trade['result']='WIN'; trade['pnl']=p
            log['stats']['wins']+=1; log['stats']['pnl']+=p
            print(f"\n  ✅ WIN!  [{trade['strat']}] {d} @ {price:.2f}c | +${p:.2f} | {trade['sess']}")
        else:
            trade['result']='LOSS'; trade['pnl']=-BET
            log['stats']['losses']+=1; log['stats']['pnl']-=BET
            print(f"\n  ❌ LOSS! [{trade['strat']}] {d} @ {price:.2f}c | -${BET:.2f} | {trade['sess']} (resolved {outcome})")
        log['stats']['pending']=max(0,log['stats']['pending']-1)
        log['daily_pnl']=log['stats']['pnl']
        updated=True
    if updated:
        s=log['stats']; t=s['wins']+s['losses']
        wr=100*s['wins']//t if t else 0
        print(f"  📊 REAL: {s['wins']}W/{s['losses']}L ({wr}%WR) | P&L: ${s['pnl']:+.2f} | {s['pending']} pending")
        save_log(log)

def main():
    print("\n"+"═"*60)
    print("  🧠 FRANKENSTEIN v2 — Fixed Smart Agent")
    print(f"  Bet: ${BET} | Min score: {MIN_SCORE}/100 | Max loss: ${MAX_LOSS}")
    print(f"  Fixes: require T60+T120 data, no duplicates, skip choppy")
    print("═"*60)

    if not init_client(): return

    log=load_log()
    seen=set(t['ts'] for t in log['trades'])
    traded_slugs=set(log.get('traded_slugs',[]))

    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M")
    s=log['stats']; t=s['wins']+s['losses']
    print(f"\n  Skipping before: {cutoff}")
    print(f"  Previous: {t} resolved | {100*s['wins']//t if t else 0}%WR | ${s['pnl']:+.2f}")
    print(f"  Pending: {s['pending']}\n")

    while True:
        try:
            if log['daily_pnl']<=-MAX_LOSS:
                print("  ⛔ Daily loss limit. Stopping."); break

            settle_pending(log)

            rows=list(csv.DictReader(open(DATA_FILE)))
            candles=build_candles(rows)

            for i in range(1,len(candles)):
                prev=candles[i-1]; c=candles[i]
                if c['ts']<=cutoff: seen.add(c['ts']); continue
                if c['ts'] in seen: continue
                seen.add(c['ts'])

                hour=int(c['ts'][11:13])
                if hour in SKIP_HOURS:
                    print(f"  ⏭️  SKIP {c['ts']} | bad hour {hour}:xx"); continue

                score,direction,price,strat,reasons=score_signal(prev,c)

                if score<MIN_SCORE or not direction:
                    skip_r=reasons[0] if reasons else 'score too low'
                    print(f"  ⏭️  SKIP {c['ts']} | {c['sess']} | {skip_r} score={score}")
                    continue

                slug,mkt=get_market()
                if not mkt:
                    print(f"  ⚠️  No active market"); continue

                # ── HARD FIX: one trade per slug ──
                if slug in traded_slugs:
                    print(f"  ⏭️  Already traded {slug[-20:]}")
                    continue

                token=mkt['up'] if direction=='UP' else mkt['dn']
                payout=round(BET/price-BET,2)

                print(f"\n  {'🔥' if strat=='B_LAG' else '🎯'} [{score}/100] [{strat}] {direction} @ {price:.2f}c")
                print(f"     {mkt['q']}")
                print(f"     {c['sess']} | Win: +${payout:.2f} | Loss: -${BET:.2f}")
                print(f"     {' | '.join(reasons[:4])}")

                ok,oid,shares=place_order(token,price)

                if ok:
                    print(f"  ✅ PLACED! {shares:.1f} shares | ID: {oid[:24]}...")
                    traded_slugs.add(slug)
                    log['traded_slugs']=list(traded_slugs)
                    log['stats']['total']+=1
                    log['stats']['pending']+=1
                else:
                    print(f"  ❌ Failed: {oid}")

                log['trades'].append({
                    'ts':c['ts'],'slug':slug,'strat':strat,'dir':direction,
                    'price':round(price,3),'score':score,'sess':c['sess'],
                    'ok':ok,'id':oid if ok else None,'result':'PENDING','pnl':None,
                    'placed_at':datetime.now(timezone.utc).isoformat(),
                    'reasons':reasons
                })
                save_log(log)

            time.sleep(15)

        except KeyboardInterrupt:
            print("\n\n  Stopped."); save_log(log); break
        except Exception as e:
            print(f"  Error: {e}")
            import traceback; traceback.print_exc()
            time.sleep(15)

if __name__=="__main__":
    main()
