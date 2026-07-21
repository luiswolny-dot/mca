"""Executor worker: consumes ``Stream.INTENTS``, produces ``Stream.FILLS``.

The container running this is the only one with a keystore mounted (see
``docker-compose.yml`` and the design note at the top of ``execution/base.py``).
This module itself never reads the keystore — it receives an already-unlocked
:class:`~mca.execution.base.Executor` per chain from whoever constructs
:class:`ExecutorWorker`, keeping "decrypt the key" and "spend from it" as
separately auditable steps.

Design decision — a failed SELL is escalated differently from a failed BUY
------------------------------------------------------------------------------
A BUY that fails to execute cost nothing beyond a missed opportunity — the
position was never opened. A SELL that fails to execute leaves a position open
that the exit engine already decided must be closed; every tick until it
succeeds is additional market exposure the risk model did not intend. That
distinction is why a failed SELL publishes a CRITICAL-shaped event to
``Stream.RISK_EVENTS`` and a failed BUY only logs.
"""

from __future__ import annotations

import logging
import os

from mca.core.bus import Bus, Stream
from mca.domain.models import Chain, Side, utcnow
from mca.execution.base import (
    Executor, ExecutionError, fill_to_payload, intent_from_payload,
)

log = logging.getLogger(__name__)


class ExecutorWorker:
    def __init__(self, bus: Bus, executors: dict[Chain, Executor], *,
                consumer_name: str | None = None) -> None:
        self.bus = bus
        self.executors = executors
        self.consumer_name = consumer_name or f"executor-{os.getpid()}"

    async def run(self) -> None:
        await self.bus.ensure_group(Stream.INTENTS, "executor")
        log.info("executor worker started chains=%s",
                [c.value for c in self.executors])
        while True:
            msgs = await self.bus.consume(Stream.INTENTS, "executor", self.consumer_name,
                                          count=5, block_ms=5000)
            for msg in msgs:
                try:
                    await self._handle(msg.payload)
                except Exception:
                    log.exception("unhandled error processing intent id=%s", msg.id)
                await self.bus.ack(Stream.INTENTS, "executor", msg.id)
            await self.bus.heartbeat("executor")

    async def _handle(self, payload: dict) -> None:
        intent = intent_from_payload(payload)
        executor = self.executors.get(intent.token.chain)
        if executor is None:
            log.error("no executor configured for chain=%s, intent=%s dropped",
                     intent.token.chain.value, intent.intent_id)
            return

        try:
            fill = await executor.execute(intent)
        except ExecutionError as exc:
            log.error("execution failed intent=%s side=%s token=%s: %s",
                     intent.intent_id, intent.side.value, intent.token.key, exc)
            if intent.side is Side.SELL:
                await self.bus.publish(Stream.RISK_EVENTS, {
                    "event": "exit_failed", "token": intent.token.address,
                    "chain": intent.token.chain.value, "intent_id": intent.intent_id,
                    "reason": str(exc), "at": utcnow().isoformat(),
                })
            return

        if fill is None:
            log.info("intent %s already executed (idempotency), no new fill", intent.intent_id)
            return

        await self.bus.publish(Stream.FILLS, fill_to_payload(fill, idempotency_key=intent.idempotency_key))
        log.info("fill recorded intent=%s side=%s base_amount=%d tx=%s",
                intent.intent_id, fill.side.value, fill.base_amount, fill.tx_hash)
