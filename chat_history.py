import os
import json
from dotenv import load_dotenv
from supabase import create_client
from redis_client import get_redis_connection
import time

redis_conn = get_redis_connection()

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

HISTORY_CACHE_TTL = 60 * 30    # 30 minutes
HISTORY_CACHE_LIMIT = 10       # how many recent turns matter for context


def _cache_key(phone: str) -> str:
    return f"history:{phone}"


def save_message(phone: str, role: str, text: str, response_time_sec: float = None):
    # Supabase is always the source of truth — write here first.
    payload = {"phone": phone, "role": role, "message": text}
    if response_time_sec is not None:
        payload["response_time_sec"] = response_time_sec
        print(f"[chat_history] {phone} ({role}) - Response Time: {response_time_sec}s")

    try:
        supabase.table("chat_history").insert(payload).execute()
    except Exception as e:
        # If response_time_sec column is missing in Supabase schema, fallback to basic insert
        if response_time_sec is not None:
            try:
                supabase.table("chat_history").insert({"phone": phone, "role": role, "message": text}).execute()
            except Exception as ex:
                print(f"[chat_history] Supabase insert failed: {ex}")
        else:
            print(f"[chat_history] Supabase insert failed (non-fatal): {e}")

    # Cache write is best-effort — never let a Redis hiccup break saving.
    try:
        key = _cache_key(phone)
        cache_item = {"role": role, "content": text}
        if response_time_sec is not None:
            cache_item["response_time_sec"] = response_time_sec
        redis_conn.rpush(key, json.dumps(cache_item))
        redis_conn.ltrim(key, -HISTORY_CACHE_LIMIT, -1)
        redis_conn.expire(key, HISTORY_CACHE_TTL)
    except Exception as e:
        print(f"[chat_history] Redis cache write failed (non-fatal): {e}")


def get_recent_history(phone: str, limit: int = HISTORY_CACHE_LIMIT):
    key = _cache_key(phone)

    try:
        cached = redis_conn.lrange(key, -limit, -1)
        if cached:
        
            return [json.loads(item) for item in cached]
    except Exception as e:
        print(f"[chat_history] Redis cache read failed, falling back to Supabase: {e}")

    # Cache miss (first message in a while, or cache expired) — go to Supabase.
    result = (
        supabase.table("chat_history")
        .select("role, message")
        .eq("phone", phone)
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
    history = [{"role": row["role"], "content": row["message"]} for row in reversed(result.data)]

    # Warm the cache so the next read is fast.
    try:
        pipe = redis_conn.pipeline()
        pipe.delete(key)
        for turn in history:
            pipe.rpush(key, json.dumps(turn))
        pipe.expire(key, HISTORY_CACHE_TTL)
        pipe.execute()
    except Exception as e:
        print(f"[chat_history] Redis cache warm-up failed (non-fatal): {e}")

    return history

def get_full_history_for_agent(phone: str):
    # Always reads Supabase directly — this is for the human-agent dashboard,
    # correctness matters more than speed here, and it's a rare call.
    result = (
        supabase.table("chat_history")
        .select("*")
        .eq("phone", phone)
        .order("created_at", desc=False)
        .execute()
    )
    return result.data


# ---------------------------------------------------------------------------
# Async non-blocking wrappers using asyncio.to_thread
# ---------------------------------------------------------------------------
import asyncio

async def save_message_async(phone: str, role: str, text: str, response_time_sec: float = None):
    """Non-blocking async wrapper to save messages to Supabase/Redis."""
    return await asyncio.to_thread(save_message, phone, role, text, response_time_sec)

async def get_recent_history_async(phone: str, limit: int = HISTORY_CACHE_LIMIT):
    """Non-blocking async wrapper to fetch recent history from Redis/Supabase."""
    return await asyncio.to_thread(get_recent_history, phone, limit)

async def get_full_history_for_agent_async(phone: str):
    """Non-blocking async wrapper for human-agent dashboard history."""
    return await asyncio.to_thread(get_full_history_for_agent, phone)