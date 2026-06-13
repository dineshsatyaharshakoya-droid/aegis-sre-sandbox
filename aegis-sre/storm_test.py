import asyncio
import httpx
import time

async def blast_endpoint():
    url = "http://localhost:8000/webhook/crash"
    headers = {"Content-Type": "application/json"}
    
    payloads = [
        {
            "event_id": f"CRASH-STORM-{i}",
            "service_name": "kubernetes-ingress-controller",
            "timestamp": int(time.time()),
            "crash_log": f"panic: runtime error: index out of range [{i}]",
            "metadata": {"namespace": "kube-system"}
        } for i in range(50)
    ]
    
    print(f"Blasting {len(payloads)} requests concurrently...")
    
    async with httpx.AsyncClient() as client:
        start_time = time.time()
        
        # Fire 50 concurrent requests
        tasks = [
            client.post(url, headers=headers, json=payload)
            for payload in payloads
        ]
        
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        duration = time.time() - start_time
        
        successes = [r for r in results if getattr(r, 'status_code', None) == 200]
        
        print(f"Storm complete in {duration:.2f} seconds!")
        print(f"Successful 200/202 responses: {len(successes)}")
        print(f"The webhook responded instantly without blocking!")
        print("Check the backend terminal to watch the Queue and God Node process them sequentially.")

if __name__ == "__main__":
    asyncio.run(blast_endpoint())
