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

# Verified 2026-07-22 against docs.raydium.io/reference/program-addresses
# (the doc's own table re-confirms RAYDIUM_AMM_V4 above verbatim, cross-
# checking the source itself). See module docstring.
RAYDIUM_CPMM: str | None = "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C"
RAYDIUM_CLMM: str | None = "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK"

# Verified 2026-07-22 against two independent sources (pump-fun/pump-public-docs
# and the third-party pumpdotfun-sdk). Same address on mainnet and devnet.
PUMPFUN_PROGRAM: str | None = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

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
#
# Keyed by PROGRAM ID, not Dex: AMM v4, CPMM and CLMM all carry the Dex.RAYDIUM
# label (same venue, for scoring/display purposes) but are three unrelated
# programs with three unrelated instruction sets — a single Dex-keyed marker
# set cannot distinguish them. CPMM/CLMM/pump.fun markers below are each
# program's Anchor-generated "Instruction: <Name>" log line, which is exact
# (not a substring guess) because Anchor emits it verbatim for every
# instruction dispatch — see anchor-lang's `declare_id!`/`#[program]` macro
# expansion. AMM v4 predates Anchor and uses its own log format, left as-is.
_INIT_LOG_MARKERS: dict[str, tuple[str, ...]] = {
    RAYDIUM_AMM_V4: ("initialize2", "init_pc_amount", "InitializeInstruction2"),
    RAYDIUM_CPMM: ("Instruction: Initialize",) if RAYDIUM_CPMM else (),
    RAYDIUM_CLMM: ("Instruction: CreatePool",) if RAYDIUM_CLMM else (),
    PUMPFUN_PROGRAM: ("Instruction: Create", "Instruction: CreateV2") if PUMPFUN_PROGRAM else (),
}

# Raydium AMM v4 ``initialize2``: 0-indexed positions in the instruction's
# resolved account list. UNVERIFIED this session — see module docstring.
_RAYDIUM_INIT2_ACCOUNTS = {
    "amm": 4, "coin_mint": 8, "pc_mint": 9,
    "coin_vault": 10, "pc_vault": 11, "lp_mint": 7,
}

# Raydium CP-Swap (CPMM) ``Initialize``: 0-indexed account positions.
# Verified 2026-07-22 directly against raydium-io/raydium-cp-swap
# programs/cp-swap/src/instructions/initialize.rs (#[derive(Accounts)] field
# order = on-chain account index order for Anchor instructions).
_RAYDIUM_CPMM_ACCOUNTS = {
    "creator": 0, "pool_state": 3, "token_0_mint": 4, "token_1_mint": 5,
    "token_0_vault": 10, "token_1_vault": 11,
}

# Raydium CLMM ``CreatePool``: 0-indexed account positions.
# Verified 2026-07-22 directly against raydium-io/raydium-clmm
# programs/amm/src/instructions/create_pool.rs.
_RAYDIUM_CLMM_ACCOUNTS = {
    "pool_creator": 0, "pool_state": 2, "token_mint_0": 3, "token_mint_1": 4,
    "token_vault_0": 5, "token_vault_1": 6,
}

# pump.fun ``create`` (legacy): 0-indexed account positions. Verified
# 2026-07-22 against two independent sources — pump-fun/pump-public-docs
# (PUMP_PROGRAM_README.md) and a third-party discriminator/account analysis
# (allenhark.com/blog/pumpfun-create-instruction-discriminator); both agree
# on mint=0, bonding_curve=1, associated_bonding_curve=2, user=6.
#
# Live traffic observed this session (2026-07-22, ~70s / ~18k pump.fun
# messages) contained ZERO ``Create`` instructions and dozens of
# ``CreateV2`` — pump.fun shipped a "cashback" upgrade around 2026-11
# (per search results referencing a 2026-04-28 fee-recipient change and a
# November Token-2022 migration) that moved new launches onto ``create_v2``.
# Docs describe both instructions as still coexisting, so this stays wired
# up for the rare legacy path, but CreateV2 below is what actually fires.
_PUMPFUN_CREATE_ACCOUNTS = {
    "mint": 0, "bonding_curve": 1, "associated_bonding_curve": 2, "user": 6,
}

# pump.fun ``create_v2``: 0-indexed account positions. Verified 2026-07-22
# directly against the official IDL — pump-fun/pump-public-docs
# idl/pump.json AND idl/pump.ts (two independent files in the same primary
# source, both agree exactly). Uses Token-2022 (not the legacy Token
# program) for the mint and associated_bonding_curve as of this upgrade.
_PUMPFUN_CREATE_V2_ACCOUNTS = {
    "mint": 0, "bonding_curve": 2, "associated_bonding_curve": 3, "user": 5,
}


def _anchor_instruction_name(log_lines: list[str], program_id: str) -> str | None:
    """Returns the bare instruction name (e.g. ``"CreateV2"``, not
    ``"Instruction: CreateV2"``) from the "Instruction: <Name>" log line
    immediately following ``program_id``'s own ``invoke`` line, or ``None``.

    Anchor emits this as the very first thing an instruction handler logs,
    before any further CPI — the same technique used to observe the real
    Buy/Sell/CreateV2 lines live this session. Scoping to program_id's own
    invoke frame (rather than searching the whole flat ``logs``/
    ``logMessages`` array, which is shared across every program touched by
    the transaction) is required: a composite transaction that also invokes
    an unrelated program sharing an instruction name — observed live this
    session as a "moon"-prefixed account appearing as the "pool" for three
    different mints, and once for two *different* mints, both impossible for
    a real per-mint PDA — would otherwise false-match on that OTHER
    program's "Instruction: Create"/"CreateV2" line and this checker would
    resolve accounts against entirely the wrong program's account list.
    """
    marker = f"Program {program_id} invoke"
    prefix = "Program log: Instruction: "
    for i, line in enumerate(log_lines):
        if line.startswith(marker) and i + 1 < len(log_lines):
            nxt = log_lines[i + 1]
            if nxt.startswith(prefix):
                return nxt.removeprefix(prefix)
    return None


def looks_like_pool_creation(log_lines: list[str], program_id: str) -> bool:
    """Stage-one filter. Pure and cheap — called on every notification.

    Markers starting with "Instruction: " are Anchor's per-instruction log
    line and are matched via :func:`_anchor_instruction_name`, scoped to
    program_id's own invoke frame — see that function's docstring for why a
    blind substring/endswith search across the whole log array is unsafe.
    AMM v4's markers predate Anchor, have no such same-prefix-or-same-name
    sibling-instruction collision risk observed, and keep the original
    substring check (their trailing text after the marker varies, e.g. "{}").
    """
    markers = _INIT_LOG_MARKERS.get(program_id, ())
    if not markers:
        return False
    anchor_markers = [m for m in markers if m.startswith("Instruction: ")]
    if anchor_markers:
        name = _anchor_instruction_name(log_lines, program_id)
        if name is not None and any(name == m.removeprefix("Instruction: ") for m in anchor_markers):
            return True
    other_markers = [m for m in markers if not m.startswith("Instruction: ")]
    if other_markers:
        joined = "\n".join(log_lines)
        if any(m in joined for m in other_markers):
            return True
    return False


_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58decode(s: str) -> bytes:
    """Minimal base58 decoder (Bitcoin/Solana alphabet), no external
    dependency. Used only to read the first 8 bytes of an instruction's raw
    ``data`` — the Anchor discriminator — never for anything security-
    sensitive like signing."""
    num = 0
    for char in s:
        num = num * 58 + _B58_ALPHABET.index(char)
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    n_pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * n_pad + body


# Anchor's instruction discriminator is the first 8 bytes of
# sha256(f"global:{instruction_name}"), where instruction_name is the
# lowercase-with-underscores name from the program's own IDL — not a guess,
# it's how the Anchor framework itself routes instruction data. Verified
# 2026-07-22 by decoding a real, currently-live pump.fun transaction's raw
# instruction data and confirming the first 8 bytes match exactly (and cross-
# checked against allenhark.com/blog/pumpfun-create-instruction-discriminator
# for the legacy "create" value independently).
_PUMPFUN_CREATE_DISCRIMINATOR = bytes([24, 30, 200, 40, 5, 28, 7, 119])
_PUMPFUN_CREATE_V2_DISCRIMINATOR = bytes([214, 144, 76, 236, 95, 139, 49, 180])


def _instruction_accounts(message: dict, program_id: str,
                          discriminator: bytes | None = None) -> list[str] | None:
    """Returns the resolved account-pubkey list for the specific top-level
    instruction in ``message["instructions"]`` whose ``programId`` matches
    (and, if given, whose raw ``data`` starts with ``discriminator``).

    This is the fix for a bug that survived two earlier attempts this
    session: every DEX branch used to read accounts by *fixed position from
    the transaction-wide* ``message["accountKeys"]``, which only happens to
    line up with a single instruction's own IDL-declared account order when
    that instruction is the *only* one in the transaction. Real transactions
    routinely bundle a ComputeBudget instruction (Solana priority fees are
    near-universal) and, for pump.fun specifically, an SPL
    "CreateIdempotent" (auto-creating the buyer's ATA) ahead of the actual
    create/trade call — Solana's account-list ordering is a *transaction-
    wide* dedup by permission class, not a concatenation of each
    instruction's own account order, so reading fixed top-level positions
    silently pulls in accounts belonging to a DIFFERENT instruction. Live
    evidence: 22 of 72 detected mints had more than one "bonding curve"
    address, and some "bonding curve" addresses were shared across
    *different* mints — both impossible for a real per-mint PDA. jsonParsed
    encoding (see ``execution/solana_rpc.py``) resolves each instruction's
    OWN ``accounts`` to real pubkeys even for a program it can't decode the
    *data* of, which is what this reads instead.

    ``discriminator`` disambiguates when one program id can mean more than
    one instruction (pump.fun's create vs create_v2 vs buy vs sell, all
    sharing a program id — verified this session by decoding real
    instruction data, see the constants above). Without it, the first
    matching instruction wins, which is what the Raydium branches below use
    — CPMM/CLMM/AMM v4 discriminators are NOT independently verified this
    session (unlike pump.fun's), so this checker does not attempt to
    disambiguate between their instructions by data; it relies on the
    stage-one log marker already having filtered for a create-shaped
    transaction before getting here.
    """
    for ix in message.get("instructions", []):
        if ix.get("programId") != program_id:
            continue
        if discriminator is not None:
            data = ix.get("data")
            if not data:
                continue
            try:
                if _b58decode(data)[:8] != discriminator:
                    continue
            except (ValueError, IndexError):
                continue
        accounts = ix.get("accounts")
        if accounts is None:
            continue
        return [a["pubkey"] if isinstance(a, dict) else a for a in accounts]
    return None


def _sol_quoted_split(pool_addr: str | None, mint_a: str | None, mint_b: str | None,
                      vault_a: str | None, vault_b: str | None,
                      signature: str) -> tuple[str, str, str] | None:
    """Shared by the three Raydium-family extractors: pick the non-SOL mint as
    base, the wSOL mint as quote, and that base mint's vault. Returns
    ``(base_mint, quote_mint, base_vault)`` or ``None`` to drop the candidate
    (fail closed — see module docstring)."""
    from mca.execution.jupiter import WSOL_MINT
    if mint_a == WSOL_MINT:
        return mint_b, mint_a, vault_b
    if mint_b == WSOL_MINT:
        return mint_a, mint_b, vault_a
    log.debug("pool %s (sig=%s) is not SOL-quoted, skipping", pool_addr, signature)
    return None


def extract_pool_from_transaction(tx: dict, program_id: str, dex: Dex, signature: str,
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

        def _pubkey_at(account_keys: list[str], idx: int) -> str | None:
            if idx >= len(account_keys):
                return None
            entry = account_keys[idx]
            return entry["pubkey"] if isinstance(entry, dict) else entry

        def _event(base_mint, quote_mint, pool_addr, deployer, base_vault) -> PoolCreatedEvent | None:
            if not (base_mint and quote_mint and pool_addr):
                log.warning("%s candidate %s did not resolve expected accounts — "
                           "dropping rather than guessing", dex.value, signature)
                return None
            return PoolCreatedEvent(
                token=TokenRef(chain=Chain.SOLANA, address=base_mint),
                quote_token=TokenRef(chain=Chain.SOLANA, address=quote_mint),
                dex=dex, pool_address=pool_addr,
                detected_at=detected_at or datetime.now(timezone.utc),
                tx_signature=signature, block_or_slot=tx.get("slot", 0),
                deployer=deployer, base_vault=base_vault,
            )

        if program_id == RAYDIUM_AMM_V4:
            # No verified discriminator for this legacy (pre-Anchor) program
            # this session — see module docstring. Picks the first
            # RAYDIUM_AMM_V4 instruction in the transaction; the stage-one
            # log-marker check already filtered for an initialize2-shaped
            # transaction before this runs.
            account_keys = _instruction_accounts(message, program_id)
            if account_keys is None:
                log.debug("no %s instruction found in confirmed tx %s", program_id, signature)
                return None
            idx = _RAYDIUM_INIT2_ACCOUNTS
            pool_addr = _pubkey_at(account_keys, idx["amm"])
            coin_mint = _pubkey_at(account_keys, idx["coin_mint"])
            pc_mint = _pubkey_at(account_keys, idx["pc_mint"])
            if not (pool_addr and coin_mint and pc_mint):
                log.warning("raydium init2 candidate %s did not resolve expected "
                           "accounts — dropping rather than guessing", signature)
                return None
            split = _sol_quoted_split(
                pool_addr, coin_mint, pc_mint,
                _pubkey_at(account_keys, idx["coin_vault"]),
                _pubkey_at(account_keys, idx["pc_vault"]), signature)
            if split is None:
                return None
            base_mint, quote_mint, base_vault = split
            return _event(base_mint, quote_mint, pool_addr, _pubkey_at(account_keys, 0), base_vault)

        if program_id == RAYDIUM_CPMM:
            # No verified discriminator this session (unlike pump.fun's,
            # which was decoded and confirmed against a real live
            # transaction) — see module docstring. First-instruction-wins,
            # same caveat as AMM v4 above.
            account_keys = _instruction_accounts(message, program_id)
            if account_keys is None:
                log.debug("no %s instruction found in confirmed tx %s", program_id, signature)
                return None
            idx = _RAYDIUM_CPMM_ACCOUNTS
            pool_addr = _pubkey_at(account_keys, idx["pool_state"])
            mint_0 = _pubkey_at(account_keys, idx["token_0_mint"])
            mint_1 = _pubkey_at(account_keys, idx["token_1_mint"])
            if not (pool_addr and mint_0 and mint_1):
                log.warning("raydium cpmm candidate %s did not resolve expected "
                           "accounts — dropping rather than guessing", signature)
                return None
            split = _sol_quoted_split(
                pool_addr, mint_0, mint_1,
                _pubkey_at(account_keys, idx["token_0_vault"]),
                _pubkey_at(account_keys, idx["token_1_vault"]), signature)
            if split is None:
                return None
            base_mint, quote_mint, base_vault = split
            return _event(base_mint, quote_mint, pool_addr,
                         _pubkey_at(account_keys, idx["creator"]), base_vault)

        if program_id == RAYDIUM_CLMM:
            # No verified discriminator this session — see module docstring.
            account_keys = _instruction_accounts(message, program_id)
            if account_keys is None:
                log.debug("no %s instruction found in confirmed tx %s", program_id, signature)
                return None
            idx = _RAYDIUM_CLMM_ACCOUNTS
            pool_addr = _pubkey_at(account_keys, idx["pool_state"])
            mint_0 = _pubkey_at(account_keys, idx["token_mint_0"])
            mint_1 = _pubkey_at(account_keys, idx["token_mint_1"])
            if not (pool_addr and mint_0 and mint_1):
                log.warning("raydium clmm candidate %s did not resolve expected "
                           "accounts — dropping rather than guessing", signature)
                return None
            split = _sol_quoted_split(
                pool_addr, mint_0, mint_1,
                _pubkey_at(account_keys, idx["token_vault_0"]),
                _pubkey_at(account_keys, idx["token_vault_1"]), signature)
            if split is None:
                return None
            base_mint, quote_mint, base_vault = split
            return _event(base_mint, quote_mint, pool_addr,
                         _pubkey_at(account_keys, idx["pool_creator"]), base_vault)

        if program_id == PUMPFUN_PROGRAM:
            # No mint choice to make: a bonding curve is always SOL-paired by
            # construction. "Quote" is represented as WSOL_MINT for consistency
            # with the rest of the codebase even though pump.fun holds native
            # lamports, not a wrapped-SOL token account.
            #
            # "Create" and "CreateV2" share this program id but have
            # different account layouts (see _PUMPFUN_CREATE_ACCOUNTS vs
            # _PUMPFUN_CREATE_V2_ACCOUNTS) — disambiguated by the verified,
            # live-confirmed Anchor discriminator (see the constants above),
            # not by log-line order, which proved unreliable when a
            # transaction invokes pump.fun more than once (e.g. create then
            # immediately buy).
            from mca.execution.jupiter import WSOL_MINT
            account_keys = _instruction_accounts(message, program_id, _PUMPFUN_CREATE_V2_DISCRIMINATOR)
            idx = _PUMPFUN_CREATE_V2_ACCOUNTS
            if account_keys is None:
                account_keys = _instruction_accounts(message, program_id, _PUMPFUN_CREATE_DISCRIMINATOR)
                idx = _PUMPFUN_CREATE_ACCOUNTS
            if account_keys is None:
                log.debug("pump.fun candidate %s matched stage-one but no "
                         "create/create_v2 instruction (by discriminator) found "
                         "in the confirmed tx — dropping rather than guessing "
                         "which layout applies", signature)
                return None
            return _event(
                base_mint=_pubkey_at(account_keys, idx["mint"]), quote_mint=WSOL_MINT,
                pool_addr=_pubkey_at(account_keys, idx["bonding_curve"]),
                deployer=_pubkey_at(account_keys, idx["user"]),
                base_vault=_pubkey_at(account_keys, idx["associated_bonding_curve"]),
            )

        log.debug("no extractor implemented for program %s (dex=%s), skipping %s",
                 program_id, dex.value, signature)
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
            if not looks_like_pool_creation(notification["logs"], self.program_id):
                continue
            try:
                await self._handle_candidate(notification)
            except Exception:
                log.exception("failed to process pool-creation candidate sig=%s",
                             notification.get("signature"))

    async def _handle_candidate(self, notification: dict) -> None:
        sig = notification["signature"]
        tx = await self.rpc.get_transaction(sig)
        event = extract_pool_from_transaction(tx, self.program_id, self.dex, sig)
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
        "base_vault": event.base_vault,
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
        deployer=payload.get("deployer"), base_vault=payload.get("base_vault"),
        correlation_id=payload["correlation_id"],
    )
