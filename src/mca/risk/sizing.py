"""Position sizing.

Design decision — size is the minimum of several independent caps
------------------------------------------------------------------
Most bots compute one number (e.g. "2% of equity") and trade it. That silently
breaks on illiquid tokens: 2% of a $50k account is $1,000, which against a $18k
pool is a 5.5% one-way price impact — an 11%+ round-trip cost before the trade
has any chance to work.

Here, four caps are computed independently and the *binding* one wins:

  1. risk-per-trade cap     — equity * max_risk / stop_distance
  2. liquidity impact cap   — largest size whose one-way impact <= max_impact_bps
  3. fractional Kelly cap   — from the calibrated edge, quartered and hard-capped
  4. absolute notional cap  — a flat ceiling, because models break

Reporting which cap binds is a first-class output. If the liquidity cap binds on
90% of trades, the strategy's real capacity is far below the account size and
scaling up will destroy the edge — that is something you want to learn from a
dashboard, not from a drawdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class BindingCap(str, Enum):
    RISK_PER_TRADE = "risk_per_trade"
    LIQUIDITY_IMPACT = "liquidity_impact"
    KELLY = "kelly"
    ABSOLUTE_NOTIONAL = "absolute_notional"
    FREE_CAPITAL = "free_capital"


@dataclass(slots=True)
class SizingConfig:
    max_risk_per_trade: float = 0.0075     # 0.75% of equity at risk at the stop
    max_notional_per_trade_usd: Decimal = Decimal(750)
    max_impact_bps: int = 120              # 1.2% one-way price impact ceiling
    kelly_fraction: float = 0.25           # quarter-Kelly
    kelly_hard_cap: float = 0.02           # never more than 2% of equity
    min_notional_usd: Decimal = Decimal(25)
    atr_stop_multiple: float = 2.2
    default_stop_pct: float = 0.25


@dataclass(slots=True)
class SizingResult:
    notional_usd: Decimal
    binding_cap: BindingCap
    stop_distance_pct: float
    expected_impact_bps: float
    caps: dict[str, float]

    @property
    def is_tradeable(self) -> bool:
        return self.notional_usd > 0


# --------------------------------------------------------------------------- #
# AMM math
# --------------------------------------------------------------------------- #


def constant_product_impact_bps(amount_in_quote: float, reserve_quote: float,
                                fee_bps: int = 30) -> float:
    """Exact one-way price impact for a constant-product (x*y=k) pool, in bps.

    For x*y=k the execution price versus the spot price satisfies

        impact = amount_in_effective / (reserve_in + amount_in_effective)

    where ``amount_in_effective = amount_in * (1 - fee)``. This is exact for
    Uniswap V2 / PancakeSwap V2 / Raydium AMM v4 style pools, and is a
    *conservative* approximation for concentrated liquidity (V3, Orca Whirlpool,
    Raydium CLMM) as long as the trade stays inside the active tick range.
    Conservative is the correct direction to be wrong in.
    """
    if reserve_quote <= 0 or amount_in_quote <= 0:
        return 10_000.0
    eff = amount_in_quote * (1.0 - fee_bps / 10_000.0)
    return (eff / (reserve_quote + eff)) * 10_000.0


def max_size_for_impact(reserve_quote: float, max_impact_bps: int,
                        fee_bps: int = 30) -> float:
    """Invert the impact formula: the largest quote-side input whose one-way
    impact stays at or below ``max_impact_bps``."""
    if reserve_quote <= 0:
        return 0.0
    k = max_impact_bps / 10_000.0
    if k >= 1.0:
        return float("inf")
    eff = (k * reserve_quote) / (1.0 - k)
    return eff / (1.0 - fee_bps / 10_000.0)


def round_trip_cost_bps(notional_usd: float, reserve_quote_usd: float,
                        buy_tax_bps: int = 0, sell_tax_bps: int = 0,
                        pool_fee_bps: int = 30, priority_fee_usd: float = 0.0,
                        extra_slippage_bps: int = 0) -> float:
    """Total expected round-trip friction in bps of notional.

    Includes: buy impact + sell impact + both pool fees + token taxes +
    priority/gas fees + a configurable allowance for adverse selection (you are
    rarely the only buyer reacting to the same signal).

    This is the number that the EV gate subtracts. Underestimating it here is
    the most expensive mistake in the codebase.
    """
    buy_impact = constant_product_impact_bps(notional_usd, reserve_quote_usd, pool_fee_bps)
    # Sell happens against a pool that our own buy has already moved.
    reserve_after = reserve_quote_usd + notional_usd
    sell_impact = constant_product_impact_bps(notional_usd, reserve_after, pool_fee_bps)
    fee_bps = (priority_fee_usd / notional_usd * 10_000.0) if notional_usd > 0 else 0.0
    return (buy_impact + sell_impact + buy_tax_bps + sell_tax_bps
            + fee_bps + extra_slippage_bps)


# --------------------------------------------------------------------------- #
# Kelly
# --------------------------------------------------------------------------- #


def kelly_fraction(win_prob: float, avg_win_pct: float, avg_loss_pct: float) -> float:
    """Kelly for an asymmetric binary payoff: f* = (p*b - q) / b, b = win/loss.

    Returns 0 for a non-positive edge. The caller must scale this down — full
    Kelly assumes the probability estimate is *correct*, and ours comes from a
    model fitted on a few thousand noisy, regime-dependent samples. Quarter
    Kelly costs ~25% of theoretical growth and roughly quarters the variance;
    on this asset class that trade is unambiguously worth taking.
    """
    if avg_loss_pct <= 0 or avg_win_pct <= 0:
        return 0.0
    b = avg_win_pct / abs(avg_loss_pct)
    q = 1.0 - win_prob
    f = (win_prob * b - q) / b
    return max(0.0, f)


# --------------------------------------------------------------------------- #
# Main entry point
# --------------------------------------------------------------------------- #


def compute_position_size(
    *,
    equity_usd: Decimal,
    free_capital_usd: Decimal,
    reserve_quote_usd: float,
    cfg: SizingConfig,
    win_prob: float | None = None,
    avg_win_pct: float = 0.85,
    avg_loss_pct: float = 0.25,
    atr_pct: float | None = None,
    pool_fee_bps: int = 30,
) -> SizingResult:
    """Return the notional to trade and which constraint produced it.

    ``atr_pct`` is ATR expressed as a fraction of price. When available the stop
    is placed at ``atr_stop_multiple * atr_pct`` rather than at a fixed percent,
    so a 40%-daily-vol token and a 400%-daily-vol token get stops that mean the
    same thing statistically — and therefore position sizes that risk the same
    dollar amount.
    """
    eq = float(equity_usd)
    caps: dict[str, float] = {}

    stop_pct = (cfg.atr_stop_multiple * atr_pct) if atr_pct else cfg.default_stop_pct
    stop_pct = min(max(stop_pct, 0.08), 0.60)   # sane bounds regardless of ATR

    # 1. risk-per-trade
    caps[BindingCap.RISK_PER_TRADE.value] = (eq * cfg.max_risk_per_trade) / stop_pct

    # 2. liquidity impact
    caps[BindingCap.LIQUIDITY_IMPACT.value] = max_size_for_impact(
        reserve_quote_usd, cfg.max_impact_bps, pool_fee_bps)

    # 3. Kelly
    if win_prob is not None:
        f = kelly_fraction(win_prob, avg_win_pct, avg_loss_pct) * cfg.kelly_fraction
        f = min(f, cfg.kelly_hard_cap)
        caps[BindingCap.KELLY.value] = eq * f
    else:
        caps[BindingCap.KELLY.value] = float("inf")

    # 4. hard ceilings
    caps[BindingCap.ABSOLUTE_NOTIONAL.value] = float(cfg.max_notional_per_trade_usd)
    caps[BindingCap.FREE_CAPITAL.value] = float(free_capital_usd)

    binding_name = min(caps, key=lambda k: caps[k])
    notional = max(0.0, caps[binding_name])

    if notional < float(cfg.min_notional_usd):
        return SizingResult(
            notional_usd=Decimal(0), binding_cap=BindingCap(binding_name),
            stop_distance_pct=stop_pct, expected_impact_bps=0.0, caps=caps,
        )

    impact = constant_product_impact_bps(notional, reserve_quote_usd, pool_fee_bps)
    return SizingResult(
        notional_usd=Decimal(str(round(notional, 2))),
        binding_cap=BindingCap(binding_name),
        stop_distance_pct=stop_pct,
        expected_impact_bps=impact,
        caps=caps,
    )
