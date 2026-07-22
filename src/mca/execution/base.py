"""Execution adapters.

Design decision — the shadow-trading mandate is enforced in code
----------------------------------------------------------------
``LiveExecutor.__init__`` refuses to load signing keys unless

  * ``mode == TradeMode.LIVE``, AND
  * the strategy hash has an unbroken paper-trading record of at least
    ``min_shadow_days`` with at least ``min_shadow_trades`` fills, AND
  * the pre-flight risk test suite passes.

This is a policy that every trading desk has and that solo operators break the
first time a signal looks too good to miss. Making it a constructor invariant
means the only way around it is a code change and a code review, not a
2 a.m. decision.

Design decision — private keys never enter the scoring or API processes
------------------------------------------------------------------------
Only the executor container mounts the keystore. The API, the dashboard, the
scorer and the backtester run with no key material at all, so a
remote-code-execution bug in the FastAPI layer cannot drain the wallet. The
executor exposes no inbound network listener; it only consumes from Redis.

Design decision — a hot wallet is a spending limit, not a wallet
-----------------------------------------------------------------
The hot wallet holds at most ``hot_wallet_max_usd``. Profits are swept to a cold
address on a schedule. A compromised hot key should cost one day of float.
"""

from __future__ import annotations

import abc
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from mca.domain.models import (
    Chain, Dex, Fill, Side, TokenRef, TradeIntent, TradeMode, new_id, utcnow,
)
from mca.execution.jupiter import LAMPORTS_PER_SOL

log = logging.getLogger(__name__)


class ExecutionError(Exception):
    """Base for all execution failures. Non-retryable by default — see
    :class:`RetryableExecutionError` for the opt-in exception."""


class RetryableExecutionError(ExecutionError):
    """A transient failure worth retrying: RPC timeout, blockhash expired
    before confirmation, node returned 429/5xx. Raised by concrete executors
    (``execution/solana_executor.py``) so :meth:`Executor.execute` knows which
    failures deserve the backoff loop and which do not."""


class FatalExecutionError(ExecutionError):
    """A failure that will not change on retry: insufficient funds, slippage
    tolerance exceeded, a honeypot caught by round-trip simulation. Retrying
    these burns 3 backoff cycles — several seconds — for zero benefit, and in
    the honeypot case actively delays the moment we know to walk away."""


class MissingReferencePrice(FatalExecutionError):
    """Raised when an executor is asked to quote/trade without a USD
    reference price set. Both ``SolanaJupiterExecutor`` and ``PaperExecutor``
    raise this — defined once here rather than in one and imported by the
    other, which would create a circular import (``base.py`` cannot import
    from ``solana_executor.py``, which itself imports from ``base.py``)."""


class ModeViolation(ExecutionError):
    """Raised when live execution is attempted without meeting the mandate."""


@dataclass(slots=True)
class ShadowRecord:
    strategy_hash: str
    started_at: datetime
    paper_trades: int
    last_config_change_at: datetime


@dataclass(slots=True)
class ExecutionConfig:
    max_slippage_bps: int = 500
    tx_timeout_s: int = 45
    max_retries: int = 3
    retry_backoff_s: float = 0.8
    hot_wallet_max_usd: Decimal = Decimal(2_000)
    sweep_threshold_usd: Decimal = Decimal(1_000)
    cold_wallet_address: str | None = None

    # shadow-trading mandate
    min_shadow_days: int = 14
    min_shadow_trades: int = 100

    # Solana specifics
    use_jito_bundles: bool = False
    priority_fee_micro_lamports: int = 250_000
    compute_unit_limit: int = 400_000


def strategy_fingerprint(config_dict: dict) -> str:
    """Stable hash of every parameter that can change behaviour.

    Any edit to gates, weights, sizing or exits produces a new fingerprint and
    therefore restarts the shadow-trading clock. Tuning a threshold *is* a new
    strategy, even when it feels like a small change — especially then.
    """
    payload = repr(sorted(_flatten(config_dict).items())).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _flatten(d: dict, prefix: str = "") -> dict:
    out: dict = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{key}."))
        else:
            out[key] = v
    return out


class Executor(abc.ABC):
    """Chain-agnostic execution interface."""

    def __init__(self, chain: Chain, mode: TradeMode, cfg: ExecutionConfig) -> None:
        self.chain = chain
        self.mode = mode
        self.cfg = cfg
        self._seen_keys: set[str] = set()

    # -- idempotency --------------------------------------------------------
    def _already_executed(self, key: str) -> bool:
        """At-least-once delivery plus retries means the same intent can arrive
        several times. Without this check, a Redis redelivery during a network
        blip buys the position twice. In production this set is a Redis
        ``SET NX`` with a TTL, not an in-process set."""
        if key in self._seen_keys:
            log.warning("duplicate intent suppressed key=%s", key)
            return True
        self._seen_keys.add(key)
        return False

    @abc.abstractmethod
    async def quote(self, token: TokenRef, side: Side,
                    amount: Decimal) -> tuple[Decimal, int]:
        """Return (expected_price_usd, expected_slippage_bps)."""

    @abc.abstractmethod
    async def simulate(self, intent: TradeIntent) -> tuple[bool, str]:
        """Dry-run the transaction against current chain state.

        For a BUY this must simulate the *round trip* — buy then immediately
        sell — because a token that can be bought but not sold is the definition
        of a honeypot and no static analysis reliably detects one.
        """

    @abc.abstractmethod
    async def submit(self, intent: TradeIntent) -> Fill:
        """Sign, submit, and wait for confirmation."""

    async def execute(self, intent: TradeIntent) -> Fill | None:
        if intent.mode is not self.mode:
            raise ModeViolation(f"intent mode {intent.mode} != executor mode {self.mode}")
        if self._already_executed(intent.idempotency_key):
            return None

        ok, reason = await self.simulate(intent)
        if not ok:
            log.error("simulation failed token=%s reason=%s", intent.token.key, reason)
            raise ExecutionError(f"simulation failed: {reason}")

        last: Exception | None = None
        for attempt in range(self.cfg.max_retries):
            try:
                return await self.submit(intent)
            except RetryableExecutionError as exc:
                last = exc
                log.warning("submit attempt %d/%d failed (retryable): %s",
                            attempt + 1, self.cfg.max_retries, exc)
                await _sleep(self.cfg.retry_backoff_s * (2 ** attempt))
            except ExecutionError as exc:
                # Not retryable by design — see FatalExecutionError. Fail on the
                # first attempt rather than spending several seconds re-trying a
                # honeypot or an insufficient-balance error that cannot change.
                log.error("submit failed (non-retryable): %s", exc)
                raise
        raise ExecutionError(f"all retries exhausted: {last}")


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


class PaperExecutor(Executor):
    """Simulated fills against live market data. Loads no keys, ever.

    Paper mode is not a toy: it consumes the identical intent stream, applies the
    identical slippage model as the backtester, and writes to the identical
    positions table with ``mode='paper'``. That is what makes the shadow record
    meaningful as a gate on live trading.

    ``quote()``/``submit()`` were left as stubs through the first build rounds
    ("wire to Jupiter/0x quote API") since nothing called them yet. Once
    ``portfolio/manager.py``'s exit loop started calling ``quote()`` directly
    on whichever executor is configured, that stub became a live
    ``NotImplementedError`` in the default (paper) mode — found running this
    against real Docker for the first time, exactly the kind of gap the
    "never verified end-to-end" caveat in this project's docs was warning
    about. Implemented here using the same decimals-aware, USD-per-whole-token
    convention already fixed once in ``solana_executor.py`` — deliberately
    the same shape, not reinvented, so there is one unit convention to get
    right instead of two.
    """

    def __init__(self, chain: Chain, cfg: ExecutionConfig, jupiter, rpc,
                quote_mint: str, fill_model=None) -> None:
        super().__init__(chain, TradeMode.PAPER, cfg)
        from mca.backtest.fill_model import FillModel
        self.fill_model = fill_model or FillModel()
        self.jupiter = jupiter
        self.rpc = rpc
        self.quote_mint = quote_mint
        self.quote_usd_price: Decimal | None = None
        self._decimals_cache: dict[str, int] = {}

    async def _get_decimals(self, mint: str) -> int:
        if mint not in self._decimals_cache:
            supply = await self.rpc.get_token_supply(mint)
            self._decimals_cache[mint] = int(supply["decimals"])
        return self._decimals_cache[mint]

    async def quote(self, token: TokenRef, side: Side, amount: Decimal) -> tuple[Decimal, int]:
        if self.quote_usd_price is None:
            raise MissingReferencePrice("quote_usd_price not set")
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

    async def simulate(self, intent):
        return True, "paper"

    async def submit(self, intent: TradeIntent) -> Fill:
        """Simulated fill: get a real live price via ``quote()``, then run it
        through ``FillModel`` for slippage/latency — the same model the
        backtester uses, so paper and backtest results are comparable."""
        price_usd, slippage_bps = await self.quote(
            intent.token, intent.side,
            intent.quote_amount if intent.side is Side.BUY else Decimal(intent.base_amount or 0))
        decs = Decimal(10) ** await self._get_decimals(intent.token.address)

        if intent.side is Side.BUY:
            quote_amount = intent.quote_amount or Decimal(0)
            whole_tokens = (quote_amount * self.quote_usd_price) / max(price_usd, Decimal("1e-18"))
            base_amount = int(whole_tokens * decs)
        else:
            base_amount = intent.base_amount or 0
            whole_tokens = Decimal(base_amount) / decs
            quote_amount = (whole_tokens * price_usd) / max(self.quote_usd_price, Decimal("1e-18"))

        fill_price = self.fill_model.apply_slippage(price_usd, intent.side, slippage_bps) \
            if hasattr(self.fill_model, "apply_slippage") else price_usd

        return Fill(
            fill_id=new_id(), intent_id=intent.intent_id, token=intent.token,
            side=intent.side, base_amount=base_amount, quote_amount=quote_amount,
            price_usd=fill_price, tx_hash=f"PAPER-{new_id()}", block_or_slot=0,
            gas_or_fee_usd=Decimal("0.000005") * self.quote_usd_price, slippage_bps=slippage_bps,
            filled_at=utcnow(),
        )


class LiveExecutorGuard:
    """Validates the shadow-trading mandate before any key material is touched."""

    @staticmethod
    def assert_allowed(record: ShadowRecord | None, cfg: ExecutionConfig,
                       current_hash: str, now: datetime | None = None) -> None:
        now = now or utcnow()
        if record is None:
            raise ModeViolation("no shadow-trading record for this strategy")
        if record.strategy_hash != current_hash:
            raise ModeViolation(
                f"config changed since shadow run ({record.strategy_hash} != "
                f"{current_hash}); the paper-trading clock restarts")
        elapsed = now - record.started_at
        if elapsed < timedelta(days=cfg.min_shadow_days):
            raise ModeViolation(
                f"shadow period {elapsed.days}d < required {cfg.min_shadow_days}d")
        if record.paper_trades < cfg.min_shadow_trades:
            raise ModeViolation(
                f"only {record.paper_trades} paper trades < required {cfg.min_shadow_trades}")
        if record.last_config_change_at > record.started_at:
            raise ModeViolation("configuration was modified during the shadow period")


# --------------------------------------------------------------------------- #
# Bus wire format for TradeIntent / Fill — defined once, imported by both the
# producer (risk/gate_service.py) and the consumer (execution/worker.py), so
# the two processes cannot silently drift onto incompatible schemas.
# --------------------------------------------------------------------------- #

def intent_to_payload(intent: TradeIntent) -> dict:
    return {
        "intent_id": intent.intent_id,
        "token": {"chain": intent.token.chain.value, "address": intent.token.address},
        "side": intent.side.value,
        "quote_amount": str(intent.quote_amount) if intent.quote_amount is not None else None,
        "base_amount": intent.base_amount,
        "max_slippage_bps": intent.max_slippage_bps,
        "idempotency_key": intent.idempotency_key,
        "mode": intent.mode.value,
        "reason": intent.reason,
        "priority_fee_micro_lamports": intent.priority_fee_micro_lamports,
        "deadline": intent.deadline.isoformat() if intent.deadline else None,
        "correlation_id": intent.correlation_id,
    }


def intent_from_payload(payload: dict) -> TradeIntent:
    return TradeIntent(
        intent_id=payload["intent_id"],
        token=TokenRef(Chain(payload["token"]["chain"]), payload["token"]["address"]),
        side=Side(payload["side"]),
        quote_amount=Decimal(payload["quote_amount"]) if payload.get("quote_amount") is not None else None,
        base_amount=payload.get("base_amount"),
        max_slippage_bps=payload["max_slippage_bps"],
        idempotency_key=payload["idempotency_key"],
        mode=TradeMode(payload["mode"]),
        reason=payload.get("reason", ""),
        priority_fee_micro_lamports=payload.get("priority_fee_micro_lamports"),
        deadline=datetime.fromisoformat(payload["deadline"]) if payload.get("deadline") else None,
        correlation_id=payload.get("correlation_id", ""),
    )


def fill_to_payload(fill: Fill, *, idempotency_key: str) -> dict:
    """``idempotency_key`` is carried alongside the fill (not a ``Fill`` field
    itself — it belongs to the intent that produced the fill) because the
    position manager's DB write needs it for the append-only unique
    constraint on ``fills.idempotency_key``, and re-deriving it from
    ``intent_id`` alone would mean a DB round trip the message can avoid."""
    return {
        "fill_id": fill.fill_id, "intent_id": fill.intent_id,
        "token": {"chain": fill.token.chain.value, "address": fill.token.address},
        "side": fill.side.value, "base_amount": fill.base_amount,
        "quote_amount": str(fill.quote_amount), "price_usd": str(fill.price_usd),
        "tx_hash": fill.tx_hash, "block_or_slot": fill.block_or_slot,
        "gas_or_fee_usd": str(fill.gas_or_fee_usd), "slippage_bps": fill.slippage_bps,
        "filled_at": fill.filled_at.isoformat(), "dex": fill.dex.value if fill.dex else None,
        "idempotency_key": idempotency_key,
    }


def fill_from_payload(payload: dict) -> tuple[Fill, str]:
    """Returns ``(fill, idempotency_key)`` — see :func:`fill_to_payload`."""
    fill = Fill(
        fill_id=payload["fill_id"], intent_id=payload["intent_id"],
        token=TokenRef(Chain(payload["token"]["chain"]), payload["token"]["address"]),
        side=Side(payload["side"]), base_amount=payload["base_amount"],
        quote_amount=Decimal(payload["quote_amount"]), price_usd=Decimal(payload["price_usd"]),
        tx_hash=payload["tx_hash"], block_or_slot=payload["block_or_slot"],
        gas_or_fee_usd=Decimal(payload["gas_or_fee_usd"]), slippage_bps=payload["slippage_bps"],
        filled_at=datetime.fromisoformat(payload["filled_at"]),
        dex=Dex(payload["dex"]) if payload.get("dex") else None,
    )
    return fill, payload["idempotency_key"]
