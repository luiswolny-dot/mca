"""Tests for db/repository.py, risk/gate_service.py, execution/worker.py's
codecs, ops/watchdog.py, ops/tasks.py, and the executor's unit-correctness fix.

Same zero-dependency-runner philosophy as test_core.py / test_execution.py.
Repository query *execution* needs Postgres and is not tested here (see
db/repository.py's module docstring) — only the pure mapping functions, which
carry the actual risk of a silent unit or field-name mismatch.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mca.domain.models import (
    Chain, Dex, ExitLevel, ExitReason, Fill, GateResult, Position, PositionStatus,
    Score, Side, TokenRef, TradeIntent, TradeMode, new_id, utcnow,
)

TOK = TokenRef(Chain.SOLANA, "TokenMint1111111111111111111111111111111111")

# --------------------------------------------------------------------------- #
# db.repository — pure mappers
# --------------------------------------------------------------------------- #

from mca.db.repository import (
    consecutive_losses, fill_to_row, position_to_row, row_to_position,
    row_to_shadow_record, shadow_record_to_row, signal_to_row, token_ref_to_row,
)
from mca.execution.base import ShadowRecord


def _position(**overrides) -> Position:
    base = dict(
        position_id=new_id(), token=TOK, status=PositionStatus.OPEN, mode=TradeMode.PAPER,
        opened_at=utcnow(), base_amount=1_000_000, original_base_amount=1_000_000,
        avg_entry_price_usd=Decimal("0.00042"), quote_invested=Decimal("0.5"),
        realized_pnl_usd=Decimal("0"), fees_paid_usd=Decimal("0.01"),
        high_water_price_usd=Decimal("0.00050"), stop_price_usd=Decimal("0.00030"),
        break_even_armed=True,
        ladder=[ExitLevel(0.45, 0.30, executed=True, executed_at=utcnow()),
               ExitLevel(1.0, 0.25, executed=False)],
        entry_score=82.5, entry_win_prob=0.51, correlation_id=new_id(), token_decimals=9,
    )
    base.update(overrides)
    return Position(**base)


def test_position_row_round_trip_preserves_ladder_and_decimals():
    pos = _position()
    row = position_to_row(pos, token_id=7, strategy_hash="abc123")
    restored = row_to_position(row, token=TOK, token_decimals=9)
    assert restored.position_id == pos.position_id
    assert restored.base_amount == pos.base_amount
    assert restored.avg_entry_price_usd == pos.avg_entry_price_usd
    assert len(restored.ladder) == 2
    assert restored.ladder[0].executed is True
    assert restored.ladder[1].executed is False
    assert restored.break_even_armed is True
    assert restored.token_decimals == 9


def test_position_row_maps_quote_invested_to_quote_invested_usd_column():
    """The dataclass field is `quote_invested`; the DB column is
    `quote_invested_usd`. Confirms the mapper bridges the naming mismatch
    rather than silently dropping the value under a KeyError-free but wrong
    key name."""
    pos = _position(quote_invested=Decimal("1.23"))
    row = position_to_row(pos, token_id=1, strategy_hash="h")
    assert row["quote_invested_usd"] == Decimal("1.23")
    assert "quote_invested" not in row


def test_position_row_closed_position_round_trip():
    pos = _position(status=PositionStatus.CLOSED, closed_at=utcnow(),
                    exit_reason=ExitReason.TAKE_PROFIT, base_amount=0)
    row = position_to_row(pos, token_id=1, strategy_hash="h")
    restored = row_to_position(row, token=TOK, token_decimals=9)
    assert restored.status is PositionStatus.CLOSED
    assert restored.exit_reason is ExitReason.TAKE_PROFIT


def test_fill_row_carries_idempotency_key_separately_from_fill_id():
    fill = Fill(fill_id=new_id(), intent_id="intent-1", token=TOK, side=Side.BUY,
               base_amount=1000, quote_amount=Decimal("0.1"), price_usd=Decimal("0.3"),
               tx_hash="sig", block_or_slot=1, gas_or_fee_usd=Decimal("0.001"),
               slippage_bps=50, filled_at=utcnow())
    row = fill_to_row(fill, position_id="pos-1", token_id=1, mode=TradeMode.PAPER,
                      idempotency_key="the-original-intent-key")
    assert row["idempotency_key"] == "the-original-intent-key"
    assert row["id"] == fill.fill_id
    assert row["idempotency_key"] != row["id"]


def test_signal_row_flattens_gate_results():
    score = Score(token=TOK, as_of=utcnow(), total=75.0,
                 components={"liquidity_depth": 80.0}, weights={"liquidity_depth": 16.0},
                 gates=[GateResult("min_liquidity", True, "ok"),
                       GateResult("min_holders", False, "too few")],
                 win_probability=0.44, expected_value_bps=310.0)
    row = signal_to_row(score, token_id=1, mode=TradeMode.PAPER, strategy_hash="h",
                        correlation_id="c1", model_version="wp-1", acted_on=True,
                        rejection_reason=None)
    assert row["gates_passed"] is False   # Score.hard_gate_passed checks ALL gates
    assert row["gates"][1]["name"] == "min_holders"
    assert row["score_total"] == 75.0


def test_token_ref_row_uses_chain_value_not_enum():
    row = token_ref_to_row(TOK, decimals=6)
    assert row["chain"] == "solana"
    assert row["decimals"] == 6


def test_shadow_record_row_round_trip():
    rec = ShadowRecord(strategy_hash="abc", started_at=utcnow(), paper_trades=42,
                       last_config_change_at=utcnow() - timedelta(days=1))
    row = shadow_record_to_row(rec)
    restored = row_to_shadow_record(row)
    assert restored.strategy_hash == rec.strategy_hash
    assert restored.paper_trades == 42


def test_consecutive_losses_stops_at_first_win():
    rows = [{"realized_pnl_usd": Decimal("-5")}, {"realized_pnl_usd": Decimal("-2")},
           {"realized_pnl_usd": Decimal("3")}, {"realized_pnl_usd": Decimal("-100")}]
    assert consecutive_losses(rows) == 2


def test_consecutive_losses_zero_when_most_recent_is_a_win():
    assert consecutive_losses([{"realized_pnl_usd": Decimal("1")}]) == 0


def test_consecutive_losses_empty_is_zero():
    assert consecutive_losses([]) == 0


# --------------------------------------------------------------------------- #
# execution.base — TradeIntent / Fill bus codecs
# --------------------------------------------------------------------------- #

from mca.execution.base import fill_from_payload, fill_to_payload, intent_from_payload, intent_to_payload


def test_intent_payload_round_trip_buy():
    intent = TradeIntent(intent_id=new_id(), token=TOK, side=Side.BUY,
                         quote_amount=Decimal("0.25"), base_amount=None,
                         max_slippage_bps=500, idempotency_key=new_id(), mode=TradeMode.PAPER,
                         reason="test", correlation_id="corr-1")
    restored = intent_from_payload(intent_to_payload(intent))
    assert restored.token == intent.token
    assert restored.quote_amount == intent.quote_amount
    assert restored.base_amount is None
    assert restored.side is Side.BUY


def test_intent_payload_round_trip_sell_has_no_quote_amount():
    intent = TradeIntent(intent_id=new_id(), token=TOK, side=Side.SELL,
                         quote_amount=None, base_amount=500_000, max_slippage_bps=800,
                         idempotency_key=new_id(), mode=TradeMode.LIVE)
    restored = intent_from_payload(intent_to_payload(intent))
    assert restored.quote_amount is None
    assert restored.base_amount == 500_000
    assert restored.mode is TradeMode.LIVE


def test_fill_payload_round_trip_preserves_idempotency_key_alongside_fill():
    fill = Fill(fill_id=new_id(), intent_id="intent-9", token=TOK, side=Side.SELL,
               base_amount=200, quote_amount=Decimal("0.02"), price_usd=Decimal("0.31"),
               tx_hash="sig9", block_or_slot=99, gas_or_fee_usd=Decimal("0.0001"),
               slippage_bps=40, filled_at=utcnow(), dex=Dex.JUPITER)
    payload = fill_to_payload(fill, idempotency_key="orig-key-1")
    restored, key = fill_from_payload(payload)
    assert restored.fill_id == fill.fill_id
    assert restored.price_usd == fill.price_usd
    assert restored.dex is Dex.JUPITER
    assert key == "orig-key-1"


# --------------------------------------------------------------------------- #
# risk.gate_service — SignalMessage codec
# --------------------------------------------------------------------------- #

from mca.risk.gate_service import SignalMessage


def test_signal_message_round_trip():
    msg = SignalMessage(
        token=TOK, mode=TradeMode.PAPER, strategy_hash="h1", as_of=utcnow(),
        score_total=81.2, components={"technicals": 70.0}, weights={"technicals": 15.0},
        gates=[{"name": "min_liquidity", "passed": True, "detail": "ok"}],
        gates_passed=True, failed_gates=[], win_probability=0.47,
        expected_value_bps=290.0, model_version="wp-1",
        liquidity_usd=Decimal("40000"), reserve_quote_usd=Decimal("20000"),
        quote_usd_price=Decimal("152.30"), atr_pct=0.18, correlation_id="c-1",
    )
    restored = SignalMessage.from_payload(msg.to_payload())
    assert restored.token == msg.token
    assert restored.liquidity_usd == Decimal("40000")
    assert restored.reserve_quote_usd == Decimal("20000")
    assert restored.win_probability == 0.47


# --------------------------------------------------------------------------- #
# execution.solana_executor — regression test for the price_usd unit bug
# --------------------------------------------------------------------------- #

from mca.execution.base import ExecutionConfig
from mca.execution.jupiter import parse_quote_response
from mca.execution.solana_executor import SolanaJupiterExecutor, _PreparedSwap


class _FakeKeypair:
    def pubkey(self) -> str:
        return "FAKEPUBKEY1111111111111111111111111111111"


class _FakeSwapResult:
    prioritization_fee_lamports = 10_000


def _intent(side: Side = Side.BUY, **overrides) -> TradeIntent:
    base = dict(intent_id=new_id(), token=TOK, side=side,
               quote_amount=Decimal("0.1") if side is Side.BUY else None,
               base_amount=None if side is Side.BUY else 50_000_000_000,
               max_slippage_bps=500, idempotency_key=new_id(), mode=TradeMode.PAPER)
    base.update(overrides)
    return TradeIntent(**base)


def test_build_fill_buy_price_is_usd_per_whole_token_not_per_base_unit():
    """The bug: an earlier version computed price_usd = quote_usd / base_amount,
    which is USD per BASE UNIT (~1e-9 of the real per-token price for a
    9-decimal mint). Position.unrealized_pnl_usd() multiplies price by
    (base_amount / 10**decimals) — i.e. it expects USD per WHOLE token. Caught
    while wiring the DB layer; this is the regression test for the fix."""
    ex = SolanaJupiterExecutor(TradeMode.PAPER, ExecutionConfig(), rpc=None, jupiter=None,
                               keypair=_FakeKeypair())
    ex.quote_usd_price = Decimal("150")   # $150 / SOL

    quote_raw = {"inputMint": "SOL", "inAmount": "100000000", "outputMint": "TOK",
                "outAmount": "50000000000", "otherAmountThreshold": "1", "swapMode": "ExactIn",
                "slippageBps": 50, "priceImpactPct": "0.01", "routePlan": []}
    q = parse_quote_response(quote_raw)
    prepared = _PreparedSwap(quote_lamports_or_base=100_000_000, quote_response=q)
    intent = _intent(side=Side.BUY, quote_amount=Decimal("0.1"))

    fill = ex._build_fill(intent, prepared, _FakeSwapResult(), "sig", 42, token_decimals=9)

    # Spent 0.1 SOL * $150/SOL = $15. Received 50e9 base units at 9 decimals = 50
    # whole tokens. Correct price: $15 / 50 = $0.30 per whole token.
    assert abs(fill.price_usd - Decimal("0.30")) < Decimal("0.0001")
    # What the bug would have produced instead: $15 / 50_000_000_000 ~= 3e-10 —
    # nine orders of magnitude off. Guard against ever regressing to that shape.
    assert fill.price_usd > Decimal("0.01")
    assert fill.base_amount == 50_000_000_000


def test_build_fill_respects_non_default_decimals():
    """Same scenario, 6-decimal mint (e.g. a USDC-like token) — proves the
    fix is decimals-aware and not just correct by coincidence for 9."""
    ex = SolanaJupiterExecutor(TradeMode.PAPER, ExecutionConfig(), rpc=None, jupiter=None,
                               keypair=_FakeKeypair())
    ex.quote_usd_price = Decimal("150")
    quote_raw = {"inputMint": "SOL", "inAmount": "100000000", "outputMint": "TOK",
                "outAmount": "50000000", "otherAmountThreshold": "1", "swapMode": "ExactIn",
                "slippageBps": 50, "priceImpactPct": "0.01", "routePlan": []}
    q = parse_quote_response(quote_raw)
    prepared = _PreparedSwap(quote_lamports_or_base=100_000_000, quote_response=q)
    intent = _intent(side=Side.BUY, quote_amount=Decimal("0.1"))

    fill = ex._build_fill(intent, prepared, _FakeSwapResult(), "sig", 1, token_decimals=6)
    # 50_000_000 base units at 6 decimals = 50 whole tokens -> same $0.30 result.
    assert abs(fill.price_usd - Decimal("0.30")) < Decimal("0.0001")


async def test_quote_buy_returns_usd_per_whole_token():
    class _FakeJupiterForQuote:
        async def quote(self, input_mint, output_mint, amount, slippage_bps):
            raw = {"inputMint": input_mint, "inAmount": str(amount), "outputMint": output_mint,
                  "outAmount": "50000000000", "otherAmountThreshold": "1", "swapMode": "ExactIn",
                  "slippageBps": slippage_bps, "priceImpactPct": "0.01", "routePlan": []}
            return parse_quote_response(raw)

    ex = SolanaJupiterExecutor(TradeMode.PAPER, ExecutionConfig(), rpc=None,
                               jupiter=_FakeJupiterForQuote(), keypair=_FakeKeypair())
    ex.quote_usd_price = Decimal("150")
    ex._decimals_cache[TOK.address] = 9   # pre-seed: skip needing a fake RPC for this test

    price, slippage = await ex.quote(TOK, Side.BUY, Decimal("0.1"))
    assert abs(price - Decimal("0.30")) < Decimal("0.0001")
    assert slippage == 100


# --------------------------------------------------------------------------- #
# ops.watchdog — pure health-decision function
# --------------------------------------------------------------------------- #

from mca.ops.watchdog import evaluate_health


def test_evaluate_health_ok_when_everything_alive():
    healthy, detail = evaluate_health(
        {"risk-gate": True, "executor": True}, rpc_ok=True,
        missed_counts={"risk-gate": 0, "executor": 0}, missed_threshold=4)
    assert healthy is True


def test_evaluate_health_unhealthy_after_threshold_missed_beats():
    healthy, detail = evaluate_health(
        {"risk-gate": False}, rpc_ok=True,
        missed_counts={"risk-gate": 4}, missed_threshold=4)
    assert healthy is False
    assert "risk-gate" in detail


def test_evaluate_health_tolerates_missed_beats_below_threshold():
    healthy, _ = evaluate_health(
        {"executor": False}, rpc_ok=True,
        missed_counts={"executor": 2}, missed_threshold=4)
    assert healthy is True


def test_evaluate_health_unhealthy_when_rpc_down():
    healthy, detail = evaluate_health(
        {"risk-gate": True}, rpc_ok=False,
        missed_counts={"risk-gate": 0}, missed_threshold=4)
    assert healthy is False
    assert "RPC" in detail


# --------------------------------------------------------------------------- #
# ops.tasks — task bodies against fake dependencies
# --------------------------------------------------------------------------- #

from mca.ops.tasks import retrain_model_impl, roll_day_impl, sweep_check_impl


class _FakeRepoForRollDay:
    def __init__(self, state: str):
        self._state = state
        self.set_calls = []
        self.event_calls = []

    async def get_risk_state_row(self):
        return {"state": self._state, "equity_usd": Decimal("1000"), "peak_equity_usd": Decimal("1200")}

    async def set_risk_state(self, state, reason, equity_usd, peak_equity_usd, **kw):
        self.set_calls.append((state, reason))

    async def record_risk_event(self, event, old_state, new_state, detail):
        self.event_calls.append((event, old_state, new_state))


async def test_roll_day_clears_derisk():
    from mca.domain.models import RiskState
    repo = _FakeRepoForRollDay("derisk")
    result = await roll_day_impl(repo)
    assert result == "derisk-cleared"
    assert repo.set_calls[0][0] is RiskState.NORMAL


async def test_roll_day_is_noop_when_normal():
    repo = _FakeRepoForRollDay("normal")
    result = await roll_day_impl(repo)
    assert result.startswith("no-op")
    assert repo.set_calls == []


async def test_roll_day_does_not_touch_locked():
    """LOCKED must only ever clear via the manual 2FA-gated API endpoint —
    confirming the scheduled task does not even look at whether it should."""
    repo = _FakeRepoForRollDay("locked")
    result = await roll_day_impl(repo)
    assert result.startswith("no-op")
    assert repo.set_calls == []


class _FakeRpcForSweep:
    def __init__(self, lamports: int):
        self.lamports = lamports

    async def get_balance(self, pubkey):
        return self.lamports


class _FakeJupiterForSweep:
    async def sol_usd_price(self):
        return Decimal("150")


async def test_sweep_check_detects_excess_over_cap():
    rpc = _FakeRpcForSweep(lamports=20 * 10**9)   # 20 SOL * $150 = $3000
    cfg = ExecutionConfig(hot_wallet_max_usd=Decimal(2000), sweep_threshold_usd=Decimal(1000))
    result = await sweep_check_impl(rpc, _FakeJupiterForSweep(), "somepubkey", cfg)
    assert result.startswith("sweep-due")


async def test_sweep_check_noop_below_threshold():
    rpc = _FakeRpcForSweep(lamports=int(5 * 10**9))   # 5 SOL * $150 = $750
    cfg = ExecutionConfig(hot_wallet_max_usd=Decimal(2000), sweep_threshold_usd=Decimal(1000))
    result = await sweep_check_impl(rpc, _FakeJupiterForSweep(), "somepubkey", cfg)
    assert result.startswith("no-op")


class _FakeRepoForRetrain:
    def __init__(self, rows):
        self.rows = rows

    async def fetch_training_rows(self, mode, since):
        return self.rows


async def test_retrain_noop_with_no_history():
    from mca.ml.calibration import TrainConfig
    repo = _FakeRepoForRetrain([])
    result = await retrain_model_impl(repo, TrainConfig())
    assert result.startswith("no-op")


async def test_retrain_reports_insufficient_samples_without_crashing():
    from mca.ml.calibration import TrainConfig
    rows = [{"realized_pnl_usd": Decimal("1"), "quote_invested_usd": Decimal("10"),
            "opened_at": utcnow(), "closed_at": utcnow(), "score_total": 75.0,
            "components": {"technicals": 60.0}, "as_of": utcnow()}
           for _ in range(5)]   # far below min_train_samples
    repo = _FakeRepoForRetrain(rows)
    result = await retrain_model_impl(repo, TrainConfig(min_train_samples=800))
    assert "no-op" in str(result)


# --------------------------------------------------------------------------- #
# ml.calibration — training_rows_to_arrays
# --------------------------------------------------------------------------- #

from mca.ml.calibration import FEATURE_ORDER, training_rows_to_arrays


def test_training_rows_to_arrays_labels_by_net_return():
    rows = [
        {"realized_pnl_usd": Decimal("5"), "quote_invested_usd": Decimal("10"),
        "score_total": 80.0, "components": {"technicals": 70.0}, "as_of": utcnow()},
        {"realized_pnl_usd": Decimal("-3"), "quote_invested_usd": Decimal("10"),
        "score_total": 72.0, "components": {"technicals": 40.0}, "as_of": utcnow()},
    ]
    X, y, ts = training_rows_to_arrays(rows)
    assert y.tolist() == [1, 0]
    assert X.shape == (2, len(FEATURE_ORDER))
    assert X[0, FEATURE_ORDER.index("score_total")] == 80.0
    assert X[0, FEATURE_ORDER.index("technicals")] == 70.0
    import numpy as np
    assert np.isnan(X[0, FEATURE_ORDER.index("rsi_14")])   # honestly not populated yet
