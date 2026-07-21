"""The concrete executor: Solana chain, Jupiter aggregator, real wallet.

This is where every earlier design decision in ``execution/`` actually gets
enforced, not just documented:

  * ``TradeMode.LIVE`` still cannot construct this class without a satisfied
    :class:`~mca.execution.base.LiveExecutorGuard` — the check happens before
    ``self._keypair`` is ever assigned (see ``__init__``).
  * ``simulate`` always round-trips a BUY (buy quote, then an immediate sell
    quote on the proceeds) before ``submit`` is ever called, per the abstract
    contract in ``execution/base.py``.
  * Failures are classified into ``RetryableExecutionError`` (network hiccups)
    and ``FatalExecutionError`` (insufficient funds, honeypot, rejected
    transaction) so ``Executor.execute``'s retry loop backs off on the former
    and gives up immediately on the latter.
  * A missing SOL/USD reference price blocks the trade *before* anything is
    submitted — see the note on ``quote_usd_price`` below. Fabricating a Fill
    with a wrong price would silently corrupt every downstream PnL number,
    which is worse than refusing to trade.

Nothing in this file has run against a live endpoint (no network access in the
build sandbox — see the module docstrings in ``solana_rpc.py`` and
``jupiter.py``). Validate on devnet or a trivial mainnet amount first.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from decimal import Decimal

from mca.domain.models import (
    Chain, Dex, Fill, Side, TokenRef, TradeIntent, TradeMode, new_id, utcnow,
)
from mca.execution.base import (
    Executor, ExecutionConfig, ExecutionError, FatalExecutionError,
    LiveExecutorGuard, RetryableExecutionError, ShadowRecord,
)
from mca.execution.jupiter import (
    LAMPORTS_PER_SOL, JupiterClient, JupiterError, JupiterTransportError,
    NoRouteFound, WSOL_MINT,
)
from mca.execution.solana_rpc import (
    RpcResponseError, RpcTransportError, SolanaRpcClient,
)

log = logging.getLogger(__name__)

try:
    from solders.transaction import VersionedTransaction  # type: ignore
except ImportError:
    VersionedTransaction = None  # type: ignore[assignment,misc]

SOLANA_BASE_FEE_LAMPORTS = 5_000     # the fixed signature fee, on top of priority fee
# Round-trip loss beyond which we treat a token as a honeypot rather than a bad
# quote — see the design note on ``simulate`` below for why this is a percentage
# of the *swap size*, not an absolute dollar figure.
HONEYPOT_LOSS_THRESHOLD = 0.60


class MissingReferencePrice(FatalExecutionError):
    """No SOL/USD price has been supplied. Raised before any network call, so
    that "we cannot price this trade" never turns into "we made this trade and
    do not know what it cost." See :attr:`SolanaJupiterExecutor.quote_usd_price`.
    """


@dataclass(slots=True)
class _PreparedSwap:
    quote_lamports_or_base: int
    quote_response: object   # mca.execution.jupiter.QuoteResponse, loosely typed to avoid an import cycle in type hints


class SolanaJupiterExecutor(Executor):
    def __init__(
        self,
        mode: TradeMode,
        cfg: ExecutionConfig,
        rpc: SolanaRpcClient,
        jupiter: JupiterClient,
        keypair,
        *,
        quote_mint: str = WSOL_MINT,
        shadow_record: ShadowRecord | None = None,
        strategy_hash: str = "",
    ) -> None:
        if mode is TradeMode.LIVE:
            # Deliberately the first line of the constructor: no attribute is
            # set, no key is stored, until this passes.
            LiveExecutorGuard.assert_allowed(shadow_record, cfg, strategy_hash)
        super().__init__(Chain.SOLANA, mode, cfg)
        self.rpc = rpc
        self.jupiter = jupiter
        self._keypair = keypair
        self.quote_mint = quote_mint
        self.pubkey = str(keypair.pubkey()) if keypair is not None else None

        # Set every tick by the position manager from live market data. Not a
        # constructor argument because it is genuinely time-varying and the
        # executor is long-lived; see MissingReferencePrice for what happens
        # when a caller forgets to set it.
        self.quote_usd_price: Decimal | None = None

        # Decimals are static on-chain metadata, unlike the price above — safe
        # to cache forever once read, unlike a price that goes stale in seconds.
        self._decimals_cache: dict[str, int] = {}

    async def _get_decimals(self, mint: str) -> int:
        if mint not in self._decimals_cache:
            supply = await self.rpc.get_token_supply(mint)
            self._decimals_cache[mint] = int(supply["decimals"])
        return self._decimals_cache[mint]

    # -- quote: a convenience price check, not on the execute() hot path -----
    async def quote(self, token: TokenRef, side: Side, amount: Decimal) -> tuple[Decimal, int]:
        """Convenience price/slippage estimate. Unit convention (documented
        here because the abstract base method's single ``amount: Decimal``
        parameter cannot express it structurally): for BUY, ``amount`` is SOL
        to spend; for SELL, ``amount`` is base token units, matching
        ``TradeIntent.base_amount``'s convention so a caller quoting ahead of
        building an intent uses the same numbers either way.

        Returns ``price_usd`` per *whole* token — matching
        ``Position.avg_entry_price_usd``'s convention, not per base unit. A
        9-decimal meme coin's base-unit price is ~1e-9 of this number; using
        the wrong one silently wrecks every PnL calculation downstream.
        """
        if self.quote_usd_price is None:
            raise MissingReferencePrice("quote_usd_price not set")
        try:
            decs = Decimal(10) ** await self._get_decimals(token.address)
            if side is Side.BUY:
                lamports = int(amount * LAMPORTS_PER_SOL)
                q = await self.jupiter.quote(self.quote_mint, token.address, lamports, 100)
                whole_tokens = Decimal(q.out_amount) / decs
                price_usd = (amount * self.quote_usd_price) / max(whole_tokens, Decimal("1e-18"))
            else:
                base_units = int(amount)
                q = await self.jupiter.quote(token.address, self.quote_mint, base_units, 100)
                proceeds_sol = Decimal(q.out_amount) / LAMPORTS_PER_SOL
                whole_tokens = amount / decs
                price_usd = (proceeds_sol * self.quote_usd_price) / max(whole_tokens, Decimal("1e-18"))
            return price_usd, q.slippage_bps
        except NoRouteFound as exc:
            raise FatalExecutionError(f"no route for {token.key}: {exc}") from exc
        except JupiterTransportError as exc:
            raise RetryableExecutionError(f"quote failed: {exc}") from exc

    # -- simulate: round-trip, always, per the base class contract -----------
    async def simulate(self, intent: TradeIntent) -> tuple[bool, str]:
        if self.pubkey is None:
            return False, "no wallet loaded"
        try:
            prepared = await self._prepare(intent)
            swap = await self.jupiter.build_swap_transaction(
                prepared.quote_response, self.pubkey, self.cfg)
            sim = await self.rpc.simulate_transaction(swap.swap_transaction_b64, sig_verify=False)
            if not sim.ok:
                return False, f"simulation reverted: {sim.error}"

            if intent.side is Side.BUY:
                # The round-trip check this class exists for: can what we would
                # receive be sold back for a sane fraction of what we spent?
                # A route that quotes fine for buying but implies a near-total
                # loss on the way out is the signature of a honeypot / a tax
                # that only activates on sell — see scoring/gates.py's note on
                # why a third-party "is honeypot" flag is not trusted for this.
                out_amount = prepared.quote_response.out_amount
                sell_quote = await self.jupiter.quote(
                    intent.token.address, self.quote_mint, out_amount, 500)
                spent_lamports = prepared.quote_lamports_or_base
                implied_loss = 1.0 - (sell_quote.out_amount / max(spent_lamports, 1))
                if implied_loss > HONEYPOT_LOSS_THRESHOLD:
                    return False, (f"round-trip implies {implied_loss:.0%} loss on exit "
                                   f"— treating as honeypot, not a bad quote")
            return True, "simulation ok"
        except NoRouteFound as exc:
            return False, f"no route: {exc}"
        except (RpcTransportError, JupiterTransportError) as exc:
            # A transport hiccup during simulation means "unknown", not "unsafe".
            # execute() treats any False here as "do not submit" either way —
            # for entries that is the position manager's cue to reschedule the
            # candidate rather than the risk gate's cue to blacklist the token.
            return False, f"simulation unavailable (transport): {exc}"
        except MissingReferencePrice as exc:
            return False, str(exc)

    # -- submit: sign, send, confirm, record --------------------------------
    async def submit(self, intent: TradeIntent) -> Fill:
        if self.quote_usd_price is None:
            raise MissingReferencePrice(
                "quote_usd_price not set — refusing to trade without a USD "
                "reference price rather than record a Fill we cannot price correctly")
        if self.pubkey is None or self._keypair is None:
            raise FatalExecutionError("no wallet loaded")

        prepared = await self._prepare(intent)

        try:
            swap = await self.jupiter.build_swap_transaction(
                prepared.quote_response, self.pubkey, self.cfg)
        except JupiterTransportError as exc:
            raise RetryableExecutionError(f"swap build failed: {exc}") from exc
        except JupiterError as exc:
            raise FatalExecutionError(f"swap build rejected: {exc}") from exc

        signed_b64 = self._sign(swap.swap_transaction_b64)

        try:
            sig = await self.rpc.send_transaction(signed_b64, skip_preflight=True)
        except RpcTransportError as exc:
            raise RetryableExecutionError(f"send failed: {exc}") from exc
        except RpcResponseError as exc:
            raise FatalExecutionError(f"node rejected transaction: {exc}") from exc

        try:
            status = await self.rpc.confirm_transaction(sig, timeout_s=self.cfg.tx_timeout_s)
        except TimeoutError as exc:
            # We do not know if it landed. execute() will not blindly retry this
            # intent — retrying a BUY whose fate is unknown risks a double spend;
            # the idempotency key in Executor._already_executed guards against a
            # duplicate *intent*, not against us not knowing the outcome of this
            # one. The position manager is expected to reconcile "unknown" fills
            # against on-chain history before deciding to re-attempt.
            raise RetryableExecutionError(f"confirmation timed out for {sig}: {exc}") from exc

        if status.err is not None:
            raise FatalExecutionError(f"transaction {sig} failed on-chain: {status.err}")

        decimals = await self._get_decimals(intent.token.address)
        return self._build_fill(intent, prepared, swap, sig, status.slot, decimals)

    # -- internals ------------------------------------------------------------
    async def _prepare(self, intent: TradeIntent) -> _PreparedSwap:
        """Resolve the Jupiter quote for this intent's actual side/size. Shared
        by ``simulate`` and ``submit`` so they never quote the same intent
        differently — a simulation that priced one route and a submission that
        priced a different one would make the simulation meaningless."""
        try:
            if intent.side is Side.BUY:
                if intent.quote_amount is None:
                    raise FatalExecutionError("BUY intent missing quote_amount")
                lamports = int(intent.quote_amount * LAMPORTS_PER_SOL)
                q = await self.jupiter.quote(
                    self.quote_mint, intent.token.address, lamports, intent.max_slippage_bps)
                return _PreparedSwap(quote_lamports_or_base=lamports, quote_response=q)
            else:
                if intent.base_amount is None:
                    raise FatalExecutionError("SELL intent missing base_amount")
                q = await self.jupiter.quote(
                    intent.token.address, self.quote_mint, intent.base_amount,
                    intent.max_slippage_bps)
                return _PreparedSwap(quote_lamports_or_base=intent.base_amount, quote_response=q)
        except NoRouteFound as exc:
            raise FatalExecutionError(f"no route: {exc}") from exc
        except JupiterTransportError as exc:
            raise RetryableExecutionError(f"quote failed: {exc}") from exc

    def _sign(self, swap_transaction_b64: str) -> str:
        if VersionedTransaction is None:
            raise RuntimeError("solders is not installed: pip install 'mca[chains]'")
        raw = base64.b64decode(swap_transaction_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [self._keypair])
        return base64.b64encode(bytes(signed)).decode()

    def _build_fill(self, intent: TradeIntent, prepared: _PreparedSwap,
                    swap, sig: str, slot: int | None, token_decimals: int) -> Fill:
        """``token_decimals`` must come from on-chain truth (see
        ``_get_decimals``), never assumed — Solana SPL tokens are not all
        9-decimal (USDC-style stablecoins use 6, and nothing stops a mint from
        using any other value). Getting this wrong does not raise an error: it
        silently produces a ``price_usd`` off by a power of ten, which then
        silently corrupts every PnL and stop-loss calculation downstream. This
        was caught while wiring the DB repository layer, which is exactly the
        kind of cross-module unit mismatch that stays invisible until two
        modules that both assume "their" convention is the only one meet.
        """
        q = prepared.quote_response
        price = self.quote_usd_price
        assert price is not None  # guarded at the top of submit()
        decs = Decimal(10) ** token_decimals

        priority_lamports = swap.prioritization_fee_lamports or 0
        total_fee_lamports = SOLANA_BASE_FEE_LAMPORTS + priority_lamports
        gas_usd = (Decimal(total_fee_lamports) / LAMPORTS_PER_SOL) * price

        if intent.side is Side.BUY:
            base_amount = q.out_amount
            quote_amount = intent.quote_amount or Decimal(0)
            quote_usd = quote_amount * price
        else:
            base_amount = intent.base_amount or 0
            quote_amount = Decimal(q.out_amount) / LAMPORTS_PER_SOL
            quote_usd = quote_amount * price

        whole_tokens = Decimal(base_amount) / decs
        price_usd = quote_usd / max(whole_tokens, Decimal("1e-18"))

        return Fill(
            fill_id=new_id(), intent_id=intent.intent_id, token=intent.token,
            side=intent.side, base_amount=base_amount, quote_amount=quote_amount,
            price_usd=price_usd, tx_hash=sig, block_or_slot=slot or 0,
            gas_or_fee_usd=gas_usd, slippage_bps=q.slippage_bps,
            filled_at=utcnow(), dex=Dex.JUPITER,
        )
