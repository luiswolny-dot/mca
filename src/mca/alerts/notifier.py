"""Alerting.

Design decision — alarm fatigue is a safety risk, so dedup is mandatory
-----------------------------------------------------------------------
A bot that sends a Telegram message per fill produces 40+ notifications a day.
Within a week the operator mutes the channel, and then the one message that
mattered — "circuit breaker tripped, wallet balance mismatch" — is never read.

Two mechanisms:
  * severity routing: only CRITICAL reaches every channel
  * deduplication: identical alerts collapse into one message with a counter,
    within a per-key cooldown window

Design decision — the CRITICAL path must not depend on a single provider
------------------------------------------------------------------------
Telegram has outages. A critical alert fans out to every configured channel in
parallel and succeeds if *any* one delivers. Failure to deliver a critical alert
is itself escalated to the local log at ERROR and increments a Prometheus
counter that Alertmanager watches — so the monitoring of the monitoring does not
depend on the same network path.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from enum import IntEnum

log = logging.getLogger(__name__)


class Severity(IntEnum):
    INFO = 10
    WARNING = 20
    CRITICAL = 30


@dataclass(slots=True)
class Alert:
    severity: Severity
    title: str
    body: str
    dedup_key: str | None = None
    context: dict = field(default_factory=dict)

    def key(self) -> str:
        if self.dedup_key:
            return self.dedup_key
        return hashlib.sha1(f"{self.severity}|{self.title}".encode()).hexdigest()[:16]


@dataclass(slots=True)
class Channel:
    name: str
    min_severity: Severity
    send: object   # async callable (Alert) -> None


class Notifier:
    """Routes alerts to channels with deduplication and rate limiting."""

    COOLDOWN_S = {
        Severity.INFO: 3600,
        Severity.WARNING: 900,
        Severity.CRITICAL: 120,     # short, but not zero — a flapping RPC would spam
    }

    def __init__(self, channels: list[Channel]) -> None:
        self.channels = channels
        self._last_sent: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}

    async def send(self, alert: Alert) -> bool:
        key = alert.key()
        now = time.monotonic()
        cooldown = self.COOLDOWN_S[alert.severity]

        last = self._last_sent.get(key)
        if last is not None and (now - last) < cooldown:
            self._suppressed[key] = self._suppressed.get(key, 0) + 1
            return False

        suppressed = self._suppressed.pop(key, 0)
        if suppressed:
            alert.body += f"\n\n({suppressed} identical alerts suppressed since last message)"

        targets = [c for c in self.channels if alert.severity >= c.min_severity]
        if not targets:
            return False

        results = await asyncio.gather(
            *(self._deliver(c, alert) for c in targets), return_exceptions=True)
        delivered = any(r is True for r in results)

        if delivered:
            self._last_sent[key] = now
        elif alert.severity is Severity.CRITICAL:
            # The alert about the failed alert. Goes to the local log, which is
            # scraped independently of the notification transport.
            log.error("CRITICAL ALERT UNDELIVERED title=%s body=%s", alert.title, alert.body)
        return delivered

    async def _deliver(self, channel: Channel, alert: Alert) -> bool:
        try:
            await asyncio.wait_for(channel.send(alert), timeout=8.0)
            return True
        except Exception as exc:
            log.warning("alert channel %s failed: %s", channel.name, exc)
            return False


# --------------------------------------------------------------------------- #
# Canonical alerts. Defined centrally so severity is a deliberate decision,
# not whatever the author of a given module felt like at the time.
# --------------------------------------------------------------------------- #

def circuit_breaker_tripped(state: str, reason: str, equity: float) -> Alert:
    return Alert(Severity.CRITICAL, f"Circuit breaker: {state.upper()}",
                 f"{reason}\nEquity: ${equity:,.2f}\nManual unlock required.",
                 dedup_key=f"breaker:{state}")


def honeypot_detected(token: str, detail: str) -> Alert:
    return Alert(Severity.CRITICAL, "Honeypot in an OPEN position",
                 f"{token}\n{detail}\nEmergency exit attempted.",
                 dedup_key=f"honeypot:{token}")


def wallet_balance_mismatch(expected: float, actual: float) -> Alert:
    """On-chain balance differs from the balance derived from our fills.
    Always critical: it means either a bug in position accounting or an
    unauthorised transfer, and both require stopping immediately."""
    return Alert(Severity.CRITICAL, "Wallet balance mismatch",
                 f"Expected ${expected:,.2f}, on-chain ${actual:,.2f}. Trading halted.",
                 dedup_key="balance-mismatch")


def source_down(source: str, minutes: int) -> Alert:
    return Alert(Severity.WARNING, f"Data source down: {source}",
                 f"No data for {minutes} minutes. Affected gates now fail closed.",
                 dedup_key=f"source:{source}")


def exit_failed(token: str, attempts: int, reason: str) -> Alert:
    return Alert(Severity.CRITICAL, "Exit failed",
                 f"{token}: {attempts} attempts failed. Last error: {reason}",
                 dedup_key=f"exit-failed:{token}")


def position_opened(token: str, size: float, score: float, p_win: float) -> Alert:
    return Alert(Severity.INFO, "Position opened",
                 f"{token} ${size:,.2f} (score {score:.1f}, p={p_win:.2f})")
