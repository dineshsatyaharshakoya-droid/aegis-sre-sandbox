import asyncio
import httpx

async def fire_sentry_webhook():
    url = "http://localhost:8000/webhook/sentry"
    headers = {"Content-Type": "application/json"}
    
    # Simulating a realistic Sentry Issue Webhook payload
    payload = {
        "action": "created",
        "project_name": "aegis-payment-service",
        "id": "77777777777",
        "url": "https://sentry.io/organizations/aegis/issues/77777777777/",
        "data": {
            "event": {
                "event_id": "LATEST-CRASH-TEST-999",
                "title": "IndexError: list index out of range",
                "culprit": "payment_processor.checkout",
                "exception": {
                    "values": [
                        {
                            "type": "IndexError",
                            "value": "list index out of range",
                            "stacktrace": {
                                "frames": [
                                    {
                                        "filename": "/app/main.py",
                                        "function": "process_order",
                                        "lineno": 111
                                    },
                                    {
                                        "filename": "/app/payment_processor/checkout.py",
                                        "function": "calculate_tax",
                                        "lineno": 222
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }
    }
    
    print("Firing simulated Sentry Webhook...")
    async with httpx.AsyncClient() as client:
        response = await client.post(url, headers=headers, json=payload)
        print(f"Response Code: {response.status_code}")
        print(f"Response Body: {response.json()}")

if __name__ == "__main__":
    asyncio.run(fire_sentry_webhook())
