"""
clear_stale_state.py — Clears stale is_target_ad flags from Redis for specific phone numbers.
Run this once after deploying the fix to reset any users who were incorrectly flagged.
"""
import sys
import json
from redis_client import get_upstash_redis

redis_conn = get_upstash_redis()

def clear_phone_state(phone: str):
    """Remove is_target_ad flag and reset stage to NEW for a phone number."""
    redis_key = f"user_state:{phone}"
    raw = redis_conn.get(redis_key)
    
    if not raw:
        print(f"  No state found in Redis for {phone}")
        return
    
    state = json.loads(raw)
    print(f"  BEFORE: {json.dumps(state, indent=2)}")
    
    # Reset the targeting flag and stage
    state["is_target_ad"] = False
    state["stage"] = "NEW"
    state["timing"] = None
    state["package"] = None
    state["fee"] = None
    state["low_confidence_count"] = 0
    
    redis_conn.set(redis_key, json.dumps(state), ex=60 * 60 * 24 * 30)
    print(f"  AFTER:  is_target_ad={state['is_target_ad']}, stage={state['stage']}")
    print(f"  [OK] State reset for {phone}")

if __name__ == "__main__":
    phones = sys.argv[1:] if len(sys.argv) > 1 else ["917361045453"]
    
    print("=" * 60)
    print("[CLEANUP] Clearing stale is_target_ad state from Redis")
    print("=" * 60)
    
    for phone in phones:
        print(f"\n[PHONE] Phone: {phone}")
        clear_phone_state(phone)
    
    print("\n[DONE] Restart the server to pick up code changes.")
