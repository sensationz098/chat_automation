"""
main.py — the FastAPI app and message-handling flow. Talks to Interakt
only through interakt.py, never directly, so this file stays focused
on DECISIONS (what to do with a message) rather than API mechanics.
"""

import os
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from interakt import (
    send_text_message,
    send_image_message,
    assign_chat_to_agent,
    verify_webhook_signature,
)
from csv_logger import log_message
from chat_history import save_message, get_recent_history, get_full_history_for_agent
from rag import stream_rag
from chat_state import mark_escalated, is_escalated

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # fine for local testing; restrict before real production use
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Config -------------------------------------------------------
ALLOWED_TEST_NUMBERS = os.getenv("ALLOWED_TEST_NUMBERS")

# Every message gets assigned to this agent by default.
PRIORITY_AGENT_EMAIL = os.getenv("PRIORITY_AGENT_EMAIL")

# If the customer explicitly asks for a human, the chat is RE-assigned
# to this agent instead, and the AI reply is skipped.
PRIORITY_AGENT_EMAIL_ANOTHER = os.getenv("PRIORITY_AGENT_EMAIL_ANOTHER")

AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]

# PAYMENT_TRIGGER_WORDS = ["pay", "payment", "qr", "upi", "how to pay", "send qr", "payment link", "checkout"]
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

    customer = data["data"]["customer"]
    message = data["data"]["message"]
    phone = customer["channel_phone_number"]
    text = message.get("message", "")

    print(f"Message from {phone}: {text}")
    log_message(phone, "user", text)
    if ALLOWED_TEST_NUMBERS and phone != ALLOWED_TEST_NUMBERS and phone != "917361045453":
        print(f"Ignoring message from {phone} — not the allowed test number.")
        return {"status": "ignored, not test number"}

    save_message(phone, "user", text)
    log_message(phone, "user", text)

    # If this chat was already escalated to a human, the bot stops
    # touching it entirely from here on.
    if is_escalated(phone):
        print(f"{phone} is already escalated — bot staying out of it.")
        return {"status": "escalated, bot not responding"}
    
    if PRIORITY_AGENT_EMAIL:
        assign_chat_to_agent(phone, PRIORITY_AGENT_EMAIL)

    text_lower = text.lower()

    # --- Intent 1: explicit human handoff -----------------------
    if any(word in text_lower for word in AGENT_TRIGGER_WORDS):
        return handle_agent_handoff(phone)

    # --- Intent 2: payment -----------------------------------------
    # if any(word in text_lower for word in PAYMENT_TRIGGER_WORDS):
    #     return handle_payment_intent(phone)

    # --- Default: AI reply, with conversation memory ----------------
    return handle_ai_reply(phone, text)


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


# def handle_payment_intent(phone: str):
#     print(f"Payment intent detected from {phone} — sending QR code.")

#     if not PAYMENT_QR_IMAGE_URL:
#         reply = "You can complete payment at checkout — let me know if you'd like help with anything else!"
#         send_text_message(phone, reply)
#         save_message(phone, "assistant", reply)
#         return {"status": "payment intent, no QR configured"}

#     send_image_message(phone, PAYMENT_QR_IMAGE_URL, PAYMENT_CAPTION)
#     save_message(phone, "assistant", f"[sent payment QR image] {PAYMENT_CAPTION}")
    return {"status": "payment QR sent"}


def handle_ai_reply(phone: str, text: str):
    history = get_recent_history(phone)

    full_answer_parts = []
    for chunk_text in stream_rag(text, chat_history=history):
        send_text_message(phone, chunk_text)
        full_answer_parts.append(chunk_text)

    save_message(phone, "assistant", " ".join(full_answer_parts))
    return {"status": "ok"}

