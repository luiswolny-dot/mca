"""On-chain security checks — no third-party API, straight from the RPC node.

Design decision — trust the node's own parser, not a hand-rolled one
------------------------------------------------------------------------
``mint_authority_revoked`` / ``freeze_authority_revoked`` come from
``getAccountInfo`` with ``jsonParsed`` encoding (see
``execution/solana_rpc.py``), which delegates the SPL Token / Token-2022
binary layout to the RPC node's own account decoder rather than a fixed-offset
parser written here and never tested against a live account. If the owning
program is neither the classic Token program nor Token-2022 — or the node
cannot parse it — this returns ``None`` (unknown), which fails the hard gate
that depends on it. That is the same "None means unknown, not safe" rule
``domain/models.py`` states for ``SecurityReport`` generally.

Design decision — exclude the pool's own vault from holder concentration
-------------------------------------------------------------------------
Immediately after a pool is created, the single largest holder of the base
token is almost always the pool's own vault account — that is what a
liquidity pool *is*. Computing "top holder %" without excluding it would
report ~90%+ concentration on every single fresh launch, making the whale
gate fail closed on everything and effectively disabling entry — a
different, quieter failure mode than the one it exists to catch. See
``PoolCreatedEvent.base_vault``, threaded through from ``ingest/solana_pools.py``
specifically so this exclusion is possible.
"""

from __future__ import annotations

import logging
from datetime import datetime

from mca.domain.models import SecurityReport, TokenRef, utcnow
from mca.execution.solana_rpc import SolanaRpcClient

log = logging.getLogger(__name__)

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


def parse_mint_authorities(account_info: dict | None) -> tuple[bool | None, bool | None, int | None]:
    """Returns ``(mint_authority_revoked, freeze_authority_revoked, decimals)``.

    Pure function over the ``getAccountInfo`` jsonParsed response — the piece
    that actually carries correctness risk, isolated from the RPC call itself
    so it is testable with a fixture dict."""
    if account_info is None:
        return None, None, None
    owner = account_info.get("owner")
    if owner not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        return None, None, None
    parsed = account_info.get("data", {}).get("parsed")
    if not parsed or parsed.get("type") != "mint":
        return None, None, None
    info = parsed.get("info", {})
    mint_revoked = info.get("mintAuthority") is None
    freeze_revoked = info.get("freezeAuthority") is None
    decimals = info.get("decimals")
    return mint_revoked, freeze_revoked, decimals


def parse_supply(account_info: dict | None) -> int | None:
    """Returns the mint's total supply, or ``None`` if unknown.

    Pure function, same shape and same "unknown, not unsafe" contract as
    :func:`parse_mint_authorities`. ``data`` is only a dict (with a
    ``"parsed"`` key) when the RPC node's jsonParsed decoder recognised the
    account's owner as a program it knows how to parse (SPL Token /
    Token-2022); for any other owner it falls back to the raw
    ``["<base64>", "base64"]`` tuple form, which ``isinstance`` here rejects
    rather than crashing on — this happens for any address that is not
    actually a mint, e.g. one from a bad or malicious extraction upstream.
    """
    if account_info is None:
        return None
    data = account_info.get("data")
    parsed = data.get("parsed") if isinstance(data, dict) else None
    info = (parsed or {}).get("info", {})
    raw_supply = info.get("supply")
    return int(raw_supply) if raw_supply is not None else None


def compute_top_holder_pcts(largest_accounts: list[dict], supply: int,
                            exclude_addresses: set[str]) -> tuple[float | None, float | None]:
    """Returns ``(top1_holder_pct, top10_holder_pct)`` excluding the pool's own
    vault(s) and any other address the caller knows is not a real holder."""
    if supply <= 0:
        return None, None
    amounts = [int(a["amount"]) for a in largest_accounts if a.get("address") not in exclude_addresses]
    if not amounts:
        return None, None
    amounts.sort(reverse=True)
    top1 = amounts[0] / supply
    top10 = sum(amounts[:10]) / supply
    return min(top1, 1.0), min(top10, 1.0)


class OnChainSecurityChecker:
    def __init__(self, rpc: SolanaRpcClient) -> None:
        self.rpc = rpc

    async def check(self, token: TokenRef, *, deployer: str | None,
                    created_at: datetime | None, exclude_addresses: set[str] | None = None
                    ) -> SecurityReport:
        exclude_addresses = exclude_addresses or set()
        account_info = None
        largest = []
        try:
            account_info = await self.rpc.get_account_info_parsed(token.address)
        except Exception as exc:
            log.warning("getAccountInfo failed for %s: %s", token.key, exc)
        try:
            largest = await self.rpc.get_token_largest_accounts(token.address)
        except Exception as exc:
            log.warning("getTokenLargestAccounts failed for %s: %s", token.key, exc)

        mint_revoked, freeze_revoked, decimals = parse_mint_authorities(account_info)
        supply = parse_supply(account_info)

        top1, top10 = (None, None)
        if supply is not None:
            top1, top10 = compute_top_holder_pcts(largest, supply, exclude_addresses)

        return SecurityReport(
            token=token, checked_at=utcnow(),
            mint_authority_revoked=mint_revoked, freeze_authority_revoked=freeze_revoked,
            top1_holder_pct=top1, top10_holder_pct=top10,
            deployer=deployer, created_at=created_at,
            sources={"onchain_rpc": True},
        )
