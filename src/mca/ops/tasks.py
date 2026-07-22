"""Scheduled maintenance: ``celery -A mca.ops.tasks beat`` / ``worker``.

Design decision — task bodies are plain async functions; Celery is a thin shell
--------------------------------------------------------------------------------
Every ``*_impl`` function takes its dependencies as arguments and contains no
Celery-specific code, so it is unit-testable without a broker. The
``@app.task``-wrapped versions below just call them. ``celery`` is not
installed in this build sandbox (see the package check in earlier commits),
so the whole Celery-specific section is guarded — the ``_impl`` functions
still import and run under the plain test suite.

Design decision — the sweep task alerts, it never signs
------------------------------------------------------------
Moving funds from the hot wallet to cold storage is exactly the kind of action
that deserves a human confirmation, not a cron job with signing authority (see
``execution/wallet.py``'s design note on the hot wallet as a spending limit).
``sweep_check_task`` only detects that a sweep is due and alerts; nothing in
this module holds or uses a private key.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from decimal import Decimal

from mca.domain.models import RiskState, TradeMode, utcnow

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Task bodies — plain async functions, fully testable without Celery
# --------------------------------------------------------------------------- #

async def roll_day_impl(repo) -> str:
    """Clears a DERISK state at the start of a new trading day.

    ``DERISK`` is a soft warning band (70% of the daily loss budget used); a
    new UTC day means a fresh budget, so the warning no longer applies.
    ``LOCKED`` is never touched here — that state requires the manual,
    2FA-gated unlock in ``api/app.py`` by design, and a scheduled job silently
    clearing it would defeat the entire point of that control.

    Note this is narrower than a classic "reset daily counters" job: daily and
    weekly realised PnL are computed fresh from a date-filtered SQL query every
    time ``Repository.load_portfolio_state`` runs (see ``db/repository.py``),
    not from a persisted running total — so there is nothing to zero for those.
    """
    rs = await repo.get_risk_state_row()
    if rs["state"] != "derisk":
        return f"no-op (state={rs['state']})"
    await repo.set_risk_state(RiskState.NORMAL, "new trading day",
                              Decimal(rs["equity_usd"]), Decimal(rs["peak_equity_usd"]))
    await repo.record_risk_event("roll_day", RiskState.DERISK, RiskState.NORMAL,
                                 {"reason": "scheduled daily reset"})
    log.info("risk state DERISK -> NORMAL (new trading day)")
    return "derisk-cleared"


async def sweep_check_impl(rpc, jupiter, wallet_pubkey: str, exec_cfg, notifier=None) -> str:
    """Detects (does not execute) a due hot-wallet sweep. See module docstring."""
    from mca.execution.jupiter import LAMPORTS_PER_SOL
    from mca.execution.wallet import hot_wallet_sweep_amount_usd

    lamports = await rpc.get_balance(wallet_pubkey)
    sol_balance = Decimal(lamports) / LAMPORTS_PER_SOL
    sol_usd = await jupiter.sol_usd_price()
    balance_usd = sol_balance * sol_usd

    sweep_amount = hot_wallet_sweep_amount_usd(balance_usd, exec_cfg)
    if sweep_amount <= 0:
        return f"no-op (balance=${balance_usd:.2f} <= cap)"

    log.warning("hot wallet balance $%.2f exceeds cap $%.2f — $%.2f should be swept to cold storage",
               balance_usd, exec_cfg.hot_wallet_max_usd, sweep_amount)
    if notifier is not None:
        from mca.alerts.notifier import Alert, Severity
        await notifier.send(Alert(
            Severity.WARNING, "Hot wallet sweep due",
            f"Balance ${balance_usd:,.2f}, cap ${exec_cfg.hot_wallet_max_usd:,.2f}. "
            f"Move ${sweep_amount:,.2f} to the cold address manually.",
            dedup_key="sweep-due"))
    return f"sweep-due:{sweep_amount}"


async def retrain_model_impl(repo, train_cfg, mode: TradeMode = TradeMode.PAPER,
                             lookback_days: int = 60):
    """Pulls closed positions, retrains, and returns the calibration report.

    A fresh deployment has no trade history — ``WinProbabilityModel.fit``
    already refuses to train below ``min_train_samples`` (see
    ``ml/calibration.py``) by raising ``ValueError``; that is caught here and
    logged as an expected, routine outcome, not a task failure. Retrying a
    model retrain hourly until there is enough data is the correct behaviour,
    not a bug to suppress.
    """
    from mca.ml.calibration import WinProbabilityModel, training_rows_to_arrays

    since = utcnow() - timedelta(days=lookback_days)
    rows = await repo.fetch_training_rows(mode, since)
    if not rows:
        return "no-op (no closed positions with a linked signal yet)"

    X, y, ts = training_rows_to_arrays(rows, label_threshold=train_cfg.label_threshold)
    model = WinProbabilityModel(train_cfg)
    try:
        report = model.fit(X, y, ts)
    except ValueError as exc:
        log.info("retrain skipped: %s", exc)
        return f"no-op ({exc})"
    log.info("model %s trained: %s", model.version, report.notes)
    return report


# --------------------------------------------------------------------------- #
# Celery wiring — guarded; the sandbox this was built in has no broker
# --------------------------------------------------------------------------- #

CELERY_AVAILABLE = True
try:
    from celery import Celery
    from celery.schedules import crontab
except ImportError:
    CELERY_AVAILABLE = False
    Celery = None  # type: ignore[assignment]


def make_app(broker_url: str) -> "Celery":
    if not CELERY_AVAILABLE:
        raise RuntimeError("celery is not installed: pip install 'celery[redis]'")
    app = Celery("mca", broker=broker_url, backend=broker_url)
    app.conf.task_routes = {"mca.ops.tasks.*": {"queue": "maintenance"}}
    app.conf.timezone = "UTC"
    app.conf.beat_schedule = {
        "roll-day": {"task": "mca.ops.tasks.roll_day_task",
                    "schedule": crontab(minute=0, hour=0)},
        "sweep-check": {"task": "mca.ops.tasks.sweep_check_task", "schedule": 3600.0},
        "retrain-model": {"task": "mca.ops.tasks.retrain_model_task",
                          "schedule": crontab(minute=0, hour=3)},
    }
    return app


if CELERY_AVAILABLE:
    app = make_app(os.environ.get("REDIS_URL", "redis://redis:6379/0"))

    def _build_context():
        """Constructs the shared dependencies every task needs from env/config.
        Kept as a plain function (not module-level state) so each task
        invocation gets a fresh Repository/RPC client rather than one that has
        outlived a connection reset."""
        import asyncio

        from mca.core.config import load_config
        from mca.db.repository import Repository
        from mca.execution.jupiter import JupiterClient
        from mca.execution.solana_rpc import SolanaRpcClient

        cfg = load_config()
        repo = Repository(os.environ["DATABASE_URL"])
        solana_cfg = cfg.chains.get("solana")
        rpc = SolanaRpcClient(solana_cfg.rpc_http) if solana_cfg else None
        jupiter = JupiterClient(api_key=os.environ.get("JUPITER_API_KEY"))
        return cfg, repo, rpc, jupiter

    @app.task(name="mca.ops.tasks.roll_day_task")
    def roll_day_task():
        import asyncio
        _cfg, repo, _rpc, _jupiter = _build_context()
        mode = TradeMode(_cfg.system.mode)
        return asyncio.run(roll_day_impl(repo))

    @app.task(name="mca.ops.tasks.sweep_check_task")
    def sweep_check_task():
        import asyncio
        cfg, _repo, rpc, jupiter = _build_context()
        exec_cfg = cfg.to_execution_config()
        wallet_pubkey = os.environ.get("WALLET_PUBKEY")
        if not wallet_pubkey or rpc is None:
            return "no-op (WALLET_PUBKEY or Solana RPC not configured)"
        return asyncio.run(sweep_check_impl(rpc, jupiter, wallet_pubkey, exec_cfg))

    @app.task(name="mca.ops.tasks.retrain_model_task")
    def retrain_model_task():
        import asyncio
        cfg, repo, _rpc, _jupiter = _build_context()
        mode = TradeMode(cfg.system.mode)
        train_cfg = cfg.to_ml_train_config()
        return asyncio.run(retrain_model_impl(repo, train_cfg, mode))
