import csv
import os
from datetime import datetime, timezone

CSV_PATH = "chat_data.csv"
FIELDNAMES = ["timestamp", "phone", "role", "message"]

def _ensure_csv_exists():
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()
 
 
def log_message(phone: str, role: str, message: str):
    """
    role should be one of: 'customer', 'ai', 'agent'
    Appends one row to the CSV and prints it to the terminal.
    """
    _ensure_csv_exists()
 
    row = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phone": phone,
        "role": role,
        "message": message,
    }
 
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writerow(row)
 
    print(f"[csv_logger] [{role.upper()}] {phone}: {message}")
 
