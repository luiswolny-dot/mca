"""Repository: the only module that speaks SQL.

Every other module works with the plain dataclasses in ``domain/models.py``.
This module translates between those and the tables in ``db/schema.sql`` —
nothing upstream needs to know a database exists, matching the "domain
imports nothing" rule from ``ARCHITECTURE.md``.

Design decision — mapping functions are pure, execution is not
--------------------------------------------------------------------
``position_to_row`` / ``row_to_position`` and their siblings take and return
plain dicts/dataclasses — no engine, no connection, no ``async``. They are
fully unit-tested. The :class:`Repository` methods that actually run SQL are
guarded behind an optional ``sqlalchemy``/``asyncpg`` import and have **not**
executed against a live Postgres in this session — there is no database
available in the build sandbox. The query shapes below use only well-established
SQLAlchemy 2.0 Core patterns (``Table``/``select``/``insert``/
``on_conflict_do_nothing``); still, run the test suite against a real Postgres
(``docker compose up -d postgres`` then the raw ``schema.sql``) before trusting
this with real trades. See ``execution/base.py``'s shadow-trading mandate —
this is exactly the kind of untested path it exists to keep out of the live
path until it has proven itself in paper mode.

Design decision — token_id resolution is select-then-insert-then-reselect
-------------------------------------------------------------------------
Multiple ingest/enrich workers can see the same new token in the same instant.
``get_or_create_token_id`` selects, and on a miss inserts with
``ON CONFLICT DO NOTHING ... RETURNING id``; if a concurrent writer won the
race, the ``RETURNING`` clause returns nothing and a second ``SELECT`` picks up
whichever row won. No advisory lock needed — this converges in at most one
extra round trip and never raises on the race.

Design decision — idempotent fill inserts, not a duplicate-catch
------------------------------------------------------------------
``insert_fill`` uses ``ON CONFLICT (idempotency_key) DO NOTHING`` rather than
catching a unique-violation exception. Both approaches are correct;
``ON CONFLICT`` also makes the intent explicit in the statement itself — a
reader does not have to know that a particular exception type is expected and
benign.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from mca.domain.models import (
    Chain, ExitLevel, ExitReason, Fill, Position, PositionStatus,
    RiskState, Score, TokenRef, TradeMode, new_id, utcnow,
)
from mca.execution.base import ShadowRecord
from mca.risk.limits import PortfolioState

log = logging.getLogger(__name__)


class RepositoryError(Exception):
    """Base for repository failures."""


class RepositoryUnavailable(RepositoryError):
    """``sqlalchemy``/``asyncpg`` not installed, or the DB is unreachable."""


# --------------------------------------------------------------------------- #
# Pure mapping functions — no DB dependency, fully unit-tested
# --------------------------------------------------------------------------- #

def _ladder_to_json(ladder: list[ExitLevel]) -> list[dict]:
    return [{"trigger_pct": l.trigger_pct, "sell_fraction": l.sell_fraction,
            "executed": l.executed,
            "executed_at": l.executed_at.isoformat() if l.executed_at else None}
           for l in ladder]


def _ladder_from_json(raw: list[dict] | None) -> list[ExitLevel]:
    out = []
    for l in raw or []:
        out.append(ExitLevel(
            trigger_pct=l["trigger_pct"], sell_fraction=l["sell_fraction"],
            executed=l["executed"],
            executed_at=datetime.fromisoformat(l["executed_at"]) if l.get("executed_at") else None,
        ))
    return out


def position_to_row(pos: Position, *, token_id: int, strategy_hash: str,
                    signal_id: int | None = None) -> dict:
    return {
        "id": pos.position_id, "token_id": token_id, "signal_id": signal_id,
        "mode": pos.mode.value, "status": pos.status.value, "strategy_hash": strategy_hash,
        "opened_at": pos.opened_at, "closed_at": pos.closed_at,
        "base_amount": pos.base_amount, "original_base_amount": pos.original_base_amount,
        "avg_entry_price_usd": pos.avg_entry_price_usd, "quote_invested_usd": pos.quote_invested,
        "realized_pnl_usd": pos.realized_pnl_usd, "fees_paid_usd": pos.fees_paid_usd,
        "high_water_price_usd": pos.high_water_price_usd, "stop_price_usd": pos.stop_price_usd,
        "break_even_armed": pos.break_even_armed, "ladder": _ladder_to_json(pos.ladder),
        "entry_score": pos.entry_score, "entry_win_prob": pos.entry_win_prob,
        "exit_reason": pos.exit_reason.value if pos.exit_reason else None,
        "correlation_id": pos.correlation_id or new_id(),
    }


def row_to_position(row: Mapping[str, Any], *, token: TokenRef, token_decimals: int) -> Position:
    ladder_raw = row["ladder"]
    if isinstance(ladder_raw, str):
        ladder_raw = json.loads(ladder_raw or "[]")
    return Position(
        position_id=str(row["id"]), token=token, status=PositionStatus(row["status"]),
        mode=TradeMode(row["mode"]), opened_at=row["opened_at"], closed_at=row["closed_at"],
        base_amount=int(row["base_amount"]), original_base_amount=int(row["original_base_amount"]),
        avg_entry_price_usd=Decimal(row["avg_entry_price_usd"]),
        quote_invested=Decimal(row["quote_invested_usd"]),
        realized_pnl_usd=Decimal(row["realized_pnl_usd"]), fees_paid_usd=Decimal(row["fees_paid_usd"]),
        high_water_price_usd=Decimal(row["high_water_price_usd"] or 0),
        stop_price_usd=Decimal(row["stop_price_usd"]) if row["stop_price_usd"] is not None else None,
        break_even_armed=bool(row["break_even_armed"]), ladder=_ladder_from_json(ladder_raw),
        entry_score=row["entry_score"], entry_win_prob=row["entry_win_prob"],
        exit_reason=ExitReason(row["exit_reason"]) if row["exit_reason"] else None,
        correlation_id=str(row["correlation_id"]), token_decimals=token_decimals,
    )


def fill_to_row(fill: Fill, *, position_id: str, token_id: int, mode: TradeMode,
                idempotency_key: str) -> dict:
    return {
        "id": fill.fill_id, "position_id": position_id, "intent_id": fill.intent_id,
        "idempotency_key": idempotency_key, "token_id": token_id, "side": fill.side.value,
        "mode": mode.value, "base_amount": fill.base_amount,
        "quote_amount_usd": fill.quote_amount, "price_usd": fill.price_usd,
        "slippage_bps": fill.slippage_bps, "gas_or_fee_usd": fill.gas_or_fee_usd,
        "tx_hash": fill.tx_hash, "block_or_slot": fill.block_or_slot,
        "latency_ms": None, "filled_at": fill.filled_at,
    }


def signal_to_row(score: Score, *, token_id: int, mode: TradeMode, strategy_hash: str,
                  correlation_id: str, model_version: str | None,
                  acted_on: bool, rejection_reason: str | None) -> dict:
    return {
        "token_id": token_id, "as_of": score.as_of, "mode": mode.value,
        "score_total": score.total, "components": score.components, "weights": score.weights,
        "gates": [{"name": g.name, "passed": g.passed, "detail": g.detail} for g in score.gates],
        "gates_passed": score.hard_gate_passed, "failed_gates": score.failed_gates,
        "win_probability": score.win_probability, "expected_value_bps": score.expected_value_bps,
        "model_version": model_version, "strategy_hash": strategy_hash, "acted_on": acted_on,
        "rejection_reason": rejection_reason, "correlation_id": correlation_id,
    }


def token_ref_to_row(token: TokenRef, *, decimals: int = 9, symbol: str | None = None,
                     name: str | None = None, deployer: str | None = None,
                     created_on_chain_at: datetime | None = None) -> dict:
    return {"chain": token.chain.value, "address": token.address, "symbol": symbol,
           "name": name, "decimals": decimals, "deployer": deployer,
           "created_on_chain_at": created_on_chain_at}


def shadow_record_to_row(rec: ShadowRecord) -> dict:
    return {"strategy_hash": rec.strategy_hash, "started_at": rec.started_at,
           "paper_trades": rec.paper_trades, "last_config_change_at": rec.last_config_change_at}


def row_to_shadow_record(row: Mapping[str, Any]) -> ShadowRecord:
    return ShadowRecord(strategy_hash=row["strategy_hash"], started_at=row["started_at"],
                        paper_trades=row["paper_trades"],
                        last_config_change_at=row["last_config_change_at"])


def consecutive_losses(closed_positions_newest_first: list[Mapping[str, Any]]) -> int:
    """Count trailing losses from the most recent close backwards, stopping at
    the first win. A pure function over already-fetched rows so the "stop at
    the first win" control flow — awkward to express as a single SQL
    aggregate — is a plain, testable Python loop instead of a window-function
    puzzle nobody wants to debug at 2 a.m."""
    count = 0
    for row in closed_positions_newest_first:
        if Decimal(row["realized_pnl_usd"]) < 0:
            count += 1
        else:
            break
    return count


# --------------------------------------------------------------------------- #
# SQLAlchemy Core layer — guarded, mirrors schema.sql exactly
# --------------------------------------------------------------------------- #

SQLALCHEMY_AVAILABLE = True
try:
    import sqlalchemy as sa
    from sqlalchemy.dialects.postgresql import JSONB, UUID
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

    metadata = sa.MetaData()

    tokens_t = sa.Table(
        "tokens", metadata,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("chain", sa.Text), sa.Column("address", sa.Text),
        sa.Column("symbol", sa.Text), sa.Column("name", sa.Text),
        sa.Column("decimals", sa.SmallInteger),
        sa.Column("created_on_chain_at", sa.DateTime(timezone=True)),
        sa.Column("deployer", sa.Text),
        sa.Column("first_seen_at", sa.DateTime(timezone=True)),
    )

    signals_t = sa.Table(
        "signals", metadata,
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("token_id", sa.BigInteger), sa.Column("as_of", sa.DateTime(timezone=True)),
        sa.Column("mode", sa.Text), sa.Column("score_total", sa.Float),
        sa.Column("components", JSONB), sa.Column("weights", JSONB), sa.Column("gates", JSONB),
        sa.Column("gates_passed", sa.Boolean),
        sa.Column("failed_gates", sa.ARRAY(sa.Text)),
        sa.Column("win_probability", sa.Float), sa.Column("expected_value_bps", sa.Float),
        sa.Column("model_version", sa.Text), sa.Column("strategy_hash", sa.Text),
        sa.Column("acted_on", sa.Boolean), sa.Column("rejection_reason", sa.Text),
        sa.Column("correlation_id", UUID),
    )

    positions_t = sa.Table(
        "positions", metadata,
        sa.Column("id", UUID, primary_key=True),
        sa.Column("token_id", sa.BigInteger), sa.Column("signal_id", sa.BigInteger),
        sa.Column("mode", sa.Text), sa.Column("status", sa.Text),
        sa.Column("strategy_hash", sa.Text),
        sa.Column("opened_at", sa.DateTime(timezone=True)),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("base_amount", sa.Numeric(78, 0)), sa.Column("original_base_amount", sa.Numeric(78, 0)),
        sa.Column("avg_entry_price_usd", sa.Numeric(36, 18)), sa.Column("quote_invested_usd", sa.Numeric(24, 6)),
        sa.Column("realized_pnl_usd", sa.Numeric(24, 6)), sa.Column("fees_paid_usd", sa.Numeric(24, 6)),
        sa.Column("high_water_price_usd", sa.Numeric(36, 18)), sa.Column("stop_price_usd", sa.Numeric(36, 18)),
        sa.Column("break_even_armed", sa.Boolean), sa.Column("ladder", JSONB),
        sa.Column("entry_score", sa.Float), sa.Column("entry_win_prob", sa.Float),
        sa.Column("exit_reason", sa.Text), sa.Column("correlation_id", UUID),
    )

    fills_t = sa.Table(
        "fills", metadata,
        sa.Column("id", UUID, primary_key=True), sa.Column("position_id", UUID),
        sa.Column("intent_id", UUID), sa.Column("idempotency_key", sa.Text),
        sa.Column("token_id", sa.BigInteger), sa.Column("side", sa.Text), sa.Column("mode", sa.Text),
        sa.Column("base_amount", sa.Numeric(78, 0)), sa.Column("quote_amount_usd", sa.Numeric(24, 6)),
        sa.Column("price_usd", sa.Numeric(36, 18)), sa.Column("slippage_bps", sa.Float),
        sa.Column("gas_or_fee_usd", sa.Numeric(24, 6)), sa.Column("tx_hash", sa.Text),
        sa.Column("block_or_slot", sa.BigInteger), sa.Column("latency_ms", sa.Integer),
        sa.Column("filled_at", sa.DateTime(timezone=True)),
    )

    risk_state_t = sa.Table(
        "risk_state", metadata,
        sa.Column("id", sa.SmallInteger, primary_key=True),
        sa.Column("state", sa.Text), sa.Column("reason", sa.Text),
        sa.Column("equity_usd", sa.Numeric(24, 6)), sa.Column("peak_equity_usd", sa.Numeric(24, 6)),
        sa.Column("changed_at", sa.DateTime(timezone=True)),
        sa.Column("unlocked_by", sa.Text), sa.Column("unlock_note", sa.Text),
    )

    risk_events_t = sa.Table(
        "risk_events", metadata,
        sa.Column("id", sa.BigInteger, primary_key=True), sa.Column("at", sa.DateTime(timezone=True)),
        sa.Column("event", sa.Text), sa.Column("old_state", sa.Text), sa.Column("new_state", sa.Text),
        sa.Column("detail", JSONB),
    )

    shadow_records_t = sa.Table(
        "shadow_records", metadata,
        sa.Column("strategy_hash", sa.Text, primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True)), sa.Column("paper_trades", sa.Integer),
        sa.Column("last_config_change_at", sa.DateTime(timezone=True)),
        sa.Column("promoted_at", sa.DateTime(timezone=True)), sa.Column("promoted_by", sa.Text),
    )

except ImportError:
    SQLALCHEMY_AVAILABLE = False


class Repository:
    """Async facade over Postgres. One instance per process; the engine's own
    connection pool handles concurrency, so callers do not manage connections."""

    def __init__(self, dsn: str, *, pool_size: int = 10) -> None:
        self.dsn = dsn
        self._engine: "AsyncEngine | None" = None
        self.pool_size = pool_size

    def _require_engine(self) -> "AsyncEngine":
        if not SQLALCHEMY_AVAILABLE:
            raise RepositoryUnavailable("sqlalchemy/asyncpg not installed")
        if self._engine is None:
            self._engine = create_async_engine(self.dsn, pool_size=self.pool_size, pool_pre_ping=True)
        return self._engine

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    # -- tokens ---------------------------------------------------------------
    async def get_or_create_token_id(self, token: TokenRef, **extra: Any) -> int:
        engine = self._require_engine()
        async with engine.begin() as conn:
            row = (await conn.execute(
                sa.select(tokens_t.c.id).where(
                    tokens_t.c.chain == token.chain.value, tokens_t.c.address == token.address)
            )).first()
            if row:
                return row.id

            stmt = pg_insert(tokens_t).values(**token_ref_to_row(token, **extra)).on_conflict_do_nothing(
                index_elements=["chain", "address"]).returning(tokens_t.c.id)
            row = (await conn.execute(stmt)).first()
            if row:
                return row.id

            row = (await conn.execute(
                sa.select(tokens_t.c.id).where(
                    tokens_t.c.chain == token.chain.value, tokens_t.c.address == token.address)
            )).first()
            if row is None:
                raise RepositoryError(f"token {token.key} not found after insert race")
            return row.id

    async def get_token_decimals(self, token_id: int) -> int:
        engine = self._require_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(
                sa.select(tokens_t.c.decimals).where(tokens_t.c.id == token_id))).first()
            if row is None:
                raise RepositoryError(f"unknown token_id {token_id}")
            return int(row.decimals)

    # -- signals ----------------------------------------------------------------
    async def insert_signal(self, row: dict) -> int:
        engine = self._require_engine()
        async with engine.begin() as conn:
            result = await conn.execute(sa.insert(signals_t).values(**row).returning(signals_t.c.id))
            return result.scalar_one()

    # -- positions --------------------------------------------------------------
    async def get_open_position(self, token: TokenRef, mode: TradeMode,
                                strategy_hash: str) -> tuple[Position, int] | None:
        """Returns ``(position, token_id)`` — the caller already has ``token``
        and rarely needs ``token_id`` again, but the fill-recording path does,
        and re-querying it there would be a wasted round trip."""
        engine = self._require_engine()
        async with engine.connect() as conn:
            token_row = (await conn.execute(
                sa.select(tokens_t.c.id, tokens_t.c.decimals).where(
                    tokens_t.c.chain == token.chain.value, tokens_t.c.address == token.address)
            )).first()
            if token_row is None:
                return None
            pos_row = (await conn.execute(
                sa.select(positions_t).where(
                    positions_t.c.token_id == token_row.id,
                    positions_t.c.mode == mode.value,
                    positions_t.c.strategy_hash == strategy_hash,
                    positions_t.c.status.in_(["open", "scaling_out"]),
                )
            )).mappings().first()
            if pos_row is None:
                return None
            return row_to_position(pos_row, token=token, token_decimals=token_row.decimals), token_row.id

    async def get_open_positions(self, mode: TradeMode) -> list[tuple[Position, int]]:
        engine = self._require_engine()
        async with engine.connect() as conn:
            rows = (await conn.execute(
                sa.select(positions_t, tokens_t.c.chain, tokens_t.c.address, tokens_t.c.decimals)
                .join(tokens_t, tokens_t.c.id == positions_t.c.token_id)
                .where(positions_t.c.mode == mode.value,
                      positions_t.c.status.in_(["open", "scaling_out"]))
            )).mappings().all()
            out = []
            for row in rows:
                token = TokenRef(Chain(row["chain"]), row["address"])
                out.append((row_to_position(row, token=token, token_decimals=row["decimals"]),
                           row["token_id"]))
            return out

    async def insert_position(self, pos: Position, *, token_id: int, strategy_hash: str,
                              signal_id: int | None = None) -> None:
        engine = self._require_engine()
        async with engine.begin() as conn:
            await conn.execute(sa.insert(positions_t).values(
                **position_to_row(pos, token_id=token_id, strategy_hash=strategy_hash,
                                 signal_id=signal_id)))

    async def update_position(self, pos: Position, *, token_id: int, strategy_hash: str) -> None:
        engine = self._require_engine()
        row = position_to_row(pos, token_id=token_id, strategy_hash=strategy_hash)
        row.pop("id")
        async with engine.begin() as conn:
            await conn.execute(sa.update(positions_t).where(positions_t.c.id == pos.position_id).values(**row))

    # -- fills --------------------------------------------------------------
    async def insert_fill(self, fill: Fill, *, position_id: str, token_id: int,
                          mode: TradeMode, idempotency_key: str) -> bool:
        """Returns ``True`` if newly recorded, ``False`` if this
        ``idempotency_key`` was already present (safe to call twice)."""
        engine = self._require_engine()
        row = fill_to_row(fill, position_id=position_id, token_id=token_id, mode=mode,
                          idempotency_key=idempotency_key)
        stmt = pg_insert(fills_t).values(**row).on_conflict_do_nothing(
            index_elements=["idempotency_key"])
        async with engine.begin() as conn:
            result = await conn.execute(stmt)
            return result.rowcount > 0

    # -- risk state -----------------------------------------------------------
    async def get_risk_state_row(self) -> Mapping[str, Any]:
        engine = self._require_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(sa.select(risk_state_t).where(risk_state_t.c.id == 1))).mappings().first()
            if row is None:
                raise RepositoryError("risk_state singleton row missing — schema not initialised?")
            return row

    async def set_risk_state(self, state: RiskState, reason: str, equity_usd: Decimal,
                             peak_equity_usd: Decimal, *, unlocked_by: str | None = None,
                             unlock_note: str | None = None) -> None:
        engine = self._require_engine()
        async with engine.begin() as conn:
            await conn.execute(sa.update(risk_state_t).where(risk_state_t.c.id == 1).values(
                state=state.value, reason=reason, equity_usd=equity_usd,
                peak_equity_usd=peak_equity_usd, changed_at=utcnow(),
                unlocked_by=unlocked_by, unlock_note=unlock_note))

    async def record_risk_event(self, event: str, old_state: RiskState | None,
                                new_state: RiskState | None, detail: dict) -> None:
        engine = self._require_engine()
        async with engine.begin() as conn:
            await conn.execute(sa.insert(risk_events_t).values(
                event=event, old_state=old_state.value if old_state else None,
                new_state=new_state.value if new_state else None, detail=detail))

    async def load_portfolio_state(self, mode: TradeMode, *, now: datetime | None = None) -> PortfolioState:
        """Rebuilds :class:`~mca.risk.limits.PortfolioState` from Postgres —
        the "read on boot" half of the circuit breaker's persistence guarantee
        described in ``risk/limits.py``."""
        now = now or utcnow()
        engine = self._require_engine()
        rs = await self.get_risk_state_row()

        async with engine.connect() as conn:
            open_rows = (await conn.execute(
                sa.select(positions_t.c.quote_invested_usd, tokens_t.c.chain)
                .join(tokens_t, tokens_t.c.id == positions_t.c.token_id)
                .where(positions_t.c.mode == mode.value,
                      positions_t.c.status.in_(["open", "scaling_out"]))
            )).all()

            day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
            week_start = day_start - timedelta(days=now.weekday())

            daily_pnl = (await conn.execute(
                sa.select(sa.func.coalesce(sa.func.sum(positions_t.c.realized_pnl_usd), 0))
                .where(positions_t.c.mode == mode.value, positions_t.c.closed_at >= day_start)
            )).scalar_one()
            weekly_pnl = (await conn.execute(
                sa.select(sa.func.coalesce(sa.func.sum(positions_t.c.realized_pnl_usd), 0))
                .where(positions_t.c.mode == mode.value, positions_t.c.closed_at >= week_start)
            )).scalar_one()
            entries_last_hour = (await conn.execute(
                sa.select(sa.func.count()).select_from(positions_t)
                .where(positions_t.c.mode == mode.value,
                      positions_t.c.opened_at >= now - timedelta(hours=1))
            )).scalar_one()
            last_entry = (await conn.execute(
                sa.select(sa.func.max(positions_t.c.opened_at))
                .where(positions_t.c.mode == mode.value)
            )).scalar_one()

            recent_closed = (await conn.execute(
                sa.select(positions_t.c.realized_pnl_usd, positions_t.c.closed_at)
                .where(positions_t.c.mode == mode.value, positions_t.c.status == "closed")
                .order_by(positions_t.c.closed_at.desc()).limit(20)
            )).mappings().all()

        open_by_chain: dict[Chain, int] = {}
        gross = Decimal(0)
        for row in open_rows:
            gross += Decimal(row.quote_invested_usd)
            chain = Chain(row.chain)
            open_by_chain[chain] = open_by_chain.get(chain, 0) + 1

        last_loss_at = next((r["closed_at"] for r in recent_closed if Decimal(r["realized_pnl_usd"]) < 0), None)

        return PortfolioState(
            equity_usd=Decimal(rs["equity_usd"]), free_capital_usd=Decimal(rs["equity_usd"]) - gross,
            peak_equity_usd=Decimal(rs["peak_equity_usd"]), open_positions=len(open_rows),
            open_by_chain=open_by_chain, gross_exposure_usd=gross,
            realized_pnl_today_usd=Decimal(daily_pnl), realized_pnl_week_usd=Decimal(weekly_pnl),
            consecutive_losses=consecutive_losses(recent_closed), entries_last_hour=entries_last_hour,
            last_entry_at=last_entry, last_loss_at=last_loss_at,
            risk_state=RiskState(rs["state"]), day=now.date(),
        )

    # -- shadow records -------------------------------------------------------
    async def get_shadow_record(self, strategy_hash: str) -> ShadowRecord | None:
        engine = self._require_engine()
        async with engine.connect() as conn:
            row = (await conn.execute(
                sa.select(shadow_records_t).where(shadow_records_t.c.strategy_hash == strategy_hash)
            )).mappings().first()
            return row_to_shadow_record(row) if row else None

    async def increment_shadow_trades(self, strategy_hash: str, started_at: datetime) -> None:
        """Called once per paper fill. If no record exists yet for this
        strategy hash, this is where the shadow clock actually starts —
        matching ``LiveExecutorGuard``'s expectation that ``started_at`` marks
        the first paper trade under an unchanged configuration, not the
        process boot time."""
        engine = self._require_engine()
        stmt = pg_insert(shadow_records_t).values(
            strategy_hash=strategy_hash, started_at=started_at, paper_trades=1,
            last_config_change_at=started_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["strategy_hash"],
            set_={"paper_trades": shadow_records_t.c.paper_trades + 1})
        async with engine.begin() as conn:
            await conn.execute(stmt)

    # -- ML training data -------------------------------------------------------
    async def fetch_training_rows(self, mode: TradeMode, since: datetime) -> list[Mapping[str, Any]]:
        """Closed positions joined with the ``signals`` row that authorised
        them (via ``positions.signal_id``). Positions opened before
        ``signal_id`` was wired to a real value (or opened by a manual/paper
        test with no matching signal) are excluded — a training row without
        the score components that produced the trade cannot be turned into a
        feature vector, and silently zero-filling would teach the model a
        pattern that never happened.
        """
        engine = self._require_engine()
        async with engine.connect() as conn:
            rows = (await conn.execute(
                sa.select(
                    positions_t.c.realized_pnl_usd, positions_t.c.quote_invested_usd,
                    positions_t.c.opened_at, positions_t.c.closed_at,
                    signals_t.c.score_total, signals_t.c.components, signals_t.c.as_of,
                )
                .join(signals_t, signals_t.c.id == positions_t.c.signal_id)
                .where(positions_t.c.mode == mode.value, positions_t.c.status == "closed",
                      positions_t.c.closed_at >= since, positions_t.c.signal_id.is_not(None))
            )).mappings().all()
            return rows
