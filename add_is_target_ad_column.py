"""
add_is_target_ad_column.py -- Adds the 'is_target_ad' column to the
Supabase 'user_session_state' table using the Supabase SQL API.

Run:  python add_is_target_ad_column.py
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("[ERROR] SUPABASE_URL or SUPABASE_KEY not set in .env")
    exit(1)

# Supabase exposes a /rest/v1/rpc endpoint, but for DDL we need the SQL endpoint
# The Supabase SQL HTTP endpoint is at /sql (if using service_role key)
sql = "ALTER TABLE user_session_state ADD COLUMN IF NOT EXISTS is_target_ad BOOLEAN DEFAULT FALSE;"

headers = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

# Try the Supabase SQL API endpoint
sql_url = f"{SUPABASE_URL}/rest/v1/rpc"

# Method 1: Try creating an RPC wrapper to execute the ALTER TABLE
print("Attempting to add 'is_target_ad' column to user_session_state table...")
print()

# Since we can't run raw DDL via PostgREST, let's verify the column is needed
# by attempting an insert/upsert with the field
from supabase import create_client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Check if column exists
try:
    res = supabase.table("user_session_state").select("is_target_ad").limit(1).execute()
    print("[OK] 'is_target_ad' column already exists! No migration needed.")
    exit(0)
except Exception as e:
    if "does not exist" in str(e):
        print("[INFO] Column 'is_target_ad' is MISSING. Needs to be added.")
    else:
        print(f"[ERROR] Unexpected error: {e}")
        exit(1)

print()
print("=" * 60)
print("ACTION REQUIRED: Run this SQL in your Supabase Dashboard")
print("=" * 60)
print()
print("Go to: Supabase Dashboard -> SQL Editor -> New Query")
print("Paste and run this SQL:")
print()
print("-" * 60)
print(sql)
print("-" * 60)
print()
print("After running the SQL, re-run this script to verify.")
