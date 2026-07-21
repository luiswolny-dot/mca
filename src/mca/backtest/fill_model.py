"""Execution simulation for the backtester.

Design decision — this module exists because candle-close backtests lie
-----------------------------------------------------------------------
A backtest that fills at the close of the bar where the signal fired will
produce a beautiful equity curve for almost any meme-coin momentum rule, and
that curve is fiction. Four effects destroy it in reality, and each is modelled
here explicitly:

1. **Latency.** Signal -> feature computation -> scoring -> risk gate -> build tx
   -> sign -> submit -> land is 400ms-3s for a Python stack on a rented node.
   On a token doing +40% per minute, 2s is a ~1.3% worse entry, every time.

2. **Price impact.** Your own order moves an $18k pool. Fills are computed
   against the constant-product curve using the pool reserves at fill time, not
   at the mid.

3. **Transaction failure.** On Solana, congestion + slippage limits mean a real
   share of swaps simply do not land. A backtest with a 100% fill rate assumes
   you always get the good fills and never eat the cost of the failed ones.
   Failure is asymmetric: you fail most often exactly when everyone is buying,
   i.e. on your best signals.

4. **Survivorship.** APIs delete dead tokens. If your historical dataset was
   assembled by querying "tokens that exist today", every rug is missing and the
   backtest is meaningless. ``SurvivorshipAudit`` below refuses to run on a
   dataset whose rug rate is implausibly low.

None of this makes a backtest *truthful*. It makes it less dishonest. The only
real validation is paper trading against live data, which is why ``TradeMode``
gates the executor.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from decimal import Decimal

from mca.domain.models import Chain, Side


@dataclass(slots=True)
class LatencyModel:
    """Log-normal latency in milliseconds, per chain.

    Defaults are conservative measurements for an async Python service on a
    dedicated (not co-located) node with a paid RPC endpoint. Measure your own
    with ``scripts/measure_latency.py`` and replace them; latency is the single
    parameter that most changes backtest results in this asset class.
    """

    median_ms: dict[Chain, float] = None  # type: ignore[assignment]
    sigma: float = 0.55
    congestion_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if self.median_ms is None:
            self.median_ms = {
                Chain.SOLANA: 900.0,      # incl. block landing + confirmation
                Chain.BASE: 1400.0,
                Chain.BSC: 1800.0,
                Chain.ETHEREUM: 3500.0,
            }

    def sample(self, chain: Chain, rng: random.Random) -> float:
        med = self.median_ms.get(chain, 1500.0) * self.congestion_multiplier
        return med * math_exp(rng.gauss(0.0, self.sigma))


def math_exp(x: float) -> float:
    import math
    return math.exp(x)


@dataclass(slots=True)
class FillOutcome:
    filled: bool
    price_usd: Decimal
    base_amount: int
    quote_amount: Decimal
    slippage_bps: float
    fee_usd: Decimal
    latency_ms: float
    reason: str = ""


@dataclass(slots=True)
class FillModel:
    """Simulates a single swap against a constant-product pool."""

    latency: LatencyModel = None  # type: ignore[assignment]
    pool_fee_bps: int = 30
    priority_fee_usd: dict[Chain, float] = None  # type: ignore[assignment]
    base_failure_rate: float = 0.06
    congestion_failure_rate: float = 0.22   # applied when price is moving fast
    fast_move_threshold_pct: float = 0.15   # per-minute move that counts as fast
    seed: int | None = 42

    def __post_init__(self) -> None:
        if self.latency is None:
            self.latency = LatencyModel()
        if self.priority_fee_usd is None:
            self.priority_fee_usd = {
                Chain.SOLANA: 0.35, Chain.BASE: 0.12,
                Chain.BSC: 0.28, Chain.ETHEREUM: 6.50,
            }
        self._rng = random.Random(self.seed)

    def _failure_probability(self, recent_move_pct: float) -> float:
        if abs(recent_move_pct) >= self.fast_move_threshold_pct:
            return self.congestion_failure_rate
        return self.base_failure_rate

    def simulate(
        self, *, chain: Chain, side: Side, quote_amount_usd: Decimal,
        price_at_signal: Decimal, price_path: list[tuple[float, Decimal]],
        reserve_quote_usd: float, recent_move_pct: float = 0.0,
        tax_bps: int = 0, max_slippage_bps: int = 800,
    ) -> FillOutcome:
        """Simulate one swap.

        ``price_path`` is [(ms_since_signal, price)] at sub-bar resolution. If
        you only have 1-minute candles, interpolating within the bar is already
        an assumption — prefer a dataset of raw swap events.
        """
        lat = self.latency.sample(chain, self._rng)

        # 1. price at the moment our transaction actually lands
        price_at_fill = price_at_signal
        for offset_ms, px in price_path:
            if offset_ms <= lat:
                price_at_fill = px
            else:
                break

        # 2. did the transaction land at all?
        if self._rng.random() < self._failure_probability(recent_move_pct):
            fee = Decimal(str(self.priority_fee_usd.get(chain, 1.0)))
            return FillOutcome(False, Decimal(0), 0, Decimal(0), 0.0, fee, lat,
                               "tx_failed_or_dropped")

        # 3. price impact of our own order
        notional = float(quote_amount_usd)
        eff = notional * (1.0 - self.pool_fee_bps / 10_000.0)
        impact_frac = eff / (reserve_quote_usd + eff) if reserve_quote_usd > 0 else 1.0

        drift_bps = float((price_at_fill / price_at_signal - 1) * 10_000) \
            if price_at_signal > 0 else 0.0
        if side is Side.SELL:
            drift_bps = -drift_bps

        total_slip_bps = drift_bps + impact_frac * 10_000.0 + tax_bps

        # 4. slippage guard — a real swap reverts rather than filling at any price
        if total_slip_bps > max_slippage_bps:
            fee = Decimal(str(self.priority_fee_usd.get(chain, 1.0)))
            return FillOutcome(False, Decimal(0), 0, Decimal(0), total_slip_bps,
                               fee, lat, "slippage_exceeded")

        exec_price = price_at_fill * Decimal(str(1.0 + impact_frac + tax_bps / 10_000.0)) \
            if side is Side.BUY else \
            price_at_fill * Decimal(str(1.0 - impact_frac - tax_bps / 10_000.0))

        decimals = 9 if chain is Chain.SOLANA else 18
        base_amount = int(float(quote_amount_usd) / float(exec_price) * (10 ** decimals)) \
            if exec_price > 0 else 0
        fee = Decimal(str(self.priority_fee_usd.get(chain, 1.0)))

        return FillOutcome(True, exec_price, base_amount, quote_amount_usd,
                           total_slip_bps, fee, lat, "filled")


class SurvivorshipAudit:
    """Refuses to run a backtest on a dataset that has been silently cleaned.

    Empirically, the share of newly launched meme coins that lose >90% of peak
    liquidity within 24h sits far above 50% across all four supported chains. A
    dataset showing 15% is not a good market — it is a dataset assembled from
    tokens that still exist, and every backtest on it is invalid.
    """

    MIN_PLAUSIBLE_FAILURE_RATE = 0.45

    @classmethod
    def check(cls, n_tokens: int, n_failed: int) -> tuple[bool, str]:
        if n_tokens == 0:
            return False, "empty dataset"
        rate = n_failed / n_tokens
        if rate < cls.MIN_PLAUSIBLE_FAILURE_RATE:
            return False, (
                f"observed failure rate {rate:.1%} is below the plausible floor "
                f"{cls.MIN_PLAUSIBLE_FAILURE_RATE:.0%} — the dataset is very likely "
                f"survivorship-biased. Re-collect from an archival source that "
                f"retains delisted tokens."
            )
        return True, f"failure rate {rate:.1%} — plausible"
