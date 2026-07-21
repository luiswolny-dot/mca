"""Solana RPC: JSON-RPC over HTTP, plus a WebSocket log-subscription client.

Design decision — request/response shaping is pure, transport is not
------------------------------------------------------------------------
Every RPC call is split into a pure function that builds the JSON-RPC body or
parses the JSON-RPC response, and a thin async method that calls ``httpx`` and
delegates to those pure functions. ``httpx`` is not installed in every
environment that imports this module (the scorer and backtester never should),
so the import is guarded and only the transport methods fail — at call time,
with a clear message — if it is missing. The pure functions are exercised by
unit tests with canned response fixtures; nothing here has been run against a
live RPC endpoint, because this sandbox has no network access. Point this at
devnet with a throwaway key before trusting it with anything real.

Design decision — we simulate before we send, always
--------------------------------------------------------
``send_transaction`` defaults to ``skip_preflight=True`` on the assumption that
the caller already ran ``simulate_transaction`` with full checks — running
preflight simulation twice per trade doubles a cost that matters when latency
is the difference between a fill and a fail. This is a policy the *caller*
(``execution/solana_executor.py``) is responsible for actually following; this
client itself will happily skip preflight if asked to, deliberately, because a
client should not silently override what it is told.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from typing import Any

from mca.core.retry import RetryConfig, retry_async

log = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]


class RpcError(Exception):
    """Base for RPC failures."""


class RpcTransportError(RpcError):
    """Network-level failure: timeout, connection reset, 5xx. Retryable."""


class RpcResponseError(RpcError):
    """The node answered with a JSON-RPC error object. Usually not retryable —
    an invalid parameter will still be invalid on attempt two."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# Pure: request building / response parsing
# --------------------------------------------------------------------------- #

def build_request(method: str, params: list[Any], request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def parse_response(raw: dict) -> Any:
    """Unwrap a JSON-RPC envelope, raising :class:`RpcResponseError` on an
    ``error`` field. Returns the ``result`` value otherwise (``.value`` for
    the common ``{"context":..., "value":...}`` shape is *not* unwrapped here
    — callers that need the value do that themselves, since some methods
    return the context-wrapped shape and some do not)."""
    if "error" in raw and raw["error"] is not None:
        err = raw["error"]
        raise RpcResponseError(err.get("code", -1), err.get("message", "unknown error"))
    return raw.get("result")


@dataclass(slots=True, frozen=True)
class Blockhash:
    blockhash: str
    last_valid_block_height: int


def parse_latest_blockhash(raw: dict) -> Blockhash:
    result = parse_response(raw)
    value = result["value"]
    return Blockhash(blockhash=value["blockhash"],
                     last_valid_block_height=value["lastValidBlockHeight"])


@dataclass(slots=True, frozen=True)
class SimulationResult:
    ok: bool
    error: str | None
    logs: list[str]
    units_consumed: int | None


def parse_simulate_transaction(raw: dict) -> SimulationResult:
    result = parse_response(raw)
    value = result["value"]
    err = value.get("err")
    return SimulationResult(
        ok=err is None,
        error=None if err is None else str(err),
        logs=value.get("logs") or [],
        units_consumed=value.get("unitsConsumed"),
    )


@dataclass(slots=True, frozen=True)
class SignatureStatus:
    signature: str
    confirmations: int | None
    confirmation_status: str | None     # "processed" | "confirmed" | "finalized"
    err: str | None
    slot: int | None = None


def parse_signature_statuses(raw: dict, signatures: list[str]) -> list[SignatureStatus]:
    result = parse_response(raw)
    values = result["value"]
    out = []
    for sig, v in zip(signatures, values, strict=True):
        if v is None:
            out.append(SignatureStatus(sig, None, None, None, None))
        else:
            err = v.get("err")
            out.append(SignatureStatus(
                sig, v.get("confirmations"), v.get("confirmationStatus"),
                None if err is None else str(err), v.get("slot")))
    return out


def parse_log_notification(msg: dict) -> dict | None:
    """Extract the payload of a ``logsSubscribe`` push notification.

    Returns ``None`` for anything that is not a logs notification (the initial
    subscription-id acknowledgement, a ping, an unrelated method) so the caller
    can filter with a single ``if result is None: continue``.
    """
    if msg.get("method") != "logsNotification":
        return None
    value = msg["params"]["result"]["value"]
    return {
        "signature": value["signature"],
        "err": value.get("err"),
        "logs": value.get("logs") or [],
        "slot": msg["params"]["result"]["context"]["slot"],
    }


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

class SolanaRpcClient:
    def __init__(self, http_url: str, *, timeout_s: float = 10.0,
                retry: RetryConfig | None = None) -> None:
        self.http_url = http_url
        self.timeout_s = timeout_s
        self.retry = retry or RetryConfig(retries=3, base_delay_s=0.3, max_delay_s=4.0)
        self._client: "httpx.AsyncClient | None" = None
        self._next_id = 1

    def _require_httpx(self) -> "httpx.AsyncClient":
        if httpx is None:
            raise RuntimeError("httpx is not installed: pip install 'mca[chains]' / core deps")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def _call(self, method: str, params: list[Any]) -> dict:
        client = self._require_httpx()
        self._next_id += 1
        body = build_request(method, params, self._next_id)

        async def _do() -> dict:
            try:
                resp = await client.post(self.http_url, json=body)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (429, 500, 502, 503, 504):
                    raise RpcTransportError(str(exc)) from exc
                raise RpcResponseError(exc.response.status_code, exc.response.text) from exc
            except httpx.TransportError as exc:
                raise RpcTransportError(str(exc)) from exc

        return await retry_async(_do, cfg=self.retry, retryable=(RpcTransportError,))

    async def get_latest_blockhash(self, commitment: str = "confirmed") -> Blockhash:
        raw = await self._call("getLatestBlockhash", [{"commitment": commitment}])
        return parse_latest_blockhash(raw)

    async def simulate_transaction(self, raw_tx_b64: str, *,
                                   sig_verify: bool = False,
                                   replace_recent_blockhash: bool = True) -> SimulationResult:
        raw = await self._call("simulateTransaction", [raw_tx_b64, {
            "encoding": "base64", "sigVerify": sig_verify,
            "replaceRecentBlockhash": replace_recent_blockhash,
            "commitment": "processed",
        }])
        return parse_simulate_transaction(raw)

    async def send_transaction(self, signed_tx_b64: str, *,
                               skip_preflight: bool = True,
                               max_rpc_retries: int = 0) -> str:
        """Returns the transaction signature. ``max_rpc_retries`` is the RPC
        node's own internal resend count, distinct from — and normally left at
        0 relative to — this client's own :func:`retry_async` retries; letting
        both retry loops resend independently multiplies an already-submitted
        transaction's resend rate for no benefit."""
        raw = await self._call("sendTransaction", [signed_tx_b64, {
            "encoding": "base64", "skipPreflight": skip_preflight,
            "maxRetries": max_rpc_retries, "preflightCommitment": "processed",
        }])
        return parse_response(raw)

    async def get_signature_statuses(self, signatures: list[str]) -> list[SignatureStatus]:
        raw = await self._call("getSignatureStatuses",
                               [signatures, {"searchTransactionHistory": True}])
        return parse_signature_statuses(raw, signatures)

    async def confirm_transaction(self, signature: str, *, timeout_s: float = 45.0,
                                  poll_interval_s: float = 1.5,
                                  target: str = "confirmed") -> SignatureStatus:
        """Poll until ``target`` confirmation level or ``err`` is set.

        Raises :class:`TimeoutError` if neither happens in time — the caller
        (the executor) treats that as ambiguous, not failed: the transaction
        may still land after we stop watching, which is why every intent
        carries an idempotency key rather than assuming a timeout means retry
        is free.
        """
        import asyncio
        import time
        levels = {"processed": 0, "confirmed": 1, "finalized": 2}
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            statuses = await self.get_signature_statuses([signature])
            status = statuses[0]
            if status.err is not None:
                return status
            if status.confirmation_status and levels.get(status.confirmation_status, -1) >= levels[target]:
                return status
            await asyncio.sleep(poll_interval_s)
        raise TimeoutError(f"{signature} not confirmed within {timeout_s}s")

    async def get_balance(self, pubkey: str, commitment: str = "confirmed") -> int:
        """Native SOL balance in lamports."""
        raw = await self._call("getBalance", [pubkey, {"commitment": commitment}])
        return parse_response(raw)["value"]

    async def get_token_supply(self, mint: str) -> dict:
        """Returns ``{"amount": str, "decimals": int, "uiAmount": float}``.
        The only field callers actually need is ``decimals`` — see
        ``solana_executor.py``'s decimals cache — but the full value is passed
        through in case a future caller needs total supply too.
        """
        raw = await self._call("getTokenSupply", [mint])
        return parse_response(raw)["value"]

    async def get_transaction(self, signature: str, *,
                              max_supported_transaction_version: int = 0) -> dict | None:
        """Full transaction, ``jsonParsed`` encoding.

        ``jsonParsed`` does not decode a third-party program's own instruction
        *data* (only System/Token/a handful of known programs get that), but it
        does resolve every instruction's *account list* to real pubkeys,
        including through address lookup tables — which is what
        ``ingest/solana_pools.py`` actually needs: the pool/mint addresses are
        accounts referenced by the instruction, not bytes inside it.
        """
        raw = await self._call("getTransaction", [signature, {
            "encoding": "jsonParsed", "commitment": "confirmed",
            "maxSupportedTransactionVersion": max_supported_transaction_version,
        }])
        return parse_response(raw)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class SolanaWebsocketClient:
    """``logsSubscribe`` with automatic reconnect and exponential backoff.

    Yields parsed log-notification dicts (see :func:`parse_log_notification`)
    indefinitely; the caller's ``async for`` loop simply keeps running across
    reconnects. A dropped connection is not surfaced as an exception to the
    caller — it is logged and retried, because "the websocket blipped" should
    never take the whole ingest pipeline down.
    """

    def __init__(self, ws_url: str, *, retry: RetryConfig | None = None) -> None:
        self.ws_url = ws_url
        self.retry = retry or RetryConfig(retries=10_000, base_delay_s=1.0, max_delay_s=30.0)

    async def subscribe_logs(self, mentions: list[str], commitment: str = "confirmed"):
        if websockets is None:
            raise RuntimeError("websockets is not installed: pip install 'mca[chains]'")
        import asyncio
        import json as _json

        attempt = 0
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    sub = build_request("logsSubscribe", [
                        {"mentions": mentions}, {"commitment": commitment},
                    ])
                    await ws.send(_json.dumps(sub))
                    ack = _json.loads(await ws.recv())
                    log.info("logsSubscribe ack subscription_id=%s mentions=%d",
                            ack.get("result"), len(mentions))
                    attempt = 0  # connection succeeded, reset backoff
                    async for raw in ws:
                        msg = _json.loads(raw)
                        parsed = parse_log_notification(msg)
                        if parsed is not None:
                            yield parsed
            except (websockets.ConnectionClosed, OSError) as exc:
                delay = min(self.retry.base_delay_s * (self.retry.factor ** attempt),
                           self.retry.max_delay_s)
                log.warning("websocket disconnected (%s), reconnecting in %.1fs", exc, delay)
                attempt += 1
                await asyncio.sleep(delay)
