"""Unit tests for the decision-critical modules.

These are pytest-style (plain functions + assert) and also run under the
zero-dependency runner in ``scripts/run_tests.py``, so a slim container can
verify the safety logic without installing a test framework.

Testing philosophy: every test here encodes a way the system could lose money.
``test_unknown_security_field_fails_gate`` is not a coverage exercise — it is the
regression test for "the API returned null and the bot bought a honeypot".
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np

from mca.backtest import metrics as M
from mca.domain.models import (
    Candidate, Chain, MarketSnapshot, Position, PositionStatus, SecurityReport,
    TokenRef, TradeMode,
)
from mca.features import technicals as ta
from mca.portfolio.exits import ExitConfig, ExitEngine, LiveContext, build_ladder
from mca.risk.limits import PortfolioState, RiskGate, RiskLimits
from mca.risk.sizing import (
    BindingCap, SizingConfig, compute_position_size, constant_product_impact_bps,
    kelly_fraction, max_size_for_impact, round_trip_cost_bps,
)
from mca.scoring.gates import GateConfig, evaluate_gates, gates_passed
from mca.scoring.scorer import Scorer, ScorerConfig, expected_value_bps

NOW = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
TOK = TokenRef(chain=Chain.SOLANA, address="So11111111111111111111111111111111111111112")


# --------------------------------------------------------------------------- #
# fixtures (plain builders, no pytest fixtures so the fallback runner works)
# --------------------------------------------------------------------------- #

def clean_security(**over) -> SecurityReport:
    base = dict(
        token=TOK, checked_at=NOW,
        mint_authority_revoked=True, freeze_authority_revoked=True,
        liquidity_usd=Decimal(60_000), lp_locked_pct=1.0, lp_burned=True,
        buy_tax_bps=0, sell_tax_bps=0, transfer_pausable=False,
        simulated_sell_ok=True, holder_count=600, top1_holder_pct=0.04,
        top10_holder_pct=0.18, dev_holding_pct=0.01, bundled_supply_pct=0.05,
        insider_cluster_pct=0.03, deployer_prior_rugs=0,
        created_at=NOW - timedelta(minutes=40),
    )
    base.update(over)
    return SecurityReport(**base)


def clean_market(**over) -> MarketSnapshot:
    base = dict(
        token=TOK, as_of=NOW, price_usd=Decimal("0.00042"),
        liquidity_usd=Decimal(60_000), fdv_usd=Decimal(700_000),
        volume_usd={300: Decimal(40_000), 3600: Decimal(260_000)},
        buys={300: 210}, sells={300: 120},
        unique_buyers_5m=140, holder_count=600,
    )
    base.update(over)
    return MarketSnapshot(**base)


def candidate(sec=None, mkt=None, ohlcv=None) -> Candidate:
    return Candidate(token=TOK, as_of=NOW, market=mkt or clean_market(),
                     security=sec or clean_security(), ohlcv_1m=ohlcv)


def synthetic_ohlcv(n: int = 90, drift: float = 0.004, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    price = 0.0003
    rows = []
    for i in range(n):
        ret = drift + rng.normal(0, 0.02)
        new = max(price * (1 + ret), 1e-9)
        rows.append([i * 60, price, max(price, new) * 1.01,
                     min(price, new) * 0.99, new, float(rng.uniform(2000, 9000))])
        price = new
    return np.array(rows, dtype=np.float64)


# --------------------------------------------------------------------------- #
# gates
# --------------------------------------------------------------------------- #

def test_clean_token_passes_all_gates():
    res = evaluate_gates(candidate(), GateConfig())
    failed = [r.name for r in res if not r.passed]
    assert failed == [], f"unexpected gate failures: {failed}"


def test_unknown_security_field_fails_gate():
    """Fail-closed: a None must never be read as safe."""
    cand = candidate(sec=clean_security(mint_authority_revoked=None))
    res = evaluate_gates(cand, GateConfig())
    assert not gates_passed(res)
    assert any(r.name == "mint_authority_revoked" and not r.passed for r in res)


def test_live_mint_authority_is_rejected():
    cand = candidate(sec=clean_security(mint_authority_revoked=False))
    assert not gates_passed(evaluate_gates(cand, GateConfig()))


def test_honeypot_sell_simulation_failure_is_rejected():
    cand = candidate(sec=clean_security(simulated_sell_ok=False))
    res = evaluate_gates(cand, GateConfig())
    assert any(r.name == "sell_simulation" and not r.passed for r in res)


def test_concentrated_supply_is_rejected():
    cand = candidate(sec=clean_security(top10_holder_pct=0.62))
    assert not gates_passed(evaluate_gates(cand, GateConfig()))


def test_wash_trading_volume_is_rejected():
    """Vol/liq of 100x in five minutes is a volume bot, not demand."""
    cand = candidate(mkt=clean_market(volume_usd={300: Decimal(6_000_000),
                                                  3600: Decimal(9_000_000)}))
    res = evaluate_gates(cand, GateConfig())
    assert any(r.name == "volume_liquidity_sanity" and not r.passed for r in res)


def test_too_young_token_is_rejected():
    cand = candidate(sec=clean_security(created_at=NOW - timedelta(seconds=30)))
    res = evaluate_gates(cand, GateConfig())
    assert any(r.name == "min_age" and not r.passed for r in res)


def test_gate_failure_forces_score_zero():
    cand = candidate(sec=clean_security(freeze_authority_revoked=False))
    score = Scorer().score(cand)
    assert score.total == 0.0
    assert not score.hard_gate_passed


# --------------------------------------------------------------------------- #
# scorer
# --------------------------------------------------------------------------- #

def test_score_is_bounded_and_positive_for_clean_token():
    score = Scorer().score(candidate(ohlcv=synthetic_ohlcv()))
    assert 0.0 <= score.total <= 100.0
    assert score.total > 0.0


def test_weights_must_sum_to_100():
    cfg = ScorerConfig()
    cfg.weights.social = 40.0
    try:
        Scorer(cfg)
    except ValueError:
        return
    raise AssertionError("Scorer accepted weights that do not sum to 100")


def test_botted_social_is_zeroed_not_discounted():
    from mca.domain.models import SocialSnapshot
    cand = candidate(ohlcv=synthetic_ohlcv())
    cand.social = SocialSnapshot(token=TOK, as_of=NOW,
                                 mentions={900: 4000}, unique_authors={900: 12},
                                 weighted_reach=5e5, sentiment=0.9, bot_ratio=0.93)
    s = Scorer().score(cand)
    assert s.components["social"] == 0.0


def test_expected_value_accounts_for_cost():
    """A 55% win rate on symmetric payoffs must be negative after 900bps cost."""
    ev = expected_value_bps(0.55, avg_win_pct=0.25, avg_loss_pct=0.25,
                            round_trip_cost_bps=900)
    assert ev < 0, f"EV should be negative, got {ev}"


# --------------------------------------------------------------------------- #
# AMM / sizing
# --------------------------------------------------------------------------- #

def test_impact_and_inversion_are_consistent():
    reserve = 50_000.0
    size = max_size_for_impact(reserve, max_impact_bps=100, fee_bps=30)
    impact = constant_product_impact_bps(size, reserve, fee_bps=30)
    assert abs(impact - 100.0) < 0.5, impact


def test_impact_grows_with_size():
    r = 40_000.0
    assert constant_product_impact_bps(100, r) < constant_product_impact_bps(1_000, r)


def test_round_trip_cost_includes_both_legs_and_taxes():
    cost = round_trip_cost_bps(1_000, 20_000, buy_tax_bps=300, sell_tax_bps=300,
                               priority_fee_usd=0.5)
    # two ~4.7% impacts + 6% tax => comfortably over 1000bps
    assert cost > 1_000, cost


def test_kelly_is_zero_without_edge():
    assert kelly_fraction(0.30, 0.5, 0.5) == 0.0


def test_kelly_positive_with_edge_and_capped_by_config():
    cfg = SizingConfig()
    res = compute_position_size(
        equity_usd=Decimal(10_000), free_capital_usd=Decimal(9_000),
        reserve_quote_usd=5_000_000, cfg=cfg,
        win_prob=0.75, avg_win_pct=1.2, avg_loss_pct=0.25, atr_pct=0.05,
    )
    assert res.notional_usd <= cfg.max_notional_per_trade_usd


def test_liquidity_cap_binds_on_thin_pools():
    """The whole point of the module: a thin pool must shrink the trade."""
    res = compute_position_size(
        equity_usd=Decimal(50_000), free_capital_usd=Decimal(50_000),
        reserve_quote_usd=9_000, cfg=SizingConfig(), win_prob=0.8,
        avg_win_pct=1.5, avg_loss_pct=0.25, atr_pct=0.06,
    )
    assert res.binding_cap is BindingCap.LIQUIDITY_IMPACT, res.binding_cap
    assert res.expected_impact_bps <= SizingConfig().max_impact_bps + 1


def test_higher_volatility_reduces_size():
    common = dict(equity_usd=Decimal(20_000), free_capital_usd=Decimal(20_000),
                  reserve_quote_usd=8_000_000, cfg=SizingConfig(), win_prob=None)
    calm = compute_position_size(atr_pct=0.03, **common)
    wild = compute_position_size(atr_pct=0.20, **common)
    assert wild.notional_usd < calm.notional_usd


# --------------------------------------------------------------------------- #
# risk gate
# --------------------------------------------------------------------------- #

def base_state(**over) -> PortfolioState:
    d = dict(equity_usd=Decimal(10_000), free_capital_usd=Decimal(8_000),
             peak_equity_usd=Decimal(10_000))
    d.update(over)
    return PortfolioState(**d)


def test_gate_allows_normal_entry():
    assert RiskGate().check_entry(base_state(), Chain.SOLANA, Decimal(200), NOW).allowed


def test_daily_loss_limit_locks_the_system():
    st = base_state(realized_pnl_today_usd=Decimal(-700))   # -7% vs 6% limit
    d = RiskGate().check_entry(st, Chain.SOLANA, Decimal(100), NOW)
    assert not d.allowed and "risk_state" in d.reason


def test_max_drawdown_locks_the_system():
    st = base_state(equity_usd=Decimal(7_500), peak_equity_usd=Decimal(10_000))
    assert not RiskGate().check_entry(st, Chain.SOLANA, Decimal(50), NOW).allowed


def test_gross_exposure_binds_before_position_count():
    """Eight meme coins are one correlated bet, so exposure must bind first."""
    st = base_state(open_positions=2, gross_exposure_usd=Decimal(1_450))
    d = RiskGate().check_entry(st, Chain.SOLANA, Decimal(200), NOW)
    assert not d.allowed and "gross_exposure" in d.reason


def test_loss_cooldown_blocks_revenge_trade():
    st = base_state(last_loss_at=NOW - timedelta(seconds=30))
    d = RiskGate().check_entry(st, Chain.SOLANA, Decimal(100), NOW)
    assert not d.allowed and d.reason == "loss_cooldown"


def test_per_chain_cap():
    st = base_state(open_positions=3, open_by_chain={Chain.SOLANA: 3})
    limits = RiskLimits(max_open_per_chain=3)
    d = RiskGate(limits).check_entry(st, Chain.SOLANA, Decimal(50), NOW)
    assert not d.allowed and "max_open_per_chain" in d.reason


# --------------------------------------------------------------------------- #
# exits
# --------------------------------------------------------------------------- #

def open_position(entry: str = "0.0010") -> Position:
    cfg = ExitConfig()
    return Position(
        position_id="p1", token=TOK, status=PositionStatus.OPEN,
        mode=TradeMode.PAPER, opened_at=NOW,
        base_amount=1_000_000_000, original_base_amount=1_000_000_000,
        avg_entry_price_usd=Decimal(entry), quote_invested=Decimal(200),
        high_water_price_usd=Decimal(entry), ladder=build_ladder(cfg),
        token_decimals=9,
    )


def ctx(price: str, **over) -> LiveContext:
    d = dict(price_usd=Decimal(price), liquidity_usd=Decimal(60_000),
             entry_liquidity_usd=Decimal(60_000), now=NOW + timedelta(minutes=5))
    d.update(over)
    return LiveContext(**d)


def test_hard_stop_triggers_full_exit():
    sig = ExitEngine().evaluate(open_position(), ctx("0.00070"))
    assert sig.should_exit and sig.sell_fraction == 1.0


def test_first_ladder_rung_takes_partial_profit():
    sig = ExitEngine().evaluate(open_position(), ctx("0.00150"))
    assert sig.should_exit
    assert 0 < sig.sell_fraction < 1.0


def test_liquidity_drain_beats_everything():
    """Even deep in profit, a draining pool means exit now, in full."""
    sig = ExitEngine().evaluate(
        open_position(), ctx("0.00300", liquidity_usd=Decimal(20_000)))
    assert sig.should_exit and sig.sell_fraction == 1.0 and sig.urgent


def test_failed_sell_simulation_is_urgent_full_exit():
    sig = ExitEngine().evaluate(open_position(), ctx("0.00120", sell_simulation_ok=False))
    assert sig.should_exit and sig.urgent and sig.sell_fraction == 1.0


def test_break_even_stop_arms_and_never_loosens():
    eng, pos = ExitEngine(), open_position()
    eng.update_stops(pos, ctx("0.00140"))       # +40% -> break-even armed
    armed = pos.stop_price_usd
    assert pos.break_even_armed and armed >= pos.avg_entry_price_usd
    eng.update_stops(pos, ctx("0.00105"))       # price falls back
    assert pos.stop_price_usd >= armed, "stop must never move down"


def test_trailing_stop_ratchets_up():
    eng, pos = ExitEngine(), open_position()
    eng.update_stops(pos, ctx("0.00200", atr_pct=0.05))
    first = pos.stop_price_usd
    eng.update_stops(pos, ctx("0.00400", atr_pct=0.05))
    assert pos.stop_price_usd > first


def test_time_stop_closes_a_dead_position():
    sig = ExitEngine().evaluate(
        open_position(), ctx("0.00102", now=NOW + timedelta(minutes=50)))
    assert sig.should_exit and sig.sell_fraction == 1.0


def test_healthy_winner_is_held():
    pos = open_position()
    pos.ladder[0].executed = True               # first rung already taken
    sig = ExitEngine().evaluate(pos, ctx("0.00160"))
    assert not sig.should_exit


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #

def test_rsi_bounds_and_monotone_series():
    up = np.linspace(1.0, 2.0, 60)
    down = np.linspace(2.0, 1.0, 60)
    assert ta.rsi(up) > 95
    assert ta.rsi(down) < 5


def test_rsi_returns_none_when_short():
    assert ta.rsi(np.array([1.0, 2.0, 3.0])) is None


def test_ema_tracks_and_lags():
    v = np.concatenate([np.ones(50), np.ones(50) * 2.0])
    e = ta.ema(v, 10)
    assert e[-1] < 2.0 and e[-1] > 1.9


def test_vwap_weights_by_volume():
    arr = np.array([
        [0, 1, 1, 1, 1, 1.0],
        [60, 10, 10, 10, 10, 99.0],
    ])
    assert ta.vwap(arr) > 9.0


def test_atr_is_positive():
    assert ta.atr(synthetic_ohlcv()) > 0


def test_order_flow_imbalance_detects_dust_wash():
    """400 dust buys against 3 large sells must read as distribution."""
    ofi = ta.order_flow_imbalance(buys=400, sells=3, buy_vol=400.0, sell_vol=60_000.0)
    assert ofi < 0, ofi


def test_momentum_is_damped():
    """The score must grow far more slowly than the underlying return.

    A +2%/bar grind and a +25%/bar vertical differ by ~79x in realised 15-bar
    return. If the score also differed by 79x, the sizing layer would pour the
    most capital into the most extended chart — which is precisely how a bot
    buys the exact top of a pump.
    """
    mild_path = 1.02 ** np.arange(40)
    wild_path = 1.25 ** np.arange(40)

    mild = ta.momentum_score(mild_path)
    wild = ta.momentum_score(wild_path)

    return_ratio = (wild_path[-1] / wild_path[-16] - 1) / (mild_path[-1] / mild_path[-16] - 1)
    score_ratio = wild / mild

    assert wild <= 1.0, "score must be bounded"
    assert return_ratio > 50, "fixture should present a large return gap"
    assert score_ratio < 5, f"damping failed: {score_ratio:.1f}x score for {return_ratio:.0f}x return"


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #

def test_max_drawdown_basic():
    mdd, dur = M.max_drawdown(np.array([100.0, 120.0, 90.0, 130.0]))
    assert abs(mdd - 0.25) < 1e-9 and dur > 0


def test_profit_factor_and_win_rate():
    rep = M.analyze([0.5, -0.25, 0.8, -0.25, -0.25],
                    [1000, 1100, 1050, 1300, 1250, 1200])
    assert rep.n_trades == 5
    assert abs(rep.win_rate - 0.4) < 1e-9
    assert rep.profit_factor > 1.0


def test_deflated_sharpe_punishes_many_trials():
    r = np.random.default_rng(3).normal(0.004, 0.03, 250)
    eq = 1000 * np.cumprod(1 + r)
    one = M.analyze([0.1, -0.05] * 40, eq, n_trials=1)
    many = M.analyze([0.1, -0.05] * 40, eq, n_trials=500)
    assert many.deflated_sharpe < one.deflated_sharpe


def test_empty_input_returns_zero_report():
    rep = M.analyze([], [])
    assert rep.n_trades == 0 and rep.sharpe == 0.0


def test_sortino_ignores_upside_volatility():
    r = np.array([0.05, 0.05, 0.05, -0.01, 0.20, 0.05])
    assert M.sortino_ratio(r) > M.sharpe_ratio(r)
