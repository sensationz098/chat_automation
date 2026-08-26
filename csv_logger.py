"""
csv_logger.py — Thread-safe CSV audit logger.
Logs all messages (customer, ai, agent) to a local CSV file.
Auto-migrates existing CSV if new columns are added (runs once at startup).
"""

import csv
import os
import threading
from datetime import datetime, timezone

CSV_PATH = "chat_data.csv"
FIELDNAMES = ["timestamp", "phone", "role", "message", "qdrant_sources", "retrieval_query"]

# Thread lock to prevent CSV corruption from concurrent workers
_csv_lock = threading.Lock()
_schema_ready = False  # Ensures migration runs only once per process start


def _ensure_csv_exists():
    """
    Creates the CSV with correct headers if it doesn't exist.
    If it exists but has outdated columns, migrates it automatically
    (rewrites header + all rows to include new columns — no data loss).
    Runs the schema check only ONCE per server startup for performance.
    """
    global _schema_ready
    if _schema_ready:
        return

    with _csv_lock:
        if _schema_ready:  # Double-check after lock
            return

        if not os.path.exists(CSV_PATH):
            with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
                writer.writeheader()
            print(f"[csv_logger] Created new CSV: {CSV_PATH}")
        else:
            # Check if existing headers match current FIELDNAMES
            try:
                with open(CSV_PATH, "r", encoding="utf-8") as f:
                    reader = csv.DictReader(f)
                    existing_fields = list(reader.fieldnames or [])

                if existing_fields != FIELDNAMES:
                    # Schema changed — read all rows and rewrite with new columns
                    with open(CSV_PATH, "r", encoding="utf-8") as f:
                        reader = csv.DictReader(f)
                        rows = list(reader)

                    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
                        writer.writeheader()
                        for row in rows:
                            writer.writerow({k: row.get(k, "") for k in FIELDNAMES})

                    print(f"[csv_logger] Migrated CSV: {existing_fields} → {FIELDNAMES} ({len(rows)} rows preserved)")
            except Exception as e:
                print(f"[csv_logger] Schema migration failed (non-fatal): {e}")

        _schema_ready = True


def log_message(phone: str, role: str, message: str, sources: str = "", retrieval_query: str = ""):
    """
    Appends one conversation row to the CSV file.

    Args:
        phone:            Customer's WhatsApp number
        role:             'user', 'ai', or 'agent'
        message:          The full message text sent to the user
        sources:          Qdrant chunks used to generate the reply (pipe-separated previews)
        retrieval_query:  Exact query string sent to Qdrant to fetch those chunks
    """
    _ensure_csv_exists()

    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phone": phone,
        "role": role,
        "message": message,
        "qdrant_sources": sources,
        "retrieval_query": retrieval_query,
    }

    with _csv_lock:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writerow(row)

    safe_msg = message[:80].encode("ascii", errors="replace").decode("ascii")
    src_flag = " [+sources]" if sources else ""
    print(f"[csv_logger] [{role.upper()}]{src_flag} {phone}: {safe_msg}...")


# ---------------------------------------------------------------------------
# Async non-blocking wrapper using asyncio.to_thread
# ---------------------------------------------------------------------------
import asyncio

async def log_message_async(phone: str, role: str, message: str, sources: str = "", retrieval_query: str = ""):
    """Non-blocking async wrapper to append row to CSV file on disk."""
    return await asyncio.to_thread(log_message, phone, role, message, sources, retrieval_query)

