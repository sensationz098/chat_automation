"""
Run this in its OWN terminal, alongside uvicorn and worker.py:
    python scheduler.py

This is what actually fires the delayed batch-processing jobs after
the wait window passes. Without this running, messages will pile up
in the batch and never get processed — this is a required process,
not optional, once batching is in use.
"""

import os
import time
from dotenv import load_dotenv
from redis import Redis
from rq import Queue
from rq_scheduler import Scheduler

load_dotenv()

redis_conn = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
queue = Queue("interakt_messages", connection=redis_conn)
scheduler = Scheduler(queue=queue, connection=redis_conn)

if __name__ == "__main__":
    print("Scheduler started, watching for delayed batch jobs...")
    scheduler.run()