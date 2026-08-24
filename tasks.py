"""
tasks.py — Background message-processing pipeline.

KEY CONCURRENCY DESIGN:
- No per-phone Redis lock — the batching debouncer already guarantees
  only one task per phone is active at a time (token-based dedup).
- ALL I/O (Interakt API, Supabase, Redis) is fully async — never blocks the event loop.
- Different phone numbers process in parallel on the same event loop.
- Redis INCR-based round-robin for agent assignment (atomic, no global lock).
"""

import os
import time
import asyncio
from dotenv import load_dotenv
from chat_state import reset_follow_up_timer
from interakt import (
    send_text_message_async,
    assign_chat_to_agent_async,
)
from chat_state import reset_follow_up_timer
from chat_state import arm_followup_timer
from chat_history import save_message, get_recent_history
from csv_logger import log_message
from chat_state import (
    mark_escalated,
    is_escalated,
    get_user_state,
    save_user_state,
    extract_and_update_slots,
    is_user_asking_question,
    matches_any,
    advance_stage,
)
from rag import ask_rag_async, stream_rag
from redis_client import get_redis_connection
from sales_followup import get_sales_followup
import re

load_dotenv()

redis_conn = get_redis_connection()

# --- Agent Config ---
PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")
PRIORITY_AGENT_EMAIL_ANOTHER_1 = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER_1")
PRIORITY_AGENT_EMAIL_ANOTHER_2 = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER_2")

# Round-robin pool (only non-None entries)
AGENT_POOL = [e for e in [PRIORITY_AGENT_EMAIL_ANOTHER_1, PRIORITY_AGENT_EMAIL_ANOTHER_2] if e]

AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]


def _agent_nudge(user_text: str) -> str:
    """
    Returns the 'contact agent' nudge message in the user's language.
    Detects Hindi/Hinglish by checking for Devanagari script or common Hindi words.
    """
    hindi_markers = ["kya", "hai", "mujhe", "batao", "dijiye", "chahiye", "ka", "ki", "ke", "nahi", "haan", "aur", "se", "bhi"]
    text_lower = user_text.lower()
    has_devanagari = any("\u0900" <= ch <= "\u097F" for ch in user_text)
    has_hindi_word = any(w in text_lower.split() for w in hindi_markers)

    if has_devanagari or has_hindi_word:
        return (
            "\n\n💬 Iske baare mein aur jaankari ke liye aap *agent* type karein, "
            "ya hamare support team se seedha baat karein: *9898989898*"
        )
    return (
        "\n\n💬 To know more, type *agent* to connect with our support team, "
        "or call us directly at *9898989898*."
    )
def _format_for_whatsapp(text: str) -> str:
    """
    Cleans up text specifically for WhatsApp rendering:
    - Converts Markdown **bold** to WhatsApp *bold*
    - Converts lines starting with loose asterisk bullets '* ' to bullet points '• '
    - Replaces ### / ## headers with *bold headers*
    """
    if not text:
        return text

    # Convert ### Header or ## Header to *Header*
    text = re.sub(r"^(?:#{1,6})\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # Convert Markdown **bold** to WhatsApp *bold*
    text = re.sub(r"\*\*(.*?)\*\*", r"*\1*", text)

    # Convert lines starting with loose asterisk bullets to clean bullet '• '
    text = re.sub(r"^\s*\*\s+", "• ", text, flags=re.MULTILINE)

    # Clean double bullet markers if any (e.g. • - or - •)
    text = re.sub(r"^•\s*-\s*", "• ", text, flags=re.MULTILINE)

    return text.strip()


TARGET_MESSAGE_TEXT = os.getenv("TARGET_MESSAGE_TEXT", "Hello! Can I get more info on Yoga classes?")

# ── Disinterest keywords (same list as in should_skip_followup) ───────────────
_DISINTEREST_KWS = [
    "not interested", "no thanks", "nahi chahiye", "nhi chahiye", "nahi lena",
    "nhi lena", "interested nahi", "interested nhi", "abhi nahi", "abhi nhi",
    "mat bhejo", "mat send", "baad mein", "baad mai", "later", "not now",
    "dont want", "don't want", "nahi karna", "nhi karna", "no need",
    "zaroorat nahi", "zaroorat nhi", "nahi chahte", "nhi chahte",
    "nahi join", "nhi join", "join nahi", "join nhi",
    "not for me", "nahi lete", "nhi lete",
    # Common typos / alternate spellings
    "not intersted", "not intrested", "not intrestad",
    "nai chahiye", "ni chahiye", "no interest",
]

def is_disinterest_signal(text: str) -> bool:
    """Returns True if the user message is a clear disinterest / refusal signal."""
    t = text.lower().strip()
    return any(kw in t for kw in _DISINTEREST_KWS)


def _feedback_request_msg(user_text: str) -> str:
    """
    Returns a gentle, language-aware message asking WHY the user is not interested.
    Never pressures — just invites them to share their concern.
    """
    hindi_markers = ["kya", "hai", "mujhe", "batao", "chahiye", "ka", "ki", "ke", "nahi", "nhi",
                     "haan", "aur", "se", "bhi", "abhi", "mat", "nhi"]
    has_devanagari = any("\u0900" <= ch <= "\u097F" for ch in user_text)
    has_hindi_word = any(w in user_text.lower().split() for w in hindi_markers)

    if has_devanagari or has_hindi_word:
        return (
            "Bilkul theek hai, koi problem nahi! 😊\n\n"
            "Agar aap humse share kar sakein — kya cheez rok rahi hai aapko? "
            "Timing? Fees? Ya kuch aur concern hai? "
            "Hum apni best koshish karenge, agar kuch resolve ho sake toh. "
            "Warna koi pressure nahi — jab mann kare, tab baat karte hain. 🙏"
        )
    return (
        "No problem at all! 😊\n\n"
        "If you don't mind sharing — what's holding you back? "
        "Is it the timing, the fees, or something else? "
        "We'll do our best to help if we can. "
        "Either way, no pressure — we're here whenever you're ready. 🙏"
    )

INFO_INTENT_KEYWORDS = [
    # Section 20 mapping
    "fee", "price", "cost", "charges",
    "duration",
    "batch", "timing", "schedule", "time slot",
    "teacher", "instructor",
    "platform", "app",
    "syllabus", "topics", "course",
    "trial", "demo", "sample", "reference video",
    "eligib", "age", "eligible",
    "classes per week", "how many days", "class frequency",
    "benefit", "benefits", "fayda", "fayada",
    "what to bring", "keep ready", "mat", "clothing",
    "online", "offline", "virtual",
    "device", "laptop", "mobile", "tablet",
    "enroll", "registration",
    "medical", "treatment", "cure", "disease", "pcos", "back pain",

    # Section 21 phrase variations
    "monthly fee", "quarterly fee", "six-month fee", "annual fee",
    "live yoga", "sensationz app", "sensationz",
    "morning batch", "evening batch",
    "trial session", "trial yoga class", "class", "schedule",

    # General intent phrases (from your earlier chats)
    "yoga", "sikhna", "seekhna", "learn yoga", "yoga krna",
    "trust", "fraud", "scam", "genuine", "real company",
    "about company", "location", "branch", "address",
    "social media", "instagram", "facebook", "youtube", "website",
    "recording", "record", "leave", "cancel", "refund", "no refund",
    "minimum age", "8 year", "batao", "guide"
]

AGENT_SUGGEST_PATTERN = re.compile(
    r"to know more about this,?\s*you can type\s*\*?agent\*?\s*so our support team can assist you shortly\.?",
    re.IGNORECASE
)

# ---------------------------------------------------------------------------
# Round-robin agent selection (Redis INCR — atomic, multi-process safe)
# ---------------------------------------------------------------------------
def get_next_agent_email() -> str:
    """
    Returns the next agent email in round-robin order.
    Uses Redis INCR for atomicity across concurrent requests and processes.
    """
    if not AGENT_POOL:
        return PRIORITY_AGENT_EMAIL  # fallback
    counter = redis_conn.incr("agent_round_robin_counter")
    index = (counter - 1) % len(AGENT_POOL)
    agent = AGENT_POOL[index]
    print(f"[round-robin] counter={counter} -> agent[{index}] = {agent}")
    return agent


# ---------------------------------------------------------------------------
# Target ad / message verification
# ---------------------------------------------------------------------------
def is_target_ad_or_message(text: str, referral_data: dict = None, phone: str = None) -> bool:
    """
    Checks if a message qualifies for bot response.
    1. Already-verified user (state flag) → PASS
    2. First message: BOTH ad ID AND text must match → PASS
    3. Otherwise → FAIL (no reply, no assignment)
    """
    TARGET_AD_ID = os.getenv("TARGET_AD_ID")

    if phone:
        try:
            state = get_user_state(phone)
            if state.get("is_target_ad") is True:
                print(f"[target-check] {phone}: PASS — is_target_ad already True")
                return True
        except Exception as e:
            print(f"[target-check] {phone}: state lookup failed ({e})")

    cleaned_text = text.strip().lower()
    target_text = TARGET_MESSAGE_TEXT.strip().lower()
    text_matches = (cleaned_text == target_text)

    ad_id_matches = False
    if referral_data and isinstance(referral_data, dict) and referral_data:
        source_id = referral_data.get("source_id")
        source_url = referral_data.get("source_url", "")
        if TARGET_AD_ID and (str(source_id) == str(TARGET_AD_ID) or str(TARGET_AD_ID) in str(source_url)):
            ad_id_matches = True
            print(f"[target-check] {phone}: Ad ID MATCH")
        else:
            print(f"[target-check] {phone}: Ad ID MISMATCH — source_id='{source_id}' vs TARGET='{TARGET_AD_ID}'")
    else:
        print(f"[target-check] {phone}: No referral data")

    if ad_id_matches and text_matches:
        print(f"[target-check] {phone}: PASS — BOTH match")
        return True
    if text_matches and not referral_data:
        print(f"[target-check] {phone}: PASS — text matches (no referral)")
        return True

    print(f"[target-check] {phone}: FAIL — ad={ad_id_matches}, text={text_matches}")
    return False


# ---------------------------------------------------------------------------
# Agent handoff (async)
# ---------------------------------------------------------------------------
async def handle_agent_handoff_async(phone: str, start_time: float = None):
    """Handles customer request to talk to a human — fully async."""
    print(f"[tasks] Agent requested by {phone}")
    reply = "Connecting you with our team now. Someone will be with you shortly!"

    agent = get_next_agent_email()
    if agent:
        await assign_chat_to_agent_async(phone, agent)
        await send_text_message_async(phone, reply)
        mark_escalated(phone)
        log_message(phone, "agent", reply)
    else:
        reply = (
            "Our team is currently offline, but we've noted your request "
            "and someone will reach out as soon as they're back online."
        )
        await send_text_message_async(phone, reply)

    latency_sec = round(time.time() - start_time, 2) if start_time else None
    print(f"[TIMING] {phone} agent_handoff TOTAL: {latency_sec}s")
    save_message(phone, "assistant", reply, response_time_sec=latency_sec)


# ---------------------------------------------------------------------------
# AI reply pipeline (async — no blocking calls)
# ---------------------------------------------------------------------------
# async def handle_ai_reply_async(phone: str, text: str, history: list, start_time: float = None):
#     """Fully async AI reply — all I/O is non-blocking."""
#     t0 = time.perf_counter()

#     # Pre-written reply for ad trigger message
#     if text.strip().lower() == TARGET_MESSAGE_TEXT.strip().lower():
#         reply = "Hi Sir/Mam, Welcome to Sensationz Media and arts, How i can help u?"
#         t_send = time.perf_counter()
#         await send_text_message_async(phone, reply)
#         print(f"[TIMING] {phone} pre-written_send: {time.perf_counter() - t_send:.2f}s")
#         latency_sec = round(time.time() - start_time, 2) if start_time else None
#         save_message(phone, "assistant", reply, response_time_sec=latency_sec)
#         log_message(phone, "ai", reply)
#         print(f"[TIMING] {phone} pre-written TOTAL: {time.perf_counter() - t0:.2f}s")
#         return

#     # 1. Slot extraction
#     t_slots = time.perf_counter()
#     state = extract_and_update_slots(phone, text)
#     is_q = is_user_asking_question(text)
#     print(f"[TIMING] {phone} slot_extraction: {time.perf_counter() - t_slots:.2f}s")

#     # 2. RAG AI reply
#     t_rag = time.perf_counter()
#     full_reply = await ask_rag_async(text, chat_history=history, state=state)
#     full_reply = full_reply.strip()
#     print(f"[TIMING] {phone} rag_query: {time.perf_counter() - t_rag:.2f}s")

#     # Post-LLM State Transitions
#     if state.get("stage") == "READY_FOR_APP_LINK":
#         state["stage"] = "APP_LINK_SENT"
#     elif state.get("stage") == "PROFILE_COMPLETED" and not state.get("coupon_sent"):
#         state["coupon_sent"] = True
#         state["stage"] = "COUPON_SENT"

#     # 4. Low-confidence check
#     low_conf_triggers = ["unable to process", "unable to answer", "i don't have information", "not sure", "sorry, the ai service"]
#     if any(trigger in full_reply.lower() for trigger in low_conf_triggers):
#         state["low_confidence_count"] = state.get("low_confidence_count", 0) + 1
#     else:
#         state["low_confidence_count"] = 0

#     if state.get("low_confidence_count", 0) >= 2:
#         full_reply += "\n\n💬 Would you like to speak directly with our support team? Please reply by typing **'agent'** or call us directly at **9898989898** to resolve your query!"

#     save_user_state(phone, state)

#     # 5. Send reply (async)
#     t_send = time.perf_counter()
#     await send_text_message_async(phone, full_reply)
#     print(f"[TIMING] {phone} interakt_send: {time.perf_counter() - t_send:.2f}s")

#     latency_sec = round(time.time() - start_time, 2) if start_time else None
#     print(f"[TIMING] {phone} ai_reply TOTAL: {time.perf_counter() - t0:.2f}s (wall={latency_sec}s)")
#     save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
#     log_message(phone, "ai", full_reply)


def is_info_intent(text: str) -> bool:
    t = text.lower().strip()
    return any(kw in t for kw in INFO_INTENT_KEYWORDS)

def should_skip_followup(user_text: str, full_reply: str, stage: str) -> bool:
    """
    Universal suppressor for stage follow-ups (e.g. package choice / timing choice prompts).
    Returns True if a follow-up prompt should be SUPPRESSED (not sent).
    """
    u_lower = (user_text or "").lower().strip()
    r_lower = (full_reply or "").lower().strip()

    # 1. Medical & Sensitive Health Conditions (Never push sales on medical queries)
    medical_kws = [
        "cancer", "heart", "cardiac", "surgery", "doctor", "medical", "treatment",
        "disease", "illness", "bp", "blood pressure", "diabetes", "sugar",
        "spine", "spinal", "slip disc", "slipped disc", "injury", "fracture",
        "pregnant", "pregnancy", "prenatal", "postnatal", "garbhasanskar",
        "operation", "paralysis", "stroke", "kidney", "liver", "asthma",
        "arthritis", "tumour", "tumor", "chemo", "chemotherapy", "patient",
        "dawa", "dawain", "hospital", "bimari", "bimar"
    ]
    if any(kw in u_lower for kw in medical_kws) or any(kw in r_lower for kw in medical_kws):
        return True

    # 2. Unoffered / Unlisted Services & Negative Inquiries
    unoffered_kws = [
        "prenatal", "postnatal", "kids yoga", "face yoga", "offline", "studio",
        "1-on-1", "1 on 1", "one on one", "private class", "personal trainer",
        "home tutor", "personal class"
    ]
    if any(kw in u_lower for kw in unoffered_kws):
        return True

    # If the AI reply explicitly explains that something is unavailable/unoffered
    negative_phrases = [
        "available nahi", "not available", "offer nahi", "not offered",
        "nahi sikhate", "nahi karwate", "don't offer", "do not offer",
        "nahi hoti", "nahi hota", "currently not available", "currently available nahi"
    ]
    if any(p in r_lower for p in negative_phrases):
        return True

    # 3. Trial / Demo / Trust / Location inquiries (User asked to explore first)
    explore_kws = [
        "trial", "demo", "sample", "review", "reviews", "rating", "testimonial",
        "location", "address", "branch", "fraud", "fake", "trust", "legit",
        "website", "instagram", "facebook", "youtube"
    ]
    if any(kw in u_lower for kw in explore_kws):
        return True

    # 4. Support, Agent, Refund, Cancellation
    support_kws = ["agent", "human", "refund", "cancel", "complaint", "support", "call", "baat karni", "talk to"]
    if any(kw in u_lower for kw in support_kws) or any(kw in r_lower for kw in support_kws):
        return True

    # 5. Disinterest / Negative Intent — NEVER push sales after a clear refusal
    if is_disinterest_signal(user_text):
        return True

    # 6. Question already addressed in full_reply according to current stage
    if stage in ["ENROLL_CONFIRMED", "PACKAGE_SELECTED"]:
        timing_kws = ["timing", "batch", "time slot", "schedule", "samay", "kab"]
        if any(kw in r_lower for kw in timing_kws):
            return True
    elif stage in ["TIMING_SELECTED", "PACKAGE_ASKED"]:
        pkg_kws = ["package", "duration", "month", "months", "year", "fee", "fees", "price", "cost", "mahina", "₹", "rs"]
        if any(kw in r_lower for kw in pkg_kws):
            return True
    elif stage in ["READY_FOR_APP_LINK", "APP_LINK_SENT"]:
        app_kws = ["download", "install", "play store", "app store", "android", "ios", "app link", "profile", "http"]
        if any(kw in r_lower for kw in app_kws):
            return True

    return False



def get_flow_followup(state: dict) -> str:
    # 1. If enrollment completed (coupon sent or profile completed)
    if state.get("stage") in ["PROFILE_COMPLETED", "COUPON_SENT"] or state.get("coupon_sent"):
        return None
        
    # 2. If app links are sent or ready for app link
    if state.get("stage") in ["READY_FOR_APP_LINK", "APP_LINK_SENT"]:
        return (
            "\n\nTo proceed, you'll need to download the Sensationz App, through which you'll receive your special welcome discount coupon 🎁.\n\n"
            "Please download the app here:\n\n"
            "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
            "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n\n"
            "Once you've downloaded the app and created your profile, let me know here so I can activate your welcome coupon!"
        )
        
    # 3. If timing is selected but package is missing
    if state.get("timing") and not state.get("package"):
        return (
            "\n\nWhich package duration would you like to start with?\n"
            "1 Month — ₹700 | 3 Months — ₹1,750 | 6 Months — ₹3,200 | 1 Year — ₹5,000"
        )
        
    # 4. If timing is missing
    if not state.get("timing"):
        if state.get("stage") == "NEW":
            return None
        return (
            "\n\nWhich timing would you prefer for your classes?\n"
            "Morning: 6:00–7:00 AM, 7:00–8:00 AM, 8:00–9:00 AM, 10:00–11:00 AM\n"
            "Afternoon: 12:00–1:00 PM\n"
            "Evening: 4:00–5:00 PM, 5:00–6:00 PM, 6:00–7:00 PM, 7:00–8:00 PM"
        )
    return None


from chat_state import arm_followup_timer, reset_follow_up_timer

async def handle_ai_reply_async(phone: str, text: str, history: list, start_time: float = None):
    t0 = time.perf_counter()

    if text.strip().lower() == TARGET_MESSAGE_TEXT.strip().lower():
        msg1 = "Welcome to Sensationz! 🙏 We're excited to help you start your wellness journey."
        msg2 = "We offer Online Live Interactive Yoga classes (Monday to Friday) with certified expert instructors, beginner-friendly packages starting at just Rs. 700/month, and full access to class recordings."
        msg3 = "We have batches running throughout the day (Morning, Afternoon, and Evening). Which time slot works best for your schedule?"

        await send_text_message_async(phone, msg1)
        await asyncio.sleep(1)
        await send_text_message_async(phone, msg2)
        await asyncio.sleep(1)
        await send_text_message_async(phone, msg3)

        # Arm follow-up timer for this welcome message too
        state = get_user_state(phone)
        arm_followup_timer(state, topic="welcome message")
        save_user_state(phone, state)

        latency_sec = round(time.time() - start_time, 2) if start_time else None
        full_welcome = f"{msg1}\n{msg2}\n{msg3}"
        save_message(phone, "assistant", full_welcome, response_time_sec=latency_sec)
        log_message(phone, "ai", full_welcome)
        return

    # Fetch previous state stage before slot extraction ok ok
    try:
        pre_state = get_user_state(phone)
        prev_stage = pre_state.get("stage") or "NEW"
    except Exception:
        prev_stage = "NEW"

    t_slots = time.perf_counter()
    state = await extract_and_update_slots(phone, text, history)
    is_q = is_user_asking_question(text)
    print(f"[TIMING] {phone} slot_extraction: {time.perf_counter() - t_slots:.2f}s")

    # --- Handle Ambiguous Timing (requires AM/PM clarification from user) ---
    if state.get("ambiguous_timing_range") and not state.get("timing"):
        amb = state.get("ambiguous_timing_range")
        state["ambiguous_timing_range"] = None
        save_user_state(phone, state)
        reply = (
            f"Aap {amb} ki timing chahte hain — subah (AM) ya shaam (PM)? 😊\n"
            f"• Subah ke liye likhein: '{amb} AM'\n"
            f"• Shaam ke liye likhein: '{amb} PM'"
        )
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    # Define flags for fresh transitions and confirmations/greetings
    text_lower = text.lower().strip()
    GREETING_WORDS = ["hi", "hii", "hello", "hey", "namaste", "good morning", "good evening", "good afternoon"]
    CONFIRMATION_WORDS = [
        "yes", "yeah", "yep", "sure", "ok", "okay", "enroll", "join",
        # NOTE: 'interested' removed — it matches inside 'not interested'. Use full phrases instead:
        "i am interested", "mujhe interested", "i want to join", "haan", "han",
        "karna hai", "kar do", "haan ji", "proceed", "done", "thik", "thik hai"
    ]
    is_greeting = matches_any(text_lower, GREETING_WORDS)
    # Disinterest takes priority — never let it be treated as confirmation
    is_disinterest = is_disinterest_signal(text)
    is_confirmation = (not is_disinterest) and matches_any(text_lower, CONFIRMATION_WORDS)

    is_fresh_enroll_confirmed = (prev_stage != "ENROLL_CONFIRMED" and state["stage"] == "ENROLL_CONFIRMED")
    is_fresh_package_asked = (prev_stage != "PACKAGE_ASKED" and state["stage"] == "PACKAGE_ASKED")
    is_fresh_timing_selected = (prev_stage != "TIMING_SELECTED" and state["stage"] == "TIMING_SELECTED")
    is_fresh_package_selected = (prev_stage != "PACKAGE_SELECTED" and state["stage"] == "PACKAGE_SELECTED")

    # ── DISINTEREST CHECK ── Must run BEFORE any stage guard so it intercepts first.
    # Handles: 'im not interested', 'not intersted' (typo), 'nahi chahiye', etc.
    if is_disinterest and not state.get("disinterest_asked_feedback"):
        state["disinterest_asked_feedback"] = True
        arm_followup_timer(state, topic=text)
        save_user_state(phone, state)
        reply = _feedback_request_msg(text)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    if state.get("disinterest_asked_feedback"):
        state["disinterest_asked_feedback"] = False
        save_user_state(phone, state)
        if is_disinterest:
            # User still not interested — graceful exit, no pressure
            hindi_markers = ["nahi", "nhi", "mat", "abhi", "kya", "hai", "bhi", "se"]
            has_devanagari = any("\u0900" <= ch <= "\u097F" for ch in text)
            has_hindi_word = any(w in text_lower.split() for w in hindi_markers)
            if has_devanagari or has_hindi_word:
                reply = "Theek hai, samajh gaye! 🙏 Jab bhi mann kare, hum yahaan hain."
            else:
                reply = "Totally understood! 🙏 We're here whenever you're ready."
            arm_followup_timer(state, topic=text)
            save_user_state(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            save_message(phone, "assistant", reply, response_time_sec=latency_sec)
            log_message(phone, "ai", reply)
            return
        # User shared their reason — fall through to RAG with gentle context hint
        history = list(history) + [{
            "role": "system",
            "content": (
                "The customer had previously expressed disinterest. They have now shared a reason. "
                "Gently address their specific concern and show how Sensationz can help, "
                "without being pushy. Do NOT force them. Keep it warm and helpful. "
                "End with an open invitation, not a hard sell."
            )
        }]
    # ── END DISINTEREST CHECK ──

    # --- DETERMINISTIC STAGE GUARDS ---
    if not is_q and not is_info_intent(text) and state["stage"] == "ENROLL_CONFIRMED" and (is_fresh_enroll_confirmed or is_confirmation or is_greeting):
        reply = get_flow_followup(state)
        if reply:
            reply = reply.strip()
            arm_followup_timer(state, topic=text)
            save_user_state(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            save_message(phone, "assistant", reply, response_time_sec=latency_sec)
            log_message(phone, "ai", reply)
            return

    if not is_q and not is_info_intent(text) and state["stage"] == "PACKAGE_ASKED" and (is_fresh_package_asked or is_confirmation or is_greeting):
        reply = get_flow_followup(state)
        if reply:
            reply = reply.strip()
            arm_followup_timer(state, topic=text)
            save_user_state(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            save_message(phone, "assistant", reply, response_time_sec=latency_sec)
            log_message(phone, "ai", reply)
            return

    if not is_q and not is_info_intent(text) and state["stage"] == "TIMING_SELECTED" and not state.get("package") and (is_fresh_timing_selected or is_confirmation or is_greeting):
        reply = (
            f"You've selected the {state.get('timing')} batch.\n\n"
            "Would you like to go ahead and pick a package duration too? 😊\n"
            "1 Month — ₹700 | 3 Months — ₹1,750 | 6 Months — ₹3,200 | 1 Year — ₹5,000"
        )
        state["stage"] = advance_stage(state["stage"], "PACKAGE_ASKED")
        arm_followup_timer(state, topic=text)
        save_user_state(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    if not is_q and not is_info_intent(text) and state["stage"] == "PACKAGE_SELECTED" and not state.get("timing") and (is_fresh_package_selected or is_confirmation or is_greeting):
        reply = (
            f"You've selected the {state.get('package')} package.\n\n"
            "Which timing would you prefer for your classes? 😊\n"
            "Morning: 6:00–7:00 AM, 7:00–8:00 AM, 8:00–9:00 AM, 10:00–11:00 AM\n"
            "Afternoon: 12:00–1:00 PM\n"
            "Evening: 4:00–5:00 PM, 5:00–6:00 PM, 6:00–7:00 PM, 7:00–8:00 PM"
        )
        state["stage"] = advance_stage(state["stage"], "ENROLL_CONFIRMED")
        arm_followup_timer(state, topic=text)
        save_user_state(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return


    # ── Discount / coupon question — intercept at ANY stage ──────────────────
    # Never let RAG handle discount/coupon questions — it doesn't have this info.
    # We set full_reply here and fall through to the normal send path so that
    # get_flow_followup() automatically appends the next enrollment step as msg 2.
    _DISCOUNT_KWS = [
        "discount", "coupon", "offer", "special", "code",
        "discount code", "coupon code", "kya milega", "kya hoga", "kya discount",
        "special discount", "special offer", "discount btao", "koi offer",
    ]
    _is_discount_query = any(kw in text_lower for kw in _DISCOUNT_KWS)

    if _is_discount_query and not state.get("coupon_sent"):
        current_stage = state.get("stage", "NEW")

        if current_stage in ["APP_LINK_SENT", "READY_FOR_APP_LINK"]:
            discount_reply = (
                "Aapke liye ek special welcome discount code hai 🎁\n\n"
                "Sirf Sensationz App download karein aur apna profile banayein — "
                "uske baad *Done* ya *Yes* reply karein, aur main turant aapka coupon code bhej dunga!\n\n"
                "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
                "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351"
            )
            state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")
        else:
            # Early stage — answer discount question, flow follow-up will prompt next step
            discount_reply = (
                "Haan, aapko ek special *welcome coupon code* milega 🎁\n\n"
                "Ye coupon aapke course fee mein discount deta hai. Isko paane ke liye:\n"
                "1️⃣ Apna timing aur package choose karein\n"
                "2️⃣ Sensationz App download karein\n"
                "3️⃣ App mein profile banayein\n"
                "4️⃣ Yahan *Done* ya *Yes* reply karein — coupon turant bhej diya jayega!"
            )

        # Build followup_separate from enrollment flow state
        followup_separate = get_flow_followup(state)
        if followup_separate:
            followup_separate = followup_separate.strip()

        arm_followup_timer(state, topic=text)
        save_user_state(phone, state)

        discount_reply = _format_for_whatsapp(discount_reply)
        if followup_separate:
            followup_separate = _format_for_whatsapp(followup_separate)
            await send_text_message_async(phone, discount_reply)
            await asyncio.sleep(1)
            await send_text_message_async(phone, followup_separate)
            combined = discount_reply + "\n\n" + followup_separate
        else:
            await send_text_message_async(phone, discount_reply)
            combined = discount_reply

        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", combined, response_time_sec=latency_sec)
        log_message(phone, "ai", combined)
        return

    if state["stage"] == "READY_FOR_APP_LINK":
        package = state.get("package") or "3 Months"
        fee = state.get("fee") or "₹1,750"
        reply = (
            f"You've selected the {package} package for {fee}.\n\n"
            "To continue, please download the Sensationz App and create your profile. "
            "Once that's done, just reply *Done* or *Yes* here, and I'll send you a special welcome coupon code 🎁 "
            "that you can use to get a discount on your course fee.\n\n"
            "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
            "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351"
        )
        state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")
        arm_followup_timer(state, topic=text)
        save_user_state(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    # ── Coupon Send ──────────────────────────────────────────────────────────
    # Primary path: stage reached PROFILE_COMPLETED
    # Recovery path: user explicitly asks for coupon after profile is done
    _COUPON_REQUEST_KWS = ["done", "yes", "profile created", "profile done", "completed", "installed", "downloaded", "haan"]
    _is_coupon_request = matches_any(text, _COUPON_REQUEST_KWS)
    _should_send_coupon = (
        (state["stage"] == "PROFILE_COMPLETED" and not state.get("coupon_sent"))
        or (_is_coupon_request and state.get("profile_created") and not state.get("coupon_sent"))
    )


    if _should_send_coupon:
        reply = (
            "🎉 Welcome to the Sensationz family! 🌸\n"
            "Your app setup and profile are complete.\n\n"
            "🎁 Your welcome coupon code is: *SENSZAPP*\n\n"
            "Use this coupon in the app to activate your discount. See you in class! 🧘‍♀️✨"
        )
        state["coupon_sent"] = True
        state["stage"] = advance_stage(state["stage"], "COUPON_SENT")
        # No follow-up timer here — flow is complete.
        arm_followup_timer(state, topic="coupon activation")
        save_user_state(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return
    # ─────────────────────────────────────────────────────────────────────────


    # --- Genuine question / off-flow topic — goes to RAG ---
    t_rag = time.perf_counter()
    rag_result = await ask_rag_async(text, chat_history=history, state=state)
    full_reply = rag_result["reply"].strip()
    rag_sources = rag_result.get("sources", "")
    rag_retrieval_query = rag_result.get("retrieval_query", "")
    print(f"[TIMING] {phone} rag_query: {time.perf_counter() - t_rag:.2f}s")

    # Strip any "type agent" line the LLM generated, count it silently,
    # only resurface after 2 CONSECUTIVE flagged replies.
    flagged_this_turn = bool(AGENT_SUGGEST_PATTERN.search(full_reply))
    full_reply = AGENT_SUGGEST_PATTERN.sub("", full_reply).strip()

    if flagged_this_turn:
        state["low_confidence_count"] = state.get("low_confidence_count", 0) + 1
    else:
        state["low_confidence_count"] = 0

    followup_separate = None  # Will be sent as a second WhatsApp message if set

    if state.get("low_confidence_count", 0) >= 2:
        # Only suggest agent if the reply was genuinely short/unhelpful (< 60 words)
        if len(full_reply.split()) < 60:
            full_reply += _agent_nudge(text)
        state["low_confidence_count"] = 0  # reset after nudging
    else:
        followup = get_flow_followup(state)
        if followup:
            # Strip trailing LLM-generated questions before our deterministic one
            full_reply = re.sub(r"(?i)\n*would you like to.*?\?", "", full_reply)
            full_reply = re.sub(r"(?i)\n*do you want to.*?\?", "", full_reply)
            full_reply = re.sub(r"(?i)\n*please tell me your preferred.*?(?:\?|\.|!)", "", full_reply)
            full_reply = re.sub(r"(?i)\n*which (timing|package).*?\?", "", full_reply)
            full_reply = re.sub(r"(?i)\n*are you looking to enroll.*?\?", "", full_reply)
            full_reply = full_reply.strip()

            if not should_skip_followup(text, full_reply, state.get("stage")):
                # Issue 4: Two-message stream — answer first, stage question separately
                followup_separate = followup.strip()

    # Post-LLM State Transitions
    if state.get("stage") == "READY_FOR_APP_LINK":
        state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")

    # Only keep nudging the customer if the flow isn't finished yet
    # if state.get("stage") not in ["COUPON_SENT"]:
    arm_followup_timer(state, topic=text)
    save_user_state(phone, state)

    # Format full_reply and followup_separate for WhatsApp rendering
    full_reply = _format_for_whatsapp(full_reply)
    if followup_separate:
        followup_separate = _format_for_whatsapp(followup_separate)

    # Compute sales follow-up question (independent of stage follow-up)
    # get_sales_followup() handles all suppression logic internally.
    sales_followup_q = get_sales_followup(text, full_reply, state)
    if followup_separate:
        # followup_separate (enrollment step) takes priority over sales question
        # on the same turn — avoid sending 3 messages when 2 are already enough.
        sales_followup_q = None
    if sales_followup_q:
        sales_followup_q = _format_for_whatsapp(sales_followup_q)

    t_send = time.perf_counter()
    if followup_separate:
        await send_text_message_async(phone, full_reply)
        await asyncio.sleep(1)
        await send_text_message_async(phone, followup_separate)
        combined = full_reply + "\n\n" + followup_separate
        print(f"[TIMING] {phone} interakt_send (2-msg): {time.perf_counter() - t_send:.2f}s")
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", combined, response_time_sec=latency_sec)
        log_message(phone, "ai", combined, sources=rag_sources, retrieval_query=rag_retrieval_query)
    elif sales_followup_q:
        await send_text_message_async(phone, full_reply)
        await asyncio.sleep(1)
        await send_text_message_async(phone, sales_followup_q)
        combined = full_reply + "\n\n" + sales_followup_q
        print(f"[TIMING] {phone} interakt_send (2-msg + sales_q): {time.perf_counter() - t_send:.2f}s")
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", combined, response_time_sec=latency_sec)
        log_message(phone, "ai", combined, sources=rag_sources, retrieval_query=rag_retrieval_query)
    else:
        await send_text_message_async(phone, full_reply)
        print(f"[TIMING] {phone} interakt_send: {time.perf_counter() - t_send:.2f}s")
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
        log_message(phone, "ai", full_reply, sources=rag_sources, retrieval_query=rag_retrieval_query)
# ---------------------------------------------------------------------------
# Main processing pipeline (NO per-phone Redis lock — debouncer handles it)
# ---------------------------------------------------------------------------

async def process_incoming_message_async(phone: str, text: str, referral: dict = None):
    """
    Fully async message processing pipeline.

    NO Redis lock needed — the batching debouncer in batching.py guarantees
    only one task per phone is active at a time via token-based dedup.
    Different phone numbers run fully concurrently with zero contention.
    """
    start_time = time.time()
    t0 = time.perf_counter()

    # 1. Target ad check
    t_check = time.perf_counter()
    is_target = is_target_ad_or_message(text, referral, phone)
    print(f"[TIMING] {phone} target_check: {time.perf_counter() - t_check:.2f}s")

    if not is_target:
        print(f"[tasks] {phone}: NOT targeted — ignoring (no LLM, no assignment, no reply)")
        return

    # 2. Persist target flag
    try:
        state = get_user_state(phone)
        if not state.get("is_target_ad"):
            state["is_target_ad"] = True
            save_user_state(phone, state)
    except Exception as e:
        print(f"[tasks] {phone}: Failed to save target flag: {e}")
        state ={}
    reset_follow_up_timer(state)
    save_user_state(phone, state)
    # 3. Fetch history
    t_hist = time.perf_counter()
    try:
        history = get_recent_history(phone)
    except Exception as e:
        print(f"[tasks] {phone}: History fetch failed: {e}")
        history = []
    print(f"[TIMING] {phone} history_fetch: {time.perf_counter() - t_hist:.2f}s")

    # 4. Save incoming message
    try:
        save_message(phone, "user", text)
        log_message(phone, "user", text)
    except Exception as e:
        print(f"[tasks] {phone}: Failed to save message: {e}")

    # 5. Check escalation
    if is_escalated(phone):
        print(f"[tasks] {phone}: Already escalated — bot staying out.")
        return

    # 6. Round-robin agent assignment (async, non-blocking)
    t_assign = time.perf_counter()
    # agent = get_next_agent_email()
    if PRIORITY_AGENT_EMAIL and not state.get("already_assigned"):
        success = await assign_chat_to_agent_async(phone, PRIORITY_AGENT_EMAIL)
        if success:
            state["already_assigned"] = True
            save_user_state(phone, state)
    print(f"[TIMING] {phone} agent_assignment({PRIORITY_AGENT_EMAIL}): {time.perf_counter() - t_assign:.2f}s")

    # 7. Check for human agent trigger words
    text_lower = text.lower()
    if matches_any(text_lower, AGENT_TRIGGER_WORDS):
        await handle_agent_handoff_async(phone, start_time)
        print(f"[TIMING] {phone} PIPELINE TOTAL: {time.perf_counter() - t0:.2f}s")
        return

    # 8. AI reply
    await handle_ai_reply_async(phone, text, history, start_time)

    print(f"[TIMING] {phone} PIPELINE TOTAL: {time.perf_counter() - t0:.2f}s")


