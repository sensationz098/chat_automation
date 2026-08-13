"""
run_workers.py — Launches multiple worker.py processes in parallel.
Each worker process independently pulls jobs off the Redis queue.

Usage:
    python run_workers.py           (defaults to 10 workers)
    python run_workers.py 20        (or specify how many workers)

Press Ctrl+C to stop all workers cleanly.
"""

# import sys
# import subprocess
# import signal

# NUM_WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

# processes = []

# def shutdown(signum, frame):
#     print("\n[run_workers] Stopping all worker processes...")
#     for p in processes:
#         try:
#             p.terminate()
#         except Exception:
#             pass
#     sys.exit(0)

# signal.signal(signal.SIGINT, shutdown)
# signal.signal(signal.SIGTERM, shutdown)

# if __name__ == "__main__":
#     print(f"[run_workers] Starting {NUM_WORKERS} worker processes...")

#     for i in range(NUM_WORKERS):
#         p = subprocess.Popen([sys.executable, "worker.py"])
#         processes.append(p)
#         print(f"  Worker {i + 1} started (PID {p.pid})")

#     print(f"\n[run_workers] All {NUM_WORKERS} workers running. Press Ctrl+C to stop them.")

#     for p in processes:
#         p.wait()


import sys
import time
import subprocess
import signal

NUM_WORKERS = int(sys.argv[1]) if len(sys.argv) > 1 else 40
processes = {}

def start_worker(index):
    p = subprocess.Popen([sys.executable, "worker.py"])
    print(f"  Worker {index + 1} started (PID {p.pid})")
    return p

def shutdown(signum, frame):
    print("\n[run_workers] Stopping all worker processes...")
    for p in processes.values():
        try:
            p.terminate()
        except Exception:
            pass
    sys.exit(0)

signal.signal(signal.SIGINT, shutdown)
signal.signal(signal.SIGTERM, shutdown)

if __name__ == "__main__":
    print(f"[run_workers] Starting {NUM_WORKERS} worker processes...")
    for i in range(NUM_WORKERS):
        processes[i] = start_worker(i)

    print(f"\n[run_workers] All {NUM_WORKERS} workers running. Press Ctrl+C to stop them.")

    # Monitoring Loop: Restart workers if they die unexpectedly
    while True:
        time.sleep(2)
        for i, p in list(processes.items()):
            poll = p.poll()
            if poll is not None:  # Process ended or crashed
                print(f"⚠️ Worker {i + 1} (PID {p.pid}) exited with code {poll}. Restarting...")
                processes[i] = start_worker(i)
