#!/usr/bin/env python3
"""
earnings_reaction.py — how does AAPL behave right after earnings?

This is an EVENT-REACTION study, not a tradable signal: we look at what the
stock does immediately after each earnings release and ask whether there's a
reliable pop/drop, or whether it's basically random / already priced in.

EARNINGS DATES — IMPORTANT, READ THIS:
  We could not reach a source for Apple's official historical earnings dates in
  this environment (network egress blocked). So unless you pass a real list with
  --dates-file, this script APPROXIMATES earnings days by detecting unusual
  trading days: days with a big volume spike (relative to the trailing quarter)
  that are spaced ~quarterly apart. Earnings reliably cause both. This is an
  APPROXIMATION, not the official dates — it is printed loudly in the output and
  should be treated as such. (Apple reports after the close, so the detected
  high-volume day is the first POST-earnings session = the reaction day.)

What it reports, for the reaction day and 5 days out:
  - average move, % of times it went up, biggest up and biggest down
  - vs AAPL's normal base rate (how often any random day / 5-day window rose)
  - 70/30 chronological split, so we see if the behavior is stable or changed
  - plain-language verdict

Usage:
    python3 earnings_reaction.py AAPL.csv
    python3 earnings_reaction.py AAPL.csv --dates-file real_earnings.txt
    python3 earnings_reaction.py AAPL.csv --hold 5 --min-rvol 1.8

Pure historical analysis. No trading.
"""

import argparse
import csv
import sys

# Reuse the robust price/date parsing from backtest_stock.
from backtest_stock import _clean_price, _date_key, CLOSE_HEADERS, DATE_HEADERS

IN_SAMPLE_FRACTION = 0.70
VOL_BASELINE_WINDOW = 63   # ~1 quarter of trading days for the volume baseline
MIN_EVENT_GAP = 40         # trading days; earnings are ~quarterly, keep events apart
DEFAULT_MIN_RVOL = 1.8     # a day must have >= this x its trailing-median volume
DEFAULT_HOLD = 5


# ---------------------------------------------------------------------------
# Data loading (need Volume too, so a dedicated loader here)
# ---------------------------------------------------------------------------

def load_ohlcv(path):
    """Load (dates, closes, volumes) oldest-first from a Nasdaq-style CSV."""
    with open(path, newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        sys.exit(f"{path} is empty.")

    header = [h.strip().lower() for h in rows[0]]
    close_idx = next((header.index(h) for h in CLOSE_HEADERS if h in header), None)
    if close_idx is None:
        sys.exit(f"No close column found in {path} (looked for {CLOSE_HEADERS}).")
    date_idx = next((header.index(h) for h in DATE_HEADERS if h in header), None)
    vol_idx = header.index("volume") if "volume" in header else None
    if vol_idx is None:
        sys.exit(f"No 'volume' column in {path}; can't approximate earnings days. "
                 f"Pass --dates-file with real dates instead.")

    parsed = []
    for i, row in enumerate(rows[1:]):
        try:
            c = _clean_price(row[close_idx])
            v = float(row[vol_idx].replace(",", "").strip())
        except (ValueError, IndexError):
            continue
        d = row[date_idx] if date_idx is not None else str(i)
        parsed.append((d, c, v))
    if not parsed:
        sys.exit(f"No usable rows in {path}.")

    keys = [_date_key(d) for d, _, _ in parsed]
    if all(k is not None for k in keys):
        parsed = [p for _, p in sorted(zip(keys, parsed), key=lambda x: x[0])]

    dates = [d for d, _, _ in parsed]
    closes = [c for _, c, _ in parsed]
    vols = [v for _, _, v in parsed]
    return dates, closes, vols


def load_dates_file(path):
    """Load a user-supplied list of real earnings dates (one per line)."""
    out = []
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
    return out


# ---------------------------------------------------------------------------
# Earnings-day detection (the APPROXIMATION)
# ---------------------------------------------------------------------------

def median(xs):
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def detect_earnings_days(dates, closes, vols, min_rvol, max_events):
    """Approximate earnings days as spaced-out volume + price-jump days.

    Earnings reliably produce BOTH a volume spike and a large price move (the
    gap on the surprise). Pure-volume days are contaminated by quad-witching
    options-expiry days (huge volume, small move), so we score each day by
    relative-volume x absolute-move — that de-weights witching days and favors
    genuine earnings reactions. Then greedily pick the highest-scoring days that
    are >= MIN_EVENT_GAP trading days apart (so ~quarterly events, not clusters).

    NOTE: still an approximation — macro-shock days (COVID crash, tariff
    selloffs) also score high and can slip in. Returns (sorted_indices, rvol).
    """
    n = len(vols)
    rvol = [0.0] * n
    for i in range(n):
        lo = max(0, i - VOL_BASELINE_WINDOW)
        base = median(vols[lo:i]) if i > 0 else 0.0
        rvol[i] = (vols[i] / base) if base > 0 else 0.0

    # Combined score: relative volume x absolute reaction-day move.
    score = [0.0] * n
    for i in range(1, n):
        move = abs(closes[i] / closes[i - 1] - 1)
        score[i] = rvol[i] * move

    # Greedy selection by score, with a minimum-volume gate and spacing.
    order = sorted(range(n), key=lambda i: score[i], reverse=True)
    chosen = []
    for i in order:
        if rvol[i] < min_rvol:
            continue
        if all(abs(i - j) >= MIN_EVENT_GAP for j in chosen):
            chosen.append(i)
        if len(chosen) >= max_events:
            break
    chosen.sort()
    return chosen, rvol


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def daily_returns(closes):
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def fwd_return(closes, i, k):
    """k-day forward return from index i; None if out of range."""
    if i + k >= len(closes):
        return None
    return closes[i + k] / closes[i] - 1


def reaction_return(closes, i):
    """The reaction-day move itself: close[i] vs close[i-1]. None if i==0."""
    if i == 0:
        return None
    return closes[i] / closes[i - 1] - 1


def summarize_moves(moves):
    """Aggregate a list of returns into the reported stats."""
    moves = [m for m in moves if m is not None]
    n = len(moves)
    if n == 0:
        return {"n": 0}
    ups = sum(1 for m in moves if m > 0)
    return {
        "n": n,
        "avg": sum(moves) / n,
        "avg_abs": sum(abs(m) for m in moves) / n,
        "pct_up": ups / n,
        "max_up": max(moves),
        "max_down": min(moves),
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_moves(title, s, base_up):
    print(f"  {title}")
    if s["n"] == 0:
        print("    events          : 0  -- nothing to report")
        return
    print(f"    events          : {s['n']:>7}")
    print(f"    average move     : {s['avg'] * 100:+7.2f}%")
    print(f"    avg |move|       : {s['avg_abs'] * 100:7.2f}%  (typical size, "
          f"direction aside)")
    print(f"    went up          : {s['pct_up'] * 100:6.1f}%  "
          f"(base rate {base_up * 100:.1f}%, "
          f"{(s['pct_up'] - base_up) * 100:+.1f} pts)")
    print(f"    biggest up       : {s['max_up'] * 100:+7.2f}%")
    print(f"    biggest down     : {s['max_down'] * 100:+7.2f}%")


def main():
    parser = argparse.ArgumentParser(
        description="How AAPL reacts right after earnings (event study)."
    )
    parser.add_argument("csv", help="OHLCV CSV with a Volume column (e.g. AAPL.csv)")
    parser.add_argument("--dates-file", default=None,
                        help="File of REAL earnings dates (one per line) to use "
                             "instead of the volume-spike approximation.")
    parser.add_argument("--hold", type=int, default=DEFAULT_HOLD,
                        help=f"Days-after window (default: {DEFAULT_HOLD}).")
    parser.add_argument("--min-rvol", type=float, default=DEFAULT_MIN_RVOL,
                        help="Min relative volume to flag a day as earnings "
                             f"(approx mode; default: {DEFAULT_MIN_RVOL}).")
    parser.add_argument("--max-events", type=int, default=48,
                        help="Cap on detected events (approx mode; default 48).")
    args = parser.parse_args()

    dates, closes, vols = load_ohlcv(args.csv)
    years = len(dates) / 252.0

    approximate = args.dates_file is None
    if approximate:
        idxs, rvol = detect_earnings_days(dates, closes, vols, args.min_rvol,
                                          args.max_events)
    else:
        wanted = set(load_dates_file(args.dates_file))
        # Match supplied dates to row indices (by exact string or parsed date).
        want_keys = {_date_key(d) for d in wanted}
        idxs = [i for i, d in enumerate(dates)
                if d in wanted or _date_key(d) in want_keys]
        idxs.sort()

    if not idxs:
        sys.exit("No earnings events identified — nothing to analyze.")

    # Normal base rates over ALL days.
    rets = daily_returns(closes)
    base_up_1d = sum(1 for r in rets if r > 0) / len(rets)
    normal_abs_1d = sum(abs(r) for r in rets) / len(rets)
    fwd_all = [fwd_return(closes, i, args.hold) for i in range(len(closes))]
    fwd_all = [r for r in fwd_all if r is not None]
    base_up_kd = sum(1 for r in fwd_all if r > 0) / len(fwd_all)

    # Per-event moves: reaction day (day the market reacted) and k-day forward.
    react = [(i, reaction_return(closes, i)) for i in idxs]
    fwd = [(i, fwd_return(closes, i, args.hold)) for i in idxs]

    # Chronological 70/30 split of the EVENTS.
    split = int(len(idxs) * IN_SAMPLE_FRACTION)
    is_idx, oos_idx = set(idxs[:split]), set(idxs[split:])

    r_is = summarize_moves([m for i, m in react if i in is_idx])
    r_oos = summarize_moves([m for i, m in react if i in oos_idx])
    r_all = summarize_moves([m for _, m in react])
    f_is = summarize_moves([m for i, m in fwd if i in is_idx])
    f_oos = summarize_moves([m for i, m in fwd if i in oos_idx])
    f_all = summarize_moves([m for _, m in fwd])

    # ----- Report -----
    print("=" * 68)
    print("  EARNINGS REACTION STUDY  (AAPL, historical)")
    print("=" * 68)
    if approximate:
        print("  *** EARNINGS DATES ARE APPROXIMATE ***")
        print("  No earnings-date source was reachable, so these are DETECTED as")
        print(f"  volume x price-jump days (rvol >= {args.min_rvol}, >= "
              f"{MIN_EVENT_GAP} trading days apart), NOT official dates.")
        print("  Apple reports after the close -> the detected day is the first")
        print("  post-earnings session (the reaction day itself). A few macro-shock")
        print("  days (e.g. COVID/tariff selloffs) may slip in — not real earnings.")
    else:
        print(f"  earnings dates  : from {args.dates_file} (user-supplied, real)")
    print("-" * 68)
    print(f"  data            : {args.csv}  ({len(dates):,} days, "
          f"{dates[0]} -> {dates[-1]}, ~{years:.1f}y)")
    print(f"  events found    : {len(idxs)}  (~{len(idxs) / years:.1f} per year; "
          f"earnings are ~4/year)")
    print(f"  split           : {len(is_idx)} in-sample / {len(oos_idx)} out-of-sample")
    print(f"  detected dates  : {', '.join(dates[i] for i in idxs)}")
    print("=" * 68)
    print(f"  REACTION DAY MOVE  (close-to-close on the event day)")
    print(f"  normal day: up {base_up_1d * 100:.1f}% of the time, "
          f"avg |move| {normal_abs_1d * 100:.2f}%")
    print("-" * 68)
    print_moves("IN-SAMPLE (first 70%)", r_is, base_up_1d)
    print()
    print_moves("OUT-OF-SAMPLE (last 30%)", r_oos, base_up_1d)
    print("=" * 68)
    print(f"  {args.hold}-DAY-AFTER DRIFT  (event day -> {args.hold} days later)")
    print(f"  normal {args.hold}-day window: up {base_up_kd * 100:.1f}% of the time")
    print("-" * 68)
    print_moves("IN-SAMPLE (first 70%)", f_is, base_up_kd)
    print()
    print_moves("OUT-OF-SAMPLE (last 30%)", f_oos, base_up_kd)
    print("=" * 68)

    # ----- Verdict -----
    print("  VERDICT")
    # 1. Magnitude: are reaction moves bigger than a normal day?
    mag = (r_all["avg_abs"] / normal_abs_1d) if normal_abs_1d else 0.0
    if mag >= 1.5:
        mag_txt = (f"AAPL makes OUTSIZED moves after earnings — about "
                   f"{mag:.1f}x a normal day's swing ({r_all['avg_abs'] * 100:.2f}% "
                   f"vs {normal_abs_1d * 100:.2f}%).")
    else:
        mag_txt = (f"Post-earnings moves are NOT much bigger than normal "
                   f"({mag:.1f}x a normal day).")
    print(f"  - {mag_txt}")

    # 2. Direction: is the up-rate reliably different from a coin flip?
    up = r_all["pct_up"]
    if abs(up - 0.5) < 0.15:
        dir_txt = (f"DIRECTION is basically a coin flip (up {up * 100:.0f}% of "
                   f"the time) — the surprise is in SIZE, not a reliable "
                   f"pop or drop. Looks largely priced in / random.")
    elif up >= 0.65:
        dir_txt = f"Tends to POP — up {up * 100:.0f}% of the time after earnings."
    elif up <= 0.35:
        dir_txt = f"Tends to DROP — up only {up * 100:.0f}% of the time after earnings."
    else:
        dir_txt = (f"Mild directional lean (up {up * 100:.0f}%), but not strong "
                   f"enough to rely on.")
    print(f"  - {dir_txt}")

    # 3. Stability across the split.
    if r_is["n"] and r_oos["n"]:
        d_up = abs(r_oos["pct_up"] - r_is["pct_up"])
        d_avg = abs(r_oos["avg"] - r_is["avg"])
        if d_up < 0.15 and d_avg < 0.01:
            stab = (f"STABLE across time (up-rate {r_is['pct_up'] * 100:.0f}% -> "
                    f"{r_oos['pct_up'] * 100:.0f}%, avg move "
                    f"{r_is['avg'] * 100:+.2f}% -> {r_oos['avg'] * 100:+.2f}%).")
        else:
            stab = (f"CHANGED across time (up-rate {r_is['pct_up'] * 100:.0f}% -> "
                    f"{r_oos['pct_up'] * 100:.0f}%, avg move "
                    f"{r_is['avg'] * 100:+.2f}% -> {r_oos['avg'] * 100:+.2f}%) — "
                    f"behavior is not constant.")
        print(f"  - Behavior was {stab}")

    if len(idxs) < 20:
        print(f"  - NOTE: only {len(idxs)} events — modest sample, treat as "
              f"indicative.")
    if approximate:
        print(f"  - REMINDER: dates are APPROXIMATE (volume x price-jump days), "
              f"not official earnings dates.")
    print("=" * 68)


if __name__ == "__main__":
    main()
