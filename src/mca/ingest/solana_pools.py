"""New liquidity pool detection on Solana.

Design decision — two stages: a fast log filter, then a confirming fetch
----------------------------------------------------------------------------
``logsSubscribe`` on a DEX program delivers *every* transaction that touches
it — for Raydium that is overwhelmingly routine swaps, not pool creation.
Stage one is a cheap, in-memory text check against ``logs`` to discard almost
everything in microseconds. Stage two, only for the small remainder, fetches
the full transaction via ``getTransaction`` and reads the *accounts* the
instruction actually touched. This matters because program logs are
human-readable status messages, not structured data — the pool address and
token mints are reliably available only from the instruction's account list,
never by regexing a log line.

Design decision — extraction fails closed
--------------------------------------------
:func:`extract_pool_from_transaction` returns ``None`` rather than guessing
when the transaction does not match the expected instruction shape closely
enough. This is deliberate and mirrors ``scoring/gates.py``: a
``PoolCreatedEvent`` built from a misidentified instruction would carry a wrong
address into the pipeline; dropping the candidate is the safe failure mode, and
costs nothing because the enrichment stage that runs next re-derives everything
about the pool from scratch (DexScreener, on-chain reserve reads) rather than
trusting what ingest extracted. Ingest only needs to be a fast, honest filter —
not the source of truth.

Design decision — account indices are unverified in this session and say so
--------------------------------------------------------------------------------
The account ordering below for Raydium's ``initialize2`` instruction reflects
publicly documented layouts, but this sandbox has no network access to
independently confirm it against the current on-chain IDL. Verify with
``anchor idl fetch <PROGRAM_ID> --provider.cluster mainnet`` and cross-check
against https://docs.raydium.io/reference/program-addresses (which itself warns
that a mismatched program ID is "the single easiest way to lose funds on
Solana") before depending on the *values* extracted here for anything beyond
"probably worth enriching further."
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from mca.core.bus import Bus, Stream
from mca.domain.models import Chain, Dex, PoolCreatedEvent, TokenRef
from mca.execution.solana_rpc import SolanaRpcClient, SolanaWebsocketClient

log = logging.getLogger(__name__)

# Verified 2026-07-21 against two independent sources (Raydium's own docs table
# and a third-party on-chain-address-consuming SDK). See module docstring.
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"

# NOT independently verified this session. Raydium's own docs state that new
# pool creation has moved from AMM v4 to CPMM/CLMM — fill these in from
# https://docs.raydium.io/reference/program-addresses before relying on them;
# left unset on purpose rather than shipping a guessed address, per the module
# docstring's warning about mismatched program IDs.
RAYDIUM_CPMM: str | None = None
RAYDIUM_CLMM: str | None = None
PUMPFUN_PROGRAM: str | None = None

DEX_PROGRAMS: dict[str, Dex] = {
    pid: dex for pid, dex in (
        (RAYDIUM_AMM_V4, Dex.RAYDIUM),
        (RAYDIUM_CPMM, Dex.RAYDIUM),
        (RAYDIUM_CLMM, Dex.RAYDIUM),
        (PUMPFUN_PROGRAM, Dex.PUMPSWAP),
    ) if pid is not None
}

# Log-line substrings that accompany a pool/market *initialisation* instruction
# as opposed to a swap, deposit, or withdraw against an existing pool. This is
# the fast stage-one filter — approximate by nature (see module docstring);
# stage two (account inspection) is what actually confirms it.
_INIT_LOG_MARKERS: dict[Dex, tuple[str, ...]] = {
    Dex.RAYDIUM: ("initialize2", "init_pc_amount", "InitializeInstruction2"),
    Dex.PUMPSWAP: ("create", "initialize"),
}

# Raydium AMM v4 ``initialize2``: 0-indexed positions in the instruction's
# resolved account list. UNVERIFIED this session — see module docstring.
_RAYDIUM_INIT2_ACCOUNTS = {
    "amm": 4, "coin_mint": 8, "pc_mint": 9,
    "coin_vault": 10, "pc_vault": 11, "lp_mint": 7,
}


def looks_like_pool_creation(log_lines: list[str], dex: Dex) -> bool:
    """Stage-one filter. Pure and cheap — called on every notification."""
    markers = _INIT_LOG_MARKERS.get(dex, ())
    if not markers:
        return False
    joined = "\n".join(log_lines)
    return any(m in joined for m in markers)


def extract_pool_from_transaction(tx: dict, dex: Dex, signature: str,
                                  detected_at: datetime | None = None) -> PoolCreatedEvent | None:
    """Stage-two confirmation. Returns ``None`` (fail closed) rather than a
    best-guess when the transaction's shape does not match what we expect —
    see the module docstring for why that is the correct trade-off here."""
    if tx is None:
        log.debug("no transaction body for %s, skipping", signature)
        return None
    try:
        meta = tx.get("meta") or {}
        if meta.get("err") is not None:
            return None   # a failed pool-init transaction created nothing

        message = tx["transaction"]["message"]
        account_keys = message["accountKeys"]

        def _pubkey_at(idx: int) -> str | None:
            if idx >= len(account_keys):
                return None
            entry = account_keys[idx]
            return entry["pubkey"] if isinstance(entry, dict) else entry

        if dex is Dex.RAYDIUM:
            idx = _RAYDIUM_INIT2_ACCOUNTS
            pool_addr = _pubkey_at(idx["amm"])
            coin_mint = _pubkey_at(idx["coin_mint"])
            pc_mint = _pubkey_at(idx["pc_mint"])
            if not (pool_addr and coin_mint and pc_mint):
                log.warning("raydium init2 candidate %s did not resolve expected "
                           "accounts — dropping rather than guessing", signature)
                return None

            # The quote side is whichever mint is the well-known wrapped-SOL
            # mint; the base side is the other one. A pool where *neither* side
            # is SOL (e.g. two arbitrary SPL tokens) is out of scope for a
            # meme-coin/SOL strategy and is dropped here, not enriched further.
            from mca.execution.jupiter import WSOL_MINT
            if coin_mint == WSOL_MINT:
                base_mint, quote_mint = pc_mint, coin_mint
            elif pc_mint == WSOL_MINT:
                base_mint, quote_mint = coin_mint, pc_mint
            else:
                log.debug("pool %s is not SOL-quoted, skipping", pool_addr)
                return None

            slot = tx.get("slot", 0)
            deployer = _pubkey_at(0)   # fee payer, account 0 by Solana convention
            return PoolCreatedEvent(
                token=TokenRef(chain=Chain.SOLANA, address=base_mint),
                quote_token=TokenRef(chain=Chain.SOLANA, address=quote_mint),
                dex=dex, pool_address=pool_addr,
                detected_at=detected_at or datetime.now(timezone.utc),
                tx_signature=signature, block_or_slot=slot, deployer=deployer,
            )

        log.debug("no extractor implemented for %s, skipping %s", dex.value, signature)
        return None
    except (KeyError, IndexError, TypeError) as exc:
        log.warning("malformed transaction shape for %s: %r — dropping", signature, exc)
        return None


class SolanaPoolListener:
    """Ties the websocket log stream, the confirming RPC fetch, and the bus
    together. One instance per DEX program watched (matches
    ``docker-compose.yml``'s one-ingestor-per-source isolation: a parsing bug
    against one program's transactions should not take down detection for the
    others)."""

    def __init__(self, ws: SolanaWebsocketClient, rpc: SolanaRpcClient,
                bus: Bus, program_id: str) -> None:
        if program_id not in DEX_PROGRAMS:
            raise ValueError(
                f"{program_id} is not in DEX_PROGRAMS — add it with a verified "
                f"address (see module docstring) before watching it")
        self.ws = ws
        self.rpc = rpc
        self.bus = bus
        self.program_id = program_id
        self.dex = DEX_PROGRAMS[program_id]

    async def run(self) -> None:
        """Runs forever; ``SolanaWebsocketClient.subscribe_logs`` already
        handles reconnection, so this loop only needs to handle per-message
        failures without dying itself — one bad transaction must not stop the
        listener for every subsequent one."""
        async for notification in self.ws.subscribe_logs([self.program_id]):
            if notification.get("err") is not None:
                continue    # failed tx, cannot have created a pool
            if not looks_like_pool_creation(notification["logs"], self.dex):
                continue
            try:
                await self._handle_candidate(notification)
            except Exception:
                log.exception("failed to process pool-creation candidate sig=%s",
                             notification.get("signature"))

    async def _handle_candidate(self, notification: dict) -> None:
        sig = notification["signature"]
        tx = await self.rpc.get_transaction(sig)
        event = extract_pool_from_transaction(tx, self.dex, sig)
        if event is None:
            return
        await self.bus.publish(Stream.POOL_EVENTS, _event_to_payload(event))
        log.info("pool detected dex=%s token=%s pool=%s sig=%s",
                self.dex.value, event.token.address, event.pool_address, sig)


def _event_to_payload(event: PoolCreatedEvent) -> dict:
    """dict for the bus — kept next to the class that produces it rather than
    in ``core/bus.py``, so the payload shape and the struct that generates it
    change together."""
    return {
        "token": {"chain": event.token.chain.value, "address": event.token.address},
        "quote_token": {"chain": event.quote_token.chain.value, "address": event.quote_token.address},
        "dex": event.dex.value,
        "pool_address": event.pool_address,
        "detected_at": event.detected_at.isoformat(),
        "tx_signature": event.tx_signature,
        "block_or_slot": event.block_or_slot,
        "initial_base_reserve": event.initial_base_reserve,
        "initial_quote_reserve": event.initial_quote_reserve,
        "deployer": event.deployer,
        "correlation_id": event.correlation_id,
    }


def payload_to_event(payload: dict) -> PoolCreatedEvent:
    """Inverse of :func:`_event_to_payload`, used by the enricher consuming
    ``Stream.POOL_EVENTS``."""
    return PoolCreatedEvent(
        token=TokenRef(Chain(payload["token"]["chain"]), payload["token"]["address"]),
        quote_token=TokenRef(Chain(payload["quote_token"]["chain"]), payload["quote_token"]["address"]),
        dex=Dex(payload["dex"]), pool_address=payload["pool_address"],
        detected_at=datetime.fromisoformat(payload["detected_at"]),
        tx_signature=payload["tx_signature"], block_or_slot=payload["block_or_slot"],
        initial_base_reserve=payload.get("initial_base_reserve"),
        initial_quote_reserve=payload.get("initial_quote_reserve"),
        deployer=payload.get("deployer"), correlation_id=payload["correlation_id"],
    )
