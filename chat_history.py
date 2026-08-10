# import os
# from dotenv import load_dotenv
# from supabase import create_client

# load_dotenv()

# supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# MAX_HISTORY_MESSAGES = 30  # how many recent messages to feed back to the AI as context


# def save_message(phone: str, role: str, message: str):
#     """
#     role must be 'user' or 'assistant'. Call this for BOTH the
#     customer's message and the bot's reply, so the full back-and-forth
#     is captured — this is what lets a human agent read the whole
#     conversation later, and what lets the AI understand follow-ups.
#     """
#     supabase.table("chat_history").insert(
#         {"phone": phone, "role": role, "message": message}
#     ).execute()


# def get_recent_history(phone: str, limit: int = MAX_HISTORY_MESSAGES):
#     """
#     Returns the most recent messages for this phone number, oldest
#     first, in the format LangChain's message classes expect:
#     [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}]
#     """
#     result = (
#         supabase.table("chat_history")
#         .select("role, message")
#         .eq("phone", phone)
#         .order("created_at", desc=True)
#         .limit(limit)
#         .execute()
#     )

#     # Supabase gives us newest-first; reverse so the AI sees them in
#     # actual chronological order, which is what makes "add 2 more"
#     # correctly refer back to whatever came right before it.
#     messages = list(reversed(result.data))
#     return [{"role": m["role"], "content": m["message"]} for m in messages]


# def get_full_history_for_agent(phone: str):
#     """
#     Returns the ENTIRE conversation (not capped at MAX_HISTORY_MESSAGES)
#     for a human agent to review — e.g. when they pick up a handed-off
#     chat and need the full context, not just the AI's short window.
#     """
#     result = (
#         supabase.table("chat_history")
#         .select("role, message, created_at")
#         .eq("phone", phone)
#         .order("created_at", desc=False)
#         .execute()
#     )
#     return result.data

import os
import json
from dotenv import load_dotenv
from supabase import create_client
import os
from redis import Redis

redis_conn = Redis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379"),
    decode_responses=True,
)

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

HISTORY_CACHE_TTL = 60 * 30    # 30 minutes
HISTORY_CACHE_LIMIT = 10       # how many recent turns matter for context


def _cache_key(phone: str) -> str:
    return f"history:{phone}"


def save_message(phone: str, role: str, text: str):
    # Supabase is always the source of truth — write here first.
    supabase.table("chat_history").insert({
        "phone": phone, "role": role, "message": text
    }).execute()

    # Cache write is best-effort — never let a Redis hiccup break saving.
    try:
        key = _cache_key(phone)
        redis_conn.rpush(key, json.dumps({"role": role, "content": text}))
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