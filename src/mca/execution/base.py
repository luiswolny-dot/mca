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
    Chain, Dex, Fill, Side, TokenRef, TradeIntent, TradeMode, utcnow,
)

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
    """

    def __init__(self, chain: Chain, cfg: ExecutionConfig, fill_model=None) -> None:
        super().__init__(chain, TradeMode.PAPER, cfg)
        from mca.backtest.fill_model import FillModel
        self.fill_model = fill_model or FillModel()

    async def quote(self, token, side, amount):
        raise NotImplementedError("wire to Jupiter/0x quote API")

    async def simulate(self, intent):
        return True, "paper"

    async def submit(self, intent: TradeIntent) -> Fill:
        raise NotImplementedError(
            "wire to the live price feed, then delegate to FillModel.simulate()")


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
