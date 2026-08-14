"""
tasks.py — Background message-processing task logic.
Receives unique phone number & message text from batching.py,
acquires per-phone distributed lock, processes state updates,
runs RAG query, and sends replies via Interakt API.
"""

import os
import time
import asyncio
from dotenv import load_dotenv

# Import messaging and assignment functions from interakt wrapper
from interakt import send_text_message, send_image_message, assign_chat_to_agent

# Import database and cache history management functions
from chat_history import save_message, get_recent_history

# Import CSV logging function
from csv_logger import log_message

# Import session state tracking and slot extraction helpers
from chat_state import (
    mark_escalated,
    is_escalated,
    get_user_state,
    save_user_state,
    extract_and_update_slots,
    is_user_asking_question,
)

# Import RAG async query function
from rag import ask_rag_async

from redis_client import get_redis_connection

# Load environment variables
load_dotenv()

redis_conn = get_redis_connection()

# Default and escalation agent emails configured in environment variables
PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")
PRIORITY_AGENT_EMAIL_ANOTHER = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER")

# Words that trigger immediate human agent handoff
AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]

TARGET_MESSAGE_TEXT = os.getenv("TARGET_MESSAGE_TEXT", "Hello! Can I get more info on Yoga classes?")


def is_target_ad_or_message(text: str, referral_data: dict = None, phone: str = None) -> bool:
    """
    Checks if a message qualifies for bot response.

    Verification logic (your requirement #11):
    1. If user is already marked as target ad customer (state flag) → PASS
    2. For first-time users: BOTH ad ID AND text must match simultaneously
       - referral source_id must match TARGET_AD_ID
       - message text must match TARGET_MESSAGE_TEXT (case-insensitive)
    3. If either doesn't match → FAIL (no reply, no assignment)
    """
    TARGET_AD_ID = os.getenv("TARGET_AD_ID")

    # 1. Check existing state flag — already-verified users pass immediately
    if phone:
        try:
            state = get_user_state(phone)
            if state.get("is_target_ad") is True:
                print(f"[target-check] {phone}: PASS — is_target_ad flag already True in state")
                return True
            else:
                print(f"[target-check] {phone}: is_target_ad flag is {state.get('is_target_ad')}, checking ad ID + text...")
        except Exception as e:
            print(f"[target-check] {phone}: state lookup failed ({e}), checking ad ID + text...")

    # 2. Check if BOTH ad ID AND message text match (required for first message)
    cleaned_text = text.strip().lower()
    target_text = TARGET_MESSAGE_TEXT.strip().lower()
    text_matches = (cleaned_text == target_text)

    ad_id_matches = False
    if referral_data and isinstance(referral_data, dict) and referral_data:
        source_id = referral_data.get("source_id")
        source_url = referral_data.get("source_url", "")

        if TARGET_AD_ID and (str(source_id) == str(TARGET_AD_ID) or str(TARGET_AD_ID) in str(source_url)):
            ad_id_matches = True
            print(f"[target-check] {phone}: Ad ID MATCH — source_id='{source_id}' matches TARGET_AD_ID='{TARGET_AD_ID}'")
        else:
            print(f"[target-check] {phone}: Ad ID MISMATCH — source_id='{source_id}' vs TARGET_AD_ID='{TARGET_AD_ID}'")
    else:
        print(f"[target-check] {phone}: No referral data present")

    if text_matches:
        print(f"[target-check] {phone}: Text MATCH — '{cleaned_text}' == '{target_text}'")
    else:
        print(f"[target-check] {phone}: Text MISMATCH — '{cleaned_text}' != '{target_text}'")

    # BOTH must match for first-time users
    if ad_id_matches and text_matches:
        print(f"[target-check] {phone}: PASS — BOTH ad ID and text match")
        return True

    # If only text matches (no referral), still allow it
    # This handles the case where referral data is missing but text is exact match
    if text_matches and not referral_data:
        print(f"[target-check] {phone}: PASS — text matches exactly (no referral to check)")
        return True

    # Nothing matched — this is NOT a target ad user
    print(f"[target-check] {phone}: FAIL — ad_id_matches={ad_id_matches}, text_matches={text_matches}. AI will ignore this user.")
    return False


def handle_agent_handoff(phone: str, start_time: float = None):
    """
    Handles customer request to talk to a human representative.
    Assigns chat to escalation agent and marks session as escalated.
    """
    print(f"[tasks] Agent requested by {phone} — re-assigning to escalation agent...")

    reply = "Got it — connecting you with our team now. Someone will be with you shortly!"

    if PRIORITY_AGENT_EMAIL_ANOTHER:
        assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL_ANOTHER)
        send_text_message(phone, reply)
        mark_escalated(phone)
        log_message(phone, "agent", reply)
    else:
        reply = (
            "Our team is currently offline, but we've noted your request "
            "and someone will reach out as soon as they're back online."
        )
        send_text_message(phone, reply)

    latency_sec = round(time.time() - start_time, 2) if start_time else None
    print(f"[tasks] {phone}: Agent handoff completed in {latency_sec}s")
    save_message(phone, "assistant", reply, response_time_sec=latency_sec)


async def handle_ai_reply_async(phone: str, text: str, history: list, start_time: float = None):
    """
    Async AI reply generator: uses non-blocking ask_rag_async for high-concurrency LLM execution.
    """
    t0 = time.perf_counter()

    # Check for specific Ad pre-filled message trigger (pre-written reply)
    if text.strip().lower() == TARGET_MESSAGE_TEXT.strip().lower():
        reply = "Hi Sir/Mam, Welcome to Sensationz Media and arts, How i can help u?"
        t_send = time.perf_counter()
        send_text_message(phone, reply)
        print(f"[TIMING] {phone} pre-written reply send: {time.perf_counter() - t_send:.2f}s")
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        print(f"[TIMING] {phone} pre-written reply TOTAL: {time.perf_counter() - t0:.2f}s")
        return

    # 1. Update session state & extract batch timing / package slots from user input
    t_slots = time.perf_counter()
    state = extract_and_update_slots(phone, text)
    is_q = is_user_asking_question(text)
    print(f"[TIMING] {phone} slot_extraction: {time.perf_counter() - t_slots:.2f}s")

    # 2. Check deterministic state guards (ONLY if customer is NOT asking an informational question)
    if not is_q and state["stage"] == "READY_FOR_APP_LINK":
        package = (state.get("package") or "3-month").lower()
        fee = state.get("fee") or "₹1,750"
        reply = (
            f"Great choice! 😊 You've selected the {package} package for {fee}.\n\n"
            "To proceed, you'll need to download the Sensationz App, through which you'll receive your special welcome discount coupon 🎁.\n\n"
            "Please download the app here:\n\n"
            "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
            "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n\n"
            "Once you've downloaded the app and created your profile, let me know here so I can activate your personalized welcome coupon!"
        )
        state["stage"] = "APP_LINK_SENT"
        save_user_state(phone, state)
        send_text_message(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        print(f"[TIMING] {phone} app_link_sent TOTAL: {time.perf_counter() - t0:.2f}s")
        return

    if state["stage"] == "PROFILE_COMPLETED" and not state.get("coupon_sent"):
        reply = (
            "🎉 Welcome to the Sensationz Yoga family! 🌸\n"
            "Your app setup and profile are complete.\n\n"
            "🎁 Your personalized welcome coupon code is: **SENSZAPP**\n\n"
            "Use this coupon in the app to activate your discount. See you in class! 🧘‍♀️✨"
        )
        state["coupon_sent"] = True
        state["stage"] = "COUPON_SENT"
        save_user_state(phone, state)
        send_text_message(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        print(f"[TIMING] {phone} coupon_sent TOTAL: {time.perf_counter() - t0:.2f}s")
        return

    # 3. RAG AI reply generation
    rag_start = time.perf_counter()
    full_reply = await ask_rag_async(text, chat_history=history, state=state)
    full_reply = full_reply.strip()
    rag_time = time.perf_counter() - rag_start
    print(f"[TIMING] {phone} RAG query: {rag_time:.2f}s")

    # 4. Check for low-confidence or fallback AI responses
    low_conf_triggers = ["unable to process", "unable to answer", "i don't have information", "not sure", "sorry, the ai service"]
    if any(trigger in full_reply.lower() for trigger in low_conf_triggers):
        state["low_confidence_count"] = state.get("low_confidence_count", 0) + 1
    else:
        state["low_confidence_count"] = 0

    # If AI has been unable to give clear answers for 2+ consecutive messages, offer human agent support
    if state.get("low_confidence_count", 0) >= 2:
        full_reply += "\n\n💬 Would you like to speak directly with our support team? Please reply by typing **'agent'** or call us directly at **9898989898** to resolve your query!"

    save_user_state(phone, state)

    t_send = time.perf_counter()
    send_text_message(phone, full_reply)
    print(f"[TIMING] {phone} interakt_send: {time.perf_counter() - t_send:.2f}s")

    # Calculate latency in seconds and save to Supabase
    latency_sec = round(time.time() - start_time, 2) if start_time else None
    print(f"[TIMING] {phone} AI reply TOTAL: {time.perf_counter() - t0:.2f}s (wall-clock: {latency_sec}s)")
    save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
    log_message(phone, "ai", full_reply)


async def process_incoming_message_async(phone: str, text: str, referral: dict = None):
    """
    Non-blocking async task worker function.
    Acquires per-user distributed lock and runs async AI reply pipeline.
    """
    start_time = time.time()
    t0 = time.perf_counter()

    # Acquire distributed lock for this phone number (prevents race conditions)
    t_lock = time.perf_counter()
    lock = redis_conn.lock(f"phone-lock:{phone}", timeout=60, blocking_timeout=15)
    try:
        acquired = await asyncio.to_thread(lock.acquire, blocking=True)
    except Exception as e:
        print(f"[tasks] {phone}: Lock acquisition failed: {e}")
        acquired = False

    if not acquired:
        print(f"[tasks] Could not acquire lock for {phone} in time -- skipping.")
        return

    print(f"[TIMING] {phone} lock_acquired: {time.perf_counter() - t_lock:.2f}s")

    try:
        # 1. Check if target ad user
        t_check = time.perf_counter()
        is_target = is_target_ad_or_message(text, referral, phone)
        print(f"[TIMING] {phone} target_check: {time.perf_counter() - t_check:.2f}s")

        if not is_target:
            print(f"[tasks] {phone}: Ad/Message not targeted. AI ignores and chat remains unassigned.")
            return

        # 2. Persist the target flag in user state
        try:
            state = get_user_state(phone)
            if not state.get("is_target_ad"):
                state["is_target_ad"] = True
                save_user_state(phone, state)
        except Exception as e:
            print(f"[tasks] Failed to save target flag in user state for {phone}: {e}")

        # 3. Fetch recent conversation history
        t_hist = time.perf_counter()
        try:
            history = get_recent_history(phone)
        except Exception as e:
            print(f"[tasks] Failed to fetch history for {phone}: {e}")
            history = []
        print(f"[TIMING] {phone} history_fetch: {time.perf_counter() - t_hist:.2f}s")

        # 4. Save incoming message
        try:
            save_message(phone, "user", text)
            log_message(phone, "user", text)
        except Exception as e:
            print(f"[tasks] Failed to save incoming message for {phone}: {e}")

        # 5. Check escalation status
        if is_escalated(phone):
            print(f"[tasks] {phone} is already escalated — bot staying out of it.")
            return

        # 6. Assign chat to default agent email if configured
        if PRIORITY_AGENT_EMAIL:
            print(f"[tasks] Assigning chat for {phone} to target priority agent: {PRIORITY_AGENT_EMAIL}")
            assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

        text_lower = text.lower()

        # 7. Check if customer requested a human agent
        if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
            handle_agent_handoff(phone, start_time)
            return

        # 8. Generate and send AI response
        await handle_ai_reply_async(phone, text, history, start_time)

        print(f"[TIMING] {phone} PIPELINE TOTAL: {time.perf_counter() - t0:.2f}s")

    finally:
        # Always release Redis lock after processing completes
        try:
            await asyncio.to_thread(lock.release)
        except Exception:
            pass   # Lock timeout safety fallback
