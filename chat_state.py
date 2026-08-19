"""
chat_state.py — Session state management & funnel stage tracking per unique user.
Stores slot values (batch timing, selected package, app installation, profile status)
and ensures each unique phone number maintains its own isolated conversation state.
"""

import os
import re
from dotenv import load_dotenv
from supabase import create_client
from rag import extract_slot_llm
import asyncio
# Load environment variables (.env file)
load_dotenv()

# Initialize optional Supabase database client for persistent cloud state
supabase = None
try:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if url and key:
        supabase = create_client(url, key)
except Exception as e:
    print(f"[chat_state] Supabase init error: {e}")

# In-memory session dictionary: maps unique phone number -> user state dictionary
import time
import threading
_memory_lock = threading.Lock()
_memory_sessions: dict[str, dict] = {}

from rapidfuzz import fuzz

def matches_any(text: str, words: list[str]) -> bool:
    t = text.lower().strip()
    if any(re.search(rf"\b{re.escape(w)}\b", t) for w in words):
        return True
    if len(t) <= 20:
        return any(fuzz.ratio(t, w) > 85 for w in words)
    return False

STAGE_ORDER = ["NEW", "ENROLL_ASKED", "ENROLL_CONFIRMED", "TIMING_SELECTED", "PACKAGE_ASKED",
               "PACKAGE_SELECTED", "READY_FOR_APP_LINK", "APP_LINK_SENT", "PROFILE_COMPLETED", "COUPON_SENT"]

def advance_stage(current: str, proposed: str) -> str:
    cur_idx = STAGE_ORDER.index(current) if current in STAGE_ORDER else 0
    new_idx = STAGE_ORDER.index(proposed) if proposed in STAGE_ORDER else 0
    return proposed if new_idx >= cur_idx else current



def mark_escalated(phone: str):
    """
    Marks a user's phone number as escalated to a human agent so the AI bot stops automated replies.
    """
    if supabase:
        try:
            # Upsert into database table 'escalated_chats'
            supabase.table("escalated_chats").upsert({"phone": phone}).execute()
        except Exception as e:
            print(f"[chat_state] mark_escalated DB error: {e}")
    # Also update state in memory session cache
    state = get_user_state(phone)
    state["is_escalated"] = True

def is_escalated(phone: str) -> bool:
    """
    Checks whether a specific phone number has been escalated to a human agent.
    """
    if supabase:
        try:
            # Query Supabase table for escalation status
            result = (
                supabase.table("escalated_chats")
                .select("phone")
                .eq("phone", phone)
                .limit(1)
                .execute()
            )
            if len(result.data) > 0:
                return True
        except Exception as e:
            print(f"[chat_state] is_escalated DB error: {e}")
    
    # Fallback to checking in-memory session state
    state = get_user_state(phone)
    return state.get("is_escalated", False)


def clear_escalation(phone: str):
    """
    Removes human agent escalation for a phone number so the bot can resume automated replies.
    """
    if supabase:
        try:
            # Remove row from Supabase escalated_chats table
            supabase.table("escalated_chats").delete().eq("phone", phone).execute()
        except Exception as e:
            print(f"[chat_state] clear_escalation DB error: {e}")
    state = get_user_state(phone)
    state["is_escalated"] = False


import json
from redis_client import get_redis_connection

redis_conn = get_redis_connection()
STATE_CACHE_TTL = 60 * 60 * 24 * 30  # 30 days persistence across restarts


def get_default_state(phone: str) -> dict:
    """
    Returns the initial default session state dictionary for a new phone number.
    Ensures new users start fresh with stage 'NEW' and no default timing or package selected.
    """
    return {
        "phone": phone,               # Customer's unique phone number key
        "stage": "NEW",               # Funnel stage: NEW -> ENROLL_ASKED -> ENROLL_CONFIRMED -> TIMING_SELECTED -> PACKAGE_SELECTED -> READY_FOR_APP_LINK -> APP_LINK_SENT -> PROFILE_COMPLETED -> COUPON_SENT
        "timing": None,               # Selected batch timing string (e.g. "7:00–8:00 AM")
        "package": None,              # Selected package string (e.g. "3 Months")
        "fee": None,                  # Package fee string (e.g. "₹1,750")
        "app_installed": False,       # Boolean flag indicating if user installed the Sensationz App
        "profile_created": False,     # Boolean flag indicating if user created an in-app profile
        "coupon_sent": False,         # Boolean flag indicating if welcome coupon code was delivered
        "is_escalated": False,        # Escalation flag to hand off to human agent
        "is_target_ad": False,        # Whether user verified via secret string (AI enabled)
        "low_confidence_count": 0     # Consecutive unanswered / low-confidence query count
    }


def get_user_state(phone: str) -> dict:
    """
    Retrieves active session state dictionary for a phone number.
    Checks memory cache (5s TTL) -> Redis -> Supabase -> Default.
    """
    MEMORY_TTL = 5  # seconds — prevents stale reads in multi-worker mode

    # 1. Check in-memory cache (with TTL)
    with _memory_lock:
        if phone in _memory_sessions:
            entry = _memory_sessions[phone]
            if isinstance(entry, tuple):
                cached_state, cached_at = entry
                if (time.time() - cached_at) < MEMORY_TTL:
                    return cached_state
            elif isinstance(entry, dict):
                return entry  # legacy format, no TTL

    # 2. Check Redis cache (persists across server restarts)
    state = get_default_state(phone)
    redis_key = f"user_state:{phone}"
    try:
        raw_state = redis_conn.get(redis_key)
        if raw_state:
            cached_dict = json.loads(raw_state)
            state.update(cached_dict)
            with _memory_lock:
                _memory_sessions[phone] = (state, time.time())
            return state
    except Exception as e:
        print(f"[chat_state] Redis get_user_state read failed (non-fatal): {e}")

    # 3. Fallback to Supabase database lookup
    if supabase:
        try:
            res = supabase.table("user_session_state").select("*").eq("phone", phone).limit(1).execute()
            if res.data and len(res.data) > 0:
                state.update(res.data[0])
        except Exception as e:
            print(f"[chat_state] Supabase get_user_state read failed: {e}")

    # Save to Redis & memory cache for future fast reads
    with _memory_lock:
        _memory_sessions[phone] = (state, time.time())
    try:
        redis_conn.setex(redis_key, STATE_CACHE_TTL, json.dumps(state))
    except Exception:
        pass

    return state


def save_user_state(phone: str, state: dict):
    """
    Persists updated session state dictionary across Memory, Redis, and Supabase.
    """
    with _memory_lock:
        _memory_sessions[phone] = (state, time.time())
    redis_key = f"user_state:{phone}"

    # Persist in Redis across restarts
    try:
        redis_conn.setex(redis_key, STATE_CACHE_TTL, json.dumps(state))
    except Exception as e:
        print(f"[chat_state] Redis save_user_state failed (non-fatal): {e}")

    # Persist in Supabase DB
    if supabase:
        try:
            SUPABASE_STATE_COLUMNS = {
                "phone", "stage", "timing", "package", "fee", "app_installed",
                "profile_created", "coupon_sent", "is_escalated", "is_target_ad",
                "low_confidence_count", "already_assigned"
            }
            db_state = {k: v for k, v in state.items() if k in SUPABASE_STATE_COLUMNS}
            supabase.table("user_session_state").upsert(db_state, on_conflict="phone").execute()
        except Exception as e:
            print(f"[chat_state] Supabase save_user_state failed: {e}")


def is_user_asking_question(text: str) -> bool:
    """
    Determines whether a user input message is an informational question
    (e.g. asking about video links, pricing, syllabus, faculty, or demo sessions)
    rather than a simple procedural step selection or initial greeting.
    """
    txt = text.lower().strip()
    
    # Question marks explicitly signal an informational question
    if "?" in txt:
        return True
    
    # Simple greetings or initial "yoga" mentions are NOT questions
    if txt in ["hi", "hii", "hello", "hey", "yoga", "yog", "online yoga", "yoga classes", "namaste"]:
        return False

    # Strong single-word keywords that are always questions/informational requests
    strong_question_keywords = [
        "price", "prices", "pricing", "cost", "costs", "costly", "expensive", 
        "fee", "fees", "charge", "charges", "rate", "rates", "rupee", "rupees", "rs",
        "pay", "payment", "payments", "refund", "refunds",
        "discount", "discounts", "offer", "offers", "coupon", "coupons",
        "syllabus", "curriculum", "timing", "timings", "schedule", "schedules",
        "demo", "trial", "sample", "video", "videos", "recording", "recordings",
        "detail", "details", "info", "information", "link", "links", "website",
        "teacher", "teachers", "trainer", "trainers", "instructor", "instructors", "faculty"
    ]
    if any(kw in txt for kw in strong_question_keywords):
        return True

    # Priority keywords for class video/demo requests (triggers question mode even for short phrases)
    video_keywords = [
        "video", "videos", "demo", "recording", "recordings",
        "sample", "watch", "link", "trial", "youtube"
    ]
    if any(vk in txt for vk in video_keywords):
        return True
    
    # General informational question keywords
    question_keywords = [
        "who", "what", "where", "how", "why", "which", "when",
        "kitne", "kitna", "kya", "kaun", "kaunsa", "kaunsi", "kaise", "kyu", "kyon", "kab",
        "teacher", "teachers", "faculty", "student", "students",
        "discount", "costly", "expensive", "syllabus", "demo", "trial",
        "hindi", "english", "classes", "fees", "language", "languages",
        "btao", "batao", "bataen", "bataiye", "detail", "details", "info", "information"
    ]
    
    words = txt.split()
    # If text contains general question keywords with 2 or more words
    if len(words) >= 2 and any(kw in txt for kw in question_keywords):
        return True
        
    return False

VALID_PACKAGES = {"1 Month": "₹700", "3 Months": "₹1,750", "6 Months": "₹3,200", "1 Year": "₹5,000"}


GREETING_WORDS = ["hi", "hii", "hello", "hey", "namaste", "good morning", "good evening", "good afternoon"]
CONFIRMATION_WORDS = [
    "yes", "yeah", "yep", "sure", "ok", "okay", "enroll", "join",
    "interested", "i want to join", "haan", "han",
    "karna hai", "kar do", "haan ji", "proceed", "done", "thik", "thik hai"
]

async def extract_and_update_slots(phone: str, text: str) -> dict:
    """
    Analyzes incoming user message, extracts batch timing or package selection,
    and updates funnel stage (NEW -> ENROLL_ASKED -> ENROLL_CONFIRMED -> TIMING_SELECTED -> PACKAGE_ASKED -> READY_FOR_APP_LINK).
    """
    # Retrieve current session state for this phone number
    state = get_user_state(phone)
    text_lower = text.lower().strip()
    is_q = is_user_asking_question(text)

    # Keywords for initial contact vs confirmation vs slots
    is_greeting = matches_any(text_lower, GREETING_WORDS)
    is_yoga_keyword = any(w in text_lower for w in ["yoga", "yog", "yaga", "yogi", "yoga classes", "online yoga", "yoga details", "yoga course"])
    is_confirmation = matches_any(text_lower, CONFIRMATION_WORDS)

    # Stage 0: Initial Greeting & Enrollment Confirmation Check
    if state["stage"] in ["NEW", "ENROLL_ASKED"]:
        has_enroll_intent = (is_confirmation or any(w in text_lower for w in ["price", "fees", "fee", "timing", "timings", "enroll", "join", "classes", "start", "how to", "kaise", "proceed"]))
        if has_enroll_intent:
            state["stage"] = advance_stage(state["stage"], "ENROLL_CONFIRMED")
            state["timing"] = None
            state["package"] = None
            state["fee"] = None
        elif is_greeting or is_yoga_keyword:
            state["stage"] = advance_stage(state["stage"], "ENROLL_ASKED")
            state["timing"] = None
            state["package"] = None
            state["fee"] = None

    # --- 1. Detect Batch Timing Slot ---
    timing_found = False
    if any(t in text_lower for t in ["5 6 pm", "5-6 pm", "5-6pm", "5 to 6", "5:00 pm", "5pm", "5 pm"]):
        state["timing"] = "5:00–6:00 PM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True
    elif any(t in text_lower for t in ["6 7 pm", "6-7 pm", "6-7pm", "6 to 7 pm", "6:00 pm", "6pm", "6 pm"]):
        state["timing"] = "6:00–7:00 PM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True
    elif any(t in text_lower for t in ["7 8 pm", "7-8 pm", "7-8pm", "7 to 8 pm", "7:00 pm", "7pm", "7 pm"]):
        state["timing"] = "7:00–8:00 PM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True
    elif any(t in text_lower for t in ["4 5 pm", "4-5 pm", "4-5pm", "4 to 5", "4:00 pm", "4pm", "4 pm"]):
        state["timing"] = "4:00–5:00 PM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True
    elif any(t in text_lower for t in ["12 1 pm", "12-1 pm", "12-1pm", "12 to 1", "12:00", "12pm", "12 pm"]):
        state["timing"] = "12:00–1:00 PM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True
    elif any(t in text_lower for t in ["6 7 am", "6-7 am", "6-7am", "6 to 7 am", "6:00 am", "6am", "6 am"]):
        state["timing"] = "6:00–7:00 AM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True
    elif any(t in text_lower for t in ["7 8 am", "7-8 am", "7-8am", "7 to 8 am", "7:00 am", "7am", "7 am"]):
        state["timing"] = "7:00–8:00 AM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True
    elif any(t in text_lower for t in ["8 9 am", "8-9 am", "8-9am", "8 to 9 am", "8:00 am", "8am", "8 am"]):
        state["timing"] = "8:00–9:00 AM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True
    elif any(t in text_lower for t in ["10 11 am", "10-11 am", "10-11am", "10 to 11", "10:00 am", "10am", "10 am"]):
        state["timing"] = "10:00–11:00 AM"
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        timing_found = True

    # --- 2. Detect Package / Duration Slot ---
    # We allow package detection at any stage if the text contains clear indicators,
    # or if the user is in a package selection stage (e.g. TIMING_SELECTED, PACKAGE_ASKED, PACKAGE_SELECTED).
    is_package_stage = (state.get("timing") is not None or state["stage"] in ["TIMING_SELECTED", "PACKAGE_ASKED", "PACKAGE_SELECTED"])
    
    is_package_detected = False
    if (text_lower in ["3", "3m", "3 month", "3 months"] or any(p in text_lower for p in ["3 month", "1750", "1,750", "₹1,750"])) or (is_package_stage and text_lower in ["3", "three"]):
        state["package"] = "3 Months"
        state["fee"] = "₹1,750"
        is_package_detected = True
    elif (text_lower in ["1", "1m", "1 month", "one month"] or any(p in text_lower for p in ["1 month", "700", "₹700"])) or (is_package_stage and text_lower in ["1", "one"]):
        state["package"] = "1 Month"
        state["fee"] = "₹700"
        is_package_detected = True
    elif (text_lower in ["6", "6m", "6 month", "6 months"] or any(p in text_lower for p in ["6 month", "3200", "3,200", "₹3,200"])) or (is_package_stage and text_lower in ["6", "six"]):
        state["package"] = "6 Months"
        state["fee"] = "₹3,200"
        is_package_detected = True
    elif (text_lower in ["12", "1 year", "1yr", "1y", "yearly"] or any(p in text_lower for p in ["1 year", "5000", "5,000", "₹5,000"])) or (is_package_stage and text_lower in ["12", "twelve"]):
        state["package"] = "1 Year"
        state["fee"] = "₹5,000"
        is_package_detected = True

    needs_llm_fallback = (
        not timing_found and not is_package_detected
        and not is_greeting
        and len(text_lower) >= 1
        and state["stage"] in ["TIMING_SELECTED", "PACKAGE_ASKED", "PACKAGE_SELECTED", "ENROLL_CONFIRMED"]
    )
    if needs_llm_fallback:
        slot_result = await extract_slot_llm(text)
        if slot_result["timing"] and not state.get("timing"):
            state["timing"] = slot_result["timing"]
            state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
        if slot_result["package"] and not state.get("package"):
            state["package"] = slot_result["package"]
            state["fee"] = VALID_PACKAGES[slot_result["package"]]
        
    # Update funnel stage dynamically based on timing and package availability
    if state.get("timing") and state.get("package"):
        state["stage"] = advance_stage(state["stage"], "READY_FOR_APP_LINK")
    elif state.get("timing"):
        state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
    elif state.get("package"):
        state["stage"] = advance_stage(state["stage"], "PACKAGE_SELECTED")

    # Stage Transition when user confirms timing -> ask for package duration
    if state["stage"] == "TIMING_SELECTED" and is_confirmation and not is_q and not state.get("package"):
        state["stage"] = advance_stage(state["stage"], "PACKAGE_ASKED")


    # --- 4. Detect App Install & Profile Completion ---
    if state["stage"] in ["APP_LINK_SENT", "READY_FOR_APP_LINK"]:
        if any(w in text_lower for w in ["profile created", "both done", "profile done", "profile complete", "created profile", "done", "ho gaya", "ho gya", "kar liya", "kr liya", "bana liya", "download kar liya", "download kr liya", "install kar liya", "install kr liya", "app done"]):
            state["app_installed"] = True
            state["profile_created"] = True
            state["stage"] = advance_stage(state["stage"], "PROFILE_COMPLETED")
        elif any(w in text_lower for w in ["installed", "downloaded", "done app", "app done"]) and not state["profile_created"]:
            state["app_installed"] = True

    # Persist updated session state
    save_user_state(phone, state)
    return state