#!/usr/bin/env python3
"""
momentum_xs.py — cross-sectional 12-1 momentum (a POSITIVE control).

Most of this toolkit kills bad ideas (Halloween effect, RSI dip-buys). This one
tests a famously ROBUST edge: cross-sectional momentum. If the engine is honest,
it should DETECT this as a real edge — confirming it doesn't just nuke
everything.

The strategy (Jegadeesh-Titman style, 12-1):
  - Each month, rank stocks by their trailing 12-month return, SKIPPING the most
    recent month (the "-1": last month is dropped to avoid short-term reversal).
  - Go LONG the top third (winners), SHORT the bottom third (losers).
  - Hold one month, then rebalance and repeat.
  - Also track a winners-only (long-only) book.

Discipline (same as the rest of the toolkit):
  - 70/30 chronological split to check the edge held vs faded.
  - Anti-fragile check: recompute the spread after removing the single best
    month, so one monster month can't carry the whole result.

Timing (explicit, to avoid look-ahead):
  monthly closes indexed 0..M-1. For holding month k (return earned over month k):
    signal_s = close[s][k-2] / close[s][k-13] - 1   # 12m return, skipping the
                                                     # most recent month
    return_s = close[s][k]   / close[s][k-1]  - 1    # what we earn holding month k
  So the signal uses data through end of month k-2; nothing from month k leaks in.

Usage:
    python3 momentum_xs.py momentum_data/*.csv
    python3 momentum_xs.py a.csv b.csv c.csv --names AAPL,MSFT,NVDA

Pure historical analysis. No trading.
"""

import argparse
import os
import sys

from backtest_stock import load_csv, _date_key

IN_SAMPLE_FRACTION = 0.70
LOOKBACK = 12          # months of trailing return
SKIP = 1               # skip most recent month
MIN_STOCKS = 6         # need enough names to form distinct thirds
MIN_MONTHS = 24        # need enough holding months to say anything


# ---------------------------------------------------------------------------
# Monthly close construction + alignment
# ---------------------------------------------------------------------------

def month_end_closes(dates, closes):
    """(year, month) -> last-trading-day close. Rows are oldest-first."""
    eom = {}
    for d, c in zip(dates, closes):
        k = _date_key(d)
        if k is not None:
            eom[(k.year, k.month)] = c
    return eom


def align_monthly(series):
    """Given {name: eom_dict}, return (months, prices) where months is the sorted
    list of (year, month) present in EVERY stock, and prices[name] is the close
    list aligned to that month list."""
    common = None
    for eom in series.values():
        keys = set(eom)
        common = keys if common is None else (common & keys)
    if not common:
        return [], {}
    months = sorted(common)
    prices = {name: [eom[m] for m in months] for name, eom in series.items()}
    return months, prices


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def run_momentum(months, prices):
    """Run the 12-1 monthly rebalance. Returns a list of per-holding-month dicts."""
    names = list(prices)
    M = len(months)
    third = max(1, len(names) // 3)
    results = []

    for k in range(LOOKBACK + SKIP, M):
        # Signal: 12m return ending one month before formation (skip last month).
        sig = {}
        for s in names:
            past = prices[s][k - LOOKBACK - SKIP]   # close[k-13]
            end = prices[s][k - SKIP - 1]           # close[k-2]; month k-1 skipped
            if past > 0:
                sig[s] = end / past - 1
        if len(sig) < len(names):
            continue  # bad price somewhere this month

        ranked = sorted(names, key=lambda s: sig[s], reverse=True)
        winners = ranked[:third]
        losers = ranked[-third:]

        def mret(basket):
            return sum(prices[s][k] / prices[s][k - 1] - 1 for s in basket) / len(basket)

        w = mret(winners)
        l = mret(losers)
        allret = sum(prices[s][k] / prices[s][k - 1] - 1 for s in names) / len(names)
        results.append({
            "month": months[k],
            "winners": w,
            "losers": l,
            "wml": w - l,
            "all": allret,
            "win_names": winners,
            "lose_names": losers,
        })
    return results


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def avg(xs):
    return sum(xs) / len(xs) if xs else 0.0


def compound(rets):
    g = 1.0
    for r in rets:
        g *= (1.0 + r)
    return g


def annualized(total_growth, n_months):
    if n_months <= 0 or total_growth <= 0:
        return 0.0
    return total_growth ** (12.0 / n_months) - 1.0


def block_stats(rows):
    n = len(rows)
    if n == 0:
        return None
    wml = [r["wml"] for r in rows]
    beat = sum(1 for x in wml if x > 0) / n
    return {
        "n": n,
        "avg_w": avg([r["winners"] for r in rows]),
        "avg_l": avg([r["losers"] for r in rows]),
        "avg_wml": avg(wml),
        "beat": beat,
        "wml_growth": compound(wml),
    }


def print_block(title, s):
    if s is None:
        print(f"  {title}: (no months)")
        return
    print(f"  {title}  ({s['n']} months)")
    print(f"    avg winners / month : {s['avg_w'] * 100:+6.2f}%")
    print(f"    avg losers  / month : {s['avg_l'] * 100:+6.2f}%")
    print(f"    winners - losers    : {s['avg_wml'] * 100:+6.2f}%  per month")
    print(f"    winners beat losers : {s['beat'] * 100:5.1f}% of months")


def main():
    parser = argparse.ArgumentParser(
        description="Cross-sectional 12-1 momentum backtest (positive control)."
    )
    parser.add_argument("csvs", nargs="+", help="Stock CSVs (>= 6 recommended).")
    parser.add_argument("--names", default=None,
                        help="Comma-separated labels matching the CSV order.")
    parser.add_argument("--stake", type=float, default=10000.0,
                        help="Starting capital for equity curves (default 10000).")
    args = parser.parse_args()

    if args.names:
        labels = [x.strip() for x in args.names.split(",")]
        if len(labels) != len(args.csvs):
            sys.exit(f"--names has {len(labels)} labels but {len(args.csvs)} CSVs.")
    else:
        labels = [os.path.splitext(os.path.basename(p))[0] for p in args.csvs]

    if len(args.csvs) < MIN_STOCKS:
        sys.exit(
            f"Too few stocks: {len(args.csvs)} given, need >= {MIN_STOCKS} to "
            f"form meaningful winner/loser thirds. (Cross-sectional momentum is "
            f"about ranking ACROSS names — a handful can't be split into thirds.)"
        )

    # Load + monthly align.
    series = {}
    for path, name in zip(args.csvs, labels):
        d, c = load_csv(path)
        series[name] = month_end_closes(d, c)

    months, prices = align_monthly(series)
    if len(months) < LOOKBACK + SKIP + MIN_MONTHS:
        sys.exit(
            f"Not enough overlapping monthly data: only {len(months)} common "
            f"months across all {len(labels)} stocks (need > "
            f"{LOOKBACK + SKIP + MIN_MONTHS}). Check the histories overlap."
        )

    rows = run_momentum(months, prices)
    if len(rows) < MIN_MONTHS:
        sys.exit(f"Only {len(rows)} holding months — too short to judge.")

    third = max(1, len(labels) // 3)
    split = int(len(rows) * IN_SAMPLE_FRACTION)
    is_rows, oos_rows = rows[:split], rows[split:]

    overall = block_stats(rows)
    is_s = block_stats(is_rows)
    oos_s = block_stats(oos_rows)

    # Equity curves over all holding months.
    g_winners = compound([r["winners"] for r in rows])
    g_losers = compound([r["losers"] for r in rows])
    g_wml = compound([r["wml"] for r in rows])
    g_all = compound([r["all"] for r in rows])
    nm = len(rows)

    # Anti-fragile: WML spread / growth after removing the single best month.
    best = max(rows, key=lambda r: r["wml"])
    wml_ex_best = [r["wml"] for r in rows if r is not best]
    avg_wml_ex_best = avg(wml_ex_best)
    g_wml_ex_best = compound(wml_ex_best)

    # ----- Report -----
    print("=" * 70)
    print("  CROSS-SECTIONAL 12-1 MOMENTUM  (positive control)")
    print("=" * 70)
    print(f"  stocks ({len(labels)})    : {', '.join(labels)}")
    print(f"  basket size     : top/bottom {third} of {len(labels)} (thirds)")
    print(f"  common months   : {len(months)}  ({months[0][1]}/{months[0][0]} -> "
          f"{months[-1][1]}/{months[-1][0]})")
    print(f"  holding months  : {nm}  (after 12-1 warmup)")
    print(f"  split           : {len(is_rows)} in-sample / {len(oos_rows)} out-of-sample")
    print("-" * 70)
    print_block("ALL MONTHS", overall)
    print("-" * 70)
    print_block("IN-SAMPLE (first 70%)", is_s)
    print()
    print_block("OUT-OF-SAMPLE (last 30%)", oos_s)
    print("-" * 70)
    print(f"  COMPOUNDED GROWTH  (${args.stake:,.0f} start, {nm} months "
          f"~{nm / 12:.1f}y)")
    print(f"    winners (long-only) : x{g_winners:6.2f} -> ${args.stake * g_winners:>12,.0f}"
          f"   ({annualized(g_winners, nm) * 100:+.1f}%/yr)")
    print(f"    losers  (long-only) : x{g_losers:6.2f} -> ${args.stake * g_losers:>12,.0f}"
          f"   ({annualized(g_losers, nm) * 100:+.1f}%/yr)")
    print(f"    all (equal-weight)  : x{g_all:6.2f} -> ${args.stake * g_all:>12,.0f}"
          f"   ({annualized(g_all, nm) * 100:+.1f}%/yr)")
    print(f"    WML (long-short)    : x{g_wml:6.2f} -> ${args.stake * g_wml:>12,.0f}"
          f"   ({annualized(g_wml, nm) * 100:+.1f}%/yr)")
    print("-" * 70)
    print(f"  ANTI-FRAGILE CHECK (remove single best WML month: "
          f"{best['month'][1]}/{best['month'][0]}, {best['wml'] * 100:+.1f}%)")
    print(f"    avg WML/month   : {overall['avg_wml'] * 100:+.2f}% -> "
          f"{avg_wml_ex_best * 100:+.2f}%  (without best month)")
    print(f"    WML growth      : x{g_wml:.2f} -> x{g_wml_ex_best:.2f}  (without best)")
    print("-" * 70)

    # ----- Verdict -----
    oos_spread = oos_s["avg_wml"]
    is_spread = is_s["avg_wml"]
    survives = avg_wml_ex_best > 0
    if oos_spread > 0 and is_spread > 0 and survives and overall["beat"] >= 0.5:
        label = "REAL EDGE"
        why = (f"Winners beat losers in-sample ({is_spread * 100:+.2f}%/mo) AND "
               f"out-of-sample ({oos_spread * 100:+.2f}%/mo), in "
               f"{overall['beat'] * 100:.0f}% of months, and survives removing "
               f"the best month — a genuine, persistent edge.")
    elif is_spread > 0 and oos_spread <= 0:
        label = "FADED"
        why = (f"Edge in-sample ({is_spread * 100:+.2f}%/mo) but gone "
               f"out-of-sample ({oos_spread * 100:+.2f}%/mo).")
    else:
        label = "NOISE / WEAK"
        why = (f"No consistent spread (in-sample {is_spread * 100:+.2f}%/mo, "
               f"out-of-sample {oos_spread * 100:+.2f}%/mo).")
    print(f"  VERDICT: {label}")
    print(f"  {why}")
    if len(labels) < 12:
        print(f"  NOTE: only {len(labels)} stocks — thin cross-section "
              f"(thirds = {third} name(s) each); more names = more reliable.")
    print("=" * 70)

    print(f"\nSummary: 12-1 momentum on {len(labels)} stocks -> {label}. "
          f"Winners-minus-losers {overall['avg_wml'] * 100:+.2f}%/mo overall "
          f"({is_spread * 100:+.2f}% IS -> {oos_spread * 100:+.2f}% OOS), "
          f"winners long-only x{g_winners:.1f} vs equal-weight x{g_all:.1f}.")


if __name__ == "__main__":
    main()
