"""
Alertmanager webhook adapter (Stone 2, C4).

Normalizes a Prometheus Alertmanager webhook payload into `Signal(metric_alert)`s
so a *live alert* — not just a crash — can trigger the repair swarm. This is the
concrete payoff of the Stone-1 `Signal` generalization and relaxes limiter #1
(the trigger modality the SCALE_PLAN says "gates every target market").

Only `firing` alerts are turned into signals; `resolved` notifications are
skipped. The endpoint converts each Signal via `Signal.to_telemetry()` and runs
it through the existing ingest pipeline, so the whole chassis (dedup, persist,
broker, swarm, live Prometheus eyes) is reused unchanged.
"""

from __future__ import annotations

from typing import List, Optional

from aegis_sre.orchestrator.schemas import Signal, SignalKind


def _service_of(labels: dict) -> str:
    return labels.get("service") or labels.get("job") or labels.get("namespace") or "unknown"


def _service_from_tags(tags) -> Optional[str]:
    """Datadog tags may be a list or comma string; pull `service:<name>`."""
    if isinstance(tags, str):
        tags = tags.split(",")
    for t in tags or []:
        t = t.strip()
        if t.startswith("service:"):
            return t.split(":", 1)[1]
    return None


def parse_datadog(payload: dict) -> List[Signal]:
    """Datadog alert webhook -> Signal(metric_alert). Recovery events are skipped."""
    alert_type = (payload.get("alert_type") or "").lower()
    if alert_type in ("success", "recovery"):
        return []
    title = payload.get("title") or payload.get("alert_title") or "DatadogAlert"
    body = payload.get("body") or payload.get("alert_body") or title
    aid = payload.get("id") or payload.get("alert_id") or title
    tags = payload.get("tags") or ""
    service = payload.get("host") or _service_from_tags(tags) or "unknown"
    blocks = [f"ALERT: {title}", f"body: {body}"]
    if tags:
        blocks.append(f"tags: {tags}")
    return [Signal(signal_id=f"DD-{aid}", service_name=service, kind=SignalKind.METRIC_ALERT,
                   body="\n".join(blocks),
                   metadata={"source": "datadog", "alert_type": alert_type, "tags": tags})]


def parse_pagerduty(payload: dict) -> List[Signal]:
    """PagerDuty v3 webhook -> Signal(metric_alert). Only `incident.triggered` fires."""
    event = payload.get("event", {}) or {}
    if event.get("event_type") != "incident.triggered":
        return []
    data = event.get("data", {}) or {}
    title = data.get("title", "PagerDutyIncident")
    iid = data.get("id") or title
    service = (data.get("service") or {}).get("summary", "unknown")
    body = f"INCIDENT: {title}\nurgency: {data.get('urgency', '')}\nstatus: {data.get('status', '')}"
    return [Signal(signal_id=f"PD-{iid}", service_name=service, kind=SignalKind.METRIC_ALERT,
                   body=body, metadata={"source": "pagerduty", "urgency": data.get("urgency", "")})]


def parse_alertmanager(payload: dict) -> List[Signal]:
    """Turn an Alertmanager webhook payload into a list of firing `Signal`s."""
    signals: List[Signal] = []
    for alert in payload.get("alerts", []):
        if alert.get("status", "firing") != "firing":
            continue  # skip resolved notifications
        labels = alert.get("labels", {}) or {}
        annotations = alert.get("annotations", {}) or {}
        alertname = labels.get("alertname", "UnknownAlert")
        # Alertmanager provides a stable fingerprint; fall back to name+start.
        fingerprint = alert.get("fingerprint") or f"{alertname}-{alert.get('startsAt', '')}"
        summary = annotations.get("summary") or annotations.get("description") or alertname

        body_lines = [f"ALERT: {alertname}", f"summary: {summary}"]
        if annotations.get("description"):
            body_lines.append(f"description: {annotations['description']}")
        if labels:
            body_lines.append("labels: " + ", ".join(f"{k}={v}" for k, v in sorted(labels.items())))

        signals.append(Signal(
            signal_id=f"ALERT-{fingerprint}",
            service_name=_service_of(labels),
            kind=SignalKind.METRIC_ALERT,
            body="\n".join(body_lines),
            metadata={
                "source": "alertmanager",
                "severity": labels.get("severity", ""),
                "labels": labels,
                "annotations": annotations,
            },
        ))
    return signals
