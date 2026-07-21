"""Unit tests for the execution and ingestion layer.

Same philosophy as ``test_core.py``: builders instead of pytest fixtures, so
these also run under ``scripts/run_tests.py`` with zero dependencies beyond
what's already in the sandbox. Async tests are plain ``async def`` — the
zero-dependency runner detects and awaits them (see ``scripts/run_tests.py``);
pytest handles them natively via ``asyncio_mode = "auto"`` in pyproject.toml.

What is and is not exercised here matters: everything network-facing
(``solana_rpc.py``, ``jupiter.py``, ``telegram.py``) is tested at the pure
request-building / response-parsing boundary with canned fixtures, never
against a live endpoint — this sandbox has no network access. The crypto in
``wallet.py`` is fully exercised end-to-end because ``cryptography`` actually
is installed here.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import tempfile
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from mca.domain.models import (
    Chain, Dex, Fill, PoolCreatedEvent, Side, TokenRef, TradeIntent, TradeMode,
    new_id, utcnow,
)

# --------------------------------------------------------------------------- #
# core.retry
# --------------------------------------------------------------------------- #

from mca.core.retry import RetriesExhausted, RetryConfig, backoff_delays, retry_async


def test_backoff_delays_grow_and_cap():
    cfg = RetryConfig(retries=5, base_delay_s=1.0, factor=2.0, max_delay_s=6.0)
    assert backoff_delays(cfg) == [1.0, 2.0, 4.0, 6.0, 6.0]


async def test_retry_async_succeeds_after_transient_failures():
    calls = {"n": 0}

    class Transient(Exception):
        pass

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise Transient("boom")
        return "ok"

    cfg = RetryConfig(retries=5, base_delay_s=0.001, max_delay_s=0.01, jitter=0)
    result = await retry_async(flaky, cfg=cfg, retryable=(Transient,))
    assert result == "ok" and calls["n"] == 3


async def test_retry_async_raises_retries_exhausted_with_attempt_count():
    class Transient(Exception):
        pass

    async def always_fails():
        raise Transient("nope")

    cfg = RetryConfig(retries=2, base_delay_s=0.001, max_delay_s=0.01, jitter=0)
    try:
        await retry_async(always_fails, cfg=cfg, retryable=(Transient,))
        raise AssertionError("should have raised")
    except RetriesExhausted as exc:
        assert exc.attempts == 3   # first try + 2 retries


async def test_retry_async_does_not_retry_non_retryable():
    class Fatal(Exception):
        pass

    calls = {"n": 0}

    async def fails_fatally():
        calls["n"] += 1
        raise Fatal("no")

    cfg = RetryConfig(retries=5, base_delay_s=0.001)
    try:
        await retry_async(fails_fatally, cfg=cfg, retryable=(ValueError,))
        raise AssertionError("should have raised")
    except Fatal:
        pass
    assert calls["n"] == 1   # no retry attempted


# --------------------------------------------------------------------------- #
# core.config — pure functions only; the Pydantic path needs pydantic, which
# this sandbox does not have installed (see PYDANTIC_AVAILABLE below).
# --------------------------------------------------------------------------- #

from mca.core.config import (
    PYDANTIC_AVAILABLE, MissingEnvVars, deep_merge, load_raw_config, substitute_env,
)


def test_substitute_env_replaces_known_vars():
    result = substitute_env({"a": "${FOO}", "b": [1, "${BAR}"]}, env={"FOO": "x", "BAR": "y"})
    assert result == {"a": "x", "b": [1, "y"]}


def test_substitute_env_collects_every_missing_var_at_once():
    try:
        substitute_env({"a": "${MISSING1}", "b": {"c": "${MISSING2}"}}, env={})
        raise AssertionError("should have raised")
    except MissingEnvVars as exc:
        assert set(exc.names) == {"MISSING1", "MISSING2"}


def test_deep_merge_overrides_nested_without_mutating_base():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    override = {"a": {"y": 20}}
    merged = deep_merge(base, override)
    assert merged == {"a": {"x": 1, "y": 20}, "b": 3}
    assert base == {"a": {"x": 1, "y": 2}, "b": 3}


def test_load_raw_config_reads_yaml_and_substitutes_env():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "config.yaml"
        p.write_text("system:\n  mode: paper\nchains:\n  solana:\n    rpc_http: ${RPC}\n")
        raw = load_raw_config(p, local_override=Path(d) / "missing.yaml", env={"RPC": "http://x"})
        assert raw["chains"]["solana"]["rpc_http"] == "http://x"


def test_app_config_converters_when_pydantic_available():
    """Guards itself: passes trivially where pydantic is not installed, so this
    still runs cleanly in this sandbox while documenting the intended contract
    for an environment that has the full dependency set."""
    if not PYDANTIC_AVAILABLE:
        return
    from mca.core.config import AppConfig
    raw = {
        "scoring": {"weights": {"liquidity_depth": 16, "volume_momentum": 20,
                                "holder_dynamics": 14, "smart_money_flow": 18,
                                "technicals": 15, "social": 9, "safety_margin": 8}},
    }
    cfg = AppConfig.model_validate(raw)
    gate_cfg = cfg.to_gate_config()
    assert gate_cfg.min_liquidity_usd == Decimal("15000")
    scorer_cfg = cfg.to_scorer_config()
    assert scorer_cfg.weights.liquidity_depth == 16
    exec_cfg = cfg.to_execution_config(Chain.SOLANA)
    assert exec_cfg.hot_wallet_max_usd == Decimal("2000")


# --------------------------------------------------------------------------- #
# core.bus
# --------------------------------------------------------------------------- #

from mca.core.bus import BusError, decode_message, encode_message


def test_encode_decode_message_round_trip_including_decimal():
    payload = {"a": 1, "b": "x", "c": Decimal("1.5")}
    fields = encode_message(payload)
    assert fields["schema_version"] == "1"
    decoded = decode_message(fields)
    assert decoded == {"a": 1, "b": "x", "c": "1.5"}


def test_decode_message_rejects_future_schema_version():
    try:
        decode_message({"schema_version": "99", "data": "{}"})
        raise AssertionError("should have raised")
    except BusError:
        pass


def test_decode_message_accepts_bytes_fields():
    fields = {b"schema_version": b"1", b"data": b'{"x":1}'}
    assert decode_message(fields) == {"x": 1}


# --------------------------------------------------------------------------- #
# execution.wallet — fully exercised: cryptography is installed here.
# --------------------------------------------------------------------------- #

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from mca.execution.wallet import (
    InvalidPassphrase, WalletManager, b58encode, hot_wallet_sweep_amount_usd,
)


def test_b58encode_zero_bytes_become_leading_ones():
    assert b58encode(b"\x00") == "1"
    assert b58encode(b"\x00\x00\x00") == "111"


def test_b58encode_single_byte_known_value():
    assert b58encode(b"\x01") == "2"   # alphabet[1] == '2'


def test_b58encode_classic_test_vector():
    raw = bytes.fromhex("00010966776006953D5567439E5E39F86A0D273BEED61967F6")
    assert b58encode(raw) == "16UwLL9Risc3QfPqBUvKofHmBQ7wMtjvM"


def test_keystore_round_trip_32_byte_seed():
    secret = os.urandom(32)
    ks = WalletManager.create(secret, "correct horse battery staple")
    assert WalletManager.unlock_raw(ks, "correct horse battery staple") == secret


def test_keystore_round_trip_64_byte_secret():
    seed = os.urandom(32)
    priv = Ed25519PrivateKey.from_private_bytes(seed)
    pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    secret64 = seed + pub
    ks = WalletManager.create(secret64, "pw")
    assert WalletManager.unlock_raw(ks, "pw") == secret64


def test_keystore_pubkey_hint_is_stable_and_solana_shaped():
    seed = os.urandom(32)
    ks = WalletManager.create(seed, "pw")
    assert 32 <= len(ks.pubkey) <= 48
    assert WalletManager.create(seed, "pw2").pubkey == ks.pubkey  # same key -> same hint


def test_wrong_passphrase_is_rejected():
    ks = WalletManager.create(os.urandom(32), "right")
    try:
        WalletManager.unlock_raw(ks, "wrong")
        raise AssertionError("should have raised")
    except InvalidPassphrase:
        pass


def test_tampered_ciphertext_is_rejected():
    ks = WalletManager.create(os.urandom(32), "pw")
    tampered = bytearray(base64.b64decode(ks.ciphertext))
    tampered[0] ^= 0xFF
    ks2 = replace(ks, ciphertext=base64.b64encode(bytes(tampered)).decode())
    try:
        WalletManager.unlock_raw(ks2, "pw")
        raise AssertionError("should have raised")
    except InvalidPassphrase:
        pass


def test_keystore_save_load_round_trip_and_permissions():
    ks = WalletManager.create(os.urandom(32), "pw")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "keystore.json"
        ks.save(p)
        assert oct(p.stat().st_mode)[-3:] == "600"
        loaded = ks.__class__.load(p)
        assert loaded == ks


def test_hot_wallet_sweep_amount_zero_below_threshold():
    from mca.execution.base import ExecutionConfig
    cfg = ExecutionConfig(hot_wallet_max_usd=Decimal(2000), sweep_threshold_usd=Decimal(1000))
    assert hot_wallet_sweep_amount_usd(Decimal(500), cfg) == Decimal(0)


def test_hot_wallet_sweep_amount_sweeps_excess_above_cap():
    from mca.execution.base import ExecutionConfig
    cfg = ExecutionConfig(hot_wallet_max_usd=Decimal(2000), sweep_threshold_usd=Decimal(1000))
    assert hot_wallet_sweep_amount_usd(Decimal(3500), cfg) == Decimal(1500)


# --------------------------------------------------------------------------- #
# execution.solana_rpc — pure request/response functions only.
# --------------------------------------------------------------------------- #

from mca.execution.solana_rpc import (
    RpcResponseError, build_request, parse_latest_blockhash, parse_log_notification,
    parse_response, parse_signature_statuses, parse_simulate_transaction,
)


def test_build_request_shape():
    req = build_request("getBalance", ["abc"], request_id=5)
    assert req == {"jsonrpc": "2.0", "id": 5, "method": "getBalance", "params": ["abc"]}


def test_parse_response_raises_on_error_field():
    try:
        parse_response({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "boom"}})
        raise AssertionError("should have raised")
    except RpcResponseError as exc:
        assert exc.code == -32000


def test_parse_latest_blockhash_extracts_fields():
    raw = {"result": {"context": {"slot": 1},
                      "value": {"blockhash": "abc", "lastValidBlockHeight": 999}}}
    bh = parse_latest_blockhash(raw)
    assert bh.blockhash == "abc" and bh.last_valid_block_height == 999


def test_parse_simulate_transaction_ok():
    raw = {"result": {"context": {}, "value": {"err": None, "logs": ["a"], "unitsConsumed": 500}}}
    sim = parse_simulate_transaction(raw)
    assert sim.ok and sim.units_consumed == 500


def test_parse_simulate_transaction_reports_error():
    raw = {"result": {"context": {},
                      "value": {"err": {"InstructionError": [0, "Custom"]}, "logs": []}}}
    sim = parse_simulate_transaction(raw)
    assert not sim.ok and sim.error is not None


def test_parse_signature_statuses_mixed_known_and_unknown():
    raw = {"result": {"context": {}, "value": [
        {"confirmations": 3, "confirmationStatus": "confirmed", "err": None, "slot": 42},
        None,
    ]}}
    out = parse_signature_statuses(raw, ["sig1", "sig2"])
    assert out[0].confirmation_status == "confirmed" and out[0].slot == 42
    assert out[1].confirmation_status is None


def test_parse_log_notification_extracts_fields():
    msg = {"method": "logsNotification", "params": {"result": {
        "context": {"slot": 7}, "value": {"signature": "sig", "err": None, "logs": ["x"]}}}}
    assert parse_log_notification(msg) == {
        "signature": "sig", "err": None, "logs": ["x"], "slot": 7}


def test_parse_log_notification_ignores_other_methods():
    assert parse_log_notification({"method": "subscriptionAck"}) is None


# --------------------------------------------------------------------------- #
# execution.jupiter — pure request/response functions only.
# --------------------------------------------------------------------------- #

from mca.execution.jupiter import (
    JupiterError, NoRouteFound, build_quote_params, build_swap_request_body,
    micro_lamports_to_priority_ceiling, parse_quote_response, parse_swap_response,
)

_QUOTE_FIXTURE = {
    "inputMint": "A", "inAmount": "1000", "outputMint": "B", "outAmount": "2000",
    "otherAmountThreshold": "1980", "swapMode": "ExactIn", "slippageBps": 50,
    "priceImpactPct": "0.01", "routePlan": ["hop1"],
}


def test_build_quote_params_rejects_non_positive_amount():
    try:
        build_quote_params("A", "B", 0, 50)
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_parse_quote_response_extracts_fields_and_keeps_raw():
    q = parse_quote_response(_QUOTE_FIXTURE)
    assert q.in_amount == 1000 and q.out_amount == 2000
    assert q.raw is _QUOTE_FIXTURE   # identity, not a copy — /swap needs the exact object


def test_parse_quote_response_raises_no_route_when_empty():
    try:
        parse_quote_response({})
        raise AssertionError("should have raised")
    except NoRouteFound:
        pass


def test_build_swap_request_body_embeds_raw_quote_and_priority_ceiling():
    q = parse_quote_response(_QUOTE_FIXTURE)
    body = build_swap_request_body(q, "pubkey123", max_priority_lamports=999)
    assert body["quoteResponse"] is q.raw
    assert body["userPublicKey"] == "pubkey123"
    assert body["prioritizationFeeLamports"]["priorityLevelWithMaxLamports"]["maxLamports"] == 999


def test_parse_swap_response_requires_transaction_field():
    try:
        parse_swap_response({})
        raise AssertionError("should have raised")
    except JupiterError:
        pass


def test_parse_swap_response_extracts_fields():
    sw = parse_swap_response({"swapTransaction": "BASE64", "lastValidBlockHeight": 123,
                              "prioritizationFeeLamports": 456, "computeUnitLimit": 789})
    assert sw.swap_transaction_b64 == "BASE64" and sw.last_valid_block_height == 123


def test_micro_lamports_to_priority_ceiling_conversion():
    assert micro_lamports_to_priority_ceiling(250_000, 400_000) == 100_000


# --------------------------------------------------------------------------- #
# execution.base — retryable vs. fatal classification actually changes behaviour
# --------------------------------------------------------------------------- #

from mca.execution.base import (
    Executor, ExecutionConfig, FatalExecutionError, LiveExecutorGuard,
    ModeViolation, RetryableExecutionError, ShadowRecord,
)


def _intent(side: Side = Side.BUY, **overrides) -> TradeIntent:
    base = dict(
        intent_id=new_id(), token=TokenRef(Chain.SOLANA, "TokenMint111111111111111111111111111111111"),
        side=side, quote_amount=Decimal("0.1") if side is Side.BUY else None,
        base_amount=None if side is Side.BUY else 1_000_000,
        max_slippage_bps=500, idempotency_key=new_id(), mode=TradeMode.PAPER,
    )
    base.update(overrides)
    return TradeIntent(**base)


def _fill() -> Fill:
    return Fill(fill_id=new_id(), intent_id="x", token=TokenRef(Chain.SOLANA, "T"),
               side=Side.BUY, base_amount=1, quote_amount=Decimal("0.1"),
               price_usd=Decimal("1"), tx_hash="sig", block_or_slot=1,
               gas_or_fee_usd=Decimal("0.01"), slippage_bps=50, filled_at=utcnow())


async def test_execute_retries_only_retryable_errors():
    calls = {"n": 0}

    class DummyExecutor(Executor):
        async def quote(self, token, side, amount):
            raise NotImplementedError

        async def simulate(self, intent):
            return True, "ok"

        async def submit(self, intent):
            calls["n"] += 1
            if calls["n"] < 2:
                raise RetryableExecutionError("transient")
            return _fill()

    cfg = ExecutionConfig(max_retries=3, retry_backoff_s=0.001)
    ex = DummyExecutor(Chain.SOLANA, TradeMode.PAPER, cfg)
    fill = await ex.execute(_intent())
    assert fill is not None and calls["n"] == 2


async def test_execute_does_not_retry_fatal_errors():
    calls = {"n": 0}

    class DummyExecutor(Executor):
        async def quote(self, token, side, amount):
            raise NotImplementedError

        async def simulate(self, intent):
            return True, "ok"

        async def submit(self, intent):
            calls["n"] += 1
            raise FatalExecutionError("insufficient funds")

    cfg = ExecutionConfig(max_retries=5, retry_backoff_s=0.001)
    ex = DummyExecutor(Chain.SOLANA, TradeMode.PAPER, cfg)
    try:
        await ex.execute(_intent())
        raise AssertionError("should have raised")
    except FatalExecutionError:
        pass
    assert calls["n"] == 1   # no retry burned on a non-transient failure


# --------------------------------------------------------------------------- #
# execution.solana_executor — the shadow-trading mandate and reference-price
# guard, both enforceable without a real RPC/Jupiter/solders dependency.
# --------------------------------------------------------------------------- #

from mca.execution.solana_executor import MissingReferencePrice, SolanaJupiterExecutor


class _FakeKeypair:
    def pubkey(self) -> str:
        return "FAKEPUBKEY1111111111111111111111111111111"


def test_live_executor_rejects_missing_shadow_record():
    cfg = ExecutionConfig()
    try:
        SolanaJupiterExecutor(TradeMode.LIVE, cfg, rpc=None, jupiter=None,
                              keypair=_FakeKeypair(), shadow_record=None, strategy_hash="abc")
        raise AssertionError("should have raised")
    except ModeViolation:
        pass


def test_live_executor_allowed_with_satisfied_shadow_record():
    cfg = ExecutionConfig(min_shadow_days=1, min_shadow_trades=1)
    record = ShadowRecord(strategy_hash="abc", started_at=utcnow() - timedelta(days=2),
                          paper_trades=10, last_config_change_at=utcnow() - timedelta(days=3))
    ex = SolanaJupiterExecutor(TradeMode.LIVE, cfg, rpc=None, jupiter=None,
                               keypair=_FakeKeypair(), shadow_record=record, strategy_hash="abc")
    assert ex.mode is TradeMode.LIVE


def test_paper_executor_needs_no_shadow_record():
    ex = SolanaJupiterExecutor(TradeMode.PAPER, ExecutionConfig(), rpc=None, jupiter=None,
                               keypair=_FakeKeypair())
    assert ex.mode is TradeMode.PAPER


async def test_submit_refuses_without_reference_price():
    ex = SolanaJupiterExecutor(TradeMode.PAPER, ExecutionConfig(), rpc=None, jupiter=None,
                               keypair=_FakeKeypair())
    try:
        await ex.submit(_intent())
        raise AssertionError("should have raised")
    except MissingReferencePrice:
        pass


async def test_quote_refuses_without_reference_price():
    ex = SolanaJupiterExecutor(TradeMode.PAPER, ExecutionConfig(), rpc=None, jupiter=None,
                               keypair=_FakeKeypair())
    try:
        await ex.quote(TokenRef(Chain.SOLANA, "T"), Side.BUY, Decimal("0.1"))
        raise AssertionError("should have raised")
    except MissingReferencePrice:
        pass


# --------------------------------------------------------------------------- #
# ingest.solana_pools — the fail-closed extraction logic
# --------------------------------------------------------------------------- #

from mca.execution.jupiter import WSOL_MINT
from mca.ingest.solana_pools import (
    extract_pool_from_transaction, looks_like_pool_creation, payload_to_event,
    _event_to_payload,
)


def _dummy_pubkey(n: int) -> str:
    return f"Addr{n:039d}"


def _fixture_raydium_init2_tx(coin_mint: str | None = None, pc_mint: str | None = None,
                              err=None) -> dict:
    keys = [_dummy_pubkey(i) for i in range(15)]
    keys[4] = "PoolAmmAddress11111111111111111111111111111"
    keys[8] = coin_mint or "TokenMint1111111111111111111111111111111111"
    keys[9] = pc_mint or WSOL_MINT
    return {"slot": 123_456, "meta": {"err": err},
           "transaction": {"message": {"accountKeys": keys}}}


def test_looks_like_pool_creation_matches_init_marker():
    logs = ["Program log: ray_log", "Program log: initialize2: InitializeInstruction2 {}"]
    assert looks_like_pool_creation(logs, Dex.RAYDIUM)


def test_looks_like_pool_creation_rejects_ordinary_swap_logs():
    logs = ["Program log: ray_log", "Program log: SwapBaseIn"]
    assert not looks_like_pool_creation(logs, Dex.RAYDIUM)


def test_extract_pool_from_transaction_success():
    event = extract_pool_from_transaction(_fixture_raydium_init2_tx(), Dex.RAYDIUM, "sig123")
    assert event is not None
    assert event.dex is Dex.RAYDIUM
    assert event.quote_token.address == WSOL_MINT
    assert event.pool_address == "PoolAmmAddress11111111111111111111111111111"


def test_extract_pool_from_transaction_drops_non_sol_quoted_pool():
    tx = _fixture_raydium_init2_tx(coin_mint="NotSolA1111111111111111111111111111111111",
                                   pc_mint="NotSolB1111111111111111111111111111111111")
    assert extract_pool_from_transaction(tx, Dex.RAYDIUM, "sig") is None


def test_extract_pool_from_transaction_drops_failed_transaction():
    tx = _fixture_raydium_init2_tx(err={"InstructionError": [0, "Custom"]})
    assert extract_pool_from_transaction(tx, Dex.RAYDIUM, "sig") is None


def test_extract_pool_from_transaction_fails_closed_on_malformed_shape():
    assert extract_pool_from_transaction({"transaction": {}}, Dex.RAYDIUM, "sig") is None
    assert extract_pool_from_transaction(None, Dex.RAYDIUM, "sig") is None


def test_pool_event_payload_round_trip():
    event = PoolCreatedEvent(
        token=TokenRef(Chain.SOLANA, "TokenMint1111111111111111111111111111111111"),
        quote_token=TokenRef(Chain.SOLANA, WSOL_MINT), dex=Dex.RAYDIUM,
        pool_address="Pool1111111111111111111111111111111111111", detected_at=utcnow(),
        tx_signature="sig", block_or_slot=42, deployer="Dep1111111111111111111111111111111111111",
    )
    restored = payload_to_event(_event_to_payload(event))
    assert restored.token == event.token
    assert restored.pool_address == event.pool_address
    assert restored.tx_signature == event.tx_signature


# --------------------------------------------------------------------------- #
# alerts.telegram
# --------------------------------------------------------------------------- #

from mca.alerts.notifier import Alert, Severity
from mca.alerts.telegram import (
    TelegramError, TelegramTransportError, build_send_message_request,
    format_alert_text, parse_send_response,
)


def test_format_alert_text_includes_title_and_body():
    text = format_alert_text(Alert(Severity.CRITICAL, "Title", "Body text"))
    assert "Title" in text and "Body text" in text


def test_build_send_message_request_shape():
    url, body = build_send_message_request("TOKEN123", "chat1", "hello")
    assert url == "https://api.telegram.org/botTOKEN123/sendMessage"
    assert body["chat_id"] == "chat1" and body["text"] == "hello"


def test_build_send_message_request_truncates_long_text():
    _, body = build_send_message_request("T", "c", "x" * 5000)
    assert len(body["text"]) <= 3900


def test_build_send_message_request_rejects_empty_token():
    try:
        build_send_message_request("", "c", "x")
        raise AssertionError("should have raised")
    except ValueError:
        pass


def test_parse_send_response_ok_does_not_raise():
    parse_send_response(200, {"ok": True})


def test_parse_send_response_rate_limited_is_retryable():
    try:
        parse_send_response(429, {"description": "too many requests"})
        raise AssertionError("should have raised")
    except TelegramTransportError:
        pass


def test_parse_send_response_bad_request_is_fatal():
    try:
        parse_send_response(400, {"description": "chat not found"})
        raise AssertionError("should have raised")
    except TelegramError:
        pass
