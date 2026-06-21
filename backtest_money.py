#!/usr/bin/env python3
"""
backtest_money.py — turn a validated signal into demo-trading dollar P&L.

validate.py answers "does this signal predict direction better than chance?".
This script answers the next question: "if I had actually TRADED it on a demo
account, would I have made or lost money?" A signal can predict direction
correctly and STILL lose money if the entry price was too expensive — that's
the whole point of measuring dollars, not just win rate.

This is Polymarket binary-market logic:
  - Each candle has up_price and dn_price: the cost (in $, between 0 and 1) to
    buy one share of the UP or DN outcome.
  - The winning side pays out $1.00 per share; the losing side pays $0.

A single trade (fixed $10 stake by default):
  1. Signal fires on a row -> we bet a fixed side (--bet UP or --bet DN).
  2. We buy shares of that side at the row's actual price:
        shares = stake / entry_price
  3. The candle resolves via the `outcome` column ("UP" / "DN").
  4. If our side won  -> payout = shares * $1.00,  profit = payout - stake.
     If our side lost -> profit = -stake.

Discipline (same as validate.py):
  - Locked-down eval for the rule (reused from validate.py).
  - Chronological 70/30 split; out-of-sample P&L is the number that matters.

Usage:
    python3 backtest_money.py "rsi < 30" --bet DN
    python3 backtest_money.py "momentum_3m > 0.5" --bet UP --stake 25 --fee 0.02

Pure historical simulation — no live trading, no orders, no broker.
"""

import argparse
import sys

# Reuse the loader, locked-down rule eval, and split fraction from validate.py
# so the two tools stay consistent (same data parsing, same 70/30 boundary,
# same safe evaluation of the signal expression).
from validate import (
    DATA_FILE,
    FEATURE_COLUMNS,
    IN_SAMPLE_FRACTION,
    compile_rule,
    load_rows,
    signal_fires,
)

# Default per-trade stake in dollars.
DEFAULT_STAKE = 10.0


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def simulate(rows, code, bet, stake, fee):
    """Simulate trading the signal over a set of rows.

    For every row where the signal fires we place one trade on `bet` (UP/DN)
    at that row's actual entry price (up_price for UP, dn_price for DN).

    Returns a dict of aggregate stats for the split.
    """
    # The price column we'd actually pay depending on which side we bet.
    price_col = "up_price" if bet == "UP" else "dn_price"

    trades = 0          # trades actually taken
    wins = 0            # trades whose side matched the outcome
    skipped = 0         # signal fired but price was untradeable
    total_pnl = 0.0     # sum of per-trade profit (after fees)
    total_staked = 0.0  # sum of stake over all trades
    total_entry = 0.0   # sum of entry prices paid (for the average)

    for r in rows:
        if not signal_fires(code, r):
            continue

        entry = r[price_col]
        # Can't trade if the price is missing/degenerate: <=0 means free/no
        # market, >=1.0 means no possible profit (you'd pay >= the $1 payout).
        if entry is None or entry <= 0.0 or entry >= 1.0:
            skipped += 1
            continue

        shares = stake / entry          # how many $1-payout shares we buy
        won = (r["outcome"] == bet)
        payout = shares * 1.0 if won else 0.0
        # Profit = what we got back, minus what we put in, minus the fee.
        pnl = payout - stake - fee

        trades += 1
        wins += 1 if won else 0
        total_pnl += pnl
        total_staked += stake
        total_entry += entry

    return {
        "trades": trades,
        "wins": wins,
        "skipped": skipped,
        "win_rate": (wins / trades) if trades else 0.0,
        "total_pnl": total_pnl,
        "avg_pnl": (total_pnl / trades) if trades else 0.0,
        "avg_entry": (total_entry / trades) if trades else 0.0,
        "total_staked": total_staked,
        "return_on_stake": (total_pnl / total_staked) if total_staked else 0.0,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_block(title, s):
    """Pretty-print one split's money stats."""
    print(f"  {title}")
    if s["trades"] == 0:
        note = f"  ({s['skipped']:,} untradeable rows skipped)" if s["skipped"] else ""
        print(f"    trades taken    :        0{note}  -- nothing to report")
        return
    print(f"    trades taken    : {s['trades']:>10,}"
          + (f"  ({s['skipped']:,} untradeable rows skipped)" if s["skipped"] else ""))
    print(f"    win rate        : {s['win_rate'] * 100:9.2f}%")
    print(f"    total P&L       : ${s['total_pnl']:>12,.2f}")
    print(f"    avg P&L / trade : ${s['avg_pnl']:>12,.4f}")
    print(f"    avg entry price : ${s['avg_entry']:>12,.4f}  (cost per share; "
          f"lower is cheaper)")
    print(f"    total staked    : ${s['total_staked']:>12,.2f}")
    print(f"    return on stake : {s['return_on_stake'] * 100:+9.2f}%")


def verdict_line(oos):
    """Plain verdict based on out-of-sample dollar P&L."""
    if oos["trades"] == 0:
        return ["VERDICT: NO TRADES OUT-OF-SAMPLE — can't judge."]

    lines = []
    if oos["total_pnl"] > 0:
        lines.append(f"VERDICT: PROFITABLE OUT-OF-SAMPLE "
                     f"(${oos['total_pnl']:+,.2f} over {oos['trades']:,} trades)")
    else:
        lines.append(f"VERDICT: LOSES MONEY OUT-OF-SAMPLE "
                     f"(${oos['total_pnl']:+,.2f} over {oos['trades']:,} trades)")
        # The key insight: did it predict well but still bleed money on price?
        # If more than half the trades won yet we still lost, the entry price
        # was too high to profit.
        if oos["win_rate"] > 0.50:
            lines.append(f"NOTE: won {oos['win_rate'] * 100:.1f}% of trades but "
                         f"still lost — predicts direction but entry price too "
                         f"high (avg ${oos['avg_entry']:.4f}/share; need to win "
                         f"more than {oos['avg_entry'] * 100:.1f}% just to break "
                         f"even).")
    return lines


def main():
    parser = argparse.ArgumentParser(
        description="Backtest a signal's dollar P&L (Polymarket binary logic)."
    )
    parser.add_argument(
        "rule",
        help='Signal rule, e.g. "rsi < 30" or "momentum_3m > 0.5 and rsi < 40"',
    )
    parser.add_argument(
        "--bet",
        choices=["UP", "DN"],
        required=True,
        help="Which side to buy when the signal fires (UP or DN).",
    )
    parser.add_argument(
        "--stake", type=float, default=DEFAULT_STAKE,
        help=f"Dollars staked per trade (default: {DEFAULT_STAKE}).",
    )
    parser.add_argument(
        "--fee", type=float, default=0.0,
        help="Flat fee in dollars subtracted per trade (default: 0).",
    )
    parser.add_argument(
        "--data", default=DATA_FILE,
        help=f"CSV data file (default: {DATA_FILE}).",
    )
    args = parser.parse_args()

    if args.stake <= 0:
        sys.exit("--stake must be positive.")

    # Load and split chronologically (file is already in time order), exactly
    # like validate.py.
    rows, bad = load_rows(args.data)
    if not rows:
        sys.exit(f"No usable rows loaded from {args.data}.")

    split = int(len(rows) * IN_SAMPLE_FRACTION)
    in_sample = rows[:split]
    out_sample = rows[split:]

    code = compile_rule(args.rule)
    is_stats = simulate(in_sample, code, args.bet, args.stake, args.fee)
    oos_stats = simulate(out_sample, code, args.bet, args.stake, args.fee)

    # Report.
    print("=" * 64)
    print("  MONEY BACKTEST  (demo trades — historical simulation only)")
    print("=" * 64)
    print(f"  rule            : {args.rule}")
    print(f"  betting side    : {args.bet}")
    print(f"  stake / trade   : ${args.stake:,.2f}")
    print(f"  fee / trade     : ${args.fee:,.4f}")
    print(f"  data            : {args.data}")
    print(f"  total rows used : {len(rows):,}" + (f"  ({bad:,} skipped)" if bad else ""))
    print(f"  split           : {len(in_sample):,} in-sample / "
          f"{len(out_sample):,} out-of-sample")
    print("-" * 64)
    print_block("IN-SAMPLE  (first 70%)", is_stats)
    print()
    print_block("OUT-OF-SAMPLE  (last 30%)", oos_stats)
    print("-" * 64)
    for line in verdict_line(oos_stats):
        print(f"  {line}")
    print("=" * 64)


if __name__ == "__main__":
    main()
