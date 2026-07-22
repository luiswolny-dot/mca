"""Composite 0-100 buy score.

Architecture of the decision
----------------------------
    Candidate
       |
       +--> hard gates ------------- fail -> score 0, no trade, log the reason
       |
       +--> feature extraction  (point-in-time, as_of enforced)
       |
       +--> per-feature normalisation to [0,1]  (cohort-relative percentile)
       |
       +--> weighted aggregation --> raw score 0..100
       |
       +--> ML calibration --------> P(win)
       |
       +--> expected value ---------> trade / no trade
                                        ^
                                        |
                          THIS is the actual decision rule

Design decision — the score is a ranking device, not the decision
-----------------------------------------------------------------
"Buy everything above 75" is intuitive and wrong, because it says nothing about
cost. On a token with $30k liquidity, a $500 round trip costs roughly 3.3% in
price impact plus priority fee plus (on EVM) up to 10% in taxes. A setup with a
55% win rate and symmetric payoffs is a *losing* strategy after those costs.

So the score feeds a calibrated win-probability model, and the trade is only
taken when

    EV = p*E[win] - (1-p)*E[loss] - cost  >  min_edge

Cost is estimated from the actual pool reserves for the actual intended size.
This is the single most important line in the whole system.

Design decision — cohort-relative, not absolute, thresholds
------------------------------------------------------------
"$50k of 5-minute volume is bullish" is meaningless without context: it is
enormous for a 20-minute-old token with $30k liquidity and unremarkable for a
day-old token with $800k. Every feature is therefore converted to a percentile
against a rolling cohort of tokens of similar age and liquidity bucket
(``CohortStats``, refreshed continuously from live observations). Absolute
thresholds live only in the hard gates, where they encode safety, not attraction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from mca.domain.models import Candidate, Score
from mca.features import technicals as ta
from mca.scoring.gates import GateConfig, evaluate_gates, gates_passed


# --------------------------------------------------------------------------- #
# Normalisation helpers
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CohortStats:
    """Rolling empirical distribution for one (age_bucket, liquidity_bucket).

    Stores sorted samples per feature so we can produce a percentile in O(log n).
    In production this is backed by Redis sorted sets with a sliding 24h window;
    the in-memory version here is what the backtester and the unit tests use.
    """

    samples: dict[str, np.ndarray] = field(default_factory=dict)

    def percentile_of(self, feature: str, value: float) -> float:
        arr = self.samples.get(feature)
        if arr is None or arr.size < 30:
            return 0.5          # not enough evidence -> neutral, never bullish
        return float(np.searchsorted(arr, value, side="right") / arr.size)


def squash(x: float, midpoint: float, steepness: float = 1.0) -> float:
    """Logistic mapping to [0,1]. Used where a *known* absolute scale exists
    (e.g. holder count) and the cohort has not warmed up yet."""
    try:
        return 1.0 / (1.0 + math.exp(-steepness * (x - midpoint) / max(abs(midpoint), 1e-9)))
    except OverflowError:
        return 0.0 if x < midpoint else 1.0


def clip01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ScoreWeights:
    """Group weights, must sum to 100. Tuned by walk-forward optimisation, never
    by hand after a losing week."""

    liquidity_depth: float = 16.0
    volume_momentum: float = 20.0
    holder_dynamics: float = 14.0
    smart_money_flow: float = 18.0
    technicals: float = 15.0
    social: float = 9.0
    safety_margin: float = 8.0

    def as_dict(self) -> dict[str, float]:
        return {
            "liquidity_depth": self.liquidity_depth,
            "volume_momentum": self.volume_momentum,
            "holder_dynamics": self.holder_dynamics,
            "smart_money_flow": self.smart_money_flow,
            "technicals": self.technicals,
            "social": self.social,
            "safety_margin": self.safety_margin,
        }

    def validate(self) -> None:
        total = sum(self.as_dict().values())
        if abs(total - 100.0) > 1e-6:
            raise ValueError(f"ScoreWeights must sum to 100, got {total}")


@dataclass(slots=True)
class ScorerConfig:
    weights: ScoreWeights = field(default_factory=ScoreWeights)
    gates: GateConfig = field(default_factory=GateConfig)
    min_score: float = 70.0
    min_win_probability: float = 0.42
    min_edge_bps: float = 250.0         # required EV after all costs
    social_max_bot_ratio: float = 0.55  # above this, social signal is muted to 0


# --------------------------------------------------------------------------- #
# Feature groups
# --------------------------------------------------------------------------- #


def _liquidity_depth(cand: Candidate, cohort: CohortStats) -> float:
    sec, mkt = cand.security, cand.market
    liq = float(sec.liquidity_usd or 0)
    if liq <= 0:
        return 0.0

    # Depth relative to cohort: more is better, but with diminishing returns —
    # we are hunting asymmetric moves, and a $5m pool cannot 10x on retail flow.
    depth = cohort.percentile_of("liquidity_usd", liq)
    depth = depth * (1.0 - 0.5 * clip01((liq - 300_000) / 1_700_000))

    # Liquidity should be growing, not draining. A shrinking pool on rising price
    # means LPs are exiting into the pump.
    fdv = float(mkt.fdv_usd or 0)
    ratio = liq / fdv if fdv > 0 else 0.0
    healthy_ratio = clip01(ratio / 0.10)     # 10% liq/FDV is a healthy structure

    return clip01(0.6 * depth + 0.4 * healthy_ratio)


def _volume_momentum(cand: Candidate, cohort: CohortStats) -> float:
    mkt = cand.market
    v5 = float(mkt.volume_usd.get(300, 0))
    v60 = float(mkt.volume_usd.get(3600, 0))
    liq = float(cand.security.liquidity_usd or 1)

    turnover = v5 / liq if liq > 0 else 0.0
    turnover_pct = cohort.percentile_of("turnover_5m", turnover)

    # Acceleration: is the last 5 minutes hot relative to the last hour?
    expected_5m = v60 / 12.0
    accel = v5 / expected_5m if expected_5m > 0 else 1.0
    accel_norm = clip01(math.log1p(max(accel - 1.0, 0.0)) / math.log(6.0))

    bsr = mkt.buy_sell_ratio(300) or 1.0
    bsr_norm = clip01((bsr - 0.8) / 1.2)     # 0.8 -> 0, 2.0 -> 1

    return clip01(0.4 * turnover_pct + 0.35 * accel_norm + 0.25 * bsr_norm)


def _holder_dynamics(cand: Candidate, cohort: CohortStats) -> float:
    sec, mkt = cand.security, cand.market
    holders = sec.holder_count or 0
    buyers5 = mkt.unique_buyers_5m or 0

    base = squash(float(holders), midpoint=400.0, steepness=3.0)
    growth = cohort.percentile_of("unique_buyers_5m", float(buyers5))

    # Concentration penalty: even inside the gate, 25% in top-10 is worse
    # than 12%, and the score should say so.
    top10 = sec.top10_holder_pct
    conc = 1.0 - clip01(((top10 or 0.30) - 0.05) / 0.25)

    return clip01(0.35 * base + 0.40 * growth + 0.25 * conc)


def _smart_money_flow(cand: Candidate, cohort: CohortStats) -> float:
    """Wallet-cohort flow.

    Important boundary: this uses *publicly observable on-chain history* to
    identify wallets with good realised PnL, and follows their public trades.
    It does not use non-public information, and it does not attempt to front-run
    any pending transaction. Copy-trading a public ledger is legitimate; using
    the mempool to trade ahead of a specific user's pending order is not, and is
    explicitly out of scope for this system.
    """
    flow = cand.flow
    if flow is None:
        return 0.35     # absence of evidence, not evidence of absence

    liq = float(cand.security.liquidity_usd or 1)
    smart_net = float(flow.smart_net_usd) / max(liq, 1.0)
    whale_net = float(flow.whale_net_usd) / max(liq, 1.0)

    smart_norm = clip01((math.tanh(smart_net * 8.0) + 1.0) / 2.0)
    whale_norm = clip01((math.tanh(whale_net * 5.0) + 1.0) / 2.0)

    breadth = clip01(flow.smart_buyers / 12.0)
    skill = clip01(((flow.top_cohort_pnl_30d or 0.0) + 0.5) / 2.0)

    # Insider accumulation is a *negative*: it means the exit liquidity is us.
    insider_pen = clip01(float(flow.insider_net_usd) / max(liq, 1.0) * 4.0)

    raw = 0.35 * smart_norm + 0.25 * whale_norm + 0.25 * breadth + 0.15 * skill
    return clip01(raw - 0.5 * insider_pen)


def _technicals(cand: Candidate) -> float:
    arr = cand.ohlcv_1m
    if arr is None or len(arr) < 20:
        return 0.4      # young token, no chart yet — neutral-ish, not bullish
    arr = np.asarray(arr, dtype=np.float64)
    closes = arr[:, ta.CLOSE]

    parts: list[tuple[float, float]] = []   # (value, weight)

    r = ta.rsi(closes, 14)
    if r is not None:
        # We want strength, not exhaustion. Peak desirability around 60-68;
        # >80 is where late buyers get filled at the top.
        parts.append((clip01(1.0 - abs(r - 64.0) / 36.0), 0.22))

    m = ta.macd(closes)
    if m is not None:
        _, _, hist = m
        scale = float(np.mean(np.abs(np.diff(closes[-30:])))) or 1e-9
        parts.append((clip01((math.tanh(hist / (scale * 3.0)) + 1.0) / 2.0), 0.20))

    e9, e21 = ta.ema(closes, 9), ta.ema(closes, 21)
    if e21.size:
        spread = (e9[-1] - e21[-1]) / e21[-1] if e21[-1] else 0.0
        parts.append((clip01((math.tanh(spread * 25.0) + 1.0) / 2.0), 0.20))

    v = ta.vwap(arr, lookback=60)
    if v:
        # Above VWAP is constructive; 40%+ above it is extended.
        dist = (closes[-1] - v) / v
        parts.append((clip01(1.0 - abs(dist - 0.08) / 0.45), 0.18))

    mom = ta.momentum_score(closes)
    if mom is not None:
        parts.append((clip01((mom + 1.0) / 2.0), 0.20))

    if not parts:
        return 0.4
    wsum = sum(w for _, w in parts)
    return clip01(sum(v * w for v, w in parts) / wsum)


def _social(cand: Candidate, cfg: ScorerConfig, cohort: CohortStats) -> float:
    soc = cand.social
    if soc is None:
        return 0.3

    # Bot-inflated mention counts are the norm, not the exception. Above the
    # threshold the entire social group is zeroed rather than discounted: a
    # heavily botted mention graph carries no information at all.
    if soc.bot_ratio is not None and soc.bot_ratio > cfg.social_max_bot_ratio:
        return 0.0

    authors = float(soc.unique_authors.get(900, 0))
    mentions = float(soc.mentions.get(900, 0))

    # Unique authors, not mentions, is the real signal — 500 posts from 6
    # accounts is a paid campaign.
    author_pct = cohort.percentile_of("unique_authors_15m", authors)
    diversity = clip01(authors / max(mentions, 1.0) * 2.0)
    reach = clip01(math.log1p(soc.weighted_reach) / math.log1p(1e6))
    sent = clip01((soc.sentiment + 1.0) / 2.0)
    trends = clip01((soc.google_trends or 0.0) / 100.0)

    return clip01(0.35 * author_pct + 0.20 * diversity + 0.20 * reach
                  + 0.15 * sent + 0.10 * trends)


def _safety_margin(cand: Candidate, cfg: ScorerConfig) -> float:
    """How far *past* the minimum bar does this token clear the safety gates?

    A token that barely scrapes every threshold is materially riskier than one
    that clears them all comfortably, even though both "passed".
    """
    sec = cand.security
    scores: list[float] = []

    if sec.top10_holder_pct is not None:
        scores.append(clip01(1.0 - sec.top10_holder_pct / cfg.gates.max_top10_holder_pct))
    if sec.dev_holding_pct is not None:
        scores.append(clip01(1.0 - sec.dev_holding_pct / max(cfg.gates.max_dev_holding_pct, 1e-9)))
    if sec.buy_tax_bps is not None and sec.sell_tax_bps is not None:
        worst = max(sec.buy_tax_bps, sec.sell_tax_bps)
        scores.append(clip01(1.0 - worst / max(cfg.gates.max_buy_tax_bps, 1)))
    if sec.lp_burned is True:
        scores.append(1.0)
    elif sec.lp_locked_pct is not None:
        scores.append(clip01(sec.lp_locked_pct))
    if sec.bundled_supply_pct is not None:
        scores.append(clip01(1.0 - sec.bundled_supply_pct / max(cfg.gates.max_bundled_supply_pct, 1e-9)))

    return float(np.mean(scores)) if scores else 0.0


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #


class Scorer:
    """Stateless apart from the cohort statistics it is handed.

    Deliberately a plain class with an injected ``CohortStats`` rather than a
    module-level singleton, so the backtester can supply cohort stats computed
    strictly from data available before the bar being scored.
    """

    def __init__(self, config: ScorerConfig | None = None) -> None:
        self.config = config or ScorerConfig()
        self.config.weights.validate()

    def score(self, cand: Candidate, cohort: CohortStats | None = None) -> Score:
        cohort = cohort or CohortStats()
        cfg = self.config
        gates = evaluate_gates(cand, cfg.gates)

        if not gates_passed(gates):
            return Score(
                token=cand.token, as_of=cand.as_of, total=0.0,
                components={}, weights=cfg.weights.as_dict(), gates=gates,
            )

        components = {
            "liquidity_depth": _liquidity_depth(cand, cohort),
            "volume_momentum": _volume_momentum(cand, cohort),
            "holder_dynamics": _holder_dynamics(cand, cohort),
            "smart_money_flow": _smart_money_flow(cand, cohort),
            "technicals": _technicals(cand),
            "social": _social(cand, cfg, cohort),
            "safety_margin": _safety_margin(cand, cfg),
        }
        w = cfg.weights.as_dict()
        total = sum(components[k] * w[k] for k in w)

        return Score(
            token=cand.token, as_of=cand.as_of,
            total=round(float(total), 3),
            components={k: round(v * 100, 2) for k, v in components.items()},
            weights=w, gates=gates,
        )


def expected_value_bps(win_prob: float, avg_win_pct: float, avg_loss_pct: float,
                       round_trip_cost_bps: float) -> float:
    """EV of the trade in basis points of notional, after costs.

    ``avg_win_pct`` / ``avg_loss_pct`` come from the realised outcome
    distribution of *this strategy variant*, not from the ladder's theoretical
    targets. The ladder says "+100%"; the realised average winner is what
    matters, and it is always smaller because of partial fills and slippage.
    """
    gross = win_prob * avg_win_pct - (1.0 - win_prob) * abs(avg_loss_pct)
    return gross * 10_000.0 - round_trip_cost_bps


def should_trade(score: Score, cfg: ScorerConfig, avg_win_pct: float,
                 avg_loss_pct: float, round_trip_cost_bps: float) -> tuple[bool, str]:
    """The actual decision rule. Returns (decision, human-readable reason)."""
    if not score.hard_gate_passed:
        return False, f"gate_failed:{','.join(score.failed_gates)}"
    if score.total < cfg.min_score:
        return False, f"score {score.total:.1f} < {cfg.min_score}"

    p = score.win_probability
    if p is None:
        return False, "no calibrated win probability (model not loaded)"
    if p < cfg.min_win_probability:
        return False, f"p_win {p:.3f} < {cfg.min_win_probability}"

    ev = expected_value_bps(p, avg_win_pct, avg_loss_pct, round_trip_cost_bps)
    score.expected_value_bps = ev
    if ev < cfg.min_edge_bps:
        return False, f"EV {ev:.0f}bps < required {cfg.min_edge_bps:.0f}bps"
    return True, f"score={score.total:.1f} p={p:.3f} EV={ev:.0f}bps"
