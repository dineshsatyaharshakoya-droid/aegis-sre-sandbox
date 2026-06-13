import asyncio
from typing import Callable, Awaitable
from aegis_sre.orchestrator.schemas import TelemetryEvent
from aegis_sre.telemetry.logger import logger
from aegis_sre.orchestrator.safety import safety_policy

class TelemetryQueue:
    def __init__(self, max_size: int = 1000):
        self.queue = asyncio.Queue(maxsize=max_size)
        self.worker_task = None
        
    async def start_worker(self, process_callback: Callable[[TelemetryEvent], Awaitable[None]]):
        """Starts the background worker to process items from the queue sequentially"""
        logger.info("starting_telemetry_queue_worker", max_size=self.queue.maxsize)
        
        async def worker():
            while True:
                # Block for the next item. A cancellation here means a clean
                # shutdown with NO outstanding item, so we must NOT call
                # task_done() (that would raise "task_done() called too many
                # times" and mask the shutdown).
                try:
                    telemetry = await self.queue.get()
                except asyncio.CancelledError:
                    logger.info("queue_worker_cancelled")
                    break

                # From here on an item is owned by this iteration and MUST be
                # balanced with exactly one task_done(), regardless of outcome.
                try:
                    logger.info("dequeued_event_for_processing", event_id=telemetry.event_id, current_qsize=self.queue.qsize())

                    # GOD NODE KILL SWITCH: Enforce a strict timeout on the LangGraph swarm
                    # If the AI loops infinitely or gets stuck, this supervisor forcefully aborts it.
                    try:
                        await asyncio.wait_for(process_callback(telemetry), timeout=safety_policy.get_timeout())
                    except asyncio.TimeoutError:
                        logger.error("god_node_kill_switch_activated", event_id=telemetry.event_id, reason="execution_timeout_exceeded")

                except asyncio.CancelledError:
                    # Cancelled mid-processing: release this item, then exit.
                    self.queue.task_done()
                    logger.info("queue_worker_cancelled")
                    break
                except Exception as e:
                    logger.error("queue_worker_error", error=str(e))
                    self.queue.task_done()
                else:
                    self.queue.task_done()
                    
        self.worker_task = asyncio.create_task(worker())
        
    async def enqueue(self, telemetry: TelemetryEvent) -> bool:
        if self.queue.full():
            logger.warning("telemetry_queue_full_dropping_event", event_id=telemetry.event_id)
            return False
        
        await self.queue.put(telemetry)
        logger.info("enqueued_event", event_id=telemetry.event_id, qsize=self.queue.qsize())
        return True

# Global singleton
telemetry_queue = TelemetryQueue()
