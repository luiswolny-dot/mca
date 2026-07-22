"""Jupiter aggregator client.

Verified against dev.jup.ag / developers.jup.ag on 2026-07-21 — Jupiter
restructured its API on 1 Dec 2024; the widely-copied ``quote-api.jup.ag/v6``
examples still circulating online are the *legacy* surface. Current canonical
paths are ``/swap/v1/quote`` and ``/swap/v1/swap`` served from either
``api.jup.ag`` (paid tier, needs an ``x-api-key`` header) or
``lite-api.jup.ag`` (free, rate-limited, no key). This client defaults to the
free tier and accepts an API key to switch to the paid one.

None of this has executed against a live endpoint — this sandbox has no network
access. Before trusting it with real funds: point it at ``lite-api.jup.ag``
with a devnet-funded wallet (Jupiter also routes some devnet pairs) or a tiny
mainnet amount, and confirm the response shapes below still match. Re-check
https://dev.jup.ag/docs before going live; DEX aggregator APIs change more
often than Solana's own JSON-RPC surface.

Design decision — the quote response is stored and replayed verbatim
------------------------------------------------------------------------
The ``/swap`` endpoint expects the *entire* object returned by ``/quote`` back
in its request body, including ``routePlan``, which this client does not (and
should not) interpret. ``QuoteResponse.raw`` keeps the untouched dict for
exactly this reason — re-deriving that payload field-by-field would silently
drop whatever Jupiter adds to the route plan in a future release.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from mca.core.retry import RetryConfig, retry_async

log = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
LAMPORTS_PER_SOL = 10 ** 9
USDC_DECIMALS = 6

# On-chain program IDs, for reference / cross-checking a route's accounts.
# https://station.jup.ag — verify before depending on these beyond logging.
JUPITER_AGGREGATOR_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

LITE_BASE_URL = "https://lite-api.jup.ag"
PAID_BASE_URL = "https://api.jup.ag"


class JupiterError(Exception):
    """Base for Jupiter API failures."""


class JupiterTransportError(JupiterError):
    """Network/HTTP-level failure. Retryable."""


class NoRouteFound(JupiterError):
    """Jupiter could not find a route for this pair/size. Not retryable — the
    pool genuinely may not support this trade, most often because it is thinner
    than the requested size or the token has no indexed route yet."""


@dataclass(slots=True, frozen=True)
class QuoteResponse:
    input_mint: str
    in_amount: int
    output_mint: str
    out_amount: int
    other_amount_threshold: int
    swap_mode: str
    slippage_bps: int
    price_impact_pct: float
    raw: dict          # the untouched response — required by /swap, see module docstring


@dataclass(slots=True, frozen=True)
class SwapResponse:
    swap_transaction_b64: str
    last_valid_block_height: int
    prioritization_fee_lamports: int | None
    compute_unit_limit: int | None


# --------------------------------------------------------------------------- #
# Pure: request building / response parsing
# --------------------------------------------------------------------------- #

def build_quote_params(input_mint: str, output_mint: str, amount: int,
                       slippage_bps: int, *, restrict_intermediate_tokens: bool = True,
                       only_direct_routes: bool = False) -> dict:
    if amount <= 0:
        raise ValueError(f"amount must be positive base units, got {amount}")
    return {
        "inputMint": input_mint,
        "outputMint": output_mint,
        "amount": str(amount),
        "slippageBps": slippage_bps,
        "restrictIntermediateTokens": restrict_intermediate_tokens,
        "onlyDirectRoutes": only_direct_routes,
    }


def parse_quote_response(data: dict) -> QuoteResponse:
    if not data or "outAmount" not in data:
        raise NoRouteFound(f"no route in response: {data}")
    return QuoteResponse(
        input_mint=data["inputMint"], in_amount=int(data["inAmount"]),
        output_mint=data["outputMint"], out_amount=int(data["outAmount"]),
        other_amount_threshold=int(data.get("otherAmountThreshold", data["outAmount"])),
        swap_mode=data.get("swapMode", "ExactIn"),
        slippage_bps=int(data.get("slippageBps", 0)),
        price_impact_pct=float(data.get("priceImpactPct", 0.0) or 0.0),
        raw=data,
    )


def build_swap_request_body(quote: QuoteResponse, user_pubkey: str, *,
                            wrap_and_unwrap_sol: bool = True,
                            priority_level: str = "high",
                            max_priority_lamports: int = 2_000_000,
                            dynamic_compute_unit_limit: bool = True) -> dict:
    """``max_priority_lamports`` is a *total lamport* ceiling for Jupiter's own
    dynamic fee estimator — a different knob than Solana's native
    micro-lamports-per-compute-unit (``ExecutionConfig.priority_fee_micro_lamports``).
    The caller (``solana_executor.py``) is responsible for converting one
    config into a sane bound for the other; this function does not silently
    assume they are interchangeable.
    """
    return {
        "quoteResponse": quote.raw,
        "userPublicKey": user_pubkey,
        "wrapAndUnwrapSol": wrap_and_unwrap_sol,
        "dynamicComputeUnitLimit": dynamic_compute_unit_limit,
        "dynamicSlippage": False,   # we set slippage explicitly via the quote; see gates
        "prioritizationFeeLamports": {
            "priorityLevelWithMaxLamports": {
                "priorityLevel": priority_level,
                "maxLamports": max_priority_lamports,
            }
        },
    }


def parse_swap_response(data: dict) -> SwapResponse:
    if "swapTransaction" not in data:
        raise JupiterError(f"swap response missing swapTransaction: {data}")
    return SwapResponse(
        swap_transaction_b64=data["swapTransaction"],
        last_valid_block_height=int(data["lastValidBlockHeight"]),
        prioritization_fee_lamports=data.get("prioritizationFeeLamports"),
        compute_unit_limit=data.get("computeUnitLimit"),
    )


def micro_lamports_to_priority_ceiling(priority_fee_micro_lamports: int,
                                       compute_unit_limit: int) -> int:
    """Convert Solana's native microLamports-per-CU into an approximate total
    lamport ceiling for Jupiter's ``maxLamports`` knob. ``1 CU * 1 microLamport
    = 1e-6 lamports``, so total = micro_lamports * compute_units / 1_000_000."""
    return max(1, (priority_fee_micro_lamports * compute_unit_limit) // 1_000_000)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

class JupiterClient:
    def __init__(self, base_url: str = LITE_BASE_URL, api_key: str | None = None,
                *, timeout_s: float = 10.0, retry: RetryConfig | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.retry = retry or RetryConfig(retries=3, base_delay_s=0.4, max_delay_s=5.0)
        self._client: "httpx.AsyncClient | None" = None

    def _require_httpx(self) -> "httpx.AsyncClient":
        if httpx is None:
            raise RuntimeError("httpx is not installed: pip install 'mca[chains]' / core deps")
        if self._client is None:
            headers = {"x-api-key": self.api_key} if self.api_key else {}
            self._client = httpx.AsyncClient(timeout=self.timeout_s, headers=headers)
        return self._client

    async def quote(self, input_mint: str, output_mint: str, amount: int,
                    slippage_bps: int) -> QuoteResponse:
        client = self._require_httpx()
        params = build_quote_params(input_mint, output_mint, amount, slippage_bps)

        async def _do() -> dict:
            try:
                resp = await client.get(f"{self.base_url}/swap/v1/quote", params=params)
                if resp.status_code == 400:
                    # Jupiter returns 400 with an error body for "no route", not a 404.
                    raise NoRouteFound(resp.text)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (429, 500, 502, 503, 504):
                    raise JupiterTransportError(str(exc)) from exc
                raise JupiterError(f"{exc.response.status_code}: {exc.response.text}") from exc
            except httpx.TransportError as exc:
                raise JupiterTransportError(str(exc)) from exc

        data = await retry_async(_do, cfg=self.retry, retryable=(JupiterTransportError,))
        return parse_quote_response(data)

    async def build_swap_transaction(self, quote: QuoteResponse, user_pubkey: str,
                                     exec_cfg) -> SwapResponse:
        """``exec_cfg`` is an ``ExecutionConfig`` — kept loosely typed here to
        avoid a hard import cycle between ``execution.jupiter`` and
        ``execution.base``; both live in the same package and are always
        deployed together, so this is a pragmatic, not a structural, choice."""
        client = self._require_httpx()
        max_lamports = micro_lamports_to_priority_ceiling(
            exec_cfg.priority_fee_micro_lamports, exec_cfg.compute_unit_limit)
        body = build_swap_request_body(quote, user_pubkey, max_priority_lamports=max_lamports)

        async def _do() -> dict:
            try:
                resp = await client.post(f"{self.base_url}/swap/v1/swap", json=body)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (429, 500, 502, 503, 504):
                    raise JupiterTransportError(str(exc)) from exc
                raise JupiterError(f"{exc.response.status_code}: {exc.response.text}") from exc
            except httpx.TransportError as exc:
                raise JupiterTransportError(str(exc)) from exc

        data = await retry_async(_do, cfg=self.retry, retryable=(JupiterTransportError,))
        return parse_swap_response(data)

    async def sol_usd_price(self, probe_sol: Decimal = Decimal("1")) -> Decimal:
        """Live SOL/USD reference price via a small USDC quote.

        Used wherever the system needs a USD reference price and does not want
        a separate market-data ingestion path for one number — the position
        manager's exit loop and the executor's ``quote_usd_price`` both use
        this. USDC is treated as pegged to USD, which is the standard
        assumption everywhere in DeFi tooling and can occasionally be wrong
        during a depeg event; this is a reference price for sizing and PnL
        display, not a source ever fed into the safety gates themselves.
        """
        lamports = int(probe_sol * LAMPORTS_PER_SOL)
        q = await self.quote(WSOL_MINT, USDC_MINT, lamports, slippage_bps=50)
        usdc_received = Decimal(q.out_amount) / (Decimal(10) ** USDC_DECIMALS)
        return usdc_received / probe_sol

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
