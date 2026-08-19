"""
csv_logger.py — Thread-safe CSV audit logger.
Logs all messages (customer, ai, agent) to a local CSV file.
"""

import csv
import os
import threading
from datetime import datetime, timezone

CSV_PATH = "chat_data.csv"
FIELDNAMES = ["timestamp", "phone", "role", "message"]

# Thread lock to prevent CSV corruption from concurrent workers
_csv_lock = threading.Lock()


def _ensure_csv_exists():
    if not os.path.exists(CSV_PATH):
        with _csv_lock:
            # Double-check after acquiring lock
            if not os.path.exists(CSV_PATH):
                with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                    writer.writeheader()


def log_message(phone: str, role: str, message: str):
    """
    role should be one of: 'customer', 'ai', 'agent'
    Appends one row to the CSV and prints it to the terminal.
    Thread-safe via file lock.
    """
    _ensure_csv_exists()

    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phone": phone,
        "role": role,
        "message": message,
    }

    with _csv_lock:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writerow(row)

    safe_message = message[:80].encode('ascii', errors='replace').decode('ascii')
    print(f"[csv_logger] [{role.upper()}] {phone}: {safe_message}...")
