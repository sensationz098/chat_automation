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

# Import RAG streaming generator
from rag import stream_rag

from redis_client import get_redis_connection

# Load environment variables
load_dotenv()

redis_conn = get_redis_connection()
# Default and escalation agent emails configured in environment variables
PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")
PRIORITY_AGENT_EMAIL_ANOTHER = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER")

# Words that trigger immediate human agent handoff
AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]

# Payment details configuration
PAYMENT_QR_IMAGE_URL = os.getenv("PAYMENT_QR_IMAGE_URL")
PAYMENT_CAPTION = """Bank details:\n Sensationz media and arts pvt ltd \n Ac no 051863300000382"""


def process_incoming_message(phone: str, text: str):
    """
    Background worker job: pulled off the interakt_messages Redis queue by worker processes.
    Acquires a Redis lock for this specific customer's phone number so out-of-order execution
    cannot corrupt conversation history. Each unique phone number gets independent execution.
    """
    # Create Redis lock keyed to customer's unique phone number
    lock = redis_conn.lock(f"phone-lock:{phone}", timeout=30, blocking_timeout=15)

    # Acquire distributed lock
    acquired = lock.acquire(blocking=True)
    if not acquired:
        print(f"[tasks] Could not acquire lock for {phone} in time -- skipping this job.")
        return

    try:
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
            assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

        text_lower = text.lower()

        # 5. Check if customer requested a human agent
        if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
            handle_agent_handoff(phone)
            return

        # 6. Generate and send AI response using session state & RAG knowledge
        handle_ai_reply(phone, text, history)

    finally:
        # Always release Redis lock after processing completes
        try:
            lock.release()
        except Exception:
            pass   # Lock timeout safety fallback


def handle_agent_handoff(phone: str):
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

    save_message(phone, "assistant", reply)


def handle_ai_reply(phone: str, text: str, history: list):
    """
    Generates AI response using session state slots, state guards, and RAG knowledge base.
    """
    # 1. Update session state & extract batch timing / package slots from user input
    state = extract_and_update_slots(phone, text)
    is_q = is_user_asking_question(text)

    # 2. Check deterministic state guards (ONLY if customer is NOT asking an informational question/video request)
    if not is_q and state["stage"] == "READY_FOR_APP_LINK":
        timing = state.get("timing") or "your selected"
        package = state.get("package") or "your selected"
        fee = state.get("fee") or ""
        reply = (
            f"🎉 Great choice! You're all set for the {timing} batch on the {package} package ({fee})! 🌸\n\n"
            "To confirm your enrollment, you must download the Sensationz App. Through the app, you will also receive your special welcome discount coupon! 🎁\n\n"
            "Please download the Sensationz App here:\n\n"
            "📱 Android:\nhttps://play.google.com/store/apps/details?id=com.sensationz.sensationz.dev\n\n"
            "🍎 iOS:\nhttps://apps.apple.com/us/app/sensationz/id6761418351\n\n"
            "Please download the app and create your profile. Once done, let me know here and I'll activate your personalized welcome coupon 🎁"
        )
        state["stage"] = "APP_LINK_SENT"
        save_user_state(phone, state)
        send_text_message(phone, reply)
        save_message(phone, "assistant", reply)
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
        save_message(phone, "assistant", reply)
        log_message(phone, "ai", reply)
        return

    # 3. Prompt generation with active state context & RAG knowledge retrieval
    full_answer_parts = []
    for chunk_text in stream_rag(text, chat_history=history, state=state):
        send_text_message(phone, chunk_text)
        full_answer_parts.append(chunk_text)

    # Save and log complete assistant reply
    full_reply = " ".join(full_answer_parts)
    save_message(phone, "assistant", full_reply)
    log_message(phone, "ai", full_reply)