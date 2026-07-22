"""Event bus: Redis Streams.

Design decision — one JSON blob per message, not one Redis field per attribute
--------------------------------------------------------------------------------
Redis Streams fields are flat ``bytes -> bytes``. Mapping every dataclass field
to its own stream field means re-deriving a type-coercion table (is
``liquidity_usd`` a ``Decimal`` or a ``str`` on the wire?) in every consumer.
Instead every message is one field, ``data``, holding a JSON string produced by
the publisher's own serialisation logic — the same approach the DB layer will
use for ``JSONB`` columns. One extra ``json.loads`` per message is negligible
next to the correctness cost of a hand-rolled field-by-field wire format.

Design decision — consumer groups, not plain reads
----------------------------------------------------
The enricher runs four replicas (see ``docker-compose.yml``). Consumer groups
give each message to exactly one replica and track delivery, so a crashed
enricher's in-flight messages are recoverable via ``XAUTOCLAIM`` instead of
silently vanishing. Plain ``XREAD`` has no notion of "this consumer died with
unacked work" — that is the entire reason consumer groups exist.

Design decision — canonical stream names live here, not scattered as literals
--------------------------------------------------------------------------------
A typo in a stream name (``"canidates"``) is a silent pipeline stall: the
publisher succeeds, the consumer never sees anything, and nothing raises. Stream
names are therefore an enum-like namespace, imported everywhere instead of
retyped.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

log = logging.getLogger(__name__)


class Stream:
    """Canonical stream names. One partition per stream; consumer groups fan
    out work across replicas within a stage."""

    POOL_EVENTS = "mca:stream:pool_events"     # ingest -> enrich
    CANDIDATES = "mca:stream:candidates"        # enrich -> score
    SIGNALS = "mca:stream:signals"              # score -> risk gate
    INTENTS = "mca:stream:intents"              # risk gate -> executor
    FILLS = "mca:stream:fills"                  # executor -> position manager
    RISK_EVENTS = "mca:stream:risk_events"      # circuit breaker -> alerts/API
    CONTROL = "mca:stream:control"              # API -> risk gate (halt/resume/unlock)


class BusError(Exception):
    """Base for bus failures."""


class BusUnavailable(BusError):
    """Raised at call time when the ``redis`` package is not installed, or the
    connection could not be established. Deliberately not raised at import
    time — a worker that only publishes to one stream should not fail to start
    because an unrelated consumer path could not resolve a hostname."""


# --------------------------------------------------------------------------- #
# Pure functions — encoding/decoding, fully unit-tested without a connection
# --------------------------------------------------------------------------- #

def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"object of type {type(o).__name__} is not JSON serialisable")


def encode_message(payload: dict, *, schema_version: int = 1) -> dict[str, str]:
    """dict -> Redis Streams fields. The envelope carries a schema version so a
    consumer can reject (rather than mis-parse) a message from a future
    publisher during a rolling deploy."""
    return {
        "schema_version": str(schema_version),
        "data": json.dumps(payload, default=_json_default, separators=(",", ":")),
    }


def decode_message(fields: dict[bytes, bytes] | dict[str, str]) -> dict:
    """Redis Streams fields -> dict. Accepts both ``bytes`` (the real client's
    default) and ``str`` (what the fallback in-memory bus and tests use)."""
    def _get(key: str) -> str:
        raw = fields.get(key) or fields.get(key.encode())
        if raw is None:
            raise BusError(f"message missing required field {key!r}")
        return raw.decode() if isinstance(raw, bytes) else raw

    data = json.loads(_get("data"))
    version = int(_get("schema_version"))
    if version != 1:
        raise BusError(f"unsupported schema_version {version}; upgrade the consumer")
    return data


@dataclass(slots=True, frozen=True)
class StreamMessage:
    id: str
    payload: dict


# --------------------------------------------------------------------------- #
# Live client — guarded import, used by every long-running worker process
# --------------------------------------------------------------------------- #

try:
    import redis.asyncio as _redis
except ImportError:
    _redis = None  # type: ignore[assignment]


class Bus:
    """Thin async wrapper around Redis Streams.

    Every method raises :class:`BusUnavailable` rather than ``AttributeError``
    when ``redis`` is not installed, so a caller can catch one exception type
    regardless of why the bus is unreachable.
    """

    def __init__(self, url: str = "redis://localhost:6379/0") -> None:
        self.url = url
        self._client = None

    def _require_client(self):
        if _redis is None:
            raise BusUnavailable("the 'redis' package is not installed")
        if self._client is None:
            self._client = _redis.from_url(self.url, decode_responses=False)
        return self._client

    async def publish(self, stream: str, payload: dict) -> str:
        client = self._require_client()
        try:
            msg_id = await client.xadd(stream, encode_message(payload))
        except Exception as exc:  # noqa: BLE001 - normalise every backend error
            raise BusUnavailable(f"publish to {stream} failed: {exc}") from exc
        return msg_id.decode() if isinstance(msg_id, bytes) else msg_id

    async def ensure_group(self, stream: str, group: str) -> None:
        client = self._require_client()
        try:
            await client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" in str(exc):
                return
            raise BusUnavailable(f"ensure_group {stream}/{group} failed: {exc}") from exc

    async def consume(self, stream: str, group: str, consumer: str,
                      count: int = 10, block_ms: int = 5000) -> list[StreamMessage]:
        client = self._require_client()
        try:
            resp = await client.xreadgroup(
                group, consumer, streams={stream: ">"}, count=count, block=block_ms)
        except Exception as exc:  # noqa: BLE001
            raise BusUnavailable(f"consume {stream}/{group} failed: {exc}") from exc
        if not resp:
            return []
        out: list[StreamMessage] = []
        for _stream_name, entries in resp:
            for entry_id, fields in entries:
                mid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                try:
                    out.append(StreamMessage(id=mid, payload=decode_message(fields)))
                except BusError:
                    log.exception("dropping malformed message id=%s stream=%s", mid, stream)
        return out

    async def ack(self, stream: str, group: str, *ids: str) -> None:
        if not ids:
            return
        client = self._require_client()
        await client.xack(stream, group, *ids)

    async def claim_stale(self, stream: str, group: str, consumer: str, *,
                          min_idle_ms: int = 60_000, count: int = 50) -> list[StreamMessage]:
        """Recover messages whose original consumer died mid-processing.

        Called on a timer by the same worker pool that normally consumes the
        stream — every replica periodically tries to claim stale work from its
        own group, so a crashed pod's in-flight candidates get re-enriched
        instead of waiting forever for a consumer that is not coming back.
        """
        client = self._require_client()
        try:
            _cursor, entries, _deleted = await client.xautoclaim(
                stream, group, consumer, min_idle_time=min_idle_ms, start_id="0", count=count)
        except Exception as exc:  # noqa: BLE001
            raise BusUnavailable(f"claim_stale {stream}/{group} failed: {exc}") from exc
        out: list[StreamMessage] = []
        for entry_id, fields in entries:
            mid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
            try:
                out.append(StreamMessage(id=mid, payload=decode_message(fields)))
            except BusError:
                log.exception("dropping malformed reclaimed message id=%s", mid)
        return out

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- heartbeats -----------------------------------------------------------
    # A TTL'd key, not a stream: a stream would need its own trimming policy for
    # data nobody ever reads twice. "Is worker X alive" is exactly what a
    # self-expiring key answers for free — no entry means no heartbeat in TTL
    # seconds, which is the only question ops/watchdog.py ever asks.

    async def heartbeat(self, worker: str, ttl_s: int = 30) -> None:
        client = self._require_client()
        await client.set(f"mca:heartbeat:{worker}", "1", ex=ttl_s)

    async def is_alive(self, worker: str) -> bool:
        client = self._require_client()
        return bool(await client.exists(f"mca:heartbeat:{worker}"))

    async def alive_workers(self, expected: list[str]) -> dict[str, bool]:
        return {w: await self.is_alive(w) for w in expected}
