"""Exit engine.

Design decision — structural exits outrank price exits
-------------------------------------------------------
On liquid assets a stop-loss is a reliable risk control. On a $40k meme-coin
pool it is a *hope*: when the LP is pulled, there is no bid at your stop price,
and the position is worth zero regardless of what the stop said. The exits that
actually protect capital in this asset class are on-chain and structural:

    liquidity drops >35%          -> exit now, do not wait for price
    deployer wallet moves tokens  -> exit now
    top holder sends to a DEX     -> exit now
    sell simulation starts failing-> exit now (tax was switched on)

Those are checked first, every tick, and they bypass the ladder entirely.

Design decision — sell into strength, always
---------------------------------------------
The realised return distribution here is extremely right-skewed with a huge mass
at -100%. The ladder is front-loaded: the first rung recovers the *entire* risk
budget at +45%, which converts the position into a free option. Waiting for the
"real" move with a full position is how a portfolio of 60% winners still loses
money.

Design decision — the ladder is defined on the ORIGINAL size
-------------------------------------------------------------
``sell_fraction`` is a fraction of ``original_base_amount``. Defining rungs on
the *remaining* balance makes the schedule depend on execution history and turns
backtest reconciliation into guesswork.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from mca.domain.models import (
    ExitLevel, ExitReason, Position, PositionStatus, utcnow,
)


@dataclass(slots=True)
class ExitConfig:
    # --- price stops ---
    hard_stop_pct: float = -0.25
    break_even_trigger_pct: float = 0.30      # once +30%, stop moves to entry+fees
    break_even_offset_pct: float = 0.02       # cover the round-trip cost
    trailing_activate_pct: float = 0.45
    trailing_atr_multiple: float = 2.5
    trailing_min_pct: float = 0.18            # floor, when ATR is unavailable
    trailing_max_pct: float = 0.45            # ceiling, so a vol spike cannot widen it forever

    # --- time ---
    time_stop_s: int = 2700                   # 45 min without hitting first rung
    time_stop_min_progress_pct: float = 0.20
    max_hold_s: int = 21_600                  # 6h absolute

    # --- structural ---
    liquidity_drop_exit_pct: float = 0.35
    momentum_fade_bars: int = 5
    social_fade_pct: float = 0.60
    whale_exit_net_usd: Decimal = Decimal(-15_000)

    ladder: list[tuple[float, float]] = field(default_factory=lambda: [
        (0.45, 0.30),    # +45%  -> sell 30% (recovers the risk budget)
        (1.00, 0.25),    # +100% -> sell 25%
        (2.00, 0.20),    # +200% -> sell 20%
        (4.00, 0.15),    # +400% -> sell 15%
    ])                    # remaining 10% rides the trailing stop indefinitely


@dataclass(slots=True)
class ExitSignal:
    should_exit: bool
    reason: ExitReason | None = None
    sell_fraction: float = 0.0        # of ORIGINAL position size
    detail: str = ""
    urgent: bool = False              # bypass batching, raise priority fee


@dataclass(slots=True)
class LiveContext:
    """Everything the exit engine may look at on this tick."""

    price_usd: Decimal
    liquidity_usd: Decimal
    entry_liquidity_usd: Decimal
    atr_pct: float | None = None
    consecutive_down_bars: int = 0
    social_mentions_now: int = 0
    social_mentions_peak: int = 0
    smart_net_usd_5m: Decimal = Decimal(0)
    deployer_moved: bool = False
    top_holder_moved_to_dex: bool = False
    sell_simulation_ok: bool = True
    now: datetime = field(default_factory=utcnow)


def build_ladder(cfg: ExitConfig) -> list[ExitLevel]:
    return [ExitLevel(trigger_pct=t, sell_fraction=f) for t, f in cfg.ladder]


class ExitEngine:
    """Pure decision function: (position, context, config) -> ExitSignal.

    No I/O, no clock reads beyond ``ctx.now``. That is what lets the backtester
    replay it bar-by-bar and the live position manager call it on every price
    update with identical semantics.
    """

    def __init__(self, cfg: ExitConfig | None = None) -> None:
        self.cfg = cfg or ExitConfig()

    # -- structural checks, evaluated first ---------------------------------
    def _structural(self, pos: Position, ctx: LiveContext) -> ExitSignal | None:
        cfg = self.cfg

        if not ctx.sell_simulation_ok:
            return ExitSignal(True, ExitReason.LIQUIDITY_DRAIN, 1.0,
                              "sell simulation now failing — tax/blacklist switched on",
                              urgent=True)

        if ctx.entry_liquidity_usd > 0:
            drop = 1.0 - float(ctx.liquidity_usd / ctx.entry_liquidity_usd)
            if drop >= cfg.liquidity_drop_exit_pct:
                return ExitSignal(True, ExitReason.LIQUIDITY_DRAIN, 1.0,
                                  f"liquidity -{drop:.0%} since entry", urgent=True)

        if ctx.deployer_moved:
            return ExitSignal(True, ExitReason.WHALE_EXIT, 1.0,
                              "deployer wallet moved tokens", urgent=True)

        if ctx.top_holder_moved_to_dex:
            return ExitSignal(True, ExitReason.WHALE_EXIT, 1.0,
                              "top holder transferred to a DEX", urgent=True)

        if ctx.smart_net_usd_5m <= cfg.whale_exit_net_usd:
            return ExitSignal(True, ExitReason.WHALE_EXIT, 1.0,
                              f"smart money net {ctx.smart_net_usd_5m} over 5m", urgent=True)

        return None

    # -- stop maintenance ---------------------------------------------------
    def update_stops(self, pos: Position, ctx: LiveContext) -> Position:
        """Ratchet the stop upward. It never moves down — a trailing stop that
        loosens on a volatility spike is just a wider stop with extra steps."""
        cfg = self.cfg
        price = ctx.price_usd
        pnl = pos.pnl_pct(price)

        if price > pos.high_water_price_usd:
            pos.high_water_price_usd = price

        candidate = pos.stop_price_usd

        # 1. initial hard stop
        if candidate is None:
            candidate = pos.avg_entry_price_usd * Decimal(str(1.0 + cfg.hard_stop_pct))

        # 2. break-even arming
        if not pos.break_even_armed and pnl >= cfg.break_even_trigger_pct:
            be = pos.avg_entry_price_usd * Decimal(str(1.0 + cfg.break_even_offset_pct))
            candidate = max(candidate, be)
            pos.break_even_armed = True

        # 3. trailing stop, ATR-scaled and bounded
        if pnl >= cfg.trailing_activate_pct:
            trail_pct = (cfg.trailing_atr_multiple * ctx.atr_pct) if ctx.atr_pct \
                else cfg.trailing_min_pct
            trail_pct = min(max(trail_pct, cfg.trailing_min_pct), cfg.trailing_max_pct)
            trail = pos.high_water_price_usd * Decimal(str(1.0 - trail_pct))
            candidate = max(candidate, trail)

        pos.stop_price_usd = candidate
        return pos

    # -- main entry point ---------------------------------------------------
    def evaluate(self, pos: Position, ctx: LiveContext) -> ExitSignal:
        if pos.status not in (PositionStatus.OPEN, PositionStatus.SCALING_OUT):
            return ExitSignal(False, detail=f"status={pos.status.value}")

        cfg = self.cfg
        structural = self._structural(pos, ctx)
        if structural:
            return structural

        self.update_stops(pos, ctx)
        price = ctx.price_usd
        pnl = pos.pnl_pct(price)

        # price stop
        if pos.stop_price_usd is not None and price <= pos.stop_price_usd:
            reason = (ExitReason.TRAILING_STOP if pnl > 0
                      else ExitReason.BREAK_EVEN if pos.break_even_armed
                      else ExitReason.STOP_LOSS)
            return ExitSignal(True, reason, 1.0,
                              f"price {price} <= stop {pos.stop_price_usd} (pnl {pnl:+.1%})")

        # take-profit ladder — first unexecuted rung whose trigger is met
        for level in pos.ladder:
            if not level.executed and pnl >= level.trigger_pct:
                return ExitSignal(True, ExitReason.TAKE_PROFIT, level.sell_fraction,
                                  f"ladder +{level.trigger_pct:.0%} -> sell {level.sell_fraction:.0%}")

        held_s = (ctx.now - pos.opened_at).total_seconds()

        if held_s >= cfg.max_hold_s:
            return ExitSignal(True, ExitReason.TIME_STOP, 1.0,
                              f"max hold {held_s/3600:.1f}h")

        if held_s >= cfg.time_stop_s and pnl < cfg.time_stop_min_progress_pct:
            return ExitSignal(True, ExitReason.TIME_STOP, 1.0,
                              f"no progress in {held_s/60:.0f}min (pnl {pnl:+.1%})")

        if ctx.consecutive_down_bars >= cfg.momentum_fade_bars and pnl > 0:
            return ExitSignal(True, ExitReason.MOMENTUM_FADE, 0.5,
                              f"{ctx.consecutive_down_bars} consecutive down bars")

        if ctx.social_mentions_peak > 20:
            fade = 1.0 - ctx.social_mentions_now / ctx.social_mentions_peak
            if fade >= cfg.social_fade_pct and pnl > 0:
                return ExitSignal(True, ExitReason.SOCIAL_FADE, 0.5,
                                  f"attention -{fade:.0%} from peak")

        return ExitSignal(False, detail=f"hold (pnl {pnl:+.1%})")
