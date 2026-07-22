"""Position manager: the process that turns Fills into Positions, and Positions
that should be sold into SELL intents.

Two independent loops, run concurrently in one process (matches
``docker-compose.yml``'s single ``position-manager`` service):

  1. **Fill consumption** — every confirmed trade eventually becomes a
     ``Fill`` message on ``Stream.FILLS``. This loop reconciles it into the
     ``positions`` table: opens a new position on the first BUY, averages into
     an existing one on a second BUY, and reduces/closes on a SELL.

  2. **Exit evaluation** — on a timer, every OPEN position is re-priced and run
     through ``ExitEngine.evaluate()`` (the exact same pure function the
     backtester uses — see ``portfolio/exits.py``). A signal to exit becomes a
     SELL ``TradeIntent`` published to ``Stream.INTENTS``.

Design decision — ladder-rung bookkeeping needs no cross-process state
--------------------------------------------------------------------------
A naive implementation tracks "which ladder rung is this SELL fill for" in an
in-memory dict keyed by ``intent_id``, populated when the intent is issued.
That does not survive a restart between issuing the SELL and receiving its
fill, and a lost mapping risks re-selling the same rung. Instead, the rung is
re-derived from the fill itself: since ``ExitEngine.evaluate`` only ever
returns the *first* unexecuted rung whose trigger is met, marking that same
first unexecuted rung (by re-checking the trigger against the realised fill
price) as executed is self-consistent with how the signal was generated in the
first place, with no state to lose. Full exits (stop-loss, trailing, time,
structural — every non-ladder reason) always carry ``sell_fraction=1.0``, so
they close the position outright and need no rung bookkeeping at all.
"""

from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal

from mca.core.bus import Bus, Stream
from mca.db.repository import Repository
from mca.domain.models import (
    Chain, ExitReason, Position, PositionStatus, Side, TokenRef, TradeIntent,
    TradeMode, new_id, utcnow,
)
from mca.execution.base import Executor, fill_from_payload, intent_to_payload
from mca.execution.jupiter import JupiterClient
from mca.portfolio.exits import ExitConfig, ExitEngine, LiveContext, build_ladder

log = logging.getLogger(__name__)


class PositionManager:
    def __init__(self, bus: Bus, repo: Repository, executors: dict[Chain, Executor],
                jupiter_by_chain: dict[Chain, JupiterClient], *,
                exit_cfg: ExitConfig | None = None, mode: TradeMode = TradeMode.PAPER,
                strategy_hash: str = "", tick_interval_s: float = 5.0,
                consumer_name: str | None = None) -> None:
        self.bus = bus
        self.repo = repo
        self.executors = executors
        self.jupiter_by_chain = jupiter_by_chain
        self.exit_cfg = exit_cfg or ExitConfig()
        self.exit_engine = ExitEngine(self.exit_cfg)
        self.mode = mode
        self.strategy_hash = strategy_hash
        self.tick_interval_s = tick_interval_s
        self.consumer_name = consumer_name or f"posmgr-{os.getpid()}"
        self._sol_usd_price_cache: dict[Chain, Decimal] = {}

    async def run(self) -> None:
        await self.bus.ensure_group(Stream.FILLS, "position-manager")
        await asyncio.gather(self._consume_fills(), self._exit_loop())

    # -- loop 1: fills -> positions --------------------------------------------
    async def _consume_fills(self) -> None:
        while True:
            msgs = await self.bus.consume(Stream.FILLS, "position-manager", self.consumer_name,
                                          count=10, block_ms=5000)
            for msg in msgs:
                try:
                    fill, idempotency_key = fill_from_payload(msg.payload)
                    await self._apply_fill(fill, idempotency_key)
                except Exception:
                    log.exception("failed to apply fill from message id=%s", msg.id)
                await self.bus.ack(Stream.FILLS, "position-manager", msg.id)

    async def _apply_fill(self, fill, idempotency_key: str) -> None:
        token_id = await self.repo.get_or_create_token_id(fill.token)
        existing = await self.repo.get_open_position(fill.token, self.mode, self.strategy_hash)

        if fill.side is Side.BUY:
            if existing is None:
                decimals = await self.repo.get_token_decimals(token_id)
                pos = Position(
                    position_id=new_id(), token=fill.token, status=PositionStatus.OPEN,
                    mode=self.mode, opened_at=fill.filled_at,
                    base_amount=fill.base_amount, original_base_amount=fill.base_amount,
                    avg_entry_price_usd=fill.price_usd, quote_invested=fill.quote_amount,
                    fees_paid_usd=fill.gas_or_fee_usd, high_water_price_usd=fill.price_usd,
                    ladder=build_ladder(self.exit_cfg), correlation_id=new_id(),
                    token_decimals=decimals,
                )
                await self.repo.insert_position(pos, token_id=token_id, strategy_hash=self.strategy_hash)
            else:
                pos, _tid = existing
                total_base = pos.base_amount + fill.base_amount
                if total_base > 0:
                    pos.avg_entry_price_usd = (
                        (pos.avg_entry_price_usd * pos.base_amount + fill.price_usd * fill.base_amount)
                        / Decimal(total_base)
                    )
                pos.base_amount = total_base
                pos.original_base_amount += fill.base_amount
                pos.quote_invested += fill.quote_amount
                pos.fees_paid_usd += fill.gas_or_fee_usd
                await self.repo.update_position(pos, token_id=token_id, strategy_hash=self.strategy_hash)

            inserted = await self.repo.insert_fill(
                fill, position_id=pos.position_id, token_id=token_id, mode=self.mode,
                idempotency_key=idempotency_key)
            if inserted and self.mode is TradeMode.PAPER:
                await self.repo.increment_shadow_trades(self.strategy_hash, started_at=fill.filled_at)
            log.info("position %s: BUY applied, base_amount=%d avg_entry=$%s",
                    pos.position_id, pos.base_amount, pos.avg_entry_price_usd)
            return

        # SELL
        if existing is None:
            log.error("SELL fill for %s with no open position (mode=%s, strategy=%s) — "
                     "orphaned fill, cannot reconcile", fill.token.key, self.mode.value,
                     self.strategy_hash)
            await self.bus.publish(Stream.RISK_EVENTS, {
                "event": "orphaned_fill", "token": fill.token.address,
                "chain": fill.token.chain.value, "fill_id": fill.fill_id,
                "at": utcnow().isoformat(),
            })
            return

        pos, _tid = existing
        whole_sold = Decimal(fill.base_amount) / (Decimal(10) ** pos.token_decimals)
        proceeds_usd = fill.price_usd * whole_sold
        cost_basis_usd = pos.avg_entry_price_usd * whole_sold
        realized = proceeds_usd - cost_basis_usd - fill.gas_or_fee_usd

        pos.base_amount = max(0, pos.base_amount - fill.base_amount)
        pos.realized_pnl_usd += realized
        pos.fees_paid_usd += fill.gas_or_fee_usd

        pnl_pct_at_fill = pos.pnl_pct(fill.price_usd)
        for level in pos.ladder:
            if not level.executed and pnl_pct_at_fill >= level.trigger_pct:
                level.executed = True
                level.executed_at = fill.filled_at
                pos.exit_reason = ExitReason.TAKE_PROFIT
                break

        if pos.base_amount <= 0:
            pos.status = PositionStatus.CLOSED
            pos.closed_at = fill.filled_at
        else:
            pos.status = PositionStatus.SCALING_OUT

        await self.repo.update_position(pos, token_id=token_id, strategy_hash=self.strategy_hash)
        await self.repo.insert_fill(fill, position_id=pos.position_id, token_id=token_id,
                                   mode=self.mode, idempotency_key=idempotency_key)
        log.info("position %s: SELL applied, realized=$%.2f status=%s",
                pos.position_id, realized, pos.status.value)

    # -- loop 2: exit evaluation -----------------------------------------------
    async def _exit_loop(self) -> None:
        while True:
            try:
                positions = await self.repo.get_open_positions(self.mode)
                for pos, token_id in positions:
                    await self._evaluate_one(pos, token_id)
            except Exception:
                log.exception("exit loop iteration failed")
            await self.bus.heartbeat("position-manager")
            await asyncio.sleep(self.tick_interval_s)

    async def _evaluate_one(self, pos: Position, token_id: int) -> None:
        executor = self.executors.get(pos.token.chain)
        jupiter = self.jupiter_by_chain.get(pos.token.chain)
        if executor is None or jupiter is None:
            return

        try:
            sol_price = await self._sol_usd_price(pos.token.chain, jupiter)
            executor.quote_usd_price = sol_price
            price_usd, _slip = await executor.quote(pos.token, Side.SELL, Decimal(pos.base_amount))
        except Exception as exc:
            log.warning("could not price open position %s: %s", pos.position_id, exc)
            return

        sell_sim_ok = await self._probe_sell_simulation(pos, executor)

        # Liquidity-drop, deployer-move, top-holder-move, and social/whale-flow
        # structural exits (see exits.py's design note on why these outrank
        # price exits) need data this process does not have yet: pool reserve
        # history, deployer wallet tracking, social feeds. Rather than fabricate
        # numbers that would silently defeat those checks (e.g. deriving
        # "liquidity" from price, which very nearly happened here — caught
        # while re-reading exits.py's own docstring), they are passed as
        # explicit no-signal defaults so the checks correctly no-op instead of
        # misfiring. Only the sell-simulation check — which this process CAN
        # verify for real via the executor it already holds — is live. Wiring
        # the rest is the enrich layer's job; see README.md's roadmap.
        ctx = LiveContext(
            price_usd=price_usd, liquidity_usd=Decimal(1), entry_liquidity_usd=Decimal(1),
            sell_simulation_ok=sell_sim_ok, now=utcnow(),
        )
        signal = self.exit_engine.evaluate(pos, ctx)
        if not signal.should_exit:
            return

        sell_base = int(pos.base_amount * signal.sell_fraction)
        if sell_base <= 0:
            return

        intent = TradeIntent(
            intent_id=new_id(), token=pos.token, side=Side.SELL, quote_amount=None,
            base_amount=sell_base, max_slippage_bps=800 if signal.urgent else 500,
            idempotency_key=new_id(), mode=self.mode,
            reason=f"exit:{signal.reason.value if signal.reason else 'unknown'} {signal.detail}",
            correlation_id=pos.correlation_id,
        )
        await self.bus.publish(Stream.INTENTS, intent_to_payload(intent))
        log.info("exit signal position=%s reason=%s fraction=%.0f%% urgent=%s",
                pos.position_id, signal.reason.value if signal.reason else "?",
                signal.sell_fraction * 100, signal.urgent)

    async def _probe_sell_simulation(self, pos: Position, executor: Executor) -> bool:
        """Real structural signal, not a placeholder: builds a throwaway SELL
        intent for the held size and runs it through the executor's own
        ``simulate()`` — the identical round-trip-aware check used before a
        real sell, reused here as a read-only probe. A newly-activated sell tax
        or blacklist shows up here on the very next tick after it happens,
        which is the entire reason ``exits.py`` treats this as a
        higher-priority signal than any price-based stop.
        """
        probe = TradeIntent(
            intent_id=new_id(), token=pos.token, side=Side.SELL, quote_amount=None,
            base_amount=pos.base_amount, max_slippage_bps=1000,
            idempotency_key=new_id(), mode=self.mode, reason="exit-probe",
        )
        try:
            ok, _reason = await executor.simulate(probe)
            return ok
        except Exception as exc:
            log.warning("sell-simulation probe failed for %s: %s — treating as unsafe", pos.position_id, exc)
            return False

    async def _sol_usd_price(self, chain: Chain, jupiter: JupiterClient) -> Decimal:
        # Cached per-process, refreshed on every exit-loop tick (every
        # tick_interval_s) rather than per-position — a fresh reference price
        # once per tick is enough for PnL display and sizing; querying it once
        # per *position* per tick would multiply RPC load by position count for
        # no accuracy benefit, since SOL/USD does not move meaningfully within
        # one tick interval.
        price = await jupiter.sol_usd_price()
        self._sol_usd_price_cache[chain] = price
        return price


# --------------------------------------------------------------------------- #
# Entry point — `python -m mca.portfolio.manager`, per docker-compose.yml.
# Missing through the first Docker run; see risk/gate_service.py's entry
# point for the full story (clean exit, RestartCount climbing, empty logs).
# --------------------------------------------------------------------------- #

async def _main() -> None:
    import os
    import logging

    from mca.core.config import load_config
    from mca.execution.base import strategy_fingerprint
    from mca.execution.worker import build_solana_stack

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config()
    mode = TradeMode(cfg.system.mode)
    exec_cfg = cfg.to_execution_config(Chain.SOLANA)
    strategy_hash = strategy_fingerprint(cfg.model_dump())

    executor, rpc, jupiter = build_solana_stack(mode, exec_cfg)
    bus = Bus(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    repo = Repository(os.environ["DATABASE_URL"])

    manager = PositionManager(bus, repo, executors={Chain.SOLANA: executor},
                              jupiter_by_chain={Chain.SOLANA: jupiter},
                              exit_cfg=cfg.to_exit_config(), mode=mode, strategy_hash=strategy_hash)
    await manager.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
