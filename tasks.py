"""
tasks.py — Background message-processing task logic run by Redis RQ worker processes (worker.py).
Receives unique phone number & message text from main.py queue, acquires per-phone distributed lock,
processes state updates, runs RAG query, and sends replies via Interakt API.
"""

import os
from dotenv import load_dotenv
from redis import Redis
from upstash_redis import Redis
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

# Import RAG streaming and async query functions
from rag import stream_rag, ask_rag_async

from redis_client import get_redis_connection
import time
# Load environment variables
load_dotenv()

redis_conn = get_redis_connection()
# Default and escalation agent emails configured in environment variables
PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")
PRIORITY_AGENT_EMAIL_ANOTHER = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER")

# Words that trigger immediate human agent handoff
AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]

# TARGET_CAMPAIGN_ID = os.getenv("TARGET_CAMPAIGN_ID")
# TARGET_AD_ID = os.getenv("TARGET_AD_ID")
TARGET_MESSAGE_TEXT = os.getenv("TARGET_MESSAGE_TEXT", "123456")

import time
def is_target_ad_or_message(text: str, referral_data: dict = None, phone: str = None) -> bool:
    """Checks if message string matches, if incoming referral contains targeted Ad/Campaign IDs, or if user is already marked as target ad customer."""
    TARGET_AD_ID = os.getenv("TARGET_AD_ID")

    # 1. Check existing state flag — ONLY the explicit is_target_ad flag
    #    DO NOT check stage here. A user whose stage advanced (e.g. "ENROLL_ASKED")
    #    from an organic message is NOT a target ad user.
    if phone:
        try:
            state = get_user_state(phone)
            if state.get("is_target_ad") is True:
                print(f"[target-check] {phone}: PASS — is_target_ad flag already True in state")
                return True
            else:
                print(f"[target-check] {phone}: is_target_ad flag is {state.get('is_target_ad')}, checking message/referral...")
        except Exception as e:
            print(f"[target-check] {phone}: state lookup failed ({e}), checking message/referral...")

    # 2. Exact string match against the pre-filled ad message
    cleaned_text = text.strip().lower()
    if cleaned_text == TARGET_MESSAGE_TEXT:
        print(f"[target-check] {phone}: PASS — text exactly matches TARGET_MESSAGE_TEXT")
        return True
    else:
        print(f"[target-check] {phone}: text '{cleaned_text}' does NOT match target '{TARGET_MESSAGE_TEXT}'")

    # 3. Check Meta Referral payload data (if passed)
    if referral_data and isinstance(referral_data, dict) and referral_data:
        source_id = referral_data.get("source_id")
        source_url = referral_data.get("source_url", "")

        if TARGET_AD_ID and (str(source_id) == str(TARGET_AD_ID) or str(TARGET_AD_ID) in str(source_url)):
            print(f"[target-check] {phone}: PASS — referral source_id/source_url matches TARGET_AD_ID '{TARGET_AD_ID}'")
            return True
        else:
            print(f"[target-check] {phone}: referral present but source_id='{source_id}' does not match TARGET_AD_ID='{TARGET_AD_ID}'")

    # 4. Nothing matched — this is NOT a target ad user
    print(f"[target-check] {phone}: FAIL — no match. AI will ignore this user.")
    return False


def process_incoming_message(phone: str, text: str, referral: dict = None):
    """
    Background worker job: pulled off the interakt_messages Redis queue by worker processes.
    Acquires a Redis lock for this specific customer's phone number so out-of-order execution
    cannot corrupt conversation history. Each unique phone number gets independent execution.
    """
    start_time = time.time()  # Start latency timer
    # Create Redis lock keyed to customer's unique phone number
    lock = redis_conn.lock(f"phone-lock:{phone}", timeout=60, blocking_timeout=15)
    # Acquire distributed lock
    acquired = lock.acquire(blocking=True)
    
    if not acquired:
        print(f"[tasks] Could not acquire lock for {phone} in time -- skipping this job.")
        return

    try:
        # Determine if target ad or message matched
        is_target = is_target_ad_or_message(text, referral, phone)

        if not is_target:
            print(f"[tasks] {phone}: Ad/Message not targeted. AI ignores and chat remains unassigned.")
            return

        # Persist target flag in user state
        try:
            state = get_user_state(phone)
            if not state.get("is_target_ad"):
                state["is_target_ad"] = True
                save_user_state(phone, state)
        except Exception as e:
            print(f"[tasks] Failed to save target flag in user state for {phone}: {e}")

        # 1. Fetch recent conversation history for this specific phone number
        try:
            history = get_recent_history(phone)
        except Exception as e:
            print(f"[tasks] Failed to fetch history for {phone}: {e}")
            history = []

        # 2. Save incoming message to Supabase chat_history table and log file
        try:
            save_message(phone, "user", text)
            log_message(phone, "user", text)
        except Exception as e:
            print(f"[tasks] Failed to save incoming message for {phone}: {e}")

        # 3. If chat is escalated to a human agent, stop AI bot processing
        if is_escalated(phone):
            print(f"{phone} is already escalated — bot staying out of it.")
            return

        # 4. Assign chat to default agent email if configured
        if PRIORITY_AGENT_EMAIL:
            print(f"[tasks] Assigning chat for {phone} to target priority agent: {PRIORITY_AGENT_EMAIL}")
            assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

        text_lower = text.lower()

        # 5. Check if customer requested a human agent
        if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
            handle_agent_handoff(phone, start_time)
            return

        # 6. Generate and send AI response using session state & RAG knowledge
        handle_ai_reply(phone, text, history, start_time)

    finally:
        # Always release Redis lock after processing completes
        try:
            lock.release()
        except Exception:
            pass   # Lock timeout safety fallback



def handle_agent_handoff(phone: str, start_time: float = None):
    """
    Handles customer request to talk to a human representative.
    Assigns chat to escalation agent and marks session as escalated.
    """
    print(f"Agent requested by {phone} — re-assigning to escalation agent...")

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
    save_message(phone, "assistant", reply, response_time_sec=latency_sec)


def handle_ai_reply(phone: str, text: str, history: list, start_time: float = None):
    """
    Generates AI response using session state slots, state guards, and RAG knowledge base.
    """
    # Check for specific Ad pre-filled message trigger
    if "hello! can i get more info on yoga classes?" in text.strip().lower():
        reply = "Thanks this is our offer for our yoga classes"
        send_text_message(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    # 1. Update session state & extract batch timing / package slots from user input
    state = extract_and_update_slots(phone, text)
    is_q = is_user_asking_question(text)

    # 2. Check deterministic state guards (ONLY if customer is NOT asking an informational question/video request)
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
        return

    # 3. Prompt generation with active state context & RAG knowledge retrieval
    full_answer_parts = []
    for chunk_text in stream_rag(text, chat_history=history, state=state):
        # full_answer_parts.append(str(chunk_text))
        full_answer_parts.append(str(chunk_text))

    full_reply = "".join(full_answer_parts).strip()

    # 4. Check for low-confidence or fallback AI responses across 2-3 consecutive queries
    low_conf_triggers = ["unable to process", "unable to answer", "i don't have information", "not sure", "sorry, the ai service"]
    if any(trigger in full_reply.lower() for trigger in low_conf_triggers):
        state["low_confidence_count"] = state.get("low_confidence_count", 0) + 1
    else:
        state["low_confidence_count"] = 0

    # If AI has been unable to give clear answers for 2 or more consecutive messages, offer human agent support
    if state.get("low_confidence_count", 0) >= 2:
        full_reply += "\n\n💬 Would you like to speak directly with our support team? Please reply by typing **'agent'** or call us directly at **9898989898** to resolve your query!"

    save_user_state(phone, state)
    send_text_message(phone, full_reply)

    # Calculate latency in seconds and save to Supabase
    latency_sec = round(time.time() - start_time, 2) if start_time else None
    print(f"[tasks] {phone}: AI reply generated & sent in {latency_sec}s")
    save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
    log_message(phone, "ai", full_reply)

async def handle_ai_reply_async(phone: str, text: str, history: list, start_time: float = None):
    """
    Async AI reply generator: uses non-blocking ask_rag_async for high-concurrency LLM execution.
    """
    # Check for specific Ad pre-filled message trigger
    if text.strip().lower() == "hello! can i get more info on yoga classes?":
        reply = "Thanks this is our offer for our yoga classes"
        send_text_message(phone, reply)
        latency_sec = round(time.time() - start_time, 2) if start_time else None
        save_message(phone, "assistant", reply, response_time_sec=latency_sec)
        log_message(phone, "ai", reply)
        return

    state = extract_and_update_slots(phone, text)
    is_q = is_user_asking_question(text)

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
        return


    rag_start = time.perf_counter()
    full_reply = await ask_rag_async(text, chat_history=history, state=state)
    full_reply = full_reply.strip()
    rag_time = time.perf_counter() - rag_start
    print(f"[TIMING] {phone} RAG: {rag_time:.2f}s")

    low_conf_triggers = ["unable to process", "unable to answer", "i don't have information", "not sure", "sorry, the ai service"]
    if any(trigger in full_reply.lower() for trigger in low_conf_triggers):
        state["low_confidence_count"] = state.get("low_confidence_count", 0) + 1
    else:
        state["low_confidence_count"] = 0

    if state.get("low_confidence_count", 0) >= 2:
        full_reply += "\n\n💬 Would you like to speak directly with our support team? Please reply by typing **'agent'** or call us directly at **9898989898** to resolve your query!"
    save_user_state(phone, state)
    send_text_message(phone, full_reply)

    latency_sec = round(time.time() - start_time, 2) if start_time else None
    print(f"[tasks-async] {phone}: AI reply generated & sent in {latency_sec}s")
    save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
    log_message(phone, "ai", full_reply)


async def process_incoming_message_async(phone: str, text: str, referral: dict = None):
    """
    Non-blocking async task worker function.
    Acquires per-user distributed lock and runs async AI reply pipeline.
    """
    start_time = time.time()
    lock = redis_conn.lock(f"phone-lock:{phone}", timeout=60, blocking_timeout=15)

    acquired = lock.acquire(blocking=True)
    if not acquired:
        print(f"[tasks-async] Could not acquire lock for {phone} in time -- skipping.")
        return

    try:
        # Determine if target ad or message matched — pass phone so
        # it can check "was this person already confirmed as a target
        # ad customer" via their saved state, not just the current
        # message's referral (which only exists on the FIRST message
        # from an ad click — every message after that has none).
        is_target = is_target_ad_or_message(text, referral, phone)

        if not is_target:
            print(f"[tasks-async] {phone}: Ad/Message not targeted. AI ignores and chat remains unassigned.")
            return

        # Persist the target flag in user state — WITHOUT this, every
        # message after the first would fail the referral/text match
        # (since only the first message carries referral data) and
        # get silently ignored forever, exactly the bug you hit.
        try:
            state = get_user_state(phone)
            if not state.get("is_target_ad"):
                state["is_target_ad"] = True
                save_user_state(phone, state)
        except Exception as e:
            print(f"[tasks-async] Failed to save target flag in user state for {phone}: {e}")

        try:
            history = get_recent_history(phone)
        except Exception as e:
            print(f"[tasks-async] Failed to fetch history for {phone}: {e}")
            history = []

        try:
            save_message(phone, "user", text)
            log_message(phone, "user", text)
        except Exception as e:
            print(f"[tasks-async] Failed to save incoming message for {phone}: {e}")

        if is_escalated(phone):
            print(f"{phone} is already escalated — bot staying out of it.")
            return

        if PRIORITY_AGENT_EMAIL:
            print(f"[tasks-async] Assigning chat for {phone} to target priority agent: {PRIORITY_AGENT_EMAIL}")
            assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

        text_lower = text.lower()

        if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
            handle_agent_handoff(phone, start_time)
            return
        
        await handle_ai_reply_async(phone, text, history, start_time)

    finally:
        try:
            lock.release()
        except Exception:
            pass
        
# """
# tasks.py — Background message-processing task logic run by Redis RQ worker processes (worker.py).
# Receives unique phone number & message text from main.py queue, acquires per-phone distributed lock,
# processes state updates, runs RAG query, and sends replies via Interakt API.
# """

# import os
# from dotenv import load_dotenv
# from redis import Redis
# from upstash_redis import Redis
# # Import messaging and assignment functions from interakt wrapper
# from interakt import send_text_message, send_image_message, assign_chat_to_agent

# # Import database and cache history management functions
# from chat_history import save_message, get_recent_history

# # Import CSV logging function
# from csv_logger import log_message

# # Import session state tracking and slot extraction helpers
# from chat_state import (
#     mark_escalated,
#     is_escalated,
#     get_user_state,
#     save_user_state,
#     extract_and_update_slots,
#     is_user_asking_question,
# )

# # Import RAG streaming and async query functions
# from rag import stream_rag, ask_rag_async

# from redis_client import get_redis_connection
# import time
# # Load environment variables
# load_dotenv()

# redis_conn = get_redis_connection()
# # Default and escalation agent emails configured in environment variables
# PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")
# PRIORITY_AGENT_EMAIL_ANOTHER = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER")

# # Words that trigger immediate human agent handoff
# AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]

# TARGET_CAMPAIGN_ID = os.getenv("TARGET_CAMPAIGN_ID")
# TARGET_AD_ID = os.getenv("TARGET_AD_ID")
# TARGET_MESSAGE_TEXT = "hello! can i get more info on yoga classes?"

# import time
# def is_target_ad_or_message(text: str, referral_data: dict = None, phone: str = None) -> bool:
#     """Checks if message string matches, if incoming referral contains targeted Ad/Campaign IDs, or if user is already marked as target ad customer."""
#     # 1. Check existing state flag in state cache
#     if phone:
#         try:
#             state = get_user_state(phone)
#             if state.get("is_target_ad") or state.get("stage") != "NEW":
#                 return True
#         except Exception:
#             pass

#     # 2. String match check
#     if TARGET_MESSAGE_TEXT in text.strip().lower():
#         return True
    
#     # 3. Check Meta Referral payload data (if passed)
#     if referral_data:
#         source_id = referral_data.get("source_id")
#         source_url = referral_data.get("source_url", "")
        
#         if TARGET_AD_ID and (str(source_id) == str(TARGET_AD_ID) or str(TARGET_AD_ID) in str(source_url)):
#             return True

#     return False


# def process_incoming_message(phone: str, text: str, referral: dict = None):
#     """
#     Background worker job: pulled off the interakt_messages Redis queue by worker processes.
#     Acquires a Redis lock for this specific customer's phone number so out-of-order execution
#     cannot corrupt conversation history. Each unique phone number gets independent execution.
#     """
#     start_time = time.time()  # Start latency timer
#     # Create Redis lock keyed to customer's unique phone number
#     lock = redis_conn.lock(f"phone-lock:{phone}", timeout=60, blocking_timeout=15)
#     # Acquire distributed lock
#     acquired = lock.acquire(blocking=True)
    
#     if not acquired:
#         print(f"[tasks] Could not acquire lock for {phone} in time -- skipping this job.")
#         return

#     try:
#         # Determine if target ad or message matched
#         is_target = is_target_ad_or_message(text, referral, phone)

#         if not is_target:
#             print(f"[tasks] {phone}: Ad/Message not targeted. AI ignores and chat remains unassigned.")
#             return

#         # Persist target flag in user state
#         try:
#             state = get_user_state(phone)
#             if not state.get("is_target_ad"):
#                 state["is_target_ad"] = True
#                 save_user_state(phone, state)
#         except Exception as e:
#             print(f"[tasks] Failed to save target flag in user state for {phone}: {e}")

#         # 1. Fetch recent conversation history for this specific phone number
#         try:
#             history = get_recent_history(phone)
#         except Exception as e:
#             print(f"[tasks] Failed to fetch history for {phone}: {e}")
#             history = []

#         # 2. Save incoming message to Supabase chat_history table and log file
#         try:
#             save_message(phone, "user", text)
#             log_message(phone, "user", text)
#         except Exception as e:
#             print(f"[tasks] Failed to save incoming message for {phone}: {e}")

#         # 3. If chat is escalated to a human agent, stop AI bot processing
#         if is_escalated(phone):
#             print(f"{phone} is already escalated — bot staying out of it.")
#             return

#         # 4. Assign chat to default agent email if configured
#         if PRIORITY_AGENT_EMAIL:
#             print(f"[tasks] Assigning chat for {phone} to target priority agent: {PRIORITY_AGENT_EMAIL}")
#             assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

#         text_lower = text.lower()

#         # 5. Check if customer requested a human agent
#         if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
#             handle_agent_handoff(phone, start_time)
#             return

#         # 6. Generate and send AI response using session state & RAG knowledge
#         handle_ai_reply(phone, text, history, start_time)

#     finally:
#         # Always release Redis lock after processing completes
#         try:
#             lock.release()
#         except Exception:
#             pass   # Lock timeout safety fallback


# def handle_agent_handoff(phone: str, start_time: float = None):
#     """
#     Handles customer request to talk to a human representative.
#     Assigns chat to escalation agent and marks session as escalated.
#     """
#     print(f"Agent requested by {phone} — re-assigning to escalation agent...")

#     reply = "Got it — connecting you with our team now. Someone will be with you shortly!"

#     if PRIORITY_AGENT_EMAIL_ANOTHER:
#         assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL_ANOTHER)
#         send_text_message(phone, reply)
#         mark_escalated(phone)
#         log_message(phone, "agent", reply)
#     else:
#         reply = (
#             "Our team is currently offline, but we've noted your request "
#             "and someone will reach out as soon as they're back online."
#         )
#         send_text_message(phone, reply)

#     latency_sec = round(time.time() - start_time, 2) if start_time else None
#     save_message(phone, "assistant", reply, response_time_sec=latency_sec)


# def handle_ai_reply(phone: str, text: str, history: list, start_time: float = None):
#     """
#     Generates AI response using session state slots, state guards, and RAG knowledge base.
#     """
#     # Check for specific Ad pre-filled message trigger
#     if "hello! can i get more info on yoga classes?" in text.strip().lower():
#         reply = "Thanks this is our offer for our yoga classes"
#         send_text_message(phone, reply)
#         latency_sec = round(time.time() - start_time, 2) if start_time else None
#         save_message(phone, "assistant", reply, response_time_sec=latency_sec)
#         log_message(phone, "ai", reply)
#         return

#     # 1. Update session state & extract batch timing / package slots from user input
#     state = extract_and_update_slots(phone, text)
#     is_q = is_user_asking_question(text)

#     # 2. Check deterministic state guards (ONLY if customer is NOT asking an informational question/video request)
#     if not is_q and state["stage"] == "READY_FOR_APP_LINK":
#         package = (state.get("package") or "3-month").lower()
#         fee = state.get("fee") or "₹1,750"
#         reply = (
#             f"Great choice! 😊 You've selected the {package} package for {fee}.\n\n"
#             "To proceed, you'll need to download the Sensationz App, through which you'll receive your special welcome discount coupon 🎁.\n\n"
#             "Please download the app here:\n\n"
#             "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
#             "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n\n"
#             "Once you've downloaded the app and created your profile, let me know here so I can activate your personalized welcome coupon!"
#         )
#         state["stage"] = "APP_LINK_SENT"
#         save_user_state(phone, state)
#         send_text_message(phone, reply)
#         latency_sec = round(time.time() - start_time, 2) if start_time else None
#         save_message(phone, "assistant", reply, response_time_sec=latency_sec)
#         log_message(phone, "ai", reply)
#         return

#     if state["stage"] == "PROFILE_COMPLETED" and not state.get("coupon_sent"):
#         reply = (
#             "🎉 Welcome to the Sensationz Yoga family! 🌸\n"
#             "Your app setup and profile are complete.\n\n"
#             "🎁 Your personalized welcome coupon code is: **SENSZAPP**\n\n"
#             "Use this coupon in the app to activate your discount. See you in class! 🧘‍♀️✨"
#         )
#         state["coupon_sent"] = True
#         state["stage"] = "COUPON_SENT"
#         save_user_state(phone, state)
#         send_text_message(phone, reply)
#         latency_sec = round(time.time() - start_time, 2) if start_time else None
#         save_message(phone, "assistant", reply, response_time_sec=latency_sec)
#         log_message(phone, "ai", reply)
#         return

#     # 3. Prompt generation with active state context & RAG knowledge retrieval
#     full_answer_parts = []
#     for chunk_text in stream_rag(text, chat_history=history, state=state):
#         # full_answer_parts.append(str(chunk_text))
#         full_answer_parts.append(str(chunk_text))

#     full_reply = "".join(full_answer_parts).strip()

#     # 4. Check for low-confidence or fallback AI responses across 2-3 consecutive queries
#     low_conf_triggers = ["unable to process", "unable to answer", "i don't have information", "not sure", "sorry, the ai service"]
#     if any(trigger in full_reply.lower() for trigger in low_conf_triggers):
#         state["low_confidence_count"] = state.get("low_confidence_count", 0) + 1
#     else:
#         state["low_confidence_count"] = 0

#     # If AI has been unable to give clear answers for 2 or more consecutive messages, offer human agent support
#     if state.get("low_confidence_count", 0) >= 2:
#         full_reply += "\n\n💬 Would you like to speak directly with our support team? Please reply by typing **'agent'** or call us directly at **9898989898** to resolve your query!"

#     save_user_state(phone, state)
#     send_text_message(phone, full_reply)

#     # Calculate latency in seconds and save to Supabase
#     latency_sec = round(time.time() - start_time, 2) if start_time else None
#     print(f"[tasks] {phone}: AI reply generated & sent in {latency_sec}s")
#     save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
#     log_message(phone, "ai", full_reply)

# async def handle_ai_reply_async(phone: str, text: str, history: list, start_time: float = None):
#     """
#     Async AI reply generator: uses non-blocking ask_rag_async for high-concurrency LLM execution.
#     """
#     # Check for specific Ad pre-filled message trigger
#     if text.strip().lower() == "hello! can i get more info on yoga classes?":
#         reply = "Thanks this is our offer for our yoga classes"
#         send_text_message(phone, reply)
#         latency_sec = round(time.time() - start_time, 2) if start_time else None
#         save_message(phone, "assistant", reply, response_time_sec=latency_sec)
#         log_message(phone, "ai", reply)
#         return

#     state = extract_and_update_slots(phone, text)
#     is_q = is_user_asking_question(text)

#     if not is_q and state["stage"] == "READY_FOR_APP_LINK":
#         package = (state.get("package") or "3-month").lower()
#         fee = state.get("fee") or "₹1,750"
#         reply = (
#             f"Great choice! 😊 You've selected the {package} package for {fee}.\n\n"
#             "To proceed, you'll need to download the Sensationz App, through which you'll receive your special welcome discount coupon 🎁.\n\n"
#             "Please download the app here:\n\n"
#             "📱 Android: https://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n"
#             "🍎 iOS: https://apps.apple.com/us/app/sensationz/id6761418351\n\n"
#             "Once you've downloaded the app and created your profile, let me know here so I can activate your personalized welcome coupon!"
#         )
#         state["stage"] = "APP_LINK_SENT"
#         save_user_state(phone, state)
#         send_text_message(phone, reply)
#         latency_sec = round(time.time() - start_time, 2) if start_time else None
#         save_message(phone, "assistant", reply, response_time_sec=latency_sec)
#         log_message(phone, "ai", reply)
#         return

#     if state["stage"] == "PROFILE_COMPLETED" and not state.get("coupon_sent"):
#         reply = (
#             "🎉 Welcome to the Sensationz Yoga family! 🌸\n"
#             "Your app setup and profile are complete.\n\n"
#             "🎁 Your personalized welcome coupon code is: **SENSZAPP**\n\n"
#             "Use this coupon in the app to activate your discount. See you in class! 🧘‍♀️✨"
#         )
#         state["coupon_sent"] = True
#         state["stage"] = "COUPON_SENT"
#         save_user_state(phone, state)
#         send_text_message(phone, reply)
#         latency_sec = round(time.time() - start_time, 2) if start_time else None
#         save_message(phone, "assistant", reply, response_time_sec=latency_sec)
#         log_message(phone, "ai", reply)
#         return


#     rag_start = time.perf_counter()
#     full_reply = await ask_rag_async(text, chat_history=history, state=state)
#     full_reply = full_reply.strip()
#     rag_time = time.perf_counter() - rag_start
#     print(f"[TIMING] {phone} RAG: {rag_time:.2f}s")

#     low_conf_triggers = ["unable to process", "unable to answer", "i don't have information", "not sure", "sorry, the ai service"]
#     if any(trigger in full_reply.lower() for trigger in low_conf_triggers):
#         state["low_confidence_count"] = state.get("low_confidence_count", 0) + 1
#     else:
#         state["low_confidence_count"] = 0

#     if state.get("low_confidence_count", 0) >= 2:
#         full_reply += "\n\n💬 Would you like to speak directly with our support team? Please reply by typing **'agent'** or call us directly at **9898989898** to resolve your query!"
#     save_user_state(phone, state)
#     send_text_message(phone, full_reply)

#     latency_sec = round(time.time() - start_time, 2) if start_time else None
#     print(f"[tasks-async] {phone}: AI reply generated & sent in {latency_sec}s")
#     save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
#     log_message(phone, "ai", full_reply)


# async def process_incoming_message_async(phone: str, text: str, referral: dict = None):
#     """
#     Non-blocking async task worker function.
#     Acquires per-user distributed lock and runs async AI reply pipeline.
#     """
#     start_time = time.time()
#     lock = redis_conn.lock(f"phone-lock:{phone}", timeout=60, blocking_timeout=15)

#     acquired = lock.acquire(blocking=True)
#     if not acquired:
#         print(f"[tasks-async] Could not acquire lock for {phone} in time -- skipping.")
#         return

#     try:
#         # Determine if target ad or message matched
#         is_target = is_target_ad_or_message(text, referral)

#         if not is_target:
#             print(f"[tasks-async] {phone}: Ad/Message not targeted. AI ignores and chat remains unassigned.")
#             return

#         try:
#             history = get_recent_history(phone)
#         except Exception as e:
#             print(f"[tasks-async] Failed to fetch history for {phone}: {e}")
#             history = []

#         try:
#             save_message(phone, "user", text)
#             log_message(phone, "user", text)
#         except Exception as e:
#             print(f"[tasks-async] Failed to save incoming message for {phone}: {e}")

#         if is_escalated(phone):
#             print(f"{phone} is already escalated — bot staying out of it.")
#             return

#         if PRIORITY_AGENT_EMAIL:
#             print(f"[tasks-async] Assigning chat for {phone} to target priority agent: {PRIORITY_AGENT_EMAIL}")
#             assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

#         text_lower = text.lower()

#         if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
#             handle_agent_handoff(phone, start_time)
#             return
        
#         await handle_ai_reply_async(phone, text, history, start_time)

#     finally:
#         try:
#             lock.release()
#         except Exception:
#             pass

