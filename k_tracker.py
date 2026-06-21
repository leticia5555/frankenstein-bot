#!/usr/bin/env python3
"""
k_tracker.py — K-tracking + running multiple-testing (Bonferroni) correction.

This is the credibility spine of the edge-validation engine. The danger it
guards against: "test 50 things, one looks great, declare victory." If you try
enough random ideas, some will pass any fixed significance bar by luck. So we
COUNT how many distinct things a user has tried (K) and RAISE the bar
accordingly — the Bonferroni threshold 0.05 / K.

Key concepts
------------
- A "hypothesis" (a.k.a. an idea / family) is the broad concept under test,
  e.g. "rsi_mean_reversion". You group variations under it with a label.
- A "variation" is one concrete test config. Its fingerprint is
      hash(formula, market, horizon, entry_cfg).
  Re-running the SAME fingerprint is FREE — it does not increment K (dedup).
- K (per hypothesis) = number of DISTINCT fingerprints tried on that idea.
  This is what drives the Bonferroni bar for that idea.
- lifetime_k(user) = total distinct fingerprints the user has ever tried,
  across all hypotheses (a global "how trigger-happy is this person" count).

Design note: the spec's fingerprint hashes (formula, market, horizon,
entry_cfg) only, so to support "K per hypothesis" we store an explicit
`hypothesis` family label alongside the fingerprint. The fingerprint still
identifies the exact variation; the label groups variations into one idea.

Verdict tiers (Spanish, as specified)
-------------------------------------
  VENTAJA REAL  -> p < 0.05 / K     (passes the K-adjusted Bonferroni gate)
  MARGINAL      -> p < 0.05 but >= 0.05 / K  (significant raw, fails the bar)
  RUIDO         -> p >= 0.05        (not even raw-significant)
  (SOBREAJUSTADO is decided later by the out-of-sample test, not here.)

Stdlib only: sqlite3 + hashlib + json. No external dependencies.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

# Tier constants (so callers don't hardcode strings).
VENTAJA_REAL = "VENTAJA REAL"
MARGINAL = "MARGINAL"
RUIDO = "RUIDO"

ALPHA = 0.05  # base significance level before the multiple-testing correction


# ---------------------------------------------------------------------------
# Pure helpers (no DB) — usable standalone
# ---------------------------------------------------------------------------

def fingerprint(formula, market, horizon, entry_cfg):
    """Stable fingerprint of one test variation.

    entry_cfg may be a dict (canonicalized with sorted keys) or any value with
    a stable str(). Returns a short hex digest.
    """
    if isinstance(entry_cfg, (dict, list)):
        cfg = json.dumps(entry_cfg, sort_keys=True, separators=(",", ":"))
    else:
        cfg = "" if entry_cfg is None else str(entry_cfg)
    blob = "|".join([
        str(formula).strip(),
        str(market).strip(),
        str(horizon).strip(),
        cfg,
    ])
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def bonferroni_threshold(K):
    """The K-adjusted significance bar: 0.05 / K.

    K must be a positive integer (you have tried at least one thing).
    """
    if not isinstance(K, int) or K < 1:
        raise ValueError(f"K must be a positive integer, got {K!r}.")
    return ALPHA / K


def verdict(p_value, K):
    """Classify a result given its p-value and the running K.

    Returns one of VENTAJA_REAL / MARGINAL / RUIDO. SOBREAJUSTADO is NOT
    decided here (that's the out-of-sample stage).
    """
    if p_value is None:
        raise ValueError("p_value is missing (None) — cannot judge a test "
                         "without a p-value.")
    if not (0.0 <= p_value <= 1.0):
        raise ValueError(f"p_value must be in [0, 1], got {p_value!r}.")
    thr = bonferroni_threshold(K)
    if p_value < thr:
        return VENTAJA_REAL
    if p_value < ALPHA:
        return MARGINAL
    return RUIDO


def explain(K):
    """Plain-language (Spanish) explainer of the current bar."""
    thr = bonferroni_threshold(K)
    return (f"Probaste {K} variaci{'ón' if K == 1 else 'ones'}; con esa "
            f"cantidad el umbral de azar sube a {thr:.4f}.")


# ---------------------------------------------------------------------------
# The tracker (SQLite-backed)
# ---------------------------------------------------------------------------

class KTracker:
    """Persists test history and answers K / threshold / verdict queries.

    Usage:
        kt = KTracker("tests.db")          # or ":memory:"
        K = kt.record_test(user_id=..., hypothesis=..., formula=...,
                           market=..., horizon=..., entry_cfg=...,
                           p_value=..., sharpe=...)
        kt.close()
    """

    def __init__(self, path=":memory:"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tests (
                user_id                TEXT NOT NULL,
                hypothesis             TEXT NOT NULL,
                hypothesis_fingerprint TEXT NOT NULL,
                market                 TEXT,
                horizon                TEXT,
                entry_cfg              TEXT,
                formula                TEXT,
                p_value                REAL,
                sharpe                 REAL,
                timestamp              TEXT,
                PRIMARY KEY (user_id, hypothesis, hypothesis_fingerprint)
            )
            """
        )
        self.conn.commit()

    # -- writing -----------------------------------------------------------

    def record_test(self, user_id, hypothesis, formula, market, horizon,
                    entry_cfg, p_value, sharpe=None, timestamp=None):
        """Log a test (dedup by fingerprint within the hypothesis).

        Returns the new K for this (user_id, hypothesis). Re-running an
        identical fingerprint updates its stored p/sharpe/timestamp but does
        NOT increase K.

        Raises ValueError on missing required inputs (don't fabricate).
        """
        missing = [name for name, val in (
            ("user_id", user_id), ("hypothesis", hypothesis),
            ("formula", formula), ("market", market), ("horizon", horizon),
        ) if val is None or (isinstance(val, str) and not val.strip())]
        if missing:
            raise ValueError("Missing required input(s): " + ", ".join(missing))
        if p_value is None:
            raise ValueError("p_value is required to record a test.")
        if not (0.0 <= p_value <= 1.0):
            raise ValueError(f"p_value must be in [0, 1], got {p_value!r}.")

        fp = fingerprint(formula, market, horizon, entry_cfg)
        cfg_str = (json.dumps(entry_cfg, sort_keys=True)
                   if isinstance(entry_cfg, (dict, list))
                   else ("" if entry_cfg is None else str(entry_cfg)))
        ts = timestamp or datetime.now(timezone.utc).isoformat()

        # Upsert: identical (user, hypothesis, fingerprint) is free for K.
        self.conn.execute(
            """
            INSERT INTO tests (user_id, hypothesis, hypothesis_fingerprint,
                               market, horizon, entry_cfg, formula,
                               p_value, sharpe, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, hypothesis, hypothesis_fingerprint)
            DO UPDATE SET p_value=excluded.p_value,
                          sharpe=excluded.sharpe,
                          timestamp=excluded.timestamp
            """,
            (user_id, hypothesis, fp, market, horizon, cfg_str, formula,
             p_value, sharpe, ts),
        )
        self.conn.commit()
        return self.k(user_id, hypothesis)

    # -- reading -----------------------------------------------------------

    def k(self, user_id, hypothesis):
        """K for one idea: distinct fingerprints tried on (user_id, hypothesis)."""
        row = self.conn.execute(
            "SELECT COUNT(*) AS k FROM tests WHERE user_id=? AND hypothesis=?",
            (user_id, hypothesis),
        ).fetchone()
        return row["k"]

    def lifetime_k(self, user_id):
        """Total distinct fingerprints this user has ever tried (all ideas)."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT hypothesis_fingerprint) AS k "
            "FROM tests WHERE user_id=?",
            (user_id,),
        ).fetchone()
        return row["k"]

    def evaluate(self, user_id, hypothesis, p_value):
        """Convenience: current K, threshold, verdict, and explainer together."""
        K = self.k(user_id, hypothesis)
        if K < 1:
            raise ValueError(f"No tests recorded yet for {user_id}/{hypothesis}.")
        return {
            "K": K,
            "threshold": bonferroni_threshold(K),
            "verdict": verdict(p_value, K),
            "explain": explain(K),
        }

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
# Self-test: log 20 tests, watch K rise and the bar tighten.
# ---------------------------------------------------------------------------

def _self_test():
    print("=" * 68)
    print("  K_TRACKER SELF-TEST  (simulate one user probing 20 variations)")
    print("=" * 68)
    kt = KTracker(":memory:")
    user = "leticia"
    hyp = "rsi_mean_reversion"

    # Simulate 20 distinct variations (different RSI thresholds). Make the
    # p-values realistic: mostly noise, with a couple that look strong — exactly
    # the "test many, one shines" trap the tracker exists to catch.
    import random
    random.seed(7)
    print(f"\n{'#':>2}  {'variation':<22} {'K':>3}  {'thr=0.05/K':>10}  "
          f"{'p':>7}  verdict")
    print("-" * 68)
    flagged = None
    for i in range(1, 21):
        threshold_rsi = 40 - i           # 39, 38, ... distinct formulas
        formula = f"rsi < {threshold_rsi}"
        entry_cfg = {"size": 1.0, "stop": 0.02}
        # One planted "winner" at i==13: p=0.011 is raw-significant (<0.05) and
        # would be VENTAJA REAL as a lone test, but should be DOWNGRADED once K
        # is large — the exact trap this module exists to catch.
        p = 0.011 if i == 13 else round(random.uniform(0.06, 0.9), 4)
        K = kt.record_test(
            user_id=user, hypothesis=hyp, formula=formula,
            market="BTC", horizon="15m", entry_cfg=entry_cfg,
            p_value=p, sharpe=round(random.uniform(-0.5, 2.5), 2),
        )
        v = verdict(p, K)
        thr = bonferroni_threshold(K)
        if i == 13:
            flagged = (K, thr, p, v)
        print(f"{i:>2}  {formula:<22} {K:>3}  {thr:>10.4f}  {p:>7.4f}  {v}")

    print("-" * 68)
    print(explain(kt.k(user, hyp)))
    print(f"lifetime_k({user}) = {kt.lifetime_k(user)} distinct tests total")

    # Show the trap explicitly: the planted winner at K=13.
    if flagged:
        K, thr, p, v = flagged
        print()
        print(f"  Planted 'winner' had p={p} at K={K}: raw-significant (p<0.05),")
        print(f"  but the Bonferroni bar was {thr:.4f}. Verdict: {v}.")
        same = verdict(p, 1)
        print(f"  Had it been the user's ONLY test (K=1, bar 0.05), it would be: "
              f"{same}.")
        print("  -> K-tracking is what stops one lucky test from faking an edge.")

    # Dedup demonstration: re-run an identical fingerprint -> K must not grow.
    before = kt.k(user, hyp)
    kt.record_test(user_id=user, hypothesis=hyp, formula="rsi < 39",
                   market="BTC", horizon="15m",
                   entry_cfg={"size": 1.0, "stop": 0.02}, p_value=0.5)
    after = kt.k(user, hyp)
    print()
    print(f"  Dedup check: re-ran an identical config -> K {before} -> {after} "
          f"({'unchanged, correct' if before == after else 'BUG'}).")

    kt.close()
    print("=" * 68)


if __name__ == "__main__":
    _self_test()
