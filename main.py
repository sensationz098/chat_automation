# """
# main.py — the FastAPI app and message-handling flow. Talks to Interakt
# only through interakt.py, never directly, so this file stays focused
# on DECISIONS (what to do with a message) rather than API mechanics.
# """

# import os
# from fastapi import FastAPI, Request
# from fastapi.middleware.cors import CORSMiddleware
# from dotenv import load_dotenv

# from interakt import (
#     send_text_message,
#     send_image_message,
#     assign_chat_to_agent,
#     verify_webhook_signature,
# )
# from csv_logger import log_message
# from chat_history import save_message, get_recent_history, get_full_history_for_agent
# from rag import stream_rag
# from chat_state import mark_escalated, is_escalated

# load_dotenv()

# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],   # fine for local testing; restrict before real production use
#     allow_methods=["*"],
#     allow_headers=["*"],
# )
# # 917361045453 917678368328. 919871303310, 918595024275
# # --- Config -------------------------------------------------------
# ALLOWED_TEST_NUMBERS = os.getenv("ALLOWED_TEST_NUMBERS")

# # Every message gets assigned to this agent by default.
# PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")

# # If the customer explicitly asks for a human, the chat is RE-assigned
# # to this agent instead, and the AI reply is skipped.
# PRIORITY_AGENT_EMAIL_ANOTHER = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER")

# AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]

# # PAYMENT_TRIGGER_WORDS = ["pay", "payment", "qr", "upi", "how to pay", "send qr", "payment link", "checkout"]
# PAYMENT_QR_IMAGE_URL = os.getenv("PAYMENT_QR_IMAGE_URL")
# PAYMENT_CAPTION = """Bank details:\n Sensationz media and arts pvt ltd \n Ac no 051863300000382"""

# # --- Endpoints ------------------------------------------------------


# @app.get("/chat-history/{phone}")
# async def view_chat_history(phone: str):
#     """GET /chat-history/919876543210 — full conversation for a customer."""
#     return get_full_history_for_agent(phone)


# @app.post("/webhook")
# async def receive_interakt_webhook(request: Request):
#     raw_body = await request.body()
#     data = await request.json()

#     signature = request.headers.get("Interakt-Signature", "")
#     if not verify_webhook_signature(raw_body, signature):
#         print("Signature verification FAILED — ignoring request.")
#         return {"status": "invalid signature"}

#     event_type = data.get("type")
#     print("Event type:", event_type)

#     if event_type != "message_received":
#         return {"status": "ignored, not a new message"}

#     customer = data["data"]["customer"]
#     message = data["data"]["message"]
#     phone = customer["channel_phone_number"]
#     text = message.get("message", "")

#     print(f"Message from {phone}: {text}")
#     log_message(phone, "user", text)
#     if ALLOWED_TEST_NUMBERS and phone != ALLOWED_TEST_NUMBERS and phone != "917361045453":
#         print(f"Ignoring message from {phone} — not the allowed test number.")
#         return {"status": "ignored, not test number"}

#     save_message(phone, "user", text)
#     log_message(phone, "user", text)

#     # If this chat was already escalated to a human, the bot stops
#     # touching it entirely from here on.
#     if is_escalated(phone):
#         print(f"{phone} is already escalated — bot staying out of it.")
#         return {"status": "escalated, bot not responding"}
    
#     if PRIORITY_AGENT_EMAIL:
#         assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

#     text_lower = text.lower()

#     # --- Intent 1: explicit human handoff -----------------------
#     if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
#         return handle_agent_handoff(phone)

#     # --- Intent 2: payment -----------------------------------------
#     # if any(word in text_lower for word in PAYMENT_TRIGGER_WORDS):
#     #     return handle_payment_intent(phone)

#     # --- Default: AI reply, with conversation memory ----------------
#     return handle_ai_reply(phone, text)


# # --- Intent handlers --------------------------------------------------

# def handle_agent_handoff(phone: str):
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

#     save_message(phone, "assistant", reply)
#     return {"status": "handed off to agent"}


# # def handle_payment_intent(phone: str):
# #     print(f"Payment intent detected from {phone} — sending QR code.")

# #     if not PAYMENT_QR_IMAGE_URL:
# #         reply = "You can complete payment at checkout — let me know if you'd like help with anything else!"
# #         send_text_message(phone, reply)
# #         save_message(phone, "assistant", reply)
# #         return {"status": "payment intent, no QR configured"}

# #     send_image_message(phone, PAYMENT_QR_IMAGE_URL, PAYMENT_CAPTION)
# #     save_message(phone, "assistant", f"[sent payment QR image] {PAYMENT_CAPTION}")
#     # return {"status": "payment QR sent"}


# def handle_ai_reply(phone: str, text: str):
#     history = get_recent_history(phone)

#     full_answer_parts = []
#     for chunk_text in stream_rag(text, chat_history=history):
#         send_text_message(phone, chunk_text)
#         full_answer_parts.append(chunk_text)

#     save_message(phone, "assistant", " ".join(full_answer_parts))
#     return {"status": "ok"}


"""
main.py — the FastAPI app and message-handling flow. Talks to Interakt
only through interakt.py, never directly, so this file stays focused
on DECISIONS (what to do with a message) rather than API mechanics.
"""

import os
import asyncio
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from redis import Redis
from rq import Queue

from interakt import (
    send_text_message,
    assign_chat_to_agent,
    verify_webhook_signature,
)
from csv_logger import log_message
from chat_history import save_message, get_recent_history, get_full_history_for_agent
from rag import stream_rag
from chat_state import (
    mark_escalated,
    is_escalated,
    get_user_state,
    save_user_state,
    extract_and_update_slots,
    is_user_asking_question,
)
from batching import add_message_to_batch
from upstash_redis import Redis
from redis_client import get_redis_connection
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # fine for local testing; restrict before real production use
    allow_methods=["*"],
    allow_headers=["*"],
)

redis_conn = get_redis_connection()
job_queue = Queue("interakt_messages", connection=redis_conn)

_phone_locks: dict[str, asyncio.Lock] = {}


def _get_lock_for_phone(phone: str) -> asyncio.Lock:
    if phone not in _phone_locks:
        _phone_locks[phone] = asyncio.Lock()
    return _phone_locks[phone]

# --- Config -------------------------------------------------------
ALLOWED_TEST_NUMBERS = os.getenv("ALLOWED_TEST_NUMBERS")

# Every message gets assigned to this agent by default.
PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")

# If the customer explicitly asks for a human, the chat is RE-assigned
# to this agent instead, and the AI reply is skipped.
PRIORITY_AGENT_EMAIL_ANOTHER = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER")

AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]

PAYMENT_QR_IMAGE_URL = os.getenv("PAYMENT_QR_IMAGE_URL")
PAYMENT_CAPTION = """Bank details:\n Sensationz media and arts pvt ltd \n Ac no 051863300000382"""

# --- Endpoints ------------------------------------------------------

@app.get("/chat-history/{phone}")
async def view_chat_history(phone: str):
    """GET /chat-history/919876543210 — full conversation for a customer."""
    return get_full_history_for_agent(phone)


@app.post("/webhook")
async def receive_interakt_webhook(request: Request):
    raw_body = await request.body()
    data = await request.json()

    signature = request.headers.get("Interakt-Signature", "")
    if not verify_webhook_signature(raw_body, signature):
        print("Signature verification FAILED — ignoring request.")
        return {"status": "invalid signature"}

    event_type = data.get("type")
    print("Event type:", event_type)

    if event_type != "message_received":
        return {"status": "ignored, not a new message"}

    # -------------------------------------------------------------------------
    # STEP 1: Extract customer details & message content from Interakt webhook payload
    # -------------------------------------------------------------------------
    customer = data["data"]["customer"]  # Dictionary containing customer profile from Interakt
    message = data["data"]["message"]    # Dictionary containing message payload details
    
    # Extract country code (e.g., "91" or "+91") and strip any plus signs for standard formatting
    country_code = str(customer.get("country_code", "")).replace("+", "").strip()
    
    # Extract customer's personal phone number (e.g., "9876543210" or "919876543210")
    phone_num = str(customer.get("phone_number") or customer.get("phoneNumber") or "").strip().replace("+", "")
    
    # Build unique phone string with country code so each user has an isolated chat history & state
    if phone_num.startswith(country_code) and country_code:
        # Phone number already includes country code (e.g. "919876543210")
        phone = phone_num
    elif country_code and phone_num:
        # Combine country code and local number (e.g. "91" + "9876543210" -> "919876543210")
        phone = country_code + phone_num
    else:
        # Fallback to phone_num or channel_phone_number if format differs in webhook
        phone = phone_num or str(customer.get("channel_phone_number", ""))
    
    # Extract text content sent by the customer
    text = message.get("message", "")

    # Print log message showing exact sender phone number and message content
    print(f"Message from {phone}: {text}")
    # Log incoming user message to local CSV file under customer's unique phone number
    log_message(phone, "user", text)

    # Optional test environment filter: if ALLOWED_TEST_NUMBERS is set, ignore other numbers
    if ALLOWED_TEST_NUMBERS and phone != ALLOWED_TEST_NUMBERS and phone != "917361045453":
        print(f"Ignoring message from {phone} — not the allowed test number.")
        return {"status": "ignored, not test number"}

    # Enqueue message into Redis batch queue for async worker execution (handles 500+ concurrent requests)
    try:
        add_message_to_batch(phone, text)
        return {"status": "ok", "message": "enqueued"}
    except Exception as e:
        print(f"[main.py] Redis enqueueing failed ({e}) — processing synchronously as fallback.")
        lock = _get_lock_for_phone(phone)
        async with lock:
            try:
                history = get_recent_history(phone)
            except Exception as ex:
                print(f"[main.py] Failed to fetch history for {phone}: {ex}")
                history = []

            try:
                save_message(phone, "user", text)
            except Exception as ex:
                print(f"[main.py] Failed to save incoming message for {phone}: {ex}")

            if is_escalated(phone):
                print(f"{phone} is already escalated — bot staying out of it.")
                return {"status": "escalated, bot not responding"}

            if PRIORITY_AGENT_EMAIL:
                assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

            text_lower = text.lower()

            if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
                return handle_agent_handoff(phone)

            return handle_ai_reply(phone, text, history)

# --- Intent handlers --------------------------------------------------

def handle_agent_handoff(phone: str):
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
    return {"status": "handed off to agent"}


def handle_ai_reply(phone: str, text: str, history: list):
    # 1. Update session state & extract slots from user text
    state = extract_and_update_slots(phone, text)
    is_q = is_user_asking_question(text)

    # 2. Check deterministic state guards (ONLY if customer is NOT asking an informational question)
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

    # 3. Prompt generation with active state context & smart RAG
    full_answer_parts = []
    for chunk_text in stream_rag(text, chat_history=history, state=state):
        send_text_message(phone, chunk_text)
        full_answer_parts.append(chunk_text)

    full_reply = " ".join(full_answer_parts)
    save_message(phone, "assistant", full_reply)
    log_message(phone, "ai", full_reply)
    return {"status": "ok"}

