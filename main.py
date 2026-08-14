"""
main.py — the FastAPI app and message-handling flow. Talks to Interakt
only through interakt.py, never directly, so this file stays focused
on DECISIONS (what to do with a message) rather than API mechanics.
"""

import os
import time
import asyncio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from interakt import (
    send_text_message,
    assign_chat_to_agent,
    verify_webhook_signature,
)
from csv_logger import log_message
from chat_history import save_message, get_recent_history, get_full_history_for_agent
from rag import ask_rag_async
from chat_state import (
    mark_escalated,
    is_escalated,
    get_user_state,
    save_user_state,
    extract_and_update_slots,
    is_user_asking_question,
)
from batching import add_message_to_batch_async
from redis_client import get_redis_connection
from tasks import is_target_ad_or_message

load_dotenv()

app = FastAPI()

# --- Config -------------------------------------------------------
# Every message gets assigned to this agent by default.
PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")

# If the customer explicitly asks for a human, the chat is RE-assigned
# to this agent instead, and the AI reply is skipped.
PRIORITY_AGENT_EMAIL_ANOTHER_1 = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER_1")
PRIORITY_AGENT_EMAIL_ANOTHER_2 = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER_2")

AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]

TARGET_MESSAGE_TEXT = os.getenv("TARGET_MESSAGE_TEXT", "Hello! Can I get more info on Yoga classes?")

# --- Endpoints ------------------------------------------------------
TARGET_AD_ID = os.getenv("TARGET_AD_ID")

@app.get("/chat-history/{phone}")
async def view_chat_history(phone: str):
    """GET /chat-history/919876543210 — full conversation for a customer."""
    return get_full_history_for_agent(phone)

@app.post("/webhook")
async def receive_interakt_webhook(request: Request):
    start_time = time.perf_counter()
    raw_body = await request.body()
    data = await request.json()

    signature = request.headers.get("Interakt-Signature", "")
    if not verify_webhook_signature(raw_body, signature):
        print("[main.py] Signature verification FAILED — ignoring request.")
        return {"status": "invalid signature"}

    event_type = data.get("type")
    print(f"[main.py] Event type: {event_type}")

    if event_type != "message_received":
        return {"status": "ignored, not a new message"}

    # -------------------------------------------------------------------------
    # STEP 1: Extract customer details & message content from Interakt webhook payload
    # -------------------------------------------------------------------------
    customer = data["data"]["customer"]
    message = data["data"]["message"]

    country_code = str(customer.get("country_code", "")).replace("+", "").strip()
    phone_num = str(customer.get("phone_number") or customer.get("phoneNumber") or "").strip().replace("+", "")

    if phone_num.startswith(country_code) and country_code:
        phone = phone_num
    elif country_code and phone_num:
        phone = country_code + phone_num
    else:
        phone = phone_num or str(customer.get("channel_phone_number", ""))

    text = message.get("message", "")
    referral = message.get("referral", {})

    print(f"[main.py] Message from {phone}: {text}")
    log_message(phone, "user", text)

    # Enqueue message into async batch queue for processing
    try:
        await add_message_to_batch_async(phone, text, referral=referral)
        elapsed = round((time.perf_counter() - start_time) * 1000, 1)
        print(f"[main.py] {phone}: enqueued in {elapsed}ms")
        return {"status": "ok", "message": "enqueued"}
    except Exception as e:
        print(f"[main.py] Batch enqueueing failed ({e}) — processing synchronously as fallback.")
        return await _process_fallback(phone, text, referral, start_time)


@app.post("/test-webhook")
async def receive_test_webhook(request: Request):
    """
    Test endpoint for automated full-flow load testing.
    Accepts Interakt-compliant JSON payload but skips Facebook/Interakt signature verification.
    """
    start_time = time.perf_counter()
    data = await request.json()
    event_type = data.get("type")

    if event_type != "message_received":
        return {"status": "ignored, not a new message"}

    customer = data.get("data", {}).get("customer", {})
    message = data.get("data", {}).get("message", {})

    country_code = str(customer.get("country_code", "")).replace("+", "").strip()
    phone_num = str(customer.get("phone_number") or customer.get("phoneNumber") or "").strip().replace("+", "")

    if phone_num.startswith(country_code) and country_code:
        phone = phone_num
    elif country_code and phone_num:
        phone = country_code + phone_num
    else:
        phone = phone_num or str(customer.get("channel_phone_number", ""))

    text = message.get("message", "")
    referral = message.get("referral", {})

    print(f"[test-webhook] Message from {phone}: {text} | Referral: {referral}")
    log_message(phone, "user", text)

    try:
        await add_message_to_batch_async(phone, text, referral=referral)
        elapsed = round((time.perf_counter() - start_time) * 1000, 1)
        print(f"[test-webhook] {phone}: enqueued in {elapsed}ms")
        return {"status": "ok", "message": "enqueued"}
    except Exception as e:
        print(f"[test-webhook] Batch enqueueing failed ({e}) — processing synchronously as fallback.")
        return await _process_fallback(phone, text, referral, start_time)


# --- Fallback sync processing (only if batching/Redis fails) ------
async def _process_fallback(phone: str, text: str, referral: dict, start_time: float):
    """Synchronous fallback when Redis batch enqueue fails."""
    try:
        history = get_recent_history(phone)
    except Exception as ex:
        print(f"[main.py] Failed to fetch history for {phone}: {ex}")
        history = []

    is_target = is_target_ad_or_message(text, referral, phone)
    if not is_target:
        print(f"[main.py] {phone}: Ad/Message not targeted. AI ignores and chat remains unassigned.")
        return {"status": "ignored, not matching target ad"}

    # Persist target flag so subsequent messages skip the check
    try:
        state = get_user_state(phone)
        if not state.get("is_target_ad"):
            state["is_target_ad"] = True
            save_user_state(phone, state)
    except Exception as ex:
        print(f"[main.py] Failed to save target flag for {phone}: {ex}")

    try:
        save_message(phone, "user", text)
    except Exception as ex:
        print(f"[main.py] Failed to save incoming message for {phone}: {ex}")

    if is_escalated(phone):
        print(f"[main.py] {phone} is already escalated — bot staying out of it.")
        return {"status": "escalated, bot not responding"}

    if PRIORITY_AGENT_EMAIL:
        print(f"[main.py] Assigning chat for {phone} to target priority agent: {PRIORITY_AGENT_EMAIL}")
        assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

    text_lower = text.lower()

    if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
        return _handle_agent_handoff(phone)

    # Check for static offer reply first (pre-written ad response)
    if text.strip().lower() == TARGET_MESSAGE_TEXT.strip().lower():
        reply = "Hi Sir/Mam, Welcome to Sensationz Media and arts, How i can help u?"
        send_text_message(phone, reply)
        save_message(phone, "assistant", reply)
        log_message(phone, "ai", reply)
        return {"status": "offer sent"}

    return await _handle_ai_reply_fallback(phone, text, history)


def _handle_agent_handoff(phone: str):
    """Handles customer request to talk to a human representative."""
    print(f"[main.py] Agent requested by {phone} — re-assigning to escalation agent...")

    reply = "Got it — connecting you with our team now. Someone will be with you shortly!"

    if PRIORITY_AGENT_EMAIL_ANOTHER_1:
        assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL_ANOTHER_1)
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
    return {"status": "handed off to agent"}


async def _handle_ai_reply_fallback(phone: str, text: str, history: list):
    """Fallback AI reply using async RAG."""
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
        save_message(phone, "assistant", reply)
        log_message(phone, "ai", reply)
        return {"status": "app link sent"}

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
        return {"status": "coupon sent"}

    # RAG AI reply
    full_reply = await ask_rag_async(text, chat_history=history, state=state)
    send_text_message(phone, full_reply)
    save_message(phone, "assistant", full_reply)
    log_message(phone, "ai", full_reply)
    return {"status": "ok"}
