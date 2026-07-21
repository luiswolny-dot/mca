"""Telegram channel for ``mca.alerts.notifier.Notifier``.

Design decision — plain text, not MarkdownV2
------------------------------------------------
Telegram's MarkdownV2 requires escaping a specific set of punctuation
characters everywhere they are *not* intended as formatting, and a token
symbol or an on-chain address routinely contains several of them (``_``,
``.``, underscores in error messages). Getting that escaping wrong does not
raise an error a developer notices — Telegram just silently fails to deliver
the message with a 400, which is exactly the failure mode this alerting layer
exists to avoid (see ``notifier.py``'s design note on undelivered CRITICAL
alerts). Plain text has no such trap and is a deliberate trade against
richer formatting.

This module slots into the existing :class:`~mca.alerts.notifier.Channel`
dataclass — it does not introduce a parallel notification path.
"""

from __future__ import annotations

import logging

from mca.alerts.notifier import Alert, Channel, Severity
from mca.core.retry import RetryConfig, retry_async

log = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

TELEGRAM_API_BASE = "https://api.telegram.org"

_ICONS = {Severity.INFO: "\u2139\ufe0f", Severity.WARNING: "\u26a0\ufe0f", Severity.CRITICAL: "\U0001f6a8"}


class TelegramError(Exception):
    """Base for Telegram delivery failures."""


class TelegramTransportError(TelegramError):
    """Network/HTTP-level failure. Retryable."""


# --------------------------------------------------------------------------- #
# Pure
# --------------------------------------------------------------------------- #

def format_alert_text(alert: Alert) -> str:
    icon = _ICONS.get(alert.severity, "")
    header = f"{icon} {alert.title}".strip()
    return f"{header}\n\n{alert.body}" if alert.body else header


def build_send_message_request(bot_token: str, chat_id: str, text: str) -> tuple[str, dict]:
    if not bot_token:
        raise ValueError("empty bot_token")
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    # Telegram's hard cap is 4096 UTF-16 code units; truncate well before that
    # so a runaway alert body cannot itself become the delivery failure.
    body = {"chat_id": chat_id, "text": text[:3900], "disable_web_page_preview": True}
    return url, body


def parse_send_response(status_code: int, data: dict) -> None:
    if status_code == 200 and data.get("ok"):
        return
    description = data.get("description", f"HTTP {status_code}")
    if status_code == 429 or status_code >= 500:
        raise TelegramTransportError(description)
    raise TelegramError(description)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

class TelegramSender:
    """The async callable a :class:`~mca.alerts.notifier.Channel` expects as
    ``send``. Kept as a class (not a closure) so it can hold a reusable
    ``httpx.AsyncClient`` across calls instead of opening a new connection per
    alert — meaningful when a suppressed-alert burst releases all at once."""

    def __init__(self, bot_token: str, chat_id: str, *, timeout_s: float = 8.0,
                retry: RetryConfig | None = None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_s = timeout_s
        self.retry = retry or RetryConfig(retries=2, base_delay_s=1.0, max_delay_s=8.0)
        self._client: "httpx.AsyncClient | None" = None

    def _require_httpx(self) -> "httpx.AsyncClient":
        if httpx is None:
            raise RuntimeError("httpx is not installed: pip install core deps")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s)
        return self._client

    async def __call__(self, alert: Alert) -> None:
        client = self._require_httpx()
        url, body = build_send_message_request(
            self.bot_token, self.chat_id, format_alert_text(alert))

        async def _do() -> None:
            try:
                resp = await client.post(url, json=body)
            except httpx.TransportError as exc:
                raise TelegramTransportError(str(exc)) from exc
            try:
                data = resp.json()
            except ValueError:
                data = {}
            parse_send_response(resp.status_code, data)

        await retry_async(_do, cfg=self.retry, retryable=(TelegramTransportError,))

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def telegram_channel(bot_token: str, chat_id: str,
                     min_severity: Severity = Severity.INFO) -> Channel:
    """Build a :class:`~mca.alerts.notifier.Channel` ready to pass into
    ``Notifier(channels=[...])``."""
    sender = TelegramSender(bot_token, chat_id)
    return Channel(name="telegram", min_severity=min_severity, send=sender)
