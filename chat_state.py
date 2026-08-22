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
from rapidfuzz import fuzz
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
    save_user_state(phone, state)

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
    save_user_state(phone, state)


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
        "low_confidence_count": 0,     # Consecutive unanswered / low-confidence query count
        "follow_up_count": 0,
        "next_follow_up_due_at": 0,
        "last_topic": 0    
    }


def reset_follow_up_timer(state: dict):
    state["next_follow_up_due_at"] = None
    state["next_followup_due_at"] = None
    state["follow_up_count"] = 0

def arm_followup_timer(state: dict, topic:str , delay_seconds:int = 300):
    """ Call when ever bot sends a reply - start/reset in 5 min"""
    state["next_followup_due_at"] = time.time() + delay_seconds
    state["last_topic"] = topic[:200]
    state["follow_up_count"] = 0


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
                "low_confidence_count", "already_assigned",
                "follow_up_count", "next_followup_due_at", "last_topic"
            }
            db_state = {k: v for k, v in state.items() if k in SUPABASE_STATE_COLUMNS}
            print(f"[DEBUG-SAVE] {phone}: db_state={db_state}")  # <-- ADD THIS

            result = supabase.table("user_session_state").upsert(db_state, on_conflict="phone").execute()
            print(f"[DEBUG-SAVE] {phone}: upsert result={result}") 
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

def _detect_timing(text_lower: str) -> tuple:
    """
    Comprehensive timing detection supporting all user typing styles:
    Hindi 'X se Y', compact 'XYam', 'X to Y am/pm', 'X-Y am/pm', 'Xam/Xpm'.
    Returns: (timing_str or None, is_ambiguous: bool, ambiguous_range: str)
    """
    t = text_lower

    def has(*pats):
        return any(p in t for p in pats)

    def re_has(pattern):
        return bool(re.search(pattern, t))

    # === Priority 0: Explicit check for UNAVAILABLE / UNSUPPORTED batch requests ===
    # If user explicitly asks for batch ranges that do not exist (e.g. 11-12, 1-2, 2-3, 3-4, 8-9 PM, 9-10),
    # return None so slot extraction NEVER locks it in, allowing RAG to explain it is unavailable.
    unsupported_patterns = [
        r"(?<![0-9])11\s*(?:to|-|se|\s)\s*12\b",
        r"(?<![0-9])11\s*:\s*00\s*(?:to|-|se|\s)\s*12\s*:\s*00\b",
        r"(?<![0-9])11se12",
        r"(?<![0-9])11to12",
        r"(?<![0-9])11-12",
        r"(?<![0-9])1\s*(?:to|-|se|\s)\s*2\b",
        r"(?<![0-9])2\s*(?:to|-|se|\s)\s*3\b",
        r"(?<![0-9])3\s*(?:to|-|se|\s)\s*4\b",
        r"(?<![0-9])8\s*(?:to|-|se|\s)\s*9\s*pm\b",
        r"(?<![0-9])8\s*:\s*00\s*(?:to|-|se|\s)\s*9\s*:\s*00\s*pm\b",
        r"(?<![0-9])8\s*9\s*pm\b",
        r"(?<![0-9])8se9pm\b",
        r"(?<![0-9])8to9pm\b",
        r"(?<![0-9])8-9pm\b",
        r"(?<![0-9])89pm\b",
        r"(?<![0-9])9\s*(?:to|-|se|\s)\s*10\b",
        r"(?<![0-9])9\s*:\s*00\s*(?:to|-|se|\s)\s*10\s*:\s*00\b",
        r"(?<![0-9])9se10\b",
        r"(?<![0-9])9to10\b",
        r"(?<![0-9])9-10\b",
    ]
    if any(re_has(p) for p in unsupported_patterns):
        return None, False, ""

    # === Priority 1: Explicit range WITH AM (covers 'X se Y am', 'XYam', 'X to Y am', 'X-Y am') ===
    if has("5 se 6 am", "5se6am", "5 to 6 am", "5to6am", "5-6 am", "5-6am", "5 6 am", "56am", "5:00 am", "subah 5", "5 baje subah"):
        return "5:00–6:00 AM", False, ""
    if has("6 se 7 am", "6se7am", "6 to 7 am", "6to7am", "6-7 am", "6-7am", "6 7 am", "67am", "6:00 am", "subah 6", "6 baje subah", "morning 6"):
        return "6:00–7:00 AM", False, ""
    if has("7 se 8 am", "7se8am", "7 to 8 am", "7to8am", "7-8 am", "7-8am", "7 8 am", "78am", "7:00 am", "subah 7", "7 baje subah", "morning 7"):
        return "7:00–8:00 AM", False, ""
    if has("8 se 9 am", "8se9am", "8 to 9 am", "8to9am", "8-9 am", "8-9am", "8 9 am", "89am", "8:00 am", "subah 8", "8 baje subah", "morning 8"):
        return "8:00–9:00 AM", False, ""
    if has("10 se 11 am", "10se11am", "10 to 11 am", "10to11am", "10-11 am", "10-11am", "10 11 am", "1011am", "10:00 am", "subah 10", "10 baje subah", "das baje"):
        return "10:00–11:00 AM", False, ""

    # === Priority 2: Explicit range WITH PM ===
    if has("12 se 1 pm", "12se1pm", "12 to 1 pm", "12to1pm", "12-1 pm", "12-1pm", "12 1 pm", "121pm", "12:00 pm", "dopahar", "lunch", "baarah baje", "12 baje dopahar"):
        return "12:00–1:00 PM", False, ""
    if has("4 se 5 pm", "4se5pm", "4 to 5 pm", "4to5pm", "4-5 pm", "4-5pm", "4 5 pm", "45pm", "4:00 pm", "shaam 4", "4 baje shaam", "evening 4"):
        return "4:00–5:00 PM", False, ""
    if has("5 se 6 pm", "5se6pm", "5 to 6 pm", "5to6pm", "5-6 pm", "5-6pm", "5 6 pm", "56pm", "5:00 pm", "shaam 5", "5 baje shaam", "evening 5"):
        return "5:00–6:00 PM", False, ""
    if has("6 se 7 pm", "6se7pm", "6 to 7 pm", "6to7pm", "6-7 pm", "6-7pm", "6 7 pm", "67pm", "6:00 pm", "shaam 6", "6 baje shaam", "evening 6"):
        return "6:00–7:00 PM", False, ""
    if has("7 se 8 pm", "7se8pm", "7 to 8 pm", "7to8pm", "7-8 pm", "7-8pm", "7 8 pm", "78pm", "7:00 pm", "shaam 7", "7 baje shaam", "evening 7"):
        return "7:00–8:00 PM", False, ""

    # === Priority 3: Single time with explicit AM/PM (using word boundaries to prevent range collisions) ===
    if re_has(r"(?<![0-9\-])\b6\s*am\b"):
        return "6:00–7:00 AM", False, ""
    if re_has(r"(?<![0-9\-])\b7\s*am\b"):
        return "7:00–8:00 AM", False, ""
    if re_has(r"(?<![0-9\-])\b8\s*am\b"):
        return "8:00–9:00 AM", False, ""
    if re_has(r"(?<![0-9\-])\b10\s*am\b"):
        return "10:00–11:00 AM", False, ""
    if re_has(r"(?<![0-9\-])\b12\s*pm\b"):
        return "12:00–1:00 PM", False, ""
    if re_has(r"(?<![0-9\-])\b4\s*pm\b"):
        return "4:00–5:00 PM", False, ""
    if re_has(r"(?<![0-9\-])\b5\s*pm\b"):
        return "5:00–6:00 PM", False, ""
    if re_has(r"(?<![0-9\-])\b6\s*pm\b"):
        return "6:00–7:00 PM", False, ""
    if re_has(r"(?<![0-9\-])\b7\s*pm\b"):
        return "7:00–8:00 PM", False, ""

    # === Priority 4: Unambiguous ranges WITHOUT AM/PM (only one slot exists for that pair) ===
    if has("10 to 11", "10 se 11", "10-11", "10 11", "10 baje", "das baje"):
        return "10:00–11:00 AM", False, ""
    if has("8 to 9", "8 se 9", "8-9"):
        return "8:00–9:00 AM", False, ""
    if has("4 to 5", "4 se 5", "4-5", "4 baje shaam"):
        return "4:00–5:00 PM", False, ""
    if has("12 to 1", "12 se 1", "12-1", "12 baje", "dopahar"):
        return "12:00–1:00 PM", False, ""
    if has("5 to 6", "5 se 6"):
        return "5:00–6:00 PM", False, ""

    # === Priority 5: Ambiguous ranges (both AM and PM slots exist — ask user) ===
    if has("6 to 7", "6 se 7"):
        return None, True, "6 to 7"
    if has("7 to 8", "7 se 8"):
        return None, True, "7 to 8"

    return None, False, ""


async def extract_and_update_slots(phone: str, text: str, chat_history: list = None) -> dict:
    """
    Analyzes incoming user message, extracts batch timing or package selection,
    and updates funnel stage (NEW -> ENROLL_ASKED -> ENROLL_CONFIRMED -> TIMING_SELECTED -> PACKAGE_ASKED -> READY_FOR_APP_LINK).
    """
    # Retrieve current session state for this phone number
    state = get_user_state(phone)
    # Reset follow-up counter whenever customer is actively chatting
    state["follow_up_count"] = 0
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

    # --- 0.5 Detect Intent to Change / Remove Slots ---
    change_kws = ["change", "remove", "galat", "wrong", "dusra", "delete", "nahi chahiye", "cancel"]
    if any(kw in text_lower for kw in change_kws) and state["stage"] not in ["NEW", "PROFILE_COMPLETED", "COUPON_SENT"]:
        changed = False
        if any(w in text_lower for w in ["timing", "time", "batch", "slot", "baje"]):
            state["timing"] = None
            if state["stage"] in ["TIMING_SELECTED", "PACKAGE_ASKED", "PACKAGE_SELECTED", "READY_FOR_APP_LINK", "APP_LINK_SENT"]:
                state["stage"] = "ENROLL_CONFIRMED"
            changed = True
        
        if any(w in text_lower for w in ["package", "plan", "duration", "month", "months", "year", "mahina"]):
            state["package"] = None
            state["fee"] = None
            if state["stage"] in ["PACKAGE_SELECTED", "READY_FOR_APP_LINK", "APP_LINK_SENT"]:
                state["stage"] = "TIMING_SELECTED"
            changed = True
            
        if not changed and ("remove it" in text_lower or "change it" in text_lower or "change" in text_lower):
            # General fallback if ambiguous
            state["timing"] = None
            state["package"] = None
            state["fee"] = None
            if state["stage"] in ["TIMING_SELECTED", "PACKAGE_ASKED", "PACKAGE_SELECTED", "READY_FOR_APP_LINK", "APP_LINK_SENT"]:
                state["stage"] = "ENROLL_CONFIRMED"

    # --- 1. Detect Batch Timing Slot ---
    timing_found = False
    if not state.get("timing"):  # Only detect if timing not already set
        timing_str, is_ambiguous, ambiguous_range = _detect_timing(text_lower)
        if timing_str:
            state["timing"] = timing_str
            state["stage"] = advance_stage(state["stage"], "TIMING_SELECTED")
            timing_found = True
            state.pop("ambiguous_timing_range", None)  # clear any pending ambiguity
        elif is_ambiguous:
            state["ambiguous_timing_range"] = ambiguous_range

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
        slot_result = await extract_slot_llm(text, chat_history)
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