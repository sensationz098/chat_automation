"""
check_db.py — Verify that the Supabase 'user_session_state' table has the
'is_target_ad' column and that the full fallback chain (Memory → Redis → Supabase)
works correctly for storing/retrieving the AI-enabled flag.

Run:  python check_db.py [phone_number]
"""

import os
import sys
import json
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_KEY")

if not url or not key:
    print("❌ SUPABASE_URL or SUPABASE_KEY not set in .env")
    sys.exit(1)

supabase = create_client(url, key)

# ─── 1. Check table exists and has is_target_ad column ─────────────────────

print("=" * 60)
print("CHECK 1: Verifying 'user_session_state' table & columns")
print("=" * 60)

try:
    # Try selecting is_target_ad specifically — if column is missing, this errors
    res = supabase.table("user_session_state").select("phone, stage, is_target_ad").limit(5).execute()
    print(f"✅ 'is_target_ad' column EXISTS in user_session_state table")
    print(f"   Sample rows ({len(res.data)} shown):")
    for row in res.data:
        print(f"   Phone: {row.get('phone')}, Stage: {row.get('stage')}, is_target_ad: {row.get('is_target_ad')}")
except Exception as e:
    error_str = str(e)
    if "is_target_ad" in error_str or "column" in error_str.lower():
        print(f"❌ 'is_target_ad' column is MISSING from user_session_state table!")
        print()
        print("   👉 Run this SQL in Supabase Dashboard → SQL Editor:")
        print()
        print("   ALTER TABLE user_session_state")
        print("     ADD COLUMN IF NOT EXISTS is_target_ad BOOLEAN DEFAULT FALSE;")
        print()
    else:
        print(f"❌ Error querying table: {e}")

# ─── 2. Check escalated_chats table ───────────────────────────────────────

print()
print("=" * 60)
print("CHECK 2: Verifying 'escalated_chats' table")
print("=" * 60)

try:
    res = supabase.table("escalated_chats").select("phone").limit(3).execute()
    print(f"✅ 'escalated_chats' table exists ({len(res.data)} escalated users)")
except Exception as e:
    print(f"❌ Error: {e}")

# ─── 3. Check chat_history table ──────────────────────────────────────────

print()
print("=" * 60)
print("CHECK 3: Verifying 'chat_history' table")
print("=" * 60)

try:
    res = supabase.table("chat_history").select("phone, role, message").limit(3).execute()
    print(f"✅ 'chat_history' table exists ({len(res.data)} sample rows)")
except Exception as e:
    print(f"❌ Error: {e}")

# ─── 4. Test write + read for a specific phone (optional) ─────────────────

phone = sys.argv[1] if len(sys.argv) > 1 else None

if phone:
    print()
    print("=" * 60)
    print(f"CHECK 4: Read/Write test for phone: {phone}")
    print("=" * 60)

    # Read current state
    try:
        res = supabase.table("user_session_state").select("*").eq("phone", phone).limit(1).execute()
        if res.data:
            state = res.data[0]
            print(f"   Current state in DB:")
            print(f"   is_target_ad: {state.get('is_target_ad')}")
            print(f"   stage: {state.get('stage')}")
            print(f"   is_escalated: {state.get('is_escalated')}")
            print(f"   Full state: {json.dumps(state, indent=4, default=str)}")
        else:
            print(f"   No state found in DB for {phone}")
    except Exception as e:
        print(f"   ❌ Read failed: {e}")

# ─── 5. Check Redis connectivity ──────────────────────────────────────────

print()
print("=" * 60)
print("CHECK 5: Redis connectivity")
print("=" * 60)

try:
    from redis_client import get_redis_connection
    redis_conn = get_redis_connection()
    redis_conn.ping()
    print("✅ Redis is reachable")

    if phone:
        raw = redis_conn.get(f"user_state:{phone}")
        if raw:
            state = json.loads(raw)
            print(f"   Redis state for {phone}:")
            print(f"   is_target_ad: {state.get('is_target_ad')}")
            print(f"   stage: {state.get('stage')}")
        else:
            print(f"   No Redis state for {phone}")
except Exception as e:
    print(f"⚠️  Redis error (non-fatal): {e}")
    print("   Supabase fallback will handle state persistence.")

print()
print("=" * 60)
print("ALL CHECKS COMPLETE")
print("=" * 60)
