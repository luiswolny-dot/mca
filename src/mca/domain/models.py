"""Canonical domain types.

Design decision: plain ``dataclasses`` with ``slots=True`` instead of Pydantic.

The hot path (ingest -> enrich -> score -> risk gate) allocates one object per
token event, thousands per minute. Pydantic v2 validation costs ~1-3us per model
and, more importantly, drags a C-extension into every worker image. Validation
belongs at the *system boundary* (HTTP payloads, config files), not in the inner
loop. Pydantic is therefore used only in ``mca.api.schemas`` and ``mca.core.config``.

Every monetary value is a ``Decimal`` at rest and a ``float`` only inside numeric
kernels. Token amounts are integers in base units (lamports / wei) because
float rounding on a 9- or 18-decimal integer silently loses dust and produces
"insufficient funds" errors at submit time.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any


def utcnow() -> datetime:
    """Timezone-aware UTC now. Never use ``datetime.utcnow()`` — it is naive."""
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Chain(str, Enum):
    SOLANA = "solana"
    ETHEREUM = "ethereum"
    BASE = "base"
    BSC = "bsc"

    @property
    def is_evm(self) -> bool:
        return self is not Chain.SOLANA

    @property
    def native_decimals(self) -> int:
        return 9 if self is Chain.SOLANA else 18


class Dex(str, Enum):
    RAYDIUM = "raydium"
    JUPITER = "jupiter"          # aggregator, not a venue
    ORCA = "orca"
    METEORA = "meteora"
    PUMPSWAP = "pumpswap"
    UNISWAP_V2 = "uniswap_v2"
    UNISWAP_V3 = "uniswap_v3"
    UNISWAP_V4 = "uniswap_v4"
    AERODROME = "aerodrome"
    PANCAKESWAP_V2 = "pancakeswap_v2"
    PANCAKESWAP_V3 = "pancakeswap_v3"


class TradeMode(str, Enum):
    """Execution realism level. Enforced in code, not by convention."""

    BACKTEST = "backtest"    # replay of historical events
    PAPER = "paper"          # live data, simulated fills, no keys loaded
    LIVE = "live"            # real funds


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class PositionStatus(str, Enum):
    PENDING_ENTRY = "pending_entry"
    OPEN = "open"
    SCALING_OUT = "scaling_out"
    PENDING_EXIT = "pending_exit"
    CLOSED = "closed"
    FAILED = "failed"          # entry never confirmed
    STUCK = "stuck"            # cannot sell (honeypot discovered post-entry)


class ExitReason(str, Enum):
    TAKE_PROFIT = "take_profit"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"
    BREAK_EVEN = "break_even"
    TIME_STOP = "time_stop"
    MOMENTUM_FADE = "momentum_fade"
    LIQUIDITY_DRAIN = "liquidity_drain"
    WHALE_EXIT = "whale_exit"
    SOCIAL_FADE = "social_fade"
    CIRCUIT_BREAKER = "circuit_breaker"
    MANUAL = "manual"
    MAX_DRAWDOWN = "max_drawdown"


class RiskState(str, Enum):
    """Global kill-switch state. Persisted, survives restarts."""

    NORMAL = "normal"
    DERISK = "derisk"      # no new entries, existing positions managed
    HALT = "halt"          # no new entries, flatten everything
    LOCKED = "locked"      # manual unlock required (loss limit tripped)


# --------------------------------------------------------------------------- #
# Token identity & security
# --------------------------------------------------------------------------- #


@dataclass(slots=True, frozen=True)
class TokenRef:
    """Stable identity of a token across all subsystems."""

    chain: Chain
    address: str            # mint (Solana) or contract address (EVM), checksummed/base58

    def __str__(self) -> str:
        return f"{self.chain.value}:{self.address}"

    @property
    def key(self) -> str:
        return str(self)


@dataclass(slots=True)
class SecurityReport:
    """Output of the enrichment stage. ``None`` means *unknown*, not *safe*.

    The distinction matters: an unknown value must fail a hard gate. Treating
    ``None`` as ``False`` is how bots buy honeypots.
    """

    token: TokenRef
    checked_at: datetime = field(default_factory=utcnow)

    # --- authorities (Solana) / ownership (EVM) ---
    mint_authority_revoked: bool | None = None
    freeze_authority_revoked: bool | None = None
    contract_renounced: bool | None = None
    is_proxy_upgradeable: bool | None = None
    has_blacklist_function: bool | None = None
    has_mint_function: bool | None = None

    # --- liquidity ---
    liquidity_usd: Decimal | None = None
    lp_locked_pct: float | None = None       # 0..1 of LP supply locked or burned
    lp_burned: bool | None = None
    lp_lock_expiry: datetime | None = None

    # --- taxes / transferability (from simulation, not from source parsing) ---
    buy_tax_bps: int | None = None
    sell_tax_bps: int | None = None
    transfer_pausable: bool | None = None
    simulated_sell_ok: bool | None = None    # eth_call / simulateTransaction round trip

    # --- distribution ---
    holder_count: int | None = None
    top1_holder_pct: float | None = None
    top10_holder_pct: float | None = None
    dev_holding_pct: float | None = None
    insider_cluster_pct: float | None = None  # wallets funded by the same source
    bundled_supply_pct: float | None = None   # bought in the deploy bundle

    # --- provenance ---
    deployer: str | None = None
    deployer_prior_rugs: int | None = None
    created_at: datetime | None = None

    sources: dict[str, Any] = field(default_factory=dict)

    def age_seconds(self, as_of: datetime | None = None) -> float | None:
        """Token age relative to ``as_of``, NOT to wall-clock time.

        This signature is deliberate. An earlier version read ``utcnow()``
        internally, which is correct live and silently wrong in the backtester:
        replaying a 2026-03 event in 2026-07 reported the token as four months
        old, so the ``max_age`` gate passed everything and the ``min_age`` gate
        rejected nothing. Every time-derived feature must take its reference
        time as an argument — that is the only way point-in-time correctness is
        enforceable rather than aspirational.
        """
        if self.created_at is None:
            return None
        return ((as_of or utcnow()) - self.created_at).total_seconds()


# --------------------------------------------------------------------------- #
# Market snapshot & features
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MarketSnapshot:
    """Point-in-time market state. ``as_of`` is load-bearing: the backtester and
    the live scorer consume the *same* struct, so any feature computed from a
    field observed after ``as_of`` is look-ahead bias."""

    token: TokenRef
    as_of: datetime
    price_usd: Decimal
    liquidity_usd: Decimal
    fdv_usd: Decimal | None = None

    # rolling windows, keys are seconds: 60, 300, 900, 3600, 21600, 86400
    volume_usd: dict[int, Decimal] = field(default_factory=dict)
    buys: dict[int, int] = field(default_factory=dict)
    sells: dict[int, int] = field(default_factory=dict)
    price_change_pct: dict[int, float] = field(default_factory=dict)

    holder_count: int | None = None
    unique_buyers_5m: int | None = None
    pool_reserve_base: int | None = None   # base units, for exact AMM impact math
    pool_reserve_quote: int | None = None
    dex: Dex | None = None
    pool_address: str | None = None

    def buy_sell_ratio(self, window: int = 300) -> float | None:
        b, s = self.buys.get(window), self.sells.get(window)
        if b is None or s is None:
            return None
        return b / max(s, 1)


@dataclass(slots=True)
class SocialSnapshot:
    token: TokenRef
    as_of: datetime
    mentions: dict[int, int] = field(default_factory=dict)     # window -> count
    unique_authors: dict[int, int] = field(default_factory=dict)
    weighted_reach: float = 0.0        # follower-weighted, log-damped
    sentiment: float = 0.0             # -1..1
    bot_ratio: float | None = None     # share of mentions from low-quality accounts
    google_trends: float | None = None
    sources: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class FlowSnapshot:
    """Smart-money / whale flow derived from labelled wallet cohorts."""

    token: TokenRef
    as_of: datetime
    smart_buyers: int = 0
    smart_sellers: int = 0
    smart_net_usd: Decimal = Decimal(0)
    whale_net_usd: Decimal = Decimal(0)
    top_cohort_pnl_30d: float | None = None   # historical skill of buying cohort
    insider_net_usd: Decimal = Decimal(0)


@dataclass(slots=True)
class PoolCreatedEvent:
    """Raw signal straight off the chain: a new liquidity pool now exists.

    Deliberately the smallest struct in this module — it carries only what can
    be read directly from the creation transaction's logs/accounts, nothing
    that needs a follow-up HTTP call. Conflating this with ``Candidate`` would
    mean the ingest listener blocks on slow enrichment APIs while new blocks
    keep arriving; keeping them separate is what lets ingest, enrich, and score
    run as independent processes connected by the bus (``mca.core.bus``).
    """

    token: TokenRef
    quote_token: TokenRef
    dex: Dex
    pool_address: str
    detected_at: datetime
    tx_signature: str
    block_or_slot: int
    initial_base_reserve: int | None = None
    initial_quote_reserve: int | None = None
    deployer: str | None = None
    correlation_id: str = field(default_factory=new_id)


@dataclass(slots=True)
class Candidate:
    """Everything the scorer is allowed to see, bundled with one timestamp."""

    token: TokenRef
    as_of: datetime
    market: MarketSnapshot
    security: SecurityReport
    social: SocialSnapshot | None = None
    flow: FlowSnapshot | None = None
    ohlcv_1m: Any | None = None      # numpy array (n,6): ts,o,h,l,c,v
    correlation_id: str = field(default_factory=new_id)


# --------------------------------------------------------------------------- #
# Scoring output
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class GateResult:
    name: str
    passed: bool
    detail: str = ""
    value: Any = None


@dataclass(slots=True)
class Score:
    token: TokenRef
    as_of: datetime
    total: float                        # 0..100
    components: dict[str, float]        # group -> 0..100 contribution basis
    weights: dict[str, float]
    gates: list[GateResult]
    win_probability: float | None = None    # calibrated P(win), from ML
    expected_value_bps: float | None = None
    model_version: str | None = None

    @property
    def hard_gate_passed(self) -> bool:
        return all(g.passed for g in self.gates)

    @property
    def failed_gates(self) -> list[str]:
        return [g.name for g in self.gates if not g.passed]


# --------------------------------------------------------------------------- #
# Orders, fills, positions
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TradeIntent:
    """Immutable instruction produced by the risk gate. The ``idempotency_key``
    is the contract that prevents double-buys under at-least-once delivery."""

    intent_id: str
    token: TokenRef
    side: Side
    quote_amount: Decimal | None      # for BUY: how much SOL/ETH/BNB to spend
    base_amount: int | None           # for SELL: base units to sell
    max_slippage_bps: int
    idempotency_key: str
    mode: TradeMode
    reason: str = ""
    priority_fee_micro_lamports: int | None = None
    deadline: datetime | None = None
    correlation_id: str = ""


@dataclass(slots=True)
class Fill:
    fill_id: str
    intent_id: str
    token: TokenRef
    side: Side
    base_amount: int
    quote_amount: Decimal
    price_usd: Decimal
    tx_hash: str
    block_or_slot: int
    gas_or_fee_usd: Decimal
    slippage_bps: int
    filled_at: datetime
    dex: Dex | None = None


@dataclass(slots=True)
class ExitLevel:
    """One rung of the take-profit ladder."""

    trigger_pct: float      # +0.5 == +50% from average entry
    sell_fraction: float    # fraction of the *original* position size
    executed: bool = False
    executed_at: datetime | None = None


@dataclass(slots=True)
class Position:
    position_id: str
    token: TokenRef
    status: PositionStatus
    mode: TradeMode
    opened_at: datetime

    base_amount: int = 0              # currently held, base units
    original_base_amount: int = 0
    avg_entry_price_usd: Decimal = Decimal(0)
    quote_invested: Decimal = Decimal(0)
    realized_pnl_usd: Decimal = Decimal(0)
    fees_paid_usd: Decimal = Decimal(0)

    high_water_price_usd: Decimal = Decimal(0)
    stop_price_usd: Decimal | None = None
    break_even_armed: bool = False
    ladder: list[ExitLevel] = field(default_factory=list)

    entry_score: float | None = None
    entry_win_prob: float | None = None
    closed_at: datetime | None = None
    exit_reason: ExitReason | None = None
    correlation_id: str = ""

    def unrealized_pnl_usd(self, price: Decimal) -> Decimal:
        if self.base_amount == 0:
            return Decimal(0)
        decs = Decimal(10) ** self.token_decimals
        held_value = (Decimal(self.base_amount) / decs) * price
        cost_basis = (Decimal(self.base_amount) / decs) * self.avg_entry_price_usd
        return held_value - cost_basis

    token_decimals: int = 9

    def pnl_pct(self, price: Decimal) -> float:
        if self.avg_entry_price_usd == 0:
            return 0.0
        return float(price / self.avg_entry_price_usd) - 1.0
