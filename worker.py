# """
# Run this in its OWN terminal, separate from uvicorn:
#     python worker.py

# Run this command in MULTIPLE terminals to get multiple workers
# processing messages in parallel — that's the actual mechanism that
# lets you handle 500 people messaging at once. Each worker independently
# pulls the next job off the queue and runs it.
# """

# import os
# from dotenv import load_dotenv
# from redis import Redis
# from rq import Queue
# from rq.worker import SimpleWorker

# load_dotenv()

# redis_conn = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))

# if __name__ == "__main__":
#     queue = Queue("interakt_messages", connection=redis_conn)

#     # SimpleWorker runs jobs in the same process instead of forking a
#     # child process per job. RQ's default Worker uses os.fork(), which
#     # doesn't exist on Windows — SimpleWorker works cross-platform.
#     worker = SimpleWorker([queue], connection=redis_conn)
#     print("Worker started, waiting for jobs...")
#     worker.work()


"""
Launches multiple worker.py processes at once, so you don't have to
manually open 10-20 terminal windows. This is what actually creates
real parallelism -- each process independently pulls the next job off
the queue and works on it at the same time as the others.

Usage:
    python run_workers.py           (defaults to 10 workers)
    python run_workers.py 20        (or specify how many)

Press Ctrl+C to stop all of them.
"""

import sys
import subprocess
import signal

NUM_WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

processes = []

def shutdown(signum, frame):
    print("\nStopping all workers...")
    for p in processes:
        p.terminate()
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)

if __name__ == "__main__":
    print(f"Starting {NUM_WORKERS} worker processes...")

    for i in range(NUM_WORKERS):
        p = subprocess.Popen([sys.executable, "worker.py"])
        processes.append(p)
        print(f"  Worker {i + 1} started (pid {p.pid})")

    print(f"\nAll {NUM_WORKERS} workers running. Press Ctrl+C to stop them all.")

    # Wait for all of them (keeps this script alive so Ctrl+C works cleanly)
    for p in processes:
        p.wait()