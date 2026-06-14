"""
Incident alerting tool — the agent's voice to on-call (roadmap C5).

Aegis pushes its incident lifecycle to an external alerting sink so humans can
watch what the autonomous loop is doing in real time. For testing we point it at
a webhook catcher (webhook.site); the payloads are shaped like the **PagerDuty
Events API v2**, so swapping the catcher URL for a real PagerDuty routing
endpoint is the only change needed to go live.

Lifecycle (keyed by a stable `dedup_key` = the incident id):
    trigger     -> a new incident is firing (severity/description/timestamp)
    acknowledge -> the agent/human has taken ownership (e.g. patch proposed/approved)
    resolve     -> the incident is closed out (e.g. fix PR opened)

Design notes:
  * httpx (already a dependency) for async POST with a hard per-call timeout.
  * Payloads carry only incident metadata (severity, summary, timestamp, custom
    details) — never tokens, DSNs, or source. Nothing secret leaves the process.
  * The notifier never raises into the repair loop: callers fire alerts guarded,
    so an alerting outage can't fail a repair (`send` returns a result dict with
    `ok=False` instead of throwing).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from aegis_sre.config import get_settings
from aegis_sre.telemetry.logger import logger

# PagerDuty Events v2 severity vocabulary; anything else is coerced to "critical".
_VALID_SEVERITY = {"critical", "error", "warning", "info"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class IncidentNotifier:
    """Posts PagerDuty-v2-shaped incident events to an alerting webhook."""

    def __init__(
        self,
        webhook_url: str,
        routing_key: str = "aegis-mock-routing-key",
        source: str = "aegis-sre",
        timeout: float = 10.0,
    ):
        if not webhook_url:
            raise ValueError("IncidentNotifier requires a webhook_url")
        self.webhook_url = webhook_url
        self.routing_key = routing_key
        self.source = source
        self.timeout = timeout

    async def _post(self, body: dict) -> dict:
        """POST one event. Returns {ok, status, action, dedup_key}; never raises."""
        action = body.get("event_action")
        dedup_key = body.get("dedup_key")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.webhook_url, json=body)
            ok = resp.is_success
            (logger.info if ok else logger.warning)(
                "incident_alert_sent", action=action, dedup_key=dedup_key, status=resp.status_code
            )
            return {"ok": ok, "status": resp.status_code, "action": action, "dedup_key": dedup_key}
        except httpx.HTTPError as e:
            logger.warning("incident_alert_failed", action=action, dedup_key=dedup_key, error=str(e))
            return {"ok": False, "status": None, "action": action, "dedup_key": dedup_key, "error": str(e)}

    async def trigger(
        self,
        *,
        dedup_key: str,
        severity: str,
        description: str,
        timestamp: Optional[str] = None,
        **details,
    ) -> dict:
        """Fire a new incident alert. `dedup_key` (the incident id) ties the later
        acknowledge/resolve to this alert."""
        sev = severity if severity in _VALID_SEVERITY else "critical"
        body = {
            "routing_key": self.routing_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {
                "summary": description,
                "severity": sev,
                "source": self.source,
                "timestamp": timestamp or _now_iso(),
                "custom_details": details,
            },
        }
        return await self._post(body)

    async def acknowledge(self, dedup_key: str, note: Optional[str] = None) -> dict:
        """Mark an incident acknowledged (ownership taken)."""
        return await self._post({
            "routing_key": self.routing_key,
            "event_action": "acknowledge",
            "dedup_key": dedup_key,
            "payload": {"summary": note or "Acknowledged by Aegis",
                        "source": self.source, "timestamp": _now_iso()},
        })

    async def resolve(self, dedup_key: str, note: Optional[str] = None) -> dict:
        """Mark an incident resolved (closed out)."""
        return await self._post({
            "routing_key": self.routing_key,
            "event_action": "resolve",
            "dedup_key": dedup_key,
            "payload": {"summary": note or "Resolved by Aegis",
                        "source": self.source, "timestamp": _now_iso()},
        })


_notifier: Optional[IncidentNotifier] = None


def get_incident_notifier() -> Optional[IncidentNotifier]:
    """Process-wide notifier from settings, or None when ALERT_WEBHOOK_URL is
    unset (callers treat None as 'alerting disabled')."""
    global _notifier
    if _notifier is None:
        url = get_settings().alert_webhook_url
        if not url:
            return None
        _notifier = IncidentNotifier(url, routing_key=get_settings().alert_routing_key)
    return _notifier
