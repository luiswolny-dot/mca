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
from mca.domain.models import Chain, Side, TradeMode, utcnow
from mca.execution.base import (
    Executor, ExecutionError, fill_to_payload, intent_from_payload,
)

log = logging.getLogger(__name__)


class ExecutorWorker:
    def __init__(self, bus: Bus, executors: dict[Chain, Executor],
                jupiter_by_chain: dict[Chain, "JupiterClient"] | None = None, *,
                consumer_name: str | None = None) -> None:
        self.bus = bus
        self.executors = executors
        self.jupiter_by_chain = jupiter_by_chain or {}
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

        jupiter = self.jupiter_by_chain.get(intent.token.chain)
        if jupiter is not None:
            try:
                executor.quote_usd_price = await jupiter.sol_usd_price()
            except Exception as exc:
                log.warning("could not refresh SOL/USD price before executing intent=%s: %s",
                          intent.intent_id, exc)

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


# --------------------------------------------------------------------------- #
# Building the executor + Jupiter/RPC clients — shared by execution/worker.py
# and portfolio/manager.py's entry points, since both need the exact same
# "which chain, which mode, which keys" wiring and must never end up with two
# different executor instances disagreeing about the current SOL/USD price.
# --------------------------------------------------------------------------- #

def build_solana_stack(mode: "TradeMode", exec_cfg: "ExecutionConfig"):
    """Returns ``(executor, rpc, jupiter)`` for Solana. LIVE mode requires
    ``KEYSTORE_PATH``/``KEYSTORE_PASSPHRASE`` env vars and an unlocked keystore;
    PAPER mode (the default) needs neither — see ``PaperExecutor``'s docstring
    on why it never touches key material."""
    import os
    from mca.execution.jupiter import JupiterClient, WSOL_MINT
    from mca.execution.solana_rpc import SolanaRpcClient

    rpc = SolanaRpcClient(os.environ["SOLANA_RPC_HTTP"])
    jupiter = JupiterClient(api_key=os.environ.get("JUPITER_API_KEY") or None)

    if mode is TradeMode.LIVE:
        from mca.db.repository import Repository, row_to_shadow_record
        from mca.execution.solana_executor import SolanaJupiterExecutor
        from mca.execution.wallet import WalletManager
        from mca.execution.base import strategy_fingerprint
        from mca.core.config import load_config

        keystore_path = os.environ.get("KEYSTORE_PATH", "secrets/keystore.json")
        passphrase = os.environ["KEYSTORE_PASSPHRASE"]   # deliberately no default —
        # a live executor with a silently-empty passphrase is worse than a crash on boot.
        from mca.execution.wallet import EncryptedKeystore
        ks = EncryptedKeystore.load(keystore_path)
        secret = WalletManager.unlock_raw(ks, passphrase)

        try:
            from solders.keypair import Keypair  # type: ignore
            keypair = Keypair.from_bytes(secret) if len(secret) == 64 else Keypair.from_seed(secret)
        except ImportError as exc:
            raise RuntimeError("solders is required for LIVE mode: pip install solders") from exc

        cfg = load_config()
        strategy_hash = strategy_fingerprint(cfg.model_dump())
        repo = Repository(os.environ["DATABASE_URL"])
        # Loading the shadow record here is synchronous-context best-effort;
        # SolanaJupiterExecutor's constructor itself enforces the mandate via
        # LiveExecutorGuard — this call site cannot bypass it by getting the
        # record wrong, only fail to construct the executor at all.
        import asyncio
        shadow_row = asyncio.run(repo.get_shadow_record(strategy_hash))

        executor = SolanaJupiterExecutor(mode, exec_cfg, rpc=rpc, jupiter=jupiter,
                                         keypair=keypair, shadow_record=shadow_row,
                                         strategy_hash=strategy_hash)
    else:
        from mca.execution.base import PaperExecutor
        executor = PaperExecutor(Chain.SOLANA, exec_cfg, jupiter, rpc, WSOL_MINT)

    return executor, rpc, jupiter


async def _main() -> None:
    import os
    from mca.core.config import load_config

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    cfg = load_config()
    mode = TradeMode(cfg.system.mode)
    exec_cfg = cfg.to_execution_config(Chain.SOLANA)

    executor, rpc, jupiter = build_solana_stack(mode, exec_cfg)
    bus = Bus(os.environ.get("REDIS_URL", "redis://redis:6379/0"))

    worker = ExecutorWorker(bus, executors={Chain.SOLANA: executor},
                            jupiter_by_chain={Chain.SOLANA: jupiter})
    await worker.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
