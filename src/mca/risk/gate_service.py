"""Risk gate service: consumes ``Stream.SIGNALS``, produces ``Stream.INTENTS``.

This is the process ``docker-compose.yml`` runs with exactly one replica —
see ``risk/limits.py``'s module docstring for why that is a hard requirement,
not a scaling choice made under time pressure. Nothing here changes that
constraint; this module is the thing the constraint was written for.

Design decision — the SignalMessage contract is defined here, not upstream
------------------------------------------------------------------------------
The scorer worker that will publish to ``Stream.SIGNALS`` does not exist yet
(see ``README.md``'s roadmap). Rather than guess at its internals, this module
defines the exact message shape it needs as an explicit contract —
:class:`SignalMessage` plus its wire codec — the same way
``ingest/solana_pools.py`` defined ``PoolCreatedEvent`` as a contract before
anything downstream consumed it. Whoever builds the scorer worker next
publishes to this shape; nothing here needs to change when they do.

Design decision — two separate liquidity numbers, not one
--------------------------------------------------------------
``SignalMessage.liquidity_usd`` (total pool value, matches
``SecurityReport.liquidity_usd``'s convention used by every gate) and
``SignalMessage.reserve_quote_usd`` (one side only, what
``risk/sizing.py``'s constant-product math actually needs) are different
numbers — typically off by a factor of ~2 for a balanced pool. Collapsing them
into one field is exactly the kind of unit mismatch that stays invisible until
a position sizes at double or half of what was intended.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from mca.alerts.notifier import Alert, Severity, circuit_breaker_tripped
from mca.core.bus import Bus, Stream
from mca.core.retry import RetryConfig, retry_async
from mca.db.repository import Repository, signal_to_row
from mca.domain.models import (
    Chain, Side, TokenRef, TradeIntent, TradeMode, new_id, utcnow,
)
from mca.risk.limits import RiskGate, RiskLimits
from mca.risk.sizing import SizingConfig, compute_position_size

log = logging.getLogger(__name__)


@dataclass(slots=True)
class SignalMessage:
    """Everything the risk gate needs about one scored candidate. See the
    module docstring for why this is defined here rather than imported."""

    token: TokenRef
    mode: TradeMode
    strategy_hash: str
    as_of: datetime
    score_total: float
    components: dict[str, float]
    weights: dict[str, float]
    gates: list[dict]
    gates_passed: bool
    failed_gates: list[str]
    win_probability: float | None
    expected_value_bps: float | None
    model_version: str | None
    liquidity_usd: Decimal
    reserve_quote_usd: Decimal
    quote_usd_price: Decimal
    atr_pct: float | None
    correlation_id: str = field(default_factory=new_id)

    def to_payload(self) -> dict:
        return {
            "token": {"chain": self.token.chain.value, "address": self.token.address},
            "mode": self.mode.value, "strategy_hash": self.strategy_hash,
            "as_of": self.as_of.isoformat(), "score_total": self.score_total,
            "components": self.components, "weights": self.weights, "gates": self.gates,
            "gates_passed": self.gates_passed, "failed_gates": self.failed_gates,
            "win_probability": self.win_probability, "expected_value_bps": self.expected_value_bps,
            "model_version": self.model_version, "liquidity_usd": str(self.liquidity_usd),
            "reserve_quote_usd": str(self.reserve_quote_usd),
            "quote_usd_price": str(self.quote_usd_price), "atr_pct": self.atr_pct,
            "correlation_id": self.correlation_id,
        }

    @classmethod
    def from_payload(cls, p: dict) -> "SignalMessage":
        return cls(
            token=TokenRef(Chain(p["token"]["chain"]), p["token"]["address"]),
            mode=TradeMode(p["mode"]), strategy_hash=p["strategy_hash"],
            as_of=datetime.fromisoformat(p["as_of"]), score_total=p["score_total"],
            components=p["components"], weights=p["weights"], gates=p["gates"],
            gates_passed=p["gates_passed"], failed_gates=p["failed_gates"],
            win_probability=p["win_probability"], expected_value_bps=p["expected_value_bps"],
            model_version=p["model_version"], liquidity_usd=Decimal(p["liquidity_usd"]),
            reserve_quote_usd=Decimal(p["reserve_quote_usd"]),
            quote_usd_price=Decimal(p["quote_usd_price"]), atr_pct=p["atr_pct"],
            correlation_id=p["correlation_id"],
        )


class RiskGateService:
    def __init__(self, bus: Bus, repo: Repository, *, limits: RiskLimits | None = None,
                sizing_cfg: SizingConfig | None = None, mode: TradeMode = TradeMode.PAPER,
                strategy_hash: str = "", default_max_slippage_bps: int = 500,
                notifier=None, consumer_name: str | None = None) -> None:
        self.bus = bus
        self.repo = repo
        self.gate = RiskGate(limits)
        self.sizing_cfg = sizing_cfg or SizingConfig()
        self.mode = mode
        self.strategy_hash = strategy_hash
        self.default_max_slippage_bps = default_max_slippage_bps
        self.notifier = notifier
        self.consumer_name = consumer_name or f"gate-{os.getpid()}"
        self._retry = RetryConfig(retries=3, base_delay_s=0.5, max_delay_s=5.0)

    async def run(self) -> None:
        await self.bus.ensure_group(Stream.SIGNALS, "risk-gate")
        log.info("risk gate service started mode=%s strategy_hash=%s", self.mode.value, self.strategy_hash)
        while True:
            msgs = await self.bus.consume(Stream.SIGNALS, "risk-gate", self.consumer_name,
                                          count=10, block_ms=5000)
            for msg in msgs:
                try:
                    await retry_async(lambda m=msg: self._handle(m.payload), cfg=self._retry,
                                      retryable=(ConnectionError,))
                except Exception:
                    log.exception("failed to process signal id=%s (giving up after retries)", msg.id)
                await self.bus.ack(Stream.SIGNALS, "risk-gate", msg.id)
            await self.bus.heartbeat("risk-gate")

    async def _handle(self, payload: dict) -> None:
        sig = SignalMessage.from_payload(payload)
        token_id = await self.repo.get_or_create_token_id(sig.token)

        if not sig.gates_passed:
            await self._record(sig, token_id, acted_on=False,
                              reason=f"gate_failed:{','.join(sig.failed_gates)}")
            return
        if sig.win_probability is None or sig.expected_value_bps is None:
            await self._record(sig, token_id, acted_on=False, reason="no_calibrated_ev")
            return

        state = await self.repo.load_portfolio_state(self.mode)
        state = RiskGate.roll_day(state)

        sizing = compute_position_size(
            equity_usd=state.equity_usd, free_capital_usd=state.free_capital_usd,
            reserve_quote_usd=float(sig.reserve_quote_usd), cfg=self.sizing_cfg,
            win_prob=sig.win_probability, atr_pct=sig.atr_pct,
        )
        if not sizing.is_tradeable:
            await self._record(sig, token_id, acted_on=False,
                              reason=f"sizing_zero:{sizing.binding_cap.value}")
            return

        decision = self.gate.check_entry(state, sig.token.chain, sizing.notional_usd)

        if decision.new_state is not None and decision.new_state != state.risk_state:
            await self._apply_state_change(state.risk_state, decision.new_state,
                                           decision.reason, state.equity_usd, state.peak_equity_usd)

        if not decision.allowed:
            await self._record(sig, token_id, acted_on=False, reason=decision.reason)
            return

        signal_id = await self._record(sig, token_id, acted_on=True, reason=None)

        quote_sol = sizing.notional_usd / sig.quote_usd_price if sig.quote_usd_price > 0 else Decimal(0)
        if quote_sol <= 0:
            log.error("computed zero SOL notional for %s — quote_usd_price=%s, dropping",
                     sig.token.key, sig.quote_usd_price)
            return

        intent = TradeIntent(
            intent_id=new_id(), token=sig.token, side=Side.BUY, quote_amount=quote_sol,
            base_amount=None, max_slippage_bps=self.default_max_slippage_bps,
            idempotency_key=new_id(), mode=self.mode,
            reason=f"signal:{signal_id} score={sig.score_total:.1f} binding={sizing.binding_cap.value}",
            correlation_id=sig.correlation_id,
        )
        from mca.execution.base import intent_to_payload
        await self.bus.publish(Stream.INTENTS, intent_to_payload(intent))
        log.info("entry authorized token=%s notional=$%.2f binding=%s",
                sig.token.key, sizing.notional_usd, sizing.binding_cap.value)

    async def _record(self, sig: SignalMessage, token_id: int, *, acted_on: bool,
                      reason: str | None) -> int:
        from mca.domain.models import GateResult, Score
        score = Score(token=sig.token, as_of=sig.as_of, total=sig.score_total,
                     components=sig.components, weights=sig.weights,
                     gates=[GateResult(g["name"], g["passed"], g.get("detail", "")) for g in sig.gates],
                     win_probability=sig.win_probability, expected_value_bps=sig.expected_value_bps,
                     model_version=sig.model_version)
        row = signal_to_row(score, token_id=token_id, mode=sig.mode, strategy_hash=sig.strategy_hash,
                           correlation_id=sig.correlation_id, model_version=sig.model_version,
                           acted_on=acted_on, rejection_reason=reason)
        return await self.repo.insert_signal(row)

    async def _apply_state_change(self, old_state, new_state, reason: str,
                                  equity_usd: Decimal, peak_equity_usd: Decimal) -> None:
        await self.repo.set_risk_state(new_state, reason, equity_usd, peak_equity_usd)
        await self.repo.record_risk_event("circuit_breaker", old_state, new_state, {"reason": reason})
        await self.bus.publish(Stream.RISK_EVENTS, {
            "event": "circuit_breaker", "old_state": old_state.value, "new_state": new_state.value,
            "reason": reason, "equity_usd": str(equity_usd), "at": utcnow().isoformat(),
        })
        log.warning("risk state %s -> %s: %s", old_state.value, new_state.value, reason)
        if self.notifier is not None:
            await self.notifier.send(circuit_breaker_tripped(new_state.value, reason, float(equity_usd)))


# --------------------------------------------------------------------------- #
# Entry point — `python -m mca.risk.gate_service`, per docker-compose.yml.
#
# Missing through the first Docker run of this project: the module defined
# the class but nothing that actually constructed and ran one. `python -m`
# imported the module, found nothing to execute, and exited 0 — which Docker's
# `restart: unless-stopped` policy then restarted immediately, forever,
# without ever logging an error (there was nothing to fail). Found by
# `docker inspect` showing RestartCount: 66 with ExitCode: 0 and empty logs —
# a clean exit, not a crash, is its own diagnosis once you know to look for it.
# --------------------------------------------------------------------------- #

async def _main() -> None:
    import os
    from mca.core.config import load_config
    from mca.execution.base import strategy_fingerprint

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config()
    bus = Bus(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    repo = Repository(os.environ["DATABASE_URL"])
    mode = TradeMode(cfg.system.mode)
    strategy_hash = strategy_fingerprint(cfg.model_dump())

    from mca.alerts.notifier import Notifier
    from mca.alerts.telegram import telegram_channel
    notifier = None
    tg_token, tg_chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if tg_token and tg_chat:
        notifier = Notifier([telegram_channel(tg_token, tg_chat)])

    service = RiskGateService(bus, repo, limits=cfg.to_risk_limits(), sizing_cfg=cfg.to_sizing_config(),
                              mode=mode, strategy_hash=strategy_hash, notifier=notifier)
    await service.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
