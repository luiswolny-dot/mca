"""Watchdog: the process that watches the other processes.

Two responsibilities, both "meta" oversight rather than trading logic, which is
why they live in one small process instead of being split further:

  1. **Heartbeat monitoring.** Every long-running worker calls
     ``Bus.heartbeat(name)`` on each loop iteration (see ``risk/gate_service.py``,
     ``execution/worker.py``, ``portfolio/manager.py``). A worker that stops
     heartbeating — crashed, deadlocked, stuck retrying — is detected here
     within ``watchdog_missed_beats_before_halt`` missed checks and escalated
     to ``RiskState.HALT``, matching the state diagram in
     ``docs/ARCHITECTURE.md``: a dead risk gate or executor means new entries
     must stop, because nothing is left to enforce the limits on them.

  2. **Relaying risk events to alerts.** ``risk/gate_service.py`` and
     ``execution/worker.py`` publish structured events to ``Stream.RISK_EVENTS``
     but hold no Telegram/Discord credentials themselves — see the isolation
     principle in ``execution/base.py``. The watchdog is the one process
     that both reads that stream and has a ``Notifier`` configured, so it is
     the natural place to turn "circuit_breaker tripped" into a phone
     notification.

Design decision — HALT here, LOCKED only via the API
--------------------------------------------------------
The watchdog can move the system to ``HALT`` (stop opening new positions) on
its own authority — a dead worker is an operational fact, not a judgement
call. It never sets ``LOCKED``; that state means a loss limit was breached and
by design requires the human-in-the-loop 2FA unlock in ``api/app.py``. Letting
an automated health check both trip and clear a loss-limit state would quietly
undermine the entire reason that control exists.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from mca.alerts.notifier import Alert, Notifier, Severity
from mca.core.bus import Bus, Stream
from mca.domain.models import RiskState, utcnow

log = logging.getLogger(__name__)

DEFAULT_WORKERS = ["risk-gate", "executor", "position-manager"]


@dataclass(slots=True)
class HealthStatus:
    at: float
    workers: dict[str, bool]
    rpc_ok: bool
    risk_state: str | None
    healthy: bool
    detail: str = ""


def evaluate_health(workers: dict[str, bool], rpc_ok: bool, missed_counts: dict[str, int],
                    missed_threshold: int) -> tuple[bool, str]:
    """Pure decision function: given the current heartbeat/RPC readings and
    the running miss-count per worker, is the system healthy? Separated from
    the actual polling loop so this logic is unit-testable without a Bus or a
    live RPC endpoint."""
    dead = [w for w, count in missed_counts.items() if count >= missed_threshold]
    if dead:
        return False, f"workers not heartbeating: {', '.join(sorted(dead))}"
    if not rpc_ok:
        return False, "RPC endpoint unreachable"
    return True, "ok"


class Watchdog:
    def __init__(self, bus: Bus, repo, rpc, notifier: Notifier | None = None, *,
                workers: list[str] | None = None, check_interval_s: float = 15.0,
                missed_beats_before_halt: int = 4) -> None:
        self.bus = bus
        self.repo = repo
        self.rpc = rpc
        self.notifier = notifier
        self.workers = workers or DEFAULT_WORKERS
        self.check_interval_s = check_interval_s
        self.missed_beats_before_halt = missed_beats_before_halt
        self._missed_counts: dict[str, int] = {w: 0 for w in self.workers}
        self._halted = False

    async def run(self) -> None:
        await self.bus.ensure_group(Stream.RISK_EVENTS, "watchdog")
        log.info("watchdog started: watching %s, check every %.0fs, halt after %d missed beats",
                self.workers, self.check_interval_s, self.missed_beats_before_halt)
        await asyncio.gather(self._health_loop(), self._relay_risk_events())

    # -- health loop ------------------------------------------------------------
    async def _health_loop(self) -> None:
        while True:
            try:
                await self._check_once()
            except Exception:
                log.exception("watchdog health check failed")
            await asyncio.sleep(self.check_interval_s)

    async def _check_once(self) -> HealthStatus:
        alive = await self.bus.alive_workers(self.workers)
        for w, is_alive in alive.items():
            self._missed_counts[w] = 0 if is_alive else self._missed_counts[w] + 1

        rpc_ok = await self._check_rpc()
        healthy, detail = evaluate_health(alive, rpc_ok, self._missed_counts,
                                          self.missed_beats_before_halt)

        status = HealthStatus(at=time.time(), workers=alive, rpc_ok=rpc_ok,
                              risk_state=None, healthy=healthy, detail=detail)

        if not healthy and not self._halted:
            await self._trigger_halt(detail)
        elif healthy and self._halted:
            await self._clear_halt()

        return status

    async def _check_rpc(self) -> bool:
        if self.rpc is None:
            return True   # no chain configured for this instance — nothing to check
        try:
            await asyncio.wait_for(self.rpc.get_latest_blockhash(), timeout=8.0)
            return True
        except Exception as exc:
            log.warning("RPC health check failed: %s", exc)
            return False

    async def _trigger_halt(self, reason: str) -> None:
        self._halted = True
        log.error("WATCHDOG HALT: %s", reason)
        try:
            rs = await self.repo.get_risk_state_row()
            if rs["state"] == "normal" or rs["state"] == "derisk":
                from decimal import Decimal
                await self.repo.set_risk_state(RiskState.HALT, f"watchdog: {reason}",
                                              Decimal(rs["equity_usd"]), Decimal(rs["peak_equity_usd"]))
                await self.repo.record_risk_event("watchdog_halt", RiskState(rs["state"]),
                                                  RiskState.HALT, {"reason": reason})
        except Exception:
            log.exception("failed to persist HALT state — alerting anyway")
        if self.notifier is not None:
            await self.notifier.send(Alert(Severity.CRITICAL, "Watchdog: system HALTED", reason,
                                          dedup_key="watchdog-halt"))

    async def _clear_halt(self) -> None:
        self._halted = False
        log.info("watchdog: conditions recovered, clearing HALT")
        try:
            rs = await self.repo.get_risk_state_row()
            if rs["state"] == "halt":
                from decimal import Decimal
                await self.repo.set_risk_state(RiskState.NORMAL, "watchdog: recovered",
                                              Decimal(rs["equity_usd"]), Decimal(rs["peak_equity_usd"]))
                await self.repo.record_risk_event("watchdog_recovered", RiskState.HALT,
                                                  RiskState.NORMAL, {})
        except Exception:
            log.exception("failed to persist HALT recovery")
        if self.notifier is not None:
            await self.notifier.send(Alert(Severity.WARNING, "Watchdog: HALT cleared",
                                          "All workers heartbeating, RPC reachable.",
                                          dedup_key="watchdog-halt"))

    # -- risk-event relay ---------------------------------------------------
    async def _relay_risk_events(self) -> None:
        consumer = "watchdog-relay"
        while True:
            msgs = await self.bus.consume(Stream.RISK_EVENTS, "watchdog", consumer,
                                          count=10, block_ms=5000)
            for msg in msgs:
                try:
                    await self._relay_one(msg.payload)
                except Exception:
                    log.exception("failed to relay risk event id=%s", msg.id)
                await self.bus.ack(Stream.RISK_EVENTS, "watchdog", msg.id)
            await self.bus.heartbeat("watchdog")

    async def _relay_one(self, payload: dict) -> None:
        if self.notifier is None:
            return
        event = payload.get("event", "unknown")
        severity = Severity.CRITICAL if event in (
            "circuit_breaker", "exit_failed", "orphaned_fill") else Severity.WARNING
        await self.notifier.send(Alert(
            severity, f"Risk event: {event}",
            "\n".join(f"{k}: {v}" for k, v in payload.items() if k != "event"),
            dedup_key=f"risk-event:{event}:{payload.get('token', '')}"))


# --------------------------------------------------------------------------- #
# Entry point — `python -m mca.ops.watchdog`, per docker-compose.yml.
# Missing through the first Docker run; see risk/gate_service.py's entry
# point for the full story.
# --------------------------------------------------------------------------- #

async def _main() -> None:
    import os
    import logging

    from mca.core.config import load_config
    from mca.db.repository import Repository
    from mca.execution.solana_rpc import SolanaRpcClient

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config()
    bus = Bus(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    repo = Repository(os.environ["DATABASE_URL"])
    rpc = SolanaRpcClient(os.environ["SOLANA_RPC_HTTP"]) if os.environ.get("SOLANA_RPC_HTTP") else None

    notifier = None
    from mca.alerts.notifier import Notifier
    from mca.alerts.telegram import telegram_channel
    tg_token, tg_chat = os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID")
    if tg_token and tg_chat:
        notifier = Notifier([telegram_channel(tg_token, tg_chat)])

    watchdog = Watchdog(bus, repo, rpc, notifier,
                        check_interval_s=float(cfg.observability.watchdog_heartbeat_s)
                        if hasattr(cfg, "observability") else 15.0,
                        missed_beats_before_halt=cfg.observability.watchdog_missed_beats_before_halt
                        if hasattr(cfg, "observability") else 4)
    await watchdog.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
