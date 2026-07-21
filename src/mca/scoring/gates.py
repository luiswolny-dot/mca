"""Hard gates: boolean vetoes evaluated *before* any weighted score.

Design decision — why vetoes and not weights
--------------------------------------------
The single most common failure mode in retail trading bots is folding safety
checks into a weighted score. A token with explosive volume and a live mint
authority scores 85/100 and gets bought; the deployer then mints 10x supply and
the position goes to zero. Safety properties are *not* substitutable for
momentum. They are preconditions.

So: gates return a hard pass/fail. A single failure short-circuits to score 0.
The weighted score only ranks the survivors.

Design decision — ``None`` fails
--------------------------------
An unknown value is treated as a failure, not as a pass. If Birdeye is down and
we cannot read the holder distribution, we do not trade. "Fail closed" costs
missed trades; "fail open" costs the account.

Design decision — no unbounded trust in a single source
-------------------------------------------------------
``simulated_sell_ok`` must come from an actual transaction simulation against a
forked/current state (``eth_call`` on EVM, ``simulateTransaction`` on Solana),
never from a third-party "is honeypot" boolean. Third-party scanners are
routinely defeated by tax logic that activates on the Nth sell or above a size
threshold. Simulate with *your* intended size.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from mca.domain.models import Candidate, Chain, GateResult


@dataclass(slots=True)
class GateConfig:
    """All thresholds are configuration, never literals in the logic."""

    min_liquidity_usd: Decimal = Decimal(15_000)
    max_liquidity_usd: Decimal = Decimal(2_000_000)   # too big = we are late
    min_token_age_s: int = 180        # skip the first 3 minutes: snipers own it
    max_token_age_s: int = 172_800    # 48h; after that it is not a fresh setup

    require_mint_authority_revoked: bool = True
    require_freeze_authority_revoked: bool = True
    require_lp_locked_or_burned: bool = True
    min_lp_locked_pct: float = 0.90
    min_lp_lock_remaining_s: int = 86_400

    max_buy_tax_bps: int = 500        # 5%
    max_sell_tax_bps: int = 500
    require_sell_simulation: bool = True

    min_holders: int = 75
    max_top1_holder_pct: float = 0.08
    max_top10_holder_pct: float = 0.30
    max_dev_holding_pct: float = 0.05
    max_bundled_supply_pct: float = 0.25
    max_insider_cluster_pct: float = 0.20

    max_deployer_prior_rugs: int = 0

    min_volume_5m_usd: Decimal = Decimal(5_000)
    min_unique_buyers_5m: int = 25
    max_volume_to_liquidity_ratio: float = 25.0   # >25x in 5m ⇒ wash trading

    # EVM-only
    allow_proxy_upgradeable: bool = False
    allow_blacklist_function: bool = False


def _gate(name: str, ok: bool, detail: str = "", value=None) -> GateResult:
    return GateResult(name=name, passed=ok, detail=detail, value=value)


def _require(name: str, value, predicate, detail: str) -> GateResult:
    """Fail-closed helper: ``None`` never passes."""
    if value is None:
        return _gate(name, False, f"unknown ({detail})", None)
    return _gate(name, bool(predicate(value)), detail, value)


def evaluate_gates(cand: Candidate, cfg: GateConfig) -> list[GateResult]:
    """Run every gate. We deliberately evaluate *all* of them rather than
    short-circuiting, because the failure histogram is the most valuable
    diagnostic we collect: it tells us which filter is doing the work and
    whether a source has silently started returning nulls."""
    sec = cand.security
    mkt = cand.market
    out: list[GateResult] = []

    # --- authorities -------------------------------------------------------
    if cand.token.chain is Chain.SOLANA:
        if cfg.require_mint_authority_revoked:
            out.append(_require("mint_authority_revoked", sec.mint_authority_revoked,
                                lambda v: v is True, "supply must be fixed"))
        if cfg.require_freeze_authority_revoked:
            out.append(_require("freeze_authority_revoked", sec.freeze_authority_revoked,
                                lambda v: v is True, "issuer must not be able to freeze ATAs"))
    else:
        out.append(_require("contract_renounced_or_safe", sec.contract_renounced,
                            lambda v: v is True, "owner can otherwise change taxes"))
        if not cfg.allow_proxy_upgradeable:
            out.append(_require("not_upgradeable", sec.is_proxy_upgradeable,
                                lambda v: v is False, "proxy can be repointed"))
        if not cfg.allow_blacklist_function:
            out.append(_require("no_blacklist_fn", sec.has_blacklist_function,
                                lambda v: v is False, "can block our sell"))
        out.append(_require("no_mint_fn", sec.has_mint_function,
                            lambda v: v is False, "supply must be fixed"))

    # --- liquidity ---------------------------------------------------------
    out.append(_require("min_liquidity", sec.liquidity_usd,
                        lambda v: v >= cfg.min_liquidity_usd,
                        f">= ${cfg.min_liquidity_usd}"))
    out.append(_require("max_liquidity", sec.liquidity_usd,
                        lambda v: v <= cfg.max_liquidity_usd,
                        f"<= ${cfg.max_liquidity_usd} (else we are late)"))

    if cfg.require_lp_locked_or_burned:
        locked = sec.lp_burned is True or (
            sec.lp_locked_pct is not None and sec.lp_locked_pct >= cfg.min_lp_locked_pct
        )
        out.append(_gate("lp_locked_or_burned", locked,
                         f"burned={sec.lp_burned} locked={sec.lp_locked_pct}"))
        if sec.lp_burned is not True and sec.lp_lock_expiry is not None:
            remaining = (sec.lp_lock_expiry - cand.as_of).total_seconds()
            out.append(_gate("lp_lock_duration", remaining >= cfg.min_lp_lock_remaining_s,
                             f"{remaining:.0f}s remaining", remaining))

    # --- transferability ---------------------------------------------------
    out.append(_require("buy_tax", sec.buy_tax_bps,
                        lambda v: v <= cfg.max_buy_tax_bps, f"<= {cfg.max_buy_tax_bps}bps"))
    out.append(_require("sell_tax", sec.sell_tax_bps,
                        lambda v: v <= cfg.max_sell_tax_bps, f"<= {cfg.max_sell_tax_bps}bps"))
    out.append(_require("not_pausable", sec.transfer_pausable,
                        lambda v: v is False, "transfers must not be pausable"))
    if cfg.require_sell_simulation:
        out.append(_require("sell_simulation", sec.simulated_sell_ok,
                            lambda v: v is True, "round-trip simulate at intended size"))

    # --- distribution ------------------------------------------------------
    out.append(_require("min_holders", sec.holder_count,
                        lambda v: v >= cfg.min_holders, f">= {cfg.min_holders}"))
    out.append(_require("top1_holder", sec.top1_holder_pct,
                        lambda v: v <= cfg.max_top1_holder_pct,
                        f"<= {cfg.max_top1_holder_pct:.0%}"))
    out.append(_require("top10_holders", sec.top10_holder_pct,
                        lambda v: v <= cfg.max_top10_holder_pct,
                        f"<= {cfg.max_top10_holder_pct:.0%}"))
    out.append(_require("dev_holding", sec.dev_holding_pct,
                        lambda v: v <= cfg.max_dev_holding_pct,
                        f"<= {cfg.max_dev_holding_pct:.0%}"))
    out.append(_require("bundled_supply", sec.bundled_supply_pct,
                        lambda v: v <= cfg.max_bundled_supply_pct,
                        f"<= {cfg.max_bundled_supply_pct:.0%}"))
    out.append(_require("insider_cluster", sec.insider_cluster_pct,
                        lambda v: v <= cfg.max_insider_cluster_pct,
                        f"<= {cfg.max_insider_cluster_pct:.0%}"))

    # --- provenance --------------------------------------------------------
    out.append(_require("deployer_history", sec.deployer_prior_rugs,
                        lambda v: v <= cfg.max_deployer_prior_rugs,
                        f"<= {cfg.max_deployer_prior_rugs} prior rugs"))

    age = sec.age_seconds(cand.as_of)
    out.append(_require("min_age", age, lambda v: v >= cfg.min_token_age_s,
                        f">= {cfg.min_token_age_s}s"))
    out.append(_require("max_age", age, lambda v: v <= cfg.max_token_age_s,
                        f"<= {cfg.max_token_age_s}s"))

    # --- activity / wash-trading detection ---------------------------------
    vol5 = mkt.volume_usd.get(300)
    out.append(_require("min_volume_5m", vol5,
                        lambda v: v >= cfg.min_volume_5m_usd,
                        f">= ${cfg.min_volume_5m_usd}"))
    out.append(_require("unique_buyers_5m", mkt.unique_buyers_5m,
                        lambda v: v >= cfg.min_unique_buyers_5m,
                        f">= {cfg.min_unique_buyers_5m}"))

    # Volume far in excess of liquidity over a short window is the signature of
    # a wash-trading volume bot, not of organic demand.
    if vol5 is not None and sec.liquidity_usd not in (None, 0):
        ratio = float(vol5) / float(sec.liquidity_usd)
        out.append(_gate("volume_liquidity_sanity",
                         ratio <= cfg.max_volume_to_liquidity_ratio,
                         f"5m vol/liq = {ratio:.1f}x", ratio))
    else:
        out.append(_gate("volume_liquidity_sanity", False, "unknown"))

    return out


def gates_passed(results: list[GateResult]) -> bool:
    return all(r.passed for r in results)
