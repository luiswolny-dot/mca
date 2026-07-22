"""Enrich worker: consumes ``Stream.POOL_EVENTS``, produces ``Stream.CANDIDATES``.

Design decision — social and flow snapshots stay ``None``, honestly
------------------------------------------------------------------------
``Candidate.social`` and ``Candidate.flow`` are left unset here, not filled
with zeros. ``ingest/social`` (Twitter/Telegram/Reddit/trends) and whale-wallet
labelling are not built — see ``docs/UEBERBLICK.md``. ``scoring/scorer.py``
already treats a missing snapshot as "no signal" rather than "bad signal" (a
zero would read as *negative* sentiment/no smart money, which is a different
and wrong claim). Wiring those two inputs later needs no change here.

Design decision — a missing price is a dropped candidate, not a zero-scored one
------------------------------------------------------------------------------
If DexScreener has not indexed the pool yet, or on-chain checks fail
entirely, this worker drops the event rather than publish a ``Candidate``
built from partial guesses. ``scoring/gates.py`` would fail such a candidate
anyway (``None`` fails hard gates), but dropping it here means the DB's
``signals`` table — used for the gate-rejection histogram — only records
tokens that were genuinely evaluated, not ones enrichment gave up on.
"""

from __future__ import annotations

import logging
import os

from mca.core.bus import Bus, Stream
from mca.domain.models import Candidate, new_id, utcnow
from mca.enrich.dexscreener import (
    DexScreenerClient, pair_created_at, parse_pair_to_market_snapshot,
)
from mca.enrich.onchain import OnChainSecurityChecker
from mca.execution.solana_rpc import SolanaRpcClient
from mca.ingest.solana_pools import payload_to_event

log = logging.getLogger(__name__)

# DexScreener's chain slug for Solana — confirmed via docs.dexscreener.com,
# not assumed.
DEXSCREENER_SOLANA_CHAIN_ID = "solana"


def candidate_to_payload(cand: Candidate) -> dict:
    """Wire format for Stream.CANDIDATES — deliberately flat and explicit
    rather than a generic dataclass-to-dict dump, so a field rename in
    domain/models.py cannot silently change the bus contract."""
    sec = cand.security
    mkt = cand.market
    return {
        "token": {"chain": cand.token.chain.value, "address": cand.token.address},
        "as_of": cand.as_of.isoformat(),
        "correlation_id": cand.correlation_id,
        "security": {
            "mint_authority_revoked": sec.mint_authority_revoked,
            "freeze_authority_revoked": sec.freeze_authority_revoked,
            "top1_holder_pct": sec.top1_holder_pct, "top10_holder_pct": sec.top10_holder_pct,
            "liquidity_usd": str(sec.liquidity_usd) if sec.liquidity_usd is not None else None,
            "deployer": sec.deployer,
            "created_at": sec.created_at.isoformat() if sec.created_at else None,
            "simulated_sell_ok": sec.simulated_sell_ok,
        },
        "market": {
            "price_usd": str(mkt.price_usd), "liquidity_usd": str(mkt.liquidity_usd),
            "fdv_usd": str(mkt.fdv_usd) if mkt.fdv_usd is not None else None,
            "volume_usd": {k: str(v) for k, v in mkt.volume_usd.items()},
            "buys": mkt.buys, "sells": mkt.sells, "price_change_pct": mkt.price_change_pct,
            "pool_reserve_base": mkt.pool_reserve_base, "pool_reserve_quote": mkt.pool_reserve_quote,
        },
    }


def payload_to_candidate(payload: dict) -> Candidate:
    """Inverse of :func:`candidate_to_payload`, used by scoring/worker.py."""
    from decimal import Decimal
    from datetime import datetime
    from mca.domain.models import Chain, MarketSnapshot, SecurityReport, TokenRef

    token = TokenRef(Chain(payload["token"]["chain"]), payload["token"]["address"])
    s = payload["security"]
    security = SecurityReport(
        token=token,
        mint_authority_revoked=s["mint_authority_revoked"],
        freeze_authority_revoked=s["freeze_authority_revoked"],
        top1_holder_pct=s["top1_holder_pct"], top10_holder_pct=s["top10_holder_pct"],
        liquidity_usd=Decimal(s["liquidity_usd"]) if s.get("liquidity_usd") is not None else None,
        deployer=s.get("deployer"),
        created_at=datetime.fromisoformat(s["created_at"]) if s.get("created_at") else None,
        simulated_sell_ok=s.get("simulated_sell_ok"),
    )
    m = payload["market"]
    market = MarketSnapshot(
        token=token, as_of=datetime.fromisoformat(payload["as_of"]),
        price_usd=Decimal(m["price_usd"]), liquidity_usd=Decimal(m["liquidity_usd"]),
        fdv_usd=Decimal(m["fdv_usd"]) if m.get("fdv_usd") is not None else None,
        volume_usd={int(k): Decimal(v) for k, v in m.get("volume_usd", {}).items()},
        buys={int(k): v for k, v in m.get("buys", {}).items()},
        sells={int(k): v for k, v in m.get("sells", {}).items()},
        price_change_pct={int(k): v for k, v in m.get("price_change_pct", {}).items()},
        pool_reserve_base=m.get("pool_reserve_base"), pool_reserve_quote=m.get("pool_reserve_quote"),
    )
    return Candidate(token=token, as_of=datetime.fromisoformat(payload["as_of"]), market=market,
                     security=security, social=None, flow=None, ohlcv_1m=None,
                     correlation_id=payload["correlation_id"])


class EnrichWorker:
    def __init__(self, bus: Bus, rpc: SolanaRpcClient, dexscreener: DexScreenerClient, *,
                consumer_name: str | None = None) -> None:
        self.bus = bus
        self.rpc = rpc
        self.dexscreener = dexscreener
        self.onchain = OnChainSecurityChecker(rpc)
        self.consumer_name = consumer_name or f"enrich-{os.getpid()}"

    async def run(self) -> None:
        await self.bus.ensure_group(Stream.POOL_EVENTS, "enrich")
        log.info("enrich worker started")
        while True:
            msgs = await self.bus.consume(Stream.POOL_EVENTS, "enrich", self.consumer_name,
                                          count=10, block_ms=5000)
            for msg in msgs:
                try:
                    await self._handle(msg.payload)
                except Exception:
                    log.exception("failed to enrich pool event id=%s", msg.id)
                await self.bus.ack(Stream.POOL_EVENTS, "enrich", msg.id)
            await self.bus.heartbeat("enrich")

    async def _handle(self, payload: dict) -> None:
        event = payload_to_event(payload)

        try:
            pair = await self.dexscreener.get_pair(DEXSCREENER_SOLANA_CHAIN_ID, event.pool_address)
        except Exception as exc:
            log.warning("DexScreener lookup failed for pool %s: %s", event.pool_address, exc)
            pair = None
        if pair is None:
            log.info("pool %s not yet indexed by DexScreener, dropping (will not retry — "
                    "ingest sees it once, enrich must decide now)", event.pool_address)
            return

        exclude = {event.base_vault} if event.base_vault else set()
        security = await self.onchain.check(
            event.token, deployer=event.deployer,
            created_at=pair_created_at(pair) or event.detected_at, exclude_addresses=exclude)
        security.liquidity_usd = None  # filled below from the same market snapshot, avoid two
                                        # slightly different liquidity numbers disagreeing silently

        decimals = None
        if security.mint_authority_revoked is not None:
            # onchain.check() read the mint account; decimals were available
            # there but not carried on SecurityReport (not its job) — refetch
            # is wasteful, so this worker rereads it once, directly.
            try:
                info = await self.rpc.get_account_info_parsed(event.token.address)
                decimals = ((info or {}).get("data", {}).get("parsed", {}) or {}).get("info", {}).get("decimals")
            except Exception:
                decimals = None

        market = parse_pair_to_market_snapshot(pair, event.token, token_decimals=decimals)
        if market is None:
            log.info("pool %s has no usable price/liquidity yet, dropping", event.pool_address)
            return
        security.liquidity_usd = market.liquidity_usd

        candidate = Candidate(
            token=event.token, as_of=utcnow(), market=market, security=security,
            social=None, flow=None, ohlcv_1m=None, correlation_id=event.correlation_id or new_id(),
        )
        await self.bus.publish(Stream.CANDIDATES, candidate_to_payload(candidate))
        log.info("candidate published token=%s liquidity=$%.0f mint_revoked=%s top10=%s",
                event.token.key, market.liquidity_usd, security.mint_authority_revoked,
                security.top10_holder_pct)


# --------------------------------------------------------------------------- #
# Entry point — `python -m mca.enrich.worker`, per docker-compose.yml.
# Missing through the first Docker run; see risk/gate_service.py's entry
# point for the full story.
# --------------------------------------------------------------------------- #

async def _main() -> None:
    import os
    import logging

    from mca.enrich.dexscreener import DexScreenerClient
    from mca.execution.solana_rpc import SolanaRpcClient

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bus = Bus(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    rpc = SolanaRpcClient(os.environ["SOLANA_RPC_HTTP"])
    dexscreener = DexScreenerClient()

    worker = EnrichWorker(bus, rpc, dexscreener)
    await worker.run()


if __name__ == "__main__":
    import asyncio
    asyncio.run(_main())
