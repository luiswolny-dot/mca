"""Tests for enrich/onchain.py, enrich/dexscreener.py, enrich/worker.py's
codecs, and scoring/worker.py's pure helpers."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mca.domain.models import (
    Candidate, Chain, Dex, MarketSnapshot, PoolCreatedEvent, SecurityReport,
    TokenRef, new_id, utcnow,
)

TOK = TokenRef(Chain.SOLANA, "TokenMint1111111111111111111111111111111111")

# --------------------------------------------------------------------------- #
# enrich.onchain
# --------------------------------------------------------------------------- #

from mca.enrich.onchain import (
    TOKEN_2022_PROGRAM_ID, TOKEN_PROGRAM_ID, compute_top_holder_pcts,
    parse_mint_authorities, parse_supply,
)


def _mint_account(owner=TOKEN_PROGRAM_ID, mint_authority=None, freeze_authority=None,
                  decimals=9, mint_type="mint"):
    return {
        "owner": owner,
        "data": {"parsed": {"type": mint_type,
                            "info": {"mintAuthority": mint_authority,
                                    "freezeAuthority": freeze_authority,
                                    "decimals": decimals, "supply": "1000000000000000"}}},
    }


def test_parse_mint_authorities_both_revoked():
    revoked_mint, revoked_freeze, decimals = parse_mint_authorities(_mint_account())
    assert revoked_mint is True and revoked_freeze is True and decimals == 9


def test_parse_mint_authorities_still_held_is_not_revoked():
    acct = _mint_account(mint_authority="SomeDeployerPubkey1111111111111111111111111")
    revoked_mint, revoked_freeze, _ = parse_mint_authorities(acct)
    assert revoked_mint is False
    assert revoked_freeze is True   # freeze_authority still None in this fixture


def test_parse_mint_authorities_none_account_is_unknown_not_safe():
    assert parse_mint_authorities(None) == (None, None, None)


def test_parse_mint_authorities_rejects_unknown_owner_program():
    """Fail closed rather than assume a non-Token-program account layout
    matches the SPL Mint struct by coincidence."""
    acct = _mint_account(owner="SomeRandomProgram1111111111111111111111111")
    assert parse_mint_authorities(acct) == (None, None, None)


def test_parse_mint_authorities_accepts_token_2022():
    acct = _mint_account(owner=TOKEN_2022_PROGRAM_ID)
    revoked_mint, revoked_freeze, decimals = parse_mint_authorities(acct)
    assert revoked_mint is True and decimals == 9


def test_parse_supply_reads_from_parsed_mint():
    assert parse_supply(_mint_account()) == 1_000_000_000_000_000


def test_parse_supply_none_account_is_unknown():
    assert parse_supply(None) is None


def test_parse_supply_does_not_crash_on_unparseable_data_shape():
    """jsonParsed falls back to the raw ``["<base64>", "base64"]`` tuple form
    when the account's owner isn't a program the RPC node can parse — e.g. a
    garbage/non-mint address from a bad extraction upstream. This crashed the
    enricher in production before the isinstance guard was added."""
    acct = {"owner": "SomeRandomProgram1111111111111111111111111",
           "data": ["some-base64-blob==", "base64"]}
    assert parse_supply(acct) is None


def test_parse_supply_missing_data_key_is_unknown():
    assert parse_supply({"owner": TOKEN_PROGRAM_ID}) is None


def test_compute_top_holder_pcts_excludes_pool_vault():
    supply = 1_000_000
    accounts = [
        {"address": "PoolVaultXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "amount": "900000"},
        {"address": "RealWhale111111111111111111111111111111111", "amount": "50000"},
        {"address": "SmallHolder11111111111111111111111111111111", "amount": "1000"},
    ]
    top1, top10 = compute_top_holder_pcts(accounts, supply,
                                          exclude_addresses={"PoolVaultXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"})
    assert abs(top1 - 0.05) < 1e-9   # 50000/1000000, NOT 900000/1000000
    assert abs(top10 - 0.051) < 1e-9


def test_compute_top_holder_pcts_without_exclusion_would_show_the_pool_itself():
    """Documents why exclusion matters: without it, every fresh pool reads as
    a ~90% single-holder concentration."""
    accounts = [{"address": "PoolVault", "amount": "900000"}]
    top1, _ = compute_top_holder_pcts(accounts, 1_000_000, exclude_addresses=set())
    assert top1 > 0.85


def test_compute_top_holder_pcts_zero_supply_is_unknown():
    assert compute_top_holder_pcts([], 0, set()) == (None, None)


# --------------------------------------------------------------------------- #
# enrich.dexscreener
# --------------------------------------------------------------------------- #

from mca.enrich.dexscreener import pair_created_at, parse_pair_to_market_snapshot

_PAIR_FIXTURE = {
    "chainId": "solana", "dexId": "raydium", "pairAddress": "Pool111111111111111111111111111111111111",
    "baseToken": {"address": TOK.address, "name": "Test", "symbol": "TST"},
    "quoteToken": {"address": "So11111111111111111111111111111111111111112", "name": "Wrapped SOL", "symbol": "SOL"},
    "priceNative": "0.002", "priceUsd": "0.30",
    "txns": {"m5": {"buys": 12, "sells": 3}, "h1": {"buys": 80, "sells": 20},
            "h6": {"buys": 200, "sells": 100}, "h24": {"buys": 500, "sells": 300}},
    "volume": {"m5": 1200.5, "h1": 8000.0, "h6": 30000.0, "h24": 90000.0},
    "priceChange": {"m5": 2.1, "h1": 15.0, "h6": 40.0, "h24": 120.0},
    "liquidity": {"usd": 42000.0, "base": 70000.0, "quote": 140.0},
    "fdv": 300000.0, "pairCreatedAt": 1753100000000,
}


def test_parse_pair_maps_all_windows():
    snap = parse_pair_to_market_snapshot(_PAIR_FIXTURE, TOK, token_decimals=9)
    assert snap is not None
    assert snap.price_usd == Decimal("0.30")
    assert snap.liquidity_usd == Decimal("42000.0")
    assert snap.volume_usd[300] == Decimal("1200.5")
    assert snap.volume_usd[86400] == Decimal("90000.0")
    assert snap.buys[300] == 12 and snap.sells[300] == 3
    assert snap.price_change_pct[3600] == 15.0


def test_parse_pair_converts_liquidity_base_to_raw_units_using_decimals():
    """liquidity.base is decimal-adjusted (70000.0 whole tokens); pool_reserve_base
    must be raw base units — the exact unit class of bug this project keeps
    finding and fixing elsewhere."""
    snap = parse_pair_to_market_snapshot(_PAIR_FIXTURE, TOK, token_decimals=9)
    assert snap.pool_reserve_base == 70000 * 10**9
    assert snap.pool_reserve_quote == int(140.0 * 10**9)


def test_parse_pair_returns_none_for_missing_price():
    fixture = dict(_PAIR_FIXTURE, priceUsd=None)
    assert parse_pair_to_market_snapshot(fixture, TOK) is None


def test_parse_pair_returns_none_for_missing_liquidity():
    fixture = {**_PAIR_FIXTURE, "liquidity": None}
    assert parse_pair_to_market_snapshot(fixture, TOK) is None


def test_parse_pair_returns_none_for_none_pair():
    assert parse_pair_to_market_snapshot(None, TOK) is None


def test_parse_pair_without_decimals_leaves_reserve_base_none():
    """Honest gap, not a fabricated zero: without decimals we cannot convert
    liquidity.base to raw units, so pool_reserve_base stays unset rather than
    silently wrong."""
    snap = parse_pair_to_market_snapshot(_PAIR_FIXTURE, TOK, token_decimals=None)
    assert snap.pool_reserve_base is None
    assert snap.pool_reserve_quote is not None   # quote side (SOL, 9 decimals) is always known


def test_pair_created_at_converts_epoch_ms():
    dt = pair_created_at(_PAIR_FIXTURE)
    assert dt.tzinfo is not None
    assert dt.year == 2025 or dt.year == 2026   # sanity: not 1970 or a far future date


def test_pair_created_at_missing_is_none():
    assert pair_created_at({}) is None


# --------------------------------------------------------------------------- #
# enrich.worker — Candidate <-> payload codec
# --------------------------------------------------------------------------- #

from mca.enrich.worker import candidate_to_payload, payload_to_candidate


def _candidate() -> Candidate:
    market = MarketSnapshot(token=TOK, as_of=utcnow(), price_usd=Decimal("0.42"),
                            liquidity_usd=Decimal("35000"), volume_usd={300: Decimal("500")},
                            buys={300: 5}, sells={300: 1}, price_change_pct={300: 3.2},
                            pool_reserve_base=1_000_000_000, pool_reserve_quote=5_000_000_000)
    security = SecurityReport(token=TOK, mint_authority_revoked=True, freeze_authority_revoked=True,
                              top1_holder_pct=0.04, top10_holder_pct=0.12, liquidity_usd=Decimal("35000"),
                              deployer="Deployer111111111111111111111111111111111", created_at=utcnow())
    return Candidate(token=TOK, as_of=utcnow(), market=market, security=security, correlation_id=new_id())


def test_candidate_payload_round_trip():
    cand = _candidate()
    restored = payload_to_candidate(candidate_to_payload(cand))
    assert restored.token == cand.token
    assert restored.market.price_usd == cand.market.price_usd
    assert restored.market.liquidity_usd == cand.market.liquidity_usd
    assert restored.security.mint_authority_revoked is True
    assert restored.security.top10_holder_pct == 0.12
    assert restored.social is None and restored.flow is None


def test_candidate_payload_round_trip_preserves_volume_windows():
    cand = _candidate()
    restored = payload_to_candidate(candidate_to_payload(cand))
    assert restored.market.volume_usd[300] == Decimal("500")
    assert restored.market.buys[300] == 5


# --------------------------------------------------------------------------- #
# ingest.solana_pools — base_vault extraction (added this round)
# --------------------------------------------------------------------------- #

from mca.enrich.dexscreener import BASE_URL  # noqa: F401 (import smoke check)
from mca.ingest.solana_pools import extract_pool_from_transaction, RAYDIUM_AMM_V4
from mca.execution.jupiter import WSOL_MINT


def _dummy_pubkey(n: int) -> str:
    return f"Addr{n:039d}"


def _fixture_tx_with_vaults():
    # Instruction-scoped accounts (see _instruction_accounts in
    # solana_pools.py) — position matches _RAYDIUM_INIT2_ACCOUNTS, not a
    # transaction-wide accountKeys list.
    accounts = [_dummy_pubkey(i) for i in range(15)]
    accounts[4] = "PoolAmmAddress11111111111111111111111111111"
    accounts[8] = "TokenMint1111111111111111111111111111111111"   # coin_mint (base)
    accounts[9] = WSOL_MINT                                        # pc_mint (quote)
    accounts[10] = "CoinVaultAddress111111111111111111111111111"   # base vault
    accounts[11] = "PcVaultAddress1111111111111111111111111111"
    ix = {"programId": RAYDIUM_AMM_V4, "accounts": accounts}
    return {"slot": 1, "meta": {"err": None}, "transaction": {"message": {"instructions": [ix]}}}


def test_extract_pool_populates_base_vault_for_the_base_side():
    event = extract_pool_from_transaction(_fixture_tx_with_vaults(), RAYDIUM_AMM_V4, Dex.RAYDIUM, "sig")
    assert event is not None
    assert event.base_vault == "CoinVaultAddress111111111111111111111111111"


# --------------------------------------------------------------------------- #
# scoring.worker
# --------------------------------------------------------------------------- #

from mca.scoring.worker import RollingCohort, _quote_usd_price, _reserve_quote_usd


def test_rolling_cohort_produces_percentiles_after_warmup():
    cohort = RollingCohort(window=50)
    for i in range(40):
        cand = _candidate()
        cand.market.liquidity_usd = Decimal(str(10_000 + i * 100))
        cohort.observe(cand)
    stats = cohort.snapshot()
    # 40 >= 30 samples -> no longer forced neutral
    p = stats.percentile_of("liquidity_usd", 10_000 + 39 * 100)
    assert p > 0.9   # the highest value observed should land near the top percentile


def test_rolling_cohort_neutral_before_warmup():
    cohort = RollingCohort(window=50)
    for i in range(5):
        cohort.observe(_candidate())
    stats = cohort.snapshot()
    assert stats.percentile_of("liquidity_usd", 999_999) == 0.5


def test_rolling_cohort_is_bounded():
    cohort = RollingCohort(window=10)
    for _ in range(100):
        cohort.observe(_candidate())
    assert len(cohort._buffers["liquidity_usd"]) == 10


def test_reserve_quote_usd_is_half_of_total_liquidity():
    cand = _candidate()
    assert _reserve_quote_usd(cand) == Decimal("35000") / 2


def test_quote_usd_price_derives_from_reserve_ratio():
    cand = _candidate()   # pool_reserve_base=1e9 (base units), pool_reserve_quote=5e9 (lamports)
    price = _quote_usd_price(cand)
    assert price > 0


def test_quote_usd_price_zero_when_reserves_unknown():
    cand = _candidate()
    cand.market.pool_reserve_base = None
    assert _quote_usd_price(cand) == Decimal(0)
