"""
worker.py — Background RQ worker process.
Pulls message-processing jobs off the 'interakt_messages' Redis queue and executes them.
SimpleWorker is cross-platform compatible (works on Windows without os.fork).
"""

import os
from dotenv import load_dotenv
from redis import Redis
from rq import Queue
from rq.worker import SimpleWorker
from redis_client import get_redis_connection

load_dotenv()

redis_conn = get_redis_connection()

if __name__ == "__main__":
    queue = Queue("interakt_messages", connection=redis_conn)
    worker = SimpleWorker([queue], connection=redis_conn)
    print("[worker] Worker started, waiting for jobs on 'interakt_messages' queue...")
    worker.work()
