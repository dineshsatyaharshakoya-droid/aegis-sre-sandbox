import requests
import time

BASE_URL = "http://localhost:8000/webhook/sentry"

incidents = [
    {
        "action": "created",
        "project_name": "stripe-checkout-api",
        "id": "LIVE-DEMO-001",
        "url": "https://sentry.io/issues/LIVE-DEMO-001/",
        "data": {
            "event": {
                "event_id": "LIVE-CRASH-STRIPE-001",
                "title": "NullPointerException: payment_intent.customer is null",
                "culprit": "stripe_checkout.create_session",
                "exception": {
                    "values": [{
                        "type": "NullPointerException",
                        "value": "payment_intent.customer is null — Stripe webhook received before user profile was persisted to DB",
                        "stacktrace": {
                            "frames": [
                                {"filename": "/app/webhooks/stripe_handler.py", "function": "handle_checkout_complete", "lineno": 87},
                                {"filename": "/app/models/customer.py", "function": "get_or_create", "lineno": 34}
                            ]
                        }
                    }]
                }
            }
        }
    },
    {
        "action": "created",
        "project_name": "kubernetes-ingress-controller",
        "id": "LIVE-DEMO-002",
        "url": "https://sentry.io/issues/LIVE-DEMO-002/",
        "data": {
            "event": {
                "event_id": "LIVE-CRASH-K8S-002",
                "title": "TLSHandshakeError: certificate has expired for *.prod.aegis.io",
                "culprit": "ingress_controller.ssl_termination",
                "exception": {
                    "values": [{
                        "type": "TLSHandshakeError",
                        "value": "x509: certificate has expired — cert issued 2025-06-12, expired 2026-06-12T00:00:00Z, current time 2026-06-13T04:56:00Z",
                        "stacktrace": {
                            "frames": [
                                {"filename": "/app/proxy/tls_manager.go", "function": "TerminateSSL", "lineno": 203},
                                {"filename": "/app/certs/auto_renew.go", "function": "CheckExpiry", "lineno": 41}
                            ]
                        }
                    }]
                }
            }
        }
    },
    {
        "action": "created",
        "project_name": "realtime-notification-service",
        "id": "LIVE-DEMO-003",
        "url": "https://sentry.io/issues/LIVE-DEMO-003/",
        "data": {
            "event": {
                "event_id": "LIVE-CRASH-NOTIF-003",
                "title": "RateLimitExceeded: Firebase Cloud Messaging quota hit (500k/day)",
                "culprit": "notification_service.push_dispatcher",
                "exception": {
                    "values": [{
                        "type": "RateLimitExceeded",
                        "value": "FCM daily quota exceeded — 500,000 push notifications sent, 23,847 queued and dropped. Retry-After: 3600s",
                        "stacktrace": {
                            "frames": [
                                {"filename": "/app/dispatchers/fcm_client.py", "function": "send_batch", "lineno": 156},
                                {"filename": "/app/queue/priority_router.py", "function": "flush_queue", "lineno": 72}
                            ]
                        }
                    }]
                }
            }
        }
    }
]

print()
print("🔴🔴🔴 LIVE DEMO — FIRING 3 PRODUCTION INCIDENTS 🔴🔴🔴")
print("=" * 55)

for i, payload in enumerate(incidents, 1):
    print(f"\n  💥 [{i}/3] {payload['project_name']}")
    print(f"     {payload['data']['event']['title']}")
    resp = requests.post(BASE_URL, json=payload)
    print(f"     → {resp.json()['status'].upper()}")
    time.sleep(2)

print("\n" + "=" * 55)
print("✅ ALL INCIDENTS LIVE — TELL YOUR FRIEND TO DEPLOY!")
print("=" * 55)
print()
