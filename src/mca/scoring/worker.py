"""Scoring worker: consumes ``Stream.CANDIDATES``, produces ``Stream.SIGNALS``.

Design decision — the rolling cohort lives in this process, in memory
--------------------------------------------------------------------------
``scoring/scorer.py``'s ``CohortStats.percentile_of`` already defines the
correct behaviour for a cold start: fewer than 30 samples for a feature
returns a neutral 0.5, "not enough evidence — never bullish" (see that
module). That means this worker can start with a genuinely empty cohort and
let it warm up from real traffic, rather than needing a pre-seeded historical
distribution before it can run at all. The cohort is bounded (a fixed-size
ring buffer per feature) so memory does not grow unbounded over a long
uptime; production docs mention Redis-backed sorted sets for a sliding
24h window (see ``scoring/scorer.py``'s docstring) — swapping the in-memory
version for that is a real follow-up, not a hidden gap, since the interface
(``CohortStats``) is already the same either way.

Design decision — win_probability is None until a model exists, on purpose
--------------------------------------------------------------------------------
``ml.calibration.WinProbabilityModel.predict()`` already returns ``None`` for
an untrained model. This worker always calls it (never skips it), so the
moment ``ops/tasks.py``'s ``retrain_model_task`` produces a trained model and
it is loaded here, predictions start flowing with no code change — the
"not yet calibrated" state is the model's own honest answer, not a branch
this worker has to remember to remove later.
"""

from __future__ import annotations

import logging
import os
from collections import deque

import numpy as np

from mca.core.bus import Bus, Stream
from mca.domain.models import Candidate, TradeMode
from mca.enrich.worker import payload_to_candidate
from mca.ml.calibration import FEATURE_ORDER, WinProbabilityModel
from mca.risk.gate_service import SignalMessage
from mca.scoring.scorer import CohortStats, Scorer, ScorerConfig, expected_value_bps, should_trade

log = logging.getLogger(__name__)

# Which of scoring/scorer.py's percentile-relative components read raw
# candidate fields the cohort needs samples of. Kept next to the worker that
# owns the cohort's lifetime, not inside scorer.py, which stays free of any
# notion of "how much history is this process keeping."
_COHORT_FEATURES = {
    "liquidity_usd": lambda c: float(c.market.liquidity_usd),
    "volume_5m": lambda c: float(c.market.volume_usd.get(300, 0)),
    "holder_count": lambda c: float(c.market.holder_count or 0),
    "smart_net_usd": lambda c: float(c.flow.smart_net_usd) if c.flow else 0.0,
}


class RollingCohort:
    """Bounded ring-buffer backing for :class:`CohortStats`."""

    def __init__(self, window: int = 500) -> None:
        self.window = window
        self._buffers: dict[str, deque] = {name: deque(maxlen=window) for name in _COHORT_FEATURES}

    def observe(self, cand: Candidate) -> None:
        for name, extractor in _COHORT_FEATURES.items():
            try:
                self._buffers[name].append(extractor(cand))
            except Exception:
                pass

    def snapshot(self) -> CohortStats:
        return CohortStats(samples={name: np.array(buf) for name, buf in self._buffers.items() if buf})


class ScoringWorker:
    def __init__(self, bus: Bus, *, scorer_cfg: ScorerConfig | None = None,
                model: WinProbabilityModel | None = None, mode: TradeMode = TradeMode.PAPER,
                strategy_hash: str = "",
                avg_win_pct: float = 0.85, avg_loss_pct: float = 0.25,
                round_trip_cost_bps: float = 350.0, consumer_name: str | None = None) -> None:
        self.bus = bus
        self.cfg = scorer_cfg or ScorerConfig()
        self.scorer = Scorer(self.cfg)
        self.model = model
        self.mode = mode
        self.strategy_hash = strategy_hash
        self.avg_win_pct = avg_win_pct
        self.avg_loss_pct = avg_loss_pct
        self.round_trip_cost_bps = round_trip_cost_bps
        self.cohort = RollingCohort()
        self.consumer_name = consumer_name or f"scorer-{os.getpid()}"

    async def run(self) -> None:
        await self.bus.ensure_group(Stream.CANDIDATES, "scorer")
        log.info("scoring worker started strategy_hash=%s", self.strategy_hash)
        while True:
            msgs = await self.bus.consume(Stream.CANDIDATES, "scorer", self.consumer_name,
                                          count=10, block_ms=5000)
            for msg in msgs:
                try:
                    await self._handle(msg.payload)
                except Exception:
                    log.exception("failed to score candidate id=%s", msg.id)
                await self.bus.ack(Stream.CANDIDATES, "scorer", msg.id)
            await self.bus.heartbeat("scorer")

    async def _handle(self, payload: dict) -> None:
        cand = payload_to_candidate(payload)
        score = self.scorer.score(cand, self.cohort.snapshot())
        self.cohort.observe(cand)

        if self.model is not None and score.hard_gate_passed:
            features = {k: score.components.get(k, np.nan) for k in FEATURE_ORDER}
            features["score_total"] = score.total
            score.win_probability = self.model.predict(features)
            if score.win_probability is not None:
                score.expected_value_bps = expected_value_bps(
                    score.win_probability, self.avg_win_pct, self.avg_loss_pct,
                    self.round_trip_cost_bps)

        decision, reason = should_trade(score, self.cfg, self.avg_win_pct, self.avg_loss_pct,
                                        self.round_trip_cost_bps)

        msg = SignalMessage(
            token=cand.token, mode=self.mode, strategy_hash=self.strategy_hash, as_of=cand.as_of,
            score_total=score.total, components=score.components, weights=score.weights,
            gates=[{"name": g.name, "passed": g.passed, "detail": g.detail} for g in score.gates],
            gates_passed=score.hard_gate_passed, failed_gates=score.failed_gates,
            win_probability=score.win_probability, expected_value_bps=score.expected_value_bps,
            model_version=self.model.version if self.model else None,
            liquidity_usd=cand.security.liquidity_usd or cand.market.liquidity_usd,
            reserve_quote_usd=_reserve_quote_usd(cand),
            quote_usd_price=_quote_usd_price(cand), atr_pct=None,
            correlation_id=cand.correlation_id,
        )
        await self.bus.publish(Stream.SIGNALS, msg.to_payload())
        log.info("signal published token=%s score=%.1f decision=%s (%s)",
                cand.token.key, score.total, decision, reason)


def _reserve_quote_usd(cand: Candidate):
    """Half of total liquidity as a reserve-side estimate for a balanced
    constant-product pool — see risk/gate_service.py's docstring on why this
    must not be conflated with total liquidity_usd. An exact figure would come
    from pool_reserve_quote directly once its own USD value is known; until
    then this is a documented approximation, not silently wrong math."""
    from decimal import Decimal
    return cand.market.liquidity_usd / Decimal(2)


def _quote_usd_price(cand: Candidate):
    """SOL/USD implied by this pair's own price + reserves, when both are
    available; falls back to a value the caller must override before using it
    for sizing if reserves are missing (0 signals "do not trust this")."""
    from decimal import Decimal
    if cand.market.pool_reserve_base and cand.market.pool_reserve_quote:
        # price_usd is USD per whole base token; reserve ratio gives the
        # implied quote(SOL)-per-base rate, so price_usd / that ratio = USD/SOL.
        base_per_quote = Decimal(cand.market.pool_reserve_base) / Decimal(cand.market.pool_reserve_quote)
        return cand.market.price_usd * base_per_quote
    return Decimal(0)


# --------------------------------------------------------------------------- #
# Entry point — `python -m mca.scoring.worker`, per docker-compose.yml.
# Missing through the first Docker run; see risk/gate_service.py's entry
# point for the full story.
# --------------------------------------------------------------------------- #

async def _main() -> None:
    import os
    import logging

    from mca.core.config import load_config
    from mca.execution.base import strategy_fingerprint

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config()
    bus = Bus(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    mode = TradeMode(cfg.system.mode)
    strategy_hash = strategy_fingerprint(cfg.model_dump())

    # No trained model yet on a fresh deployment — see ops/tasks.py's
    # retrain_model_task, which produces one once enough paper trades exist.
    # win_probability legitimately stays None until then; should_trade()
    # already refuses to trade on a None probability rather than guessing.
    worker = ScoringWorker(bus, scorer_cfg=cfg.to_scorer_config(), model=None,
                           mode=mode, strategy_hash=strategy_hash)
    await worker.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
