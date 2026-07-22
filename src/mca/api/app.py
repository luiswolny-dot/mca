"""FastAPI application.

Design decision — the API is read-mostly and holds no keys
----------------------------------------------------------
Three write operations exist: pause, resume-from-LOCKED, and force-close a
position. Everything else is a read. The API cannot open a position — it can
only *stop* things. An attacker who fully compromises this service can halt
trading and close positions, which is annoying; they cannot open a position in
an attacker-controlled token, which would be fatal.

Design decision — control actions are commands on a stream, not direct calls
----------------------------------------------------------------------------
``POST /control/halt`` publishes to Redis; the risk gate acts on it. The API
never mutates trading state directly, so there is exactly one code path that can
change it and exactly one place to audit.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Schemas — Pydantic lives here, at the boundary, and nowhere else
# --------------------------------------------------------------------------- #

class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "halted"]
    mode: Literal["backtest", "paper", "live"]
    risk_state: str
    uptime_s: float
    sources_healthy: dict[str, bool]
    last_signal_at: datetime | None
    open_positions: int


class PositionOut(BaseModel):
    id: str
    symbol: str
    chain: str
    status: str
    opened_at: datetime
    quote_invested_usd: float
    unrealized_pnl_usd: float
    unrealized_pnl_pct: float
    realized_pnl_usd: float
    stop_price_usd: float | None
    entry_score: float | None
    entry_win_prob: float | None
    ladder_progress: str = Field(description="e.g. '1/4 rungs executed'")


class SignalOut(BaseModel):
    token: str
    symbol: str | None
    chain: str
    as_of: datetime
    score: float
    components: dict[str, float]
    gates_passed: bool
    failed_gates: list[str]
    win_probability: float | None
    expected_value_bps: float | None
    acted_on: bool
    rejection_reason: str | None


class PerformanceOut(BaseModel):
    mode: str
    n_trades: int
    win_rate: float
    profit_factor: float
    expectancy_pct: float
    sharpe: float
    sortino: float
    deflated_sharpe: float
    max_drawdown_pct: float
    total_return_pct: float
    equity_curve: list[tuple[str, float]]


class RiskOut(BaseModel):
    state: str
    equity_usd: float
    peak_equity_usd: float
    daily_pnl_usd: float
    daily_budget_used_pct: float
    weekly_budget_used_pct: float
    gross_exposure_pct: float
    open_positions: int
    max_open_positions: int
    consecutive_losses: int
    locked_reason: str | None


class HaltRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)
    flatten: bool = False


class UnlockRequest(BaseModel):
    """Unlocking a tripped circuit breaker is the single most dangerous action
    in the system, so it demands friction: a second factor and a written
    justification that lands in the append-only audit log."""

    totp_code: str = Field(pattern=r"^\d{6}$")
    justification: str = Field(min_length=20, max_length=1000)
    acknowledge_risk: Literal[True]


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("api starting")
    # app.state.db = await create_pool(...)
    # app.state.redis = await create_redis(...)
    yield
    log.info("api stopping")


app = FastAPI(
    title="Meme-Coin Alpha API",
    version="0.1.0",
    description=(
        "Read-mostly control plane. This service holds no signing keys and "
        "cannot open positions."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


async def require_auth() -> str:
    """JWT verification. Returns the operator id used in audit records."""
    raise NotImplementedError("wire to your JWT verifier")


Operator = Annotated[str, Depends(require_auth)]


# --- read endpoints --------------------------------------------------------

@app.get("/health", response_model=HealthResponse, tags=["ops"])
async def health() -> HealthResponse:
    """Liveness + trading readiness. Used by Docker healthcheck and the watchdog.

    Note that ``status: ok`` and ``risk_state: locked`` can coexist: the service
    is healthy, it is simply not permitted to trade.
    """
    raise NotImplementedError


@app.get("/positions", response_model=list[PositionOut], tags=["trading"])
async def positions(
    status: Annotated[str, Query()] = "open",
    mode: Annotated[str, Query()] = "paper",
) -> list[PositionOut]:
    raise NotImplementedError


@app.get("/signals", response_model=list[SignalOut], tags=["trading"])
async def signals(
    limit: Annotated[int, Query(le=500)] = 100,
    min_score: Annotated[float, Query(ge=0, le=100)] = 0,
    include_rejected: Annotated[bool, Query()] = True,
) -> list[SignalOut]:
    """Rejected signals are included by default and that is intentional — the
    rejection histogram is the primary diagnostic for a broken data source."""
    raise NotImplementedError


@app.get("/performance", response_model=PerformanceOut, tags=["analytics"])
async def performance(
    mode: Annotated[str, Query()] = "paper",
    days: Annotated[int, Query(le=365)] = 30,
) -> PerformanceOut:
    raise NotImplementedError


@app.get("/risk", response_model=RiskOut, tags=["risk"])
async def risk() -> RiskOut:
    raise NotImplementedError


@app.get("/gates/rejections", tags=["analytics"])
async def gate_rejections(hours: Annotated[int, Query(le=168)] = 24) -> dict[str, int]:
    """Histogram of which gate rejected how many candidates.

    Read this every morning. A gate that suddenly rejects everything usually
    means an upstream API started returning nulls, not that the market changed.
    """
    raise NotImplementedError


# --- control endpoints (the only writes) -----------------------------------

@app.post("/control/halt", tags=["control"])
async def halt(req: HaltRequest, operator: Operator) -> dict:
    """Stop opening new positions. With ``flatten=true``, also close all open
    positions at market. Publishes a command; the risk gate executes it."""
    raise NotImplementedError


@app.post("/control/resume", tags=["control"])
async def resume(operator: Operator) -> dict:
    """Leave HALT. Refuses if the state is LOCKED — that needs ``/control/unlock``."""
    raise NotImplementedError


@app.post("/control/unlock", tags=["control"])
async def unlock(req: UnlockRequest, operator: Operator) -> dict:
    """Clear a tripped circuit breaker. Requires TOTP and a written justification.

    The friction is the feature. The most common path to account destruction is
    an operator overriding a loss limit after a bad day, so this endpoint is
    deliberately annoying to call and permanently logged.
    """
    raise NotImplementedError


@app.post("/positions/{position_id}/close", tags=["control"])
async def close_position(position_id: str, operator: Operator) -> dict:
    raise NotImplementedError


# --- realtime --------------------------------------------------------------

@app.websocket("/ws")
async def websocket_feed(ws: WebSocket) -> None:
    """Live feed: signals, fills, position updates, risk-state changes.

    Backpressure policy: if the client cannot keep up, drop the oldest messages
    rather than buffering. A dashboard showing slightly stale data is fine; an
    API process OOM-killed by an unbounded send queue is not.
    """
    await ws.accept()
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass


@app.exception_handler(NotImplementedError)
async def not_implemented(request, exc):
    raise HTTPException(status_code=501, detail="endpoint scaffolded, not yet wired")
