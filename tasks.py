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

from interakt import (
    send_text_message_async,
    assign_chat_to_agent_async,
)
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
from rag import ask_rag_async
from redis_client import get_redis_connection
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
TARGET_MESSAGE_TEXT = os.getenv("TARGET_MESSAGE_TEXT", "Hello! Can I get more info on Yoga classes?")

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
    reply = "Got it — connecting you with our team now. Someone will be with you shortly!"

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

def should_skip_followup(full_reply: str, stage: str) -> bool:
    reply_lower = full_reply.lower()
    if stage in ["ENROLL_CONFIRMED", "PACKAGE_SELECTED"]:
        timing_kws = ["timing", "batch", "time slot", "schedule", "time", "samay", "kab"]
        if any(kw in reply_lower for kw in timing_kws):
            return True
    elif stage in ["TIMING_SELECTED", "PACKAGE_ASKED"]:
        pkg_kws = ["package", "duration", "month", "months", "year", "fee", "fees", "price", "cost", "mahina"]
        if any(kw in reply_lower for kw in pkg_kws):
            return True
    elif stage in ["READY_FOR_APP_LINK", "APP_LINK_SENT"]:
        app_kws = ["download", "install", "play store", "app store", "android", "ios", "app link", "profile"]
        if any(kw in reply_lower for kw in app_kws) or "http" in reply_lower:
            return True
    return False

AGENT_SUGGEST_PATTERN = re.compile(
    r"to know more about this,?\s*you can type\s*\*?agent\*?\s*so our support team can assist you shortly\.?",
    re.IGNORECASE
)

def get_flow_followup(state: dict) -> str:
    # 1. If enrollment completed (coupon sent or profile completed)
    if state.get("stage") in ["PROFILE_COMPLETED", "COUPON_SENT"] or state.get("coupon_sent"):
        return None
        
    # 2. If app links are sent or ready for app link
    if state.get("stage") in ["READY_FOR_APP_LINK", "APP_LINK_SENT"]:
        return (
            "\n\nWonderful! 😊 To proceed, you'll need to download the Sensationz App, through which you'll receive your special welcome discount coupon 🎁.\n\n"
            "Please download the app here:\n\n"
            "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
            "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n\n"
            "Once you've downloaded the app and created your profile, let me know here so I can activate your welcome coupon!"
        )
        
    # 3. If timing is selected but package is missing
    if state.get("timing") and not state.get("package"):
        return (
            "\n\nWonderful! 😊 Which package duration would you like to start with?\n"
            "1 Month — ₹700 | 3 Months — ₹1,750 | 6 Months — ₹3,200 | 1 Year — ₹5,000"
        )
        
    # 4. If timing is missing
    if not state.get("timing"):
        if state.get("stage") == "NEW":
            return None
        return (
            "\n\nWonderful! 😊 Which timing would you prefer for your classes?\n"
            "Morning: 6:00–7:00 AM, 7:00–8:00 AM, 8:00–9:00 AM, 10:00–11:00 AM\n"
            "Afternoon: 12:00–1:00 PM\n"
            "Evening: 4:00–5:00 PM, 5:00–6:00 PM, 6:00–7:00 PM, 7:00–8:00 PM"
        )
    return None


async def handle_ai_reply_async(phone: str, text: str, history: list, start_time: float = None):
    t0 = time.perf_counter()

    if text.strip().lower() == TARGET_MESSAGE_TEXT.strip().lower():
        reply = "Hi Sir/Mam, Welcome to Sensationz Media and arts, How i can help u?"
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    # Fetch previous state stage before slot extraction
    try:
        pre_state = get_user_state(phone)
        prev_stage = pre_state.get("stage") or "NEW"
    except Exception:
        prev_stage = "NEW"

    t_slots = time.perf_counter()
    state = await extract_and_update_slots(phone, text)
    is_q = is_user_asking_question(text)
    print(f"[TIMING] {phone} slot_extraction: {time.perf_counter() - t_slots:.2f}s")

    # Define flags for fresh transitions and confirmations/greetings
    text_lower = text.lower().strip()
    GREETING_WORDS = ["hi", "hii", "hello", "hey", "namaste", "good morning", "good evening", "good afternoon"]
    CONFIRMATION_WORDS = [
        "yes", "yeah", "yep", "sure", "ok", "okay", "enroll", "join",
        "interested", "i want to join", "haan", "han",
        "karna hai", "kar do", "haan ji", "proceed", "done", "thik", "thik hai"
    ]
    is_greeting = matches_any(text_lower, GREETING_WORDS)
    is_confirmation = matches_any(text_lower, CONFIRMATION_WORDS)

    is_fresh_enroll_confirmed = (prev_stage != "ENROLL_CONFIRMED" and state["stage"] == "ENROLL_CONFIRMED")
    is_fresh_package_asked = (prev_stage != "PACKAGE_ASKED" and state["stage"] == "PACKAGE_ASKED")
    is_fresh_timing_selected = (prev_stage != "TIMING_SELECTED" and state["stage"] == "TIMING_SELECTED")
    is_fresh_package_selected = (prev_stage != "PACKAGE_SELECTED" and state["stage"] == "PACKAGE_SELECTED")

    # --- DETERMINISTIC STAGE GUARDS ---
    if not is_q and not is_info_intent(text) and state["stage"] == "ENROLL_CONFIRMED" and (is_fresh_enroll_confirmed or is_confirmation or is_greeting):
        reply = get_flow_followup(state)
        if reply:
            reply = reply.strip()
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            save_message(phone, "assistant", reply, response_time_sec=latency_sec)
            log_message(phone, "ai", reply)
            return

    if not is_q and not is_info_intent(text) and state["stage"] == "PACKAGE_ASKED" and (is_fresh_package_asked or is_confirmation or is_greeting):
        reply = get_flow_followup(state)
        if reply:
            reply = reply.strip()
            await send_text_message_async(phone, reply)
            latency_sec = round(time.time() - start_time, 2) if start_time else None
            save_message(phone, "assistant", reply, response_time_sec=latency_sec)
            log_message(phone, "ai", reply)
            return

    if not is_q and not is_info_intent(text) and state["stage"] == "TIMING_SELECTED" and not state.get("package") and (is_fresh_timing_selected or is_confirmation or is_greeting):
        reply = (
            f"Wonderful! 😊 you've selected the {state.get('timing')} batch.\n\n"
            "Would you like to go ahead and pick a package duration too? 😊\n"
            "1 Month — ₹700 | 3 Months — ₹1,750 | 6 Months — ₹3,200 | 1 Year — ₹5,000"
        )
        state["stage"] = advance_stage(state["stage"], "PACKAGE_ASKED")
        save_user_state(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    if not is_q and not is_info_intent(text) and state["stage"] == "PACKAGE_SELECTED" and not state.get("timing") and (is_fresh_package_selected or is_confirmation or is_greeting):
        reply = (
            f"Wonderful! 😊 you've selected the {state.get('package')} package.\n\n"
            "Which timing would you prefer for your classes? 😊\n"
            "Morning: 6:00–7:00 AM, 7:00–8:00 AM, 8:00–9:00 AM, 10:00–11:00 AM\n"
            "Afternoon: 12:00–1:00 PM\n"
            "Evening: 4:00–5:00 PM, 5:00–6:00 PM, 6:00–7:00 PM, 7:00–8:00 PM"
        )
        state["stage"] = advance_stage(state["stage"], "ENROLL_CONFIRMED")
        save_user_state(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return


    if not is_q and not is_info_intent(text) and state["stage"] == "READY_FOR_APP_LINK":
        package = state.get("package") or "3 Months"
        fee = state.get("fee") or "₹1,750"
        reply = (
            f"Wonderful! 😊 you've selected the {package} package for {fee}.\n\n"
            "To proceed, you'll need to download the Sensationz App, through which you'll receive your special welcome discount coupon 🎁.\n\n"
            "Please download the app here:\n\n"
            "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
            "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n\n"
            "Once you've downloaded the app and created your profile, let me know here so I can activate your welcome coupon!"
        )
        state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")
        save_user_state(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    if state["stage"] == "PROFILE_COMPLETED" and not is_info_intent(text) and not state.get("coupon_sent"):
        reply = (
            "🎉 Welcome to the Sensationz Yoga family! 🌸\n"
            "Your app setup and profile are complete.\n\n"
            "🎁 Your personalized welcome coupon code is: **SENSZAPP**\n\n"
            "Use this coupon in the app to activate your discount. See you in class! 🧘‍♀️✨"
        )
        state["coupon_sent"] = True
        state["stage"] = advance_stage(state["stage"], "COUPON_SENT")
        save_user_state(phone, state)
        await send_text_message_async(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    # --- Genuine question / off-flow topic — goes to RAG ---
    t_rag = time.perf_counter()
    full_reply = await ask_rag_async(text, chat_history=history, state=state)
    full_reply = full_reply.strip()
    print(f"[TIMING] {phone} rag_query: {time.perf_counter() - t_rag:.2f}s")

    # Strip any "type agent" line the LLM generated, count it silently,
    # only resurface after 2 CONSECUTIVE flagged replies.
    flagged_this_turn = bool(AGENT_SUGGEST_PATTERN.search(full_reply))
    full_reply = AGENT_SUGGEST_PATTERN.sub("", full_reply).strip()

    if flagged_this_turn:
        state["low_confidence_count"] = state.get("low_confidence_count", 0) + 1
    else:
        state["low_confidence_count"] = 0

    if state.get("low_confidence_count", 0) >= 2:
        full_reply += "\n\n💬 Would you like to speak directly with our support team? Please reply by typing **'agent'** or call us directly at **9898989898** to resolve your query!"
        state["low_confidence_count"] = 0  # reset after nudging, don't nag every message after
    else:
        # Always steer back to the flow if enrollment isn't complete yet. Deterministic, not LLM.
        followup = get_flow_followup(state)
        if followup and not should_skip_followup(full_reply, state.get("stage")):
            full_reply += followup

    # Post-LLM State Transitions
    if state.get("stage") == "READY_FOR_APP_LINK":
        state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")

    save_user_state(phone, state)

    t_send = time.perf_counter()
    await send_text_message_async(phone, full_reply)
    print(f"[TIMING] {phone} interakt_send: {time.perf_counter() - t_send:.2f}s")

    latency_sec = round(time.time() - start_time, 2) if start_time else None
    save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
    log_message(phone, "ai", full_reply)
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
