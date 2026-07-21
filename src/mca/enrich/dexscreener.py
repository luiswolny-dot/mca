"""DexScreener client — free, unauthenticated, read-only pair data.

Verified against the current official docs (docs.dexscreener.com/api/reference,
fetched 2026-07-21) rather than trusted from training data, matching how
Jupiter's API was verified in an earlier round: no API key required for the
endpoint used here, base URL ``https://api.dexscreener.com``.

Design decision — GET /latest/dex/pairs/{chainId}/{pairId}, not a token search
--------------------------------------------------------------------------------
``ingest/solana_pools.py`` already knows the exact pool address from the
creation transaction. Looking the pair up directly by address is one
deterministic result; searching by token address can return multiple pairs
(a token can have several pools) and would need extra logic to pick the right
one for no benefit here.

Design decision — a missing/null field is missing, not zero
------------------------------------------------------------------
``liquidity`` and ``priceUsd`` are nullable in DexScreener's own schema (a
freshly-indexed pair sometimes has no liquidity object yet). Silently mapping
that to ``0`` would make the min-liquidity gate fail closed for the wrong
reason — indistinguishable from "genuinely no liquidity" — so a missing pair,
missing liquidity, or missing price all produce ``None`` from
:func:`parse_pair_to_market_snapshot`, and the caller decides whether to skip
the candidate rather than score it on fabricated data.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from mca.domain.models import MarketSnapshot, TokenRef, utcnow

log = logging.getLogger(__name__)

BASE_URL = "https://api.dexscreener.com"

# DexScreener window keys -> MarketSnapshot's second-based window keys.
_WINDOW_MAP = {"m5": 300, "h1": 3600, "h6": 21600, "h24": 86400}

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False


class DexScreenerError(Exception):
    pass


class DexScreenerClient:
    def __init__(self, timeout_s: float = 8.0) -> None:
        if not HTTPX_AVAILABLE:
            raise RuntimeError("httpx not installed: pip install httpx")
        self._client = httpx.AsyncClient(base_url=BASE_URL, timeout=timeout_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_pair(self, chain_id: str, pair_address: str) -> dict | None:
        """Returns the raw ``Pair`` object, or ``None`` if not (yet) indexed —
        DexScreener typically lags pool creation by seconds to low minutes, so
        a fresh pool legitimately 404s for a short window; that is not an
        error condition worth raising on."""
        resp = await self._client.get(f"/latest/dex/pairs/{chain_id}/{pair_address}")
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise DexScreenerError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
        pairs = data.get("pairs") or data.get("pair")
        if isinstance(pairs, list):
            return pairs[0] if pairs else None
        return pairs


def parse_pair_to_market_snapshot(pair: dict | None, token: TokenRef, *,
                                  token_decimals: int | None = None,
                                  as_of: datetime | None = None) -> MarketSnapshot | None:
    if pair is None:
        return None
    price_raw = pair.get("priceUsd")
    if price_raw is None:
        return None
    liquidity = pair.get("liquidity")
    if liquidity is None or liquidity.get("usd") is None:
        return None

    as_of = as_of or utcnow()
    volume_raw = pair.get("volume") or {}
    txns_raw = pair.get("txns") or {}
    change_raw = pair.get("priceChange") or {}

    volume_usd = {sec: Decimal(str(volume_raw[k])) for k, sec in _WINDOW_MAP.items() if k in volume_raw}
    buys = {sec: int(txns_raw[k]["buys"]) for k, sec in _WINDOW_MAP.items()
           if k in txns_raw and "buys" in txns_raw[k]}
    sells = {sec: int(txns_raw[k]["sells"]) for k, sec in _WINDOW_MAP.items()
            if k in txns_raw and "sells" in txns_raw[k]}
    price_change_pct = {sec: float(change_raw[k]) for k, sec in _WINDOW_MAP.items() if k in change_raw}

    # DexScreener's liquidity.base/quote are decimal-adjusted (human-readable)
    # token amounts, e.g. "2.3968" — MarketSnapshot.pool_reserve_base wants raw
    # base units (an int) for exact AMM impact math in risk/sizing.py. This
    # exact mismatch (decimal-adjusted vs. raw base units) is what produced a
    # real unit bug elsewhere in this project — see README's "gefundene
    # Inkonsistenzen" — so the conversion is explicit and guarded, not implicit.
    pool_reserve_base = None
    if token_decimals is not None and liquidity.get("base") is not None:
        pool_reserve_base = int(Decimal(str(liquidity["base"])) * (Decimal(10) ** token_decimals))
    pool_reserve_quote = None
    if liquidity.get("quote") is not None:
        # quote side is SOL (9 decimals) for every pair this system trades.
        pool_reserve_quote = int(Decimal(str(liquidity["quote"])) * Decimal(10 ** 9))

    return MarketSnapshot(
        token=token, as_of=as_of, price_usd=Decimal(str(price_raw)),
        liquidity_usd=Decimal(str(liquidity["usd"])),
        fdv_usd=Decimal(str(pair["fdv"])) if pair.get("fdv") is not None else None,
        volume_usd=volume_usd, buys=buys, sells=sells, price_change_pct=price_change_pct,
        pool_reserve_base=pool_reserve_base, pool_reserve_quote=pool_reserve_quote,
    )


def pair_created_at(pair: dict) -> datetime | None:
    ms = pair.get("pairCreatedAt")
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
