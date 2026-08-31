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



def get_flow_followup(state: dict) -> str:
    # 1. If enrollment completed (coupon sent or profile completed)
    if state.get("stage") in ["PROFILE_COMPLETED", "COUPON_SENT"] or state.get("coupon_sent"):
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
        
    # 3. If timing is selected but package is missing
    if state.get("timing") and not state.get("package"):
        return (
            "\n\nWhich package duration would you like to start with?\n"
            "Fees: 1M: 700 (offer price: 500), 3M: 1750 (offer price: 600), 6M: 3200 (offer price: 2050), 1Y: 5000 (offer price: 3850)\n\n"
            "*Note:* Offer price will be only applicable through app and welcome coupon. Once the app is downloaded and the profile is created, the welcome coupon will be sent here 😊"
        )
        
    # 4. If timing is missing
    if not state.get("timing"):
        if state.get("stage") == "NEW":
            return None
        return (
            "\n\nWhich timing would you prefer for your classes?\n"
            "Morning: 5:00-6:00AM, 6:00–7:00 AM, 7:00–8:00 AM, 8:00–9:00 AM, 10:00–11:00 AM\n"
            "Afternoon: 12:00–1:00 PM\n"
            "Evening: 4:00–5:00 PM, 5:00–6:00 PM, 6:00–7:00 PM, 7:00–8:00 PM"
        )
    return None


from chat_state import arm_followup_timer, reset_follow_up_timer

async def handle_ai_reply_async(phone: str, text: str, history: list, start_time: float = None):
    t0 = time.perf_counter()

    if text.strip().lower() == TARGET_MESSAGE_TEXT.strip().lower():
        msg1 = "Welcome to Sensationz! 🙏 We're excited to help you start your wellness journey."
        msg2 = "We offer Online Live Interactive Yoga classes (Monday to Friday) with certified expert instructors, beginner-friendly packages starting at just Rs. 700/month (offer price: Rs. 500), and full access to class recordings."
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
    # ── END DISINTEREST CHECK ──

    # --- DETERMINISTIC STAGE GUARDS ---
    if not is_q and not is_info_intent(text) and state["stage"] == "ENROLL_CONFIRMED" and (is_fresh_enroll_confirmed or is_confirmation or is_greeting):
        reply = get_flow_followup(state)
        if reply:
            reply = reply.strip()
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
            reply = reply.strip()
            arm_followup_timer(state, topic=text)
            await save_user_state_async(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            # Fire-and-forget background logging
            asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
            asyncio.create_task(log_message_async(phone, "ai", reply))
            return

    if not is_q and not is_info_intent(text) and state["stage"] == "TIMING_SELECTED" and not state.get("package") and (is_fresh_timing_selected or is_confirmation or is_greeting):
        reply = (
            f"You've selected the {state.get('timing')} batch.\n\n"
            "Would you like to go ahead and pick a package duration too? 😊\n"
            "Fees: 1M: 700 (offer price: 500), 3M: 1750 (offer price: 600), 6M: 3200 (offer price: 2050), 1Y: 5000 (offer price: 3850)\n\n"
            "*Note:* Offer price will be only applicable through app and welcome coupon. Once the app is downloaded and the profile is created, the welcome coupon will be sent here."
        )
        state["stage"] = advance_stage(state["stage"], "PACKAGE_ASKED")
        arm_followup_timer(state, topic=text)
        await save_user_state_async(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", reply))
        return

    if not is_q and not is_info_intent(text) and state["stage"] == "PACKAGE_SELECTED" and not state.get("timing") and (is_fresh_package_selected or is_confirmation or is_greeting):
        reply = (
            f"You've selected the {state.get('package')} package.\n\n"
            "Which timing would you prefer for your classes? 😊\n"
            "Morning: 5:00–6:00 AM, 6:00–7:00 AM, 7:00–8:00 AM, 8:00–9:00 AM, 10:00–11:00 AM\n"
            "Afternoon: 12:00–1:00 PM\n"
            "Evening: 4:00–5:00 PM, 5:00–6:00 PM, 6:00–7:00 PM, 7:00–8:00 PM"
        )
        state["stage"] = advance_stage(state["stage"], "ENROLL_CONFIRMED")
        arm_followup_timer(state, topic=text)
        await save_user_state_async(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", reply))
        return


    # ── Discount / coupon question — intercept at ANY stage ──────────────────
    # Never let RAG handle discount/coupon questions — it doesn't have this info.
    # We set full_reply here and fall through to the normal send path so that
    # get_flow_followup() automatically appends the next enrollment step as msg 2.
    _DISCOUNT_KWS = [
        "discount", "coupon", "offer", "special offer", "discount code", "coupon code",
        "kya milega", "kya hoga", "kya discount", "special discount", "discount btao",
        "koi offer", "kam karo", "ad mein", "ad me", "ads mein", "ads me",
        "wahan toh", "waha to", "wahan to", "waha toh", "alg price", "alag price",
        "kam price", "kam tha", "kam btaya", "kam rate", "sasta"
    ]
    _words_set = set(re.findall(r"\b\w+\b", text_lower))
    _is_ad_word = bool(_words_set & {"ad", "ads", "advertisement", "insta", "instagram", "facebook", "fb", "reel"})
    _is_discount_query = any(kw in text_lower for kw in _DISCOUNT_KWS) or _is_ad_word

    if _is_discount_query and not state.get("coupon_sent"):
        current_stage = state.get("stage", "NEW")
        is_ad_mention = _is_ad_word or any(p in text_lower for p in ["ad mein", "ad me", "ads mein", "ads me", "wahan toh", "waha to", "wahan to", "waha toh", "alag price", "kam price", "kam tha", "kam btaya", "kam rate"])

        if current_stage in ["APP_LINK_SENT", "READY_FOR_APP_LINK"]:
            discount_reply = (
                "Aapke liye ek special welcome discount code hai 🎁\n\n"
                "Sirf Sensationz App download karein ya website par jayein aur apna profile banayein — "
                "uske baad *Done* ya *Yes* reply karein, aur main turant aapka coupon code bhej dunga!\n\n"
                "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
                "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n"
                "💻 Website / PC / Laptop: https://shop.sensationzperformingarts.com/"
            )
            state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")
        elif is_ad_mention:
            # Dynamic ad pricing explanation — applies universally to any past or future ad campaign
            discount_reply = (
                "Haan bilkul! 😊 Ads mein jo promotional / special offer price dikhaya jata hai, "
                "woh hamare new members ke *welcome discount coupon* ke through hi unlock hota hai 🎁\n\n"
                "Ye discount paane ke simple steps hain:\n"
                "1️⃣ Apna timing aur package choose karein\n"
                "2️⃣ Sensationz App download karein ya website https://shop.sensationzperformingarts.com/ visit karein\n"
                "3️⃣ Profile banayein\n"
                "4️⃣ Yahan *Done* ya *Yes* reply karein — coupon code turant bhej diya jayega!"
            )
        else:
            # Early stage — answer general discount question
            discount_reply = (
                "Haan, aapko ek special *welcome coupon code* milega 🎁\n\n"
                "Ye coupon aapke course fee mein discount deta hai. Isko paane ke liye:\n"
                "1️⃣ Apna timing aur package choose karein\n"
                "2️⃣ Sensationz App download karein ya website https://shop.sensationzperformingarts.com/ visit karein\n"
                "3️⃣ Profile banayein\n"
                "4️⃣ Yahan *Done* ya *Yes* reply karein — coupon turant bhej diya jayega!"
            )


        # Build followup_separate from enrollment flow state.
        # IMPORTANT: If the discount_reply already has the app download instructions
        # (i.e. stage was READY_FOR_APP_LINK / APP_LINK_SENT), do NOT also call
        # get_flow_followup() — it returns the same app-link content and causes a duplicate message.
        if current_stage in ["APP_LINK_SENT", "READY_FOR_APP_LINK"]:
            followup_separate = None
        else:
            followup_separate = get_flow_followup(state)
            if followup_separate:
                followup_separate = followup_separate.strip()

        arm_followup_timer(state, topic=text)
        await save_user_state_async(phone, state)

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
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", combined, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", combined))
        return

    if not is_q and not is_info_intent(text) and state["stage"] == "READY_FOR_APP_LINK":
        package = state.get("package") or "3 Months"
        fee = state.get("fee") or "₹1,750 (Offer Price: ₹600)"
        reply = (
            f"You've selected the {package} package for {fee}.\n\n"
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
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging
        asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", reply))
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
            # Stage B: User already completed profile / unlocked coupon -> Always provide the actual code YOGA600
            hindi_markers = ["kya", "hai", "bhejo", "batao", "do", "kaha", "kha", "dobara", "phir", "fhrse", "mujhe"]
            has_hindi = any(w in text_lower for w in hindi_markers) or any("\u0900" <= ch <= "\u097F" for ch in text)
            if has_hindi:
                reply = (
                    "Aapka welcome discount coupon code ye raha 🎁\n\n"
                    "✨ Coupon Code: *YOGA600*\n\n"
                    "Isko Sensationz App mein checkout par enter karke apply karein. Class mein milte hain! 🧘‍♀️"
                )
            else:
                reply = (
                    "Here is your welcome discount coupon code 🎁\n\n"
                    "✨ Coupon Code: *YOGA600*\n\n"
                    "Please enter this code during checkout in the Sensationz App to activate your discount. See you in class! 🧘‍♀️"
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
            # Stage A: User has NOT completed profile yet -> Explain unlock requirement, do NOT leak code
            if state["stage"] in ["READY_FOR_APP_LINK", "APP_LINK_SENT"]:
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
            else:
                reply = (
                    "Aapka special welcome discount coupon code app mein profile banane ke baad unlock hota hai 🎁\n\n"
                    "Isko paane ke liye apna timing aur package choose karein, app download karke profile banayein, aur yahan *Done* reply karein!"
                )
            arm_followup_timer(state, topic=text)
            await save_user_state_async(phone, state)
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            # Fire-and-forget background logging
            asyncio.create_task(save_message_async(phone, "assistant", reply, response_time_sec=latency_sec))
            asyncio.create_task(log_message_async(phone, "ai", reply))
            return

    # ── 2. Primary Coupon Delivery on Profile Confirmation ───────────────────
    _COUPON_REQUEST_KWS = ["profile created", "profile done", "profile completed", "app downloaded", "app installed", "installed", "downloaded"]
    _is_coupon_request = matches_any(text, _COUPON_REQUEST_KWS)
    _should_send_coupon = (
        (state["stage"] == "PROFILE_COMPLETED" and not state.get("coupon_sent"))
        or (_is_coupon_request and state.get("profile_created") and not state.get("coupon_sent"))
    )

    if _should_send_coupon:
        reply = (
            "🎉 Welcome to the Sensationz family! 🌸\n"
            "Your app setup and profile are complete.\n\n"
            "🎁 Your welcome coupon code is: *YOGA600*\n\n"
            "Use this coupon in the app to activate your discount. See you in class! 🧘‍♀️✨"
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
        print(f"[TIMING] {phone} interakt_send (2-msg + sales_q): {time.perf_counter() - t_send:.2f}s")
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        # Fire-and-forget background logging to Supabase and CSV without blocking user reply
        asyncio.create_task(save_message_async(phone, "assistant", combined, response_time_sec=latency_sec))
        asyncio.create_task(log_message_async(phone, "ai", combined, sources=rag_sources, retrieval_query=rag_retrieval_query))
    else:
        await send_text_message_async(phone, full_reply)
        print(f"[TIMING] {phone} interakt_send: {time.perf_counter() - t_send:.2f}s")
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
        state = await get_user_state_async(phone)
        if not state.get("is_target_ad"):
            state["is_target_ad"] = True
            await save_user_state_async(phone, state)
    except Exception as e:
        print(f"[tasks] {phone}: Failed to save target flag: {e}")
        state = {}
    reset_follow_up_timer(state)
    await save_user_state_async(phone, state)

    # 3. Fetch history
    t_hist = time.perf_counter()
    try:
        history = await get_recent_history_async(phone)
    except Exception as e:
        print(f"[tasks] {phone}: History fetch failed: {e}")
        history = []
    print(f"[TIMING] {phone} history_fetch: {time.perf_counter() - t_hist:.2f}s")

    # 4. Save incoming message (fire-and-forget in background so it doesn't add latency to processing)
    try:
        asyncio.create_task(save_message_async(phone, "user", text))
        asyncio.create_task(log_message_async(phone, "user", text))
    except Exception as e:
        print(f"[tasks] {phone}: Failed to schedule message save: {e}")

    # 5. Check escalation
    if await is_escalated_async(phone):
        print(f"[tasks] {phone}: Already escalated — bot staying out.")
        return

    # 6. Round-robin agent assignment (async, non-blocking)
    t_assign = time.perf_counter()
    if PRIORITY_AGENT_EMAIL and not state.get("already_assigned"):
        success = await assign_chat_to_agent_async(phone, PRIORITY_AGENT_EMAIL)
        if success:
            state["already_assigned"] = True
            await save_user_state_async(phone, state)
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


