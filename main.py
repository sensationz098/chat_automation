"""
main.py — FastAPI webhook entry point.

Receives Interakt webhooks, extracts phone/message/referral,
enqueues into the batching debouncer for async processing.
The fallback path also uses fully async I/O.
"""

import os
import time
import asyncio
from fastapi import FastAPI, Request
from dotenv import load_dotenv

from interakt import (
    send_text_message_async,
    assign_chat_to_agent_async,
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
    matches_any,
    advance_stage,
)
from batching import add_message_to_batch_async
from tasks import is_target_ad_or_message, get_next_agent_email

load_dotenv()

app = FastAPI()

AGENT_TRIGGER_WORDS = ["agent", "human", "talk to someone", "real person", "representative", "support"]
TARGET_MESSAGE_TEXT = os.getenv("TARGET_MESSAGE_TEXT", "Hello! Can I get more info on Yoga classes?")


# ---------------------------------------------------------------------------
# Helper: extract phone from webhook payload
# ---------------------------------------------------------------------------
def _extract_phone(customer: dict) -> str:
    country_code = str(customer.get("country_code", "")).replace("+", "").strip()
    phone_num = str(customer.get("phone_number") or customer.get("phoneNumber") or "").strip().replace("+", "")
    if phone_num.startswith(country_code) and country_code:
        return phone_num
    elif country_code and phone_num:
        return country_code + phone_num
    return phone_num or str(customer.get("channel_phone_number", ""))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/chat-history/{phone}")
async def view_chat_history(phone: str):
    return get_full_history_for_agent(phone)


@app.post("/webhook")
async def receive_interakt_webhook(request: Request):
    start_time = time.perf_counter()
    raw_body = await request.body()
    data = await request.json()

    signature = request.headers.get("Interakt-Signature", "")
    if not verify_webhook_signature(raw_body, signature):
        print("[main] Signature verification FAILED")
        return {"status": "invalid signature"}

    event_type = data.get("type")
    if event_type != "message_received":
        return {"status": "ignored"}

    customer = data["data"]["customer"]
    message = data["data"]["message"]
    phone = _extract_phone(customer)
    text = message.get("message", "")
    referral = message.get("referral", {})

    print(f"[main] Message from {phone}: {text}")
    log_message(phone, "user", text)

    try:
        await add_message_to_batch_async(phone, text, referral=referral)
        elapsed = round((time.perf_counter() - start_time) * 1000, 1)
        print(f"[main] {phone}: enqueued in {elapsed}ms")
        return {"status": "ok", "message": "enqueued"}
    except Exception as e:
        print(f"[main] Batch enqueue failed ({e}) — fallback")
        return await _process_fallback(phone, text, referral, time.time())


@app.post("/test-webhook")
async def receive_test_webhook(request: Request):
    """Test endpoint — skips signature verification."""
    start_time = time.perf_counter()
    data = await request.json()
    if data.get("type") != "message_received":
        return {"status": "ignored"}

    customer = data.get("data", {}).get("customer", {})
    message = data.get("data", {}).get("message", {})
    phone = _extract_phone(customer)
    text = message.get("message", "")
    referral = message.get("referral", {})

    print(f"[test-webhook] {phone}: {text}")
    log_message(phone, "user", text)

    try:
        await add_message_to_batch_async(phone, text, referral=referral)
        elapsed = round((time.perf_counter() - start_time) * 1000, 1)
        print(f"[test-webhook] {phone}: enqueued in {elapsed}ms")
        return {"status": "ok", "message": "enqueued"}
    except Exception as e:
        print(f"[test-webhook] Batch enqueue failed ({e}) — fallback")
        return await _process_fallback(phone, text, referral, time.time())


# ---------------------------------------------------------------------------
# Fallback (only used if Redis/batching fails — fully async)
# ---------------------------------------------------------------------------
async def _process_fallback(phone: str, text: str, referral: dict, start_time: float):
    is_target = is_target_ad_or_message(text, referral, phone)
    if not is_target:
        return {"status": "ignored, not matching target ad"}

    try:
        state = get_user_state(phone)
        if not state.get("is_target_ad"):
            state["is_target_ad"] = True
            save_user_state(phone, state)
    except Exception:
        pass

    try:
        save_message(phone, "user", text)
    except Exception:
        pass

    if is_escalated(phone):
        return {"status": "escalated"}

    # Round-robin agent assignment (async)
    agent = get_next_agent_email()
    if agent:
        await assign_chat_to_agent_async(phone, agent)

    text_lower = text.lower()
    if matches_any(text_lower, AGENT_TRIGGER_WORDS):
        reply = "Got it — connecting you with our team now. Someone will be with you shortly!"
        await send_text_message_async(phone, reply)
        mark_escalated(phone)
        save_message(phone, "assistant", reply)
        return {"status": "handed off"}

    if text.strip().lower() == TARGET_MESSAGE_TEXT.strip().lower():
        reply = "Hi Sir/Mam, Welcome to Sensationz Media and arts, How i can help u?"
        await send_text_message_async(phone, reply)
        save_message(phone, "assistant", reply)
        return {"status": "offer sent"}

    try:
        history = get_recent_history(phone)
    except Exception:
        history = []

    state = extract_and_update_slots(phone, text)
    full_reply = await ask_rag_async(text, chat_history=history, state=state)
    
    # Post-LLM State Transitions
    if state.get("stage") == "READY_FOR_APP_LINK":
        state["stage"] = advance_stage(state["stage"], "APP_LINK_SENT")
        save_user_state(phone, state)
    elif state.get("stage") == "PROFILE_COMPLETED" and not state.get("coupon_sent"):
        state["coupon_sent"] = True
        state["stage"] = advance_stage(state["stage"], "COUPON_SENT")
        save_user_state(phone, state)

    await send_text_message_async(phone, full_reply)
    latency_sec = round(time.time() - start_time, 2)
    save_message(phone, "assistant", full_reply, response_time_sec=latency_sec)
    log_message(phone, "ai", full_reply)
    return {"status": "ok"}
