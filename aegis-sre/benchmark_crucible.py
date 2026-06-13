import asyncio
import httpx
import time
import uuid

# URL of the Aegis FastAPI Webhook
WEBHOOK_URL = "http://localhost:8000/webhook/crash"

async def send_crash_event(client, index):
    """Sends a single crash event to Aegis."""
    event_id = f"CRASH-BENCH-{uuid.uuid4().hex[:8]}"
    payload = {
        "event_id": event_id,
        "service_name": f"payment-service-pod-{index}",
        "timestamp": time.time(),
        "crash_log": "TypeError: 'NoneType' object is not subscriptable\n  File 'main.py', line 42, in process_payment",
        "metadata": {"namespace": "production", "benchmark_run": True}
    }
    
    start_time = time.time()
    try:
        response = await client.post(WEBHOOK_URL, json=payload, timeout=10.0)
        latency = time.time() - start_time
        return response.status_code == 200, latency
    except Exception as e:
        print(f"Request {index} failed: {e}")
        return False, time.time() - start_time

async def run_benchmark(num_requests=50, concurrency=10):
    """Fires multiple concurrent crash events at Aegis to test async throughput."""
    print("========================================")
    print(f"🔥 AEGIS CHAOS CRUCIBLE BENCHMARK 🔥")
    print(f"Targeting: {WEBHOOK_URL}")
    print(f"Total Events: {num_requests}")
    print(f"Concurrency Level: {concurrency}")
    print("========================================\n")

    limits = httpx.Limits(max_connections=concurrency)
    async with httpx.AsyncClient(limits=limits) as client:
        start_time = time.time()
        
        # Create tasks
        tasks = [send_crash_event(client, i) for i in range(num_requests)]
        
        # Execute concurrently
        results = await asyncio.gather(*tasks)
        
        total_time = time.time() - start_time
        
    # Calculate metrics
    successes = sum(1 for r in results if r[0])
    failures = num_requests - successes
    avg_latency = sum(r[1] for r in results) / num_requests
    rps = num_requests / total_time
    
    print("\n========================================")
    print("📊 BENCHMARK RESULTS 📊")
    print("========================================")
    print(f"Total Time Taken: {total_time:.2f} seconds")
    print(f"Requests Per Second (Throughput): {rps:.2f} req/s")
    print(f"Average Webhook Latency: {avg_latency*1000:.2f} ms")
    print(f"Successful Ingestions: {successes}/{num_requests}")
    print(f"Failed Ingestions: {failures}")
    print("========================================")
    print("Note: Aegis processes these in the background. Check the server logs to observe the concurrent LangGraph resolutions!")

if __name__ == "__main__":
    asyncio.run(run_benchmark(num_requests=500, concurrency=100))
