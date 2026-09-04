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
from interakt import send_text_message_async, assign_chat_to_agent_async
from chat_state import reset_follow_up_timer, arm_followup_timer

from chat_history import (
    save_message, get_recent_history,
    save_message_async, get_recent_history_async
)
from csv_logger import log_message, log_message_async
from chat_state import (
    mark_escalated,
    is_escalated,
    get_user_state,
    save_user_state,
    mark_escalated_async,
    is_escalated_async,
    get_user_state_async,
    save_user_state_async,
    extract_and_update_slots,
    is_user_asking_question,
    matches_any,
    advance_stage,
)
from rag import ask_rag_async, stream_rag
from redis_client import get_redis_connection
from sales_followup import get_sales_followup
from agent_summary import send_agent_summary_async
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


TARGET_MESSAGE_TEXT = os.getenv("TARGET_MESSAGE_TEXT", "Hello!! Can I get more info on Yoga classes?")

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
def get_next_agent_email() -> tuple:
    """
    Returns (agent_email, agent_index) in round-robin order.
    Uses Redis INCR for atomicity across concurrent requests and processes.
    agent_index is used to send the summary to the matching agent phone number.
    """
    if not AGENT_POOL:
        return PRIORITY_AGENT_EMAIL, 0  # fallback
    counter = redis_conn.incr("agent_round_robin_counter")
    index = (counter - 1) % len(AGENT_POOL)
    agent = AGENT_POOL[index]
    print(f"[round-robin] counter={counter} -> agent[{index}] = {agent}")
    return agent, index


# ---------------------------------------------------------------------------
# Target ad / message verification
# ---------------------------------------------------------------------------
def is_target_ad_or_message(text: str, referral_data: dict = None, phone: str = None) -> bool:
    """
    Checks if a message qualifies for bot response.
    1. Already-verified user (state flag) → PASS
    2. First message: Match exact code '0123456789' in text → PASS
    3. Otherwise → FAIL (no reply, no assignment)
    """
    if phone:
        try:
            state = get_user_state(phone)
            if state.get("is_target_ad") is True:
                print(f"[2] 🎯 DECISION : {phone} -> WILL REPLY | Reason: Already verified customer (is_target_ad=True)")
                return True
        except Exception:
            pass

    # Match exact code '0123456789' in incoming message
    TARGET_CODE = "0123456789"
    if TARGET_CODE in (text or ""):
        print(f"[2] 🎯 DECISION : {phone} -> WILL REPLY | Reason: Target code '{TARGET_CODE}' matched in message")
        return True

    print(f"[2] 🎯 DECISION : {phone} -> WILL NOT REPLY | Reason: Target code '{TARGET_CODE}' not found in message")
    return False



# ---------------------------------------------------------------------------
# Agent handoff (async)
# ---------------------------------------------------------------------------
async def handle_agent_handoff_async(phone: str, start_time: float = None):
    """Handles customer request to talk to a human / call support — fully async."""
    print(f"[tasks] Agent requested by {phone}")
    reply = (
        "Connecting you with our support team! 🙏\n\n"
        "Aapki request humari team tak pahunch gayi hai. "
        "Ek team member aapse jald connect karenge 😊"
    )

    agent, agent_index = get_next_agent_email()
    if agent:
        await assign_chat_to_agent_async(phone, agent)
        await send_text_message_async(phone, reply)
        await mark_escalated_async(phone)
        asyncio.create_task(log_message_async(phone, "agent", reply))
        # Send Hinglish summary to the assigned agent's WhatsApp number
        asyncio.create_task(
            send_agent_summary_async(phone, agent_index, escalation_reason="Customer ne 'agent' type kiya")
        )
    else:
        await send_text_message_async(phone, reply)

    latency_sec = round(time.time() - start_time, 2) if start_time else None
    print(f"[TIMING] {phone} agent_handoff TOTAL: {latency_sec}s")
    asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))




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

    # 4. Support, Agent, Refund, Cancellation, Policy, Complaint, Dispute
    support_kws = ["agent", "human", "refund", "cancel", "cancellation", "complaint", "dispute", "policy", "attendance", "reschedule", "pause", "support", "call", "baat karni", "talk to"]
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



def _strip_trailing_questions(text: str) -> str:
    """Strips trailing LLM-generated follow-up questions from the main answer to ensure Message 1 is clean."""
    patterns = [
        r"(?i)\n*would you like to.*?\?",
        r"(?i)\n*do you want to.*?\?",
        r"(?i)\n*please tell me your preferred.*?(?:\?|\.|!)",
        r"(?i)\n*which (timing|package|teacher|time slot|duration).*?\?",
        r"(?i)\n*what is your main (goal|focus).*?\?",
        r"(?i)\n*aap (kaunsa|kis|kisme|kya|laptop|morning|subah).*?\?",
        r"(?i)\n*kya aap pehle ek free trial.*?\?",
        r"(?i)\n*kya aap.*?\?",
        r"(?i)\n*are you looking to enroll.*?\?",
        r"(?i)\n*are you ready to.*?\?",
        r"(?i)\n*how long would you like.*?\?",
        r"(?i)\n*is there anything else.*?\?",
    ]
    cleaned = text
    for p in patterns:
        cleaned = re.sub(p, "", cleaned)
    return cleaned.strip()


def get_flow_followup(state: dict) -> str:
    # 1. If enrollment completed or trial mode active
    if (state.get("stage") in ["PROFILE_COMPLETED", "COUPON_SENT", "TRIAL_STEPS_SENT", "TRIAL_REQUESTED"]
            or state.get("coupon_sent") or state.get("wants_trial")):
        return None
        
    # 2. If app links are sent or ready for app link
    if state.get("stage") in ["READY_FOR_APP_LINK", "APP_LINK_SENT"]:
        return (
            "Aapke liye ek special welcome discount code hai 🎁\n\n"
            "Sirf Sensationz App download karein ya website par jayein aur apna profile banayein — "
            "uske baad *Done* ya *Yes* reply karein, aur main turant aapka coupon code bhej dunga!\n\n"
            "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
            "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n"
            "💻 Website / PC / Laptop: https://shop.sensationzperformingarts.com/"
        )
        
    # 3. If timing is selected but package is missing (and user is NOT in trial mode)
    if state.get("timing") and not state.get("package") and not state.get("wants_trial"):
        return (
            "Which package duration would you like to start with? 😊\n"
            "Fees: 1M: 700 (offer price: 500), 3M: 1750 (offer price: 600), 6M: 3200 (offer price: 2050), 1Y: 5000 (offer price: 3850)\n\n"
            "*Note:* Offer price will be only applicable through app and welcome coupon. Once the app is downloaded and the profile is created, the welcome coupon will be sent here."
        )
        
    # 4. If timing is missing
    if not state.get("timing"):
        if state.get("stage") == "NEW":
            return None
        return (
            "Which timing would you prefer for your classes? 😊\n"
            "Morning: 5:00–6:00 AM, 6:00–7:00 AM, 7:00–8:00 AM, 8:00–9:00 AM, 10:00–11:00 AM\n"
            "Afternoon: 12:00–1:00 PM\n"
            "Evening: 4:00–5:00 PM, 5:00–6:00 PM, 6:00–7:00 PM, 7:00–8:00 PM"
        )
    return None


from chat_state import arm_followup_timer, reset_follow_up_timer, reset_user_state_async

async def handle_ai_reply_async(phone: str, text: str, history: list, start_time: float = None):
    t0 = time.perf_counter()

    # Developer/tester reset command
    if text.strip().lower() in ["#reset", "/reset", "reset chat", "reset session"]:
        await reset_user_state_async(phone)
        reply = "🔄 Session reset successfully! You can now test from the beginning as a new customer. How can I help you? 😊"
        await send_text_message_async(phone, reply)
        return

    if text.strip().lower() == TARGET_MESSAGE_TEXT.strip().lower():
        msg1 = "Welcome to Sensationz! 🙏 We're excited to help you start your wellness journey."
        msg2 = "We offer Online Live Interactive Yoga classes (Monday to Friday) with certified expert instructors, beginner-friendly packages starting at just Rs. 700/month (offer price: Rs. 300)."
        msg3 = "We have batches running throughout the day (Morning, Afternoon, and Evening). Which time slot works best for your schedule?"

        await send_text_message_async(phone, msg1)
        await asyncio.sleep(1)
        await send_text_message_async(phone, msg2)
        await asyncio.sleep(1)
        await send_text_message_async(phone, msg3)

        # Arm follow-up timer for this welcome message too
        state = await get_user_state_async(phone)
        arm_followup_timer(state, topic="welcome message")
        await save_user_state_async(phone, state)

        latency_sec = round(time.time() - start_time, 2) if start_time else None
        full_welcome = f"{msg1}\n{msg2}\n{msg3}"
        # Fire-and-forget background logging to Supabase and CSV so WhatsApp reply is never delayed
        asyncio.create_task(save_message_async(phone, "assistant", full_welcome, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", full_welcome))
        return

    # Fetch previous state stage before slot extraction ok ok
    try:
        pre_state = await get_user_state_async(phone)
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
        await save_user_state_async(phone, state)
        reply = (
            f"Aap {amb} ki timing chahte hain — subah (AM) ya shaam (PM)? 😊\n"
            f"• Subah ke liye likhein: '{amb} AM'\n"
            f"• Shaam ke liye likhein: '{amb} PM'"
        )
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", reply))
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
        await save_user_state_async(phone, state)
        reply = _feedback_request_msg(text)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", reply))
        return

    if state.get("disinterest_asked_feedback"):
        state["disinterest_asked_feedback"] = False
        await save_user_state_async(phone, state)
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
            await save_user_state_async(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            # Fire-and-forget background logging
            asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
            asyncio.create_task(log_message_async(phone, "ai", reply))
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
    # ── TRIAL / DEMO REQUEST HANDLER (Pure procedural requests only; informational queries go to RAG) ──
    _PURE_TRIAL_BOOKING_KWS = [
        "trial book", "book trial", "trial lena hai", "demo lena hai", "trial kaise book karein",
        "trial kaise book kare", "send trial link", "trial link do", "trial link bhejo", "book my trial"
    ]
    is_pure_trial_req = (
        not is_q
        and not is_info_intent(text)
        and matches_any(text_lower, _PURE_TRIAL_BOOKING_KWS)
    )
    if is_pure_trial_req and state.get("stage") != "TRIAL_STEPS_SENT" and state.get("stage") not in ["PROFILE_COMPLETED", "COUPON_SENT"]:
        state["wants_trial"] = True
        state["stage"] = "TRIAL_STEPS_SENT"

        hindi_markers = ["kya", "hai", "mujhe", "batao", "chahiye", "ka", "ki", "ke", "nahi", "haan", "se", "bhi", "kab", "kaise", "kitna", "subah", "shaam", "pehle", "baad"]
        has_hindi = any(w in text_lower for w in hindi_markers) or any("\u0900" <= ch <= "\u097F" for ch in text)
        if has_hindi:
            msg1 = (
                "Aap bilkul pehle demo videos dekh sakte hain aur free live trial class attend kar sakte hain! 😊 Har student ko 3 free live trial classes milti hain.\n\n"
                "🎥 *Sample / Demo Videos:*\n"
                "• Trainer Suman: https://youtu.be/IiVVdu4NkwI?si=leLgCK40Uo5Qhr0V\n"
                "• Trainer Mradula: https://youtu.be/vXZ6UtrWpM8?si=WYpuo8Us7xIkXT8n\n"
                "• Trainer Priya Mathur: https://youtu.be/M2Zh9SaHpX4?si=RXg-HXGI5n_ftxs-"
            )
            msg2 = (
                "📲 *Live Trial Book Karne Ke Simple Steps:*\n"
                "1️⃣ Sensationz App download karein ya website visit karein\n"
                "2️⃣ Profile create karke 'Trial Links' par tap karein\n"
                "3️⃣ Apna preferred batch timing choose karein aur live trial confirm karein!\n\n"
                "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
                "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n"
                "💻 Website / PC / Laptop: https://shop.sensationzperformingarts.com/"
            )
        else:
            msg1 = (
                "You can watch our sample demo videos and attend free live trial classes first! 😊 Up to 3 free live trial classes are allowed per student.\n\n"
                "🎥 *Sample / Demo Videos:*\n"
                "• Trainer Suman: https://youtu.be/IiVVdu4NkwI?si=leLgCK40Uo5Qhr0V\n"
                "• Trainer Mradula: https://youtu.be/vXZ6UtrWpM8?si=WYpuo8Us7xIkXT8n\n"
                "• Trainer Priya Mathur: https://youtu.be/M2Zh9SaHpX4?si=RXg-HXGI5n_ftxs-"
            )
            msg2 = (
                "📲 *Steps to Book Your Live Trial:*\n"
                "1️⃣ Download the Sensationz App or visit our website\n"
                "2️⃣ Create your profile and tap on 'Trial Links'\n"
                "3️⃣ Select your preferred batch timing and confirm your live trial!\n\n"
                "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
                "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n"
                "💻 Website / PC / Laptop: https://shop.sensationzperformingarts.com/"
            )
        arm_followup_timer(state, topic=text)
        await save_user_state_async(phone, state)
        msg1 = _format_for_whatsapp(msg1)
        msg2 = _format_for_whatsapp(msg2)
        await send_text_message_async(phone, msg1)
        await asyncio.sleep(1)
        await send_text_message_async(phone, msg2)
        combined = msg1 + "\n\n" + msg2
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        asyncio.create_task(save_message_async(phone, "assistant", combined, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", combined))
        return

    # --- DETERMINISTIC STAGE GUARDS ---

    if not is_q and not is_info_intent(text) and state["stage"] == "ENROLL_CONFIRMED" and (is_fresh_enroll_confirmed or is_confirmation or is_greeting):
        reply = get_flow_followup(state)
        if reply:
            reply = _format_for_whatsapp(reply.strip())
            arm_followup_timer(state, topic=text)
            await save_user_state_async(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            # Fire-and-forget background logging
            asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
            asyncio.create_task(log_message_async(phone, "ai", reply))
            return

    if not is_q and not is_info_intent(text) and state["stage"] == "PACKAGE_ASKED" and (is_fresh_package_asked or is_confirmation or is_greeting):
        reply = get_flow_followup(state)
        if reply:
            reply = _format_for_whatsapp(reply.strip())
            arm_followup_timer(state, topic=text)
            await save_user_state_async(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            # Fire-and-forget background logging
            asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
            asyncio.create_task(log_message_async(phone, "ai", reply))
            return

    if not is_q and not is_info_intent(text) and state["stage"] == "TIMING_SELECTED" and not state.get("package") and (is_fresh_timing_selected or is_confirmation or is_greeting):
        msg1 = f"You've selected the {state.get('timing')} batch. 👍"
        msg2 = (
            "Which package duration would you like to start with? 😊\n"
            "Fees: 1M: 700 (offer price: 500), 3M: 1750 (offer price: 600), 6M: 3200 (offer price: 2050), 1Y: 5000 (offer price: 3850)\n\n"
            "*Note:* Offer price will be only applicable through app and welcome coupon. Once the app is downloaded and the profile is created, the welcome coupon will be sent here."
        )
        state["stage"] = advance_stage(state["stage"], "PACKAGE_ASKED")
        arm_followup_timer(state, topic=text)
        await save_user_state_async(phone, state)
        msg1 = _format_for_whatsapp(msg1)
        msg2 = _format_for_whatsapp(msg2)
        await send_text_message_async(phone, msg1)
        await asyncio.sleep(1)
        await send_text_message_async(phone, msg2)
        combined = msg1 + "\n\n" + msg2
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", combined, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", combined))
        return

    if not is_q and not is_info_intent(text) and state["stage"] == "PACKAGE_SELECTED" and not state.get("timing") and (is_fresh_package_selected or is_confirmation or is_greeting):
        msg1 = f"You've selected the {state.get('package')} package. 👍"
        msg2 = (
            "Which timing would you prefer for your classes? 😊\n"
            "Morning: 5:00–6:00 AM, 6:00–7:00 AM, 7:00–8:00 AM, 8:00–9:00 AM, 10:00–11:00 AM\n"
            "Afternoon: 12:00–1:00 PM\n"
            "Evening: 4:00–5:00 PM, 5:00–6:00 PM, 6:00–7:00 PM, 7:00–8:00 PM"
        )
        state["stage"] = advance_stage(state["stage"], "ENROLL_CONFIRMED")
        arm_followup_timer(state, topic=text)
        await save_user_state_async(phone, state)
        msg1 = _format_for_whatsapp(msg1)
        msg2 = _format_for_whatsapp(msg2)
        await send_text_message_async(phone, msg1)
        await asyncio.sleep(1)
        await send_text_message_async(phone, msg2)
        combined = msg1 + "\n\n" + msg2
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", combined, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", combined))
        return


    # ── Deterministic Coupon Unlock & Resend Handlers ────────────────────────


    if not is_q and not is_info_intent(text) and state["stage"] == "READY_FOR_APP_LINK":
        package = state.get("package") or "3 Months"
        fee = state.get("fee") or "₹1,750 (Offer Price: ₹600)"
        msg1 = f"You've selected the {package} package ({fee}). 👍"
        msg2 = (
            "To continue, please download the Sensationz App (or access via website) and create your profile. "
            "Once that's done, just reply *Done* or *Yes* here, and I'll send you a special welcome coupon code 🎁 "
            "that you can use to unlock your offer price during checkout in the app or website.\n\n"
            "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
            "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n"
            "💻 Website / PC / Laptop: https://shop.sensationzperformingarts.com/"
        )
        state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")
        arm_followup_timer(state, topic=text)
        await save_user_state_async(phone, state)
        msg1 = _format_for_whatsapp(msg1)
        msg2 = _format_for_whatsapp(msg2)
        await send_text_message_async(phone, msg1)
        await asyncio.sleep(1)
        await send_text_message_async(phone, msg2)
        combined = msg1 + "\n\n" + msg2
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", combined, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", combined))
        return

    # ── 1. Explicit Coupon Request / Resend Handler ─────────────────────────
    # Handles user asking for code ("konsa coupon", "send coupon", "fhrse bjhdo", "kha h")
    _EXPLICIT_COUPON_ASK_KWS = [
        "konsa coupon", "konsa code", "kya code", "code kya", "coupon code kya",
        "send coupon", "send code", "send discount coupon", "code do", "code bhej",
        "bhejo code", "coupon do", "coupon bhejo", "kaha hai", "kha h", "kaha h",
        "fhrse bjhdo", "phir se bhejo", "dobara bhejo", "again send", "resend", "resend code",
        "code nahi mila", "code nhi mila", "code nahi aaya", "code nhi aaya", "where is code",
        "where is coupon", "give coupon", "give code"
    ]
    _is_explicit_coupon_ask = matches_any(text_lower, _EXPLICIT_COUPON_ASK_KWS)

    if _is_explicit_coupon_ask:
        has_unlocked_coupon = (
            state.get("coupon_sent")
            or state.get("profile_created")
            or state.get("stage") in ["PROFILE_COMPLETED", "COUPON_SENT"]
        )

        if has_unlocked_coupon:
            # Stage B: User already completed profile / unlocked coupon -> Provide duration-specific code (YOGA300 for 1M, YOGA600 for 3M, YOGA1000 for 6M, YOGA1800 for 1Y)
            pkg = state.get("package") or ""
            pkg_lower = pkg.lower()
            is_1_month = "1 month" in pkg_lower or "one month" in pkg_lower
            is_3_month = "3 month" in pkg_lower or "three month" in pkg_lower
            is_6_month = "6 month" in pkg_lower or "six month" in pkg_lower
            is_1_year = "1 year" in pkg_lower or "12 month" in pkg_lower or "one year" in pkg_lower or "yearly" in pkg_lower

            hindi_markers = ["kya", "hai", "bhejo", "batao", "do", "kaha", "kha", "dobara", "phir", "fhrse", "mujhe", "konsa"]
            has_hindi = any(w in text_lower for w in hindi_markers) or any("\u0900" <= ch <= "\u097F" for ch in text)

            if is_1_month:
                code = "YOGA300"
                pkg_name = "1 Month"
            elif is_3_month:
                code = "YOGA600"
                pkg_name = "3 Months"
            elif is_6_month:
                code = "YOGA1000"
                pkg_name = "6 Months"
            elif is_1_year:
                code = "YOGA1800"
                pkg_name = "1 Year"
            else:
                code = None
                pkg_name = None

            if code:
                if has_hindi:
                    reply = (
                        "Aapka welcome discount coupon code ye raha 🎁\n\n"
                        f"✨ Coupon Code: *{code}* ({pkg_name} package ke liye)\n\n"
                        "Isko Sensationz App ya website checkout par enter karke apply karein. Class mein milte hain! 🧘‍♀️"
                    )
                else:
                    reply = (
                        "Here is your welcome discount coupon code 🎁\n\n"
                        f"✨ Coupon Code: *{code}* (for {pkg_name} package)\n\n"
                        "Please enter this code during checkout in the Sensationz App or website to activate your discount. See you in class! 🧘‍♀️"
                    )
            else:
                if has_hindi:
                    reply = (
                        "Aapke welcome discount coupon codes ye rahe 🎁\n\n"
                        "• 1 Month duration: *YOGA300*\n"
                        "• 3 Months duration: *YOGA600*\n"
                        "• 6 Months duration: *YOGA1000*\n"
                        "• 1 Year duration: *YOGA1800*\n\n"
                        "Isko Sensationz App ya website checkout par enter karke apply karein. Class mein milte hain! 🧘‍♀️"
                    )
                else:
                    reply = (
                        "Here are your welcome discount coupon codes 🎁\n\n"
                        "• 1 Month duration: *YOGA300*\n"
                        "• 3 Months duration: *YOGA600*\n"
                        "• 6 Months duration: *YOGA1000*\n"
                        "• 1 Year duration: *YOGA1800*\n\n"
                        "Please enter the applicable code during checkout in the Sensationz App or website to activate your discount. See you in class! 🧘‍♀️"
                    )

            state["coupon_sent"] = True
            arm_followup_timer(state, topic="coupon resend")
            await save_user_state_async(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            # Fire-and-forget background logging
            asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
            asyncio.create_task(log_message_async(phone, "ai", reply))
            return
        else:
            # Stage A: User has NOT completed profile yet -> Explain unlock requirement with app links, do NOT leak code
            reply = (
                "Aapka special welcome discount coupon code app ya website par profile banane ke baad unlock hota hai 🎁\n\n"
                "1️⃣ Sensationz App download karein ya website visit karein\n"
                "2️⃣ Profile complete karein\n"
                "3️⃣ Yahan *Done* ya *Yes* reply karein\n\n"
                "Aur main turant aapka coupon code yahan bhej dunga!\n\n"
                "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
                "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n"
                "💻 Website / PC / Laptop: https://shop.sensationzperformingarts.com/"
            )
            state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")
            arm_followup_timer(state, topic=text)
            await save_user_state_async(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            # Fire-and-forget background logging
            asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
            asyncio.create_task(log_message_async(phone, "ai", reply))
            return

    # ── 2. Primary Coupon Delivery on Profile Confirmation ───────────────────
    _COUPON_REQUEST_KWS = [
        "profile created", "profile done", "profile completed", "created profile",
        "profile ban gayi", "profile bana li", "app downloaded", "app installed",
        "installed", "downloaded", "done", "yes", "haan"
    ]
    _is_coupon_request = matches_any(text, _COUPON_REQUEST_KWS)
    _should_send_coupon = (
        (state.get("profile_created") and not state.get("coupon_sent"))
        or (state.get("stage") in ["PROFILE_COMPLETED", "COUPON_SENT"] and not state.get("coupon_sent"))
        or (_is_coupon_request and not state.get("coupon_sent") and state.get("profile_created"))
    )

    if _should_send_coupon:
        pkg = state.get("package") or ""
        pkg_lower = pkg.lower()
        is_1_month = "1 month" in pkg_lower or "one month" in pkg_lower
        is_3_month = "3 month" in pkg_lower or "three month" in pkg_lower
        is_6_month = "6 month" in pkg_lower or "six month" in pkg_lower
        is_1_year = "1 year" in pkg_lower or "12 month" in pkg_lower or "one year" in pkg_lower or "yearly" in pkg_lower

        if is_1_month:
            code_line = "🎁 Your welcome coupon code for 1 Month is: *YOGA300*"
        elif is_3_month:
            code_line = "🎁 Your welcome coupon code for 3 Months is: *YOGA600*"
        elif is_6_month:
            code_line = "🎁 Your welcome coupon code for 6 Months is: *YOGA1000*"
        elif is_1_year:
            code_line = "🎁 Your welcome coupon code for 1 Year is: *YOGA1800*"
        else:
            code_line = (
                "🎁 Your welcome coupon codes:\n"
                "• 1 Month duration: *YOGA300*\n"
                "• 3 Months duration: *YOGA600*\n"
                "• 6 Months duration: *YOGA1000*\n"
                "• 1 Year duration: *YOGA1800*"
            )

        reply = (
            "🎉 Welcome to the Sensationz family! 🌸\n"
            "Your app setup and profile are complete.\n\n"
            f"{code_line}\n\n"
            "Use this coupon in the app or website to activate your discount. See you in class! 🧘‍♀️✨"
        )
        state["coupon_sent"] = True
        state["stage"] = advance_stage(state["stage"], "COUPON_SENT")
        # No follow-up timer here — flow is complete.
        arm_followup_timer(state, topic="coupon activation")
        await save_user_state_async(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", reply))
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
        if followup and not should_skip_followup(text, full_reply, state.get("stage")):
            # Issue 4: Two-message stream — answer first, stage question separately
            followup_separate = followup.strip()

    # Clean trailing LLM-generated questions so full_reply is purely the direct answer (Message 1)
    full_reply = _strip_trailing_questions(full_reply)

    # Post-LLM State Transitions
    if state.get("stage") == "READY_FOR_APP_LINK":
        state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")

    # Only keep nudging the customer if the flow isn't finished yet
    # if state.get("stage") not in ["COUPON_SENT"]:
    arm_followup_timer(state, topic=text)
    await save_user_state_async(phone, state)

    # Format full_reply and followup_separate for WhatsApp rendering
    full_reply = _format_for_whatsapp(full_reply)
    if followup_separate:
        followup_separate = _format_for_whatsapp(followup_separate)

    # Compute sales follow-up question (independent of stage follow-up)
    # get_sales_followup() handles all topic suppression internally (medical, refund, complaint, agent)
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
        # Fire-and-forget background logging to Supabase and CSV without blocking user reply
        asyncio.create_task(save_message_async(phone, "assistant", combined, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", combined, sources=rag_sources, retrieval_query=rag_retrieval_query))
    elif sales_followup_q:
        await send_text_message_async(phone, full_reply)
        await asyncio.sleep(1)
        await send_text_message_async(phone, sales_followup_q)
        combined = full_reply + "\n\n" + sales_followup_q
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging to Supabase and CSV without blocking user reply
        asyncio.create_task(save_message_async(phone, "assistant", combined, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", combined, sources=rag_sources, retrieval_query=rag_retrieval_query))
    else:
        await send_text_message_async(phone, full_reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging to Supabase and CSV without blocking user reply
        asyncio.create_task(save_message_async(phone, "assistant", full_reply, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", full_reply, sources=rag_sources, retrieval_query=rag_retrieval_query))
# ---------------------------------------------------------------------------
# Main processing pipeline (NO per-phone Redis lock — debouncer handles it)
# ---------------------------------------------------------------------------

async def _execute_pipeline_async(phone: str, text: str, referral: dict = None):
    """Internal execution logic for message processing."""
    start_time = time.time()

    # Log 1: Incoming message
    print("\n" + "="*80)
    print(f"[1] 📩 INCOMING : {phone} -> {repr(text)}")

    # Log 2: Target check & decision
    is_target = is_target_ad_or_message(text, referral, phone)

    if not is_target:
        # Log 3: Ignored action
        print(f"[3] 🚫 ACTION   : {phone} -> Ignored (No reply sent)")
        print("="*80 + "\n")
        return

    # Persist target flag
    try:
        state = await get_user_state_async(phone)
        if not state.get("is_target_ad"):
            state["is_target_ad"] = True
            await save_user_state_async(phone, state)
    except Exception as e:
        state = {}
    reset_follow_up_timer(state)
    await save_user_state_async(phone, state)

    # Fetch history
    try:
        history = await get_recent_history_async(phone)
    except Exception as e:
        history = []

    # Save incoming message (fire-and-forget in background)
    try:
        asyncio.create_task(save_message_async(phone, "user", text))
        asyncio.create_task(log_message_async(phone, "user", text))
    except Exception as e:
        pass

    # Check escalation
    if await is_escalated_async(phone):
        print(f"[3] 👤 ACTION   : {phone} -> Already escalated to agent (AI staying out)")
        print("="*80 + "\n")
        return

    # Round-robin agent assignment (async, non-blocking)
    if PRIORITY_AGENT_EMAIL and not state.get("already_assigned"):
        success = await assign_chat_to_agent_async(phone, PRIORITY_AGENT_EMAIL)
        if success:
            state["already_assigned"] = True
            await save_user_state_async(phone, state)

    # Check for human agent trigger words
    text_lower = text.lower()
    if matches_any(text_lower, AGENT_TRIGGER_WORDS):
        await handle_agent_handoff_async(phone, start_time)
        print(f"[3] 👤 ACTION   : {phone} -> Handed off to human agent")
        print("="*80 + "\n")
        return

    # AI reply
    await handle_ai_reply_async(phone, text, history, start_time)
    print(f"[3] 🤖 ACTION   : {phone} -> AI Reply sent successfully (Message 1 + Message 2)")
    print("="*80 + "\n")




async def process_incoming_message_async(phone: str, text: str, referral: dict = None):
    """
    Fully async message processing pipeline with In-Flight Busy Guard and Queue Drain.
    If the AI is actively generating a reply for this phone number, new incoming messages
    are safely queued and processed together as 1 clean follow-up turn when active processing ends.
    """
    proc_lock_key = f"is_processing:{phone}"
    pending_queue_key = f"pending_queue:{phone}"

    # Check if AI is currently busy generating a reply for this user
    if redis_conn.get(proc_lock_key):
        print(f"[tasks] {phone}: AI is currently busy generating a reply — queueing message '{text}'")
        redis_conn.rpush(pending_queue_key, text)
        return

    # Acquire lock (30s max safety TTL)
    redis_conn.setex(proc_lock_key, 30, "true")

    try:
        await _execute_pipeline_async(phone, text, referral)
    finally:
        # Drain pending queue if any messages arrived while AI was typing
        try:
            raw_pending = redis_conn.lrange(pending_queue_key, 0, -1)
            redis_conn.delete(pending_queue_key)
            if raw_pending:
                pending_msgs = [m.decode() if isinstance(m, bytes) else m for m in raw_pending]
                combined_pending = "\n".join(pending_msgs)
                print(f"[tasks] {phone}: draining {len(pending_msgs)} queued messages -> '{combined_pending}'")
                # Release processing lock before recursive drain call
                redis_conn.delete(proc_lock_key)
                await process_incoming_message_async(phone, combined_pending, referral=referral)
            else:
                redis_conn.delete(proc_lock_key)
        except Exception as ex:
            redis_conn.delete(proc_lock_key)
            print(f"[tasks] {phone}: error during queue drain: {ex}")


