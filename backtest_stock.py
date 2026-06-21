#!/usr/bin/env python3
"""
backtest_stock.py — run our validation discipline on a REAL stock.

Same skepticism as validate.py / backtest_money.py (chronological 70/30 split,
real entry prices, catch the lucky-longshot that fakes the whole result), but
the TRADE MATH IS DIFFERENT because a stock is not a Polymarket binary market.

  Polymarket: buy a share at price P, it settles at $1 (win) or $0 (lose).
  Stock:      buy at today's close, hold N days, sell at a future close.
              LONG  profit_pct = (exit - entry) / entry
              SHORT profit_pct = (entry - exit) / entry
  Dollar P&L on a fixed stake = stake * profit_pct.

There is NO $1-payout logic here. Profit comes from the price change.

Data: ~2 years of DAILY closes from Yahoo Finance (server-side fetch). If the
network egress policy blocks Yahoo (common in sandboxes), pass --csv FILE to
read closes from a local CSV instead, so the tool still works offline.

Signal: standard 14-day RSI (Wilder smoothing). Default fires when RSI < 30
("oversold"). Tunable threshold, direction, and holding period.

Usage:
    python3 backtest_stock.py AAPL
    python3 backtest_stock.py AAPL --direction short
    python3 backtest_stock.py NVDA --rsi-below 25 --hold 10
    python3 backtest_stock.py AAPL --csv aapl.csv        # offline data source

Pure historical simulation. No live trading, no orders. One file.
"""

import argparse
import csv
import json
import sys
import urllib.request

RSI_PERIOD = 14
IN_SAMPLE_FRACTION = 0.70  # same 70/30 boundary as validate.py


# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

def fetch_yahoo(ticker, rng="2y", interval="1d"):
    """Pull daily closes from Yahoo Finance.

    Returns (dates, closes) as parallel lists, oldest first. `dates` are
    'YYYY-MM-DD' strings derived from the unix timestamps; `closes` are floats.
    Rows with a null close (Yahoo occasionally emits these on holidays) are
    dropped, keeping dates and closes aligned.

    Tries query1 then query2; a browser-like User-Agent reduces 403s.
    """
    import datetime as _dt

    last_err = None
    for host in ("query1", "query2"):
        url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
               f"{ticker}?range={rng}&interval={interval}")
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0 Safari/537.36"),
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except Exception as e:  # network/HTTP error — try the next host
            last_err = e
            continue

        result = data.get("chart", {}).get("result")
        if not result:
            err = data.get("chart", {}).get("error")
            last_err = RuntimeError(f"Yahoo returned no data for {ticker}: {err}")
            continue

        res = result[0]
        timestamps = res.get("timestamp") or []
        quote = res["indicators"]["quote"][0]
        raw_closes = quote.get("close") or []

        dates, closes = [], []
        for ts, c in zip(timestamps, raw_closes):
            if c is None:
                continue
            dates.append(_dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d"))
            closes.append(float(c))
        if closes:
            return dates, closes
        last_err = RuntimeError(f"No usable closes parsed for {ticker}.")

    # Both hosts failed — surface a clear, actionable error.
    raise SystemExit(
        f"Could not fetch {ticker} from Yahoo Finance: {last_err}\n"
        f"If this is an egress-allowlist block, add 'query1.finance.yahoo.com' "
        f"(and query2) to the environment's network settings, or pass a local "
        f"data file with --csv FILE."
    )


def load_csv(path):
    """Load (dates, closes) from a local CSV — an offline fallback.

    Looks for a 'close' column (case-insensitive); for the date it tries
    'date' / 'timestamp', else just uses the row index. Falls back to the last
    numeric column if there's no header named 'close'. Oldest row first.
    """
    dates, closes = [], []
    with open(path, newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    if not rows:
        raise SystemExit(f"{path} is empty.")

    header = [h.strip().lower() for h in rows[0]]
    has_header = any(h in ("close", "adj close", "adjclose") for h in header)

    if has_header:
        for name in ("close", "adj close", "adjclose"):
            if name in header:
                close_idx = header.index(name)
                break
        date_idx = next((header.index(n) for n in ("date", "timestamp")
                         if n in header), None)
        body = rows[1:]
    else:
        # No recognizable header: assume last column is the close.
        close_idx = len(rows[0]) - 1
        date_idx = 0 if len(rows[0]) > 1 else None
        body = rows

    for i, row in enumerate(body):
        try:
            c = float(row[close_idx])
        except (ValueError, IndexError):
            continue
        closes.append(c)
        dates.append(row[date_idx] if date_idx is not None else str(i))
    if not closes:
        raise SystemExit(f"No numeric closes found in {path}.")
    return dates, closes


# ---------------------------------------------------------------------------
# Signal: standard 14-day RSI (Wilder's smoothing)
# ---------------------------------------------------------------------------

def compute_rsi(closes, period=RSI_PERIOD):
    """Return a list of RSI values aligned to `closes`.

    The first `period` entries are None (not enough history yet). Uses the
    standard Wilder method: a simple average of the first `period` gains/losses
    to seed, then exponential smoothing thereafter.
    """
    n = len(closes)
    rsi = [None] * n
    if n <= period:
        return rsi

    gains, losses = [], []
    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def to_rsi(ag, al):
        if al == 0:
            return 100.0  # no losses -> maximally overbought
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    rsi[period] = to_rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = to_rsi(avg_gain, avg_loss)
    return rsi


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

def trade_return(entry, exit_, direction):
    """Fractional return of one trade given entry/exit close and direction."""
    if direction == "long":
        return (exit_ - entry) / entry
    return (entry - exit_) / entry  # short


def base_rate(closes, hold, direction):
    """How often a trade in `direction` would simply have won across ALL days.

    This is the 'just buy randomly' benchmark: of every day that has `hold`
    days of future data, what fraction had a positive return in our direction?
    """
    wins = 0
    total = 0
    for i in range(len(closes) - hold):
        total += 1
        if trade_return(closes[i], closes[i + hold], direction) > 0:
            wins += 1
    return (wins / total) if total else 0.0, total


def run_signal(dates, closes, rsi, threshold, direction, hold, stake):
    """Build the chronological list of trades the signal would have taken.

    A trade is taken on each day where RSI < threshold AND there is enough
    future data to exit `hold` days later. Returns a list of dicts.
    """
    trades = []
    for i in range(len(closes)):
        r = rsi[i]
        if r is None or r >= threshold:
            continue
        if i + hold >= len(closes):
            continue  # not enough future data to close the trade
        entry = closes[i]
        exit_ = closes[i + hold]
        ret = trade_return(entry, exit_, direction)
        trades.append({
            "entry_date": dates[i],
            "exit_date": dates[i + hold],
            "entry": entry,
            "exit": exit_,
            "ret": ret,
            "pnl": stake * ret,
            "rsi": r,
        })
    return trades


def summarize(trades, stake):
    """Aggregate stats for one split of trades."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    pnls = [t["pnl"] for t in trades]
    rets = [t["ret"] for t in trades]
    wins = sum(1 for t in trades if t["ret"] > 0)
    total_pnl = sum(pnls)
    total_staked = stake * n
    best = max(trades, key=lambda t: t["pnl"])
    pnl_without_best = total_pnl - best["pnl"]
    return {
        "n": n,
        "wins": wins,
        "win_rate": wins / n,
        "total_pnl": total_pnl,
        "total_staked": total_staked,
        "return_on_staked": total_pnl / total_staked if total_staked else 0.0,
        "avg_ret": sum(rets) / n,
        "best": best,
        "pnl_without_best": pnl_without_best,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

LOW_SAMPLE = 10  # fewer trades than this -> flag low-confidence


def print_block(title, s):
    print(f"  {title}")
    if s["n"] == 0:
        print("    trades taken    : 0  -- signal never fired (with enough future data)")
        return
    flag = "  *** LOW SAMPLE — low confidence ***" if s["n"] < LOW_SAMPLE else ""
    print(f"    trades taken    : {s['n']:>8,}{flag}")
    print(f"    win rate        : {s['win_rate'] * 100:8.2f}%  (positive-return trades)")
    print(f"    total P&L       : ${s['total_pnl']:>12,.2f}")
    print(f"    return on stake : {s['return_on_staked'] * 100:+8.2f}%  "
          f"(on ${s['total_staked']:,.0f} staked)")
    print(f"    avg ret / trade : {s['avg_ret'] * 100:+8.2f}%")
    b = s["best"]
    print(f"    max single trade: ${b['pnl']:>12,.2f}  "
          f"({b['ret'] * 100:+.2f}% on {b['entry_date']}->{b['exit_date']})")
    print(f"    P&L w/o best    : ${s['pnl_without_best']:>12,.2f}  "
          f"(does the edge survive without the one lucky trade?)")


def verdict(oos):
    """Plain-language verdict from the out-of-sample split."""
    if oos["n"] == 0:
        return "NO EDGE / LOSES", "Signal never fired out-of-sample — nothing to trade."
    pos = oos["total_pnl"] > 0
    survives = oos["pnl_without_best"] > 0
    if pos and survives:
        return ("REAL EDGE",
                "Out-of-sample is profitable AND stays positive after removing "
                "the single best trade — not dependent on one lucky winner.")
    if pos and not survives:
        return ("FRAGILE",
                "Out-of-sample is profitable ONLY because of the single best "
                "trade; remove it and the edge flips negative.")
    return ("NO EDGE / LOSES",
            "Out-of-sample loses money over the period.")


def main():
    parser = argparse.ArgumentParser(
        description="Backtest an RSI signal on a real stock (honest P&L)."
    )
    parser.add_argument("ticker", help="Stock ticker, e.g. AAPL")
    parser.add_argument("--rsi-below", type=float, default=30.0,
                        help="Signal fires when RSI < this (default: 30).")
    parser.add_argument("--direction", choices=["long", "short"], default="long",
                        help="long = buy on signal (default); short = bet it falls.")
    parser.add_argument("--hold", type=int, default=5,
                        help="Trading days to hold before selling (default: 5).")
    parser.add_argument("--stake", type=float, default=1000.0,
                        help="Dollars staked per trade (default: 1000).")
    parser.add_argument("--range", default="2y",
                        help="Yahoo history range (default: 2y).")
    parser.add_argument("--csv", default=None,
                        help="Load closes from a local CSV instead of Yahoo "
                             "(offline fallback).")
    args = parser.parse_args()

    if args.hold < 1:
        sys.exit("--hold must be at least 1.")

    # Data.
    if args.csv:
        dates, closes = load_csv(args.csv)
        source = f"local CSV ({args.csv})"
    else:
        dates, closes = fetch_yahoo(args.ticker, rng=args.range)
        source = f"Yahoo Finance ({args.range} daily)"

    if len(closes) <= RSI_PERIOD + args.hold:
        sys.exit(f"Not enough data ({len(closes)} closes) for RSI + hold.")

    # Signal + trades.
    rsi = compute_rsi(closes)
    trades = run_signal(dates, closes, rsi, args.rsi_below,
                        args.direction, args.hold, args.stake)

    # Chronological 70/30 split ON THE FIRES (same discipline as validate.py).
    split = int(len(trades) * IN_SAMPLE_FRACTION)
    is_trades = trades[:split]
    oos_trades = trades[split:]
    is_stats = summarize(is_trades, args.stake)
    oos_stats = summarize(oos_trades, args.stake)

    # Base rate: how often the move went our way across ALL days.
    br, br_n = base_rate(closes, args.hold, args.direction)

    # Report.
    print("=" * 66)
    print("  STOCK BACKTEST  (historical simulation only)")
    print("=" * 66)
    print(f"  ticker          : {args.ticker}")
    print(f"  data source     : {source}")
    print(f"  date range      : {dates[0]} -> {dates[-1]}  ({len(closes)} closes)")
    print(f"  signal          : RSI(14) < {args.rsi_below:g}")
    print(f"  direction       : {args.direction.upper()}")
    print(f"  hold            : {args.hold} trading days")
    print(f"  stake / trade   : ${args.stake:,.2f}")
    print(f"  total fires     : {len(trades):,}  (days signal fired with exit data)")
    if trades:
        print(f"  split           : {len(is_trades):,} in-sample / "
              f"{len(oos_trades):,} out-of-sample")
    print("-" * 66)
    print_block("IN-SAMPLE  (first 70% of fires)", is_stats)
    print()
    print_block("OUT-OF-SAMPLE  (last 30% of fires)", oos_stats)
    print("-" * 66)
    print(f"  base rate       : {br * 100:.2f}% of ALL {br_n:,} days went "
          f"{'UP' if args.direction == 'long' else 'DOWN'} over a "
          f"{args.hold}-day hold")
    if oos_stats["n"]:
        beats = oos_stats["win_rate"] - br
        print(f"  signal win rate : {oos_stats['win_rate'] * 100:.2f}% "
              f"out-of-sample ({beats * 100:+.2f} pts vs base rate)")
    print("-" * 66)

    label, why = verdict(oos_stats)
    print(f"  VERDICT: {label}")
    print(f"  {why}")
    if len(trades) < LOW_SAMPLE * 2:
        print(f"  WARNING: only {len(trades)} total fires — small sample, "
              f"treat as low-confidence.")
    print("=" * 66)

    # One-line plain-English summary.
    if oos_stats["n"]:
        print(f"\nSummary: {args.direction.upper()} {args.ticker} on RSI<"
              f"{args.rsi_below:g} -> {label}. Out-of-sample "
              f"{oos_stats['n']} trades, "
              f"${oos_stats['total_pnl']:+,.0f} P&L "
              f"(${oos_stats['pnl_without_best']:+,.0f} without the best trade).")
    else:
        print(f"\nSummary: {args.direction.upper()} {args.ticker} on RSI<"
              f"{args.rsi_below:g} -> {label} (no out-of-sample trades).")


if __name__ == "__main__":
    main()
