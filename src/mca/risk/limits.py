"""Portfolio-level limits and the circuit breaker.

Design decision — the risk gate is the only writer
--------------------------------------------------
Every trade authorisation flows through a single ``RiskGate`` process consuming
from a single stream partition. This is deliberately *not* horizontally scaled.

The alternative — several scorer workers each checking "are we under 5 open
positions?" — has a textbook race: three workers read ``open=4`` concurrently and
all three open a position. You end up at 7. Under-scaling one component to make
a global invariant enforceable is the correct trade; the gate handles thousands
of decisions per second, which is orders of magnitude more than we need.

Design decision — circuit breaker state is persisted
-----------------------------------------------------
A daily-loss breaker that resets when the container restarts is not a breaker.
State lives in Postgres, is read on boot, and ``LOCKED`` requires an explicit
human unlock through the API. A bot that has lost its daily budget should not be
able to argue its way back in.

Design decision — correlation, not count, is the real exposure limit
---------------------------------------------------------------------
Holding eight different meme coins is not diversification. In any risk-off move
the whole complex correlates to ~0.9 against SOL/ETH beta, so eight positions
behave like one position of eight times the size. ``max_gross_exposure_pct``
therefore binds long before ``max_open_positions`` does, and that is intended.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from mca.domain.models import Chain, RiskState, utcnow


@dataclass(slots=True)
class RiskLimits:
    max_open_positions: int = 5
    max_open_per_chain: int = 3
    max_gross_exposure_pct: float = 0.15      # of equity, across all positions
    max_daily_loss_pct: float = 0.06
    max_weekly_loss_pct: float = 0.12
    max_drawdown_pct: float = 0.20            # from all-time equity high -> LOCKED
    max_consecutive_losses: int = 6
    max_new_positions_per_hour: int = 12
    min_seconds_between_entries: int = 20
    max_exposure_single_token_pct: float = 0.04
    cooldown_after_loss_s: int = 300


@dataclass(slots=True)
class PortfolioState:
    """Snapshot the gate reasons over. Rebuilt from Postgres on boot."""

    equity_usd: Decimal
    free_capital_usd: Decimal
    peak_equity_usd: Decimal
    open_positions: int = 0
    open_by_chain: dict[Chain, int] = field(default_factory=dict)
    gross_exposure_usd: Decimal = Decimal(0)
    realized_pnl_today_usd: Decimal = Decimal(0)
    realized_pnl_week_usd: Decimal = Decimal(0)
    consecutive_losses: int = 0
    entries_last_hour: int = 0
    last_entry_at: datetime | None = None
    last_loss_at: datetime | None = None
    risk_state: RiskState = RiskState.NORMAL
    day: date = field(default_factory=lambda: utcnow().date())


@dataclass(slots=True)
class GateDecision:
    allowed: bool
    reason: str
    new_state: RiskState | None = None


class CircuitBreaker:
    """Evaluates the *global* halt conditions. Separated from per-trade checks
    because these change the whole system's state, not one decision."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def evaluate(self, st: PortfolioState) -> RiskState:
        eq = float(st.equity_usd)
        peak = float(st.peak_equity_usd) or eq
        lim = self.limits

        drawdown = (peak - eq) / peak if peak > 0 else 0.0
        if drawdown >= lim.max_drawdown_pct:
            return RiskState.LOCKED

        daily_loss = -float(st.realized_pnl_today_usd) / eq if eq > 0 else 0.0
        if daily_loss >= lim.max_daily_loss_pct:
            return RiskState.LOCKED

        weekly_loss = -float(st.realized_pnl_week_usd) / eq if eq > 0 else 0.0
        if weekly_loss >= lim.max_weekly_loss_pct:
            return RiskState.LOCKED

        if st.consecutive_losses >= lim.max_consecutive_losses:
            return RiskState.DERISK

        # Soft warning band: stop opening new risk at 70% of the daily budget.
        if daily_loss >= 0.70 * lim.max_daily_loss_pct:
            return RiskState.DERISK

        return RiskState.NORMAL


class RiskGate:
    """Single authority for "may this trade happen".

    Returns a *reason string* on every rejection. Rejection reasons are counted
    and charted; a gate that never rejects is either mis-configured or the
    upstream scorer is not producing enough candidates.
    """

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.breaker = CircuitBreaker(self.limits)

    def check_entry(self, st: PortfolioState, chain: Chain,
                    notional_usd: Decimal, now: datetime | None = None) -> GateDecision:
        now = now or utcnow()
        lim = self.limits
        eq = float(st.equity_usd)

        state = self.breaker.evaluate(st)
        if state is not RiskState.NORMAL:
            return GateDecision(False, f"risk_state={state.value}", state)

        if st.open_positions >= lim.max_open_positions:
            return GateDecision(False, f"max_open_positions ({st.open_positions})")

        if st.open_by_chain.get(chain, 0) >= lim.max_open_per_chain:
            return GateDecision(False, f"max_open_per_chain {chain.value}")

        new_gross = float(st.gross_exposure_usd + notional_usd)
        if eq > 0 and new_gross / eq > lim.max_gross_exposure_pct:
            return GateDecision(
                False, f"gross_exposure {new_gross/eq:.1%} > {lim.max_gross_exposure_pct:.1%}")

        if eq > 0 and float(notional_usd) / eq > lim.max_exposure_single_token_pct:
            return GateDecision(False, "single_token_exposure")

        if notional_usd > st.free_capital_usd:
            return GateDecision(False, "insufficient_free_capital")

        if st.entries_last_hour >= lim.max_new_positions_per_hour:
            return GateDecision(False, "entry_rate_limit")

        if st.last_entry_at and (now - st.last_entry_at).total_seconds() < lim.min_seconds_between_entries:
            return GateDecision(False, "min_seconds_between_entries")

        # Cooldown after a loss: stops the revenge-trading pattern where the bot
        # immediately re-enters a correlated setup right after being stopped out
        # of the same regime.
        if st.last_loss_at and (now - st.last_loss_at).total_seconds() < lim.cooldown_after_loss_s:
            return GateDecision(False, "loss_cooldown")

        return GateDecision(True, "ok")

    @staticmethod
    def roll_day(st: PortfolioState, now: datetime | None = None) -> PortfolioState:
        """Reset daily counters at UTC midnight. Called by the scheduler, never
        implicitly, so that a clock jump cannot silently clear a loss budget."""
        now = now or utcnow()
        if now.date() != st.day:
            st.realized_pnl_today_usd = Decimal(0)
            st.day = now.date()
            if now.weekday() == 0 and (now.date() - st.day) >= timedelta(days=0):
                st.realized_pnl_week_usd = Decimal(0)
        return st
