"""
worker.py — High-Concurrency Multi-threaded RQ worker process.
Pulls message-processing jobs off the 'interakt_messages' Redis queue and executes them in parallel
using a ThreadPoolExecutor so 100 simultaneous users get replies in seconds instead of minutes.
"""

import time
import concurrent.futures

from dotenv import load_dotenv
from rq import Queue
from redis_client import get_redis_connection

load_dotenv()

redis_conn = get_redis_connection()

QUEUE_NAME = "interakt_messages"
MAX_WORKERS = 50


def process_job(job):
    """Execute one RQ job."""
    try:
        print(f"[worker] Starting job {job.id}")

        result = job.perform()

        print(f"[worker] Finished job {job.id}")

        return result

    except Exception as e:
        print(f"[worker] Job {job.id} failed: {e}")
        raise


def run_worker():
    queue = Queue(
        QUEUE_NAME,
        connection=redis_conn
    )

    print("=" * 60)
    print("🚀 Concurrent RQ Worker")
    print("=" * 60)
    print(f"Queue          : {QUEUE_NAME}")
    print(f"Max concurrency: {MAX_WORKERS}")
    print("=" * 60)

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    )

    futures = set()

    try:
        while True:

            # Remove completed futures
            completed = {
                future
                for future in futures
                if future.done()
            }

            for future in completed:
                futures.remove(future)

                try:
                    future.result()
                except Exception as e:
                    print(f"[worker] Job error: {e}")

            # Number of available slots
            available_slots = MAX_WORKERS - len(futures)

            if available_slots > 0:

                # Get jobs from RQ's queue
                jobs = queue.get_jobs(
                    offset=0,
                    length=available_slots
                )

                for job in jobs:

                    print(
                        f"[worker] Submitting job {job.id} "
                        f"({len(futures) + 1}/{MAX_WORKERS})"
                    )

                    future = executor.submit(
                        process_job,
                        job
                    )

                    futures.add(future)

            time.sleep(0.1)

    except KeyboardInterrupt:

        print("\n[worker] Stopping...")

        executor.shutdown(
            wait=True
        )

        print("[worker] Stopped.")


if __name__ == "__main__":
    run_worker()


