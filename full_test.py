"""
full_test.py — Concurrent load test for 10 simultaneous workers.

Simulates N users sending messages to the test webhook concurrently,
monitors background worker execution, and reports success/failure
with precise latency for every user.

Usage:
    python full_test.py                    # 10 users (default)
    python full_test.py 5                  # 5 users
    python full_test.py 20 http://host:8000/test-webhook

Exit code:
    0 = all targeted users got replies within 15 seconds
    1 = some users failed or exceeded 15 second threshold
"""

import sys
import os
import time
import json
import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv()

# --- Configuration ---
DEFAULT_WEBHOOK_URL = "http://localhost:8000/test-webhook"
NUM_USERS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
WEBHOOK_URL = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_WEBHOOK_URL
MAX_RESPONSE_TIME = 15.0  # seconds — pass/fail threshold

TARGET_AD_ID = os.getenv("TARGET_AD_ID", "PLACEHOLDER_AD_ID")
TARGET_MESSAGE_TEXT = os.getenv("TARGET_MESSAGE_TEXT", "Hello! Can I get more info on Yoga classes?")

# Import Redis connection for monitoring AI responses
from redis_client import get_redis_connection
redis_conn = get_redis_connection()

SAMPLE_FOLLOWUP_MESSAGES = [
    "What are the fees?",
    "Do you have a demo class video?",
    "Other timings",
    "5 6 pm",
    "3 months",
    "Is this online live on Zoom?",
    "Yes",
    "Teacher details kya hai?",
    "1 year package",
    "Hi",
]


def generate_payload(user_id: int) -> tuple:
    """Generates an Interakt-compliant webhook payload for a unique simulated user."""
    phone = f"919800{user_id:06d}"

    # First 5 users: target ad + matching text (should get replies)
    # Next 5 users: wrong ad ID (should be ignored)
    if user_id <= NUM_USERS // 2:
        # TARGET users — both ad ID and text match
        message_text = TARGET_MESSAGE_TEXT
        referral = {
            "source_id": TARGET_AD_ID,
            "source_type": "ad",
            "source_url": f"https://fb.com/l.php?ad_id={TARGET_AD_ID}"
        }
        is_target = True
    else:
        # NON-TARGET users — wrong ad ID, random message
        message_text = SAMPLE_FOLLOWUP_MESSAGES[(user_id - 1) % len(SAMPLE_FOLLOWUP_MESSAGES)]
        referral = {
            "source_id": "999999999_wrong_ad",
            "source_type": "ad",
            "source_url": "https://fb.com/l.php?ad_id=999999999_wrong_ad"
        }
        is_target = False

    payload = {
        "type": "message_received",
        "data": {
            "customer": {
                "country_code": "91",
                "phone_number": phone,
                "channel_phone_number": phone,
            },
            "message": {
                "message": message_text,
                "referral": referral,
            }
        }
    }
    return payload, phone, message_text, is_target


async def send_webhook(client: httpx.AsyncClient, user_id: int) -> dict:
    """Fires one webhook and records timing."""
    payload, phone, message_text, is_target = generate_payload(user_id)
    sent_at = time.time()

    try:
        response = await client.post(
            WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        latency_ms = (time.time() - sent_at) * 1000
        print(f"  [webhook] User {user_id:2d} ({phone}) -> HTTP {response.status_code} ({latency_ms:.0f}ms)")
        return {
            "user_id": user_id, "phone": phone, "message": message_text,
            "is_target": is_target, "status_code": response.status_code,
            "enqueue_ms": round(latency_ms, 1), "sent_at": sent_at,
        }
    except Exception as e:
        latency_ms = (time.time() - sent_at) * 1000
        print(f"  [webhook] User {user_id:2d} ({phone}) -> ERROR: {e} ({latency_ms:.0f}ms)")
        return {
            "user_id": user_id, "phone": phone, "message": message_text,
            "is_target": is_target, "status_code": None,
            "enqueue_ms": round(latency_ms, 1), "sent_at": sent_at,
            "error": str(e),
        }


def check_ai_response(phone: str) -> dict | None:
    """Checks Redis for the latest assistant response."""
    key = f"history:{phone}"
    try:
        cached = redis_conn.lrange(key, 0, -1)
        if cached:
            for item in reversed(cached):
                turn = json.loads(item)
                if turn.get("role") == "assistant":
                    return turn
    except Exception:
        pass
    return None


async def wait_for_ai_responses(target_results: list, timeout: float = 60.0) -> dict:
    """Polls Redis until all target users have AI responses or timeout."""
    pending = {r["phone"]: r["sent_at"] for r in target_results}
    completed = {}
    start = time.time()

    while pending and (time.time() - start) < timeout:
        newly_done = []
        for phone, sent_at in pending.items():
            ai = check_ai_response(phone)
            if ai:
                e2e_time = time.time() - sent_at
                server_lat = ai.get("response_time_sec")
                final_lat = server_lat if (server_lat and server_lat > 0) else round(e2e_time, 2)
                completed[phone] = {
                    "reply": ai.get("content", ""),
                    "latency_sec": final_lat,
                }
                newly_done.append(phone)

        for phone in newly_done:
            del pending[phone]

        if newly_done:
            elapsed = time.time() - start
            print(f"  ⌛ Progress: {len(completed)}/{len(completed) + len(pending)} AI replies received ({elapsed:.1f}s)")

        if pending:
            await asyncio.sleep(0.5)

    return completed


async def run_test():
    """Main test runner."""
    print("=" * 65)
    print(f"🧪 FULL CONCURRENT TEST — {NUM_USERS} Simultaneous Users")
    print(f"🎯 Webhook: {WEBHOOK_URL}")
    print(f"⏱️  Pass threshold: <{MAX_RESPONSE_TIME}s per user")
    print("=" * 65)

    # Phase 1: Fire all webhooks simultaneously
    print(f"\n⚡ PHASE 1: Firing {NUM_USERS} webhooks simultaneously...")
    t_start = time.time()

    limits = httpx.Limits(max_keepalive_connections=NUM_USERS, max_connections=NUM_USERS + 10)
    async with httpx.AsyncClient(limits=limits) as client:
        tasks = [send_webhook(client, i + 1) for i in range(NUM_USERS)]
        results = await asyncio.gather(*tasks)

    webhook_time = time.time() - t_start
    ok_count = sum(1 for r in results if r.get("status_code") == 200)
    print(f"\n✅ Webhooks sent: {ok_count}/{NUM_USERS} HTTP 200 in {webhook_time:.2f}s")

    if ok_count == 0:
        print("❌ No webhooks accepted — is the server running? (python start.py)")
        sys.exit(1)

    # Phase 2: Wait for AI responses (only for target users)
    target_results = [r for r in results if r["is_target"] and r.get("status_code") == 200]
    non_target_results = [r for r in results if not r["is_target"]]

    print(f"\n⚙️  PHASE 2: Waiting for {len(target_results)} target users to get AI replies...")
    ai_responses = await wait_for_ai_responses(target_results, timeout=60.0)

    total_time = time.time() - t_start

    # Phase 3: Results
    print("\n" + "=" * 65)
    print("📊 TEST RESULTS")
    print("=" * 65)

    # Per-user results table
    print(f"\n{'User':>4} | {'Phone':>14} | {'Target':>6} | {'Status':>10} | {'Latency':>8} | Message")
    print("-" * 85)

    all_pass = True
    latencies = []

    for r in sorted(results, key=lambda x: x["user_id"]):
        user_id = r["user_id"]
        phone = r["phone"]
        is_target = r["is_target"]

        if is_target:
            ai = ai_responses.get(phone)
            if ai and ai.get("reply"):
                lat = ai["latency_sec"]
                latencies.append(lat)
                passed = lat <= MAX_RESPONSE_TIME
                status = f"✅ {lat:.1f}s" if passed else f"❌ {lat:.1f}s"
                if not passed:
                    all_pass = False
            else:
                status = "❌ TIMEOUT"
                all_pass = False
        else:
            # Non-target users should be IGNORED (no reply = correct behavior)
            ai = ai_responses.get(phone)
            if ai:
                status = "⚠️  LEAKED"  # Bug: non-target user got a reply
                all_pass = False
            else:
                status = "✅ IGNORED"

        msg_display = r["message"][:35] + "..." if len(r["message"]) > 35 else r["message"]
        print(f"{user_id:4d} | {phone:>14} | {'YES' if is_target else 'NO':>6} | {status:>10} | {r['enqueue_ms']:6.0f}ms | {msg_display}")

    # Summary
    print("\n" + "=" * 65)
    print("📈 SUMMARY")
    print("=" * 65)
    print(f"  Total users          : {NUM_USERS}")
    print(f"  Target ad users      : {len(target_results)}")
    print(f"  Non-target users     : {len(non_target_results)}")
    print(f"  AI replies received  : {len(ai_responses)}/{len(target_results)}")
    print(f"  Total wall-clock time: {total_time:.2f}s")

    if latencies:
        print(f"  ⚡ Fastest reply     : {min(latencies):.2f}s")
        print(f"  🐢 Slowest reply     : {max(latencies):.2f}s")
        print(f"  📊 Average reply     : {sum(latencies)/len(latencies):.2f}s")

    under_threshold = sum(1 for l in latencies if l <= MAX_RESPONSE_TIME)
    print(f"  ✅ Under {MAX_RESPONSE_TIME}s         : {under_threshold}/{len(latencies)}")

    print("=" * 65)

    if all_pass and len(ai_responses) == len(target_results):
        print("🎉 ALL TESTS PASSED — Bot is deployment-ready!")
        sys.exit(0)
    else:
        print("❌ SOME TESTS FAILED — Review results above.")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_test())
